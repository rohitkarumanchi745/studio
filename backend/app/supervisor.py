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

Job kinds are a CLOSED set (KINDS below) and each one has an explicit
executor. There is deliberately NO fall-through: a kind nobody handles raises
instead of inheriting whatever branch happens to be last, so a future kind can
never silently acquire the warehouse write path.

platform_run jobs (kind "platform_run", target = an orchestration platform from
platforms.PLATFORMS) trigger external pipelines. Platforms aren't data sources,
so RBAC table policy doesn't apply — instead only admins and analysts may
submit, and every run waits for a human admin. A successful trigger leaves the
job "running" with the platform's {run_ref, url} in the result JSON column
(schema unchanged); GET /jobs/{id}/live then feeds status / logs / quality back
from the platform and flips the job on a terminal state.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agent, db, email_service, gateway, platforms, queryguard, rbac
from .auth import current_user
from .sources import connector_or_400

router = APIRouter(prefix="/jobs", tags=["jobs"])

MAX_RETRIES = 2            # automatic retries before escalating to a human
_WRITE = queryguard.FORBIDDEN  # DML/DDL keyword detector, reused from the guard

#: SQL text, run statement-by-statement against a data source.
SQL_KIND = "sql_script"
#: A Databricks Jobs run-submit body, handed to connector.submit_spark_job.
SPARK_KIND = "spark_job"
#: An external orchestration platform run (platforms.PLATFORMS).
PLATFORM_KIND = "platform_run"
#: A generated CODE ARTIFACT (toolbuilder's MCP server / tool) whose "execution"
#: is a human's approval decision and NOTHING ELSE. It rides the supervised-job
#: lifecycle purely for the admin gate + audit trail; its script is Python, not
#: SQL, so it must never reach a warehouse executor — see _execute_artifact.
ARTIFACT_KIND = "mcp_build"

#: Every kind the supervisor knows how to execute. _execute dispatches on this
#: set and RAISES on anything else — the fall-through that used to hand an
#: unknown kind to connector.run_script is gone on purpose.
KINDS = (SQL_KIND, SPARK_KIND, PLATFORM_KIND, ARTIFACT_KIND)


def init_tables():
    with db.connect() as c:
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


# ── Risk classification + supervisor agent ──────────────────────────────

def classify(kind, script):
    if kind == ARTIFACT_KIND:
        # Not a statement at all — a code deliverable awaiting an admin's
        # signature. Classifying it as SQL would read Python as "write / DDL"
        # and imply a warehouse is involved; none is.
        return "artifact"
    if kind in (SPARK_KIND, PLATFORM_KIND):
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
    if kind == PLATFORM_KIND:
        return _supervise_platform_run(target, script, user)
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
    if risk == "artifact":
        return {"decision": "needs_human", "risk": risk,
                "reasons": reasons + [
                    f"Generated code artifact scoped to {target} — an administrator "
                    "must approve it before it can be registered as a tool server. "
                    "Nothing is executed by approving it."]}
    label = "Spark job" if risk == "job" else "write / DDL"
    return {"decision": "needs_human", "risk": risk,
            "reasons": reasons + [f"{label} on {target} — requires human approval before it runs."]}


def _supervise_platform_run(target, script, user):
    """Platforms are orchestrators, not data sources, so rbac.can_access does
    not apply. Submitting is a role gate (admin / analyst); every run still
    waits for a human admin — policy, not the model, decides."""
    if user["role"] not in ("admin", "analyst"):
        return {"decision": "reject", "risk": "job",
                "reasons": ["Your role cannot submit platform runs."]}
    p = platforms.PLATFORMS.get(target)
    if p is None:
        return {"decision": "reject", "risk": "job",
                "reasons": [f"Unknown platform '{target}' — choose one of: "
                            f"{', '.join(sorted(platforms.PLATFORMS))}."]}
    if not p.configured():
        return {"decision": "reject", "risk": "job",
                "reasons": [f"{p.label} is not configured — set its credentials "
                            "in the environment first."]}
    reasons = []
    if agent.llm_available(agent.llm_spec(), user):
        try:
            reasons += _llm_review("platform_run", target, script, user)
        except Exception:
            pass
    return {"decision": "needs_human", "risk": "job",
            "reasons": reasons + [f"Pipeline run on {p.label} — requires human "
                                  "approval before it runs."]}


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


