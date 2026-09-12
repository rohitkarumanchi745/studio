"""Generated Airflow lifecycle with real compiler/planner/DB and fake platform.

Tests use a per-test SQLite file and shared-DAG tmp directory. No real warehouse
SQL, Airflow APIs, model calls, messages, or training services are accessed.
"""
import copy
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import (agent, db, email_service, jobs, lightning, pipeline_dags,
                 platforms, rbac, supervisor, workflow_runs)


ADMIN = {"id": "workflow-admin", "email": "workflow-admin@example.test", "name": "Admin", "role": "admin"}
ANALYST = {"id": "workflow-analyst", "email": "workflow-analyst@example.test", "name": "Analyst", "role": "analyst"}
OTHER = {"id": "workflow-other", "email": "workflow-other@example.test", "name": "Other", "role": "analyst"}


class Catalog:
    dialect = "postgres"

    def configured(self):
        return True

    def list_tables(self):
        return ["sales"]

    def qualifiers(self):
        return ["public"]

    def query(self, *_args, **_kwargs):
        pytest.fail("Planning and publishing must not query a warehouse")

    run_script = query


class FakeAirflow:
    label = "Fake Airflow"
    name = "airflow"

    def __init__(self):
        self.available = True
        self.registered = False
        self.trigger_error = None
        self.triggered = []
        self.readiness_checks = []
        self.state = "running"

    def configured(self):
        return self.available

    def dag_ready(self, dag_id):
        self.readiness_checks.append(dag_id)
        return {"ready": self.registered, "detail": None if self.registered else "DAG not registered yet"}

    def trigger(self, payload):
        self.triggered.append(copy.deepcopy(payload))
        if self.trigger_error:
            raise self.trigger_error
        return {"run_ref": payload["dag_id"] + ":run-" + str(len(self.triggered)), "url": "https://airflow.example.test/run"}

    def status(self, run_ref):
        return {"state": self.state, "detail": "Bad column" if self.state == "failed" else None,
                "url": "https://airflow.example.test/run", "metrics": {"run_ref": run_ref}}

    def logs(self, run_ref):
        return "No warehouse rows in these logs"

    def quality(self, run_ref):
        return []


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "workflow.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.delenv("STUDIO_AGL_URL", raising=False)
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", '{"warehouse":"warehouse_conn"}')
    directory = tmp_path / "dags"
    directory.mkdir()
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(directory))
    monkeypatch.setattr(agent, "llm_available", lambda *_a, **_k: False)
    monkeypatch.setattr(email_service, "send", lambda *_a, **_k: {"mode": "test"})
    monkeypatch.setattr(lightning, "_client", lambda: pytest.fail("No training service in tests"))
    monkeypatch.setattr(pipeline_dags, "get_connector", lambda source: Catalog())
    monkeypatch.setattr(rbac, "allowed_sources", lambda role: ["warehouse"])
    monkeypatch.setattr(rbac, "allowed_tables", lambda role, source, tables: list(tables))
    monkeypatch.setattr(rbac, "can_access", lambda role, source, table: role in ("admin", "analyst"))
    db.init_db()
    jobs.init_tables()
    supervisor.init_tables()
    with db.connect() as connection:
        for user in (ADMIN, ANALYST, OTHER):
            connection.execute(
                "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) VALUES (?,?,?,?,?,?,?)",
                (user["id"], user["email"], db.UNUSABLE_PASSWORD_HASH, user["name"], user["role"], 1, time.time()))
        connection.commit()
    airflow = FakeAirflow()
    monkeypatch.setitem(platforms.PLATFORMS, "airflow", airflow)
    return airflow, directory


