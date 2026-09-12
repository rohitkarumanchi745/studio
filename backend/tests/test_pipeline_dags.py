"""DAG planning tests use a stub catalog and never touch the local database."""
import copy
import json
from types import SimpleNamespace

import pytest

from app import pipeline_dags


USER = {"id": "dag-planner", "role": "analyst"}


class Catalog:
    name = "postgres"
    dialect = "postgres"

    def configured(self):
        return True

    def qualifiers(self):
        return frozenset({"public"})

    def list_tables(self):
        return ["sales", "customers"]

    def get_schema(self, table):
        return [{"name": "sale_id", "type": "INTEGER"}, {"name": "revenue", "type": "FLOAT"}]

    def run_query(self, *args, **kwargs):
        pytest.fail("Plan validation must not execute SQL")

    def run_script(self, *args, **kwargs):
        pytest.fail("Plan validation must not execute writes")


@pytest.fixture(autouse=True)
def scope(monkeypatch):
    monkeypatch.setattr(pipeline_dags, "get_connector", lambda source: Catalog())
    monkeypatch.setattr(pipeline_dags.rbac, "allowed_sources", lambda role: {"postgres"})
    monkeypatch.setattr(pipeline_dags.rbac, "allowed_tables", lambda role, source, tables: list(tables))
    monkeypatch.setattr(pipeline_dags.rbac, "can_access", lambda role, source, table: table != "secrets")
    monkeypatch.setattr(pipeline_dags.jobs, "check_claim", lambda: None)
    monkeypatch.setattr(pipeline_dags.governance, "_rules_for", lambda *args: None)
    monkeypatch.setattr(pipeline_dags.governance, "column_rules", lambda *args: {"deny": set(), "mask": set()})
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", '{"postgres":"warehouse_pg"}')
    monkeypatch.setattr(pipeline_dags.agent, "llm_available", lambda *a, **k: False)


def plan():
    return {"version": 1, "dag_id": "daily_sales", "name": "Daily sales", "source": "postgres",
            "prompt": "Create daily_sales from sales then summarize revenue", "schedule": None,
            "parameters": {}, "tasks": [
                {"id": "extract", "sql": "CREATE TABLE daily_sales AS SELECT * FROM sales", "produces": "daily_sales", "depends_on": []},
                {"id": "total", "sql": "SELECT SUM(revenue) FROM daily_sales", "depends_on": ["extract"]}]}


def test_valid_dependency_aware_dag_is_statically_checked_without_execution():
    raw = plan()
    before = copy.deepcopy(raw)
    result = pipeline_dags.validate(USER, raw)
    assert raw == before
    assert result["status"] == "ready", result
    assert result["execution_mode"] == "airflow_dag"
    assert result["connection_id"] == "warehouse_pg"
    assert result["tasks"][0]["kind"] == "create_table_as"
    assert result["tasks"][1]["depends_on"] == ["extract"]
    assert all(t["conn_id"] == "warehouse_pg" for t in result["tasks"])
    assert result["requires_approval"]


def test_out_of_order_tasks_normalize_into_dependency_order():
    raw = plan()
    raw["tasks"].reverse()
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "ready", result
    assert [t["id"] for t in result["tasks"]] == ["extract", "total"]


def test_transitive_dependencies_can_read_ancestor_output():
    raw = plan()
    raw["tasks"].append({"id": "check", "sql": "SELECT * FROM daily_sales", "depends_on": ["total"]})
    assert pipeline_dags.validate(USER, raw)["status"] == "ready"


def test_duplicate_sql_steps_are_rejected_instead_of_executed_twice():
    raw = plan()
    raw["tasks"] = [{"id": "one", "sql": "SELECT * FROM sales"},
                    {"id": "two", "sql": "SELECT/**/ * FROM sales;"}]
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "blocked"
    assert "Duplicate SQL tasks" in result["errors"][0]


