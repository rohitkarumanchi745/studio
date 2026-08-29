"""Same-named tables across sources — ask, don't guess.

Proves: ambiguous_tables flags a table the prompt names only when it exists
in more than one accessible source (whole-phrase match for multi-word names,
word/plural match for one-word names); an "All sources" turn that hits one
records a clarify message with one option per source plus "both" and runs NO
agent; allow_ambiguous (the user chose "both") runs the orchestrator with the
already-built roster; an unambiguous prompt runs straight through.

Run from the backend directory:
    python -m pytest tests/test_clarify.py -q
"""
import os
import tempfile
import time
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="studio-clarify-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import chat, db, orchestrator

ANA = {"id": "u-ana", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}


def _src(name, tables):
    return {"connector": SimpleNamespace(name=name, dialect="sqlite"),
            "allowed": tables, "schemas": {}, "skill": ""}


SOURCES = [_src("demo", ["sales", "customers", "ecommerce_orders"]),
           _src("snowflake", ["sales", "ecommerce_orders", "inventory"])]


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    yield


def test_ambiguous_tables_detection():
    amb = orchestrator.ambiguous_tables
    assert amb("show sales by region", SOURCES) == [{"table": "sales", "sources": ["demo", "snowflake"]}]
    assert amb("SALES per month", SOURCES) == [{"table": "sales", "sources": ["demo", "snowflake"]}]
    assert amb("top customers", SOURCES) == []                     # one source only → no clash
    assert amb("inventory levels", SOURCES) == []                  # one source only
    assert amb("ecommerce orders by day", SOURCES) == [
        {"table": "ecommerce_orders", "sources": ["demo", "snowflake"]}]
    assert amb("ecommerce_orders by day", SOURCES)[0]["table"] == "ecommerce_orders"
    assert amb("orders by day", SOURCES) == []                     # multi-word needs the phrase
    assert amb("revenue trend", SOURCES) == []                     # names no table
    assert amb("", SOURCES) == []


def test_no_stemming_so_ordinary_english_does_not_ask():
    srcs = [_src("demo", ["orders", "status", "users"]), _src("snowflake", ["orders", "status", "users"])]
    amb = orchestrator.ambiguous_tables
    assert amb("show revenue in order of region", srcs) == []      # "order" ≠ table `orders`
    assert amb("which user signed up last", srcs) == []            # "user" ≠ `users`
    assert amb("top users by spend", srcs) == [{"table": "users", "sources": ["demo", "snowflake"]}]
    assert amb("what is the status of the migration", srcs) == [  # the table IS named
        {"table": "status", "sources": ["demo", "snowflake"]}]


def test_punctuated_table_names_normalize_like_the_prompt():
    srcs = [_src("demo", ["web-traffic", "analytics.events"]),
            _src("neo4j", ["web-traffic", "analytics.events"])]
    amb = orchestrator.ambiguous_tables
    assert amb("web traffic by day", srcs) == [{"table": "web-traffic", "sources": ["demo", "neo4j"]}]
    assert amb("web-traffic by day", srcs)[0]["table"] == "web-traffic"
    assert amb("analytics events count", srcs)[0]["table"] == "analytics.events"


def _ctx(prompt, allow=False):
    cid = db.create_conversation(ANA["id"], prompt)
    return {"mode": "*", "cid": cid, "history": [], "model": None, "prompt": prompt,
            "allow_ambiguous": allow}


def test_all_sources_turn_asks_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user, **k: SOURCES)
    ran = []
    monkeypatch.setattr(orchestrator, "run_orchestrated", lambda *a, **k: ran.append(1))

    ctx = _ctx("show sales by region")
    r = chat._run_turn(ctx, ANA)
    assert r["mode"] == "clarify" and ran == []                    # no agent ran
    assert "`sales` is in demo and snowflake" in r["text"] and "ask both side by side" in r["text"]
    assert [o["source"] for o in r["clarify"]["options"]] == ["demo", "snowflake", "*"]
    assert r["clarify"]["options"][0]["tables"] == ["sales"]
    assert r["clarify"]["prompt"] == "show sales by region"
    assert r["sql"] is None and r["rows"] == []
    # The question is stored as an assistant message in the conversation.
    stored = [m for m in db.list_messages(ctx["cid"]) if m["role"] == "assistant"]
    assert stored and stored[-1]["content"]["mode"] == "clarify"


