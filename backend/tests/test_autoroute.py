"""Auto routing — the question picks its source and tables.

Real SQLite sources, discovered by the metadata graph: a question that one
source covers goes there as if picked; one split across sources goes to the
multi-agent path over just those; the same tables in two places ask which;
a follow-up naming nothing new stays put; a role never routes to what it may
not read. Through chat: the route is decided once, at request time, written
into the queued request and the user message, and shown on the answer; a
route sent by a client is never taken.
"""
import importlib
import json
import sqlite3
import types

import pytest

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANA = {"id": "u-ana", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}

SHOP = ["CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, amount REAL, "
        "region TEXT, ordered_at TEXT)",
        "CREATE TABLE customers (customer_id INTEGER, customer_name TEXT, segment TEXT)",
        "INSERT INTO orders VALUES (1, 1, 120.0, 'EU', '2026-10-01'), "
        "(2, 2, 80.0, 'US', '2026-10-02')",
        "INSERT INTO customers VALUES (1, 'Ana', 'enterprise'), (2, 'Ben', 'smb')"]
SUPPORT = ["CREATE TABLE tickets (ticket_id INTEGER, customer_id INTEGER, priority TEXT, "
           "status TEXT, opened_at TEXT)",
           "CREATE TABLE agents (agent_id INTEGER, agent_name TEXT, team TEXT)",
           "INSERT INTO tickets VALUES (1, 1, 'high', 'open', '2026-10-03')"]

