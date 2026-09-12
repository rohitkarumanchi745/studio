"""Chat uses visible, message-bound pipeline versions and the real query gate."""
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, chat, chat_pipelines, db, governance, jobs, keys, pipelines, queries
from app.auth import current_user
from app.connectors import demo


SALES_SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM_BASE_URL", "SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)
    db.init_db()
    for module in (chat, pipelines, queries, governance, jobs, keys):
        module.init_tables()
    demo.seed()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: False)
    monkeypatch.setattr(chat.semantic, "answer", lambda *a, **k: None)
    monkeypatch.setattr(chat.qcache, "lookup", lambda *a, **k: None)
    monkeypatch.setattr(chat.qcache, "store", lambda *a, **k: None)
    monkeypatch.setattr(chat.model_router, "choose", lambda *a, **k: ("frontier", None))
    monkeypatch.setattr(chat.skills, "get_skill", lambda *a, **k: "")
    monkeypatch.setattr(chat.lightning, "record_chat_trace", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(pipelines.email_service, "send", lambda *a, **k: pytest.fail("No email from a chat pipeline"))
    user = db.get_user_by_email("viewer@studio.local")

    def answer(**kwargs):
        result = queries.verify_sql(kwargs["user"], "demo", "sales", SALES_SQL)
        return {"text": "Revenue by region", "sql": result["sql"],
                "rows": result["rows"], "columns": result["columns"],
                "panels": [], "mode": "agent", "errors": [], "model": "test"}

    monkeypatch.setattr(agent, "run_agent", answer)
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as c:
        c.user = user
        yield c
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)


