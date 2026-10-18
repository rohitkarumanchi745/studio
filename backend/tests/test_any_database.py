"""Any SQL database through one connection — nothing vendor-specific.

A SQLAlchemy URL names the engine; everything about it is learned. Locked in:
the guard enforces the identifier rule the ENGINE reported (and fails closed
when it cannot be learned), the row cap follows the engine's limit syntax,
metadata comes from SQLAlchemy's inspector (tables, views, columns, keys, view
SQL), names outside the default schema are written in full, a URL to a file on
Studio's own server is refused, secrets never reach a listing or an error, and
the metadata graph and change capture work on it like on any source.

The engine here is SQLite through SQLAlchemy (a real database, no stand-in);
the real-Postgres run is in test_metagraph_postgres.py.
"""
import importlib
import json
import sqlite3
import types

import pytest

from app import queryguard
from app.connectors import sql_conn
from app.connectors.sql_conn import EngineDialect, SqlConnector
from app.queryguard import QueryRejected

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
VIEWER = {"id": "u-viewer", "email": "viewer@studio.test", "role": "viewer", "name": "Viewer"}


def ok(sql, allowed, rule, qualifiers=None):
    return queryguard.validate(sql, allowed, qualifiers=qualifiers,
                               dialect=EngineDialect("someengine", rule))


def refused(sql, allowed, rule, qualifiers=None):
    with pytest.raises(QueryRejected):
        ok(sql, allowed, rule, qualifiers)


# ── The guard enforces what the engine said, and fails closed otherwise ──

def test_a_lower_folding_engine_cannot_be_tricked_by_a_quoted_lookalike():
    ok("SELECT * FROM Customers", ["customers"], "lower")          # folds to customers
    refused('SELECT * FROM "Customers"', ["customers"], "lower")   # a different table
    refused('WITH "CUSTOMERS" AS (SELECT * FROM sales) SELECT * FROM customers',
            ["sales"], "lower")                                    # the CTE does not answer it
    ok("WITH c AS (SELECT * FROM sales) SELECT * FROM C", ["sales"], "lower")


def test_an_upper_folding_engine_reads_bare_names_as_capitals():
    ok("SELECT * FROM customers", ["CUSTOMERS"], "upper")
    refused('SELECT * FROM "customers"', ["CUSTOMERS"], "upper")
    refused('WITH "customers" AS (SELECT * FROM SALES) SELECT * FROM customers',
            ["SALES"], "upper")


def test_an_engine_that_keeps_names_as_written_matches_them_exactly():
    ok("SELECT * FROM SalesOrderHeader", ["SalesOrderHeader"], "exact")
    ok('SELECT * FROM [SalesOrderHeader] JOIN `Customer` ON 1=1',
       ["SalesOrderHeader", "Customer"], "exact")
    refused("SELECT * FROM salesorderheader", ["SalesOrderHeader"], "exact")
    refused("WITH cte AS (SELECT * FROM SalesOrderHeader) SELECT * FROM CTE",
            ["SalesOrderHeader"], "exact")                        # CTE is not cte here


def test_an_engine_whose_rule_is_unknown_is_read_only_through_quoted_names():
    ok('SELECT * FROM "Customers" JOIN "orders" ON 1=1', ["Customers", "orders"],
       "quoted_only")
    refused("SELECT * FROM Customers", ["Customers"], "quoted_only")
    refused("SELECT * FROM orders", ["orders"], "quoted_only")     # it might fold up
    ok('WITH t AS (SELECT * FROM "orders") SELECT * FROM t', ["orders"], "quoted_only")


def test_a_refusal_says_how_to_write_a_name_the_engine_would_misread():
    with pytest.raises(QueryRejected, match="in quotes, exactly as listed"):
        ok("SELECT * FROM OrderLines", ["OrderLines"], "lower")
    with pytest.raises(QueryRejected, match="not permitted for your role"):
        ok("SELECT * FROM Payroll", ["OrderLines"], "lower")         # not granted: no hint


