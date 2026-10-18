"""Metadata graph, phase 04: graph-grounded agent execution.

Acceptance: Studio discovers the correct retail tables from a natural-language
question that names none of them — by their columns and names in the
metadata graph — adds the tables needed to join them, and hands the SQL agent
their columns and the join conditions. The same grounding orders the
multi-source planners' roster and briefs every planned node's worker.
"""
import pytest

from app import metaground
from test_metagraph_discovery import ADMIN, RetailConnector, env  # noqa: F401

QUESTION = "Which customer segment brought in the most net sales in each store region?"


class RetailWithViews(RetailConnector):
    def view_definitions(self):
        with self._c() as c:
            return dict(c.execute("SELECT name, sql FROM sqlite_master WHERE type='view'"))


@pytest.fixture()
def grounded(env):
    conn = RetailWithViews(env.warehouse)
    from app import connectors
    env.mp.setitem(connectors._REGISTRY, "retail", conn)
    env.retail = conn
    env.jobs.handlers()["metagraph_discover"]({"source": "retail", "trigger": "t"}, None)
    env.allowed = conn.list_tables()
    return env


def joined(g):
    return {(j["from_object"], j["to_object"]) for j in g["joins"]}


# ── The acceptance ───────────────────────────────────────────────────────

def test_a_question_that_names_no_table_finds_the_right_ones(grounded):
    assert not any(t.replace("retail_", "") in QUESTION.lower().replace(" ", "_")
                   for t in ["retail_order_items", "retail_orders"])
    g = metaground.ground("retail", QUESTION, grounded.allowed)
    tables = set(g["tables"])
    assert {"retail_customers", "retail_order_items", "retail_stores"} <= set(g["matched"])
    assert "retail_orders" in g["bridges"]                 # needed only to join them
    assert {"retail_customers", "retail_orders", "retail_order_items",
            "retail_stores"} <= tables
    assert not tables & {"retail_returns", "retail_inventory", "retail_products"}
    assert joined(g) == {("retail_orders", "retail_customers"),
                         ("retail_orders", "retail_stores"),
                         ("retail_order_items", "retail_orders")}
    assert "segment" in g["matched"]["retail_customers"]
    assert "net_sale" in g["matched"]["retail_order_items"]
    assert [c["name"] for c in g["schemas"]["retail_order_items"]][-1] == "net_sales"


@pytest.mark.parametrize("question,expected", [
    ("top ten products by quantity sold", {"retail_products", "retail_order_items"}),
    ("why do people send things back? count by reason", {"retail_returns"}),
    ("on hand stock by store", {"retail_inventory", "retail_stores"}),
])
def test_other_questions_ground_on_their_tables(grounded, question, expected):
    g = metaground.ground("retail", question, grounded.allowed)
    assert expected <= set(g["tables"]), g["matched"]


def test_join_hints_read_as_conditions_with_their_evidence(grounded):
    g = metaground.ground("retail", QUESTION, grounded.allowed)
    lines = metaground.join_hint_lines(g["joins"])
    assert ("retail_orders.customer_id = retail_customers.customer_id  "
            "(observed, confidence 0.9)") in lines


def test_a_matching_view_is_offered_with_what_it_already_joins(grounded):
    g = metaground.ground("retail", QUESTION, grounded.allowed)
    assert set(g["views"]["retail_sales_detail"]) >= {"retail_orders", "retail_order_items"}
    lines = metaground.join_hint_lines(g["joins"], g["views"])
    assert any(line.startswith("retail_sales_detail is a view over") and
               line.endswith("it may answer this without joins") for line in lines)


def test_an_isolated_first_table_does_not_strand_the_rest():
    from app import metarelations as mr
    rel = {"kind": "join", "state": "observed", "confidence": 0.9, "from_object": "b",
           "to_object": "c", "from_columns": ["x"], "to_columns": ["x"]}
    joins, missing = mr.join_path([rel], ["a", "b", "c"])
    assert joins == [rel] and missing == ["a"]


# ── Permissions and fallbacks ────────────────────────────────────────────

def test_grounding_stays_inside_what_the_turn_may_read(grounded):
    allowed = [t for t in grounded.allowed if t != "retail_orders"]
    g = metaground.ground("retail", QUESTION, allowed)
    assert "retail_orders" not in g["tables"]
    assert all("retail_orders" not in (j["from_object"], j["to_object"]) for j in g["joins"])
    assert "retail_orders" not in g["schemas"]


def test_an_undiscovered_source_falls_back(grounded):
    assert metaground.ground("nowhere", QUESTION, ["a", "b"]) is None
    assert metaground.rank("nowhere", QUESTION, ["b", "a"]) == ["b", "a"]
    assert metaground.worker_context("nowhere", QUESTION, ["a"]) == ({}, [])


