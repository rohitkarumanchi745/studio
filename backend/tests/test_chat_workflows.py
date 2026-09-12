"""Real chat planning/submission with disposable DBs and stub model/catalog."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, chat, chat_workflows, db, governance, jobs, keys, lightning, pipeline_dags, supervisor
from app.auth import current_user


PROMPT = "Build an Airflow pipeline to create daily_sales from sales then summarize revenue"
PLAN = {"version": 1, "name": "Daily sales", "dag_id": "daily_sales", "source": "demo", "schedule": None,
        "parameters": {}, "tasks": [
            {"id": "extract", "sql": "CREATE TABLE daily_sales AS SELECT * FROM sales", "produces": "daily_sales", "depends_on": []},
            {"id": "total", "sql": "SELECT SUM(revenue) FROM daily_sales", "depends_on": ["extract"]}]}


class Catalog:
    name = "demo"
    dialect = "sqlite"
    configured = lambda self: True
    qualifiers = lambda self: frozenset({"main"})
    list_tables = lambda self: ["sales", "web_traffic"]
    get_schema = lambda self, table: [{"name": "revenue", "type": "REAL"}, {"name": "region", "type": "TEXT"}]
    run_query = lambda *a, **k: pytest.fail("Planning must not run SQL")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "chat-workflows.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.delenv("STUDIO_AGL_URL", raising=False)
    monkeypatch.setenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", '{"demo":"warehouse_demo"}')
    dag_dir = tmp_path / "dags"
    dag_dir.mkdir()
    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(dag_dir))
    db.init_db()
    for module in (chat, jobs, keys, supervisor, governance):
        module.init_tables()
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0, ident=None)
    monkeypatch.setattr(pipeline_dags, "get_connector", lambda source: Catalog())
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    requests = []

    def invoke(messages):
        requests.append(messages)
        return SimpleNamespace(content=json.dumps(copy.deepcopy(PLAN)))

    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: SimpleNamespace(invoke=invoke))
    monkeypatch.setattr(supervisor, "_llm_review", lambda *a, **k: [])
    monkeypatch.setattr(supervisor, "_email", lambda *a, **k: pytest.fail("Chat must not email"))
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "configured", lambda: True)
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "trigger", lambda *a, **k: pytest.fail("Chat cannot trigger before approval"))
    monkeypatch.setattr(chat, "_checkpoint", lambda *a, **k: None)
    user = db.get_user_by_email("analyst@studio.local")
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as c:
        c.user, c.requests, c.dag_dir = user, requests, dag_dir
        yield c
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0, ident=None)


def ask(client, prompt=PROMPT, cid=None, **fields):
    response = client.post("/api/chat", json={"prompt": prompt, "source": "demo", "table": "*",
        "conversation_id": cid, **fields})
    assert response.status_code == 200, response.text
    return response.json()


def message_id(cid):
    return [m for m in db.list_messages(cid) if m["role"] == "assistant"][-1]["id"]


def test_prompt_builds_real_dependency_plan_and_download_without_execution(client):
    plan = ask(client)["message"]["pipeline"]
    assert plan["status"] == "ready", plan
    assert plan["tasks"][1]["depends_on"] == ["extract"]
    assert "SQLExecuteQueryOperator" in plan["artifact"]["source"]
    assert not list(client.dag_dir.iterdir())
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 0


def test_natural_followup_submits_exact_plan_for_approval_only(client):
    first = ask(client)
    plan = ask(client, "Run this pipeline", first["conversation_id"])["message"]["pipeline"]
    assert plan["status"] == "awaiting_approval", plan
    row = supervisor._get(plan["job_id"])
    assert row["kind"] == "airflow_dag" and row["human_by"] is None
    assert json.loads(row["script"])["plan"]["tasks"][1]["depends_on"] == ["extract"]
    assert not list(client.dag_dir.iterdir())


def test_exact_historical_success_revalidated_without_model(client, monkeypatch):
    first = ask(client)["message"]["pipeline"]
    tid = lightning.record_pipeline_outcome(client.user, run_id="successful-dag", prompt=PROMPT,
        source="demo", action={"type": "airflow_dag", "plan": first}, status="succeeded")
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: pytest.fail("Exact memory must not call model"))
    second = ask(client)["message"]["pipeline"]
    assert second["status"] == "ready", second
    assert second["memory"]["trace_id"] == tid
    assert second["memory"]["reuse_type"] == "exact_revalidated"


def test_similar_request_reaches_model_as_adaptation_not_blind_reuse(client):
    first = ask(client)["message"]["pipeline"]
    lightning.record_pipeline_outcome(client.user, run_id="successful-dag", prompt=PROMPT,
        source="demo", action={"type": "airflow_dag", "plan": first}, status="succeeded")
    result = ask(client, PROMPT + " for the North region")["message"]["pipeline"]
    payload = json.loads(client.requests[-1][1][1])
    assert "North" in payload["request"] and payload["historical_examples"]
    assert result["status"] == "needs_input"
    assert result["memory"]["reuse_type"] == "adaptation_unconfirmed"


def test_missing_model_does_not_invent_etl(client, monkeypatch):
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: False)
    plan = ask(client, "Load yesterday sales, remove duplicates and update revenue table")["message"]["pipeline"]
    assert plan["status"] == "needs_input" and not plan["tasks"]
    assert not list(client.dag_dir.iterdir())


def test_build_and_submit_retry_recovers_recipe_without_regeneration(client, monkeypatch):
    response = client.post("/api/chat/background", json={"prompt": PROMPT.replace("Build an", "Build and run an"), "source": "demo", "table": "*"})
    assert response.status_code == 202, response.text
    queued = response.json()
    job = jobs.get(chat._task_job_id(queued["task_id"]))
    original = chat._answer
    monkeypatch.setattr(chat, "_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("answer interrupted")))
    with pytest.raises(RuntimeError, match="answer interrupted"):
        chat._chat_turn_job(job["payload"], {**job, "attempts": 1})
    monkeypatch.setattr(chat, "_answer", original)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: pytest.fail("Retry must recover, not regenerate"))
    chat._chat_turn_job(job["payload"], {**job, "attempts": 2})
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 1


def test_shared_editor_cannot_see_dag_prompt_or_recipe(client):
    first = ask(client, PROMPT + " for private_parameter")
    uid = db.create_user("other-dag@test.invalid", "password-for-test", "Other", role="analyst")
    other = db.get_user(uid)
    db.share_conversation(first["conversation_id"], uid, "edit")
    client.app.dependency_overrides[current_user] = lambda: other
    response = client.get(f"/api/conversations/{first['conversation_id']}/messages")
    assert response.status_code == 200
    assert all(m["content"].get("redacted") for m in response.json())
    assert "private_parameter" not in response.text and "CREATE TABLE" not in response.text


def test_explicit_status_never_submits_another_dag(client):
    first = ask(client)
    cid = first["conversation_id"]
    submitted = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    answer = ask(client, "Check pipeline status", cid, pipeline_action="status", pipeline_message_id=message_id(cid))
    assert answer["message"]["pipeline"]["job_id"] == submitted["job_id"]
    assert answer["message"]["pipeline"]["status"] == "awaiting_approval"


def _failed_dag(client):
    """Persist a simulated terminal observation; never execute the platform."""
    first = ask(client)
    cid = first["conversation_id"]
    submitted = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    row = supervisor._get(submitted["job_id"])
    result = json.loads(row.get("result") or "{}")
    result.update(run_ref="daily_sales:original_run", state="failed",
                  detail="Aggregate expression failed; correct the SQL")
    supervisor._save(row, status="failed", human_by="test-admin-approval",
                     last_error=result["detail"], result=json.dumps(result))
    return cid, submitted["job_id"], f"{submitted['job_id']}:{result['run_ref']}"


def _model_response(monkeypatch, client, response):
    def invoke(messages):
        client.requests.append(messages)
        return SimpleNamespace(content=json.dumps(copy.deepcopy(response)))
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: SimpleNamespace(invoke=invoke))


def test_failed_job_repair_preserves_original_objective_and_learning_link(client, monkeypatch):
    cid, failed_job_id, failed_run_id = _failed_dag(client)
    corrected = copy.deepcopy(PLAN)
    corrected["tasks"][1]["sql"] = "SELECT COALESCE(SUM(revenue), 0) FROM daily_sales"
    _model_response(monkeypatch, client, corrected)

    repaired = ask(client, "Fix the failed aggregate expression", cid)["message"]["pipeline"]
    assert repaired["status"] == "ready", repaired
    assert repaired["prompt"] == PROMPT
    assert repaired["revision_prompt"] == "Fix the failed aggregate expression"
    assert repaired["repairs_run_id"] == failed_run_id
    payload = json.loads(client.requests[-1][1][1])
    assert payload["previous_plan"]["approved"] is True
    assert payload["previous_plan"]["status"] == "failed"
    assert payload["previous_plan"]["failure"]["run_id"] == failed_run_id
    assert "Aggregate expression failed" in payload["previous_plan"]["failure"]["error"]

    submitted = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    assert submitted["status"] == "awaiting_approval"
    assert submitted["job_id"] != failed_job_id
    job = supervisor._get(submitted["job_id"])
    assert job["human_by"] is None
    learning = json.loads(job["result"])["studio"]
    assert learning["prompt"] == PROMPT
    assert learning["repairs_run_id"] == failed_run_id
    assert not list(client.dag_dir.iterdir())


def test_copying_failed_sql_is_not_presented_as_a_correction(client):
    cid, _failed_job_id, failed_run_id = _failed_dag(client)
    repaired = ask(client, "Repair the failed aggregate expression", cid)["message"]["pipeline"]
    assert repaired["status"] == "needs_input", repaired
    assert any("did not change the failed SQL" in question for question in repaired["missing"])
    assert repaired["prompt"] == PROMPT
    assert repaired["repairs_run_id"] == failed_run_id
    assert "artifact" not in repaired
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 1
    assert not list(client.dag_dir.iterdir())


def test_task_renaming_does_not_prove_adaptation_to_changed_region(client, monkeypatch):
    first = ask(client)["message"]["pipeline"]
    tid = lightning.record_pipeline_outcome(client.user, run_id="successful-before-region-change",
        prompt=PROMPT, source="demo", action={"type": "airflow_dag", "plan": first}, status="succeeded")
    renamed = copy.deepcopy(PLAN)
    renamed["tasks"][0]["id"] = "new_extract_name"
    renamed["tasks"][1].update(id="new_total_name", depends_on=["new_extract_name"])
    _model_response(monkeypatch, client, renamed)

    adapted = ask(client, PROMPT + " for the North region")["message"]["pipeline"]
    assert adapted["status"] == "needs_input", adapted
    assert adapted["memory"]["trace_id"] == tid
    assert adapted["memory"]["reuse_type"] == "adaptation_unconfirmed"
    assert any("previous SQL" in question for question in adapted["missing"])
    assert "artifact" not in adapted
    assert not list(client.dag_dir.iterdir())


@pytest.mark.parametrize("history_role", ["user", "assistant"])
def test_chat_history_cannot_authorize_a_new_output_destination(client, monkeypatch, history_role):
    first = ask(client)
    cid = first["conversation_id"]
    db.add_message(cid, history_role, {
        "text": "An earlier example mentioned invented_output; that is not a destination approved for a new pipeline.",
        "author_role": "analyst", "source": "demo"})
    invented = copy.deepcopy(PLAN)
    invented["tasks"][0].update(sql="CREATE TABLE invented_output AS SELECT * FROM sales", produces="invented_output")
    invented["tasks"][1]["sql"] = "SELECT SUM(revenue) FROM invented_output"
    _model_response(monkeypatch, client, invented)
    request = "Create an Airflow pipeline to prepare sales data"

    result = ask(client, request, cid)["message"]["pipeline"]
    payload = json.loads(client.requests[-1][1][1])
    assert payload["request"] == request
    assert "invented_output" in json.dumps(payload["conversation_context"])
    assert result["status"] == "needs_input", result
    assert any("Confirm the output table 'invented_output'" in question for question in result["missing"])
    assert result["confirmed_outputs"] == []
    assert "artifact" not in result
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 0
    assert not list(client.dag_dir.iterdir())


@pytest.mark.parametrize("missing_setting", ["dag_directory", "credentials"])
def test_missing_deployment_configuration_can_be_fixed_then_resubmitted(client, monkeypatch, missing_setting):
    first = ask(client)
    cid = first["conversation_id"]
    airflow = supervisor.platforms.PLATFORMS["airflow"]
    if missing_setting == "dag_directory":
        monkeypatch.delenv("STUDIO_AIRFLOW_DAGS_DIR")
    else:
        monkeypatch.setattr(airflow, "configured", lambda: False)

    blocked = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    assert blocked["status"] == "needs_configuration", blocked
    assert blocked["artifact"]["source"]
    assert "job_id" not in blocked
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 0

    monkeypatch.setenv("STUDIO_AIRFLOW_DAGS_DIR", str(client.dag_dir))
    monkeypatch.setattr(airflow, "configured", lambda: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: pytest.fail("Configuration retry must reuse the reviewed plan"))
    submitted = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    assert submitted["status"] == "awaiting_approval", submitted
    assert submitted["missing"] == []
    row = supervisor._get(submitted["job_id"])
    assert row["human_by"] is None
    assert json.loads(row["script"])["plan"]["tasks"] == first["message"]["pipeline"]["tasks"]
    assert not list(client.dag_dir.iterdir())


def test_no_overwrite_safety_constraint_still_routes_to_dag_planning(client):
    request = PROMPT + ". Do not overwrite or append to any existing table."
    result = ask(client, request)["message"]["pipeline"]
    assert result["execution_mode"] == "airflow_dag"
    assert result["status"] == "ready", result
    assert result["prompt"] == request
    assert all(task["kind"] != "insert_select" for task in result["tasks"])
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 0
    assert not list(client.dag_dir.iterdir())


@pytest.mark.parametrize("reply", [
    "Use daily_sales as the output", "Use order_id as key and keep latest updated_at",
    "Keep the latest updated_at", "Retain the earliest created_at", "Deduplicate sales by order_id",
    "Append to daily_sales", "Create output table daily_sales", "Create daily_sales as the destination",
    "Use full-row DISTINCT",
])
def test_explicit_clarifications_route_only_while_waiting_for_details(reply):
    pending = {"execution_mode": "airflow_dag", "status": "needs_input"}
    assert chat_workflows.intent(reply, pending) == "build"
    assert chat_workflows.intent(reply, {**pending, "status": "ready"}) is None
    assert chat_workflows.intent(reply) is None


@pytest.mark.parametrize("reply", ["Thanks", "What is a DAG?", "Use blue as the chart color", "Keep explaining", "Create a chart", "Do not create a pipeline"])
def test_unrelated_messages_do_not_become_clarification_builds(reply):
    pending = {"execution_mode": "airflow_dag", "status": "needs_input"}
    assert chat_workflows.intent(reply, pending) is None


def test_output_clarification_rebuilds_real_chat_plan_and_preserves_objective(client):
    objective = "Build an Airflow pipeline to prepare sales data"
    first = ask(client, objective)
    assert first["message"]["pipeline"]["status"] == "needs_input"
    assert first["message"]["pipeline"]["confirmed_outputs"] == []
    clarification = "Use daily_sales as the output"
    result = ask(client, clarification, first["conversation_id"])["message"]["pipeline"]
    assert result["status"] == "ready", result
    assert result["confirmed_outputs"] == ["daily_sales"]
    assert result["prompt"] == objective + " Clarification: " + clarification
    assert result["revision_prompt"] == clarification
    payload = json.loads(client.requests[-1][1][1])
    assert payload["request"] == clarification
    assert payload["previous_plan"]["prompt"] == objective
    submitted = ask(client, "Run this pipeline", first["conversation_id"])["message"]["pipeline"]
    learning = json.loads(supervisor._get(submitted["job_id"])["result"])["studio"]
    assert learning["prompt"] == result["prompt"]
    assert submitted["status"] == "awaiting_approval"
    assert not list(client.dag_dir.iterdir())


def test_key_clarification_routes_without_approving_an_unconfirmed_destination(client):
    objective = "Build an Airflow pipeline to deduplicate sales and summarize revenue"
    first = ask(client, objective)
    assert first["message"]["pipeline"]["status"] == "needs_input"
    assert "Which columns identify duplicates" in first["message"]["pipeline"]["missing"][0]
    clarification = "Use order_id as key and keep latest updated_at"
    result = ask(client, clarification, first["conversation_id"])["message"]["pipeline"]
    assert result["execution_mode"] == "airflow_dag"
    assert result["status"] == "needs_input", result
    assert result["prompt"] == objective + " Clarification: " + clarification
    assert any("Confirm the output table 'daily_sales'" in question for question in result["missing"])
    assert result["confirmed_outputs"] == []
    payload = json.loads(client.requests[-1][1][1])
    assert payload["request"] == clarification
    assert payload["previous_plan"]["prompt"] == objective
    final = ask(client, "Use daily_sales as the output", first["conversation_id"])["message"]["pipeline"]
    assert final["status"] == "ready", final
    assert final["prompt"].startswith(objective)
    assert clarification in final["prompt"]
    assert final["prompt"].endswith("Clarification: Use daily_sales as the output")
    assert not list(client.dag_dir.iterdir())


def test_repeated_run_command_observes_existing_pending_or_running_job(client, monkeypatch):
    first = ask(client)
    cid = first["conversation_id"]
    submitted = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    job_id = submitted["job_id"]
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: pytest.fail("Repeating run must not replan"))
    monkeypatch.setattr(supervisor, "submit", lambda *a, **k: pytest.fail("Repeating run must not create a second approval"))
    repeated = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    assert repeated["job_id"] == job_id
    assert repeated["status"] == "awaiting_approval"

    # The visible card still says pending; the next reply reads current local
    # job state rather than creating another request from its stale snapshot.
    supervisor._save(supervisor._get(job_id), status="running")
    running = ask(client, "Run this pipeline", cid)["message"]["pipeline"]
    assert running["job_id"] == job_id
    assert running["status"] == "running"
    assert running["job"]["status"] == "running"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"] == 1
    assert not list(client.dag_dir.iterdir())
