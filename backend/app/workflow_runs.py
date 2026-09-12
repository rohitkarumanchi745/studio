"""Approved Airflow DAG publication and asynchronous, fenced first trigger.

Only the worker starts a published DAG. A status GET never deploys, unpauses,
or triggers anything. An uncertain trigger is escalated, never auto-retried.
"""
import json
import time
import uuid

from fastapi import HTTPException

from . import db, jobs, platforms


def prepare(plan, user):
    from . import airflow_dags, pipeline_dags
    if isinstance(plan, dict) and plan.get("status") == "needs_configuration":
        plan = {**plan, "missing": []}
    checked = pipeline_dags.validate(user, plan)
    if checked.get("status") != "ready":
        raise HTTPException(400, {"message": "The DAG needs changes before approval", "plan": checked})
    artifact = airflow_dags.artifact(checked)
    return checked, artifact


def execute(job):
    from . import airflow_dags, supervisor
    requester = db.get_user(job["user_id"])
    if not requester or requester.get("role") not in ("admin", "analyst") or not requester.get("verified", 1):
        raise RuntimeError("Requester can no longer deploy pipelines")
    if not job.get("human_by") or job.get("status") != "approved":
        raise RuntimeError("A generated DAG needs administrator approval")
    spec = json.loads(job["script"])
    checked, artifact = prepare(spec["plan"], requester)
    if artifact["digest"] != spec["digest"]:
        raise RuntimeError("DAG definition or connection mapping changed; request fresh approval")
    jobs.check_claim()
    deployed = airflow_dags.deploy(checked, approver={"id": job["human_by"], "role": "admin"},
                                   expected_digest=spec["digest"])
    old = json.loads(job.get("result") or "{}")
    return {"deployment": deployed, "studio": old.get("studio") or {},
            "publication_token": old.get("publication_token"),
            "detail": "DAG published; waiting for Airflow registration. Nothing has run yet."}


def monitor_ref(row):
    """Durable queue identity for the current approved publication/run stage."""
    result = json.loads(row.get("result") or "{}")
    if row["status"] == "approved":
        token = result.get("publication_token")
        return f"publish:{token}" if token else None
    return result.get("run_ref") or (result.get("deployment") or {}).get("dag_id")


def recover_interrupted_approval(row):
    """Retire legacy approvals stranded before durable publication existed.

    Older API processes committed ``running`` before publishing. With no
    external run identity, such a row cannot be observed or reapproved. Do not
    infer a new trigger: make the interruption explicit and require approval.
    """
    result = json.loads(row.get("result") or "{}")
    if row["status"] == "running" and not result.get("run_ref") and time.time() - row["updated_at"] > 300:
        return _transition(row, require_claim=False, status="escalated", last_error=
            "An older approval was interrupted before a run identity was recorded. Review the DAG and approve publication again; no automatic trigger was attempted.")
    return False


def _transition(row, *, claim=None, require_claim=True, **fields):
    """Commit only the observed job revision; old queue workers cannot clobber it.

    The POST result may be recorded after its queue lease expires, but still
    requires this exact launching revision. It cannot replace a watchdog's
    escalation, a newer approval, or a different external run identity.
    """
    if require_claim:
        jobs.check_claim()
    fields["updated_at"] = time.time()
    fence = ""
    values = [*fields.values(), row["id"], row["status"], row["updated_at"], row.get("result") or ""]
    if require_claim and claim is not None:
        # Check the actual token inside the UPDATE, not only the heartbeat's
        # eventually-consistent abort event. Reclaim and commit cannot race.
        fence = " AND EXISTS (SELECT 1 FROM background_jobs WHERE id=? AND locked_by=? AND status='running')"
        values.extend([claim["id"], claim.get("locked_by")])
    with db.connect() as connection:
        cursor = connection.execute(
            "UPDATE supervised_jobs SET " + ", ".join(f"{key}=?" for key in fields)
            + " WHERE id=? AND status=? AND updated_at=? AND COALESCE(result,'')=?" + fence, values)
        connection.commit()
    if cursor.rowcount != 1:
        return False
    row.update(fields)
    return True


