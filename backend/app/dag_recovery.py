"""Lightning-controller decisions, Studio-fenced Airflow recovery.

Only observed failures of enrolled chat DAGs enter this loop. A diagnosis is
not a success, and an uncertain trigger never qualifies for recovery. New
write-capable recipes always need fresh human approval; read-only retries may
inherit the original approval within its table scope and two-attempt budget.
"""
import copy
import json
import logging
import time
import uuid

from . import db, jobs, queryguard

log = logging.getLogger("studio.dag_recovery")
MAX_ATTEMPTS = 2
ACTIVE = {"pending", "diagnosing", "retrying", "awaiting_approval"}


def policy(value=None):
    value = value if isinstance(value, dict) else {}
    if value.get("enabled") is not True:
        return {"enabled": False}
    try:
        maximum = max(0, min(MAX_ATTEMPTS, int(value.get("max_attempts", MAX_ATTEMPTS))))
        attempt = max(0, int(value.get("attempt", 0)))
    except (ValueError, TypeError):
        return {"enabled": False}
    return {"enabled": True, "max_attempts": maximum, "attempt": attempt,
            **{k: str(value[k])[:1000] for k in ("root_run_id", "decision_rollout_id") if value.get(k)}}


def _stored(row):
    try:
        result = row.get("result")
        result = result if isinstance(result, dict) else json.loads(result or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def _policy(row):
    return policy((_stored(row).get("studio") or {}).get("agent_recovery"))


def _known_failure(row):
    from . import supervisor
    result = _stored(row)
    return (row.get("kind") == supervisor.DAG_KIND and row.get("status") == "failed"
            and result.get("state") == "failed" and bool(result.get("run_ref")))


def _request_id(row):
    return f"dag-recovery:{row['id']}:{_stored(row)['run_ref']}"


def _queue(row, connection, delay=5):
    identity = str(uuid.uuid5(uuid.NAMESPACE_URL, _request_id(row)))
    now = time.time()
    connection.execute(
        "INSERT INTO background_jobs (id,kind,payload,status,attempts,max_attempts,run_after,user_id,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status='queued',attempts=0,error=NULL,"
        "result=NULL,finished_at=NULL,run_after=excluded.run_after "
        "WHERE background_jobs.status IN ('done','failed') AND background_jobs.finished_at<=?",
        (identity, "dag_recovery", json.dumps({"job_id": row["id"], "run_ref": _stored(row)["run_ref"]}),
         "queued", 0, 3, now + delay, row["user_id"], now, now - 10))
    return identity


def enroll(row):
    """Atomically save recovery intent and a durable queue item. No model call."""
    if not row or not _known_failure(row) or not _policy(row).get("enabled"):
        return
    result, rules = _stored(row), _policy(row)
    recovery = result.get("recovery") or {}
    if recovery.get("state") and recovery["state"] not in {"pending", "diagnosing"}:
        return
    if not recovery:
        exhausted = rules["attempt"] >= rules["max_attempts"]
        recovery = {"state": "exhausted" if exhausted else "pending",
                    "attempt": rules["attempt"], "max_attempts": rules["max_attempts"],
                    "root_run_id": rules.get("root_run_id") or f"{row['id']}:{result['run_ref']}",
                    "repairs_run_id": f"{row['id']}:{result['run_ref']}",
                    "reason": "Agent retry budget exhausted." if exhausted else "Waiting for the Lightning recovery agent.",
                    "started_at": time.time()}
        result["recovery"] = recovery
    with db.connect() as connection:
        updated = connection.execute(
            "UPDATE supervised_jobs SET result=? WHERE id=? AND status='failed' AND result=?",
            (json.dumps(result), row["id"], row["result"]))
        if updated.rowcount == 1 and recovery["state"] in {"pending", "diagnosing"}:
            _queue({**row, "result": json.dumps(result)}, connection)
        connection.commit()


def _save(row, recovery, claim=None):
    from .workflow_runs import _transition
    result = _stored(row)
    result["recovery"] = recovery
    return _transition(row, claim=claim, result=json.dumps(result))


def _tables(plan):
    return {table for task in plan.get("tasks", []) for table in queryguard.base_tables(task["sql"])}


def _auto_safe(old, new):
    return (old["source"] == new["source"] and old.get("tasks") and new.get("tasks")
            and all(task.get("kind") == "select" for task in [*old["tasks"], *new["tasks"]])
            and _tables(new).issubset(_tables(old)))


def _schema(user, plan):
    """Only current, authorized input schema is provided to the recovery agent."""
    from . import pipeline_dags, rbac, governance
    connector = pipeline_dags.get_connector(plan["source"])
    allowed = set(rbac.allowed_tables(user["role"], plan["source"], connector.list_tables()))
    schemas = {}
    for table in sorted(_tables(plan) & allowed)[:30]:
        jobs.check_claim()
        denied = governance.column_rules(plan["source"], table)["deny"]
        schemas[table] = [{k: column.get(k) for k in ("name", "type")}
                          for column in connector.get_schema(table)
                          if isinstance(column, dict) and str(column.get("name", "")).lower() not in denied]
    return schemas


def _authorize_readonly(child, parent, old, new, claim=None):
    """Use recorded delegated read-only approval, never impersonate an admin API call."""
    from . import supervisor, workflow_runs
    if not parent.get("human_by") or not _known_failure(parent) or not _auto_safe(old, new):
        return False
    if child["status"] != "awaiting_approval":
        return child["status"] in {"approved", "deploying", "launching", "running", "succeeded", "failed"}
    jobs.check_claim()
    result = _stored(child)
    result["publication_token"] = uuid.uuid4().hex
    result["agent_authorization"] = {"parent_job_id": parent["id"], "scope": "read_only_retry",
                                     "original_approver": parent["human_by"]}
    fields = {"status": "approved", "human_by": parent["human_by"],
              "result": json.dumps(result), "updated_at": time.time()}
    fence, values = "", [*fields.values(), child["id"], child["updated_at"], parent["id"], parent["result"]]
    if claim:
        fence = " AND EXISTS (SELECT 1 FROM background_jobs WHERE id=? AND locked_by=? AND status='running')"
        values.extend([claim["id"], claim.get("locked_by")])
    with db.connect() as connection:
        changed = connection.execute(
            "UPDATE supervised_jobs SET " + ",".join(f"{key}=?" for key in fields)
            + " WHERE id=? AND status='awaiting_approval' AND updated_at=? "
            "AND EXISTS (SELECT 1 FROM supervised_jobs parent WHERE parent.id=? AND parent.status='failed' AND parent.result=?)"
            + fence, values)
        if changed.rowcount == 1:
            child.update(fields)
            supervisor._schedule_platform_monitor(child, workflow_runs.monitor_ref(child), conn=connection)
        connection.commit()
    return changed.rowcount == 1


@jobs.handler("dag_recovery")
def recover(payload, job=None):
    from . import recovery_planner, supervisor, workflow_runs, pipeline_dags, rbac
    row = supervisor._get(payload["job_id"])
    if not row or not _known_failure(row) or _stored(row).get("run_ref") != payload.get("run_ref"):
        return {"skipped": "not_the_failed_run"}
    rules, result = _policy(row), _stored(row)
    recovery = copy.deepcopy(result.get("recovery") or {})
    if not rules.get("enabled") or recovery.get("state") not in {"pending", "diagnosing"}:
        return {"skipped": "not_pending"}
    user = db.get_user(row["user_id"])
    if not user or user.get("role") not in {"analyst", "admin"} or not user.get("verified", 1):
        recovery.update(state="escalated", reason="Requester authorization changed; recovery stopped.")
        _save(row, recovery, job)
        return {"state": "escalated"}
    if rules["attempt"] >= rules["max_attempts"]:
        recovery.update(state="exhausted", reason="Agent retry budget exhausted.")
        _save(row, recovery, job)
        return {"state": "exhausted"}
    old = json.loads(row["script"])["plan"]
    try:
        if not rbac.can_access(user["role"], old["source"], "*"):
            raise ValueError("Source permission revoked")
        # Static validation may need input after partial CTAS execution. Its
        # security errors still forbid exposing the old recipe to an agent.
        old_checked = pipeline_dags.validate(user, old)
        if old_checked.get("errors") or not old_checked.get("tasks"):
            raise ValueError("Original recipe no longer passes current permissions")
        decision = recovery.get("decision")
        if not decision:
            if time.time() - recovery.get("started_at", time.time()) > 600:
                decision = {"decision": "escalate", "reason": "Lightning recovery did not finish within ten minutes."}
            else:
                decision = recovery_planner.diagnose(user, prompt=result.get("studio", {}).get("prompt") or old.get("prompt"),
                    action={"type": "airflow_dag", "plan": old}, error=row.get("last_error") or result.get("detail"),
                    request_id=_request_id(row), schema=_schema(user, old),
                    history=[{"attempt": rules["attempt"], "status": "failed", "error": row.get("last_error")}])
            recovery.update(state="diagnosing", reason=decision.get("reason") or "Lightning agent is evaluating the failure.",
                            rollout_id=decision.get("rollout_id"))
            if decision.get("decision") == "pending":
                _save(row, recovery, job)
                return {"state": "diagnosing"}
            recovery["decision"] = decision
            if not _save(row, recovery, job):
                return {"state": "claimed_elsewhere"}
        if decision.get("decision") not in {"retry", "repair"}:
            recovery.update(state="escalated", reason=decision.get("reason") or "Agent could not safely recover this run.")
            _save(row, recovery, job)
            return {"state": "escalated"}
        candidate = copy.deepcopy(old)
        if decision["decision"] == "repair":
            action = decision.get("action") or {}
            if action.get("type") != "airflow_dag" or not isinstance(action.get("plan"), dict):
                raise ValueError("Agent returned no typed DAG correction")
            candidate = copy.deepcopy(action["plan"])
            from .chat_workflows import _sql_recipe
            if _sql_recipe(candidate.get("tasks")) == _sql_recipe(old.get("tasks")):
                raise ValueError("The proposed correction repeats the failed SQL")
        # An agent cannot silently introduce a new source/output. A separate
        # user request is required to widen those boundaries.
        if candidate.get("source") != old["source"]:
            raise ValueError("Agent correction changed the source")
        topology = lambda value: sorted((task["id"], tuple(sorted(task.get("depends_on") or [])), task.get("produces") or "")
                                       for task in value.get("tasks", []))
        if topology(candidate) != topology(old):
            raise ValueError("Agent correction changed the task graph; explicit user input is required")
        if {t.get("produces") for t in candidate.get("tasks", []) if t.get("produces")} != {
                t.get("produces") for t in old["tasks"] if t.get("produces")}:
            raise ValueError("Agent correction changed destination tables; explicit user input is required")
        candidate["prompt"] = old.get("prompt") or result.get("studio", {}).get("prompt")
        checked, artifact = workflow_runs.prepare(candidate, user)
        child_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _request_id(row) + ":child"))
        # Recover the recorded candidate before invoking submission again.
        child = supervisor._get(child_id)
        if child is None:
            jobs.check_claim()
            child = supervisor.submit(supervisor.DAG_KIND, "airflow",
                json.dumps({"plan": checked, "digest": artifact["digest"]}, sort_keys=True), user,
                job_id=child_id, notify=False, learning_context={
                    "prompt": candidate["prompt"], "conversation_id": result.get("studio", {}).get("conversation_id"),
                    "repairs_run_id": f"{row['id']}:{result['run_ref']}",
                    "agent_recovery": {**rules, "attempt": rules["attempt"] + 1,
                        "root_run_id": recovery["root_run_id"], "decision_rollout_id": decision.get("rollout_id")}})
        automatic = _authorize_readonly(child, row, old_checked, checked, job)
        recovery.update(state="retrying" if automatic else "awaiting_approval", child_job_id=child_id,
                        attempt=rules["attempt"] + 1, reason=decision.get("reason"),
                        approval_reason=None if automatic else "This correction can write data or changes approved read scope; fresh administrator approval is required.")
        if not _save(row, recovery, job):
            return {"state": "claimed_elsewhere"}
        db.log_activity(user, "pipeline_agent_recovery", prompt=decision.get("decision"), source=old["source"])
        return {"state": recovery["state"], "child_job_id": child_id}
    except jobs.ClaimLost:
        raise
    except Exception:
        log.warning("DAG recovery stopped for %s", row["id"], exc_info=True)
        recovery.update(state="escalated", reason="The agent correction could not pass current validation or recovery configuration. Review the failed run before retrying.")
        _save(row, recovery, job)
        return {"state": "escalated"}


