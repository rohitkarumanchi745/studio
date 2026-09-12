"""DAG compiler/publication tests: AST inspection, never import/execute Airflow.

All writes target pytest temporary directories. No database, network, or live
Airflow deployment is used. Policy validation has its own planner tests; these
tests replace that boundary explicitly to exercise publication in isolation.
"""
import ast
import copy
import hashlib
import json
import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app import airflow_dags


@pytest.fixture
def plan(monkeypatch):
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", json.dumps({"warehouse": "warehouse_conn"}))
    monkeypatch.delenv("STUDIO_AIRFLOW_DAGS_DIR", raising=False)
    return {"version": 1, "name": "Daily sales", "dag_id": "daily_sales", "source": "warehouse",
            "schedule": None, "parameters": {"day": "2026-09-11", "minimum": 0, "flag": False},
            "tasks": [
                {"id": "extract", "name": "Extract sales", "source": "warehouse",
                 "sql": "CREATE TABLE staged_sales AS SELECT * FROM sales WHERE day = '2026-09-11'",
                 "depends_on": [], "produces": "staged_sales"},
                {"id": "summarize", "name": "Summarize sales", "source": "warehouse",
                 "sql": "SELECT SUM(amount) AS revenue FROM staged_sales",
                 "depends_on": ["extract"]},
            ]}


@pytest.fixture
def approval(monkeypatch):
    """Explicitly stub only the policy boundary for filesystem unit tests."""
    import app
    policy = types.ModuleType("app.pipeline_dags")
    calls = []

    def validate(user, submitted):
        calls.append((user, submitted))
        return copy.deepcopy(submitted)

    policy.validate = validate
    monkeypatch.setitem(sys.modules, "app.pipeline_dags", policy)
    monkeypatch.setattr(app, "pipeline_dags", policy, raising=False)
    return {"id": "reviewer", "role": "admin", "verified": True}, calls


def _calls(tree, name):
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == name]


def _kwargs(call):
    return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords
            if kw.arg != "start_date"}


def test_compiler_emits_explicit_dependencies_and_safe_operator_settings(plan):
    compiled = airflow_dags.artifact(plan)
    tree = ast.parse(compiled["source"])
    dag = _kwargs(_calls(tree, "DAG")[0])
    assert dag["dag_id"] == compiled["dag_id"]
    assert dag["schedule"] is None
    assert dag["catchup"] is False
    # Publishing requires approval, and schedule=None cannot start a run.
    assert dag["is_paused_upon_creation"] is False
    assert dag["default_args"] == {"retries": 0}
    assert dag["max_active_runs"] == 1
    operators = [_kwargs(call) for call in _calls(tree, "SQLExecuteQueryOperator")]
    assert [operator["task_id"] for operator in operators] == ["extract", "summarize"]
    assert all(operator["conn_id"] == "warehouse_conn" for operator in operators)
    assert all(operator["parameters"] is None for operator in operators)
    assert compiled["parameters_mode"] == "compiled_sql"
    audit = next(node for node in tree.body if isinstance(node, ast.Assign)
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "STUDIO_PARAMETERS")
    assert ast.literal_eval(audit.value) == plan["parameters"]
    assert all(operator["autocommit"] is False and operator["split_statements"] is False
               and operator["do_xcom_push"] is False for operator in operators)
    assert all(operator["trigger_rule"] == "all_success" for operator in operators)
    edges = [node for node in ast.walk(tree) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift)]
    assert len(edges) == 1
    assert ast.literal_eval(edges[0].left.slice) == "extract"
    assert ast.literal_eval(edges[0].right.slice) == "summarize"
    assert ".template_fields = ()" in compiled["source"]
    assert ".template_ext = ()" in compiled["source"]
    assert "from airflow.sdk import DAG" in compiled["source"]
    assert "from airflow import DAG" in compiled["source"]
    assert hashlib.sha256(compiled["source"].encode()).hexdigest() == compiled["source_sha256"]


def test_compilation_is_deterministic_and_input_unchanged(plan):
    original = copy.deepcopy(plan)
    first = airflow_dags.artifact(plan)
    reordered = copy.deepcopy(plan)
    reordered["tasks"].reverse()
    reordered["parameters"] = dict(reversed(list(reordered["parameters"].items())))
    assert airflow_dags.artifact(reordered) == first
    assert plan == original
    assert first["filename"] == first["dag_id"] + ".py"
    assert first["dag_id"].endswith(first["digest"])
    assert airflow_dags.compile_dag(plan) == first["source"]


def test_source_and_connection_changes_get_new_immutable_identity(plan, monkeypatch):
    first = airflow_dags.artifact(plan)
    plan["tasks"][1]["sql"] = "SELECT COUNT(*) FROM staged_sales"
    second = airflow_dags.artifact(plan)
    assert first["dag_id"] != second["dag_id"]
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", '{"warehouse":"reviewed_new_connection"}')
    third = airflow_dags.artifact(plan)
    assert second["dag_id"] != third["dag_id"]