@pytest.mark.parametrize("change,fragment", [
    (lambda p: p["tasks"][1].update(depends_on=[]), "not permitted"),
    (lambda p: p["tasks"][0].update(depends_on=["total"]), "cycle"),
    (lambda p: p["tasks"][0].update(depends_on=["missing"]), "Unknown dependency"),
    (lambda p: p["tasks"][1].update(id="extract"), "unique"),
    (lambda p: p["tasks"][1].update(depends_on=["extract", "extract"]), "unique"),
    (lambda p: p["tasks"][1].update(source="snowflake"), "Cross-source"),
    (lambda p: p.update(schedule="@daily"), "scheduling"),
    (lambda p: p.update(code="import os"), "executable model code"),
    (lambda p: p["tasks"][0].update(operator="BashOperator"), "executable model code"),
    (lambda p: p["tasks"][0].update(id="a; import os"), "safe identifiers"),
    (lambda p: p.update(dag_id="../../outside"), "safe identifier"),
    (lambda p: p["tasks"][0].update(produces="different_output"), "does not match"),
    (lambda p: p["tasks"][1].update(produces="total"), "SELECT task"),
])
def test_structural_or_dependency_violations_fail_closed(change, fragment):
    raw = plan()
    change(raw)
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "blocked", result
    assert result["tasks"] == []
    assert fragment in result["errors"][0]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM secret_schema.sales",
    "SELECT * FROM other_db.public.sales",
    'SELECT * FROM "secret_schema"."sales"',
    "SELECT * FROM sales, secret_schema.customers",
    "WITH sales AS (SELECT * FROM customers) SELECT * FROM secret_schema.sales",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM sales; DROP TABLE sales",
    "SELECT * FROM sales;;",
    "SELECT * FROM {{ var.value.secret }}",
    "SELECT * FROM sales WHERE sale_id = %(sale_id)s",
    "SELECT * FROM sales WHERE sale_id = :sale_id",
    "DELETE FROM sales",
    "MERGE INTO daily_sales USING sales ON daily_sales.sale_id=sales.sale_id WHEN MATCHED THEN UPDATE SET revenue=sales.revenue",
    "CREATE OR REPLACE TABLE daily_sales AS SELECT * FROM sales",
    "INSERT INTO daily_sales VALUES (1)",
    "print('hello')",
])
def test_unsafe_or_unsupported_sql_cannot_be_deployed(sql):
    raw = plan()
    raw["tasks"] = [{"id": "unsafe", "sql": sql, "depends_on": []}]
    assert pipeline_dags.validate(USER, raw)["status"] == "blocked"


@pytest.mark.parametrize("target", ["secret_schema.out", '"secret_schema"."out"', "other_db.public.out", "secrets"])
def test_output_destination_namespace_and_role_are_checked(target):
    raw = plan()
    raw["tasks"] = [{"id": "write", "sql": f"CREATE TABLE {target} AS SELECT * FROM sales", "produces": target}]
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "blocked", result


def test_authorized_qualified_and_quoted_output_keeps_case_identity():
    raw = plan()
    raw["tasks"] = [
        {"id": "write", "sql": 'CREATE TABLE public."DAILY" AS SELECT * FROM sales', "produces": 'public."DAILY"'},
        {"id": "read", "sql": 'SELECT * FROM public."DAILY"', "depends_on": ["write"]}]
    assert pipeline_dags.validate(USER, raw)["status"] == "ready"
    raw["tasks"][1]["sql"] = "SELECT * FROM public.daily"
    assert pipeline_dags.validate(USER, raw)["status"] == "blocked"


def test_output_must_be_declared_even_when_sql_is_supported():
    raw = plan()
    del raw["tasks"][0]["produces"]
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "blocked"
    assert "explicitly declare" in result["errors"][0]


def test_input_selection_is_not_widened_by_model():
    result = pipeline_dags.validate(USER, plan(), tables=["customers"])
    assert result["status"] == "blocked"
    assert "not permitted" in result["errors"][0]


def test_source_selection_is_not_widened_by_model():
    result = pipeline_dags.validate(USER, plan(), source="snowflake")
    assert result["status"] == "blocked"
    assert "outside the selected" in result["errors"][0]


