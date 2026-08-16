"""Staged pipeline flow — typed JSON contracts, one named agent per stage.

    Business request
       │  Pipeline planner        → PipelineSpec
       │  Code generator          → GeneratedArtifact
       │  Validator               → ValidationResult
       │  Approval agent / human  → DeploymentRequest
       │  Airflow / Databricks    → ExecutionResult
       ▼
    Agent Lightning records every stage as a trace (serialized artifact, the
    tools it called, its result or failure, and a reward).

Each stage takes the previous stage's artifact and emits the next as a
validated Pydantic model, so the boundary between agents is an explicit JSON
contract — not an ad-hoc dict. Nothing here re-implements the underlying work:
the planner is pipelines.build, the generator is pybuild, validation reuses the
query guard + verify_sql, and approval/execution go through supervisor.py so
Studio's read-only invariant and human-in-the-loop gate are preserved. The flow
just sequences them, records each as a rollout, and fails fast — a failing stage
stops the chain and is traced with reward 0.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, pipelines, pybuild, queries, queryguard, roster, supervisor
from .auth import current_user

router = APIRouter(prefix="/flow", tags=["flow"])


# ── Typed contracts between stages ───────────────────────────────────────

class Step(BaseModel):
    name: str
    table: str | None = None
    sql: str = ""


class PipelineSpec(BaseModel):
    request: str
    source: str
    steps: list[Step] = []
    matched_tables: list[str] = []
    repo: dict | None = None
    lineage: dict | None = None
    planner: str = "Pipeline planner"


class GeneratedArtifact(BaseModel):
    language: str = "python"
    code: str = ""
    mode: str = "scaffold"          # "agent" | "scaffold"
    mcp_servers: list[str] = []
    generator: str = "Code generator"
    note: str | None = None


class Check(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class ValidationResult(BaseModel):
    ok: bool
    checks: list[Check] = []
    errors: list[str] = []
    validator: str = "Validator"


class DeploymentRequest(BaseModel):
    target: str
    kind: str                       # "sql_script" | "spark_job"
    risk: str = "read"              # read | write | job
    decision: str = "reject"        # approve | needs_human | reject
    reasons: list[str] = []
    script: str = ""
    approver: str = "Approval agent"


class ExecutionResult(BaseModel):
    status: str                     # succeeded | awaiting_approval | escalated | rejected | failed | skipped
    target: str
    result: object | None = None
    error: str | None = None
    job_id: str | None = None
    executor: str = "Deployment executor"


# ── Per-stage Agent Lightning trace ──────────────────────────────────────

def _trace(user, stage, agent_name, request, source, artifact, ok, reward,
           error=None, sql=None, duration_ms=None):
    """Record one stage as a rollout: the serialized artifact, which named
    agent produced it, whether it succeeded, and a reward."""
    try:
        return db.add_trace(
            user, prompt=f"[{stage}] {request}"[:1000], mode=f"flow:{stage}",
            source=source, sql=sql, ok=ok, error=(error or "")[:500] if error else None,
            duration_ms=duration_ms, reward=reward, reward_source="flow",
            # "agents" (list) matches the chat traces so the /learning per-agent
            # tally counts stage agents too; "artifact" is the serialized task.
            meta={"stage": stage, "agent": agent_name, "agents": [agent_name],
                  "artifact": artifact},
        )
    except Exception:
        return None


# ── Stages ───────────────────────────────────────────────────────────────

def plan(user, request):
    """Pipeline planner → PipelineSpec. Routes the request to an accessible
    source and drafts verified steps (pipelines.build does the real work)."""
    t0 = time.time()
    try:
        draft = pipelines.build(user, request)
    except HTTPException as e:
        spec = PipelineSpec(request=request, source="", steps=[])
        tid = _trace(user, "plan", "Pipeline planner", request, None,
                     spec.model_dump(), ok=False, reward=0.0,
                     error=e.detail, duration_ms=int((time.time() - t0) * 1000))
        return spec, tid, e.detail
    spec = PipelineSpec(
        request=request, source=draft["source"],
        steps=[Step(**{k: s.get(k) for k in ("name", "table", "sql")}) for s in draft["steps"]],
        matched_tables=draft.get("matched_tables", []),
        repo=draft.get("repo"), lineage=draft.get("lineage"),
    )
    ok = bool(spec.steps)
    tid = _trace(user, "plan", "Pipeline planner", request, spec.source,
                 spec.model_dump(), ok=ok, reward=1.0 if ok else 0.0,
                 error=None if ok else "no steps drafted",
                 duration_ms=int((time.time() - t0) * 1000))
    return spec, tid, None if ok else "Planner drafted no steps"


def generate(user, spec):
    """Code generator → GeneratedArtifact. Reuses pybuild (existing scripts via
    MCP as context); the planned SQL steps are handed over as reference."""
    t0 = time.time()
    context = "\n\n".join(f"-- {s.name}\n{s.sql}" for s in spec.steps if s.sql)
    prompt = (f"Build a deployable pipeline for: {spec.request}. Source: {spec.source}. "
              f"Implement these steps as one runnable job.")
    try:
        out = pybuild.build(pybuild.BuildIn(prompt=prompt, context=context or None), user)
    except Exception as e:
        art = GeneratedArtifact(code="", mode="scaffold", note=f"generation error: {e}")
        tid = _trace(user, "codegen", "Code generator", spec.request, spec.source,
                     art.model_dump(), ok=False, reward=0.0, error=str(e),
                     duration_ms=int((time.time() - t0) * 1000))
        return art, tid
    art = GeneratedArtifact(
        language="python", code=out.get("python", ""),
        mode="agent" if out.get("mode") == "agent" else "scaffold",
        mcp_servers=out.get("mcp_servers", []), note=out.get("note"))
    ok = bool(art.code.strip())
    reward = 1.0 if art.mode == "agent" and ok else (0.5 if ok else 0.0)
    tid = _trace(user, "codegen", "Code generator", spec.request, spec.source,
                 art.model_dump(), ok=ok, reward=reward,
                 error=None if ok else "empty artifact",
                 duration_ms=int((time.time() - t0) * 1000))
    return art, tid


def validate(user, spec, artifact):
    """Validator → ValidationResult. Re-verifies every planned step (RBAC +
    guard + real execution) and static-checks the generated code."""
    t0 = time.time()
    checks, errors = [], []
    for s in spec.steps:
        v = queries.verify_sql(user, spec.source, s.table, s.sql)
        checks.append(Check(name=f"step: {s.name}", ok=v["ok"],
                            detail=None if v["ok"] else v.get("error")))
        if not v["ok"]:
            errors.append(f"{s.name}: {v.get('error')}")

    # Static check of the generated code — never executed here (that's the
    # supervised executor's job), only parsed/guarded.
    if artifact.code.strip():
        if artifact.language == "python":
            try:
                compile(artifact.code, "<artifact>", "exec")
                checks.append(Check(name="python syntax", ok=True))
            except SyntaxError as e:
                checks.append(Check(name="python syntax", ok=False, detail=str(e)))
                errors.append(f"python syntax: {e}")
        else:
            bad = queryguard.FORBIDDEN.search(artifact.code)
            checks.append(Check(name="sql guard", ok=not bad,
                                detail=None if not bad else f"forbidden: {bad.group(0)}"))
            if bad:
                errors.append(f"sql guard: {bad.group(0)}")

    ok = not errors and bool(spec.steps)
    result = ValidationResult(ok=ok, checks=checks, errors=errors)
    tid = _trace(user, "validate", "Validator", spec.request, spec.source,
                 result.model_dump(), ok=ok, reward=1.0 if ok else 0.0,
                 error=None if ok else "; ".join(errors[:3]),
                 duration_ms=int((time.time() - t0) * 1000))
    return result, tid


def request_approval(user, spec, validation, target, kind):
    """Approval agent → DeploymentRequest. The supervisor classifies risk and
    decides approve / needs_human; an invalid pipeline is rejected outright."""
    t0 = time.time()
    # What actually gets deployed: the verified read-only steps as a script, or
    # (for a Spark target) a job payload the executor submits.
    if kind == "spark_job":
        script = json.dumps({"tasks": [{"name": s.name, "sql": s.sql} for s in spec.steps]})
    else:
        script = ";\n".join(s.sql for s in spec.steps if s.sql)

    if not validation.ok:
        req = DeploymentRequest(target=target, kind=kind, risk="n/a", decision="reject",
                                reasons=["Validation failed — not eligible for deployment."],
                                script=script)
        tid = _trace(user, "approve", "Approval agent", spec.request, target,
                     req.model_dump(), ok=False, reward=0.0, error="validation failed",
                     duration_ms=int((time.time() - t0) * 1000))
        return req, tid

    verdict = supervisor.supervise(kind, target, script, user)
    req = DeploymentRequest(target=target, kind=kind, risk=verdict["risk"],
                            decision=verdict["decision"], reasons=verdict["reasons"],
                            script=script)
    # needs_human is not a failure — it's the gate working as designed.
    reward = {"approve": 1.0, "needs_human": 0.7, "reject": 0.0}.get(verdict["decision"], 0.0)
    tid = _trace(user, "approve", "Approval agent", spec.request, target,
                 req.model_dump(), ok=verdict["decision"] != "reject", reward=reward,
                 error=None if verdict["decision"] != "reject" else "; ".join(verdict["reasons"][:2]),
                 duration_ms=int((time.time() - t0) * 1000))
    return req, tid


def deploy_execute(user, spec, deployment):
    """Airflow / Databricks executor → ExecutionResult. Routes through
    supervisor.submit: read-only deployments run now; writes/Spark jobs create
    an awaiting-approval job (the human gate) and defer."""
    t0 = time.time()
    executor = roster.executor_name(deployment.target)
    if deployment.decision == "reject":
        res = ExecutionResult(status="rejected", target=deployment.target,
                              error="; ".join(deployment.reasons[:2]), executor=executor)
        tid = _trace(user, "execute", executor, spec.request, deployment.target,
                     res.model_dump(), ok=False, reward=0.0, error=res.error,
                     duration_ms=int((time.time() - t0) * 1000))
        return res, tid

    try:
        job = supervisor.submit(deployment.kind, deployment.target, deployment.script, user)
    except HTTPException as e:
        # Target not connected (e.g. Airflow/Databricks without credentials).
        res = ExecutionResult(status="skipped", target=deployment.target,
                              error=e.detail, executor=executor)
        tid = _trace(user, "execute", executor, spec.request, deployment.target,
                     res.model_dump(), ok=False, reward=0.5, error=e.detail,
                     duration_ms=int((time.time() - t0) * 1000))
        return res, tid

    status_map = {"succeeded": "succeeded", "awaiting_approval": "awaiting_approval",
                  "escalated": "escalated", "rejected": "rejected", "running": "succeeded",
                  "retrying": "failed", "failed": "failed"}
    status = status_map.get(job["status"], job["status"])
    res = ExecutionResult(status=status, target=deployment.target,
                          result=job.get("result"), error=job.get("last_error"),
                          job_id=job["id"], executor=executor)
    reward = {"succeeded": 1.0, "awaiting_approval": 0.7, "skipped": 0.5,
              "escalated": 0.2, "failed": 0.0, "rejected": 0.0}.get(status, 0.0)
    tid = _trace(user, "execute", executor, spec.request, deployment.target,
                 res.model_dump(), ok=status in ("succeeded", "awaiting_approval"),
                 reward=reward, error=res.error, duration_ms=int((time.time() - t0) * 1000))
    return res, tid


# ── Persistence + driver ─────────────────────────────────────────────────

def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS flow_runs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            request TEXT NOT NULL,
            target TEXT,
            kind TEXT,
            status TEXT NOT NULL,
            spec TEXT, artifact TEXT, validation TEXT, deployment TEXT, execution TEXT,
            trace_ids TEXT,
            job_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_flow_user ON flow_runs(user_id, updated_at DESC);
        """
    )
    c.commit()
    c.close()


