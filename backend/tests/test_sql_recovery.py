"""Lightning-owned recovery decisions; local durable orchestration, no network."""
import copy
import json
import time

import pytest
from fastapi import HTTPException

from app import chat_pipelines, db, governance, jobs, lightning, pipelines, recovery_planner, sql_recovery
from app.connectors import demo

USER = {"id": "sql-recovery-owner", "email": "sql-recovery@studio.test", "role": "viewer", "verified": 1}
SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region"
FIXED = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region LIMIT 10"
PIPELINE = {"id": "original-pipeline", "name": "Regional revenue", "prompt": "Show revenue by region",
            "source": "demo", "steps": [{"name": "Revenue", "source": "demo", "table": "sales", "sql": SQL}],
            "agent_recovery": {"enabled": True, "max_attempts": 2}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "recovery.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    monkeypatch.delenv("STUDIO_AGL_URL", raising=False)
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0, ident=None)
    db.init_db()
    jobs.init_tables()
    pipelines.init_tables()
    governance.init_tables()
    demo.seed()
    with db.connect() as c:
        c.execute("INSERT INTO users(id,email,password_hash,name,role,verified,created_at) VALUES (?,?,?,?,?,?,?)",
                  (USER["id"], USER["email"], "not-a-login", "Tester", USER["role"], 1, time.time()))
        c.commit()
    monkeypatch.setattr(pipelines.email_service, "send", lambda *a, **k: pytest.fail("Unexpected email"))
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: {"decision": "retry", "reason": "Transient failure", "rollout_id": "rollout-1"})
    monkeypatch.setattr(recovery_planner, "record_outcome", lambda *a, **k: {"delivered": True}, raising=False)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0, ident=None)


def _rows(table):
    with db.connect() as c:
        return [dict(row) for row in c.execute(f"SELECT * FROM {table}").fetchall()]


def _failed(monkeypatch, *, pipeline=None, error="temporary connection error"):
    pipeline = copy.deepcopy(pipeline or PIPELINE)
    # Save the library entry while healthy, then fail only the execution.
    saved = pipelines.save_pipeline(pipelines.SaveIn(**{k: pipeline[k] for k in ("name", "prompt", "source", "steps")}),
                                    USER, pipeline_id=pipeline["id"])
    original = pipelines.queries.verify_sql
    monkeypatch.setattr(pipelines.queries, "verify_sql", lambda *a, **k: {"ok": False, "error": error})
    run = pipelines.run_pipeline({**saved, "agent_recovery": pipeline.get("agent_recovery")}, USER,
                                 run_id="root-run", notify_failure=False)
    monkeypatch.setattr(pipelines.queries, "verify_sql", original)
    assert run["status"] == "failed"
    return run


def _recover():
    assert jobs.run_one("sql-test-worker", kinds=[sql_recovery.KIND])


def test_ordinary_pipeline_failure_does_not_enroll(monkeypatch):
    pipeline = copy.deepcopy(PIPELINE)
    pipeline.pop("agent_recovery")
    _failed(monkeypatch, pipeline=pipeline)
    assert not _rows("sql_agent_recoveries")
    assert not _rows("background_jobs")


def test_lightning_retry_creates_new_success_and_preserves_failed_reward(monkeypatch):
    failed = _failed(monkeypatch)
    db.set_trace_reward(failed["trace_id"], 0.25, source="user", note="Keep my feedback")
    _recover()
    state = sql_recovery.status("root-run", USER)
    assert state["status"] == "succeeded", state
    assert state["attempts"] == 1
    assert state["child_run_id"] != "root-run"
    assert state["child_pipeline_id"] == PIPELINE["id"]
    traces = [lightning._trace(row["id"]) for row in _rows("agent_traces")]
    assert sorted(t["reward"] for t in traces) == [0.25, 1]
    success = next(t for t in traces if t["ok"])
    assert success["meta"]["repairs_run_id"] == "root-run"
    assert any(row["kind"] == sql_recovery.OUTCOME_KIND for row in _rows("background_jobs"))


def test_lightning_repair_uses_returned_action_without_calling_builder(monkeypatch):
    _failed(monkeypatch, error="result needs revised limit")
    action = {"type": "sql_pipeline", "steps": [{**PIPELINE["steps"][0], "sql": FIXED}]}
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: {"decision": "repair", "reason": "Add bounded limit", "action": action, "rollout_id": "repair-rollout"})
    monkeypatch.setattr(chat_pipelines, "build", lambda *a, **k: pytest.fail("Must not invoke a second model"))
    _recover()
    state = sql_recovery.status("root-run", USER)
    assert state["status"] == "succeeded", state
    current = pipelines._own_or_404(state["child_pipeline_id"], USER)
    assert current["steps"][0]["sql"] == FIXED
    assert current["prompt"] == PIPELINE["prompt"]


def test_reclaimed_worker_cannot_dispatch_after_lightning_decision(monkeypatch):
    _failed(monkeypatch)
    def steal_claim(*a, **kw):
        with db.connect() as connection:
            connection.execute("UPDATE background_jobs SET locked_by='replacement-worker' WHERE kind=? AND status='running'", (sql_recovery.KIND,))
            connection.commit()
        return {"decision": "retry", "reason": "Transient failure"}
    monkeypatch.setattr(recovery_planner, "diagnose", steal_claim)
    _recover()
    assert len(_rows("pipeline_runs")) == 1
    assert sql_recovery.status("root-run", USER)["state"] == "diagnosing"


