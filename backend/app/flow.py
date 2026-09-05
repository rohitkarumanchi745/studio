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

INVARIANT — the generated Python is a DELIVERABLE, not a deployable. Studio
never executes model-generated Python in-process; the Validator only PARSES it
(ValidationResult.artifact_status == "syntax_checked_not_executed") and the
executor only ever submits DeploymentRequest.script, which is built from the
verified SQL steps (DeploymentRequest.deploys / artifact_deployed=False). So a
run whose artifact would raise on its first line is reported as
"succeeded_sql_only", never as a plain success, and the pipeline view and the
digest email both name what actually ran (ExecutionResult.deployed).
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import (db, email_service, pipelines, pybuild, queries, queryguard, roster,
               supervisor)
from .auth import current_user
from .connectors import databricks_conn
from .sources import connector_or_400

router = APIRouter(prefix="/flow", tags=["flow"])

MAX_REPAIRS = 2   # validate→repair attempts before a pipeline goes to human review

# Overall statuses that count as "the run did what it could". "succeeded_sql_only"
# is here because the verified SQL steps really did deploy and run — it is
# separate from "succeeded" only so nobody reads it as "the generated Python
# artifact ran too". Nothing in Studio ever executes model-generated Python.
OK_STATUSES = ("succeeded", "succeeded_sql_only", "awaiting_approval")


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
    # What was actually done to the generated artifact. Studio NEVER executes
    # model-generated Python in-process, so a passing python check means the
    # file PARSED — not that it runs.
    #   "syntax_checked_not_executed" | "sql_guarded" | "none"
    artifact_status: str = "none"
    validator: str = "Validator"


class DeploymentRequest(BaseModel):
    target: str
    kind: str                       # "sql_script" | "spark_job"
    risk: str = "read"              # read | write | job
    decision: str = "reject"        # approve | needs_human | reject
    reasons: list[str] = []
    script: str = ""
    # What this request actually deploys. `script` is built from the VERIFIED
    # SQL steps (or a Spark payload of them) — the generated Python artifact
    # is a deliverable the user takes away, and is never submitted or run.
    deploys: str = "sql_steps"      # "sql_steps" | "spark_job"
    artifact_deployed: bool = False
    approver: str = "Approval agent"


class ExecutionResult(BaseModel):
    # succeeded | awaiting_approval | deploy_failed | rejected | rolled_back | failed | skipped
    status: str
    target: str
    deploy: dict | None = None      # {ok, detail} — submission to the platform
    run: dict | None = None         # {ok, attempts, detail} — execution + retries
    rollback: dict | None = None    # {done, detail} — undo on terminal run failure
    result: object | None = None
    error: str | None = None
    job_id: str | None = None
    # Plain-English statement of WHAT RAN, so no reader has to infer it from
    # the presence of an artifact. Rendered in the pipeline view and the email.
    deployed: str | None = None
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


# ── Lakehouse output contract (spark_job write→read loop) ────────────────
#
# A spark_job flow may DECLARE an S3 parquet sink. That declaration threads —
# unchanged — through request_approval into the job script, so on genuine job
# SUCCESS the supervisor's write→read bridge can auto-register it as a queryable
# dataset. The contract mirrors objectstore's DatasetIn ({source,name,uri,
# format}) plus `from_dataset`, the registered SOURCE dataset the job read, from
# which objectstore derives the allowed output prefix.

def _source_dataset(spec):
    """The registered objectstore dataset the job reads — the first matched
    table that resolves to a real dataset on the spec's source. Lazily imports
    objectstore so nothing here depends on it being configured."""
    try:
        from .connectors import objectstore
        want = set(spec.matched_tables or [])
        for d in objectstore.datasets(spec.source):
            if d["name"] in want:
                return d
    except Exception:
        return None
    return None


def _dir_of(uri):
    """Directory a parquet read-glob points into: cut at the first glob char,
    then keep up to the last '/'. s3://b/orders/*.parquet -> s3://b/orders/ .
    The Spark job writes to this dir; the read-glob registers it back."""
    cut = min((uri.find(c) for c in "*?[" if c in uri), default=len(uri))
    base = uri[:cut]
    slash = base.rfind("/")
    return base[:slash + 1] if slash > len("s3://") else base