def test_viewers_cannot_create_external_execution_plans():
    result = pipeline_dags.validate({**USER, "role": "viewer"}, plan())
    assert result["status"] == "blocked"
    assert "admins and analysts" in result["errors"][0]


def test_missing_operator_mapping_is_configuration_not_success(monkeypatch):
    monkeypatch.delenv("STUDIO_AIRFLOW_CONNECTIONS_JSON")
    raw = plan()
    raw["connection_id"] = "model_guessed_credentials"
    raw["tasks"][0]["conn_id"] = "model_guessed_credentials"
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "needs_input"
    assert result["connection_id"] is None
    assert "STUDIO_AIRFLOW_CONNECTIONS_JSON" in result["missing"][0]


def test_insert_select_is_supported_and_append_risk_is_visible(monkeypatch):
    monkeypatch.setattr(Catalog, "list_tables", lambda self: ["sales", "customers", "daily_sales"])
    raw = plan()
    raw["tasks"] = [{"id": "append", "sql": "INSERT INTO public.daily_sales (sale_id, revenue) SELECT sale_id, revenue FROM sales", "produces": "public.daily_sales"}]
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "ready", result
    assert result["tasks"][0]["kind"] == "insert_select"
    assert any("duplicate rows" in warning for warning in result["warnings"])


def test_comments_are_removed_and_write_suffix_is_guarded():
    raw = plan()
    raw["tasks"][0]["sql"] = "CREATE/**/TABLE daily_sales AS SELECT * FROM/**/sales; -- harmless"
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "ready", result
    assert "/**/" not in result["tasks"][0]["sql"]
    assert "--" not in result["tasks"][0]["sql"]


def test_no_model_means_needs_input_not_a_fabricated_pipeline():
    result = pipeline_dags.build(USER, "Create daily_sales from sales", source="postgres")
    assert result["status"] == "needs_input"
    assert result["tasks"] == []
    assert "planning model" in result["missing"][0]


def test_no_source_is_never_guessed():
    result = pipeline_dags.build(USER, "Load sales into revenue")
    assert result["status"] == "needs_input"
    assert "Select the data source" in result["missing"][0]


def _model(monkeypatch, response):
    calls = []
    def invoke(messages):
        calls.append(messages)
        return SimpleNamespace(content=response)
    monkeypatch.setattr(pipeline_dags.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_dags.agent, "make_llm", lambda *a, **k: SimpleNamespace(invoke=invoke))
    return calls


def test_model_json_becomes_a_ready_validated_dag(monkeypatch):
    calls = _model(monkeypatch, json.dumps(plan()))
    result = pipeline_dags.build(USER, "Create daily_sales from sales then summarize revenue", source="postgres", tables=["sales"])
    assert result["status"] == "ready", result
    assert result["generation"] == "model"
    payload = json.loads(calls[0][1][1])
    assert set(payload["authorized_input_schema"]) == {"sales"}
    assert "deduplication keys" in calls[0][0][1]


def test_model_invented_destination_requires_confirmation(monkeypatch):
    _model(monkeypatch, json.dumps(plan()))
    result = pipeline_dags.build(USER, "Prepare sales data", source="postgres")
    assert result["status"] == "needs_input"
    assert any("Confirm the output table 'daily_sales'" in q for q in result["missing"])


def test_missing_business_requirements_remain_questions_not_success(monkeypatch):
    _model(monkeypatch, json.dumps({"source": "postgres", "tasks": [], "missing": ["Which key identifies duplicates?"]}))
    result = pipeline_dags.build(USER, "Remove duplicates and load revenue", source="postgres")
    assert result["status"] == "needs_input"
    assert result["tasks"] == []
    assert "Which columns identify duplicates" in result["missing"][0]


@pytest.mark.parametrize("response", ["import os\nos.system('bad')", "```python\nprint(1)\n```", "[]", '{"tasks": "bad"}'])
def test_invalid_model_output_cannot_become_runnable(monkeypatch, response):
    _model(monkeypatch, response)
    result = pipeline_dags.build(USER, "Create daily_sales from sales", source="postgres")
    assert result["status"] in ("needs_input", "blocked")
    assert result["tasks"] == []