def test_the_engine_name_is_still_a_plain_string():
    d = EngineDialect("mssql", "exact")
    assert d == "mssql" and json.dumps({"dialect": d}) == '{"dialect": "mssql"}'
    assert d.identifier_rule == "exact"


# ── Learned from the engine and the dialect ──────────────────────────────

@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_SQL_ALLOW_LOCAL_FILES", "1")
    sql_conn._LEARNED.clear()
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, segment TEXT)")
        c.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER "
                  "REFERENCES customers(customer_id), amount REAL, placed_on TEXT)")
        c.execute("CREATE TABLE payroll (employee TEXT, salary REAL)")
        c.execute("CREATE VIEW big_orders AS SELECT o.order_id, c.segment FROM orders o "
                  "JOIN customers c ON c.customer_id = o.customer_id WHERE o.amount > 10")
        c.executemany("INSERT INTO customers VALUES (?, ?)", [(1, "smb"), (2, "ent")])
        c.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)",
                      [(i, 1 + i % 2, i * 3.0, "2026-10-01") for i in range(1, 31)])
    yield types.SimpleNamespace(path=path, url=f"sqlite:///{path}")
    sql_conn._LEARNED.clear()


def test_the_rule_and_the_limit_syntax_are_learned_not_listed(warehouse, monkeypatch):
    from sqlalchemy.dialects import mssql, mysql, oracle, postgresql
    conn = SqlConnector("shop", warehouse.url)
    assert conn.dialect == "sqlite" and conn.dialect.identifier_rule == "exact"
    assert conn.sql_row_limit is True
    for dialect, expect in ((postgresql.dialect(), True), (mysql.dialect(), True),
                            (mssql.dialect(), False), (oracle.dialect(), False)):
        assert sql_conn.limit_syntax(dialect) is expect, dialect.name
    # An engine that cannot be asked gets the fail-closed rule.
    sql_conn._LEARNED.clear()
    other = SqlConnector("shop2", warehouse.url)
    monkeypatch.setattr(sql_conn, "PROBE", "SELECT broken FROM nowhere")
    assert other.dialect.identifier_rule == "quoted_only"


def test_metadata_comes_from_the_inspector(warehouse):
    conn = SqlConnector("shop", warehouse.url)
    assert conn.list_tables() == ["big_orders", "customers", "orders", "payroll"]
    assert [c["name"] for c in conn.get_schema("orders")] == [
        "order_id", "customer_id", "amount", "placed_on"]
    rel = conn.describe_relations()
    assert rel["big_orders"]["kind"] == "view" and rel["orders"]["kind"] == "table"
    keys = conn.describe_constraints()
    assert keys["primary_keys"]["orders"] == ["order_id"]
    assert keys["foreign_keys"] == [{"name": "orders_fk", "from": "orders",
                                     "from_columns": ["customer_id"], "to": "customers",
                                     "to_columns": ["customer_id"]}]
    assert "JOIN customers" in conn.view_definitions()["big_orders"]
    assert conn.describe_relation("orders")["columns"][0]["name"] == "order_id"
    assert conn.describe_relation("no_such") is None
    assert conn.qualifiers() == frozenset({"main"})            # a file: no database prefix
    assert {n["schema"] for n in conn.list_namespaces()} == {"main"}


def test_a_source_pinned_to_another_schema_names_every_table_in_full(warehouse, monkeypatch):
    conn = SqlConnector("shop", warehouse.url, schema="archive")
    monkeypatch.setattr(conn, "_relations", lambda i: {"orders": "table"})
    assert conn.list_tables() == ["archive.orders"]
    assert conn.qualifiers() is queryguard.FULL_NAMES and conn.full_names
    with pytest.raises(ValueError):
        conn.get_schema("orders")                              # a bare name is not this source's


# ── Through the gateway, like every source ───────────────────────────────