def _resolve_output(spec, output):
    """Resolve a spark_job flow's declared S3 parquet sink into the full output
    contract, or None when the loop can't be closed (no matched source
    dataset). A caller-declared `output` is honoured and back-filled; otherwise
    a safe default sink is derived UNDER the source dataset's own prefix, so it
    passes objectstore's prefix confinement without any extra caller input."""
    from_ds = next(iter(spec.matched_tables or []), None)
    if isinstance(output, dict) and output.get("uri"):
        out = dict(output)
        out.setdefault("source", spec.source)
        out.setdefault("format", "parquet")
        out.setdefault("from_dataset", from_ds)
        out.setdefault("name", f"{from_ds}_out" if from_ds else "spark_out")
        return out
    src = _source_dataset(spec)
    if not src or not from_ds:
        return None
    name = f"{from_ds}_out"
    uri = f"{_dir_of(src['uri'])}{name}/*.parquet"   # stays under the source prefix
    return {"source": spec.source, "name": name, "uri": uri,
            "format": "parquet", "from_dataset": from_ds}


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


def generate(user, spec, kind="sql_script", output=None):
    """Code generator → GeneratedArtifact. Reuses pybuild (existing scripts via
    MCP as context); the planned SQL steps are handed over as reference.

    For a spark_job with a declared S3 output, the prompt is steered to emit a
    PySpark job that READS the registered S3 source dataset, applies the steps
    as DataFrame transforms, and WRITES parquet to the declared sink dir — so
    the generated code closes the lakehouse loop. This only shapes the prompt;
    nothing runs here, so it stays dormant-safe without any credentials."""
    t0 = time.time()
    context = "\n\n".join(f"-- {s.name}\n{s.sql}" for s in spec.steps if s.sql)
    prompt = (f"Build a deployable pipeline for: {spec.request}. Source: {spec.source}. "
              f"Implement these steps as one runnable job.")
    if kind == "spark_job" and isinstance(output, dict) and output.get("uri"):
        src = _source_dataset(spec)
        src_uri = (src or {}).get("uri")
        out_dir = _dir_of(output["uri"])
        if src_uri:
            prompt += (
                f"\n\nEmit a PySpark job: read parquet from '{src_uri}' (the registered "
                f"'{output.get('from_dataset')}' dataset on {spec.source}), apply the SQL "
                f"steps above as DataFrame transforms, and write the result as parquet to "
                f"'{out_dir}' with mode='overwrite'. Read the source by its S3 URI and take "
                f"credentials from the Spark environment — never hardcode them.")
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


def repair(user, spec, artifact, validation, attempt):
    """Repair → a new GeneratedArtifact. Feeds the validation errors back to the
    code generator so it can fix the artifact. Recorded as its own rollout, so
    Agent Lightning sees the failure and the repair attempt. Without an LLM key
    the generator can't actually fix anything — the loop then exhausts and the
    pipeline correctly goes to human review rather than shipping a bad artifact."""
    t0 = time.time()
    errs = "\n".join(f"- {e}" for e in validation.errors)
    prompt = (f"The generated pipeline for '{spec.request}' failed validation. "
              f"Fix it so every check passes. Validation errors:\n{errs}")
    context = f"{artifact.code}\n\n# Validation errors to fix:\n# " + "\n# ".join(validation.errors)
    try:
        out = pybuild.build(pybuild.BuildIn(prompt=prompt, context=context), user)
        fixed = GeneratedArtifact(
            language="python", code=out.get("python", ""),
            mode="agent" if out.get("mode") == "agent" else "scaffold",
            mcp_servers=out.get("mcp_servers", []),
            note=f"repair attempt {attempt}: {out.get('note') or 'regenerated'}")
    except Exception as e:
        fixed = GeneratedArtifact(code=artifact.code, mode="scaffold",
                                  note=f"repair attempt {attempt} error: {e}")
    tid = _trace(user, "repair", "Code generator", spec.request, spec.source,
                 {**fixed.model_dump(), "attempt": attempt, "fixing": validation.errors[:5]},
                 ok=bool(fixed.code.strip()), reward=0.4,
                 error="; ".join(validation.errors[:2]),
                 duration_ms=int((time.time() - t0) * 1000))
    return fixed, tid


#: The Python artifact check's name. It carries the caveat IN THE NAME because
#: the name is what the stage view, the pipeline view and the email render —
#: "python syntax" alone read as "the generated program works".
PY_CHECK = "python syntax (static only — not executed)"