def test_provider_credentials_are_not_reflected_in_error_artifacts(monkeypatch):
    monkeypatch.setattr(pipeline_dags.agent, "llm_available", lambda *a, **k: True)
    def broken(*a, **k):
        raise RuntimeError("Authorization: sk-super-secret")
    monkeypatch.setattr(pipeline_dags.agent, "make_llm", broken)
    result = pipeline_dags.build(USER, "Create daily_sales from sales", source="postgres")
    assert result["status"] == "needs_input"
    assert "sk-super-secret" not in json.dumps(result)


def test_revalidation_rechecks_current_permissions(monkeypatch):
    result = pipeline_dags.validate(USER, plan())
    assert result["status"] == "ready"
    monkeypatch.setattr(pipeline_dags.rbac, "allowed_tables", lambda *args: [])
    assert pipeline_dags.validate(USER, result)["status"] == "blocked"


@pytest.mark.parametrize("rules", [
    {"deny": {"revenue"}, "mask": set(), "max_rows": None},
    {"deny": set(), "mask": {"revenue"}, "max_rows": None},
    {"deny": set(), "mask": set(), "max_rows": 20},
])
def test_external_execution_refuses_gateway_only_governance(monkeypatch, rules):
    monkeypatch.setattr(pipeline_dags.governance, "_rules_for", lambda *args: rules)
    result = pipeline_dags.validate(USER, plan())
    assert result["status"] == "blocked"
    assert "governed data" in result["errors"][0]


def test_qualified_table_cannot_use_cte_to_hide_governance(monkeypatch):
    def rules(source, tables):
        return {"deny": {"revenue"}, "mask": set(), "max_rows": None} if "sales" in tables else None
    monkeypatch.setattr(pipeline_dags.governance, "_rules_for", rules)
    raw = plan()
    raw["tasks"] = [{"id": "read", "sql": "WITH sales AS (SELECT * FROM customers) SELECT * FROM public.sales"}]
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "blocked"
    assert "governed data" in result["errors"][0]


def test_denied_column_names_are_not_sent_to_planning_model(monkeypatch):
    monkeypatch.setattr(pipeline_dags.governance, "column_rules", lambda *args: {"deny": {"sale_id"}, "mask": set()})
    calls = _model(monkeypatch, json.dumps({"source": "postgres", "tasks": [], "missing": ["Need destination"]}))
    pipeline_dags.build(USER, "Prepare sales", source="postgres", tables=["sales"])
    schema = json.loads(calls[0][1][1])["authorized_input_schema"]["sales"]
    assert schema == [{"name": "revenue", "type": "FLOAT"}]


def test_sql_literals_are_not_mistaken_for_unbound_parameters():
    raw = plan()
    raw["tasks"] = [{"id": "read", "sql": "SELECT '? :value %(value)s' AS explanation, sale_id::text FROM sales"}]
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "ready", result


def test_nonempty_parameter_values_are_metadata_and_require_fresh_sql_adaptation():
    raw = plan()
    raw["parameters"] = {"day": "2026-09-10"}
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "ready"
    assert result["parameters"] == {"day": "2026-09-10"}
    assert any("SQL contains concrete values" in warning for warning in result["warnings"])


def test_previous_ctas_success_cannot_reuse_an_existing_target(monkeypatch):
    monkeypatch.setattr(Catalog, "list_tables", lambda self: ["sales", "customers", "daily_sales"])
    result = pipeline_dags.validate(USER, plan())
    assert result["status"] == "needs_input", result
    assert "already exists" in result["missing"][0]


def test_existing_produced_table_still_needs_ancestor_dependency(monkeypatch):
    monkeypatch.setattr(Catalog, "list_tables", lambda self: ["sales", "customers", "daily_sales"])
    raw = plan()
    raw["tasks"][1]["depends_on"] = []
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "blocked"
    assert "not permitted" in result["errors"][0]


