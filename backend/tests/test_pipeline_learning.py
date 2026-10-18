"""Executed failures and corrected successes remain distinct learning examples."""
import copy
import concurrent.futures
import json
from types import SimpleNamespace

import pytest

from app import db, governance, jobs, lightning, trainer
from app.connectors import demo


USER = {"id": "pipeline-learner", "email": "learner@studio.test", "role": "viewer"}
OTHER = {"id": "other-learner", "email": "other@studio.test", "role": "viewer"}
SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region"
ACTION = {"type": "sql_pipeline", "steps": [
    {"name": "Revenue", "source": "demo", "table": "sales", "sql": SQL}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "learning.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    monkeypatch.delenv("STUDIO_AGL_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)
    db.init_db()
    for module in (governance, jobs):
        module.init_tables()
    demo.seed()
    monkeypatch.setattr(lightning, "_client", lambda: pytest.fail("Learning must only enqueue, never contact AGL here"))
    yield
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)


def record(run_id="run-1", **kw):
    return lightning.record_pipeline_outcome(
        kw.pop("user", USER), run_id=run_id, prompt="Revenue by region",
        source=kw.pop("source", "demo"), action=kw.pop("action", ACTION),
        status=kw.pop("status", "success"), **kw)


def rows(table):
    with db.connect() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table}").fetchall()]


@pytest.mark.parametrize("status", ["queued", "running", "submitted", "awaiting_approval",
                                   "rejected", "cancelled", "escalated", "unknown", None])
def test_unproved_or_approval_outcomes_are_not_training_successes(status):
    assert record(status=status) is None
    assert rows("agent_traces") == []


@pytest.mark.parametrize("status", ["success", "succeeded", "succeeded_sql_only"])
def test_proven_success_keeps_complete_structured_action(status):
    action = copy.deepcopy(ACTION)
    action["steps"].append({"name": "Count", "source": "demo", "table": "sales",
                            "sql": "SELECT COUNT(*) AS n FROM sales", "rows": [[123]]})
    tid = record(status=status, action=action, conversation_id="chat-1")
    trace = lightning._trace(tid)
    assert trace["reward"] == 1.0 and trace["ok"] == 1
    assert trace["sql"] is None
    assert trace["conversation_id"] == "chat-1"
    assert len(trace["meta"]["action"]["steps"]) == 2
    assert "rows" not in json.dumps(trace["meta"]["action"])
    assert trace["meta"]["status"] == status


def test_replay_preserves_failure_feedback_and_stream_cursor():
    failed = record(status="failed", error="missing column")
    original = lightning._trace(failed)
    db.set_trace_reward(
        failed, 0.2, user_id=USER["id"], source="user",
        note="Needs a different column")
    # Even a contradictory later callback must not rewrite this attempt.
    assert record(status="success") == failed
    current = lightning._trace(failed)
    assert current["ok"] == 0 and current["error"] == "missing column"
    assert current["reward"] == 0.2 and current["reward_source"] == "user"
    assert current["created_at"] == original["created_at"]
    assert current["meta"]["feedback_note"] == "Needs a different column"
    assert len(rows("agent_traces")) == 1


def test_corrected_success_is_a_new_trace_linked_to_the_failure():
    failed_action = copy.deepcopy(ACTION)
    failed_action["steps"][0]["sql"] = "SELECT missing_column FROM sales"
    failed = record("failed-run", action=failed_action, status="failed", error="missing column")
    corrected = record("corrected-run", repairs_run_id="failed-run")
    assert failed != corrected
    assert lightning._trace(failed)["reward"] == 0
    fixed = lightning._trace(corrected)
    assert fixed["reward"] == 1 and fixed["meta"]["repairs_run_id"] == "failed-run"
    assert fixed["meta"]["action"] == ACTION


def test_configured_generic_delivery_does_not_receive_pipeline_bundles(monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", "https://agl.invalid")
    first = record()
    assert record() == first
    assert rows("background_jobs") == []
    assert len(rows("agent_traces")) == 1
    assert lightning._trace(first)["meta"]["global_train_eligible"] is False


def test_generic_delivery_queue_failure_cannot_erase_local_pipeline_outcome(monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", "https://agl.invalid")
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue unavailable")))
    assert record() is not None
    assert len(rows("agent_traces")) == 1
    assert rows("background_jobs") == []


def test_concurrent_terminal_callbacks_create_one_local_trace(monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", "https://agl.invalid")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        callbacks = [pool.submit(record) for _ in range(2)]
        ids = [f.result(timeout=10) for f in callbacks]
    assert ids[0] and ids[0] == ids[1]
    assert len(rows("agent_traces")) == 1
    assert rows("background_jobs") == []


def test_platform_actions_stay_out_of_generic_training_stream_and_scalar_sql():
    action = {"type": "platform_run", "target": "airflow", "payload": {"dag_id": "daily_revenue"}}
    tid = record("platform-1:remote-run-7", source="airflow", action=action,
                 repairs_run_id="platform-1:remote-run-6")
    trace = lightning._trace(tid)
    assert trainer.stream()["rollouts"] == []
    assert trace["sql"] is None
    schemas = SimpleNamespace(EventCreate=lambda **kwargs: SimpleNamespace(**kwargs))
    events = lightning.trajectory_events(trace, schemas)
    payload = next(e.data for e in events if e.event_type == "studio.action")
    assert payload["action"] == action
    assert not any(e.event_type == "studio.query" for e in events)
    assert lightning.rollout_metadata(trace)["repairs_run_id"] == "platform-1:remote-run-6"
    assert lightning.rollout_metadata(trace)["action"] == action