def validate(user, spec, artifact):
    """Validator → ValidationResult. Re-verifies every planned step (RBAC +
    guard + real execution) and STATICALLY checks the generated code — the
    artifact is parsed, never run (see PY_CHECK / artifact_status)."""
    t0 = time.time()
    checks, errors = [], []
    for s in spec.steps:
        v = queries.verify_sql(user, spec.source, s.table, s.sql)
        checks.append(Check(name=f"step: {s.name}", ok=v["ok"],
                            detail=None if v["ok"] else v.get("error")))
        if not v["ok"]:
            errors.append(f"{s.name}: {v.get('error')}")

    # Static check of the generated code. Studio's invariant is that it NEVER
    # executes model-generated Python — not here, not in the executor — so this
    # is a PARSE, and the check is named to say so. A Python artifact that
    # compiles can still raise on its first line at runtime; nothing in this
    # flow has ever run it, and nothing downstream may claim otherwise.
    artifact_status = "none"
    if artifact.code.strip():
        if artifact.language == "python":
            artifact_status = "syntax_checked_not_executed"
            try:
                compile(artifact.code, "<artifact>", "exec")
                checks.append(Check(name=PY_CHECK, ok=True,
                                    detail="parsed only — Studio does not run generated Python"))
            except SyntaxError as e:
                checks.append(Check(name=PY_CHECK, ok=False, detail=str(e)))
                errors.append(f"{PY_CHECK}: {e}")
        else:
            artifact_status = "sql_guarded"
            bad = queryguard.FORBIDDEN.search(artifact.code)
            checks.append(Check(name="sql guard", ok=not bad,
                                detail=None if not bad else f"forbidden: {bad.group(0)}"))
            if bad:
                errors.append(f"sql guard: {bad.group(0)}")

    ok = not errors and bool(spec.steps)
    result = ValidationResult(ok=ok, checks=checks, errors=errors,
                              artifact_status=artifact_status)
    tid = _trace(user, "validate", "Validator", spec.request, spec.source,
                 result.model_dump(), ok=ok, reward=1.0 if ok else 0.0,
                 error=None if ok else "; ".join(errors[:3]),
                 duration_ms=int((time.time() - t0) * 1000))
    return result, tid


def request_approval(user, spec, validation, target, kind, output=None):
    """Approval agent → DeploymentRequest. The supervisor classifies risk and
    decides approve / needs_human; an invalid pipeline is rejected outright.

    A declared spark_job `output` (the S3 parquet sink) is embedded as a sibling
    key in the job script here; it threads immutably through supervisor.submit
    into supervised_jobs.script, so the write→read bridge can read it back on
    SUCCESS and register the output as a queryable dataset.

    The Spark payload is built by databricks_conn.build_submission — a real
    Jobs 2.1 run-submit body (one sql_task per verified step, each with a
    unique task_key and the configured warehouse), not a hand-rolled dict the
    endpoint would 400 on. If the body CANNOT be built (no
    DATABRICKS_WAREHOUSE_ID, no statements) the request is REFUSED here with
    that reason: a submission the API would reject is worse than an honest
    "not configured", and nothing is posted. The human-approval gate below is
    untouched — this is about the body, not the policy."""
    t0 = time.time()
    # What actually gets deployed: the verified read-only steps as a script, or
    # (for a Spark target) a job payload the executor submits.
    if kind == "spark_job":
        try:
            payload = databricks_conn.build_submission(
                [(s.name, s.sql) for s in spec.steps if (s.sql or "").strip()],
                run_name=f"studio: {spec.request}")
        except ValueError as e:
            req = DeploymentRequest(target=target, kind=kind, risk="job",
                                    decision="reject", reasons=[str(e)], script="",
                                    deploys="spark_job", artifact_deployed=False)
            tid = _trace(user, "approve", "Approval agent", spec.request, target,
                         req.model_dump(), ok=False, reward=0.0, error=str(e),
                         duration_ms=int((time.time() - t0) * 1000))
            return req, tid
        if isinstance(output, dict) and output.get("uri"):
            # Studio-private sibling key: stripped from the posted body by the
            # connector, read back off the stored script by the bridge.
            payload["output"] = output
        script = json.dumps(payload)
    else:
        script = ";\n".join(s.sql for s in spec.steps if s.sql)

    # The artifact is a DELIVERABLE, never a deployable: `script` above is built
    # from the verified SQL steps (or a Spark payload of them) and that is the
    # only thing supervisor.submit ever sees.
    deploys = "spark_job" if kind == "spark_job" else "sql_steps"

    if not validation.ok:
        req = DeploymentRequest(target=target, kind=kind, risk="n/a", decision="reject",
                                reasons=["Validation failed — not eligible for deployment."],
                                script=script, deploys=deploys, artifact_deployed=False)
        tid = _trace(user, "approve", "Approval agent", spec.request, target,
                     req.model_dump(), ok=False, reward=0.0, error="validation failed",
                     duration_ms=int((time.time() - t0) * 1000))
        return req, tid

    verdict = supervisor.supervise(kind, target, script, user)
    req = DeploymentRequest(target=target, kind=kind, risk=verdict["risk"],
                            decision=verdict["decision"], reasons=verdict["reasons"],
                            script=script, deploys=deploys, artifact_deployed=False)
    # needs_human is not a failure — it's the gate working as designed.
    reward = {"approve": 1.0, "needs_human": 0.7, "reject": 0.0}.get(verdict["decision"], 0.0)
    tid = _trace(user, "approve", "Approval agent", spec.request, target,
                 req.model_dump(), ok=verdict["decision"] != "reject", reward=reward,
                 error=None if verdict["decision"] != "reject" else "; ".join(verdict["reasons"][:2]),
                 duration_ms=int((time.time() - t0) * 1000))
    return req, tid