def test_governance_shapes_the_columns_handed_over(grounded, monkeypatch):
    from app import governance
    monkeypatch.setattr(governance, "governed_schema",
                        lambda source, table, cols: [c for c in cols
                                                     if c["name"] != "customer_name"])
    g = metaground.ground("retail", QUESTION, grounded.allowed)
    assert "customer_name" not in [c["name"] for c in g["schemas"]["retail_customers"]]


# ── The chat agent receives it ───────────────────────────────────────────

def test_the_chat_agent_gets_the_tables_and_how_they_join(grounded, monkeypatch):
    from app import chat, progress
    emitted = []
    monkeypatch.setattr(progress, "emit", lambda text, **k: emitted.append(text))
    ctx = {"source": "retail", "allowed": grounded.allowed, "connector": grounded.retail,
           "schemas": {"retail_products": grounded.retail.get_schema("retail_products")},
           "cid": None}
    focus, hints = chat._grounding(ctx, ADMIN, QUESTION)
    assert {"retail_customers", "retail_orders", "retail_order_items",
            "retail_stores"} <= set(focus)
    assert "retail_products" not in focus                       # already in the briefing
    assert any("retail_order_items.order_id = retail_orders.order_id" in h for h in hints)
    assert any(e.startswith("grounded in the metadata graph:") and "retail_orders" in e
               for e in emitted)


def test_the_system_prompt_carries_the_join_conditions(grounded):
    from app import agent
    _stable, volatile = agent._system_blocks(
        grounded.retail, "*", grounded.allowed, {}, [], skill_md="",
        focus_schemas={"retail_orders": [{"name": "order_id", "type": "INTEGER"}]},
        join_hints=["retail_order_items.order_id = retail_orders.order_id  "
                    "(observed, confidence 0.9)"])
    assert "How these tables join" in volatile
    assert "retail_order_items.order_id = retail_orders.order_id" in volatile


def test_a_full_chat_turn_hands_the_grounding_to_the_agent(grounded, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import agent, chat, keys, queries
    from app.auth import current_user
    for module in (chat, keys, queries):
        module.init_tables()
    seen = {}

    def run_agent(**kwargs):
        seen.update(kwargs)
        return {"text": "ok", "sql": None, "rows": [], "columns": [], "panels": [],
                "mode": "agent", "errors": [], "model": "test"}
    monkeypatch.setattr(agent, "run_agent", run_agent)
    monkeypatch.setattr(chat.semantic, "answer", lambda *a, **k: None)
    monkeypatch.setattr(chat.qcache, "lookup", lambda *a, **k: None)
    monkeypatch.setattr(chat.qcache, "store", lambda *a, **k: None)
    monkeypatch.setattr(chat.model_router, "choose", lambda *a, **k: ("frontier", None))
    monkeypatch.setattr(chat.skills, "get_skill", lambda *a, **k: "")
    monkeypatch.setattr(chat.lightning, "record_chat_trace", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(chat, "FOCUS_TABLES", 8)
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: dict(ADMIN)
    with TestClient(app) as client:
        r = client.post("/api/chat", json={"prompt": QUESTION, "source": "retail", "table": "*"})
    assert r.status_code == 200, r.text
    assert {"retail_customers", "retail_orders", "retail_order_items",
            "retail_stores"} <= set(seen["schemas"]) | set(seen["focus_schemas"])
    assert any("retail_orders.store_id = retail_stores.store_id" in h
               for h in seen["join_hints"])


# ── The multi-source planners and their workers ──────────────────────────

def test_the_planner_roster_lists_the_relevant_tables_first(grounded):
    from app import agent_graph
    sources = [{"connector": grounded.retail, "allowed": sorted(grounded.allowed)}]
    plain = agent_graph._roster_digest(sources, max_tables=3)
    ranked = agent_graph._roster_digest(sources, max_tables=3, prompt=QUESTION)
    assert plain[0]["tables"] == sorted(grounded.allowed)[:3]
    assert set(ranked[0]["tables"]) <= {"retail_customers", "retail_order_items",
                                        "retail_stores", "retail_sales_detail"}
    assert agent_graph.trajectory_input(QUESTION, sources)["sources"] == \
        agent_graph._roster_digest(sources, prompt=QUESTION)    # train == serve


def test_each_planned_node_worker_is_grounded(grounded, monkeypatch):
    from app import agent_graph
    seen = {}

    def run_agent(prompt, connector, table, allowed, schemas, history, user, model=None,
                  skill_md=None, **kwargs):
        seen.update(kwargs)
        return {"text": "ok", "sql": None, "rows": [], "columns": [], "panels": [],
                "errors": []}
    monkeypatch.setattr(agent_graph.agent, "run_agent", run_agent)
    entry = {"connector": grounded.retail, "allowed": grounded.allowed, "schemas": {},
             "skill": ""}
    agent_graph.run_node({"id": "n1", "source": "retail", "task": QUESTION, "depends_on": []},
                         entry, QUESTION, ADMIN, None, {})
    assert "retail_orders" in seen["focus_schemas"]
    assert seen["join_hints"] and seen["tool_profile"] == "graph_worker"