def run_flow(user, request, target=None, kind="sql_script"):
    """Drive one business request through every stage. Fails fast: a stage that
    fails stops the chain, and the run's status reflects where it stopped."""
    request = (request or "").strip()
    if not request:
        raise HTTPException(400, "Describe the pipeline to build")
    traces = {}

    spec, traces["plan"], perr = plan(user, request)
    if perr:
        return _finish(user, request, target or spec.source, kind, "plan_failed",
                       spec, None, None, None, None, traces)

    target = target or spec.source
    artifact, traces["codegen"] = generate(user, spec)
    validation, traces["validate"] = validate(user, spec, artifact)
    deployment, traces["approve"] = request_approval(user, spec, validation, target, kind)

    execution = None
    if deployment.decision == "reject":
        status = "rejected"
    else:
        execution, traces["execute"] = deploy_execute(user, spec, deployment)
        status = {"succeeded": "succeeded", "awaiting_approval": "awaiting_approval",
                  "escalated": "escalated", "failed": "execute_failed",
                  "skipped": "execute_skipped", "rejected": "rejected"}.get(
                      execution.status, execution.status)

    return _finish(user, request, target, kind, status, spec, artifact,
                   validation, deployment, execution, traces)


def _finish(user, request, target, kind, status, spec, artifact, validation,
            deployment, execution, traces):
    fid = str(uuid.uuid4())
    now = time.time()
    job_id = execution.job_id if execution else None
    c = db._conn()
    c.execute(
        "INSERT INTO flow_runs (id, user_id, request, target, kind, status, spec, "
        "artifact, validation, deployment, execution, trace_ids, job_id, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, user["id"], request, target, kind, status,
         _dump(spec), _dump(artifact), _dump(validation), _dump(deployment),
         _dump(execution), json.dumps(traces), job_id, now, now),
    )
    c.commit()
    c.close()
    db.log_activity(user, "flow_run", prompt=request[:200], source=target,
                    ok=status in ("succeeded", "awaiting_approval"))
    return {
        "id": fid, "status": status, "request": request, "target": target, "kind": kind,
        "spec": _obj(spec), "artifact": _obj(artifact), "validation": _obj(validation),
        "deployment": _obj(deployment), "execution": _obj(execution),
        "trace_ids": traces, "job_id": job_id,
        "stages": _stage_view(spec, artifact, validation, deployment, execution, traces),
    }