def test_constants_cannot_escape_into_python(plan):
    attack = "value'); __import__('os').system('bad'); #\n\\x00"
    plan["name"] = attack
    plan["parameters"]["day"] = attack
    plan["tasks"][0]["sql"] = "SELECT '" + attack + "'"
    tree = ast.parse(airflow_dags.compile_dag(plan))
    called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name)}
    assert called == {"DAG", "datetime", "SQLExecuteQueryOperator"}
    assert _kwargs(_calls(tree, "DAG")[0])["description"] == attack


def test_planner_metadata_is_not_executable_or_digest_material(plan):
    first = airflow_dags.artifact(plan)
    plan.update(execution_mode="airflow_dag", status="ready", missing=[], errors=[],
                prompt="untrusted instructions", connection_id="warehouse_conn")
    plan["tasks"][0].update(kind="create_table_as", read_sql="SELECT * FROM sales",
                             conn_id="warehouse_conn", arbitrary_code="raise RuntimeError('bad')")
    assert airflow_dags.artifact(plan) == first


@pytest.mark.parametrize("output", ['"public"."Daily Sales"', '`public`.`daily_sales`', '[public].[daily_sales]'])
def test_quoted_output_identifiers_remain_metadata_and_cannot_escape_python(plan, output):
    plan["tasks"][0]["produces"] = output
    compiled = airflow_dags.artifact(plan)
    ast.parse(compiled["source"])
    assert airflow_dags.validate_plan(plan)["tasks"][0]["produces"] == output


@pytest.mark.parametrize("key,value", [
    ("version", 2), ("version", True), ("dag_id", "../../overwrite"),
    ("dag_id", "x\nimport os"), ("dag_id", "a" * 101),
    ("schedule", "@daily"), ("source", "../db"), ("parameters", []),
    ("parameters", {"bad-key": 1}), ("parameters", {"day": [1]}),
    ("parameters", {"day": float("nan")}), ("parameters", {"day": float("inf")}),
    ("parameters", {"day": "{{ var.value.password }}"}),
    ("parameters", {"day": "\x00"}), ("tasks", []),
    ("status", "blocked"), ("errors", ["Missing permission"]),
    ("missing", ["connection"]),
])
def test_invalid_plan_is_rejected(plan, key, value):
    plan[key] = value
    with pytest.raises(airflow_dags.AirflowPlanError):
        airflow_dags.artifact(plan)


@pytest.mark.parametrize("key,value", [
    ("id", "bad.id"), ("id", "bad-id"), ("id", "x'] = None"),
    ("source", "other_source"), ("sql", ""), ("sql", "SELECT {{ var.value.password }}"),
    ("sql", "SELECT '{% do unsafe() %}'"), ("sql", "SELECT 1\x00"),
    ("depends_on", ["unknown"]), ("depends_on", ["extract"]),
    ("depends_on", ["summarize", "summarize"]), ("depends_on", "summarize"),
    ("produces", "../../table"),
])
def test_invalid_task_is_rejected(plan, key, value):
    plan["tasks"][0][key] = value
    with pytest.raises(airflow_dags.AirflowPlanError):
        airflow_dags.artifact(plan)


def test_cycle_duplicate_and_size_limits(plan):
    plan["tasks"][0]["depends_on"] = ["summarize"]
    with pytest.raises(airflow_dags.AirflowPlanError, match="cycle"):
        airflow_dags.artifact(plan)
    plan["tasks"][0]["depends_on"] = []
    plan["tasks"].append(copy.deepcopy(plan["tasks"][0]))
    with pytest.raises(airflow_dags.AirflowPlanError, match="Duplicate"):
        airflow_dags.artifact(plan)
    plan["tasks"].pop()
    plan["tasks"][0]["sql"] = "a" * (64 * 1024 + 1)
    with pytest.raises(airflow_dags.AirflowPlanError, match="SQL"):
        airflow_dags.artifact(plan)


@pytest.mark.parametrize("raw", ["", "broken json", "[]", "{}", '{"warehouse":"https://user:secret@db"}'])
def test_only_server_configured_connection_ids_are_used(plan, monkeypatch, raw):
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", raw)
    plan["tasks"][0]["conn_id"] = "model_invented_connection"
    with pytest.raises(airflow_dags.AirflowConfigurationError):
        airflow_dags.artifact(plan)


@pytest.mark.parametrize("location", ["top", "task"])
def test_reviewed_connection_mapping_cannot_silently_change(plan, location):
    if location == "top":
        plan["connection_id"] = "old_connection"
    else:
        plan["tasks"][0]["conn_id"] = "old_connection"
    with pytest.raises(airflow_dags.AirflowPlanError, match="changed"):
        airflow_dags.artifact(plan)