def test_missing_recovery_queue_record_is_reconciled(monkeypatch):
    _failed(monkeypatch)
    with db.connect() as connection:
        connection.execute("DELETE FROM background_jobs WHERE kind=?", (sql_recovery.KIND,))
        connection.commit()
    sql_recovery._reconcile()
    assert len([job for job in _rows("background_jobs") if job["kind"] == sql_recovery.KIND]) == 1


@pytest.mark.parametrize("action", [
    {"type": "sql_pipeline", "steps": PIPELINE["steps"]},
    {"type": "sql_pipeline", "steps": [{"source": "demo", "sql": "DELETE FROM sales"}]},
    {"type": "sql_pipeline", "steps": [{"source": "demo", "sql": "SELECT * FROM secret_schema.sales"}]},
    {"type": "sql_pipeline", "steps": [{"source": "snowflake", "sql": "SELECT * FROM sales"}]},
    {"type": "sql_pipeline", "steps": []},
])
def test_unsafe_or_unchanged_repairs_escalate_without_execution(monkeypatch, action):
    _failed(monkeypatch)
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: {"decision": "repair", "action": action})
    _recover()
    assert sql_recovery.status("root-run", USER)["status"] == "escalated"
    assert len(_rows("pipeline_runs")) == 1


def test_pending_rollout_uses_same_request_id_and_does_not_spend_execution_attempt(monkeypatch):
    _failed(monkeypatch)
    calls = []
    def decide(*a, **kw):
        calls.append(kw)
        return {"decision": "pending" if len(calls) == 1 else "retry", "rollout_id": "durable-rollout"}
    monkeypatch.setattr(recovery_planner, "diagnose", decide)
    _recover()
    state = sql_recovery.status("root-run", USER)
    assert state["status"] == "planning" and state["attempts"] == 0
    with db.connect() as c:
        c.execute("UPDATE background_jobs SET run_after=0 WHERE status='queued'")
        c.commit()
    _recover()
    assert calls[0]["request_id"] == calls[1]["request_id"]
    assert "sales" in calls[0]["schema"]["demo"]
    assert sql_recovery.status("root-run", USER)["status"] == "succeeded"


def test_repeated_failures_stop_after_two_agent_execution_attempts(monkeypatch):
    _failed(monkeypatch)
    original = pipelines.run_pipeline
    def fail_run(pipeline, user, **kw):
        verify = pipelines.queries.verify_sql
        monkeypatch.setattr(pipelines.queries, "verify_sql", lambda *a, **k: {"ok": False, "error": "connection unavailable"})
        try:
            return original(pipeline, user, **kw)
        finally:
            monkeypatch.setattr(pipelines.queries, "verify_sql", verify)
    monkeypatch.setattr(pipelines, "run_pipeline", fail_run)
    _recover()
    assert sql_recovery.status("root-run", USER)["status"] == "queued"
    _recover()
    state = sql_recovery.status("root-run", USER)
    assert state["status"] == "escalated" and state["attempts"] == 2
    assert len(_rows("pipeline_runs")) == 3
    assert not jobs.run_one("sql-test-worker", kinds=[sql_recovery.KIND])


@pytest.mark.parametrize("change", ["deleted", "unverified", "scope"])
def test_current_permissions_rechecked_before_diagnosis(monkeypatch, change):
    _failed(monkeypatch)
    if change == "scope":
        governance._set("version: 1\nroles:\n  viewer:\n    sources:\n      demo: [web_traffic]\n", "test")
    else:
        with db.connect() as c:
            c.execute("DELETE FROM users WHERE id=?" if change == "deleted" else "UPDATE users SET verified=0 WHERE id=?", (USER["id"],))
            c.commit()
    monkeypatch.setattr(recovery_planner, "diagnose", lambda *a, **k: pytest.fail("Revoked user reached Lightning"))
    _recover()
    assert _rows("sql_agent_recoveries")[0]["state"] == "escalated"
    assert len(_rows("pipeline_runs")) == 1


def test_uncertain_outcome_never_enqueues_retry(monkeypatch):
    _failed(monkeypatch, error="unknown outcome: request still running")
    assert sql_recovery.status("root-run", USER)["status"] == "escalated"
    assert not _rows("background_jobs")


def test_duplicate_terminal_callback_and_job_replay_do_not_duplicate_runs(monkeypatch):
    _failed(monkeypatch)
    sql_recovery.observe("root-run")
    sql_recovery._reconcile()
    assert len([j for j in _rows("background_jobs") if j["kind"] == sql_recovery.KIND]) == 1
    _recover()
    before = len(_rows("pipeline_runs"))
    sql_recovery._recover({"root_run_id": "root-run", "failed_run_id": "root-run"}, {})
    sql_recovery._reconcile()
    assert len(_rows("pipeline_runs")) == before


def test_status_endpoint_is_execution_owner_scoped(monkeypatch):
    _failed(monkeypatch)
    response = pipelines.recovery_status(PIPELINE["id"], "root-run", USER)
    assert response["recovery"]["status"] == "queued"
    assert sql_recovery.status("root-run", {"id": "another-user"}) is None
    with pytest.raises(HTTPException) as exc:
        pipelines.recovery_status(PIPELINE["id"], "root-run", {"id": "another-user", "role": "admin"})
    assert exc.value.status_code == 404