ROLES = """version: 1
roles:
  admin: { sources: "*" }
  analyst:
    sources: {SOURCES}
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_SECRET", "autoroute-secret")
    monkeypatch.setenv("STUDIO_SQL_ALLOW_LOCAL_FILES", "1")
    monkeypatch.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "HARRIER_EMBED_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import (agent, autoroute, chat, connections, connectors, governance, jobs,
                     metagraph, orchestrator, qcache, queries, semantic)
    from app.connectors import demo
    from app.connectors.sql_conn import SqlConnector
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    _db.init_db()
    for module in (chat, governance, semantic, jobs, metagraph, connections, qcache, queries):
        module.init_tables()

    def source(name, ddl, discover=True):
        path = tmp_path / f"{name}.db"
        with sqlite3.connect(path) as c:
            for stmt in ddl:
                c.execute(stmt)
        monkeypatch.setitem(connectors._REGISTRY, name, SqlConnector(name, f"sqlite:///{path}"))
        if discover:
            metagraph.discover(name)

    def roles(spec):
        governance._set(ROLES.replace("{SOURCES}", spec), "test")

    ran = {"agent": [], "orchestrated": []}

    def fake_agent(**kw):
        ran["agent"].append(kw["connector"].name)
        return {"text": "answered", "sql": None, "columns": [], "rows": [], "chart": None,
                "panels": [], "errors": [], "agents": [], "mode": "agent", "model": "x"}

    def fake_orchestrated(prompt, user, history, model=None, conversation_id=None, sources=None):
        ran["orchestrated"].append(sorted(s["connector"].name for s in sources))
        # Like the real one, the answer carries its first agent's source name.
        return {"text": "fanned out", "source": sources[0]["connector"].name, "sql": None,
                "columns": [], "rows": [],
                "chart": None, "panels": [], "agents": [], "mode": "orchestrated",
                "agents_used": [s["connector"].name for s in sources]}
    monkeypatch.setattr(agent, "run_agent", fake_agent)
    monkeypatch.setattr(orchestrator, "run_orchestrated", fake_orchestrated)
    source("shop", SHOP)
    source("support", SUPPORT)
    roles('{shop: "*", support: "*"}')
    yield types.SimpleNamespace(ar=autoroute, chat=chat, db=_db, jobs=jobs, source=source,
                                roles=roles, ran=ran, mp=monkeypatch)
    semantic._STATE.update(doc=None, yaml="", source=None)
    governance._STATE.update(doc=None, yaml="", source=None)


def route(env, prompt, previous=None, user=ANA):
    return env.ar.route(user, prompt, previous)


def test_a_question_one_source_covers_goes_there(env):
    r = route(env, "total amount of orders by region")
    assert r["mode"] == "single" and r["source"] == "shop"
    assert r["tables"][0] == "orders" and {"amount", "region"} <= set(r["terms"])
    r = route(env, "open tickets by priority")
    assert r["mode"] == "single" and r["source"] == "support" and r["tables"][0] == "tickets"


def test_a_question_split_across_sources_asks_just_those(env):
    env.source("hr", ["CREATE TABLE employees (employee_id INTEGER, salary REAL)"])
    env.roles('{shop: "*", support: "*", hr: "*"}')
    r = route(env, "order amount for customers with high priority tickets")
    assert r["mode"] == "multi" and sorted(r["sources"]) == ["shop", "support"]
    assert "hr" not in r["tables"]


def test_the_same_tables_in_two_places_is_a_tie_not_a_guess(env):
    env.source("shop_eu", SHOP)
    env.roles('{shop: "*", support: "*", shop_eu: "*"}')
    r = route(env, "total amount of orders by region")
    assert r["mode"] == "multi" and sorted(r["sources"]) == ["shop", "shop_eu"]
    # …unless the conversation is already on one of them.
    kept = route(env, "total amount of orders by region",
                 previous={"mode": "single", "source": "shop_eu"})
    assert kept["mode"] == "single" and kept["source"] == "shop_eu"


def test_nothing_named_keeps_the_conversation_where_it_was(env):
    assert route(env, "hello, what can you do?")["mode"] == "all"
    prev = {"mode": "single", "source": "support", "tables": ["tickets"], "terms": ["ticket"]}
    r = route(env, "and only last week?", previous=prev)
    assert r["mode"] == "single" and r["source"] == "support" and r["kept"]
    assert r["terms"] == [] and r["tables"] == ["tickets"]         # not the earlier question's words
    # A previous route to a source this user can no longer use is not kept.
    env.roles('{shop: "*"}')
    r = route(env, "and only last week?", previous=prev)
    assert r["source"] == "shop" and not r.get("kept")             # the only one left


def test_a_route_never_uses_what_the_role_may_not_read(env):
    env.roles('{shop: [customers], support: "*"}')
    r = route(env, "total amount of orders by region")
    assert r["mode"] == "all"                                       # orders is not this role's
    assert "orders" not in json.dumps(r) and "amount" not in json.dumps(r)
    r = route(env, "customer segments")
    assert r["mode"] == "single" and r["source"] == "shop" and r["tables"] == ["customers"]
    env.roles('{shop: "*", support: "*"}')
    env.ar.governance._set(ROLES.replace("{SOURCES}", '{shop: "*", support: "*"}')
                           + "compliance:\n  shop:\n    orders:\n"
                             "      deny_columns: [amount, region]\n", "test")
    r = route(env, "amount by region")
    assert r["mode"] == "all"                                       # denied columns are invisible


def test_a_source_not_yet_discovered_is_matched_by_its_table_names(env):
    env.source("crm", ["CREATE TABLE opportunities (opportunity_id INTEGER, stage TEXT)"],
               discover=False)
    env.roles('{shop: "*", support: "*", crm: "*"}')
    r = route(env, "opportunities won this quarter")
    assert r["mode"] == "single" and r["source"] == "crm"


def ask(env, user=ANA, **kw):
    return env.chat.ask(env.chat.Ask(**{"source": "demo", "table": "*", **kw}), user=user)


def test_chat_answers_from_the_routed_source_and_says_so(env):
    out = ask(env, prompt="total amount of orders by region", auto=True)
    msg = out["message"]
    assert env.ran["agent"] == ["shop"] and env.ran["orchestrated"] == []
    assert msg["auto_route"]["source"] == "shop" and msg["auto_route"]["mode"] == "single"
    # Which database answered, by type and engine (its icon and name in the chat).
    assert msg["auto_route"]["types"]["shop"]["engine"] == "sqlite"
    assert msg["source_meta"] == {"source_type": "shop", "engine": "sqlite"}
    stored = [m for m in env.db.list_messages(out["conversation_id"]) if m["role"] == "user"]
    assert stored[-1]["content"]["source"] == "shop"
    assert stored[-1]["content"]["auto_route"]["source"] == "shop"
    # A follow-up that names nothing new stays on shop.
    ask(env, prompt="and only last week?", auto=True, conversation_id=out["conversation_id"])
    assert env.ran["agent"] == ["shop", "shop"]


def test_a_follow_up_stays_on_the_source_picked_by_hand(env):
    out = ask(env, prompt="open tickets by priority", source="support")
    ask(env, prompt="and only last week?", auto=True, conversation_id=out["conversation_id"])
    assert env.ran["agent"] == ["support", "support"]


def test_chat_asks_only_the_sources_a_split_question_needs(env):
    env.source("hr", ["CREATE TABLE employees (employee_id INTEGER, salary REAL)"])
    env.roles('{shop: "*", support: "*", hr: "*"}')
    out = ask(env, prompt="order amount for customers with high priority tickets", auto=True)
    assert env.ran["orchestrated"] == [["shop", "support"]] and env.ran["agent"] == []
    stored = [m for m in env.db.list_messages(out["conversation_id"]) if m["role"] == "user"]
    assert stored[-1]["content"]["table"] == "shop, support"
    assert out["message"]["auto_route"]["mode"] == "multi"
    assert "source_meta" not in out["message"]                     # no one database to name
    assert set(out["message"]["auto_route"]["types"]) == {"shop", "support"}


def test_a_route_is_never_taken_from_the_client(env):
    forged = {"mode": "single", "source": "shop", "why": "trust me"}
    out = ask(env, prompt="open tickets by priority", source="support", route=forged)
    assert "auto_route" not in out["message"]
    stored = [m for m in env.db.list_messages(out["conversation_id"]) if m["role"] == "user"]
    assert "auto_route" not in stored[-1]["content"]
    out = ask(env, prompt="open tickets by priority", auto=True, route=forged)
    assert out["message"]["auto_route"]["source"] == "support"     # decided by the server


def test_the_background_turn_queues_the_routed_request(env):
    env.mp.setattr(env.chat.jobs, "enqueue", lambda kind, payload, **kw: queued.append(payload))
    queued = []
    env.chat.ask_background(env.chat.Ask(prompt="open tickets by priority", source="demo",
                                         table="*", auto=True), user=ANA)
    body = queued[0]["body"]
    assert body["source"] == "support" and body["route"]["source"] == "support"
    # The worker rebuilds the turn from that body and does not route again.
    env.mp.setattr(env.ar, "route", lambda *a, **k: pytest.fail("routed twice"))
    ctx = env.chat._build_ctx(env.chat.Ask(**body), ANA, queued[0]["cid"])
    assert ctx["source"] == "support" and ctx["auto_route"]["source"] == "support"