def deployed_note(spec, deployment, artifact_status, status):
    """One sentence naming WHAT RAN — the phrase the pipeline view and the
    email print. Written here, once, so the view, the email and the trace can
    never drift into implying the generated Python was deployed."""
    n = sum(1 for s in spec.steps if s.sql)
    plural = "" if n == 1 else "s"
    if status in ("deploy_failed", "rejected"):
        ran = "nothing was deployed"
    elif deployment.deploys == "spark_job":
        ran = f"submitted a Spark job built from the {n} verified SQL step{plural}"
    else:
        ran = f"deployed the {n} verified SQL step{plural}"
    if artifact_status == "syntax_checked_not_executed":
        ran += ("; the generated Python artifact is a deliverable and was not "
                "executed (syntax-checked only)")
    return ran


def deploy_execute(user, spec, deployment, artifact_status="none"):
    """Airflow / Databricks executor → ExecutionResult, in two phases:

      Deploy — submit to the platform via supervisor.submit (policy enforced).
               A deploy failure reports the error and does NOT bypass policy.
      Run   — execution, with the platform's automatic retries. If it keeps
              failing the run is STOPPED, the requester is ALERTED, and the
              deployment is ROLLED BACK (best-effort, per connector).

    What is deployed is `deployment.script` — the VERIFIED SQL steps, or a
    Spark payload built from them. The generated Python artifact is never
    submitted and never executed; `artifact_status` only flows in so the
    result can SAY that (ExecutionResult.deployed).
    """
    t0 = time.time()
    executor = roster.executor_name(deployment.target)

    def done(res, reward, error=None):
        res.deployed = deployed_note(spec, deployment, artifact_status, res.status)
        tid = _trace(user, "execute", executor, spec.request, deployment.target,
                     res.model_dump(), ok=res.status in ("succeeded", "awaiting_approval"),
                     reward=reward, error=error, duration_ms=int((time.time() - t0) * 1000))
        return res, tid

    # ── Deploy: submit. supervisor is the ONLY executor, so policy (RBAC +
    # human approval for writes/jobs) can never be bypassed here. ──
    try:
        job = supervisor.submit(deployment.kind, deployment.target, deployment.script, user)
    except HTTPException as e:
        _alert(user, spec.request, "deploy", [e.detail])
        return done(ExecutionResult(status="deploy_failed", target=deployment.target,
                                    deploy={"ok": False, "detail": e.detail},
                                    error=e.detail, executor=executor),
                    reward=0.3, error=e.detail)

    js = job["status"]
    if js == "rejected":   # blocked by policy — reported, never bypassed
        return done(ExecutionResult(status="rejected", target=deployment.target,
                                    deploy={"ok": False, "detail": "blocked by policy"},
                                    error="; ".join(deployment.reasons[:2]), executor=executor),
                    reward=0.0, error="policy reject")
    if js == "awaiting_approval":   # deployed; run deferred to a human
        return done(ExecutionResult(status="awaiting_approval", target=deployment.target,
                                    deploy={"ok": True, "detail": "submitted"},
                                    run={"ok": None, "detail": "pending human approval"},
                                    job_id=job["id"], executor=executor), reward=0.7)

    # ── Run outcomes (supervisor already retried up to its max) ──
    if js in ("succeeded", "running"):
        return done(ExecutionResult(status="succeeded", target=deployment.target,
                                    deploy={"ok": True, "detail": "submitted"},
                                    run={"ok": True, "attempts": (job.get("attempts") or 0) + 1},
                                    result=job.get("result"), job_id=job["id"], executor=executor),
                    reward=1.0)
    if js == "escalated":   # kept failing → stop, alert, rollback
        rb = _rollback(user, deployment, job)
        _alert(user, spec.request, "run",
               [job.get("last_error"), "run stopped after retries; rolled back"])
        return done(ExecutionResult(status="rolled_back", target=deployment.target,
                                    deploy={"ok": True, "detail": "submitted"},
                                    run={"ok": False, "attempts": job.get("attempts"),
                                         "detail": job.get("last_error")},
                                    rollback=rb, error=job.get("last_error"),
                                    job_id=job["id"], executor=executor),
                    reward=0.0, error=job.get("last_error"))
    return done(ExecutionResult(status="failed", target=deployment.target,
                                deploy={"ok": True, "detail": "submitted"},
                                run={"ok": False, "detail": job.get("last_error")},
                                error=job.get("last_error"), job_id=job["id"], executor=executor),
                reward=0.0, error=job.get("last_error"))


