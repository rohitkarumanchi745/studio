"""Metadata graph, phase 01: automatic source discovery.

Saving a connection queues discovery in the same transaction; the job lists
the source's tables and views, reads their columns and records a snapshot;
the graph is readable per role and mirrored to Neo4j when one is configured.

The retail source here is SYNTHETIC — a SQLite stand-in shaped like
studio_dev.retail_qa (seven tables plus a view, listed the way Databricks lists
them: views included). The real catalog is checked on the deployment.
"""
import importlib
import json
import sqlite3
import types

import pytest

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
VIEWER = {"id": "u-viewer", "email": "viewer@studio.test", "role": "viewer", "name": "Viewer"}

RETAIL_TABLES = {
    "retail_customers": "customer_id INTEGER, customer_name TEXT, segment TEXT, region TEXT",
    "retail_products": "product_id INTEGER, product_name TEXT, category TEXT, unit_price REAL",
    "retail_stores": "store_id INTEGER, store_name TEXT, region TEXT",
    "retail_orders": "order_id INTEGER, customer_id INTEGER, store_id INTEGER, "
                     "order_date TEXT, status TEXT",
    "retail_order_items": "order_item_id INTEGER, order_id INTEGER, product_id INTEGER, "
                          "quantity INTEGER, net_sales REAL",
    "retail_returns": "return_id INTEGER, order_item_id INTEGER, return_date TEXT, reason TEXT",
    "retail_inventory": "store_id INTEGER, product_id INTEGER, on_hand INTEGER, as_of_date TEXT",
}
RETAIL_VIEW = ("CREATE VIEW retail_sales_detail AS SELECT o.order_id, o.order_date, o.status, "
               "c.segment, s.store_name, p.product_name, p.category, i.quantity, i.net_sales "
               "FROM retail_orders o JOIN retail_customers c ON c.customer_id = o.customer_id "
               "JOIN retail_stores s ON s.store_id = o.store_id "
               "JOIN retail_order_items i ON i.order_id = o.order_id "
               "JOIN retail_products p ON p.product_id = i.product_id")


