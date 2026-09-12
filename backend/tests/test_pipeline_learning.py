"""Executed failures and corrected successes remain distinct learning examples."""
import copy
import concurrent.futures
import json
from types import SimpleNamespace

import pytest

from app import chat_pipelines, db, governance, jobs, lightning, pipelines, trainer
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
    for module in (governance, jobs, pipelines):
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
    db.set_trace_reward(failed, 0.2, source="user", note="Needs a different column")
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


def test_configured_delivery_is_atomic_and_idempotent(monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", "https://agl.invalid")
    first = record()
    assert record() == first
    queued = rows("background_jobs")
    assert len(queued) == 1 and queued[0]["kind"] == "agl_emit"
    assert json.loads(queued[0]["payload"])["trace_id"] == first
    assert len(rows("agent_traces")) == 1


def test_failed_enqueue_rolls_back_outcome_and_can_be_retried(monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", "https://agl.invalid")
    enqueue = jobs.enqueue
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue unavailable")))
    assert record() is None
    assert rows("agent_traces") == []
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    assert record() is not None
    assert len(rows("agent_traces")) == len(rows("background_jobs")) == 1


def test_concurrent_terminal_callbacks_create_one_trace_and_one_delivery(monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", "https://agl.invalid")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        callbacks = [pool.submit(record) for _ in range(2)]
        ids = [f.result(timeout=10) for f in callbacks]
    assert ids[0] and ids[0] == ids[1]
    assert len(rows("agent_traces")) == len(rows("background_jobs")) == 1


def test_stream_and_agl_events_keep_platform_actions_out_of_scalar_sql():
    action = {"type": "platform_run", "target": "airflow", "payload": {"dag_id": "daily_revenue"}}
    tid = record("platform-1:remote-run-7", source="airflow", action=action,
                 repairs_run_id="platform-1:remote-run-6")
    trace = lightning._trace(tid)
    sample = trainer.stream()["rollouts"][0]
    assert sample["action"] == action and "sql" not in sample["action"]
    assert sample["meta"]["action"] == action
    assert sample["run_id"] == "platform-1:remote-run-7"
    assert sample["repairs_run_id"] == "platform-1:remote-run-6"
    assert sample["execution_status"] == "success"
    assert trace["sql"] is None
    schemas = SimpleNamespace(EventCreate=lambda **kwargs: SimpleNamespace(**kwargs))
    events = lightning.trajectory_events(trace, schemas)
    payload = next(e.data for e in events if e.event_type == "studio.action")
    assert payload["action"] == action
    assert not any(e.event_type == "studio.query" for e in events)
    assert lightning.rollout_metadata(trace)["repairs_run_id"] == "platform-1:remote-run-6"
    assert lightning.rollout_metadata(trace)["action"] == action


def test_recent_examples_are_owner_scoped_and_follow_current_permissions():
    record("mine-failed", status="failed", error="column does not exist")
    record("mine-fixed", repairs_run_id="mine-failed")
    record("theirs", user=OTHER)
    examples = lightning.recent_pipeline_examples(USER, source="demo")
    assert {e["run_id"] for e in examples} == {"mine-failed", "mine-fixed"}
    assert {e["status"] for e in examples} == {"failed", "success"}
    governance._set("version: 1\nroles:\n  viewer:\n    sources:\n      demo: [web_traffic]\n", "test")
    assert lightning.recent_pipeline_examples(USER, source="demo") == []


def test_real_runner_records_failed_and_corrected_full_recipes():
    bad = {"id": "bad-pipeline", "name": "Revenue", "prompt": "Revenue by region",
           "source": "demo", "steps": [{"name": "Revenue", "source": "demo", "table": "sales",
                                         "sql": "SELECT missing_column FROM sales"}]}
    failed = pipelines.run_pipeline(bad, USER, run_id="failed-run", notify_failure=False)
    assert failed["status"] == "failed"
    fixed = {**bad, "id": "fixed-pipeline", "steps": ACTION["steps"], "repairs_run_id": "failed-run"}
    success = pipelines.run_pipeline(fixed, USER, run_id="fixed-run", notify_failure=False)
    assert success["status"] == "success"
    trace = lightning._trace(success["trace_id"])
    assert trace["meta"]["repairs_run_id"] == "failed-run"
    assert trace["meta"]["action"] == ACTION
    assert lightning._trace(failed["trace_id"])["reward"] == 0


def test_terminal_replay_repairs_missing_outcome_after_execution(monkeypatch):
    pipeline = {"id": "pipeline", "name": "Revenue", "prompt": "Revenue by region",
                "source": "demo", "steps": ACTION["steps"]}
    real = lightning.record_pipeline_outcome
    monkeypatch.setattr(lightning, "record_pipeline_outcome", lambda *a, **k: None)
    result = pipelines.run_pipeline(pipeline, USER, run_id="run", notify_failure=False)
    assert result["status"] == "success" and not result["trace_id"]
    monkeypatch.setattr(lightning, "record_pipeline_outcome", real)
    result = pipelines.run_pipeline(pipeline, USER, run_id="run", notify_failure=False)
    assert result["trace_id"] and len(rows("agent_traces")) == 1


def test_future_builder_uses_only_allowed_owner_experience(monkeypatch):
    record("success")
    record("private-other", user=OTHER)
    captured = {}

    def build(user, prompt, **kwargs):
        captured.update(prompt=prompt, planner_context=kwargs.get("planner_context"))
        return {"source": "demo", "steps": ACTION["steps"], "lineage": {}, "dropped": [], "generation": "model"}

    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(pipelines, "build", build)
    chat_pipelines.build(USER, "Build a pipeline for regional revenue", source="demo", tables=["sales"])
    assert "prior execution examples" in captured["planner_context"]
    assert "private-other" not in captured["planner_context"]
    assert SQL in captured["planner_context"]
    assert SQL not in captured["prompt"]
    chat_pipelines.build(USER, "Build a traffic pipeline", source="demo", tables=["web_traffic"])
    assert captured["planner_context"] is None


@pytest.mark.parametrize("model_fails", [False, True])
def test_historical_grains_and_table_tokens_cannot_change_fallback(monkeypatch, model_fails):
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a, **k: False)
    connector = pipelines.get_connector("demo")
    source = {"connector": connector, "allowed": ["sales", "web_traffic"],
              "schemas": {t: connector.get_schema(t) for t in ("sales", "web_traffic")}}
    monkeypatch.setattr(pipelines, "_accessible", lambda user: [source])
    prompt = "Build a pipeline for revenue by region"
    baseline = chat_pipelines.build(USER, prompt)
    lightning.record_pipeline_outcome(USER, run_id="old-monthly-traffic", prompt="Monthly traffic by page",
        source="demo", action={"type": "sql_pipeline", "steps": [{"name": "Monthly visits",
            "source": "demo", "table": "web_traffic", "sql": "SELECT page, visits FROM web_traffic"}]},
        status="success")
    model_inputs = []
    if model_fails:
        monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a, **k: True)

        def fail_model(user, source, schemas, prompt, spec):
            model_inputs.append(prompt)
            raise RuntimeError("model unavailable")

        monkeypatch.setattr(pipelines, "_llm_steps", fail_model)
    current = chat_pipelines.build(USER, prompt)
    assert current["generation"] == "deterministic"
    assert current["source"] == baseline["source"]
    assert [s["sql"] for s in current["steps"]] == [s["sql"] for s in baseline["steps"]]
    assert [s["intent_warnings"] for s in current["steps"]] == [s["intent_warnings"] for s in baseline["steps"]]
    assert current["prompt"] == prompt
    if model_fails:
        assert "Monthly traffic by page" in model_inputs[0]
        assert "prior execution examples" in model_inputs[0]


def test_model_receives_experience_but_routing_and_saved_prompt_do_not(monkeypatch):
    captured = {}
    route = pipelines.route

    def traced_route(user, prompt):
        captured["routing"] = prompt
        return route(user, prompt)

    def model(user, source, schemas, prompt, spec):
        captured["model"] = prompt
        return [{"name": "Revenue", "table": "sales", "sql": SQL}]

    monkeypatch.setattr(pipelines, "route", traced_route)
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(pipelines, "_llm_steps", model)
    requirement = "Revenue by region"
    experience = "Past monthly Snowflake inventory pipeline succeeded."
    result = pipelines.build(USER, requirement, planner_context=experience)
    assert captured["routing"] == result["prompt"] == requirement
    assert experience in captured["model"]
    assert result["steps"][0]["intent_warnings"] == []


def test_correction_draft_carries_failed_run_link_to_execution(monkeypatch):
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(pipelines, "build", lambda *a, **k: {
        "source": "demo", "steps": ACTION["steps"], "lineage": {}, "dropped": [], "generation": "model"})
    previous = {"prompt": "Revenue by region", "run": {"id": "failed-run", "status": "failed"},
                "steps": [{**ACTION["steps"][0], "sql": "SELECT missing_column FROM sales"}]}
    draft = chat_pipelines.build(USER, "Fix the revenue query", source="demo", previous=previous)
    assert draft["repairs_run_id"] == "failed-run"
    result = chat_pipelines.run(USER, draft, request_id="corrected-request")
    trace = lightning._trace(result["run"]["trace_id"])
    assert trace["meta"]["repairs_run_id"] == "failed-run"
    assert trace["prompt"] == previous["prompt"]
