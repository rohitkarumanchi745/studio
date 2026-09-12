"""Lightning decisions enter guarded durable DAG recovery, never blind POST retries."""
import copy
import json
import time

import pytest

from app import dag_recovery, db, jobs, recovery_planner, supervisor, workflow_runs
from test_workflow_runs import ADMIN, ANALYST, _approve_and_launch, isolated, plan


@pytest.fixture(autouse=True)
def agent_stub(monkeypatch):
    monkeypatch.setattr(dag_recovery, "_schema", lambda *a: {"sales": [{"name": "amount", "type": "numeric"}]})
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: {
        "decision": "retry", "reason": "Transient connection reset", "rollout_id": "studio-recovery-00000000-0000-0000-0000-000000000001"})
    monkeypatch.setattr(recovery_planner, "record_outcome", lambda *a, **k: {"delivered": True})


def _failed(plan, isolated, *, read_only=True, attempt=0):
    airflow, _ = isolated
    plan = copy.deepcopy(plan)
    if read_only:
        plan["tasks"] = [{"id": "total", "source": "warehouse", "sql": "SELECT SUM(amount) FROM sales", "depends_on": []}]
    row = _approve_and_launch(plan, airflow)
    result = json.loads(row["result"])
    result["studio"]["agent_recovery"] = {"enabled": True, "max_attempts": 2, "attempt": attempt}
    supervisor._save(row, result=json.dumps(result))
    airflow.state = "failed"
    supervisor.live_job(row["id"], ANALYST)
    return supervisor._get(row["id"])


def _recover(row):
    with db.connect() as connection:
        connection.execute("UPDATE background_jobs SET run_after=? WHERE kind='dag_recovery'", (time.time() - 1,))
        connection.commit()
    assert jobs.run_one("dag-recovery-test", kinds=["dag_recovery"])
    return supervisor.get_job(row["id"], ANALYST)


def test_known_readonly_failure_automatically_authorizes_one_new_child(plan, isolated):
    airflow, _ = isolated
    row = _failed(plan, isolated)
    assert len(airflow.triggered) == 1
    result = _recover(row)
    assert result["status"] == "failed"
    recovery = result["recovery"]
    assert recovery["state"] == "retrying" and recovery["attempt"] == 1
    child = supervisor._get(recovery["child_job_id"])
    assert child["status"] == "approved"
    assert child["human_by"] == row["human_by"]
    assert json.loads(child["result"])["agent_authorization"]["parent_job_id"] == row["id"]
    assert len(airflow.triggered) == 1  # Only publication/trigger workers execute.
    assert dag_recovery.recover({"job_id": row["id"], "run_ref": json.loads(row["result"])["run_ref"]})["skipped"] == "not_pending"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 2


def test_write_capable_retry_needs_fresh_human_approval(plan, isolated):
    row = _failed(plan, isolated, read_only=False)
    result = _recover(row)
    assert result["recovery"]["state"] == "awaiting_approval"
    child = supervisor._get(result["recovery"]["child_job_id"])
    assert child["status"] == "awaiting_approval" and not child["human_by"]
    assert len(isolated[0].triggered) == 1


def test_lightning_pending_does_not_spend_execution_budget(plan, isolated, monkeypatch):
    row = _failed(plan, isolated)
    captured = {}
    def pending(*a, **kw):
        captured.update(kw)
        return {"decision": "pending", "reason": "Controller working", "rollout_id": "rollout"}
    monkeypatch.setattr(recovery_planner, "diagnose", pending)
    result = _recover(row)
    assert result["recovery"]["state"] == "diagnosing" and result["recovery"]["attempt"] == 0
    assert isinstance(captured["history"], list)
    assert captured["request_id"].startswith("dag-recovery:")
    assert len(isolated[0].triggered) == 1