class RetailConnector:
    """A Databricks-shaped source over SQLite: list_tables() includes the view,
    describe_relations() reports kinds and columns in bulk."""
    dialect = "sqlite"
    tables_truncated = False

    def __init__(self, path, name="retail", bulk=True):
        self.path, self.name, self.bulk = path, name, bulk
        self.fail_listing = False
        self.fail_columns = set()

    def configured(self):
        return True

    def _c(self):
        return sqlite3.connect(self.path)

    def list_tables(self):
        if self.fail_listing:
            raise RuntimeError("warehouse unreachable")
        with self._c() as c:
            return [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name")]

    def get_schema(self, table):
        if table in self.fail_columns:
            raise RuntimeError("permission denied on DESCRIBE")
        with self._c() as c:
            return [{"name": r[1], "type": r[2]} for r in c.execute(f'PRAGMA table_info("{table}")')]

    def describe_relations(self):
        if not self.bulk:
            return None
        with self._c() as c:
            kinds = dict(c.execute("SELECT name, type FROM sqlite_master "
                                   "WHERE type IN ('table','view')").fetchall())
        return {n: {"kind": k, "namespace": "studio_dev.retail_qa",
                    "columns": self.get_schema(n)} for n, k in kinds.items()}

    def qualifiers(self):
        return frozenset()


def make_retail(path):
    with sqlite3.connect(path) as c:
        for name, cols in RETAIL_TABLES.items():
            c.execute(f"CREATE TABLE {name} ({cols})")
        c.execute(RETAIL_VIEW)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "metagraph.db"))
    monkeypatch.setenv("STUDIO_SECRET", "metagraph-secret")
    for k in ("METAGRAPH_NEO4J_URI", "METAGRAPH_NEO4J_USER", "METAGRAPH_NEO4J_PASSWORD",
              "NEO4J_URI", "NEO4J_DATABASE", "METAGRAPH_NEO4J_DATABASE"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, governance, jobs, metagraph, migrations, rbac
    _db.init_db()
    jobs.init_tables()
    metagraph.init_tables()
    connections.init_tables()
    warehouse = tmp_path / "retail.db"
    make_retail(warehouse)
    retail = RetailConnector(str(warehouse))
    monkeypatch.setitem(connectors._REGISTRY, "retail", retail)
    governance._set("version: 1\nroles:\n  admin: { sources: \"*\" }\n"
                    "  viewer: { sources: { retail: [retail_orders, retail_products] } }\n", "test")
    yield types.SimpleNamespace(db=_db, mg=metagraph, jobs=jobs, connections=connections,
                                migrations=migrations, rbac=rbac, retail=retail,
                                warehouse=str(warehouse), mp=monkeypatch)
    governance._STATE.update(doc=None, yaml="", source=None)


def run_job(env, source="retail", trigger="test"):
    return env.jobs.handlers()["metagraph_discover"]({"source": source, "trigger": trigger}, None)


def stored(env, source="retail"):
    with env.db.connect() as c:
        return {r["name"]: dict(r) for r in c.execute(
            "SELECT * FROM meta_objects WHERE source=?", (source,)).fetchall()}


# ── Discovery ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bulk", [True, False])
def test_discovers_all_seven_retail_tables_plus_the_view(env, bulk):
    env.retail.bulk = bulk
    out = run_job(env)
    assert out["objects"] == 8 and out["added"] == 8 and out["complete"]
    objs = stored(env)
    assert set(objs) == set(RETAIL_TABLES) | {"retail_sales_detail"}
    if bulk:
        assert objs["retail_sales_detail"]["kind"] == "view"
        assert {objs[t]["kind"] for t in RETAIL_TABLES} == {"table"}
        assert objs["retail_orders"]["namespace"] == "studio_dev.retail_qa"
    else:
        assert {o["kind"] for o in objs.values()} == {"unknown"}   # no bulk metadata: unknown
    assert objs["retail_orders"]["column_count"] == 5
    detail = env.mg.get_source("retail", user=ADMIN)
    assert detail["kinds"] == ({"table": 7, "view": 1} if bulk else {"unknown": 8})
    orders = next(r for r in detail["relations"] if r["name"] == "retail_orders")
    assert [c["name"] for c in orders["columns"]] == [
        "order_id", "customer_id", "store_id", "order_date", "status"]


def test_saving_a_connection_queues_discovery_with_the_row(env):
    env.mp.setattr(env.connections, "_probe", lambda ctype, cfg: {"ok": True})
    env.mp.setattr(env.connections, "_require_pin", lambda *a, **k: None)
    env.connections._create(ADMIN, "retail-qa", "postgres",
                            {"host": "h", "database": "d", "user": "u", "password": "p",
                             "schema": "retail_qa"},
                            {"host": "h", "database": "d", "user": "u", "password": "p",
                             "schema": "retail_qa"})
    with env.db.connect() as c:
        job = c.execute("SELECT kind, payload FROM background_jobs").fetchone()
    assert job["kind"] == "metagraph_discover"
    assert json.loads(job["payload"]) == {"source": "retail-qa", "trigger": "connection_saved"}
    assert env.mg._state("retail-qa")["status"] == "queued"     # with the row, same commit


def test_a_failed_save_queues_nothing(env):
    env.mp.setattr(env.connections, "_probe", lambda ctype, cfg: {"ok": False, "error": "no"})
    env.mp.setattr(env.connections, "_require_pin", lambda *a, **k: None)
    with pytest.raises(Exception):
        env.connections._create(ADMIN, "retail-qa", "postgres", {"host": "h"}, {"host": "h"})
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM background_jobs").fetchone()["n"] == 0


def test_a_rerun_records_what_changed(env):
    run_job(env)
    again = run_job(env)
    assert (again["added"], again["changed"], again["removed"]) == (0, 0, 0)
    with sqlite3.connect(env.warehouse) as c:
        c.execute("ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
        c.execute("DROP TABLE retail_returns")
        c.execute("CREATE TABLE retail_promotions (promo_id INTEGER, name TEXT)")
    out = run_job(env)
    assert (out["added"], out["changed"], out["removed"]) == (1, 1, 1)
    objs = stored(env)
    assert objs["retail_returns"]["removed_at"] is not None
    assert objs["retail_stores"]["column_count"] == 4
    names = {r["name"] for r in env.mg.get_source("retail", user=ADMIN)["relations"]}
    assert "retail_returns" not in names and "retail_promotions" in names


def test_a_failed_listing_changes_nothing(env):
    run_job(env)
    before = stored(env)
    env.retail.fail_listing = True
    with pytest.raises(RuntimeError):
        run_job(env)
    assert stored(env) == before
    state = env.mg._state("retail")
    assert state["status"] == "failed" and "warehouse unreachable" in state["error"]
    with env.db.connect() as c:
        snap = c.execute("SELECT status FROM meta_snapshots ORDER BY started_at DESC").fetchone()
    assert snap["status"] == "failed"


def test_unreadable_columns_never_remove_or_rewrite_an_object(env):
    env.retail.bulk = False
    run_job(env)
    before = stored(env)["retail_orders"]
    with sqlite3.connect(env.warehouse) as c:
        c.execute("DROP TABLE retail_inventory")
    env.retail.fail_columns = {"retail_orders"}
    out = run_job(env)
    assert not out["complete"] and out["removed"] == 0          # incomplete: no removals
    after = stored(env)
    assert after["retail_orders"]["fingerprint"] == before["fingerprint"]
    assert after["retail_inventory"]["removed_at"] is None
    assert env.mg._state("retail")["status"] == "partial"


def test_a_truncated_listing_never_removes(env):
    run_job(env)
    env.mp.setenv("STUDIO_METAGRAPH_MAX_OBJECTS", "3")
    out = run_job(env)
    assert not out["complete"] and out["removed"] == 0
    assert all(o["removed_at"] is None for o in stored(env).values())


def test_untrusted_names_are_stored_as_data(env):
    with sqlite3.connect(env.warehouse) as c:
        c.execute('CREATE TABLE "x\'); DROP TABLE meta_objects;--" ("a""b" TEXT)')
    run_job(env)
    assert "x'); DROP TABLE meta_objects;--" in stored(env)


# ── Reading it back, per role ────────────────────────────────────────────

def test_a_role_sees_only_what_it_may_read(env):
    run_job(env)
    viewer = env.mg.get_source("retail", user=VIEWER)
    assert {r["name"] for r in viewer["relations"]} == {"retail_orders", "retail_products"}
    assert viewer["objects"] == 2 and viewer["kinds"] == {"table": 2}   # counts leak nothing
    assert "error" not in viewer and "mirror_status" not in viewer
    graph = env.mg.source_graph("retail", user=VIEWER)
    objects = {n["name"] for n in graph["nodes"] if n["type"] in ("table", "view")}
    assert objects == {"retail_orders", "retail_products"}
    assert not any("retail_customers" in n["id"] or "retail_sales_detail" in n["id"]
                   for n in graph["nodes"])
    listed = {s["source"]: s for s in env.mg.list_sources(user=VIEWER)["sources"]}
    assert listed["retail"]["objects"] == 2


def test_a_source_the_role_cannot_use_is_not_found(env):
    run_job(env)
    from fastapi import HTTPException
    analyst = {"id": "u-a", "email": "a@x", "role": "analyst", "name": "A"}
    with pytest.raises(HTTPException) as e:
        env.mg.get_source("retail", user=analyst)
    assert e.value.status_code == 404
    assert "retail" not in {s["source"] for s in env.mg.list_sources(user=analyst)["sources"]}


def test_only_admins_can_request_discovery(env):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        env.mg.request_discovery("retail", user=VIEWER)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        env.mg.request_discovery("nope", user=ADMIN)
    assert e.value.status_code == 404
    queued = env.mg.request_discovery("retail", user=ADMIN)
    with env.db.connect() as c:
        row = c.execute("SELECT kind FROM background_jobs WHERE id=?",
                        (queued["job_id"],)).fetchone()
    assert row["kind"] == "metagraph_discover"


def test_the_graph_has_source_namespace_object_and_column_nodes(env):
    run_job(env)
    g = env.mg.source_graph("retail", user=ADMIN)
    types_ = {n["type"] for n in g["nodes"]}
    assert types_ == {"source", "namespace", "table", "view", "column"}
    assert {"from": "namespace:retail:studio_dev.retail_qa", "to": "object:retail:retail_orders",
            "type": "CONTAINS"} in g["edges"]
    assert {"from": "object:retail:retail_orders", "to": "column:retail:retail_orders:order_id",
            "type": "HAS_COLUMN"} in g["edges"]


# ── Backfill for sources saved before discovery existed ──────────────────

def test_migration_queues_one_discovery_per_enabled_saved_connection(env):
    with env.db.connect() as c:
        for name, enabled in (("dbx-retail", 1), ("parked", 0)):
            c.execute("INSERT INTO data_connections (id, name, ctype, label, config, created_by, "
                      "created_at, enabled) VALUES (?,?,?,?,?,?,?,?)",
                      (name, name, "databricks", "", "x", "u", 0, enabled))
        c.commit()
    for _ in range(2):                                      # idempotent
        with env.db.connect() as c:
            env.migrations._m17_metagraph_backfill(c, env.db.IS_PG)
            c.commit()
    with env.db.connect() as c:
        rows = c.execute("SELECT id, payload FROM background_jobs").fetchall()
    assert [(r["id"], json.loads(r["payload"])) for r in rows] == [
        ("metagraph-backfill-dbx-retail", {"source": "dbx-retail", "trigger": "backfill"})]


# ── Neo4j mirror ─────────────────────────────────────────────────────────

class FakeNeo4j:
    """Records each transaction posted to Neo4j's HTTP endpoint."""
    def __init__(self):
        self.requests, self.errors = [], []


@pytest.fixture()
def neo4j(env, monkeypatch):
    fake = FakeNeo4j()

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        fake.requests.append({"url": req.full_url, "body": json.loads(req.data.decode())})
        return Resp(json.dumps({"results": [], "errors": fake.errors}).encode())
    monkeypatch.setattr(env.mg.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("METAGRAPH_NEO4J_URI", "https://graph.example:7473")
    monkeypatch.setenv("METAGRAPH_NEO4J_USER", "studio")
    monkeypatch.setenv("METAGRAPH_NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("METAGRAPH_NEO4J_DATABASE", "metadata")
    monkeypatch.setenv("STUDIO_METAGRAPH_TENANT", "studio")     # the mirror needs its own name
    return fake


def test_discovery_populates_neo4j_in_one_transaction(env, neo4j):
    run_job(env)
    assert len(neo4j.requests) == 1
    req = neo4j.requests[0]
    assert req["url"] == "https://graph.example:7473/db/metadata/tx/commit"
    statements = req["body"]["statements"]
    params = statements[1]["parameters"]
    assert {o["name"] for o in params["objects"]} == set(RETAIL_TABLES) | {"retail_sales_detail"}
    assert {"object": "retail_orders", "name": "order_id", "type": "INTEGER", "ordinal": 0} \
        in params["columns"]
    # Names travel only as parameters, never inside the Cypher text.
    assert not any("retail_" in s["statement"] for s in statements)
    assert env.mg._state("retail")["mirror_status"] == "ok"


def test_neo4j_mirror_removes_dropped_objects(env, neo4j):
    run_job(env)
    with sqlite3.connect(env.warehouse) as c:
        c.execute("DROP TABLE retail_returns")
    run_job(env)
    removal = next(s for s in neo4j.requests[-1]["body"]["statements"]
                   if "WHERE NOT t.name IN $names DETACH DELETE t" in s["statement"])
    assert "retail_returns" not in removal["parameters"]["names"]


def test_a_neo4j_failure_is_recorded_and_never_fails_discovery(env, neo4j):
    neo4j.errors = [{"code": "Neo.ClientError.Security.Unauthorized", "message": "bad auth"}]
    out = run_job(env)
    assert out["objects"] == 8
    state = env.mg._state("retail")
    assert state["status"] == "ok" and state["mirror_status"] == "failed"
    assert "Unauthorized" in state["mirror_error"]


def test_the_mirror_refuses_the_database_agents_query(env, neo4j, monkeypatch):
    monkeypatch.setenv("NEO4J_URI", "https://graph.example:7473")
    monkeypatch.setenv("NEO4J_DATABASE", "metadata")
    run_job(env)
    assert neo4j.requests == []
    assert env.mg._state("retail")["mirror_status"] == "refused"


def test_without_neo4j_the_mirror_is_off(env):
    run_job(env)
    assert env.mg._state("retail")["mirror_status"] == "off"
