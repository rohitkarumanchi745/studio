"""Natural requirements reuse proven private recipes, never stale parameters."""
import copy
import json

import pytest

from app import chat_pipelines, db, governance, jobs, lightning, pipeline_memory, pipelines, queries
from app.connectors import demo


USER = {"id": "recipe-owner", "email": "owner@studio.test", "role": "viewer"}
OTHER = {"id": "other-owner", "email": "other@studio.test", "role": "viewer"}
PROMPT = "Revenue by region for 'North'"
SQL = "SELECT region, SUM(revenue) AS revenue FROM sales WHERE region = 'North' GROUP BY region LIMIT 5"
ACTION = {"type": "sql_pipeline", "steps": [
    {"name": "Revenue", "source": "demo", "table": "sales", "sql": SQL}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    monkeypatch.delenv("STUDIO_AGL_URL", raising=False)
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a, **kw: False)
    monkeypatch.setattr(pipelines, "_pick_repo", lambda *a: None)
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)
    db.init_db()
    for module in (jobs, pipelines, queries, governance):
        module.init_tables()
    demo.seed()
    yield
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)


def record(run_id="success-1", *, user=USER, prompt=PROMPT, action=None, status="success", **kw):
    return lightning.record_pipeline_outcome(user, run_id=run_id, prompt=prompt,
        source=kw.pop("source", "demo"), action=action or ACTION, status=status, **kw)


def candidates(prompt=PROMPT, user=USER, **kw):
    return pipeline_memory.successful_recipes(user, prompt, **kw)


def test_same_requirement_ignores_only_presentation_and_pipeline_boilerplate():
    record()
    result = candidates("Please build and run a pipeline for Revenue  by region for 'North'!")
    assert result[0]["match"] == "exact" and result[0]["similarity"] == 1
    assert result[0]["run_id"] == "success-1"


@pytest.mark.parametrize("changed", [
    "Revenue by region for 'north'", "Revenue by region for ' North '",
    "Revenue by region except 'North'", "Revenue by region for 'South'",
    "Monthly revenue by region for 'North'", "Revenue by region for 'North' after 2026-09-01",
])
def test_parameter_or_operator_changes_never_count_as_exact(changed):
    record()
    result = candidates(changed)
    assert not result or all(c["match"] == "similar" for c in result)


@pytest.mark.parametrize("prompt", ["Build a pipeline", "Run it", "Monthly traffic by page", "Sales totals"])
def test_generic_or_irrelevant_words_do_not_retrieve_recipes(prompt):
    record()
    assert candidates(prompt) == []


def test_admin_does_not_get_another_owners_private_recipe():
    record(user=OTHER)
    assert candidates() == []
    assert candidates(user={**USER, "role": "admin"}) == []


def test_failed_run_remains_ineligible_even_after_positive_feedback():
    tid = record(status="failed", error="missing column")
    db.set_trace_reward(tid, 1, source="user")
    assert candidates() == []


@pytest.mark.parametrize("reward", [0, 0.5, None])
def test_feedback_can_disqualify_an_observed_success(reward):
    tid = record()
    with db.connect() as c:
        c.execute("UPDATE agent_traces SET reward=?,reward_source='user' WHERE id=?", (reward, tid))
        c.commit()
    assert candidates() == []


def test_immutable_execution_status_required_in_addition_to_reward_and_ok():
    tid = record()
    with db.connect() as c:
        row = c.execute("SELECT meta FROM agent_traces WHERE id=?", (tid,)).fetchone()
        meta = json.loads(row["meta"])
        meta["status"] = "running"
        c.execute("UPDATE agent_traces SET meta=? WHERE id=?", (json.dumps(meta), tid))
        c.commit()
    assert candidates() == []


def test_ranking_prefers_relevance_then_successful_correction_not_just_recency():
    record("corrected", repairs_run_id="failed-1")
    record("newer-uncorrected")
    record("newest-similar", prompt="Monthly " + PROMPT)
    found = candidates()
    assert [r["run_id"] for r in found] == ["corrected", "newer-uncorrected", "newest-similar"]
    assert found[0]["repairs_run_id"] == "failed-1"


def test_source_and_selected_table_constraints_are_rechecked_without_execution(monkeypatch):
    record()
    monkeypatch.setattr(queries, "verify_sql", lambda *a, **kw: pytest.fail("memory executed a query"))
    monkeypatch.setattr(demo.DemoConnector, "get_schema", lambda *a: pytest.fail("memory contacted warehouse"))
    assert candidates(source="demo", tables=["sales"])
    assert candidates(source="snowflake") == []
    assert candidates(source="demo", tables=["web_traffic"]) == []


