"""Supervised cloud execution with a human in the loop.

Agents can run scripts and Spark jobs against real environments (Snowflake,
Databricks, …). That breaks Studio's read-only invariant, so every such job
passes a SUPERVISOR AGENT before it runs:

    agent submits a job ─▶ supervisor reviews ─▶ approve | reject | needs-human
                                                     │            │        │
                                                  execute      blocked   wait for
                                                     │                   a human
                                              success / retry
                                                     │
                                    repeated failure ─▶ escalate ─▶ human in the loop

Policy: read-only statements the user's role may run are auto-approved. Writes,
DDL, and Spark jobs are high-risk and require a human (admin) to approve before
they execute. On execution failure the job retries; after repeated failures it
is ESCALATED — the requester is emailed and a human must approve a retry or
abort it. An LLM supervisor, when a key is present, adds a written risk review;
it advises, but policy — not the model — decides whether a write runs.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agent, db, email_service, governance, queryguard, rbac
from .auth import current_user
from .catalog import _connector_or_400

router = APIRouter(prefix="/jobs", tags=["jobs"])

MAX_RETRIES = 2            # automatic retries before escalating to a human
_WRITE = queryguard.FORBIDDEN  # DML/DDL keyword detector, reused from the guard


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS supervised_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            requester_role TEXT,
            requester_email TEXT,
            kind TEXT NOT NULL,
            target TEXT NOT NULL,
            script TEXT NOT NULL,
            risk TEXT,
            status TEXT NOT NULL,
            supervisor_decision TEXT,
            supervisor_reasons TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 2,
            last_error TEXT,
            result TEXT,
            human_by TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_user ON supervised_jobs(user_id, updated_at DESC);
        """
    )
    c.commit()
    c.close()


# ── Risk classification + supervisor agent ──────────────────────────────

def classify(kind, script):
    if kind == "spark_job":
        return "job"
    # A SQL script: 'write' if any statement is not a plain SELECT/CTE.
    for stmt in _statements(script):
        low = stmt.lstrip().lower()
        if not (low.startswith("select") or low.startswith("with")) or _WRITE.search(stmt):
            return "write"
    return "read"


def _statements(script):
    return [s.strip() for s in (script or "").split(";") if s.strip()]


def supervise(kind, target, script, user):
    """The supervisor agent's verdict on a job. Returns
    {decision: approve|reject|needs_human, risk, reasons}."""
    reasons = []
    # RBAC first: you cannot run anything against a source you can't reach.
    if not rbac.can_access(user["role"], target, "*"):
        return {"decision": "reject", "risk": classify(kind, script),
                "reasons": [f"Your role has no access to {target}."]}

    risk = classify(kind, script)

    # Optional LLM review — advisory only. It can flag concerns but can NEVER
    # turn a write into an auto-approval; policy below still gates.
    if agent.llm_available(agent.llm_spec(), user):
        try:
            reasons += _llm_review(kind, target, script, user)
        except Exception:
            pass

    if risk == "read":
        return {"decision": "approve", "risk": risk,
                "reasons": reasons + ["Read-only; auto-approved."]}
    label = "Spark job" if risk == "job" else "write / DDL"
    return {"decision": "needs_human", "risk": risk,
            "reasons": reasons + [f"{label} on {target} — requires human approval before it runs."]}


def _llm_review(kind, target, script, user):
    llm = agent.make_llm(agent.llm_spec(), user)
    sys = ("You are a data-platform supervisor. Review a job an agent wants to "
           "run against a production environment. Reply with ONE short sentence "
           "flagging any risk (data loss, scope, cost) or 'looks routine'. No prose.")
    reply = llm.invoke([("system", sys),
                        ("user", f"kind={kind} target={target}\n{script[:2000]}")])
    text = reply.content if isinstance(reply.content, str) else "".join(
        b.get("text", "") for b in reply.content if isinstance(b, dict))
    text = text.strip()
    return [f"Supervisor review: {text}"] if text else []


# ── Execution + retry + escalation ──────────────────────────────────────

def _requester(job):
    return {"id": job["user_id"], "role": job["requester_role"],
            "email": job["requester_email"], "name": job["requester_email"]}