@pytest.fixture()
def env(warehouse, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_SECRET", "any-db-secret")
    monkeypatch.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "0")
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, gateway, governance, jobs, metagraph
    from app.connectors import demo
    _db.init_db()
    jobs.init_tables()
    metagraph.init_tables()
    connections.init_tables()
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    conn = SqlConnector("shop", warehouse.url)
    monkeypatch.setitem(connectors._REGISTRY, "shop", conn)
    governance._set("version: 1\nroles:\n  admin: { sources: \"*\" }\n"
                    "  viewer: { sources: { shop: [orders, customers, big_orders] } }\n",
                    "test")
    yield types.SimpleNamespace(db=_db, mg=metagraph, gw=gateway, jobs=jobs, conn=conn,
                                connections=connections, warehouse=warehouse, mp=monkeypatch)
    governance._STATE.update(doc=None, yaml="", source=None)


def test_queries_run_read_only_through_the_guard(env):
    out = env.gw.execute(VIEWER, "shop", "SELECT c.segment, SUM(o.amount) AS total FROM "
                         "orders o JOIN customers c ON c.customer_id = o.customer_id "
                         "GROUP BY c.segment ORDER BY c.segment", "test")
    assert out.columns == ["segment", "total"] and [r[0] for r in out.rows] == ["ent", "smb"]
    with pytest.raises(QueryRejected):
        env.gw.execute(VIEWER, "shop", "SELECT * FROM payroll", "test")       # not granted
    with pytest.raises(QueryRejected):
        env.gw.execute(ADMIN, "shop", "DELETE FROM orders", "test")
    assert len(env.gw.execute(ADMIN, "shop", "SELECT * FROM orders", "test",
                              max_rows=7).rows) == 7
    with sqlite3.connect(env.warehouse.path) as c:
        assert c.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 30


def test_an_engine_without_limit_is_capped_as_rows_are_fetched(env):
    _c, _a, cleaned = env.gw.check(ADMIN, "shop", "SELECT * FROM orders")
    assert cleaned.upper().endswith("LIMIT 50000")             # a LIMIT engine gets one
    env.mp.setattr(SqlConnector, "sql_row_limit", property(lambda self: False))
    _c, _a, cleaned = env.gw.check(ADMIN, "shop", "SELECT * FROM orders")
    assert "LIMIT" not in cleaned.upper()                     # the text is left alone
    env.mp.setenv("STUDIO_MAX_ROWS", "5")
    assert len(env.gw.execute(ADMIN, "shop", "SELECT * FROM orders", "test").rows) == 5


def test_the_model_is_briefed_from_what_was_learned(env):
    from app import skills
    text = skills._build(env.conn, "admin", ["orders"], {"orders": []}, "fp")
    assert "SQL dialect: sqlite" in text and "Write SQL for sqlite" in text
    assert "exactly as listed" in text
    env.mp.setattr(SqlConnector, "sql_row_limit", property(lambda self: False))
    sql_conn._LEARNED[env.conn._key()] = {**sql_conn._LEARNED[env.conn._key()], "limit": False}
    assert "does not use LIMIT" in skills._build(env.conn, "admin", ["orders"],
                                                 {"orders": []}, "fp")


def test_the_metadata_graph_and_change_capture_work_on_it(env):
    env.mg.discover("shop", "test")
    detail = env.mg.get_source("shop", user=ADMIN)
    assert detail["kinds"] == {"table": 3, "view": 1}
    rels = env.mg.source_relationships("shop", user=ADMIN)["relationships"]
    joins = {(r["from_object"], r["to_object"], r["state"]) for r in rels if r["kind"] == "join"}
    assert ("orders", "customers", "declared") in joins            # the foreign key
    lineage = {(r["from_object"], r["to_object"]) for r in rels if r["kind"] == "derived_from"}
    assert ("big_orders", "orders") in lineage                     # read from the view's SQL
    with sqlite3.connect(env.warehouse.path) as c:
        c.execute("ALTER TABLE customers ADD COLUMN region TEXT")
    assert env.mg.tick_once()["queued"].get("shop") is None    # no whole-source rediscovery
    with env.db.connect() as c:
        rows = c.execute("SELECT * FROM background_jobs WHERE kind='metagraph_event'").fetchall()
    for row in rows:
        env.jobs.handlers()["metagraph_event"](json.loads(row["payload"]),
                                               {**dict(row), "attempts": 1})
    customers = next(r for r in env.mg.get_source("shop", user=ADMIN)["relations"]
                     if r["name"] == "customers")
    assert [c["name"] for c in customers["columns"]][-1] == "region"
    from app import metacdc
    assert metacdc.status("shop")["capture"] == "bulk"