def _rollback(user, deployment, job):
    """Best-effort undo after a terminal run failure. Read-only deployments have
    nothing to roll back; for writes/jobs, call the connector's rollback hook —
    if it has none, flag that manual intervention is required (never silent)."""
    if deployment.risk == "read":
        return {"done": True, "detail": "read-only — nothing to roll back"}
    try:
        conn = connector_or_400(deployment.target)
        conn.rollback(job.get("result"))
        return {"done": True, "detail": f"rolled back on {deployment.target}"}
    except NotImplementedError:
        return {"done": False,
                "detail": f"{deployment.target} has no rollback hook — manual intervention required"}
    except Exception as e:
        return {"done": False, "detail": f"rollback failed: {str(e)[:200]}"}


def _alert(user, request, phase, details):
    """Alert the requester when a stage fails terminally (human review needed,
    deploy error, or a run stopped + rolled back)."""
    note = {"validation": "Automated repair was exhausted — human review is required.",
            "deploy": "Deployment error — policy was not bypassed; nothing was run.",
            "run": "The run was stopped after retries, you were alerted, and it was rolled back."
            }.get(phase, "")
    body = "; ".join(str(d) for d in details if d)
    try:
        email_service.send(
            user["email"], f"Studio pipeline alert: {phase} failed",
            f"<p>The <b>{phase}</b> stage failed for your request:</p><p>{request}</p>"
            f"<pre style='background:#f6f6f6;padding:8px;border-radius:6px'>{body}</pre>"
            f"<p>{note}</p>")
    except Exception:
        pass


# ── Persistence + driver ─────────────────────────────────────────────────

def init_tables():
    with db.connect() as c:
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


