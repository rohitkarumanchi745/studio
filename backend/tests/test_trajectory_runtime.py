"""Runtime seams for the five private structured-policy contracts."""
import json
from types import SimpleNamespace

import pytest

from app import (agent_graph, db, lightning, orchestrator,
                 policy_trajectories as trajectories, recovery_planner, trainer)


USER = {"id": "trajectory-user", "email": "trajectory@studio.test",
        "role": "analyst"}


class Connector:
    def __init__(self, name):
        self.name = name
        self.dialect = "postgres"


def source(name):
    return {"connector": Connector(name), "allowed": ["sales"],
            "schemas": {"sales": [{"name": "account_id", "type": "INTEGER"},
                                    {"name": "revenue", "type": "FLOAT"}]},
            "skill": f"source: {name}"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setenv("STUDIO_SECRET", "trajectory-runtime-test-secret-which-is-long")
    monkeypatch.setenv("STUDIO_TRAJECTORY_TRAINING", "user")
    db.init_db()
    trajectories.init_tables()


def fetched(contract):
    return trajectories.fetch_training_page(
        scope=trajectories.user_scope(USER), contracts=[contract])["trajectories"]


def sample_input(sample):
    return json.loads(sample["prompt"])["input"]


def sample_target(sample):
    return json.loads(sample["completion"])


def assert_train_serve_prompt(sample):
    contract_input = sample_input(sample)
    live = trajectories.policy_prompt(
        sample["contract"],
        trajectories.normalize_contract_input(sample["contract"], contract_input))
    assert live == sample["prompt"]


def airflow_plan():
    return {
        "version": 1, "name": "Daily sales", "dag_id": "daily_sales",
        "source": "postgres", "prompt": "Build daily_sales from sales",
        "schedule": None, "parameters": {}, "missing": [], "tasks": [{
            "id": "extract", "name": "Extract sales", "source": "postgres",
            "sql": "CREATE TABLE daily_sales AS SELECT * FROM sales",
            "depends_on": [], "produces": "daily_sales",
        }],
    }


def test_complete_airflow_outcome_uses_typed_store_not_generic_rollouts():
    tid = lightning.record_pipeline_outcome(
        USER, run_id="airflow-run-1", prompt="Build daily_sales from sales",
        source="postgres", action={"type": "airflow_dag", "plan": airflow_plan()},
        status="succeeded")

    assert tid
    assert trainer.stream()["rollouts"] == []
    samples = fetched(trajectories.AIRFLOW_DAG)
    assert len(samples) == 1
    sample = samples[0]
    assert sample_target(sample)["tasks"] == [{
        "id": "extract", "name": "Extract sales", "source": "postgres",
        "sql": "CREATE TABLE daily_sales AS SELECT * FROM sales",
        "depends_on": [], "produces": "daily_sales",
    }]
    assert sample["reward"] == 1.0
    assert_train_serve_prompt(sample)


def test_invalid_airflow_recipe_never_enters_the_typed_training_store():
    plan = airflow_plan()
    plan["tasks"][0]["source"] = "snowflake"

    assert lightning.record_pipeline_outcome(
        USER, run_id="airflow-run-invalid", prompt="Build daily_sales from sales",
        source="postgres", action={"type": "airflow_dag", "plan": plan},
        status="succeeded")

    assert fetched(trajectories.AIRFLOW_DAG) == []
    assert trainer.stream()["rollouts"] == []


def test_graph_and_dependent_prompt_capture_the_graph_that_really_ran(monkeypatch):
    sources = [source("postgres"), source("snowflake")]
    raw = {"nodes": [
        {"id": "accounts", "source": "postgres", "task": "find accounts",
         "depends_on": []},
        {"id": "spend", "source": "snowflake", "task": "price those accounts",
         "depends_on": ["accounts"]},
    ], "combine": "reason"}
    plan = agent_graph.validate_plan(raw, sources, "Which accounts spend most?",
                                     strict_sources=True)
    seen = []

    def run_agent(prompt, connector, table, allowed, schemas, history, user,
                  model=None, skill_md=None, **kwargs):
        seen.append((connector.name, prompt))
        rows = ([[f"acct-{index}", index] for index in range(25)]
                if connector.name == "postgres" else [["acct-1", 42]])
        return {"text": f"{connector.name} returned 42", "sql": "SELECT 42",
                "columns": ["account_id", "revenue"], "rows": rows,
                "chart": None, "panels": [], "email": None, "errors": [],
                "mode": "agent", "model": "frontier"}

    monkeypatch.setattr(agent_graph, "plan_graph", lambda *a, **k: plan)
    monkeypatch.setattr(agent_graph.agent, "run_agent", run_agent)
    monkeypatch.setattr(agent_graph.agent, "llm_available", lambda *a, **k: False)
    monkeypatch.setattr(agent_graph.jobs, "check_claim", lambda: None)
    monkeypatch.setattr(agent_graph.lightning, "record_agent_rollout", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.jobs, "check_claim", lambda: None)
    monkeypatch.setattr(orchestrator.progress, "emit", lambda *a, **k: None)
    monkeypatch.setattr(orchestrator.lightning, "record_agent_rollout", lambda *a, **k: None)

    result = orchestrator.run_orchestrated(
        "Which accounts spend most?", USER, [], conversation_id="conversation-1",
        sources=sources)

    assert not result["errors"]
    assert [name for name, _ in seen] == ["postgres", "snowflake"]
    graph_samples = fetched(trajectories.AGENT_GRAPH)
    assert len(graph_samples) == 1
    assert sample_target(graph_samples[0]) == agent_graph.trajectory_target(plan)
    assert graph_samples[0]["reward"] == 1.0
    assert_train_serve_prompt(graph_samples[0])

    dependent = fetched(trajectories.DEPENDENT_AGENT)
    assert len(dependent) == 1
    sample = dependent[0]
    contract_input = sample_input(sample)
    assert sample_target(sample)["prompt"] == seen[1][1]
    assert len(contract_input["evidence"][0]["rows"]) == 20
    assert contract_input["evidence"][0]["rows"][0] == ["acct-0", "0"]
    assert contract_input["evidence"][0]["rows"][-1] == ["acct-19", "19"]
    assert "acct-19" in seen[1][1] and "acct-20" not in seen[1][1]
    assert contract_input["evidence"][0]["id"] in seen[1][1]
    assert_train_serve_prompt(sample)


def test_dependent_provider_fallback_is_not_eligible(monkeypatch):
    sources = [source("postgres"), source("snowflake")]
    plan = agent_graph.validate_plan({"nodes": [
        {"id": "accounts", "source": "postgres", "task": "find accounts",
         "depends_on": []},
        {"id": "spend", "source": "snowflake", "task": "price accounts",
         "depends_on": ["accounts"]},
    ], "combine": "reason"}, sources, "Find spend", strict_sources=True)

    def run_agent(prompt, connector, *args, **kwargs):
        common = {"text": "account 1", "sql": "SELECT 1", "columns": ["id"],
                  "rows": [[1]], "chart": None, "panels": [], "email": None,
                  "errors": []}
        if connector.name == "postgres":
            return {**common, "mode": "agent", "model": "frontier"}
        return common  # deterministic/provider-error preview has no agent mode

    monkeypatch.setattr(agent_graph.agent, "run_agent", run_agent)
    monkeypatch.setattr(agent_graph.agent, "llm_available", lambda *a, **k: False)
    monkeypatch.setattr(agent_graph.jobs, "check_claim", lambda: None)
    monkeypatch.setattr(agent_graph.lightning, "record_agent_rollout", lambda *a, **k: None)

    result = agent_graph.execute(plan, sources, "Find spend", USER,
                                 conversation_id="conversation-fallback")

    assert result["results"]["spend"]["_node"] == "spend"
    assert fetched(trajectories.DEPENDENT_AGENT) == []


def test_aggregator_captures_only_grounded_provider_output(monkeypatch):
    subs = [{"_node": "sales", "_source": "postgres",
             "text": "Revenue is 42", "sql": "SELECT 42 AS revenue",
             "columns": ["revenue"], "rows": [[42]], "errors": []},
            {"_node": "orders", "_source": "snowflake",
             "text": "Orders are 7", "sql": "SELECT 7 AS orders",
             "columns": ["orders"], "rows": [[7]], "errors": []}]
    response = {"text": "Revenue is 99."}
    monkeypatch.setattr(orchestrator.agent, "self_hosted", lambda *a: False)
    monkeypatch.setattr(orchestrator.agent, "concrete_model_spec", lambda value: value)
    monkeypatch.setattr(orchestrator.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(
        orchestrator.agent, "make_llm",
        lambda *a, **k: SimpleNamespace(invoke=lambda messages:
                                        SimpleNamespace(content=response["text"])))

    # The unseen 99 is refused and the deterministic summary is not training.
    text = orchestrator._aggregate("Revenue?", subs, USER, "frontier")
    assert text != response["text"]
    assert fetched(trajectories.AGGREGATOR_OUTPUT) == []

    response["text"] = "Revenue is 42 and orders are 7."
    assert orchestrator._aggregate(
        "Revenue?", subs, USER, "frontier", conversation_id="conversation-2") == \
        "Revenue is 42 and orders are 7."
    samples = fetched(trajectories.AGGREGATOR_OUTPUT)
    assert len(samples) == 1
    sample = samples[0]
    assert sample_target(sample)["citations"] == [
        contribution["id"] for contribution in sample_input(sample)["contributions"]]
    assert_train_serve_prompt(sample)

    monkeypatch.setattr(orchestrator.agent, "make_llm",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    orchestrator._aggregate("A different request", subs, USER, "frontier")
    assert len(fetched(trajectories.AGGREGATOR_OUTPUT)) == 1


def test_recovery_capture_is_outcome_labelled_and_safe_escalation_is_positive():
    action = {"type": "sql_pipeline", "steps": [{
        "name": "Revenue", "source": "postgres", "table": "sales",
        "sql": "SELECT SUM(revenue) FROM sales",
    }]}
    task = recovery_planner._task_input(
        prompt="Summarize revenue", action=action, error="connection reset",
        history=[], schema={"sales": ["revenue"]}, model="recovery-model")
    recovery_planner._capture_decision(
        USER, task, {"decision": "retry", "reason": "transient reset"},
        lineage=["rollout-1", "child-1"], reward=0.0, outcome="failed")
    recovery_planner._capture_decision(
        USER, task, {"decision": "escalate", "reason": "human review"},
        lineage=["rollout-2"], reward=1.0, outcome="escalated")

    samples = fetched(trajectories.RECOVERY_DECISION)
    assert {(sample_target(sample)["decision"], sample["reward"])
            for sample in samples} == {("retry", 0.0), ("escalate", 1.0)}
    assert all(sample_input(sample)["failure"]["state"] == "failed"
               for sample in samples)
    for sample in samples:
        assert_train_serve_prompt(sample)