def publish(row, claim=None):
    """Leased, idempotent publication of one durable administrator approval.

    ``approved`` and its queue job commit together; a reconciler also restores
    a missing queue record. A crash after file publication can replay the
    same immutable file safely. No publication attempt ever triggers Airflow.
    """
    from . import supervisor
    if row["status"] != "approved":
        return {"state": row["status"]}
    try:
        result = execute(row)
    except jobs.ClaimLost:
        raise
    except Exception as exc:
        committed = _transition(row, claim=claim, status="escalated", last_error=
            ("DAG publication failed before triggering. Review the deployment configuration and plan. " + str(exc))[:500])
        return {"state": "escalated" if committed else "claimed_elsewhere"}
    if not _transition(row, claim=claim, status="deploying", result=json.dumps(result), last_error=None):
        return {"state": "claimed_elsewhere"}
    # A scheduling error is safe to retry: the job is already deploying, so
    # the next handler/reconciler observes the published identity, not publish.
    supervisor._schedule_platform_monitor(row, result["deployment"]["dag_id"])
    return {"state": "deploying", "dag_id": result["deployment"]["dag_id"]}


def observe(row, claim=None):
    """Worker-only registration check followed by at most one trigger attempt."""
    from . import supervisor
    if row["status"] == "launching":
        # A worker may die after claiming the POST but before storing its
        # result. No retry can prove the original POST did not reach Airflow.
        if time.time() - row["updated_at"] > 300:
            if not _transition(row, claim=claim, status="escalated", last_error=
                               "Trigger outcome is uncertain after worker interruption; inspect Airflow before retrying."):
                return {"state": "claimed_elsewhere"}
        return {"state": row["status"]}
    if row["status"] != "deploying":
        return {"state": row["status"]}
    result = json.loads(row.get("result") or "{}")
    deployed = result.get("deployment") or {}
    dag_id = deployed.get("dag_id")
    if not dag_id:
        raise RuntimeError("Published DAG identity is missing")
    requester = db.get_user(row["user_id"])
    if not requester or requester.get("role") not in ("admin", "analyst") or not requester.get("verified", 1):
        committed = _transition(row, claim=claim, status="escalated", last_error="Requester authorization changed before DAG start")
        return {"state": "escalated" if committed else "claimed_elsewhere"}
    try:
        spec = json.loads(row["script"])
        checked, artifact = prepare(spec["plan"], requester)
        if artifact["digest"] != deployed.get("digest"):
            committed = _transition(row, claim=claim, status="escalated", last_error="DAG/connection mapping changed after publication; fresh approval required")
            return {"state": "escalated" if committed else "claimed_elsewhere"}
    except jobs.ClaimLost:
        raise
    except Exception as exc:
        committed = _transition(row, claim=claim, status="escalated", last_error=
                         ("DAG no longer passes pre-start validation; fresh review required. " + str(exc))[:500])
        return {"state": "escalated" if committed else "claimed_elsewhere"}
    try:
        p = platforms.get_platform("airflow")
        ready = p.dag_ready(dag_id)
    except Exception as exc:
        # An unavailable scheduler is not a failed execution.
        committed = _transition(row, claim=claim, last_error=str(exc)[:500])
        return {"state": "deploying" if committed else "claimed_elsewhere"}
    if not ready.get("ready"):
        committed = _transition(row, claim=claim, last_error=ready.get("detail") or "Waiting for Airflow to register the DAG")
        return {"state": "deploying" if committed else "claimed_elsewhere"}
    result["launch_token"] = uuid.uuid4().hex
    if not _transition(row, claim=claim, status="launching", result=json.dumps(result)):
        return {"state": "claimed_elsewhere"}
    try:
        jobs.check_claim()
        launched = p.trigger({"dag_id": dag_id, "conf": {}})
    except Exception as exc:
        committed = _transition(row, require_claim=False, status="escalated", last_error=
                         ("Trigger outcome is uncertain; inspect Airflow before retrying. " + str(exc))[:500])
        return {"state": "escalated" if committed else "claimed_elsewhere"}
    # Even if our queue lease expired during the POST, persist the external
    # identity so the next worker can observe it. Never perform another POST.
    result.update(launched)
    if not _transition(row, require_claim=False, status="running", result=json.dumps(result), last_error=None):
        return {"state": "claimed_elsewhere", "uncommitted_run_ref": launched.get("run_ref")}
    supervisor._schedule_platform_monitor(row, launched.get("run_ref"))
    return {"state": "running", "run_ref": launched.get("run_ref")}