def _execute(job):
    """Run the job against its environment. Raises on any failure."""
    user = _requester(job)
    target = job["target"]
    conn = _connector_or_400(target)
    if not conn.configured():
        raise RuntimeError(f"The {target} environment is not connected — configure its credentials.")

    if job["kind"] == "spark_job":
        return conn.submit_spark_job(json.loads(job["script"]))

    allowed = rbac.allowed_tables(user["role"], target, conn.list_tables())
    out = []
    for stmt in _statements(job["script"]):
        low = stmt.lstrip().lower()
        if low.startswith("select") or low.startswith("with"):
            cleaned = queryguard.enforce_limit(queryguard.validate(stmt, allowed), agent.MAX_ROWS)
            cols, rows = conn.run_query(cleaned)
            cols, rows = governance.filter_result(target, cleaned, cols, rows)
            out.append({"statement": stmt[:120], "type": "read", "rows": len(rows)})
        else:
            res = conn.run_script(stmt)   # write/DDL — only reached post-approval
            out.append({"statement": stmt[:120], "type": "write", "result": res})
    return out


def _run(job):
    """Execute with automatic retries; escalate to a human after repeated
    failures. Mutates + persists the job as it goes."""
    while True:
        try:
            result = _execute(job)
            _save(job, status="succeeded", result=json.dumps(result, default=str), last_error=None)
            return job
        except Exception as e:
            job["attempts"] += 1
            job["last_error"] = str(e)[:500]
            if job["attempts"] > job["max_retries"]:
                _save(job, status="escalated", attempts=job["attempts"], last_error=job["last_error"])
                _email(job, "escalated",
                       f"failed {job['attempts']} times and needs a human decision")
                return job
            _save(job, status="retrying", attempts=job["attempts"], last_error=job["last_error"])
            # immediate retry; a real deployment would back off


def submit(kind, target, script, user):
    verdict = supervise(kind, target, script, user)
    risk = verdict["risk"]
    now = time.time()
    job = {
        "id": str(uuid.uuid4()), "user_id": user["id"],
        "requester_role": user["role"], "requester_email": user.get("email"),
        "kind": kind, "target": target, "script": script, "risk": risk,
        "supervisor_decision": verdict["decision"],
        "supervisor_reasons": json.dumps(verdict["reasons"]),
        "attempts": 0, "max_retries": MAX_RETRIES, "last_error": None,
        "result": None, "human_by": None, "created_at": now, "updated_at": now,
    }
    if verdict["decision"] == "reject":
        job["status"] = "rejected"
    elif verdict["decision"] == "approve":
        job["status"] = "running"
    else:
        job["status"] = "awaiting_approval"
    _insert(job)
    db.log_activity(user, "job_submit", prompt=f"{kind}/{risk}", source=target,
                    ok=job["status"] != "rejected")

    if job["status"] == "running":
        _run(job)
    elif job["status"] == "awaiting_approval":
        _email(job, "awaiting_approval", "is waiting for a human to approve it")
    return job


# ── Persistence ─────────────────────────────────────────────────────────

def _insert(job):
    c = db._conn()
    cols = ("id,user_id,requester_role,requester_email,kind,target,script,risk,status,"
            "supervisor_decision,supervisor_reasons,attempts,max_retries,last_error,"
            "result,human_by,created_at,updated_at")
    c.execute(f"INSERT INTO supervised_jobs ({cols}) VALUES ({','.join('?' * 18)})",
              tuple(job[k] for k in cols.split(",")))
    c.commit()
    c.close()


def _save(job, **fields):
    job.update(fields)
    job["updated_at"] = time.time()
    fields["updated_at"] = job["updated_at"]
    sets = ", ".join(f"{k}=?" for k in fields)
    c = db._conn()
    c.execute(f"UPDATE supervised_jobs SET {sets} WHERE id=?",
              list(fields.values()) + [job["id"]])
    c.commit()
    c.close()