@pytest.fixture
def plan():
    return {"version": 1, "name": "Sales totals", "dag_id": "sales_totals", "source": "warehouse",
            "prompt": "Create staged sales then summarize them", "schedule": None, "parameters": {},
            "tasks": [
                {"id": "extract", "name": "Create stage", "source": "warehouse",
                 "sql": "CREATE TABLE staged_sales AS SELECT * FROM sales", "produces": "staged_sales", "depends_on": []},
                {"id": "summarize", "name": "Summarize", "source": "warehouse",
                 "sql": "SELECT SUM(amount) AS total FROM staged_sales", "depends_on": ["extract"]},
            ]}


def _submit(plan, *, job_id=None, repairs_run_id=None, user=ANALYST):
    checked, compiled = workflow_runs.prepare(plan, user)
    script = json.dumps({"plan": checked, "digest": compiled["digest"]}, sort_keys=True)
    return supervisor.submit(
        "airflow_dag", "airflow", script, user, job_id=job_id or str(uuid.uuid4()), notify=False,
        learning_context={"prompt": plan["prompt"], "conversation_id": "workflow-chat",
                          "repairs_run_id": repairs_run_id})


def _traces():
    with db.connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM agent_traces ORDER BY created_at")]


def _requeue_monitors():
    with db.connect() as connection:
        connection.execute("UPDATE background_jobs SET finished_at=? WHERE kind='platform_monitor' AND status IN ('done','failed')",
                           (time.time() - 40,))
        connection.commit()
    supervisor._reconcile_platform_monitors()


def _approve_and_publish(row):
    approved = supervisor.approve(row["id"], user=ADMIN)
    assert approved["status"] == "approved"
    assert jobs.run_one("publication-worker", kinds=["platform_monitor"])
    current = supervisor.get_job(row["id"], ADMIN)
    assert current["status"] == "deploying"
    return current


def _approve_and_launch(plan, airflow):
    row = _submit(plan)
    _approve_and_publish(row)
    airflow.registered = True
    assert jobs.run_one("workflow-worker", kinds=["platform_monitor"])
    current = supervisor._get(row["id"])
    assert current["status"] == "running"
    return current


def test_submission_waits_for_approval_and_get_never_publishes(plan, isolated):
    airflow, directory = isolated
    row = _submit(plan)
    assert row["status"] == "awaiting_approval"
    assert supervisor.live_job(row["id"], ANALYST)["state"] == "awaiting_approval"
    assert not jobs.run_one("worker", kinds=["platform_monitor"])
    assert not list(directory.iterdir())
    assert airflow.triggered == []
    assert _traces() == []