def _record_platform_rollout(user, target, run_ref):
    """Agent Lightning rollout for a platform trigger (lightning.py
    conventions: meta.agents names the acting agent, reward_source says where
    the score came from). The reward is unknown until the run finishes, so it
    stays None/"pending"; a later /live poll that discovers the terminal state
    does NOT add a second trace — one rollout per run."""
    try:
        db.add_trace(user, prompt=f"platform:{target}", mode="platform_run",
                     source=target, ok=True, reward=None, reward_source="pending",
                     meta={"agents": [f"{target} executor"], "run_ref": run_ref})
    except Exception:
        pass  # learning must never break execution


def _register_output(output, job, user):
    """Call the objectstore write→read bridge for a spark job's DECLARED S3
    parquet output. Lazily imported so the whole feature stays dormant without
    objectstore / AWS creds, and so this module never hard-depends on the
    bridge's presence. Fail-safe: never raises — a bad or out-of-prefix output
    URI comes back as {"error": ...} and is surfaced in the job result, never
    crashes the run. Confinement (_valid + allowed-prefix) and the admin
    authority gate (job["human_by"]) live in objectstore, which receives the
    job so no policy is broadened here."""
    try:
        from .connectors import objectstore
        # Call the 3-arg wrapper register_spark_output(output, job, user). The
        # module also exposes a generic register_output(user, source, name, uri,
        # ...) for the manual admin endpoint — invoking THAT with (output, job,
        # user) silently binds user=output / source=job / name=user (garbage →
        # _confine raises → the bridge NEVER registers). Bind to the wrapper by
        # name; the getattr keeps the whole feature dormant if it is absent.
        fn = getattr(objectstore, "register_spark_output", None)
        if fn is None:
            return {"error": "objectstore auto-registration is unavailable"}
        reg = fn(output, job, user)
        return reg if isinstance(reg, dict) else {"registered": str(reg)}
    except Exception as e:
        return {"error": f"auto-registration failed: {str(e)[:200]}"}


def _bridge_output(job, user):
    """Close the lakehouse loop for a SUCCEEDED spark job that declared an S3
    parquet sink: read the declared `output` block out of the (immutable) job
    script and auto-register it as a queryable objectstore dataset, recording
    the outcome under result["registered_dataset"] so /live and the Flow graph
    show `S3 parquet -> dataset`. Idempotent: a job re-polled after success
    (or a re-approved run) is not re-registered or re-logged. Returns the
    registration dict, or None when the job declared no output."""
    try:
        script = json.loads(job["script"]) if job.get("script") else {}
    except Exception:
        script = {}
    output = script.get("output") if isinstance(script, dict) else None
    if not isinstance(output, dict):
        return None
    try:
        result = json.loads(job["result"]) if job.get("result") else {}
    except Exception:
        result = {}
    if not isinstance(result, dict):
        result = {"result": result}
    prev = result.get("registered_dataset")
    if isinstance(prev, dict) and prev.get("registered") == output.get("name"):
        return prev   # already registered this exact output — don't re-log
    reg = _register_output(output, job, user)
    if reg is None:
        return None
    result["registered_dataset"] = reg
    _save(job, result=json.dumps(result, default=str))
    return reg


def _execute_artifact(job):
    """Execute a code-artifact job by recording the decision — running NOTHING.

    INVARIANT — no connector is touched on this path. The job's script is
    model-generated PYTHON; the deliverable's own module (toolbuilder) writes it
    to a confined sandbox once it sees human_by on this job, and the MCP client
    later spawns it as a separate OS process. Studio never executes it, and it
    must never be mistaken for SQL and handed to a warehouse executor.

    Fails closed: policy makes every artifact job human-gated, so arriving here
    without an approver means the gate was bypassed."""
    approver = job.get("human_by")
    if not approver:
        raise RuntimeError("A code artifact reached execution without a human approver.")
    return {"kind": job["kind"], "executed": False, "approved_by": approver,
            "detail": "Code artifact approved by an administrator; nothing was executed."}