def run_flow(user, request, target=None, kind="sql_script", output=None):
    """Drive one business request through the safe-production flow:

        generate → validate ─(fail)→ repair ×N ─(still failing)→ human review
                 → approval → deploy ─(fail)→ report, no policy bypass
                 → run ─(fail)→ retries ─(still failing)→ stop, alert, rollback
                 → record result + reward (every stage) in Agent Lightning.
    """
    request = (request or "").strip()
    if not request:
        raise HTTPException(400, "Describe the pipeline to build")
    traces = {}

    spec, traces["plan"], perr = plan(user, request)
    if perr:
        return _finish(user, request, target or spec.source, kind, "plan_failed",
                       spec, None, None, None, None, traces)

    target = target or spec.source
    # For a spark_job, resolve the declared (or defaulted) S3 parquet sink once,
    # then steer codegen with it and carry it through approval into the job so
    # the write→read bridge can register it on SUCCESS.
    declared_output = _resolve_output(spec, output) if kind == "spark_job" else None
    artifact, traces["codegen"] = generate(user, spec, kind, declared_output)

    # Validate, and on failure repair up to N times before re-validating. If it
    # still won't pass, STOP before approval and send it to human review.
    validation, traces["validate"] = validate(user, spec, artifact)
    repairs = []
    while not validation.ok and len(repairs) < MAX_REPAIRS:
        artifact, rtid = repair(user, spec, artifact, validation, len(repairs) + 1)
        repairs.append(rtid)
        validation, traces["validate"] = validate(user, spec, artifact)
    if repairs:
        traces["repair"] = repairs
    if not validation.ok:
        _alert(user, request, "validation", validation.errors)
        return _finish(user, request, target, kind, "human_review", spec, artifact,
                       validation, None, None, traces)

    deployment, traces["approve"] = request_approval(user, spec, validation, target, kind,
                                                      declared_output)
    if deployment.decision == "reject":
        return _finish(user, request, target, kind, "rejected", spec, artifact,
                       validation, deployment, None, traces)

    execution, traces["execute"] = deploy_execute(user, spec, deployment,
                                                  validation.artifact_status)
    status = {"succeeded": "succeeded", "awaiting_approval": "awaiting_approval",
              "deploy_failed": "deploy_failed", "rolled_back": "rolled_back",
              "failed": "execute_failed", "rejected": "rejected"}.get(
                  execution.status, execution.status)
    # A run that produced a Python artifact must NOT read as a plain success:
    # the SQL steps ran, the artifact did not. "succeeded_sql_only" is the
    # honest headline, and _pipeline_view/_pipeline_email spell out the rest.
    if status == "succeeded" and validation.artifact_status == "syntax_checked_not_executed":
        status = "succeeded_sql_only"
    return _finish(user, request, target, kind, status, spec, artifact,
                   validation, deployment, execution, traces)


def _finish(user, request, target, kind, status, spec, artifact, validation,
            deployment, execution, traces):
    fid = str(uuid.uuid4())
    now = time.time()
    job_id = execution.job_id if execution else None
    with db.connect() as c:
        c.execute(
            "INSERT INTO flow_runs (id, user_id, request, target, kind, status, spec, "
            "artifact, validation, deployment, execution, trace_ids, job_id, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, user["id"], request, target, kind, status,
             _dump(spec), _dump(artifact), _dump(validation), _dump(deployment),
             _dump(execution), json.dumps(traces), job_id, now, now),
        )
        c.commit()
    db.log_activity(user, "flow_run", prompt=request[:200], source=target,
                    ok=status in OK_STATUSES)
    resp = {
        "id": fid, "status": status, "request": request, "target": target, "kind": kind,
        "spec": _obj(spec), "artifact": _obj(artifact), "validation": _obj(validation),
        "deployment": _obj(deployment), "execution": _obj(execution),
        "trace_ids": traces, "job_id": job_id,
        "stages": _stage_view(spec, artifact, validation, deployment, execution, traces),
    }
    resp["pipeline"] = _pipeline_view(resp)
    # Every run emails a per-step digest (source -> transformations -> target),
    # on both success and failure.
    _pipeline_email(user, request, resp["pipeline"], status)
    return resp


def _stage_view(spec, artifact, validation, deployment, execution, traces):
    """Compact, ordered summary of the chain — reuses the dict builder so the
    live response and a re-fetched run render identically."""
    return _stage_view_from_dict({
        "spec": _obj(spec), "artifact": _obj(artifact), "validation": _obj(validation),
        "deployment": _obj(deployment), "execution": _obj(execution), "trace_ids": traces})


def _dump(model):
    return json.dumps(model.model_dump(), default=str) if model is not None else None


def _obj(model):
    return model.model_dump() if model is not None else None


# ── API ──────────────────────────────────────────────────────────────────

class RunIn(BaseModel):
    request: str
    target: str | None = None       # deploy target; defaults to the planned source
    kind: str = "sql_script"        # "sql_script" | "spark_job"
    # Optional S3 parquet sink for a spark_job: {name, uri, format?, source?,
    # from_dataset?}. Omit and a safe default under the source dataset's prefix
    # is derived. Used only for kind="spark_job".
    output: dict | None = None


