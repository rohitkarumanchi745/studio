"""What an answer used: each source, and under it the tables and the columns
its SQL names (answer_uses.py) — the chat's right-hand panel draws it.

Locked in:
- tables as written, CTEs excluded, with their aliases (queryguard.table_aliases);
- a table's columns are the ones its schema has that the SQL names — bare,
  or through that table's own alias; functions and other tables' aliases do
  not count; `*` marks a table read whole;
- the turn's own schema first, else the metadata graph's record; no source is
  queried; an unknown table still lists, with no columns;
- each view's SQL under its own source; "*" / "auto" are not sources; caps;
- every saved answer carries it; describing never breaks saving;
- a withheld answer drops it with the SQL.

Run from the backend directory:
    python -m pytest tests/test_answer_uses.py -q
"""
import json
import time
import uuid

import pytest

from app import answer_uses, chat, db, governance, metagraph, queryguard

SALES = [{"name": "region"}, {"name": "revenue"}, {"name": "customer_id"}, {"name": "day"},
         {"name": "id"}]
CUSTOMERS = [{"name": "id"}, {"name": "segment"}, {"name": "region"}, {"name": "name"}]
SCHEMAS = {"sales": SALES, "Customers": CUSTOMERS}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    governance.init_tables()
    metagraph.init_tables()
    yield


def _uses(sql, schemas=SCHEMAS, source="shop", **extra):
    return answer_uses.uses({"source": source, "sql": sql, **extra}, schemas=schemas, schema_source=source)


def _tree(out):
    return {u["source"]: [(t["name"], t["columns"], t.get("all_columns", False)) for t in u["tables"]]
            for u in out}


# ── Tables and aliases ──────────────────────────────────────────────────

@pytest.mark.parametrize("sql,want", [
    ("SELECT s.region FROM public.Sales s JOIN customers AS c ON c.id = s.customer_id, orders",
     [("Sales", "s"), ("customers", "c"), ("orders", None)]),
    ('WITH t AS (SELECT * FROM sales) SELECT x FROM t JOIN "Customers" ON 1=1',
     [("sales", None), ("Customers", None)]),
    ("SELECT 1 FROM sales LEFT JOIN regions USING (id) WHERE 1=1", [("sales", None), ("regions", None)]),
    ("SELECT * FROM sales GROUP BY 1", [("sales", None)]),
    ("SELECT * FROM (SELECT * FROM sales) x", [("sales", None)]),
    ("DROP TABLE x", []), ("", []), ("SELECT 1", []),
])
def test_tables_as_written_with_their_aliases(sql, want):
    assert queryguard.table_aliases(sql) == want


# ── Columns under each table ────────────────────────────────────────────

def test_columns_the_sql_names_bare_or_through_the_tables_alias():
    out = _uses("SELECT s.region, SUM(s.revenue) AS total, c.segment FROM sales s "
                "JOIN Customers c ON c.id = s.customer_id GROUP BY s.region, c.segment")
    assert out == [{"source": "shop", "tables": [
        {"name": "sales", "columns": ["region", "revenue", "customer_id"]},
        {"name": "Customers", "columns": ["id", "segment"]}]}]


def test_bare_names_count_for_every_table_that_has_them_aliases_only_for_their_own():
    out = _tree(_uses("SELECT region, c.name FROM sales JOIN customers c ON c.id = sales.id"))
    # "region" is bare: both tables have it. "c.name" is customers' only.
    assert out == {"shop": [("sales", ["region", "id"], False), ("customers", ["id", "region", "name"], False)]}


def test_functions_strings_and_other_words_are_not_columns():
    out = _tree(_uses("SELECT DAY(x) AS n, 'region' FROM sales WHERE revenue > 0"))
    assert out == {"shop": [("sales", ["revenue"], False)]}


def test_star_marks_a_table_read_whole():
    assert _tree(_uses("SELECT * FROM sales LIMIT 5")) == {"shop": [("sales", [], True)]}
    out = _tree(_uses("SELECT c.*, s.revenue FROM sales s JOIN customers c ON c.id = s.id"))
    assert out == {"shop": [("sales", ["revenue", "id"], False), ("customers", ["id"], True)]}
    assert _tree(_uses("SELECT COUNT(*) FROM sales")) == {"shop": [("sales", [], False)]}
    assert _tree(_uses("SELECT region, * FROM sales")) == {"shop": [("sales", ["region"], True)]}


def test_a_table_with_no_known_schema_still_lists():
    assert _tree(_uses("SELECT amount FROM ledger")) == {"shop": [("ledger", [], False)]}