def _stage_view(spec, artifact, validation, deployment, execution, traces):
    """Compact, ordered summary of the chain: one row per stage — the agent,
    the artifact type it produced, ok/failed, and its trace id."""
    rows = [
        ("plan", "Pipeline planner", "PipelineSpec", bool(spec and spec.steps)),
        ("codegen", "Code generator", "GeneratedArtifact", bool(artifact and artifact.code)),
        ("validate", "Validator", "ValidationResult", bool(validation and validation.ok)),
        ("approve", "Approval agent", "DeploymentRequest",
         bool(deployment and deployment.decision != "reject")),
        ("execute", roster.executor_name(deployment.target if deployment else None),
         "ExecutionResult", bool(execution and execution.status in ("succeeded", "awaiting_approval"))),
    ]
    out = []
    for stage, agent_name, produces, ok in rows:
        if stage == "execute" and execution is None:
            continue
        out.append({"stage": stage, "agent": agent_name, "produces": produces,
                    "ok": ok, "trace_id": traces.get(stage)})
    return out


def _dump(model):
    return json.dumps(model.model_dump(), default=str) if model is not None else None


def _obj(model):
    return model.model_dump() if model is not None else None


# ── API ──────────────────────────────────────────────────────────────────

class RunIn(BaseModel):
    request: str
    target: str | None = None       # deploy target; defaults to the planned source
    kind: str = "sql_script"        # "sql_script" | "spark_job"