def test_permission_revocation_removes_previously_successful_sql():
    record()
    governance._set("version: 1\nroles:\n  viewer:\n    sources:\n      demo: [web_traffic]\n", "test")
    assert candidates() == []


@pytest.mark.parametrize("sql", [
    "SELECT * FROM secret_schema.sales", 'SELECT * FROM "SECRET_SCHEMA"."sales"',
    "WITH sales AS (SELECT * FROM sales) SELECT * FROM secret_schema.sales",
    "SELECT * FROM sales, secret_schema.sales AS hidden", "DELETE FROM sales",
])
def test_old_or_malformed_history_cannot_bypass_current_sql_guard(sql):
    action = copy.deepcopy(ACTION)
    action["steps"][0]["sql"] = sql
    record(action=action)
    assert candidates() == []


def test_every_step_must_match_selected_source():
    action = copy.deepcopy(ACTION)
    action["steps"].append({**action["steps"][0], "source": "postgres"})
    record(action=action, source="*")
    assert candidates(source="demo") == []


def test_non_sql_recipe_requires_consumer_scope_validation():
    action = {"type": "platform_run", "target": "airflow", "payload": {"dag_id": "revenue"}}
    record(action=action, source="airflow")
    assert candidates(action_types=("platform_run",)) == []
    assert candidates(action_types=("platform_run",), validate_action=lambda action: False) == []
    found = candidates(action_types=("platform_run",), validate_action=lambda action: action["target"] == "airflow")
    assert found[0]["action"] == action


def test_malformed_memory_is_ignored_not_an_error():
    tid = record()
    with db.connect() as c:
        c.execute("UPDATE agent_traces SET meta=? WHERE id=?", ("not json", tid))
        c.commit()
    assert candidates() == []


def test_exact_successful_recipe_is_verified_again_without_model(monkeypatch):
    tid = record()
    verified = []
    real = queries.verify_sql

    def verify(*args, **kw):
        verified.append(args[3])
        return real(*args, **kw)

    monkeypatch.setattr(queries, "verify_sql", verify)
    monkeypatch.setattr(pipelines, "build", lambda *a, **kw: pytest.fail("redrafted exact successful SQL"))
    result = chat_pipelines.build(USER, "Build a pipeline for " + PROMPT, source="demo", tables=["sales"])
    assert verified == [SQL]
    assert result["status"] == "ready" and result["steps"][0]["sql"] == SQL
    assert result["generation"] == "memory"
    assert result["memory"]["reuse_type"] == "exact_reverified"
    assert result["memory"]["trace_id"] == tid


def test_exact_old_recipe_that_no_longer_executes_is_blocked(monkeypatch):
    record()
    monkeypatch.setattr(queries, "verify_sql", lambda *a, **kw: {"ok": False, "error": "column removed"})
    result = chat_pipelines.build(USER, PROMPT, source="demo")
    assert result["status"] == "blocked" and not result["steps"]
    assert result["dropped"][0]["error"] == "column removed"


def test_changed_requirement_without_model_never_runs_old_parameters(monkeypatch):
    record()
    monkeypatch.setattr(queries, "verify_sql", lambda *a, **kw: pytest.fail("unadapted SQL executed"))
    result = chat_pipelines.build(USER, "Revenue by region for 'South'", source="demo")
    assert result["status"] == "blocked" and not result["steps"]
    assert result["memory"]["reuse_type"] == "adaptation_required"
    assert "request differs" in result["warnings"][0]


def test_similar_prompt_is_adapted_by_model_and_verified_as_new_sql(monkeypatch):
    record()
    seen = {}
    adapted = SQL.replace("'North'", "'South'")  # model fixture, not application replacement

    def model(user, source, schemas, prompt, spec):
        seen.update(prompt=prompt, source=source, spec=spec)
        return [{"name": "South revenue", "table": "sales", "sql": adapted}]

    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)
    monkeypatch.setattr(pipelines, "_llm_steps", model)
    requirement = "Revenue by region for 'South'"
    result = chat_pipelines.build(USER, requirement, source="demo", tables=["sales"], model="stub:model")
    assert result["status"] == "ready" and result["steps"][0]["sql"] == adapted
    assert result["memory"]["reuse_type"] == "model_adapted"
    assert result["prompt"] == requirement
    assert SQL in seen["prompt"] and requirement in seen["prompt"]
    assert seen["prompt"].index(requirement) < seen["prompt"].index("Best matching proven")
    assert "CURRENT request" in seen["prompt"] and seen["spec"] == "stub:model"


