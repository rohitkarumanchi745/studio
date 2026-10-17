"""Read-only SQL pipelines were removed, but conversations still hold the drafts
they produced. Those stored recipes are history: they stay readable to whoever
may read every step today, stay hidden from everyone else, and can no longer
run. A new answer carries no pipeline draft at all."""
import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, chat, db, governance, jobs, keys, queries, stored_access
from app.auth import current_user
from app.connectors import demo


SALES_SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region"

# The shape a removed read-only pipeline left in a conversation.
STORED = {
    "text": "Pipeline ready: 1 verified read-only SQL step(s).", "mode": "pipeline",
    "model": None, "source": "demo", "table": "sales", "author_role": "viewer",
    "columns": [], "rows": [], "panels": [], "sql": None,
    "pipeline": {"execution_mode": "read_only_sql", "status": "ready", "source": "demo",
                 "prompt": "Revenue by region", "dropped": [],
                 "steps": [{"name": "Revenue by region", "source": "demo", "table": "sales",
                            "sql": SALES_SQL, "verified": True, "row_count": 4}]},
}


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
    for module in (chat, queries, governance, jobs, keys):
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
    return client.post("/api/chat", json={"prompt": prompt, "source": "demo", "table": "sales",
                                          "conversation_id": cid, **kwargs})


def conversation_with(client, stored):
    first = ask(client, "Revenue by region")
    assert first.status_code == 200, first.text
    cid = first.json()["conversation_id"]
    return cid, db.add_message(cid, "assistant", stored)


def test_a_new_answer_carries_no_pipeline_draft(client):
    response = ask(client, "Revenue by region")
    assert response.status_code == 200, response.text
    message = response.json()["message"]
    assert message["rows"]
    assert "pipeline" not in message


@pytest.mark.parametrize("extra", [
    {"pipeline_action": "build", "pipeline_mode": "read_only_sql"},
    {"pipeline_action": "run"},
])
def test_read_only_pipeline_requests_are_refused(client, extra):
    cid, mid = conversation_with(client, STORED)
    if extra["pipeline_action"] == "run":
        extra = {**extra, "pipeline_message_id": mid}
    before = len(db.list_messages(cid))
    response = ask(client, "Run this pipeline", cid, **extra)
    assert response.status_code == 400
    assert len(db.list_messages(cid)) == before      # nothing was recorded or run


def test_words_that_used_to_run_a_pipeline_are_just_a_question(client):
    cid, _ = conversation_with(client, STORED)
    response = ask(client, "run this pipeline", cid)
    assert response.status_code == 200, response.text
    message = response.json()["message"]
    assert message["mode"] == "agent"
    assert "pipeline" not in message


def test_stored_draft_stays_readable_without_contacting_the_live_catalog(client, monkeypatch):
    cid, _ = conversation_with(client, STORED)
    monkeypatch.setattr(demo.DemoConnector, "list_tables",
                        lambda *a: pytest.fail("History contacted live catalog"))
    messages = client.get(f"/api/conversations/{cid}/messages").json()
    assert messages[-1]["content"]["pipeline"]["steps"]
    assert not messages[-1]["content"].get("redacted")


@pytest.mark.parametrize("sql", ["SELECT * FROM customers",
                                 "SELECT * FROM secret_schema.sales"])
def test_stored_step_the_reader_cannot_see_hides_the_message(client, sql):
    restricted = copy.deepcopy(STORED)
    restricted["pipeline"]["steps"][0]["sql"] = sql
    cid, _ = conversation_with(client, restricted)
    visible = client.get(f"/api/conversations/{cid}/messages").json()
    assert visible[-1]["content"]["redacted"] is True
    assert "pipeline" not in visible[-1]["content"]


def test_inaccessible_dropped_diagnostics_are_redacted(client):
    blocked = copy.deepcopy(STORED)
    blocked["pipeline"]["status"] = "blocked"
    blocked["pipeline"]["dropped"] = [{"name": "Private customer record",
        "source": "demo", "table": "customers", "sql": "SELECT * FROM customers WHERE name='Private person'",
        "error": "Private person has invalid data", "verified": False}]
    cid, _ = conversation_with(client, blocked)
    response = client.get(f"/api/conversations/{cid}/messages")
    assert "Private person" not in response.text
    assert "Private customer" not in response.text
    current = response.json()[-1]["content"]
    assert not current.get("redacted")
    assert current["pipeline"]["steps"]
    assert "current data permissions" in current["pipeline"]["dropped"][0]["error"]


@pytest.mark.parametrize("dialect,sql", [("snowflake", "SELECT * FROM sales"),
                                       ("postgres", 'SELECT * FROM "SALES"')])
def test_stored_steps_match_a_case_insensitive_table_policy(monkeypatch, dialect, sql):
    from types import SimpleNamespace

    from app import connectors
    connector = SimpleNamespace(dialect=dialect, qualifiers=lambda: frozenset({"PUBLIC"}))
    monkeypatch.setattr(connectors, "get_connector", lambda source: connector)
    monkeypatch.setattr(stored_access.rbac, "_role_policy", lambda role, source: ["sales"])
    allowed = stored_access.stored_step_allowed
    assert allowed("viewer", {"source": "warehouse", "sql": sql})
    assert not allowed("viewer", {"source": "warehouse", "sql": "SELECT * FROM customers"})
    assert not allowed("viewer", {"source": "warehouse", "sql": "SELECT * FROM secret_schema.sales"})
