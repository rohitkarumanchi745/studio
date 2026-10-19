"""Autopilot from chat: ask for it in words, see its results in the chat.

Proves:
  - the parser reads a schedule, a threshold or new data out of a prompt, and
    leaves ordinary questions alone ("show daily revenue", "orders each day");
  - a chat turn with such a prompt is a PROPOSED agent (a card): nothing runs,
    nothing is created; the D3 & artifacts window proposes a visual agent;
    Auto fills in the source;
  - an agent made from a chat posts every run there — the answer with its
    rows and charts, or why it could not run — and marks the chat unread
    unless its owner started the run; a visual agent's answer is drawn with
    D3; nobody can point an agent at a chat they cannot write in;
  - migration 24 adds the columns.

Run from the backend directory:
    python -m pytest tests/test_autopilot_chat.py -q
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from app.autopilot_intent import parse

warnings.filterwarnings("ignore")


@pytest.mark.parametrize("prompt, trigger, config, goal", [
    ("Every day show revenue by region", "schedule", {"interval_seconds": 86400},
     "Show revenue by region"),
    ("send me a revenue by region report every Monday", "schedule",
     {"interval_seconds": 604800}, "Summarize revenue by region"),
    ("show me revenue every morning", "schedule", {"interval_seconds": 86400}, "Revenue"),
    ("every 30 minutes check inventory below reorder level", "schedule",
     {"interval_seconds": 900}, "Check inventory below reorder level"),
    ("alert me when revenue drops below 1M", "threshold",
     {"metric_prompt": "revenue", "op": "lt", "value": 1e6, "interval_seconds": 300},
     "Report revenue and explain what changed"),
    ("Notify me if daily signups goes above 2,500 and explain why", "threshold",
     {"metric_prompt": "daily signups", "op": "gt", "value": 2500.0, "interval_seconds": 300},
     "Report daily signups and explain why"),
    ("let me know when churn rate exceeds 5%", "threshold",
     {"metric_prompt": "churn rate", "op": "gt", "value": 5.0, "interval_seconds": 300},
     "Report churn rate and explain what changed"),
    ("whenever web_traffic gets new rows, summarize visits by page", "event",
     {"table": "web_traffic", "interval_seconds": 300}, "Summarize visits by page"),
])
def test_an_automation_request_is_read_out_of_a_prompt(prompt, trigger, config, goal):
    got = parse(prompt)
    assert got and got["trigger_type"] == trigger
    assert got["trigger_config"] == config and got["goal"] == goal


@pytest.mark.parametrize("prompt", [
    "revenue by region", "show daily revenue", "show revenue hourly", "how many orders each day",
    "show orders each day", "what was revenue every day last week?",
    "daily active users by country", "monitor daily revenue by store?",
    "which stores report weekly sales above target",
    "don't alert me when revenue drops below 5", "revenue below 1M by region",
    "what was in the revenue report every day last week?"])
def test_an_ordinary_question_is_not_an_automation_request(prompt):
    assert parse(prompt) is None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    monkeypatch.setenv("STUDIO_DEMO_MODE", "1")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL",
              "SMTP_HOST", "HARRIER_EMBED_URL"):
        monkeypatch.delenv(k, raising=False)
    import importlib

    from app import db as _db
    importlib.reload(_db)
    import app.main as main
    importlib.reload(main)
    from app import autopilot, db
    from app.connectors import demo
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    with TestClient(main.app) as c:
        def login(email, password):
            tok = c.post("/api/auth/login", json={"email": email, "password": password}
                         ).json()["access_token"]
            return {"Authorization": f"Bearer {tok}"}
        ns = type("Env", (), {})()
        ns.c, ns.ap, ns.db = c, autopilot, db
        ns.analyst = login("analyst@studio.local", "analyst123")
        ns.admin = login("admin@studio.local", "admin123")
        yield ns


def chat(env, prompt, headers=None, **kw):
    r = env.c.post("/api/chat", headers=headers or env.analyst,
                   json={"prompt": prompt, "source": "demo", "table": "*", **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_chat_request_is_a_proposed_agent_not_a_run(env, monkeypatch):
    from app import agent
    monkeypatch.setattr(agent, "run_agent", lambda **k: pytest.fail("a draft runs nothing"))
    out = chat(env, "alert me when revenue drops below 1M")
    msg = out["message"]
    assert msg["mode"] == "autopilot_draft" and msg["sql"] is None
    draft = msg["autopilot_draft"]
    assert draft["trigger_type"] == "threshold" and draft["source"] == "demo"
    assert draft["trigger_config"]["metric_prompt"] == "revenue" and draft["visual"] is False
    assert ("check revenue every 5 minutes and, when it drops below 1,000,000, answer “Report "
            "revenue and explain what changed”") in msg["text"]
    assert "posted to this chat" in msg["text"]
    assert env.c.get("/api/autopilot", headers=env.analyst).json()["agents"] == []
    # The D3 & artifacts window proposes an agent that draws with D3.
    d3 = chat(env, "every morning show me revenue by region", visual=True)["message"]
    assert d3["autopilot_draft"]["visual"] is True
    assert d3["autopilot_draft"]["trigger_config"] == {"interval_seconds": 86400}
    # With Auto on, the source comes from routing.
    auto = chat(env, "Every day show revenue by region", auto=True, source="*")["message"]
    assert auto["autopilot_draft"]["source"] == "demo"
    # An ordinary question is still answered.
    monkeypatch.undo()
    assert chat(env, "revenue by region")["message"]["mode"] != "autopilot_draft"


def test_an_agent_made_from_a_chat_posts_every_run_there(env):
    cid = chat(env, "Every day show revenue by region")["conversation_id"]
    r = env.c.post("/api/autopilot", headers=env.analyst, json={
        "name": "Daily revenue", "goal": "show revenue by region", "trigger_type": "schedule",
        "trigger_config": {"interval_seconds": 86400}, "source": "demo", "tables": ["sales"],
        "conversation_id": cid})
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["conversation_id"] == cid and agent["visual"] is False
    assert agent["conversation_title"]
    # A scheduled run: posted, and the chat is unread (until opened).
    run = env.ap._run_agent_now(env.ap._get_agent(agent["id"]), "schedule")
    convs = {c["id"]: c for c in env.c.get("/api/conversations", headers=env.analyst).json()}
    assert convs[cid]["unseen"] == 1
    msgs = env.c.get(f"/api/conversations/{cid}/messages", headers=env.analyst).json()
    posted = [m for m in msgs if m["role"] == "assistant" and m["content"].get("autopilot")]
    assert len(posted) == 1
    content = posted[0]["content"]
    assert content["autopilot"]["agent_name"] == "Daily revenue"
    assert content["autopilot"]["run_id"] == run["id"] and content["rows"] and content["columns"]
    assert content["source"] == "demo" and content["table"] == "sales"
    # Opening the chat (above) read it. Run now, started by its owner: posted,
    # but nothing new to tell them.
    env.c.post(f"/api/autopilot/{agent['id']}/run", headers=env.analyst)
    convs = {c["id"]: c for c in env.c.get("/api/conversations", headers=env.analyst).json()}
    assert convs[cid]["unseen"] == 0
    msgs = env.c.get(f"/api/conversations/{cid}/messages", headers=env.analyst).json()
    assert sum(1 for m in msgs if m["content"].get("autopilot")) == 2


def test_a_visual_agent_answers_with_d3(env):
    from app.agent import D3_CHART_TYPES
    cid = chat(env, "every morning show me revenue by region", visual=True)["conversation_id"]
    aid = env.c.post("/api/autopilot", headers=env.analyst, json={
        "name": "Revenue picture", "goal": "show revenue by region", "trigger_type": "manual",
        "source": "demo", "tables": ["sales"], "conversation_id": cid, "visual": True}).json()["id"]
    run = env.c.post(f"/api/autopilot/{aid}/run", headers=env.analyst).json()
    assert run["result"]["visual_mode"] is True
    msgs = env.c.get(f"/api/conversations/{cid}/messages", headers=env.analyst).json()
    content = [m for m in msgs if m["content"].get("autopilot")][-1]["content"]
    assert content["visual_mode"] is True
    charts = [content.get("chart")] + [p.get("chart") for p in content.get("panels") or []]
    assert all(c["type"] in D3_CHART_TYPES for c in charts if c)


def test_a_run_that_fails_says_so_in_the_chat(env):
    cid = chat(env, "alert me when revenue drops below 1M")["conversation_id"]
    aid = env.c.post("/api/autopilot", headers=env.analyst, json={
        "name": "Broken", "goal": "revenue", "trigger_type": "threshold", "source": "demo",
        "tables": ["sales"], "conversation_id": cid,
        "trigger_config": {"metric_prompt": "zorblax velocity", "op": "gt", "value": 0}}).json()["id"]
    env.ap._run_agent_now(env.ap._get_agent(aid), "threshold")
    msgs = env.c.get(f"/api/conversations/{cid}/messages", headers=env.analyst).json()
    note = [m for m in msgs if m["content"].get("autopilot")][-1]["content"]
    assert note["mode"] == "autopilot_failed"
    assert "Autopilot “Broken” couldn't run" in note["text"] and "zorblax velocity" in note["text"]


def test_nobody_points_an_agent_at_a_chat_they_cannot_write_in(env):
    theirs = chat(env, "revenue by region", headers=env.admin)["conversation_id"]
    r = env.c.post("/api/autopilot", headers=env.analyst, json={
        "name": "Sneaky", "goal": "revenue", "trigger_type": "manual", "source": "demo",
        "tables": ["sales"], "conversation_id": theirs})
    assert r.status_code == 404
    # A chat deleted later: the run stands, nothing is posted, nothing breaks.
    cid = chat(env, "revenue by region")["conversation_id"]
    aid = env.c.post("/api/autopilot", headers=env.analyst, json={
        "name": "Orphan", "goal": "revenue", "trigger_type": "manual", "source": "demo",
        "tables": ["sales"], "conversation_id": cid}).json()["id"]
    assert env.c.delete(f"/api/conversations/{cid}", headers=env.analyst).status_code in (200, 204)
    run = env.c.post(f"/api/autopilot/{aid}/run", headers=env.analyst).json()
    assert run["status"] == "done"


def test_migration_24_adds_the_chat_columns(env):
    from app import migrations
    with env.db.connect() as c:
        migrations._m24_autopilot_chat(c, False)
        assert env.db._has_column(c, "autopilot_agents", "conversation_id")
        assert env.db._has_column(c, "autopilot_agents", "visual")


def test_a_run_is_never_posted_into_a_chat_its_owner_cannot_write_in(env):
    from app import chat as chat_module
    analyst = {"id": env.c.get("/api/auth/me", headers=env.analyst).json()["id"]}
    theirs = chat(env, "revenue by region", headers=env.admin)["conversation_id"]
    before = len(env.db.list_messages(theirs))
    assert chat_module.deliver_autopilot(theirs, analyst, {"text": "x", "autopilot": {}}) is False
    assert len(env.db.list_messages(theirs)) == before
    mine = chat(env, "revenue by region")["conversation_id"]
    assert chat_module.deliver_autopilot(mine, analyst, {"text": "y", "autopilot": {}}) is True