def test_approval_publishes_then_worker_waits_for_registration_then_triggers_once(plan, isolated):
    airflow, directory = isolated
    row = _submit(plan)
    queued = supervisor.approve(row["id"], ADMIN)
    assert queued["status"] == "approved"
    assert not list(directory.iterdir())
    assert supervisor.live_job(row["id"], ADMIN)["state"] == "approved"
    assert jobs.run_one("publication-worker", kinds=["platform_monitor"])
    approved = supervisor.get_job(row["id"], ADMIN)
    assert approved["status"] == "deploying"
    deployed = approved["result"]["deployment"]
    assert list(directory.iterdir()) == [Path(deployed["path"])]
    assert airflow.triggered == []
    # Status polling is observational, including after the platform is ready.
    assert supervisor.live_job(row["id"], ANALYST)["state"] == "deploying"
    assert jobs.run_one("worker-1", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "deploying"
    assert airflow.triggered == []
    airflow.registered = True
    assert supervisor.live_job(row["id"], ADMIN)["state"] == "deploying"
    assert airflow.triggered == []
    _requeue_monitors()
    assert jobs.run_one("worker-2", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "running"
    assert airflow.triggered == [{"dag_id": deployed["dag_id"], "conf": {}}]
    _requeue_monitors()
    assert jobs.run_one("worker-3", kinds=["platform_monitor"])
    assert len(airflow.triggered) == 1
    assert _traces() == []  # trigger success does not mean pipeline success


def test_terminal_success_records_full_dag_for_requester_not_polling_admin(plan, isolated, monkeypatch):
    airflow, _ = isolated
    monkeypatch.setenv("STUDIO_AGL_URL", "https://learning.example.invalid")
    row = _approve_and_launch(plan, airflow)
    airflow.state = "succeeded"
    live = supervisor.live_job(row["id"], ADMIN)
    assert live["state"] == "succeeded"
    assert live["job"]["result"]["learning"]["trace_id"]
    traces = _traces()
    assert len(traces) == 1
    trace = traces[0]
    assert trace["user_id"] == ANALYST["id"]
    assert trace["conversation_id"] == "workflow-chat"
    assert trace["source"] == "warehouse"
    assert trace["reward"] == 1 and trace["sql"] is None
    meta = json.loads(trace["meta"])
    assert meta["action"]["type"] == "airflow_dag"
    assert len(meta["action"]["plan"]["tasks"]) == 2
    assert meta["action"]["plan"]["tasks"][1]["depends_on"] == ["extract"]
    assert meta["run_id"] == row["id"] + ":" + json.loads(row["result"])["run_ref"]
    with db.connect() as connection:
        emitted = connection.execute("SELECT * FROM background_jobs WHERE kind='agl_emit'").fetchall()
    assert len(emitted) == 1
    supervisor.live_job(row["id"], ANALYST)
    assert len(_traces()) == 1


def test_worker_can_record_terminal_learning_without_any_open_chat(plan, isolated):
    airflow, _ = isolated
    row = _approve_and_launch(plan, airflow)
    airflow.state = "succeeded"
    assert jobs.run_one("outcome-worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "succeeded"
    assert len(_traces()) == 1
    assert _traces()[0]["reward"] == 1


def test_replaying_same_submission_does_not_republish_or_retrigger(plan, isolated):
    airflow, directory = isolated
    row = _approve_and_launch(plan, airflow)
    inode = next(directory.iterdir()).stat().st_ino
    replay = _submit(plan, job_id=row["id"])
    assert replay["status"] == "running"
    assert next(directory.iterdir()).stat().st_ino == inode
    assert len(airflow.triggered) == 1
    with pytest.raises(HTTPException):
        supervisor.approve(row["id"], ADMIN)
    assert len(airflow.triggered) == 1


def test_failed_attempt_and_corrected_success_have_distinct_linked_traces(plan, isolated):
    airflow, _ = isolated
    failed = _approve_and_launch(plan, airflow)
    airflow.state = "failed"
    supervisor.live_job(failed["id"], ANALYST)
    failure_ref = failed["id"] + ":" + json.loads(failed["result"])["run_ref"]
    fixed_plan = copy.deepcopy(plan)
    fixed_plan["tasks"][1]["sql"] = "SELECT COUNT(*) AS total FROM staged_sales"
    fixed = _submit(fixed_plan, repairs_run_id=failure_ref)
    supervisor.approve(fixed["id"], ADMIN)
    # Consume any older terminal monitor before the new registration monitor.
    for _ in range(5):
        if supervisor._get(fixed["id"])["status"] == "running":
            break
        assert jobs.run_one("repair-worker", kinds=["platform_monitor"])
    assert supervisor._get(fixed["id"])["status"] == "running"
    airflow.state = "succeeded"
    supervisor.live_job(fixed["id"], ADMIN)
    # A later success from the platform never relabels the failed attempt.
    assert supervisor.live_job(failed["id"], ANALYST)["state"] == "failed"
    traces = _traces()
    assert [row["reward"] for row in traces] == [0, 1]
    assert json.loads(traces[1]["meta"])["repairs_run_id"] == failure_ref
    assert "COUNT(*)" in json.loads(traces[1]["meta"])["action"]["plan"]["tasks"][1]["sql"]


@pytest.mark.parametrize("state", ["unknown", "running", "queued", "canceled"])
def test_nonterminal_or_canceled_pipeline_does_not_receive_training_reward(plan, isolated, state):
    airflow, _ = isolated
    row = _approve_and_launch(plan, airflow)
    airflow.state = state
    supervisor.live_job(row["id"], ANALYST)
    assert _traces() == []


def test_lost_launching_worker_escalates_without_repeating_post(plan, isolated):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)
    with db.connect() as connection:
        connection.execute("UPDATE supervised_jobs SET status='launching',updated_at=? WHERE id=?",
                           (time.time() - 301, row["id"]))
        connection.commit()
    assert jobs.run_one("replacement-worker", kinds=["platform_monitor"])
    current = supervisor._get(row["id"])
    assert current["status"] == "escalated"
    assert "uncertain" in current["last_error"]
    assert airflow.triggered == []
    assert _traces() == []


def test_uncertain_trigger_is_never_automatically_retried(plan, isolated):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)
    airflow.registered = True
    airflow.trigger_error = TimeoutError("response lost")
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "escalated"
    _requeue_monitors()
    assert not jobs.run_one("replacement", kinds=["platform_monitor"])
    assert len(airflow.triggered) == 1
    assert _traces() == []


def test_two_stale_registration_observations_cannot_trigger_twice(plan, isolated):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)
    airflow.registered = True
    first = supervisor._get(row["id"])
    stale = copy.deepcopy(first)
    assert workflow_runs.observe(first)["state"] == "running"
    assert workflow_runs.observe(stale)["state"] == "claimed_elsewhere"
    assert len(airflow.triggered) == 1


