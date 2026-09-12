"""Platform learning follows durable observed executions, never approval/dispatch."""
import json
import time

import pytest

from app import db, jobs, lightning, platforms, supervisor, trainer


OWNER = {"id": "platform-owner", "email": "owner@studio.test", "role": "analyst"}
ADMIN = {"id": "platform-approver", "email": "admin@studio.test", "role": "admin"}


class FakePlatform:
    name = "airflow"
    label = "Test Airflow"

    def __init__(self):
        self.triggers = []
        self.polls = []
        self.states = {}

    def configured(self):
        return True

    def trigger(self, payload):
        self.triggers.append(payload)
        ref = f"{payload['dag_id']}:{len(self.triggers)}"
        self.states[ref] = "running"
        return {"run_ref": ref, "url": f"https://airflow.invalid/{len(self.triggers)}"}

    def status(self, run_ref):
        self.polls.append(run_ref)
        state = self.states[run_ref]
        if isinstance(state, Exception):
            raise state
        return {"state": state, "detail": "step failed" if state == "failed" else None,
                "metrics": {"tasks": 2}, "url": "https://airflow.invalid/run"}

    def logs(self, run_ref):
        return "test log"

    def quality(self, run_ref):
        return []


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "platform-learning.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.delenv("STUDIO_AGL_URL", raising=False)
    db.init_db()
    with db.connect() as c:
        c.execute("INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
                  "VALUES (?,?,?,?,?,?,?)", (OWNER["id"], OWNER["email"], "unused-test-hash",
                                             "Requester", OWNER["role"], 1, time.time()))
        c.commit()
    jobs.init_tables()
    supervisor.init_tables()
    monkeypatch.setattr(supervisor.agent, "llm_available", lambda *a, **k: False)
    monkeypatch.setattr(supervisor, "_email", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "_bridge_output", lambda *a, **k: None)
    monkeypatch.setattr(lightning, "_client", lambda: pytest.fail("No live AGL server"))
    fake = FakePlatform()
    monkeypatch.setitem(platforms.PLATFORMS, "airflow", fake)
    return fake


def submit(*, dag="Revenue", context=None, job_id=None):
    return supervisor.submit("platform_run", "airflow", json.dumps({"dag_id": dag}), OWNER,
                             notify=False, job_id=job_id, learning_context=context)


def approved(**kwargs):
    job = submit(**kwargs)
    live = supervisor.approve(job["id"], user=ADMIN)
    return live, live["result"]["run_ref"]


def outcomes():
    return [t for t in db.list_traces(limit=100) if t["mode"] == "pipeline"]


def monitors():
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM background_jobs WHERE kind='platform_monitor'").fetchall()]


def test_approval_and_accepted_running_are_unscored(isolated):
    job = submit()
    assert job["status"] == "awaiting_approval"
    assert isolated.triggers == [] and outcomes() == []
    running = supervisor.approve(job["id"], user=ADMIN)
    assert running["status"] == "running" and len(isolated.triggers) == 1
    assert outcomes() == [] and trainer.stream()["count"] == 0
    assert len(monitors()) == 1
    observed = supervisor.live_job(job["id"], user=ADMIN)
    assert observed["state"] == "running" and outcomes() == []


@pytest.mark.parametrize("status,reward", [("failed", 0.0), ("succeeded", 1.0)])
def test_terminal_outcome_belongs_to_requester_not_admin_poller(isolated, status, reward):
    job, ref = approved(context={"prompt": "Refresh revenue", "conversation_id": "chat-9"})
    isolated.states[ref] = status
    live = supervisor.live_job(job["id"], user=ADMIN)
    assert live["state"] == status
    assert len(outcomes()) == 1
    trace = lightning._trace(outcomes()[0]["id"])
    assert trace["user_id"] == OWNER["id"] and trace["email"] == OWNER["email"]
    assert trace["role"] == "analyst" and trace["reward"] == reward
    assert trace["prompt"] == "Refresh revenue" and trace["conversation_id"] == "chat-9"
    assert trace["meta"]["run_id"] == f"{job['id']}:{ref}"
    assert trace["meta"]["action"] == {
        "type": "platform_run", "target": "airflow", "payload": {"dag_id": "Revenue"}}
    assert trace["sql"] is None


def test_corrected_success_keeps_failure_and_links_new_physical_run(isolated):
    failed, bad_ref = approved(dag="Broken", context={"prompt": "Refresh revenue"})
    isolated.states[bad_ref] = "failed"
    supervisor.live_job(failed["id"], user=OWNER)
    failed_run = f"{failed['id']}:{bad_ref}"
    fixed, good_ref = approved(dag="Corrected", context={
        "prompt": "Refresh revenue with corrected DAG", "repairs_run_id": failed_run})
    isolated.states[good_ref] = "succeeded"
    supervisor.live_job(fixed["id"], user=ADMIN)
    learned = [lightning._trace(t["id"]) for t in outcomes()]
    assert len(learned) == 2
    failed_trace = next(t for t in learned if t["meta"]["status"] == "failed")
    success = next(t for t in learned if t["meta"]["status"] == "succeeded")
    assert failed_trace["reward"] == 0 and success["reward"] == 1
    assert success["meta"]["repairs_run_id"] == failed_run
    assert success["meta"]["action"]["payload"]["dag_id"] == "Corrected"
    assert failed_trace["meta"]["action"]["payload"]["dag_id"] == "Broken"