def ask(client, prompt, cid=None, **kwargs):
    response = client.post("/api/chat", json={"prompt": prompt, "source": "demo", "table": "sales",
                                            "conversation_id": cid, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def count(table):
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


def latest_assistant(cid):
    return [m for m in db.list_messages(cid) if m["role"] == "assistant"][-1]


def test_data_answer_has_pipeline_and_followup_runs_real_sql(client):
    answer = ask(client, "Show revenue by region")
    draft = answer["message"]["pipeline"]
    assert draft["status"] == "ready"
    assert "GROUP BY region" in draft["steps"][0]["sql"]
    assert "rows" not in draft["steps"][0]
    assert count("pipelines") == 0
    run = ask(client, "Run this pipeline", answer["conversation_id"])["message"]
    assert run["mode"] == "pipeline"
    assert run["pipeline"]["run"]["status"] == "success"
    assert run["pipeline"]["run"]["steps_result"][0]["row_count"] == 4
    assert count("pipelines") == count("pipeline_runs") == 1


def test_build_from_prompt_respects_selected_table(client):
    answer = ask(client, "Build a pipeline for monthly revenue by region")
    draft = answer["message"]["pipeline"]
    assert draft["source"] == "demo" and draft["steps"]
    assert {step["table"] for step in draft["steps"]} == {"sales"}
    assert "strftime('%Y-%m'" in draft["steps"][0]["sql"]
    assert count("pipeline_runs") == 0


def test_build_and_run_in_one_chat_prompt(client):
    answer = ask(client, "Build and run a pipeline for monthly revenue by region")
    draft = answer["message"]["pipeline"]
    assert draft["run"]["status"] == "success"
    assert "strftime('%Y-%m'" in draft["steps"][0]["sql"]
    assert count("pipelines") == count("pipeline_runs") == 1


def test_followup_builder_receives_prior_recipe_and_conversation(client, monkeypatch):
    first = ask(client, "Revenue by region for the quarter")
    seen = {}

    def build(user, prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return copy.deepcopy(first["message"]["pipeline"])

    monkeypatch.setattr(chat_pipelines, "build", build)
    ask(client, "Make it monthly", first["conversation_id"], pipeline_action="build")
    assert seen["source"] == "demo" and seen["tables"] == ["sales"]
    assert seen["previous"]["steps"] == first["message"]["pipeline"]["steps"]
    history = json.dumps(seen["context"])
    assert "Revenue by region for the quarter" in history and "GROUP BY region" in history
    assert "Make it monthly" not in history


def test_run_button_selects_exact_older_message(client):
    first = ask(client, "Revenue by region")
    cid = first["conversation_id"]
    mid = latest_assistant(cid)["id"]
    other = copy.deepcopy(first["message"])
    other["pipeline"]["steps"][0]["sql"] = "SELECT COUNT(*) AS total FROM sales"
    db.add_message(cid, "assistant", other)
    result = ask(client, "Run this pipeline", cid, pipeline_action="run", pipeline_message_id=mid)
    assert "GROUP BY region" in result["message"]["pipeline"]["run"]["steps_result"][0]["sql"]


def test_pipeline_version_cannot_come_from_another_conversation(client):
    first = ask(client, "Revenue by region")
    mid = latest_assistant(first["conversation_id"])["id"]
    second = ask(client, "Sales by region")
    response = client.post("/api/chat", json={"prompt": "Run this pipeline", "source": "demo",
        "table": "sales", "conversation_id": second["conversation_id"],
        "pipeline_action": "run", "pipeline_message_id": mid})
    assert response.status_code == 404
    assert count("pipeline_runs") == 0


def test_run_without_pipeline_explains_missing_context(client):
    response = ask(client, "Run this pipeline")
    assert "no runnable pipeline" in response["message"]["text"]
    assert count("pipeline_runs") == 0


def test_questions_about_pipelines_are_not_run_commands(client, monkeypatch):
    first = ask(client, "Revenue by region")
    monkeypatch.setattr(chat_pipelines, "run", lambda *a, **k: pytest.fail("Unexpected execution"))
    result = ask(client, "How does this pipeline run?", first["conversation_id"])
    assert result["message"]["mode"] == "agent"


def test_queued_turn_uses_history_before_its_own_message(client):
    first = ask(client, "Revenue by region")
    cid = first["conversation_id"]
    body = chat.Ask(prompt="Run this pipeline", source="demo", table="sales", conversation_id=cid)
    _, mid = chat._record_user_turn(body, client.user)
    later = copy.deepcopy(first["message"])
    later["text"] = "Later version which this queued turn must not see"
    later["pipeline"]["steps"][0]["sql"] = "SELECT COUNT(*) AS total FROM sales"
    db.add_message(cid, "assistant", later)
    db.add_message(cid, "user", {"text": "An even later question"})
    ctx = chat._build_ctx(body, client.user, cid, user_message_id=mid)
    assert "Later version" not in json.dumps(ctx["history"])
    assert "An even later question" not in json.dumps(ctx["history"])
    assert "Run this pipeline" not in json.dumps(ctx["history"])
    assert "GROUP BY region" in chat._pipeline_context(ctx, client.user)["steps"][0]["sql"]


def test_retry_after_run_before_answer_does_not_duplicate_records(client, monkeypatch):
    first = ask(client, "Revenue by region")
    cid = first["conversation_id"]
    queued = client.post("/api/chat/background", json={"prompt": "Run this pipeline", "source": "demo",
        "table": "sales", "conversation_id": cid}).json()
    job = jobs.get(chat._task_job_id(queued["task_id"]))
    original = chat._answer
    monkeypatch.setattr(chat, "_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash after run")))
    with pytest.raises(RuntimeError, match="crash after run"):
        chat._chat_turn_job(job["payload"], {**job, "attempts": 1})
    assert count("pipelines") == count("pipeline_runs") == 1
    monkeypatch.setattr(chat, "_answer", original)
    chat._chat_turn_job(job["payload"], {**job, "attempts": 2})
    assert count("pipelines") == count("pipeline_runs") == 1
    assert latest_assistant(cid)["content"]["pipeline"]["run"]["status"] == "success"


def test_build_and_run_retry_recovers_recipe_without_replanning(client, monkeypatch):
    queued = client.post("/api/chat/background", json={
        "prompt": "Build and run a pipeline for monthly revenue by region",
        "source": "demo", "table": "sales"}).json()
    job = jobs.get(chat._task_job_id(queued["task_id"]))
    original = chat._answer
    monkeypatch.setattr(chat, "_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash after run")))
    with pytest.raises(RuntimeError, match="crash after run"):
        chat._chat_turn_job(job["payload"], {**job, "attempts": 1})
    monkeypatch.setattr(chat, "_answer", original)
    monkeypatch.setattr(chat_pipelines, "build", lambda *a, **k: pytest.fail("Completed recipe was replanned"))
    chat._chat_turn_job(job["payload"], {**job, "attempts": 2})
    assert count("pipelines") == count("pipeline_runs") == 1
    assert latest_assistant(queued["conversation_id"])["content"]["pipeline"]["run"]["status"] == "success"


