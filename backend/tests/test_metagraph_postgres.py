"""Postgres metadata discovery against a REAL server — the catalog SQL is only
proven by running it (a text || "char" ambiguity shipped once because nothing
did). Skipped unless STUDIO_TEST_POSTGRES_DSN names a database this test may
create and drop a scratch schema in, e.g.
    STUDIO_TEST_POSTGRES_DSN=postgresql://studio@localhost:55432/scratch
"""
import json
import os
import time
import types
import uuid

import pytest

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}

DSN = os.getenv("STUDIO_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="STUDIO_TEST_POSTGRES_DSN not set")


@pytest.fixture()
def pg():
    import psycopg
    from app import connections
    schema = "mg_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(f"CREATE TABLE {schema}.retail_orders (order_id int, customer_id int, "
                      f"order_date date, status text)")
        admin.execute(f"CREATE TABLE {schema}.retail_customers (customer_id int, segment text)")
        admin.execute(f"CREATE VIEW {schema}.retail_sales AS SELECT o.order_id, c.segment "
                      f"FROM {schema}.retail_orders o JOIN {schema}.retail_customers c "
                      f"USING (customer_id)")
        admin.execute(f"CREATE TABLE {schema}.retail_stores (store_id int PRIMARY KEY)")
        admin.execute(f"CREATE TABLE {schema}.retail_visits (visit_id int, "
                      f"store_ref int REFERENCES {schema}.retail_stores(store_id))")
        conn = connections._DynPostgres("pg-test", {"dsn": DSN, "schema": schema})

        def run(sql):
            admin.execute(sql.replace("{s}", schema))
        try:
            yield conn, run
        finally:
            conn.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_describe_relations_reads_kinds_and_typed_columns(pg):
    conn, _ = pg
    out = conn.describe_relations()
    assert out["retail_orders"]["kind"] == "table"
    assert out["retail_sales"]["kind"] == "view"
    assert out["retail_orders"]["columns"] == [
        {"name": "order_id", "type": "integer"}, {"name": "customer_id", "type": "integer"},
        {"name": "order_date", "type": "date"}, {"name": "status", "type": "text"}]


def test_the_change_signal_moves_with_every_kind_of_ddl(pg):
    conn, run = pg
    base = conn.change_signal()
    assert base and conn.change_signal() == base                 # stable when nothing changes
    previous = base
    for ddl in ("CREATE TABLE {s}.retail_promotions (promo_id int)",
                "ALTER TABLE {s}.retail_promotions ADD COLUMN starts_on date",
                "ALTER TABLE {s}.retail_promotions ALTER COLUMN promo_id TYPE bigint",
                "ALTER TABLE {s}.retail_promotions RENAME TO retail_offers",
                "DROP TABLE {s}.retail_offers"):
        run(ddl)
        signal = conn.change_signal()
        assert signal != previous, ddl
        previous = signal
    # It is a function of the schema: back where it started, the signal is too.
    assert previous == base


def test_declared_keys_and_view_joins_on_postgres(pg):
    from app import metarelations as mr
    conn, _ = pg
    keys = conn.describe_constraints()
    assert keys["primary_keys"] == {"retail_stores": ["store_id"]}
    assert keys["foreign_keys"][0]["from"] == "retail_visits"
    assert keys["foreign_keys"][0]["from_columns"] == ["store_ref"]
    views = conn.view_definitions()
    objects = {name: {"kind": info["kind"], "columns": info["columns"]}
               for name, info in conn.describe_relations().items()}
    rels = mr.build(objects, constraints=keys, views=views)
    declared = rels[("join", "retail_visits", ("store_ref",), "retail_stores", ("store_id",),
                     True)]
    assert declared["state"] == "declared"
    # Postgres stores the view's USING join rewritten; it is still read as one.
    observed = [k for k, v in rels.items() if k[0] == "join" and v["state"] == "observed"]
    assert observed and {observed[0][1], observed[0][3]} == {"retail_orders", "retail_customers"}


# ── Change capture ───────────────────────────────────────────────────────

def test_table_signals_move_only_for_the_relation_that_changed(pg):
    conn, run = pg
    base = conn.table_signals()
    assert set(base) == {"retail_orders", "retail_customers", "retail_sales", "retail_stores",
                         "retail_visits"}                          # the view too
    assert conn.table_signals() == base                            # stable
    run("ALTER TABLE {s}.retail_orders ADD COLUMN channel text")
    after = conn.table_signals()
    assert [t for t in base if after[t] != base[t]] == ["retail_orders"]
    previous, signal = after, conn.change_signal()
    for ddl in ("ALTER TABLE {s}.retail_orders ALTER COLUMN status TYPE varchar(20)",
                "CREATE TABLE {s}.retail_promotions (promo_id int)",
                "ALTER TABLE {s}.retail_promotions RENAME TO retail_offers",
                "CREATE OR REPLACE VIEW {s}.retail_sales AS SELECT o.order_id, c.segment, "
                "o.status FROM {s}.retail_orders o JOIN {s}.retail_customers c "
                "USING (customer_id)",
                "DROP TABLE {s}.retail_offers"):
        run(ddl)
        tokens, moved = conn.table_signals(), conn.change_signal()
        assert tokens != previous and moved != signal, ddl          # whatever moves one…
        previous, signal = tokens, moved                            # …moves the other


def test_describe_relation_reads_one_relation_as_the_bulk_read_does(pg):
    conn, _ = pg
    bulk = conn.describe_relations()
    assert conn.describe_relation("retail_orders") == bulk["retail_orders"]
    assert conn.describe_relation("retail_sales")["kind"] == "view"
    assert conn.describe_relation("no_such_table") is None


@pytest.fixture()
def pg_app(pg, tmp_path, monkeypatch):
    """Studio's own state on Postgres too, as in production: a scratch
    database for it, dropped afterwards."""
    import importlib
    from urllib.parse import urlsplit, urlunsplit

    import psycopg
    conn, run = pg
    name = "mgapp_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    parts = urlsplit(DSN)
    monkeypatch.setenv("DATABASE_URL", urlunsplit(parts._replace(path="/" + name)))
    monkeypatch.setenv("STUDIO_SECRET", "metagraph-secret")
    monkeypatch.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "0")
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, governance, jobs, metacdc, metagraph
    from app.connectors import demo
    try:
        _db.init_db()
        jobs.init_tables()
        metagraph.init_tables()
        connections.init_tables()
        monkeypatch.setitem(connectors._REGISTRY, "pg-test", conn)
        monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
        governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n', "test")
        yield types.SimpleNamespace(db=_db, mg=metagraph, cdc=metacdc, jobs=jobs, run=run)
    finally:
        governance._STATE.update(doc=None, yaml="", source=None)
        _db.close_pool()
        monkeypatch.setenv("DATABASE_URL", "")
        importlib.reload(_db)
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE {name} WITH (FORCE)")