def _execute(job):
    """Run the job against its environment. Raises on any failure.

    Dispatch is EXHAUSTIVE over KINDS. An unrecognized kind raises instead of
    falling through to the SQL branch: that fall-through is how a code artifact
    ended up being fed to connector.run_script, and a default that runs scripts
    is the wrong default for anything new."""
    user = _requester(job)
    target = job["target"]
    kind = job["kind"]

    if kind == PLATFORM_KIND:
        # Adapters read their own env creds and raise RuntimeError with a clear
        # message; {"run_ref","url"} lands in the result JSON column.
        out = platforms.get_platform(target).trigger(json.loads(job["script"]))
        _record_platform_rollout(user, target, out.get("run_ref"))
        return out

    if kind == ARTIFACT_KIND:
        # Before any connector lookup — an artifact job has no environment.
        return _execute_artifact(job)

    if kind not in (SQL_KIND, SPARK_KIND):
        raise RuntimeError(
            f"Unknown job kind '{kind}' — refusing to execute it. Known kinds: "
            + ", ".join(KINDS))

    conn = connector_or_400(target)
    if not conn.configured():
        raise RuntimeError(f"The {target} environment is not connected — configure its credentials.")

    if kind == SPARK_KIND:
        return conn.submit_spark_job(json.loads(job["script"]))

    out = []
    for stmt in _statements(job["script"]):
        low = stmt.lstrip().lower()
        if low.startswith("select") or low.startswith("with"):
            # A read runs as the REQUESTER through the gateway — RBAC, guard,
            # governance and an audit row — even though the job was approved:
            # approval never widens what the requester's role may see.
            res = gateway.execute(user, target, stmt, "supervised_read")
            out.append({"statement": stmt[:120], "type": "read", "rows": res.row_count})
        else:
            # Write/DDL — only reached post-approval. This is the approved-write
            # path and stays OUTSIDE the gateway by design: the gateway is the
            # read pipeline (row-returning SELECTs), while a write's authority
            # is the human approval recorded on the job (human_by), audited by
            # job_approve. The connector's run_script is the write hook.
            res = conn.run_script(stmt)
            out.append({"statement": stmt[:120], "type": "write", "result": res})
    return out


def _run(job):
    """Execute with automatic retries; escalate to a human after repeated
    failures. Mutates + persists the job as it goes."""
    while True:
        try:
            result = _execute(job)
            if job["kind"] == PLATFORM_KIND:
                # Trigger-success only — the pipeline is now running on the
                # platform, so the job stays "running". /live owns the terminal
                # state: it flips the job to succeeded/failed when the platform
                # reports one, and registers the declared output on GENUINE
                # success. "succeeded" here would show a finished job in the
                # list while the pipeline is still in flight.
                _save(job, status="running", result=json.dumps(result, default=str), last_error=None)
                return job
            _save(job, status="succeeded", result=json.dumps(result, default=str), last_error=None)
            # Write→read bridge. A supervised spark_job that DECLARED an S3
            # parquet output has now produced it (this path runs only after an
            # admin approved the job — spark jobs are always human-gated), so
            # register that output as a queryable dataset.
            if job["kind"] == SPARK_KIND:
                _bridge_output(job, _requester(job))
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
    with db.connect() as c:
        cols = ("id,user_id,requester_role,requester_email,kind,target,script,risk,status,"
                "supervisor_decision,supervisor_reasons,attempts,max_retries,last_error,"
                "result,human_by,created_at,updated_at")
        c.execute(f"INSERT INTO supervised_jobs ({cols}) VALUES ({','.join('?' * 18)})",
                  tuple(job[k] for k in cols.split(",")))
        c.commit()


def _save(job, **fields):
    job.update(fields)
    job["updated_at"] = time.time()
    fields["updated_at"] = job["updated_at"]
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.connect() as c:
        c.execute(f"UPDATE supervised_jobs SET {sets} WHERE id=?",
                  list(fields.values()) + [job["id"]])
        c.commit()


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
    with db.connect() as c:
        r = c.execute("SELECT * FROM supervised_jobs WHERE id=?", (jid,)).fetchone()
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
    kind: str          # "sql_script" | "spark_job" | "platform_run"
    target: str        # source name (snowflake / …) — or a platform name for platform_run
    script: str        # SQL text, a Jobs run-submit JSON, or a platform payload JSON