@router.post("/run")
def run(body: RunIn, user=Depends(current_user)):
    if body.kind not in ("sql_script", "spark_job"):
        raise HTTPException(400, "kind must be 'sql_script' or 'spark_job'")
    return run_flow(user, body.request, body.target, body.kind, body.output)


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
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, request, target, kind, status, job_id, created_at, updated_at "
            "FROM flow_runs WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",
            (user["id"],)).fetchall()
    return {"flows": [dict(r) for r in rows]}


@router.get("/{fid}")
def get(fid: str, user=Depends(current_user)):
    with db.connect() as c:
        r = c.execute("SELECT * FROM flow_runs WHERE id=?", (fid,)).fetchone()
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
    d["pipeline"] = _pipeline_view(d)
    return d


@router.delete("/{fid}")
def remove(fid: str, user=Depends(current_user)):
    with db.connect() as c:
        r = c.execute("SELECT user_id FROM flow_runs WHERE id=?", (fid,)).fetchone()
        if r is None or dict(r)["user_id"] != user["id"]:
            raise HTTPException(404, "Flow not found")  # 404, not 403 — no oracle
        c.execute("DELETE FROM flow_runs WHERE id=?", (fid,))
        c.commit()
    return {"deleted": True}


def _pipeline_view(d):
    """The DATA pipeline (not the agent chain): source -> each transformation
    step -> where it runs, with per-step pass/fail. This is what the Flow view
    draws and what the per-step email reports. Step status comes from the
    Validator's per-step checks (RBAC + guard + real execution)."""
    spec = d.get("spec") or {}
    val = d.get("validation") or {}
    dep = d.get("deployment") or {}
    ex = d.get("execution") or {}
    check_by = {}
    for c in (val.get("checks") or []):
        nm = c.get("name", "")
        if nm.startswith("step: "):
            check_by[nm[len("step: "):]] = c
    steps = []
    for st in (spec.get("steps") or []):
        c = check_by.get(st.get("name"))
        if c is None:
            status, detail = "pending", None
        else:
            status, detail = ("ok" if c.get("ok") else "failed"), c.get("detail")
        steps.append({"name": st.get("name"), "table": st.get("table"),
                      "sql": st.get("sql"), "status": status, "detail": detail})
    # Lakehouse loop: the declared S3 parquet sink (from the immutable job
    # script) and, once the job succeeds, the dataset the bridge registered it
    # as — or the refused-and-surfaced error. Together these let the Flow graph
    # draw S3 source -> Spark -> S3 parquet -> dataset -> visualize.
    out = None
    try:
        sc = dep.get("script")
        out = (json.loads(sc) or {}).get("output") if sc else None
    except Exception:
        out = None
    res = ex.get("result")
    reg = res.get("registered_dataset") if isinstance(res, dict) else None
    reg = reg if isinstance(reg, dict) else {}
    # What actually ran vs. what was merely produced. The generated Python is
    # a deliverable: it is never submitted and never executed, so the view
    # states that in words rather than leaving the reader to infer it from the
    # artifact panel sitting next to a green tick.
    artifact_status = val.get("artifact_status") or "none"
    deployed = ex.get("deployed")
    if not deployed and artifact_status == "syntax_checked_not_executed":
        deployed = ("nothing was deployed; the generated Python artifact is a "
                    "deliverable and was not executed (syntax-checked only)")
    return {
        "source": spec.get("source"),
        "steps": steps,
        "target": dep.get("target") or spec.get("source"),
        "kind": dep.get("kind"),
        "risk": dep.get("risk"),
        "decision": dep.get("decision"),
        "executor": ex.get("executor"),
        "run_status": ex.get("status"),
        "deploys": dep.get("deploys"),                # "sql_steps" | "spark_job"
        "artifact_deployed": bool(dep.get("artifact_deployed")),
        "artifact_status": artifact_status,
        "deployed": deployed,                         # plain-English "what ran"
        "deploy_ok": (ex.get("deploy") or {}).get("ok") if ex.get("deploy") else None,
        "run_ok": (ex.get("run") or {}).get("ok") if ex.get("run") else None,
        "job_id": ex.get("job_id"),
        "output": out,                              # declared S3 parquet sink, or None
        "registered_dataset": reg.get("registered"),  # queryable dataset name, or None
        "register_error": reg.get("error"),        # set when registration was refused
    }