def test_change_capture_end_to_end_on_postgres(pg_app):
    env = pg_app
    assert env.db.IS_PG
    handlers = env.jobs.handlers()

    def drain():
        while True:
            with env.db.connect() as c:
                row = c.execute("SELECT * FROM background_jobs WHERE status='queued' AND kind IN "
                                "('metagraph_event', 'metagraph_refresh') "
                                "ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return
            handlers[row["kind"]](json.loads(row["payload"]),
                                  {**dict(row), "attempts": row["attempts"] + 1})
            with env.db.connect() as c:
                c.execute("UPDATE background_jobs SET status='done' WHERE id=?", (row["id"],))
                c.commit()

    def events():
        with env.db.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM meta_events WHERE source='pg-test' ORDER BY received_at").fetchall()]

    def columns(table):
        rel = next(r for r in env.mg.get_source("pg-test", user=ADMIN)["relations"]
                   if r["name"] == table)
        return [c["name"] for c in rel["columns"]]

    env.mg.discover("pg-test", "test")
    assert env.cdc.offsets("pg-test")[0]                              # the handoff
    assert env.mg.tick_once()["queued"].get("pg-test") is None
    env.run("ALTER TABLE {s}.retail_orders ADD COLUMN channel text")
    assert env.mg.tick_once()["queued"].get("pg-test") is None      # no full rediscovery
    [e] = events()
    assert (e["asset"], e["event_type"], e["origin"]) == ("retail_orders", "schema_changed",
                                                          "poller")
    drain()
    assert columns("retail_orders")[-1] == "channel"
    assert events()[0]["status"] == "applied"

    # Duplicates on Postgres: ON CONFLICT DO NOTHING reports no row.
    first = env.cdc.ingest([{"event_id": "pg-1", "source": "pg-test", "asset": "retail_orders",
                             "event_type": "data_changed", "occurred_at": 2_000}], "webhook")
    again = env.cdc.ingest([{"event_id": "pg-1", "source": "pg-test", "asset": "retail_orders",
                             "event_type": "data_changed", "occurred_at": 2_000}], "webhook")
    assert first["accepted"] == ["pg-1"] and again["duplicates"] == ["pg-1"]
    env.cdc.ingest([{"event_id": "pg-0", "source": "pg-test", "asset": "retail_orders",
                     "event_type": "data_changed", "occurred_at": 1_000}], "webhook")
    drain()
    orders = next(r for r in env.mg.get_source("pg-test", user=ADMIN)["relations"]
                  if r["name"] == "retail_orders")
    assert orders["data_changed_at"] == 2_000

    env.run("DROP TABLE {s}.retail_visits")
    env.mg.tick_once()
    drain()
    assert "retail_visits" not in {r["name"] for r in
                                   env.mg.get_source("pg-test", user=ADMIN)["relations"]}
    assert [r["name"] for r in env.mg.get_source("pg-test", user=ADMIN)["removed"]] == \
        ["retail_visits"]
    settled = [e for e in events() if e["status"] in ("applied", "superseded", "ignored")]
    assert len(settled) == 4
    assert env.cdc.prune(time.time() + 365 * 86400) == 4 and events() == []
    assert env.mg._purge_tombstones(time.time() + 365 * 86400) == 1


class _OnlyTheContract:
    """A real Postgres source seen through nothing but the contract every
    connector meets — what any other database looks like to change capture."""
    dialect = "postgres"

    def __init__(self, inner, name):
        self.inner, self.name = inner, name

    def configured(self):
        return True

    def list_tables(self):
        return self.inner.list_tables()

    def get_schema(self, table):
        return self.inner.get_schema(table)

    def qualifiers(self):
        return self.inner.qualifiers()


def test_change_capture_needs_only_the_connector_contract(pg_app, monkeypatch):
    from app import connectors
    env = pg_app
    bare = _OnlyTheContract(connectors._REGISTRY["pg-test"], "pg-bare")
    monkeypatch.setitem(connectors._REGISTRY, "pg-bare", bare)
    env.mg.discover("pg-bare", "test")
    env.run("ALTER TABLE {s}.retail_customers ADD COLUMN region text")
    env.run("CREATE TABLE {s}.retail_coupons (coupon_id int)")
    assert env.mg.tick_once()["queued"].get("pg-bare") is None
    with env.db.connect() as c:
        got = {(r["asset"], r["event_type"]) for r in c.execute(
            "SELECT * FROM meta_events WHERE source='pg-bare'").fetchall()}
    assert got == {("retail_customers", "schema_changed"), ("retail_coupons", "asset_created")}
    handler = env.jobs.handlers()["metagraph_event"]
    with env.db.connect() as c:
        rows = c.execute("SELECT * FROM background_jobs WHERE kind='metagraph_event' AND "
                         "status='queued'").fetchall()
    for row in rows:
        handler(json.loads(row["payload"]), {**dict(row), "attempts": 1})
    rels = {r["name"]: r for r in env.mg.get_source("pg-bare", user=ADMIN)["relations"]}
    assert [c["name"] for c in rels["retail_customers"]["columns"]][-1] == "region"
    assert "retail_coupons" in rels
    assert env.cdc.status("pg-bare")["capture"] == "probe"


# ── Any SQL database: real Postgres through one SQLAlchemy URL ───────────

def test_any_sql_database_reads_real_postgres_through_one_url(pg):
    from app import queryguard
    from app.connectors import sql_conn
    from app.connectors.base import unguarded
    conn, run = pg
    schema = conn._schema()
    url = DSN.replace("postgresql://", "postgresql+psycopg://", 1)
    sql_conn._LEARNED.clear()
    other = sql_conn.SqlConnector("pg-any", url, schema=schema)    # not the login's default
    assert other.dialect == "postgresql" and other.dialect.identifier_rule == "lower"
    assert other.sql_row_limit
    names = other.list_tables()
    assert {f"{schema}.retail_orders", f"{schema}.retail_sales"} <= set(names)
    assert other.qualifiers() is queryguard.FULL_NAMES
    keys = other.describe_constraints()
    assert keys["primary_keys"][f"{schema}.retail_stores"] == ["store_id"]
    assert keys["foreign_keys"][0]["from"] == f"{schema}.retail_visits"
    assert "retail_orders" in other.view_definitions()[f"{schema}.retail_sales"]
    quals, dialect = other.qualifiers(), other.dialect
    queryguard.validate(f"SELECT * FROM {schema}.retail_orders", names, qualifiers=quals,
                        dialect=dialect)
    for bad in ("SELECT * FROM retail_orders",                    # the default schema's
                f'SELECT * FROM {schema}."RETAIL_ORDERS"'):       # not the same table here
        with pytest.raises(queryguard.QueryRejected):
            queryguard.validate(bad, names, qualifiers=quals, dialect=dialect)
    run("INSERT INTO {s}.retail_orders VALUES (1, 1, '2026-10-01', 'ok')")
    with unguarded():
        cols, rows = other.run_query(f"SELECT COUNT(*) AS n FROM {schema}.retail_orders")
    assert cols == ["n"] and rows == [[1]]
    other.close()

    run("CREATE TABLE public.{s}_probe (id int)")
    try:
        pub = sql_conn.SqlConnector("pg-pub", url)                 # the default schema
        assert f"{schema}_probe" in pub.list_tables()             # bare names
        assert pub.qualifiers() == frozenset({"public", "mgscratch.public"})
        pub.close()
    finally:
        run("DROP TABLE public.{s}_probe")


def test_the_metadata_graph_over_any_sql_database(pg_app, monkeypatch):
    from app import connectors
    from app.connectors import sql_conn
    env = pg_app
    schema = connectors._REGISTRY["pg-test"]._schema()
    url = DSN.replace("postgresql://", "postgresql+psycopg://", 1)
    sql_conn._LEARNED.clear()
    monkeypatch.setitem(connectors._REGISTRY, "pg-any",
                        sql_conn.SqlConnector("pg-any", url, schema=schema))
    env.mg.discover("pg-any", "test")
    detail = env.mg.get_source("pg-any", user=ADMIN)
    assert detail["kinds"] == {"table": 4, "view": 1}
    rels = env.mg.source_relationships("pg-any", user=ADMIN)["relationships"]
    joins = {(r["from_object"], r["to_object"], r["state"]) for r in rels if r["kind"] == "join"}
    assert (f"{schema}.retail_visits", f"{schema}.retail_stores", "declared") in joins
    env.run("ALTER TABLE {s}.retail_stores ADD COLUMN region text")
    assert env.mg.tick_once()["queued"].get("pg-any") is None
    with env.db.connect() as c:
        rows = c.execute("SELECT * FROM background_jobs WHERE kind='metagraph_event'").fetchall()
    for row in rows:
        env.jobs.handlers()["metagraph_event"](json.loads(row["payload"]),
                                               {**dict(row), "attempts": 1})
    stores = next(r for r in env.mg.get_source("pg-any", user=ADMIN)["relations"]
                  if r["name"] == f"{schema}.retail_stores")
    assert [c["name"] for c in stores["columns"]][-1] == "region"
    assert env.cdc.status("pg-any")["capture"] == "bulk"


# ── Descriptions: COMMENT ON, read by both connectors ────────────────────

def test_comments_are_read_as_descriptions_and_move_the_token(pg):
    from app.connectors import sql_conn
    conn, run = pg
    before = conn.table_signals()
    run("COMMENT ON TABLE {s}.retail_orders IS 'One row per order as placed'")
    run("COMMENT ON COLUMN {s}.retail_orders.status IS 'placed, shipped or cancelled'")
    after = conn.table_signals()
    assert [t for t in before if before[t] != after[t]] == ["retail_orders"]
    bulk = conn.describe_relations()["retail_orders"]
    one = conn.describe_relation("retail_orders")
    for got in (bulk, one):
        assert got["description"] == "One row per order as placed"
        status = next(c for c in got["columns"] if c["name"] == "status")
        assert status["description"] == "placed, shipped or cancelled"
    assert "description" not in conn.describe_relations()["retail_customers"]
    sql_conn._LEARNED.clear()
    generic = sql_conn.SqlConnector("pg-any", DSN.replace("postgresql://",
                                                          "postgresql+psycopg://", 1),
                                    schema=conn._schema())
    name = f"{conn._schema()}.retail_orders"
    assert generic.describe_relations()[name]["description"] == "One row per order as placed"
    assert generic.describe_relation(name)["description"] == "One row per order as placed"
    status = next(c for c in generic.get_schema(name) if c["name"] == "status")
    assert status["description"] == "placed, shipped or cancelled"
    generic.close()