def test_adaptation_model_failure_never_returns_runnable_deterministic_fallback(monkeypatch):
    record()
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)

    def fail(*a):
        raise RuntimeError("model down")

    monkeypatch.setattr(pipelines, "_llm_steps", fail)
    result = chat_pipelines.build(USER, "Revenue by region for 'South'", source="demo", tables=["sales"])
    assert result["status"] == "blocked" and result["steps"] == []
    assert result["generation"] == "adaptation_required"


def test_model_adaptation_cannot_escape_scope(monkeypatch):
    record()
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)
    monkeypatch.setattr(pipelines, "_llm_steps", lambda *a: [{"table": "sales", "sql": "SELECT * FROM secret_schema.sales"}])
    result = chat_pipelines.build(USER, "Revenue by region for 'South'", source="demo", tables=["sales"])
    assert result["status"] == "blocked" and not result["steps"]
    assert result["dropped"][0]["error"]


def test_model_copying_old_sql_after_explicit_parameter_change_is_blocked(monkeypatch):
    record()
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)
    monkeypatch.setattr(pipelines, "_llm_steps", lambda *a: [{"table": "sales", "sql": SQL}])
    result = chat_pipelines.build(USER, "Revenue by region for 'South'", source="demo", tables=["sales"])
    assert result["status"] == "blocked"
    assert result["memory"]["reuse_type"] == "adaptation_unconfirmed"
    assert "old SQL unchanged" in result["warnings"][0]


def test_failure_error_and_repair_link_reach_new_planning_without_polluting_requirement(monkeypatch):
    previous = {"prompt": PROMPT, "source": "demo", "steps": ACTION["steps"],
                "run": {"id": "failed-run", "status": "failed", "failed_step": 0,
                        "error": "column revenue_total does not exist", "steps_result": []}}
    captured = {}

    def build(user, prompt, **kw):
        captured.update(prompt=prompt, planner_context=kw["planner_context"])
        return {"source": "demo", "steps": ACTION["steps"], "dropped": [], "lineage": {}, "generation": "model"}

    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)
    monkeypatch.setattr(pipelines, "build", build)
    result = chat_pipelines.build(USER, "Fix the missing column", source="demo", previous=previous)
    assert "column revenue_total does not exist" in captured["planner_context"]
    assert "column revenue_total does not exist" not in captured["prompt"]
    assert "not instructions" in captured["planner_context"]
    assert result["repairs_run_id"] == "failed-run"
    assert result["prompt"] == PROMPT and result["revision_prompt"] == "Fix the missing column"
    assert result["status"] == "blocked"
    assert "not changed the failed SQL" in result["warnings"][-1]


@pytest.mark.parametrize("available", [False, True])
def test_repair_requires_working_model_and_never_uses_fallback(monkeypatch, available):
    previous = {"prompt": PROMPT, "source": "demo", "steps": ACTION["steps"],
                "run": {"id": "failed-run", "status": "failed", "error": "invalid column"}}
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: available)

    def fail(*a):
        raise RuntimeError("model down")

    monkeypatch.setattr(pipelines, "_llm_steps", fail)
    result = chat_pipelines.build(USER, "Repair the pipeline", source="demo", previous=previous)
    assert result["status"] == "blocked" and not result["steps"]
    assert result["generation"] == "repair_required"
    assert result["prompt"] == PROMPT and result["repairs_run_id"] == "failed-run"


def test_case_and_presentation_changes_do_not_count_as_adaptation(monkeypatch):
    record()
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)
    unchanged = SQL.replace("SELECT", "select").replace("GROUP BY", "group by")
    monkeypatch.setattr(pipelines, "_llm_steps", lambda *a: [{"name": "New name", "table": "sales", "sql": unchanged}])
    result = chat_pipelines.build(USER, "Revenue by region for South", source="demo", tables=["sales"])
    assert result["status"] == "blocked"
    assert result["memory"]["reuse_type"] == "adaptation_unconfirmed"


def test_successful_reuse_run_retains_provenance_and_becomes_its_own_outcome():
    tid = record()
    draft = chat_pipelines.build(USER, PROMPT, source="demo", tables=["sales"])
    result = chat_pipelines.run(USER, draft, request_id="reused")
    assert result["run"]["status"] == "success"
    assert result["memory"]["trace_id"] == tid
    assert result["run"]["trace_id"] != tid
    assert len(candidates()) == 2