def _pipeline_email(user, request, pv, overall):
    """One digest per run reporting EACH step's success/failure (not 5 separate
    emails). Sent on both success and failure, so every run's per-step outcome
    lands in the inbox; _alert still fires for the urgent 'action required' cases."""
    ok = overall in OK_STATUSES
    color = {"ok": "#0e7a4d", "failed": "#c5221f", "pending": "#79766f"}
    label = {"ok": "PASS", "failed": "FAIL", "pending": "pending"}
    rows = "".join(
        f"<tr><td>{i + 1}. {s['name']}</td><td>{s.get('table') or ''}</td>"
        f"<td style='color:{color.get(s['status'], '#79766f')};font-weight:700'>{label.get(s['status'], s['status'])}</td>"
        f"<td>{(s.get('detail') or '')[:140]}</td></tr>"
        for i, s in enumerate(pv.get("steps") or []))
    runs_on = pv.get("executor") or pv.get("target") or "?"
    # The headline is the REAL status, not a success/failure coin flip: a
    # "succeeded_sql_only" run must not arrive in the inbox reading "succeeded".
    headline = (overall or "").replace("_", " ") or ("succeeded" if ok else "failed")
    # ...and the body names what ran, so nobody reads a green digest as
    # "the generated Python artifact ran".
    what_ran = pv.get("deployed")
    ran_line = f"<p>What ran: <b>{what_ran}</b>.</p>" if what_ran else ""
    html = (
        f"<p>Pipeline <b>{headline}</b> for your request:</p>"
        f"<p><i>{request}</i></p>"
        f"{ran_line}"
        f"<p>Source <b>{pv.get('source')}</b> &rarr; runs on <b>{runs_on}</b> "
        f"({pv.get('kind') or 'sql_script'}) &rarr; status <b>{pv.get('run_status') or overall}</b></p>"
        f"<table cellpadding='6' style='border-collapse:collapse;border:1px solid #e3e1de'>"
        f"<tr style='background:#f5f4f2'><th align='left'>Step (transformation)</th>"
        f"<th align='left'>Table</th><th align='left'>Result</th><th align='left'>Detail</th></tr>"
        f"{rows}</table>")
    try:
        email_service.send(user["email"],
                           f"Studio pipeline {headline}: {request[:60]}", html)
    except Exception:
        pass


def _stage_view_from_dict(d):
    spec, art = d.get("spec") or {}, d.get("artifact") or {}
    val, dep = d.get("validation"), d.get("deployment")
    ex, traces = d.get("execution"), d.get("trace_ids") or {}
    repairs = traces.get("repair") or []
    out = [
        {"stage": "plan", "agent": "Pipeline planner", "produces": "PipelineSpec",
         "ok": bool(spec.get("steps")), "trace_id": traces.get("plan")},
        # The artifact is produced, never run — say so on the stage that makes
        # it, not only in the fine print of the validation result.
        {"stage": "codegen", "agent": "Code generator", "produces": "GeneratedArtifact",
         "ok": bool(art.get("code")), "trace_id": traces.get("codegen"),
         "note": "deliverable — Studio does not execute generated Python"},
    ]
    if val is not None:
        row = {"stage": "validate", "agent": "Validator", "produces": "ValidationResult",
               "ok": bool(val.get("ok")), "trace_id": traces.get("validate"),
               "artifact_status": val.get("artifact_status") or "none"}
        if repairs:
            row["repairs"] = len(repairs)   # validate→repair loop ran this many times
        out.append(row)
    # Approval and execution only appear if the flow actually reached them.
    if dep is not None:
        out.append({"stage": "approve", "agent": "Approval agent", "produces": "DeploymentRequest",
                    "ok": dep.get("decision") not in (None, "reject"), "trace_id": traces.get("approve")})
    if ex is not None:
        out.append({"stage": "execute", "agent": roster.executor_name((dep or {}).get("target")),
                    "produces": "ExecutionResult",
                    "ok": ex.get("status") in ("succeeded", "awaiting_approval"),
                    "deployed": ex.get("deployed"),   # what actually ran
                    "trace_id": traces.get("execute")})
    return out