def test_changed_mapping_after_publication_never_triggers(plan, isolated, monkeypatch):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)
    airflow.registered = True
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", '{"warehouse":"different_conn"}')
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert airflow.triggered == []
    assert "changed" in supervisor._get(row["id"])["last_error"]


def test_changed_mapping_before_approval_does_not_publish(plan, isolated, monkeypatch):
    airflow, directory = isolated
    row = _submit(plan)
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", '{"warehouse":"different_conn"}')
    supervisor.approve(row["id"], ADMIN)
    assert jobs.run_one("publication-worker", kinds=["platform_monitor"])
    approved = supervisor.get_job(row["id"], ADMIN)
    assert approved["status"] == "escalated"
    assert not list(directory.iterdir())
    assert airflow.triggered == []


def test_revoked_requester_before_trigger_prevents_execution(plan, isolated):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)
    airflow.registered = True
    with db.connect() as connection:
        connection.execute("UPDATE users SET role='viewer' WHERE id=?", (ANALYST["id"],))
        connection.commit()
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "escalated"
    assert airflow.triggered == []


def test_missing_shared_directory_is_explicit_and_never_triggers(plan, isolated, monkeypatch):
    airflow, directory = isolated
    row = _submit(plan)
    monkeypatch.delenv("STUDIO_AIRFLOW_DAGS_DIR", raising=False)
    supervisor.approve(row["id"], ADMIN)
    assert jobs.run_one("publication-worker", kinds=["platform_monitor"])
    approved = supervisor.get_job(row["id"], ADMIN)
    assert approved["status"] == "escalated"
    assert "STUDIO_AIRFLOW_DAGS_DIR" in approved["last_error"]
    assert not list(directory.iterdir())
    assert airflow.triggered == []


def test_other_user_cannot_inspect_or_approve_job(plan, isolated):
    row = _submit(plan)
    with pytest.raises(HTTPException) as error:
        supervisor.live_job(row["id"], OTHER)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        supervisor.approve(row["id"], OTHER)
    assert error.value.status_code == 403