def test_both_side_by_side_runs_with_the_probed_roster(monkeypatch):
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user, **k: SOURCES)
    seen = {}

    def fake_run(prompt, user, history, model=None, conversation_id=None, sources=None):
        seen["sources"] = sources
        return {"text": "both answered", "sql": "SELECT 1", "columns": ["x"], "rows": [[1]],
                "chart": None, "panels": [], "email": None, "errors": [],
                "mode": "orchestrated", "model": None, "source": "demo",
                "agents_used": ["demo", "snowflake"]}
    monkeypatch.setattr(orchestrator, "run_orchestrated", fake_run)

    r = chat._run_turn(_ctx("show sales by region", allow=True), ANA)
    assert r["mode"] == "orchestrated" and r["text"] == "both answered"
    assert seen["sources"] is SOURCES                              # roster reused, not re-probed


def test_unambiguous_prompt_runs_straight_through(monkeypatch):
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user, **k: SOURCES)
    monkeypatch.setattr(orchestrator, "run_orchestrated", lambda *a, **k: {
        "text": "ok", "sql": None, "columns": [], "rows": [], "chart": None, "panels": [],
        "email": None, "errors": [], "mode": "orchestrated", "model": None,
        "source": "demo", "agents_used": ["demo"]})
    r = chat._run_turn(_ctx("top customers"), ANA)
    assert r["mode"] == "orchestrated"


def test_three_sources_and_two_clashing_tables_wording(monkeypatch):
    srcs = SOURCES + [_src("databricks", ["sales"])]
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user, **k: srcs)
    monkeypatch.setattr(orchestrator, "run_orchestrated", lambda *a, **k: None)
    r = chat._run_turn(_ctx("sales and ecommerce orders by day"), ANA)
    assert r["mode"] == "clarify"
    assert "`ecommerce_orders` is in demo and snowflake" in r["text"]
    assert "`sales` is in databricks, demo and snowflake" in r["text"]
    assert "ask all 3 side by side" in r["text"]
    opts = {o["source"]: o["tables"] for o in r["clarify"]["options"]}
    assert opts["databricks"] == ["sales"] and opts["demo"] == ["ecommerce_orders", "sales"]
    assert opts["*"] == ["ecommerce_orders", "sales"]


def test_clarify_exchange_is_hidden_from_model_history_and_both_is_sticky(monkeypatch):
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user, **k: SOURCES)
    calls = []

    def fake_run(prompt, user, history, model=None, conversation_id=None, sources=None):
        calls.append(prompt)
        return {"text": "answered", "sql": "SELECT 1", "columns": ["x"], "rows": [[1]],
                "chart": None, "panels": [], "email": None, "errors": [],
                "mode": "orchestrated", "model": None, "source": "demo",
                "agents_used": ["demo", "snowflake"]}
    monkeypatch.setattr(orchestrator, "run_orchestrated", fake_run)

    ctx = _ctx("show sales by region")
    cid = ctx["cid"]
    assert chat._run_turn(ctx, ANA)["mode"] == "clarify"
    # The model's history for the re-ask is EMPTY: the question + clarification
    # pair is dropped, so the re-ask is a genuine first turn (cache-eligible).
    assert chat._conversation(cid, ANA, "show sales by region")[1] == []

    # "Both, side by side": runs, and remembers `sales` for this conversation.
    ctx2 = {**_ctx("show sales by region", allow=True), "cid": cid}
    r = chat._run_turn(ctx2, ANA)
    assert r["mode"] == "orchestrated" and r["resolved_tables"] == ["sales"]

    # A later prompt naming `sales` in "*" mode runs straight through...
    ctx3 = {**_ctx("sales by month"), "cid": cid}
    assert chat._run_turn(ctx3, ANA)["mode"] == "orchestrated"
    # ...while a DIFFERENT shared table still asks.
    ctx4 = {**_ctx("ecommerce orders by day"), "cid": cid}
    assert chat._run_turn(ctx4, ANA)["mode"] == "clarify"
    assert calls == ["show sales by region", "sales by month"]
    # And the answered turns are what the model sees — no clarify text anywhere.
    hist = chat._conversation(cid, ANA, "next")[1]
    assert all("Which source" not in h["text"] for h in hist)
    assert [h["text"] for h in hist if h["role"] == "assistant"] == ["answered", "answered"]
