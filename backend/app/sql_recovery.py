"""Bounded, durable agent recovery for explicitly enrolled read-only runs.

This is orchestration, not reward editing: every execution keeps its own
pipeline_runs row and immutable learning outcome. A queue retry resumes the
same recovery attempt; it does not spend a new execution attempt.
"""
import json
import logging
import time
import uuid

from fastapi import HTTPException

from . import db, gateway, governance, jobs, queryguard

log = logging.getLogger(__name__)
KIND = "sql_agent_recovery"
OUTCOME_KIND = "sql_recovery_outcome"
MAX_ATTEMPTS = 2
_FINAL = {"succeeded", "escalated"}


def init_tables():
    with db.connect() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sql_agent_recoveries (
                root_run_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                current_run_id TEXT NOT NULL,
                pipeline TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                reason TEXT,
                history TEXT NOT NULL DEFAULT '[]',
                candidate TEXT,
                model TEXT,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sql_agent_recovery_run
                ON sql_agent_recoveries(current_run_id);
        """)
        c.commit()


def _row(row):
    if row is None:
        return None
    out = dict(row)
    for key in ("pipeline", "history", "candidate", "model"):
        out[key] = json.loads(out[key]) if out.get(key) else None
    return out


def _load(root):
    with db.connect() as c:
        return _row(c.execute("SELECT * FROM sql_agent_recoveries WHERE root_run_id=?", (root,)).fetchone())


def _snapshot(pipeline):
    # No query rows, arbitrary client metadata, or model credentials persist.
    out = {key: pipeline.get(key) for key in
           ("id", "name", "prompt", "source", "conversation_id", "repairs_run_id")}
    out["steps"] = [{key: step.get(key) for key in ("name", "source", "table", "sql")}
                    for step in pipeline.get("steps") or []]
    out.update(status="ready", execution_mode="read_only_sql", dropped=[])
    return out


def watch(pipeline, user, run_id):
    """Enroll before execution. Ordinary callers without opt-in are untouched."""
    policy = pipeline.get("agent_recovery") or {}
    if policy.get("enabled") is not True:
        return
    from .chat_pipelines import _sql_only
    if not pipeline.get("steps") or any(not _sql_only(step.get("sql") or "") for step in pipeline["steps"]):
        return
    init_tables()
    jobs.check_claim()
    root = policy.get("root_run_id") or run_id
    if root != run_id:
        # Only the registered recovery attempt may join an existing chain.
        row = _load(root)
        if (not row or row["user_id"] != user["id"] or row["current_run_id"] != run_id
                or row["attempts"] != policy.get("attempt")):
            raise HTTPException(409, "This recovery attempt does not belong to the pipeline")
        return
    try:
        maximum = min(MAX_ATTEMPTS, max(0, int(policy.get("max_attempts", MAX_ATTEMPTS))))
    except (ValueError, TypeError):
        maximum = MAX_ATTEMPTS
    # Model names/provider choices may be stored, never caller-supplied keys.
    model = policy.get("model")
    if isinstance(model, dict):
        model = {key: model[key] for key in ("provider", "model") if key in model}
    elif not isinstance(model, str):
        model = None
    with db.connect() as c:
        c.execute("INSERT INTO sql_agent_recoveries "
                  "(root_run_id,user_id,current_run_id,pipeline,state,max_attempts,model,updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(root_run_id) DO NOTHING",
                  (root, user["id"], run_id, json.dumps(_snapshot(pipeline)), "watching", maximum,
                   json.dumps(model) if model else None, time.time()))
        c.commit()


def _job_id(root, failed_run, poll=0):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "studio-sql-agent-recovery:" + json.dumps([root, failed_run, poll])))


def _ensure_claim(connection, claim):
    if claim is None:
        return
    # Hold the queue claim until the state write commits. A plain SELECT on
    # SQLite does not open a transaction, leaving a reclaim/write race.
    if not db.IS_PG and not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    suffix = " FOR UPDATE" if db.IS_PG else ""
    current = connection.execute("SELECT locked_by,status FROM background_jobs WHERE id=?" + suffix,
                                 (claim["id"],)).fetchone()
    if not current or current["status"] != "running" or current["locked_by"] != claim.get("locked_by"):
        raise jobs.ClaimLost("Recovery worker no longer owns this attempt")


def _set(root, state, reason, *, expected=None, claim=None):
    jobs.check_claim()
    with db.connect() as c:
        _ensure_claim(c, claim)
        sql = "UPDATE sql_agent_recoveries SET state=?,reason=?,updated_at=? WHERE root_run_id=?"
        args = [state, str(reason or "")[:2000], time.time(), root]
        if expected:
            sql += " AND current_run_id=?"
            args.append(expected)
        c.execute(sql, args)
        c.commit()


def _uncertain(error):
    text = str(error or "").lower()
    return any(term in text for term in
               ("unknown outcome", "outcome unknown", "uncertain outcome", "still running", "in-flight", "in flight", "was abandoned"))


def observe(run_id):
    """Schedule diagnosis only after a persisted, known failed execution."""
    with db.connect() as c:
        row = _row(c.execute("SELECT * FROM sql_agent_recoveries WHERE current_run_id=?", (run_id,)).fetchone())
        run = c.execute("SELECT * FROM pipeline_runs WHERE id=?", (run_id,)).fetchone()
    if not row or not run or run["user_id"] != row["user_id"]:
        return
    if run["status"] in ("success", "failed"):
        _queue_outcome(row, run)
    if row["state"] in _FINAL:
        return
    if run["status"] == "success":
        _set(row["root_run_id"], "succeeded", "Pipeline execution succeeded", expected=run_id)
        return
    if run["status"] != "failed":
        return
    if _uncertain(run["error"]):
        _set(row["root_run_id"], "escalated", "Execution outcome is uncertain; the agent will not retry", expected=run_id)
        return
    if row["state"] not in ("watching", "executing"):
        return
    if row["attempts"] >= row["max_attempts"]:
        _set(row["root_run_id"], "escalated", "Agent recovery attempt limit reached", expected=run_id)
        return
    jobs.check_claim()
    with db.connect() as c:
        cur = c.execute("UPDATE sql_agent_recoveries SET state='queued',reason=?,candidate=NULL,updated_at=? "
                        "WHERE root_run_id=? AND current_run_id=? AND state IN ('watching','executing')",
                        ("The recovery agent will diagnose this failed run", time.time(), row["root_run_id"], run_id))
        if cur.rowcount:
            jobs.enqueue(KIND, {"root_run_id": row["root_run_id"], "failed_run_id": run_id},
                         user_id=row["user_id"], job_id=_job_id(row["root_run_id"], run_id), max_attempts=2, conn=c)
        c.commit()


def _authorized(row):
    from .chat_pipelines import _sql_only
    user = db.get_user(row["user_id"])
    if not user or not user.get("verified") or user.get("role") not in ("admin", "analyst", "viewer"):
        raise HTTPException(403, "The requesting account is no longer authorized")
    for step in row["pipeline"]["steps"]:
        jobs.check_claim()
        if not _sql_only(step.get("sql") or ""):
            raise HTTPException(403, "Agent retries only execute read-only SQL")
        gateway.check(user, step["source"], step["sql"], table_label=step.get("table") or "*")
    return user


def _schema(user, pipeline):
    """Read catalog metadata inside the same current-role scope, never rows."""
    out = {}
    for source in dict.fromkeys(step["source"] for step in pipeline["steps"]):
        connector, allowed = gateway.scope(user, source)
        referenced = {table for step in pipeline["steps"] if step["source"] == source
                      for table in queryguard.base_tables(step["sql"])}
        out[source] = {}
        for table in allowed:
            if table.lower() not in {name.lower() for name in referenced}:
                continue
            denied = governance.column_rules(source, table)["deny"]
            out[source][table] = [column for column in connector.get_schema(table)
                                  if column.get("name", "").lower() not in denied]
    return out


def status(run_id, user):
    """Safe lineage/status metadata, scoped to the original execution owner."""
    with db.connect() as c:
        rows = c.execute("SELECT * FROM sql_agent_recoveries WHERE user_id=?", (user["id"],)).fetchall()
    for raw in rows:
        row = _row(raw)
        known = {row["root_run_id"], row["current_run_id"]}
        known.update(h.get("failed_run_id") for h in row["history"] or [])
        if run_id not in known:
            continue
        _authorized(row)
        if not row["history"] and row["state"] in ("watching", "succeeded"):
            return None
        public_state = {"queued": "pending", "planning": "diagnosing", "executing": "retrying"}.get(row["state"], row["state"])
        if row["state"] == "escalated" and row["attempts"] >= row["max_attempts"]:
            public_state = "exhausted"
        with db.connect() as c:
            child = c.execute("SELECT pipeline_id,status FROM pipeline_runs WHERE id=?", (row["current_run_id"],)).fetchone()
        return {"enabled": True, "root_run_id": row["root_run_id"], "current_run_id": row["current_run_id"],
                "status": row["state"], "attempts": row["attempts"], "max_attempts": row["max_attempts"],
                "state": public_state, "attempt": row["attempts"],
                "child_run_id": row["current_run_id"] if row["attempts"] else None,
                "child_pipeline_id": child["pipeline_id"] if child and row["attempts"] else None,
                "child_state": child["status"] if child and row["attempts"] else None,
                "repairs_run_id": row["history"][-1]["failed_run_id"] if row["history"] else None,
                "reason": row["reason"], "history": [{key: item.get(key) for key in ("attempt", "failed_run_id", "poll", "diagnosis")}
                                                        for item in row["history"]]}
    return None


def _queue_outcome(row, run):
    policy = (row.get("candidate") or {}).get("agent_recovery") or {}
    rollout_id = policy.get("rollout_id")
    if not rollout_id or run["id"] == row["root_run_id"]:
        return
    payload = {"rollout_id": rollout_id, "run_id": run["id"], "status": run["status"], "error": run["error"]}
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "studio-sql-recovery-outcome:" + run["id"]))
    jobs.check_claim()
    with db.connect() as c:
        # Idempotent delivery enrollment even when terminal observers race.
        c.execute("INSERT INTO background_jobs (id,kind,payload,status,attempts,max_attempts,run_after,user_id,created_at) "
                  "VALUES (?,?,?,'queued',0,3,?,?,?) ON CONFLICT(id) DO NOTHING",
                  (job_id, OUTCOME_KIND, json.dumps(payload), time.time(), row["user_id"], time.time()))
        c.commit()


@jobs.handler(OUTCOME_KIND)
def _outcome(payload, job):
    from . import recovery_planner
    return recovery_planner.record_outcome(payload["rollout_id"], run_id=payload["run_id"],
                                            status=payload["status"], error=payload.get("error"))


@jobs.handler(KIND)
def _recover(payload, job):
    from . import chat_pipelines, recovery_planner
    root, failed_id = payload["root_run_id"], payload["failed_run_id"]
    row = _load(root)
    if not row or row["state"] in _FINAL:
        return {"status": row["state"] if row else "missing"}
    # A queue replay after the child was launched resumes that exact child.
    replay = row["state"] == "executing" and row["history"][-1]["failed_run_id"] == failed_id
    if row["current_run_id"] != failed_id and not replay:
        return {"status": "superseded"}
    try:
        with db.connect() as c:
            _ensure_claim(c, job)
        user = _authorized(row)
        with db.connect() as c:
            failed = c.execute("SELECT * FROM pipeline_runs WHERE id=? AND user_id=?", (failed_id, user["id"])).fetchone()
        if not failed or failed["status"] != "failed" or _uncertain(failed["error"]):
            _set(root, "escalated", "No known terminal failure is available for diagnosis", expected=row["current_run_id"], claim=job)
            return {"status": "escalated"}
        if row["state"] == "queued":
            if row["attempts"] >= row["max_attempts"]:
                _set(root, "escalated", "Agent recovery attempt limit reached", expected=row["current_run_id"], claim=job)
                return {"status": "escalated"}
            history = row["history"] + [{"attempt": row["attempts"] + 1, "failed_run_id": failed_id,
                                         "error": str(failed["error"] or "")[:2000]}]
            jobs.check_claim()
            with db.connect() as c:
                _ensure_claim(c, job)
                cur = c.execute("UPDATE sql_agent_recoveries SET state='planning',history=?,updated_at=? "
                                "WHERE root_run_id=? AND current_run_id=? AND state='queued'",
                                (json.dumps(history), time.time(), root, failed_id))
                c.commit()
            if not cur.rowcount:
                return {"status": "superseded"}
            row = _load(root)
        if row["state"] not in ("planning", "executing"):
            return {"status": row["state"]}
        history = row["history"]
        diagnosis = history[-1].get("diagnosis")
        if diagnosis is None or diagnosis.get("decision") == "pending":
            diagnosis = recovery_planner.diagnose(
                user, prompt=row["pipeline"]["prompt"],
                action={"type": "sql_pipeline", "steps": row["pipeline"]["steps"]},
                error=failed["error"], history=[{"attempt": item["attempt"], "status": "failed", "error": item.get("error"),
                                                "decision": (item.get("diagnosis") or {}).get("decision"),
                                                "reason": (item.get("diagnosis") or {}).get("reason")}
                                               for item in history[:-1]], model=row["model"],
                request_id=f"sql-recovery:{root}:{failed_id}", schema=_schema(user, row["pipeline"]))
            if not isinstance(diagnosis, dict) or diagnosis.get("decision") not in ("pending", "retry", "repair", "escalate"):
                raise ValueError("The recovery agent returned no valid decision")
            diagnosis = {"decision": diagnosis["decision"], "reason": str(diagnosis.get("reason") or "")[:2000],
                         "action": diagnosis.get("action"), "rollout_id": diagnosis.get("rollout_id")}
            history[-1]["diagnosis"] = diagnosis
            if diagnosis["decision"] == "pending":
                polls = int(history[-1].get("poll", 0)) + 1
                if polls > 60:
                    _set(root, "escalated", "Agent Lightning diagnosis timed out; no new pipeline was executed", expected=failed_id, claim=job)
                    return {"status": "escalated"}
                history[-1]["poll"] = polls
                jobs.check_claim()
                with db.connect() as c:
                    _ensure_claim(c, job)
                    c.execute("UPDATE sql_agent_recoveries SET history=?,reason=?,updated_at=? WHERE root_run_id=?",
                              (json.dumps(history), "Waiting for Agent Lightning diagnosis", time.time(), root))
                    jobs.enqueue(KIND, payload, user_id=user["id"], run_after=time.time() + 5,
                                 job_id=_job_id(root, failed_id, polls), max_attempts=2, conn=c)
                    c.commit()
                return {"status": "planning", "rollout_id": diagnosis["rollout_id"]}
            jobs.check_claim()
            with db.connect() as c:
                _ensure_claim(c, job)
                c.execute("UPDATE sql_agent_recoveries SET history=?,reason=?,updated_at=? WHERE root_run_id=?",
                          (json.dumps(history), diagnosis["reason"], time.time(), root))
                c.commit()
        if diagnosis["decision"] == "escalate":
            _set(root, "escalated", diagnosis["reason"] or "The recovery agent needs human input", expected=failed_id, claim=job)
            return {"status": "escalated"}
        draft = row["candidate"]
        if draft is None:
            previous = dict(row["pipeline"])
            previous["run"] = dict(failed)
            previous["run"]["steps_result"] = json.loads(failed["steps_result"] or "[]")
            if diagnosis["decision"] == "repair":
                action = diagnosis.get("action") or {}
                steps = action.get("steps") or []
                if (action.get("type") != "sql_pipeline" or not steps or len(steps) != len(previous["steps"])
                        or any(not isinstance(step, dict) or not chat_pipelines._sql_only(step.get("sql") or "") for step in steps)):
                    raise ValueError("Agent Lightning did not return a complete read-only SQL repair")
                if any(step.get("source") not in {s["source"] for s in previous["steps"]} for step in steps):
                    raise ValueError("Agent Lightning cannot change the pipeline's source during automatic recovery")
                for step in steps:
                    original_tables = {table for original in previous["steps"] if original["source"] == step["source"]
                                       for table in queryguard.base_tables(original["sql"])}
                    connector, _ = gateway.scope(user, step["source"])
                    queryguard.validate(step["sql"], original_tables, qualifiers=connector.qualifiers(), dialect=connector.dialect)
                if chat_pipelines._recipe_key(steps) == chat_pipelines._recipe_key(previous["steps"]):
                    raise ValueError("Agent Lightning returned the failed SQL unchanged as a repair")
                draft = chat_pipelines._draft(user, previous["prompt"], steps, name=previous["name"])
                draft["generation"] = "agent_lightning_recovery"
                if chat_pipelines._recipe_key(draft.get("steps") or []) == chat_pipelines._recipe_key(previous["steps"]):
                    raise ValueError("Agent Lightning's verified repair is the same failed SQL")
            else:
                draft = previous
                draft.pop("run", None)
            draft.update(repairs_run_id=failed_id, conversation_id=previous.get("conversation_id"))
            attempt = history[-1]["attempt"]
            draft["agent_recovery"] = {"enabled": True, "root_run_id": root, "attempt": attempt,
                                       "max_attempts": row["max_attempts"], "model": row["model"],
                                       "rollout_id": diagnosis.get("rollout_id")}
            if draft.get("status") != "ready" or draft.get("dropped") or not draft.get("steps"):
                raise ValueError("The agent's recovery plan is not ready to run")
            # Even a model-adapted recipe stays read-only and role scoped.
            from .chat_pipelines import _sql_only
            for step in draft["steps"]:
                if not _sql_only(step.get("sql") or ""):
                    raise ValueError("The agent proposed non-read-only SQL")
                gateway.check(user, step["source"], step["sql"], table_label=step.get("table") or "*")
            request_id = f"agent-sql-recovery:{root}:{attempt}"
            child_pipeline_id, child_id = chat_pipelines._request_ids(user, request_id)
            child_snapshot = _snapshot(draft)
            # Retrying unchanged SQL reuses its existing pipeline library id.
            child_snapshot["id"] = draft.get("id") or child_pipeline_id
            jobs.check_claim()
            with db.connect() as c:
                _ensure_claim(c, job)
                changed = c.execute("UPDATE sql_agent_recoveries SET candidate=?,pipeline=?,current_run_id=?,attempts=?,state='executing',updated_at=? "
                          "WHERE root_run_id=? AND current_run_id=? AND state='planning'",
                          (json.dumps(draft), json.dumps(child_snapshot), child_id, attempt, time.time(), root, failed_id))
                c.commit()
                if not changed.rowcount:
                    return {"status": "superseded"}
        jobs.check_claim()
        with db.connect() as c:
            _ensure_claim(c, job)
        output = chat_pipelines.run(user, draft, request_id=f"agent-sql-recovery:{root}:{history[-1]['attempt']}")
        observe(output["run"]["id"])
        return {"status": _load(root)["state"], "run_id": output["run"]["id"], "attempt": history[-1]["attempt"]}
    except jobs.ClaimLost:
        raise
    except Exception as exc:
        # Failed generation/access/verification is not an executed success.
        # Do not turn infrastructure or ambiguous exceptions into blind retries.
        _set(root, "escalated", getattr(exc, "detail", None) or str(exc), claim=job)
        log.info("SQL recovery escalated root=%s: %s", root, exc)
        return {"status": "escalated"}


@jobs.reconciler
def _reconcile():
    with db.connect() as c:
        rows = [_row(r) for r in c.execute("SELECT * FROM sql_agent_recoveries WHERE state NOT IN ('succeeded','escalated')").fetchall()]
    for row in rows:
        if row["state"] in ("watching", "executing"):
            observe(row["current_run_id"])
            refreshed = _load(row["root_run_id"])
            if refreshed["state"] != row["state"]:
                continue
        if row["state"] in ("queued", "planning", "executing"):
            failed_id = (row["history"][-1]["failed_run_id"] if row["state"] != "queued"
                         else row["current_run_id"])
            poll = row["history"][-1].get("poll", 0) if row["state"] != "queued" else 0
            queued = jobs.get(_job_id(row["root_run_id"], failed_id, poll))
            if queued is None:
                jobs.enqueue(KIND, {"root_run_id": row["root_run_id"], "failed_run_id": failed_id},
                             user_id=row["user_id"], job_id=_job_id(row["root_run_id"], failed_id, poll),
                             run_after=time.time() + 5, max_attempts=2)
            if queued and queued["status"] == "failed":
                _set(row["root_run_id"], "escalated", "Recovery worker failed; human review is required")