def test_approval_and_publication_job_survive_crash_before_api_response(plan, isolated, monkeypatch):
    airflow, directory = isolated
    row = _submit(plan)
    monkeypatch.setattr(db, "log_activity", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("API interrupted")))
    with pytest.raises(RuntimeError, match="API interrupted"):
        supervisor.approve(row["id"], ADMIN)
    assert supervisor._get(row["id"])["status"] == "approved"
    assert not list(directory.iterdir())
    assert jobs.run_one("replacement-worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "deploying"
    assert len(list(directory.iterdir())) == 1
    assert airflow.triggered == []


def test_reconciler_unsticks_legacy_running_approval_without_inventing_a_trigger(plan, isolated):
    airflow, directory = isolated
    row = _submit(plan)
    with db.connect() as connection:
        connection.execute("UPDATE supervised_jobs SET status='running',human_by=?,updated_at=? WHERE id=?",
                           (ADMIN["email"], time.time() - 301, row["id"]))
        connection.commit()
    supervisor._reconcile_platform_monitors()
    current = supervisor._get(row["id"])
    assert current["status"] == "escalated"
    assert "interrupted" in current["last_error"]
    assert not jobs.run_one("worker", kinds=["platform_monitor"])
    assert not list(directory.iterdir())
    assert airflow.triggered == []
    assert supervisor.approve(row["id"], ADMIN)["status"] == "approved"
    assert jobs.run_one("publication-worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "deploying"


def test_publication_queue_failure_rolls_back_approval_atomically(plan, isolated, monkeypatch):
    airflow, directory = isolated
    row = _submit(plan)
    monkeypatch.setattr(supervisor, "_schedule_platform_monitor", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("Queue unavailable")))
    with pytest.raises(RuntimeError, match="Queue unavailable"):
        supervisor.approve(row["id"], ADMIN)
    current = supervisor._get(row["id"])
    assert current["status"] == "awaiting_approval"
    assert current["human_by"] is None
    assert not list(directory.iterdir())
    assert airflow.triggered == []


def test_reconciler_restores_missing_approved_publication_queue_record(plan, isolated):
    airflow, _ = isolated
    row = _submit(plan)
    supervisor.approve(row["id"], ADMIN)
    # This database is per-test temporary data, never Studio's local history.
    with db.connect() as connection:
        connection.execute("DELETE FROM background_jobs WHERE kind='platform_monitor'")
        connection.commit()
    assert not jobs.run_one("worker", kinds=["platform_monitor"])
    supervisor._reconcile_platform_monitors()
    assert jobs.run_one("replacement", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "deploying"
    assert airflow.triggered == []


def test_crash_after_file_publication_replays_same_immutable_file(plan, isolated, monkeypatch):
    from app import airflow_dags
    airflow, directory = isolated
    row = _submit(plan)
    supervisor.approve(row["id"], ADMIN)
    real_deploy = airflow_dags.deploy

    def interrupted(*args, **kwargs):
        real_deploy(*args, **kwargs)
        raise SystemExit("process died after publication")

    monkeypatch.setattr(airflow_dags, "deploy", interrupted)
    with pytest.raises(SystemExit, match="process died"):
        jobs.run_one("old-worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "approved"
    filename = next(directory.iterdir())
    inode = filename.stat().st_ino
    monkeypatch.setattr(airflow_dags, "deploy", real_deploy)
    with db.connect() as connection:
        connection.execute("UPDATE background_jobs SET heartbeat_at=0,locked_at=0 WHERE kind='platform_monitor'")
        connection.commit()
    jobs.reclaim_stale(stale_after=1)
    assert jobs.run_one("replacement-worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "deploying"
    assert filename.stat().st_ino == inode
    assert airflow.triggered == []


def test_stale_publisher_claim_cannot_commit_even_before_abort_event_fires(plan, isolated, monkeypatch):
    from app import airflow_dags
    airflow, directory = isolated
    row = _submit(plan)
    supervisor.approve(row["id"], ADMIN)
    real_deploy = airflow_dags.deploy

    def reclaim_after_file_write(*args, **kwargs):
        result = real_deploy(*args, **kwargs)
        with db.connect() as connection:
            connection.execute("UPDATE background_jobs SET locked_by='replacement-owner' WHERE kind='platform_monitor' AND status='running'")
            connection.commit()
        return result

    monkeypatch.setattr(airflow_dags, "deploy", reclaim_after_file_write)
    assert jobs.run_one("stale-worker", kinds=["platform_monitor"])
    current = supervisor._get(row["id"])
    assert current["status"] == "approved"
    assert "deployment" not in json.loads(current["result"])
    assert len(list(directory.iterdir())) == 1  # safe manual-only immutable artifact
    assert airflow.triggered == []


def test_reclaimed_registration_worker_cannot_make_first_trigger(plan, isolated, monkeypatch):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)

    def lose_claim_during_readiness(dag_id):
        with db.connect() as connection:
            connection.execute("UPDATE background_jobs SET locked_by='replacement-owner' WHERE kind='platform_monitor' AND status='running'")
            connection.commit()
        return {"ready": True}

    monkeypatch.setattr(airflow, "dag_ready", lose_claim_during_readiness)
    assert jobs.run_one("stale-registration-worker", kinds=["platform_monitor"])
    assert supervisor._get(row["id"])["status"] == "deploying"
    assert airflow.triggered == []


def test_stale_publisher_cannot_replace_a_new_approval_revision(plan, isolated, monkeypatch):
    from app import airflow_dags
    airflow, _ = isolated
    row = _submit(plan)
    supervisor.approve(row["id"], ADMIN)
    old = supervisor._get(row["id"])
    real_deploy = airflow_dags.deploy

    def reapprove_during_publication(*args, **kwargs):
        result = real_deploy(*args, **kwargs)
        current = supervisor._get(row["id"])
        supervisor._save(current, status="escalated", last_error="interrupted")
        supervisor.approve(row["id"], ADMIN)
        return result

    monkeypatch.setattr(airflow_dags, "deploy", reapprove_during_publication)
    assert workflow_runs.publish(old)["state"] == "claimed_elsewhere"
    current = supervisor._get(row["id"])
    assert current["status"] == "approved"
    assert json.loads(current["result"])["publication_token"] != json.loads(old["result"])["publication_token"]
    assert "deployment" not in json.loads(current["result"])
    assert airflow.triggered == []


def test_stale_launch_watchdog_cannot_escalate_an_accepted_run(plan, isolated):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)
    current = supervisor._get(row["id"])
    supervisor._save(current, status="launching")
    with db.connect() as connection:
        connection.execute("UPDATE supervised_jobs SET updated_at=? WHERE id=?", (time.time() - 301, row["id"]))
        connection.commit()
    stale = supervisor._get(row["id"])
    accepted = supervisor._get(row["id"])
    result = json.loads(accepted["result"])
    result["run_ref"] = "accepted:run"
    supervisor._save(accepted, status="running", result=json.dumps(result))
    assert workflow_runs.observe(stale)["state"] == "claimed_elsewhere"
    assert supervisor._get(row["id"])["status"] == "running"
    assert airflow.triggered == []


def test_late_trigger_response_cannot_overwrite_new_human_approval(plan, isolated, monkeypatch):
    airflow, _ = isolated
    row = _submit(plan)
    _approve_and_publish(row)
    airflow.registered = True

    def delayed_trigger(payload):
        airflow.triggered.append(payload)
        current = supervisor._get(row["id"])
        supervisor._save(current, status="escalated", last_error="watchdog timed out")
        supervisor.approve(row["id"], ADMIN)
        return {"run_ref": "old:accepted", "url": "https://airflow.example.test/old"}

    monkeypatch.setattr(airflow, "trigger", delayed_trigger)
    observed = workflow_runs.observe(supervisor._get(row["id"]))
    assert observed == {"state": "claimed_elsewhere", "uncommitted_run_ref": "old:accepted"}
    current = supervisor._get(row["id"])
    assert current["status"] == "approved"
    assert "run_ref" not in json.loads(current["result"])
    assert len(airflow.triggered) == 1