def view(row, depth=0):
    """Observe the correction chain without relabelling the failed parent."""
    from . import supervisor
    recovery = _stored(row).get("recovery")
    if not isinstance(recovery, dict):
        return None
    out = {k: recovery.get(k) for k in ("state", "attempt", "max_attempts", "reason", "approval_reason",
        "root_run_id", "repairs_run_id", "child_job_id", "rollout_id") if recovery.get(k) is not None}
    if out.get("child_job_id") and depth <= MAX_ATTEMPTS:
        child = supervisor._get(out["child_job_id"])
        if child and child["user_id"] == row["user_id"]:
            out["child_state"] = child["status"]
            if child["status"] == "succeeded":
                out["state"] = "succeeded"
            elif child["status"] in {"approved", "deploying", "launching", "running"}:
                out["state"] = "retrying"
            elif child["status"] in {"rejected", "escalated", "canceled"}:
                out.update(state="escalated", reason="The correction was stopped and requires review.")
            elif child["status"] == "failed":
                following = view(child, depth + 1)
                if following:
                    out.update(following)
                else:
                    out.update(state="pending", reason="The corrected run failed; waiting for the recovery agent.")
    return out


@jobs.handler("dag_recovery_reward")
def deliver_reward(payload, job=None):
    from . import recovery_planner, supervisor
    row = supervisor._get(payload["job_id"])
    if not row or row["status"] not in {"failed", "succeeded"}:
        return {"skipped": "not_terminal"}
    result, rules = _stored(row), _policy(row)
    if not result.get("run_ref") or not rules.get("decision_rollout_id"):
        return {"skipped": "no_decision_rollout"}
    jobs.check_claim()
    recovery_planner.record_outcome(rules["decision_rollout_id"], run_id=f"{row['id']}:{result['run_ref']}",
                                   status=row["status"], error=row.get("last_error"))
    return {"delivered": True}


@jobs.reconciler
def reconcile():
    from . import supervisor
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM supervised_jobs WHERE kind=? AND status IN ('failed','succeeded') "
                                  "AND result LIKE ? ORDER BY updated_at DESC LIMIT 200",
                                  (supervisor.DAG_KIND, '%"agent_recovery":%')).fetchall()
    for item in rows:
        row = dict(item)
        enroll(row)
        rules, result = _policy(row), _stored(row)
        if rules.get("decision_rollout_id") and result.get("run_ref"):
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"dag-recovery-reward:{row['id']}:{result['run_ref']}"))
            with db.connect() as connection:
                connection.execute("INSERT INTO background_jobs (id,kind,payload,status,attempts,max_attempts,run_after,user_id,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                    (identity, "dag_recovery_reward", json.dumps({"job_id": row["id"]}), "queued", 0, 5,
                     time.time(), row["user_id"], time.time()))
                connection.commit()