# ── Connecting one ───────────────────────────────────────────────────────

def test_the_connect_form_offers_it_and_the_probe_reads_it(env):
    kinds = {t["ctype"]: t for t in env.connections.types(user=ADMIN)}
    assert [f["key"] for f in kinds["sql"]["fields"]] == ["url", "schema"]
    assert kinds["sql"]["fields"][0]["secret"]
    probe = env.connections._probe("sql", {"url": env.warehouse.url})
    assert probe["ok"] and probe["tables"] == 4


def test_a_file_on_studios_own_server_is_refused(env):
    env.mp.delenv("STUDIO_SQL_ALLOW_LOCAL_FILES")
    out = env.connections._probe("sql", {"url": "sqlite:////app/data/studio.db"})
    assert not out["ok"] and out["stage"] == "check" and "no host" in out["fields"]["url"]


def test_a_missing_driver_says_which_ones_are_installed(env):
    out = env.connections._probe("sql", {"url": "teradatasql://u:p@td.example.com/db"})
    assert not out["ok"] and "Installed:" in out["fields"]["url"]
    assert "sqlite+pysqlite" in out["fields"]["url"]


def test_the_password_never_reaches_a_listing_or_an_error(env):
    url = "postgresql+psycopg://reader:s3cr3t-pw@127.0.0.1:1/shop"
    out = env.connections._probe("sql", {"url": url})
    assert not out["ok"] and "s3cr3t-pw" not in json.dumps(out)
    assert env.connections._hint("sql", {"url": url}) == "postgresql · 127.0.0.1"


def test_nothing_a_query_does_is_ever_committed(env):
    from app.connectors.base import unguarded
    with unguarded(), pytest.raises(Exception):
        env.conn.run_query("INSERT INTO orders VALUES (99, 1, 1.0, 'x')")   # past any guard
    with sqlite3.connect(env.warehouse.path) as c:
        assert c.execute("SELECT COUNT(*) FROM orders WHERE order_id = 99").fetchone()[0] == 0


def test_names_are_shown_as_an_upper_folding_engine_stores_them(env):
    oracle = SqlConnector("ora", "oracle+oracledb://reader:pw@ora.example.com:1521/?service_name=x")
    assert oracle._out("customers") == "CUSTOMERS" and oracle._in("CUSTOMERS") == "customers"
    assert oracle._out("MixedCase") == "MixedCase"             # a quoted name keeps its case
    assert sql_conn.limit_syntax(oracle.engine().dialect) is False   # FETCH FIRST


def test_an_engine_that_is_down_is_asked_again_only_after_a_minute(env, monkeypatch):
    probes = []
    real = sql_conn._probe_sql
    monkeypatch.setattr(sql_conn, "_probe_sql", lambda d: probes.append(1) or real(d))
    down = SqlConnector("down", "postgresql+psycopg://reader:pw@127.0.0.1:1/x")
    for _ in range(5):
        assert down.dialect.identifier_rule == "quoted_only"     # fail closed
        assert down.qualifiers() == frozenset()
    assert len(probes) == 1                                      # one attempt, remembered
    monkeypatch.setattr(sql_conn, "RETRY_SECONDS", 0)
    down.dialect
    assert len(probes) == 2                                      # asked again once it may


def test_saving_one_reads_the_logins_default_schema_or_the_one_picked(env):
    env.connections._create(ADMIN, "shop-db", "sql", {"url": env.warehouse.url},
                            {"url": env.warehouse.url})
    entry = next(s for s in env.connections.source_entries() if s["name"] == "shop-db")
    assert entry["dialect"] == "sqlite" and entry["configured"]
    listed = next(r for r in env.connections.list_connections(user=ADMIN)
                  if r["name"] == "shop-db")
    assert listed["hint"] == "sqlite" and env.warehouse.url not in json.dumps(listed)