@pytest.mark.parametrize("state", ["canceled", "unknown", RuntimeError("status timed out")])
def test_cancellation_and_unknown_state_do_not_score(isolated, state):
    job, ref = approved()
    isolated.states[ref] = state
    result = supervisor.live_job(job["id"], user=OWNER)
    assert result["state"] in ("canceled", "unknown")
    assert outcomes() == [] and trainer.stream()["count"] == 0


def test_repeated_polls_preserve_terminal_failure_even_if_adapter_changes(isolated):
    job, ref = approved()
    isolated.states[ref] = "failed"
    supervisor.live_job(job["id"], user=OWNER)
    trace_id = outcomes()[0]["id"]
    isolated.states[ref] = "succeeded"
    for _ in range(3):
        assert supervisor.live_job(job["id"], user=ADMIN)["state"] == "failed"
    assert len(isolated.polls) == 1
    assert len(outcomes()) == 1 and outcomes()[0]["id"] == trace_id
    assert lightning._trace(trace_id)["reward"] == 0
    assert supervisor._get(job["id"])["status"] == "failed"


def test_durable_queue_observes_completion_without_a_ui_poll(isolated):
    job, ref = approved()
    isolated.states[ref] = "succeeded"
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    monitor = monitors()[0]
    assert monitor["status"] == "done", monitor.get("error")
    assert supervisor._get(job["id"])["status"] == "succeeded"
    assert len(outcomes()) == 1 and len(isolated.triggers) == 1
    assert isolated.polls == [ref]


def test_scheduling_deduplicates_and_reconciles_again_only_after_30_seconds(isolated, monkeypatch):
    job, ref = approved()
    row = supervisor._get(job["id"])
    before = monitors()[0]["id"]
    for _ in range(3):
        assert supervisor._schedule_platform_monitor(row, ref) == before
        supervisor._reconcile_platform_monitors()
    assert len(monitors()) == 1
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert monitors()[0]["status"] == "done" and len(isolated.polls) == 1
    supervisor._reconcile_platform_monitors()
    assert monitors()[0]["status"] == "done"
    assert not jobs.run_one("worker", kinds=["platform_monitor"])
    later = time.time() + 31
    monkeypatch.setattr(supervisor.time, "time", lambda: later)
    isolated.states[ref] = "succeeded"
    supervisor._reconcile_platform_monitors()
    assert monitors()[0]["status"] == "queued" and len(monitors()) == 1
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert len(isolated.polls) == 2 and len(isolated.triggers) == 1
    assert len(outcomes()) == 1
    supervisor._reconcile_platform_monitors()
    assert monitors()[0]["status"] == "done"


def test_failed_queue_insert_does_not_retrigger_platform_and_reconciler_repairs(isolated, monkeypatch):
    job = submit()
    schedule = supervisor._schedule_platform_monitor
    monkeypatch.setattr(supervisor, "_schedule_platform_monitor", lambda *a: (_ for _ in ()).throw(RuntimeError("queue down")))
    running = supervisor.approve(job["id"], user=ADMIN)
    assert running["status"] == "running" and len(isolated.triggers) == 1
    assert monitors() == []
    monkeypatch.setattr(supervisor, "_schedule_platform_monitor", schedule)
    supervisor._reconcile_platform_monitors()
    assert len(monitors()) == 1
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert len(isolated.triggers) == 1


def test_terminal_learning_gap_is_recovered_by_reconciler(isolated, monkeypatch):
    job, ref = approved()
    isolated.states[ref] = "succeeded"
    record = lightning.record_pipeline_outcome
    monkeypatch.setattr(lightning, "record_pipeline_outcome", lambda *a, **k: None)
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert supervisor._get(job["id"])["status"] == "succeeded"
    assert outcomes() == []
    monkeypatch.setattr(lightning, "record_pipeline_outcome", record)
    later = time.time() + 31
    monkeypatch.setattr(supervisor.time, "time", lambda: later)
    supervisor._reconcile_platform_monitors()
    assert monitors()[0]["status"] == "queued"
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert len(outcomes()) == 1 and len(isolated.polls) == 1
    stored = json.loads(supervisor._get(job["id"])["result"])
    assert stored["learning"]["trace_id"] == outcomes()[0]["id"]


def test_monitor_for_old_external_run_does_not_poll_or_score_new_run(isolated):
    job, ref = approved()
    row = supervisor._get(job["id"])
    supervisor._save(row, result=json.dumps({"run_ref": "different-run"}))
    assert jobs.run_one("worker", kinds=["platform_monitor"])
    assert monitors()[0]["status"] == "done"
    assert json.loads(monitors()[0]["result"])["skipped"] == "different_run"
    assert isolated.polls == [] and outcomes() == []