def test_the_metadata_graph_has_the_columns_of_a_table_the_turn_did_not_load():
    oid = str(uuid.uuid4())
    now = time.time()
    with db.connect() as c:
        c.execute("INSERT INTO meta_objects (id, source, name, kind, fingerprint, column_count, "
                  "first_seen_at, last_seen_at, changed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (oid, "uses-wh", "LEDGER", "table", "f", 3, now, now, now))
        for i, name in enumerate(("AMOUNT", "BOOKED_ON", "ACCOUNT")):
            c.execute("INSERT INTO meta_columns (object_id, ordinal, name, type) VALUES (?,?,?,?)",
                      (oid, i, name, "TEXT"))
        c.commit()
    out = _tree(_uses("SELECT account, SUM(amount) FROM ledger GROUP BY 1", schemas={}, source="uses-wh"))
    assert out == {"uses-wh": [("ledger", ["AMOUNT", "ACCOUNT"], False)]}


# ── Sources ─────────────────────────────────────────────────────────────

def test_each_view_under_its_own_source_and_no_pseudo_sources():
    result = {"source": "*", "sql": "SELECT region FROM sales", "panels": [
        {"source": "shop", "sql": "SELECT region, revenue FROM sales"},
        {"source": "warehouse", "sql": "SELECT segment FROM customers"},
        {"source": "shop", "sql": "SELECT s.day FROM sales s"},
        {"chart": {"type": "bar"}}]}
    out = answer_uses.uses(result, schemas={"sales": SALES, "customers": CUSTOMERS}, schema_source="shop")
    # The turn's schema is shop's: warehouse's customers is not read through it.
    assert _tree(out) == {"shop": [("sales", ["region", "revenue", "day"], False)],
                          "warehouse": [("customers", [], False)]}
    assert answer_uses.uses({"source": "auto", "sql": "SELECT 1 FROM t"}) == []
    assert answer_uses.uses({"text": "hello"}) == []
    assert answer_uses.uses(None) == []


def test_a_sources_type_and_engine_come_along():
    out = answer_uses.uses({"source": "demo", "sql": "SELECT region FROM sales"})
    assert out[0]["source"] == "demo" and out[0]["source_type"] == "demo" and out[0]["engine"] == "sqlite"


def test_caps(monkeypatch):
    monkeypatch.setattr(answer_uses, "MAX_TABLES", 2)
    monkeypatch.setattr(answer_uses, "MAX_COLUMNS", 2)
    out = _tree(_uses("SELECT region, revenue, day FROM sales JOIN a ON 1=1 JOIN b ON 1=1"))
    assert out == {"shop": [("sales", ["region", "revenue"], False), ("a", [], False)]}
    monkeypatch.setattr(answer_uses, "MAX_SOURCES", 1)
    out = answer_uses.uses({"source": "*", "panels": [{"source": "x", "sql": "SELECT 1 FROM t"},
                                                       {"source": "y", "sql": "SELECT 1 FROM u"}]})
    assert [u["source"] for u in out] == ["x"]


# ── Saved with every answer; withheld with the SQL ──────────────────────

def test_every_saved_answer_carries_it_and_describing_never_breaks_saving(monkeypatch):
    cid = db.create_conversation("u-uses", "t")
    ctx = {"cid": cid, "source": "shop", "schemas": {"sales": SALES}}
    chat._answer(ctx, {"text": "ok", "source": "shop", "sql": "SELECT region FROM sales"})
    saved = db.list_messages(cid)[-1]["content"]
    assert _tree(saved["uses"]) == {"shop": [("sales", ["region"], False)]}
    # Already described (or no SQL): left as is.
    chat._answer(ctx, {"text": "plain"})
    assert "uses" not in db.list_messages(cid)[-1]["content"]
    monkeypatch.setattr(answer_uses, "uses", lambda *a, **k: 1 / 0)
    chat._answer(ctx, {"text": "still saved", "source": "shop", "sql": "SELECT 1 FROM sales"})
    assert db.list_messages(cid)[-1]["content"]["uses"] == []


def test_a_withheld_answer_drops_it_with_the_sql():
    ana = {"id": "u-uses-ana", "email": "uses-ana@studio.test", "role": "analyst", "name": "A"}
    viewer = {"id": "u-uses-view", "email": "uses-view@studio.test", "role": "viewer", "name": "V"}
    cid = db.create_conversation(ana["id"], "t")
    db.add_message(cid, "user", {"text": "top customers", "source": "demo", "table": "customers",
                                 "author_role": "analyst"})
    db.add_message(cid, "assistant", {
        "text": "Ada.", "source": "demo", "table": "customers", "author_role": "analyst",
        "sql": "SELECT name, lifetime_value FROM customers",
        "uses": [{"source": "demo", "tables": [{"name": "customers", "columns": ["name", "lifetime_value"]}]}]})
    db.share_conversation(cid, viewer["id"], "view")
    seen = chat._visible_messages(cid, viewer, "view")[-1]["content"]
    assert seen.get("redacted") and "uses" not in seen and "lifetime_value" not in json.dumps(seen)
    assert chat._visible_messages(cid, ana, "owner")[-1]["content"]["uses"][0]["source"] == "demo"


def test_a_real_turn_saves_what_it_read():
    from fastapi.testclient import TestClient
    import app.main as main
    with TestClient(main.app) as c:
        tok = c.post("/api/auth/login", json={"email": "analyst@studio.local",
                                              "password": "analyst123"}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        d = c.post("/api/chat/background", headers=h,
                   json={"prompt": "revenue by region", "source": "demo", "table": "sales"}).json()
        for _ in range(240):
            if c.get(f"/api/tasks/{d['task_id']}", headers=h).json().get("status") in ("done", "failed"):
                break
            time.sleep(0.25)
        msgs = c.get(f"/api/conversations/{d['conversation_id']}/messages", headers=h).json()
        msgs = msgs["messages"] if isinstance(msgs, dict) else msgs
        answer = [m for m in msgs if m["role"] == "assistant"][-1]["content"]
    assert answer["uses"][0]["source"] == "demo"
    assert [t["name"] for t in answer["uses"][0]["tables"]] == ["sales"]