def test_budget_is_not_reset_by_failure_or_reconciliation(plan, isolated):
    row = _failed(plan, isolated, attempt=2)
    assert supervisor.get_job(row["id"], ANALYST)["recovery"]["state"] == "exhausted"
    dag_recovery.reconcile()
    assert not jobs.run_one("recovery", kinds=["dag_recovery"])


def test_unknown_trigger_never_enters_recovery(plan, isolated):
    row = _failed(plan, isolated)
    supervisor._save(row, status="escalated", last_error="Trigger outcome uncertain")
    result = _recover(row)
    assert not result["recovery"].get("child_job_id")
    assert len(isolated[0].triggered) == 1


def test_agent_repair_is_validated_and_can_retry_readonly(plan, isolated, monkeypatch):
    row = _failed(plan, isolated)
    repaired = json.loads(row["script"])["plan"]
    repaired["tasks"][0]["sql"] = "SELECT SUM(amount) AS total FROM sales"
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: {
        "decision": "repair", "reason": "Correct the expression", "action": {"type": "airflow_dag", "plan": repaired}})
    result = _recover(row)
    assert result["recovery"]["state"] == "retrying"
    child = supervisor._get(result["recovery"]["child_job_id"])
    assert json.loads(child["script"])["plan"]["tasks"][0]["sql"] == repaired["tasks"][0]["sql"]


@pytest.mark.parametrize("sql", ["SELECT * FROM secret_schema.sales", "DELETE FROM sales", "SELECT SUM(amount) FROM sales"])
def test_unsafe_or_unchanged_repair_does_not_create_child(plan, isolated, monkeypatch, sql):
    row = _failed(plan, isolated)
    repaired = json.loads(row["script"])["plan"]
    repaired["tasks"][0]["sql"] = sql
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: {
        "decision": "repair", "reason": "proposal", "action": {"type": "airflow_dag", "plan": repaired}})
    assert _recover(row)["recovery"]["state"] == "escalated"
    with db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 1


def test_revoked_owner_cannot_ask_agent_or_retry(plan, isolated, monkeypatch):
    row = _failed(plan, isolated)
    with db.connect() as connection:
        connection.execute("UPDATE users SET role='viewer' WHERE id=?", (ANALYST["id"],))
        connection.commit()
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: pytest.fail("Revoked role reached agent"))
    assert _recover(row)["recovery"]["state"] == "escalated"


def test_agent_cannot_silently_change_the_task_graph(plan, isolated, monkeypatch):
    row = _failed(plan, isolated)
    repaired = json.loads(row["script"])["plan"]
    repaired["tasks"][0].update(id="replacement", sql="SELECT SUM(amount) AS total FROM sales")
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: {
        "decision": "repair", "reason": "proposal", "action": {"type": "airflow_dag", "plan": repaired}})
    assert _recover(row)["recovery"]["state"] == "escalated"


def test_terminal_child_rewards_original_decision_and_does_not_relabel_parent(plan, isolated, monkeypatch):
    row = _failed(plan, isolated)
    parent = _recover(row)
    child_id = parent["recovery"]["child_job_id"]
    assert jobs.run_one("publish", kinds=["platform_monitor"])
    child = supervisor._get(child_id)
    if child["status"] == "approved":  # A completed parent's monitor may be first.
        assert jobs.run_one("publish", kinds=["platform_monitor"])
    child = supervisor._get(child_id)
    assert child["status"] == "deploying"
    workflow_runs.observe(child)
    isolated[0].state = "succeeded"
    supervisor.live_job(child_id, ANALYST)
    delivered = []
    monkeypatch.setattr(recovery_planner, "record_outcome", lambda rid, **kw: delivered.append((rid, kw)))
    dag_recovery.reconcile()
    assert jobs.run_one("rewards", kinds=["dag_recovery_reward"])
    assert len(delivered) == 1 and delivered[0][1]["status"] == "succeeded"
    assert supervisor.get_job(row["id"], ANALYST)["recovery"]["state"] == "succeeded"
    assert supervisor._get(row["id"])["status"] == "failed"