def test_approved_deployment_is_atomic_and_idempotent(plan, approval, tmp_path, monkeypatch):
    approver, calls = approval
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(tmp_path))
    compiled = airflow_dags.artifact(plan)
    first = airflow_dags.deploy(plan, approver=approver, expected_digest=compiled["digest"])
    inode = Path(first["path"]).stat().st_ino
    second = airflow_dags.deploy(plan, approver=approver, expected_digest=compiled["digest"])
    assert first == second
    assert first["deployed"] is True
    assert Path(first["path"]).read_text() == compiled["source"]
    assert Path(first["path"]).stat().st_ino == inode
    assert len(list(tmp_path.iterdir())) == 1
    assert len(calls) == 2
    assert calls[0][0] == approver


def test_concurrent_deployments_never_overwrite(plan, approval, tmp_path, monkeypatch):
    approver, _ = approval
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(tmp_path))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: airflow_dags.deploy(plan, approver=approver), range(8)))
    assert all(result == results[0] for result in results)
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("approver", [None, "admin", {}, {"id": "x", "role": "analyst"},
                                     {"id": "x", "role": "admin", "verified": False}])
def test_deployment_requires_authenticated_admin(plan, approver, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(tmp_path))
    with pytest.raises(airflow_dags.AirflowDeploymentError, match="administrator"):
        airflow_dags.deploy(plan, approver=approver)
    assert not list(tmp_path.iterdir())


def test_deployment_revalidates_and_refuses_changed_fingerprint(plan, approval, tmp_path, monkeypatch):
    approver, calls = approval
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(tmp_path))
    reviewed = airflow_dags.artifact(plan)["digest"]
    plan["tasks"][1]["sql"] = "SELECT COUNT(*) FROM staged_sales"
    with pytest.raises(airflow_dags.AirflowDeploymentError, match="changed since review"):
        airflow_dags.deploy(plan, approver=approver, expected_digest=reviewed)
    assert len(calls) == 1
    assert not list(tmp_path.iterdir())


def test_policy_rejection_prevents_publication(plan, approval, tmp_path, monkeypatch):
    import app.pipeline_dags as policy
    approver, _ = approval
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(tmp_path))
    rejected = dict(plan, status="blocked", errors=["Denied table"])
    monkeypatch.setattr(policy, "validate", lambda *_: rejected)
    with pytest.raises(airflow_dags.AirflowPlanError, match="not ready"):
        airflow_dags.deploy(plan, approver=approver)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("setting", [None, "/", "relative/dir", "/missing-studio-dag-directory"])
def test_deployment_directory_must_be_explicit_existing_and_absolute(plan, approval, monkeypatch, setting):
    approver, _ = approval
    if setting is None:
        monkeypatch.delenv("STUDIO_AIRFLOW_DAGS_DIR", raising=False)
    else:
        monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", setting)
    with pytest.raises(airflow_dags.AirflowConfigurationError):
        airflow_dags.deploy(plan, approver=approver)


def test_symlink_directory_and_ancestor_are_rejected(plan, approval, tmp_path, monkeypatch):
    approver, _ = approval
    real = tmp_path / "real"
    real.mkdir()
    (real / "nested").mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    for directory in (link, link / "nested"):
        monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(directory))
        with pytest.raises(airflow_dags.AirflowConfigurationError, match="symlink"):
            airflow_dags.deploy(plan, approver=approver)
    assert list(real.iterdir()) == [real / "nested"]
    assert not list((real / "nested").iterdir())


def test_existing_different_file_is_preserved(plan, approval, tmp_path, monkeypatch):
    approver, _ = approval
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(tmp_path))
    filename = tmp_path / airflow_dags.artifact(plan)["filename"]
    filename.write_text("# Existing user DAG\n")
    with pytest.raises(airflow_dags.AirflowDeploymentError, match="overwrite"):
        airflow_dags.deploy(plan, approver=approver)
    assert filename.read_text() == "# Existing user DAG\n"
    assert list(tmp_path.iterdir()) == [filename]


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_nonregular_final_path_is_rejected_without_following(plan, approval, tmp_path, monkeypatch, kind):
    approver, _ = approval
    directory = tmp_path / "dags"
    directory.mkdir()
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(directory))
    filename = directory / airflow_dags.artifact(plan)["filename"]
    outside = tmp_path / "unrelated.txt"
    outside.write_text("preserve")
    if kind == "symlink":
        filename.symlink_to(outside)
    elif kind == "directory":
        filename.mkdir()
    else:
        os.mkfifo(filename)
    with pytest.raises(airflow_dags.AirflowDeploymentError):
        airflow_dags.deploy(plan, approver=approver)
    assert outside.read_text() == "preserve"
    assert list(directory.iterdir()) == [filename]