def _row(r):
    d = dict(r)
    for k in ("supervisor_reasons", "result"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def _get(jid):
    c = db._conn()
    r = c.execute("SELECT * FROM supervised_jobs WHERE id=?", (jid,)).fetchone()
    c.close()
    return dict(r) if r else None


def _email(job, event, detail):
    to = job.get("requester_email")
    if not to:
        return
    reasons = job.get("supervisor_reasons")
    reasons = json.loads(reasons) if isinstance(reasons, str) else (reasons or [])
    html = (
        f"<p>Your {job['kind']} on <b>{job['target']}</b> {detail}.</p>"
        f"<p>Risk: <b>{job['risk']}</b> · status: <b>{job['status']}</b></p>"
        f"<pre style='background:#f6f6f6;padding:8px;border-radius:6px'>{job['script'][:600]}</pre>"
        + (f"<p style='color:#b00'>Last error: {job.get('last_error')}</p>" if job.get('last_error') else "")
        + f"<p>Supervisor: {'; '.join(reasons)}</p>"
        + "<p>An administrator can approve or reject it in Studio → Jobs.</p>"
    )
    try:
        email_service.send(to, f"Studio job {event.replace('_', ' ')}: {job['kind']} on {job['target']}", html)
    except Exception:
        pass


# ── API ─────────────────────────────────────────────────────────────────

class SubmitIn(BaseModel):
    kind: str          # "sql_script" | "spark_job"
    target: str        # source name (snowflake / databricks / demo / …)
    script: str        # SQL text, or a Jobs run-submit JSON for spark_job


@router.post("", status_code=201)
def submit_job(body: SubmitIn, user=Depends(current_user)):
    if body.kind not in ("sql_script", "spark_job"):
        raise HTTPException(400, "kind must be 'sql_script' or 'spark_job'")
    if not (body.script or "").strip():
        raise HTTPException(400, "script is required")
    return _public(submit(body.kind, body.target, body.script, user))


@router.get("")
def list_jobs(user=Depends(current_user)):
    """Own jobs; admins see everything (they are the human approvers)."""
    c = db._conn()
    if user["role"] == "admin":
        rows = c.execute("SELECT * FROM supervised_jobs ORDER BY updated_at DESC LIMIT 200").fetchall()
    else:
        rows = c.execute("SELECT * FROM supervised_jobs WHERE user_id=? ORDER BY updated_at DESC LIMIT 200",
                         (user["id"],)).fetchall()
    c.close()
    return {"jobs": [_public(_row(r)) for r in rows],
            "can_approve": user["role"] == "admin"}


@router.get("/{jid}")
def get_job(jid: str, user=Depends(current_user)):
    row = _get(jid)
    if not row or (row["user_id"] != user["id"] and user["role"] != "admin"):
        raise HTTPException(404, "Job not found")
    return _public(_row(row))


def _need_approver(jid, user):
    """Approve/reject is the human-in-the-loop gate — admins only."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only an administrator can approve or reject a job")
    row = _get(jid)
    if not row:
        raise HTTPException(404, "Job not found")
    if row["status"] not in ("awaiting_approval", "escalated"):
        raise HTTPException(400, f"Job is {row['status']}, not awaiting a decision")
    return row


@router.post("/{jid}/approve")
def approve(jid: str, user=Depends(current_user)):
    row = _need_approver(jid, user)
    _save(row, status="running", human_by=user.get("email"))
    db.log_activity(user, "job_approve", prompt=row["kind"], source=row["target"])
    return _public(_run(row))


@router.post("/{jid}/reject")
def reject(jid: str, user=Depends(current_user)):
    row = _need_approver(jid, user)
    _save(row, status="rejected", human_by=user.get("email"))
    db.log_activity(user, "job_reject", prompt=row["kind"], source=row["target"])
    _email(row, "rejected", "was rejected by an administrator")
    return _public(_get(jid))


def _public(job):
    """Never leak requester internals beyond what the UI needs."""
    j = dict(job)
    if isinstance(j.get("supervisor_reasons"), str):
        try:
            j["supervisor_reasons"] = json.loads(j["supervisor_reasons"])
        except Exception:
            j["supervisor_reasons"] = [j["supervisor_reasons"]]
    if isinstance(j.get("result"), str):
        try:
            j["result"] = json.loads(j["result"])
        except Exception:
            pass
    j.pop("user_id", None)
    return j