@router.post("", status_code=201)
def submit_job(body: SubmitIn, user=Depends(current_user)):
    # ARTIFACT_KIND is deliberately NOT submittable over HTTP: an artifact job
    # is created by toolbuilder alongside the row it approves, so accepting one
    # here would mint an approval with no deliverable behind it.
    if body.kind not in (SQL_KIND, SPARK_KIND, PLATFORM_KIND):
        raise HTTPException(400, "kind must be 'sql_script', 'spark_job' or 'platform_run'")
    if not (body.script or "").strip():
        raise HTTPException(400, "script is required")
    if body.kind in (SPARK_KIND, PLATFORM_KIND):
        # Both are executed via json.loads(script) — fail at submit time with a
        # clear 400 instead of at run time inside the retry/escalation loop.
        try:
            json.loads(body.script)
        except Exception:
            raise HTTPException(400, f"{body.kind} script must be a JSON payload")
    return _public(submit(body.kind, body.target, body.script, user))


@router.get("")
def list_jobs(user=Depends(current_user)):
    """Own jobs; admins see everything (they are the human approvers)."""
    with db.connect() as c:
        if user["role"] == "admin":
            rows = c.execute("SELECT * FROM supervised_jobs ORDER BY updated_at DESC LIMIT 200").fetchall()
        else:
            rows = c.execute("SELECT * FROM supervised_jobs WHERE user_id=? ORDER BY updated_at DESC LIMIT 200",
                             (user["id"],)).fetchall()
    return {"jobs": [_public(_row(r)) for r in rows],
            "can_approve": user["role"] == "admin"}


@router.get("/platforms")
def list_platforms(user=Depends(current_user)):
    """Platform picker for the submit form. Viewers cannot submit platform
    runs, so they don't get the menu either. Registered before /{jid} so
    "platforms" is never read as a job id."""
    if user["role"] == "viewer":
        raise HTTPException(403, "Your role cannot run pipelines")
    return platforms.all_platforms()


@router.get("/{jid}")
def get_job(jid: str, user=Depends(current_user)):
    row = _get(jid)
    if not row or (row["user_id"] != user["id"] and user["role"] != "admin"):
        raise HTTPException(404, "Job not found")
    return _public(_row(row))


@router.get("/{jid}/live")
def live_job(jid: str, user=Depends(current_user)):
    """Live platform state for a job — owner or admin, 404 for everyone else
    (no oracle on other users' job ids). For a platform_run with a run_ref:
    poll the platform best-effort (a logs failure must not kill the status),
    persist the latest state into the result JSON, and flip the job's status
    on a terminal state. Other jobs just report their stored state."""
    row = _get(jid)
    if not row or (row["user_id"] != user["id"] and user["role"] != "admin"):
        raise HTTPException(404, "Job not found")

    try:
        stored = json.loads(row["result"]) if row.get("result") else {}
    except Exception:
        stored = {}
    run_ref = stored.get("run_ref") if isinstance(stored, dict) else None

    if row["kind"] != PLATFORM_KIND or not run_ref:
        # Nothing to poll — the stored job is the truth.
        return {"state": row["status"], "detail": row.get("last_error"),
                "url": None, "metrics": {}, "logs": "", "quality": [],
                "job": _public(row)}

    p = platforms.get_platform(row["target"])
    try:
        st = p.status(run_ref)
    except Exception as e:
        st = {"state": "unknown", "detail": str(e), "url": None, "metrics": {}}
    try:
        logs = p.logs(run_ref)
    except Exception:
        logs = ""
    try:
        quality = p.quality(run_ref)
    except Exception:
        quality = []

    merged = {**stored, "state": st["state"], "detail": st.get("detail"),
              "metrics": st.get("metrics") or {}}
    if st.get("url"):
        merged["url"] = st["url"]
    fields = {"result": json.dumps(merged, default=str)}
    if st["state"] == "succeeded":
        fields["status"] = "succeeded"      # idempotent: succeeded stays succeeded
    elif st["state"] == "failed":
        fields["status"] = "failed"
        fields["last_error"] = (st.get("detail") or "platform reported failure")[:500]
    _save(row, **fields)
    # Write→read bridge. On a platform_run's GENUINE terminal success (the
    # platform itself reports succeeded — not the trigger-success _run saw when
    # it submitted), register the job's declared S3 parquet output as a
    # queryable dataset. Idempotent, so repeated /live polls after success do
    # not duplicate or re-log the registration.
    if st["state"] == "succeeded":
        _bridge_output(row, user)
    # Deliberately NO second Agent Lightning trace when a poll discovers the
    # terminal state — _execute already recorded this run's rollout.

    return {"state": st["state"], "detail": st.get("detail"),
            "url": st.get("url") or stored.get("url"),
            "metrics": st.get("metrics") or {}, "logs": logs,
            "quality": quality, "job": _public(row)}


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