@router.post("/run")
def run(body: RunIn, user=Depends(current_user)):
    if body.kind not in ("sql_script", "spark_job"):
        raise HTTPException(400, "kind must be 'sql_script' or 'spark_job'")
    return run_flow(user, body.request, body.target, body.kind)


def _row(r):
    d = dict(r)
    for k in ("spec", "artifact", "validation", "deployment", "execution", "trace_ids"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                pass
    return d


@router.get("")
def listing(user=Depends(current_user)):
    c = db._conn()
    rows = c.execute(
        "SELECT id, request, target, kind, status, job_id, created_at, updated_at "
        "FROM flow_runs WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
        (user["id"],)).fetchall()
    c.close()
    return {"flows": [dict(r) for r in rows]}


@router.get("/{fid}")
def get(fid: str, user=Depends(current_user)):
    c = db._conn()
    r = c.execute("SELECT * FROM flow_runs WHERE id=?", (fid,)).fetchone()
    c.close()
    if r is None or dict(r)["user_id"] != user["id"]:
        raise HTTPException(404, "Flow not found")  # 404, not 403 — no oracle
    d = _row(r)
    # If execution is gated on a human, reflect the linked job's live status so
    # approving it in Jobs shows through here without re-running the flow.
    if d.get("job_id") and (d.get("execution") or {}).get("status") == "awaiting_approval":
        job = supervisor._get(d["job_id"])
        if job:
            d["execution"]["status"] = {"succeeded": "succeeded", "running": "succeeded",
                                        "rejected": "rejected", "escalated": "escalated"}.get(
                                            job["status"], d["execution"]["status"])
            d["execution"]["result"] = supervisor._public(supervisor._row(job)).get("result")
    d["stages"] = _stage_view_from_dict(d)
    return d


def _stage_view_from_dict(d):
    spec, art = d.get("spec") or {}, d.get("artifact") or {}
    val, dep = d.get("validation") or {}, d.get("deployment") or {}
    ex, traces = d.get("execution"), d.get("trace_ids") or {}
    rows = [
        ("plan", "Pipeline planner", "PipelineSpec", bool(spec.get("steps"))),
        ("codegen", "Code generator", "GeneratedArtifact", bool(art.get("code"))),
        ("validate", "Validator", "ValidationResult", bool(val.get("ok"))),
        ("approve", "Approval agent", "DeploymentRequest", dep.get("decision") not in (None, "reject")),
    ]
    out = [{"stage": s, "agent": a, "produces": p, "ok": ok, "trace_id": traces.get(s)}
           for s, a, p, ok in rows]
    if ex:
        out.append({"stage": "execute", "agent": roster.executor_name(dep.get("target")),
                    "produces": "ExecutionResult",
                    "ok": ex.get("status") in ("succeeded", "awaiting_approval"),
                    "trace_id": traces.get("execute")})
    return out