@pytest.mark.parametrize("sql", ["SELECT name FROM customers", "SELECT * FROM secret_schema.sales"])
def test_nested_pipeline_sql_is_redacted_and_cannot_be_run(client, sql):
    first = ask(client, "Revenue by region")
    cid = first["conversation_id"]
    restricted = copy.deepcopy(first["message"])
    restricted.update(sql=None, panels=[], rows=[], columns=[], author_role="viewer", table="sales")
    restricted["pipeline"]["steps"][0]["sql"] = sql
    mid = db.add_message(cid, "assistant", restricted)
    visible = client.get(f"/api/conversations/{cid}/messages").json()
    assert visible[-1]["content"]["redacted"] is True
    assert "pipeline" not in visible[-1]["content"]
    response = client.post("/api/chat", json={"prompt": "Run this pipeline", "source": "demo",
        "table": "sales", "conversation_id": cid, "pipeline_action": "run", "pipeline_message_id": mid})
    assert response.status_code == 403
    assert count("pipeline_runs") == 0


def test_view_only_collaborator_cannot_run_pipeline(client):
    first = ask(client, "Revenue by region")
    cid = first["conversation_id"]
    recipient = db.get_user_by_email("analyst@studio.local")
    db.share_conversation(cid, recipient["id"], "view")
    client.app.dependency_overrides[current_user] = lambda: recipient
    response = client.post("/api/chat", json={"prompt": "Run this pipeline", "source": "demo",
        "table": "sales", "conversation_id": cid})
    assert response.status_code == 403
    assert count("pipeline_runs") == 0


def test_latest_blocked_revision_does_not_run_older_ready_draft(client):
    first = ask(client, "Revenue by region")
    cid = first["conversation_id"]
    blocked = copy.deepcopy(first["message"])
    blocked["pipeline"].update(status="blocked", steps=[], dropped=[{"error": "No valid SQL"}])
    db.add_message(cid, "assistant", blocked)
    result = client.post("/api/chat", json={"prompt": "Run this pipeline", "source": "demo",
        "table": "sales", "conversation_id": cid})
    assert result.status_code in (200, 400, 422)
    assert count("pipeline_runs") == 0


def test_pipeline_history_does_not_contact_live_catalog(client, monkeypatch):
    first = ask(client, "Revenue by region")
    monkeypatch.setattr(demo.DemoConnector, "list_tables", lambda *a: pytest.fail("History contacted live catalog"))
    messages = client.get(f"/api/conversations/{first['conversation_id']}/messages").json()
    assert messages[-1]["content"]["pipeline"]["steps"]
    assert not messages[-1]["content"].get("redacted")


def test_inaccessible_dropped_diagnostics_are_redacted(client):
    first = ask(client, "Revenue by region")
    cid = first["conversation_id"]
    blocked = copy.deepcopy(first["message"])
    blocked["pipeline"]["status"] = "blocked"
    blocked["pipeline"]["dropped"] = [{"name": "Private customer record",
        "source": "demo", "table": "customers", "sql": "SELECT * FROM customers WHERE name='Private person'",
        "error": "Private person has invalid data", "verified": False}]
    db.add_message(cid, "assistant", blocked)
    response = client.get(f"/api/conversations/{cid}/messages")
    assert "Private person" not in response.text
    assert "Private customer" not in response.text
    current = response.json()[-1]["content"]
    assert not current.get("redacted")
    assert current["pipeline"]["steps"]
    assert "current data permissions" in current["pipeline"]["dropped"][0]["error"]


@pytest.mark.parametrize("dialect,sql", [("snowflake", "SELECT * FROM sales"),
                                       ("postgres", 'SELECT * FROM "SALES"')])
def test_pipeline_history_matches_case_insensitive_table_policy(client, monkeypatch, dialect, sql):
    from app import connectors
    connector = SimpleNamespace(dialect=dialect, qualifiers=lambda: frozenset({"PUBLIC"}))
    monkeypatch.setattr(connectors, "get_connector", lambda source: connector)
    monkeypatch.setattr(chat.rbac, "_role_policy", lambda role, source: ["sales"])
    assert chat._stored_pipeline_step_allowed("viewer", {"source": "warehouse", "sql": sql})
    assert not chat._stored_pipeline_step_allowed("viewer", {"source": "warehouse", "sql": "SELECT * FROM customers"})
    assert not chat._stored_pipeline_step_allowed("viewer", {"source": "warehouse", "sql": "SELECT * FROM secret_schema.sales"})