def test_insert_into_unknown_destination_requests_clarification():
    raw = plan()
    raw["tasks"] = [{"id": "append", "sql": "INSERT INTO daily_sales SELECT * FROM sales", "produces": "daily_sales"}]
    result = pipeline_dags.validate(USER, raw)
    assert result["status"] == "needs_input"
    assert "not in the current catalog" in result["missing"][0]


def test_duplicate_keys_are_requested_before_a_model_can_guess(monkeypatch):
    calls = _model(monkeypatch, json.dumps(plan()))
    result = pipeline_dags.build(USER, "Remove duplicates and create daily_sales", source="postgres")
    assert result["status"] == "needs_input"
    assert result["tasks"] == []
    assert "Which columns identify duplicates" in result["missing"][0]
    assert calls == []


def test_duplicate_winner_rule_must_be_explicit(monkeypatch):
    calls = _model(monkeypatch, json.dumps(plan()))
    result = pipeline_dags.build(USER, "Deduplicate sales by sale_id and create daily_sales", source="postgres")
    assert result["status"] == "needs_input"
    assert "Which row should be kept" in result["missing"][0]
    assert calls == []


def test_full_row_distinct_is_explicit_deduplication_semantics(monkeypatch):
    calls = _model(monkeypatch, json.dumps(plan()))
    result = pipeline_dags.build(USER, "Remove duplicates using full-row DISTINCT and create daily_sales", source="postgres")
    assert result["status"] == "ready", result
    assert calls


def test_model_history_cannot_confirm_a_proposed_output(monkeypatch):
    calls = _model(monkeypatch, json.dumps(plan()))
    result = pipeline_dags.build(USER, "Prepare sales", source="postgres",
                                 context="assistant: Please confirm proposed table daily_sales",
                                 examples=[{"prompt": "Create daily_sales"}])
    assert result["status"] == "needs_input"
    assert any("Confirm the output table" in q for q in result["missing"])
    assert result["confirmed_outputs"] == []
    payload = json.loads(calls[0][1][1])
    assert payload["request"] == "Prepare sales"
    assert "daily_sales" in payload["conversation_context"]


def test_nonready_previous_plan_cannot_self_confirm_proposed_output(monkeypatch):
    _model(monkeypatch, json.dumps(plan()))
    previous = {**plan(), "status": "needs_input"}
    result = pipeline_dags.build(USER, "Use sale_id as the key", source="postgres", previous=previous)
    assert result["status"] == "needs_input"
    assert any("Confirm the output table" in q for q in result["missing"])


def test_ready_previous_plan_retains_explicit_output_authority(monkeypatch):
    _model(monkeypatch, json.dumps(plan()))
    previous = {**plan(), "status": "ready"}
    result = pipeline_dags.build(USER, "Filter revenue above zero", source="postgres", previous=previous)
    assert result["status"] == "ready", result
    assert result["confirmed_outputs"] == ["daily_sales"]


@pytest.mark.parametrize("status", ["failed", "succeeded"])
def test_server_approved_previous_plan_retains_destination_for_repair(monkeypatch, status):
    _model(monkeypatch, json.dumps(plan()))
    previous = {**plan(), "status": status, "approved": True,
                "failure": {"run_id": "old:run", "status": "failed", "error": "bad column"}}
    result = pipeline_dags.build(USER, "Fix the invalid column", source="postgres", previous=previous)
    assert result["status"] == "ready", result
    assert result["confirmed_outputs"] == ["daily_sales"]


def test_prior_approval_does_not_authorize_a_new_destination(monkeypatch):
    changed = plan()
    changed["tasks"] = [{"id": "new", "sql": "CREATE TABLE new_destination AS SELECT * FROM sales", "produces": "new_destination"}]
    _model(monkeypatch, json.dumps(changed))
    previous = {**plan(), "status": "failed", "approved": True}
    result = pipeline_dags.build(USER, "Fix the invalid column", source="postgres", previous=previous)
    assert result["status"] == "needs_input"
    assert any("Confirm the output table 'new_destination'" in question for question in result["missing"])
