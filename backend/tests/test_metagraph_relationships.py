"""Metadata graph, phase 03: automatic relationship discovery.

Acceptance: Studio retrieves the four known join paths among the five core
retail tables (customers, orders, order_items, products, stores), each with
evidence labels — on a source that declares no keys, as Databricks schemas
usually don't. Also: declared keys, view lineage, joins seen in Studio's own
queries, confidence states, reviews that outlive recomputation, permission
filtering, and the Neo4j mirror.
"""
import json
import sqlite3
import time

import pytest

from app import metarelations as mr
from test_metagraph_discovery import (ADMIN, VIEWER, RetailConnector,  # noqa: F401
                                      env, neo4j)

CORE = ["retail_customers", "retail_orders", "retail_order_items", "retail_products",
        "retail_stores"]
KNOWN = {("retail_orders", "customer_id", "retail_customers", "customer_id"),
         ("retail_orders", "store_id", "retail_stores", "store_id"),
         ("retail_order_items", "order_id", "retail_orders", "order_id"),
         ("retail_order_items", "product_id", "retail_products", "product_id")}


class RetailWithViews(RetailConnector):
    """Like Databricks: views listed, their SQL readable, no declared keys."""
    def view_definitions(self):
        with self._c() as c:
            return dict(c.execute("SELECT name, sql FROM sqlite_master WHERE type='view'"))


@pytest.fixture()
def rel(env):
    conn = RetailWithViews(env.warehouse)
    from app import connectors
    env.mp.setitem(connectors._REGISTRY, "retail", conn)
    env.retail = conn
    return env


def discover(env):
    return env.jobs.handlers()["metagraph_discover"]({"source": "retail", "trigger": "t"}, None)


def joins(env, user=ADMIN):
    rels = env.mg.source_relationships("retail", user=user)["relationships"]
    return {(r["from_object"], r["from_columns"][0], r["to_object"], r["to_columns"][0]): r
            for r in rels if r["kind"] == "join"}


# ── The acceptance ───────────────────────────────────────────────────────

def test_the_four_known_join_paths_among_the_five_core_tables(rel):
    out = discover(rel)
    assert out["relationships"]["relationships"] >= 4
    found = joins(rel)
    among_core = {k for k in found if k[0] in CORE and k[2] in CORE}
    assert among_core == KNOWN
    for key in KNOWN:
        r = found[key]
        labels = [e["label"] for e in r["evidence"]]
        assert r["state"] == "observed"                     # the view joins them this way
        assert any(lbl.startswith("name pattern:") for lbl in labels), labels
        assert "joined this way in view retail_sales_detail" in labels, labels
        assert r["confidence"] == 0.9 and r["directed"]
    paths = rel.mg.source_paths("retail", tables=",".join(CORE), user=ADMIN)
    assert paths["connected"] and len(paths["joins"]) == 4
    assert {(j["from_object"], j["to_object"]) for j in paths["joins"]} == \
        {(a, c) for a, _, c, _ in KNOWN}


def test_the_view_is_derived_from_its_tables(rel):
    discover(rel)
    rels = rel.mg.source_relationships("retail", user=ADMIN)["relationships"]
    lineage = {r["to_object"] for r in rels
               if r["kind"] == "derived_from" and r["from_object"] == "retail_sales_detail"}
    assert lineage == set(CORE)


def test_other_true_relationships_are_inferred_not_invented(rel):
    discover(rel)
    found = joins(rel)
    # Real references outside the core five, found by naming alone:
    assert found[("retail_returns", "order_item_id", "retail_order_items",
                  "order_item_id")]["state"] == "inferred"
    assert ("retail_inventory", "store_id", "retail_stores", "store_id") in found
    # Same-named non-key columns are not joins; siblings are not linked directly.
    assert not any("region" in (k[1], k[3]) for k in found)
    assert not any({k[0], k[2]} == {"retail_orders", "retail_inventory"} for k in found)


# ── Evidence kinds and states ────────────────────────────────────────────

def test_declared_keys_are_declared(rel, tmp_path):
    path = tmp_path / "declared.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("CREATE TABLE orders (order_id INTEGER PRIMARY KEY, buyer INTEGER "
                  "REFERENCES customers(id))")
    from app.connectors.demo import DemoConnector

    class Declared(RetailConnector):
        def describe_constraints(self):
            demo = DemoConnector()
            demo._conn = lambda: sqlite3.connect(path)
            return demo.describe_constraints()
    from app import connectors
    rel.mp.setitem(connectors._REGISTRY, "retail", Declared(str(path)))
    discover(rel)
    found = joins(rel)
    r = found[("orders", "buyer", "customers", "id")]           # no name pattern needed
    assert r["state"] == "declared" and r["confidence"] == 1.0
    assert r["evidence"][0]["label"].startswith("declared foreign key")


def test_joins_in_studios_own_queries_count_as_evidence(rel):
    with rel.db.connect() as c:
        for i in range(3):
            c.execute("INSERT INTO audit_log (id, user_id, email, role, action, source, sql, ok, "
                      "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (f"a{i}", "u", "u@x", "analyst", "agent_sql", "retail",
                       "SELECT s.region, COUNT(*) FROM retail_inventory inv "
                       "JOIN retail_stores s ON s.store_id = inv.store_id GROUP BY 1", 1,
                       time.time()))
        c.execute("INSERT INTO audit_log (id, user_id, email, role, action, source, sql, ok, "
                  "created_at) VALUES ('bad','u','u@x','analyst','agent_sql','retail',"
                  "'SELECT * FROM retail_inventory i JOIN retail_products p ON p.product_id = "
                  "i.product_id',0,0)")
        c.commit()
    discover(rel)
    found = joins(rel)
    inv = found[("retail_inventory", "store_id", "retail_stores", "store_id")]
    assert inv["state"] == "observed"
    assert "joined this way in 3 successful Studio queries" in [e["label"] for e in inv["evidence"]]
    failed = found[("retail_inventory", "product_id", "retail_products", "product_id")]
    assert failed["state"] == "inferred"                       # a failed query is no evidence


def test_type_mismatch_lowers_confidence():
    objects = {"orders": {"kind": "table", "columns": [{"name": "customer_id", "type": "string"}]},
               "customers": {"kind": "table", "columns": [{"name": "customer_id",
                                                           "type": "bigint"}]}}
    (key, ev), = mr.infer(objects, {}).items()
    assert ev[0]["confidence"] < 0.6 and "types differ" in ev[0]["label"]


def test_a_review_outlives_recomputation(rel):
    discover(rel)
    found = joins(rel)
    rid = found[("retail_orders", "store_id", "retail_stores", "store_id")]["id"]
    rel.mg.review_relationship("retail", rid, {"decision": "rejected"}, user=ADMIN)
    discover(rel)
    assert ("retail_orders", "store_id", "retail_stores", "store_id") not in joins(rel, VIEWER)
    admin_view = joins(rel)[("retail_orders", "store_id", "retail_stores", "store_id")]
    assert admin_view["state"] == "rejected"
    paths = rel.mg.source_paths("retail", tables="retail_orders,retail_stores", user=ADMIN)
    assert ("retail_orders", "retail_stores") not in {(j["from_object"], j["to_object"])
                                                      for j in paths["joins"]}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        rel.mg.review_relationship("retail", rid, {"decision": "verified"}, user=VIEWER)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        rel.mg.review_relationship("retail", rid, {"decision": "maybe"}, user=ADMIN)
    assert e.value.status_code == 400


def test_a_verified_relationship_survives_losing_its_evidence(rel):
    discover(rel)
    rid = joins(rel)[("retail_returns", "order_item_id", "retail_order_items",
                      "order_item_id")]["id"]
    rel.mg.review_relationship("retail", rid, {"decision": "verified"}, user=ADMIN)
    with sqlite3.connect(rel.warehouse) as c:              # rename: the pattern no longer holds
        c.execute("ALTER TABLE retail_returns RENAME COLUMN order_item_id TO line_ref")
    discover(rel)
    kept = {r["id"]: r for r in rel.mg.source_relationships("retail", user=ADMIN)
            ["relationships"]}
    assert kept[rid]["state"] == "verified" and kept[rid]["confidence"] == 1.0


def test_a_dropped_table_takes_its_relationships_with_it(rel):
    discover(rel)
    with sqlite3.connect(rel.warehouse) as c:
        c.execute("DROP TABLE retail_inventory")
    discover(rel)
    assert not any("retail_inventory" in (k[0], k[2]) for k in joins(rel))


# ── Permissions ──────────────────────────────────────────────────────────

def test_a_role_sees_only_relationships_between_tables_it_may_read(rel):
    discover(rel)
    assert joins(rel, VIEWER) == {}                 # orders + products: never joined directly
    paths = rel.mg.source_paths("retail", tables="retail_orders,retail_customers", user=VIEWER)
    assert not paths["connected"] and paths["unknown"] == ["retail_customers"]
    assert paths["joins"] == []
    assert rel.mg.get_source("retail", user=VIEWER)["relationships"] == 0
    assert rel.mg.get_source("retail", user=ADMIN)["relationships"] >= 7


def test_a_view_name_is_hidden_from_a_role_that_cannot_see_the_view(rel):
    from app import governance          # the env fixture resets it afterwards
    governance._set("version: 1\nroles:\n  admin: { sources: \"*\" }\n  viewer: { sources: "
                    "{ retail: [retail_orders, retail_order_items] } }\n", "test")
    discover(rel)
    r = joins(rel, VIEWER)[("retail_order_items", "order_id", "retail_orders", "order_id")]
    labels = [e["label"] for e in r["evidence"]]
    assert "joined this way in a view definition" in labels
    assert not any("retail_sales_detail" in lbl for lbl in labels)
    assert r["state"] == "observed"                       # the evidence still counts
    admin = joins(rel)[("retail_order_items", "order_id", "retail_orders", "order_id")]
    assert "joined this way in view retail_sales_detail" in [e["label"] for e in admin["evidence"]]


def test_joins_in_views_outside_the_catalog_still_count(rel):
    real = rel.retail.list_tables
    rel.retail.list_tables = lambda: [n for n in real() if n != "retail_sales_detail"]
    discover(rel)                                         # Postgres: base tables only
    r = joins(rel)[("retail_order_items", "product_id", "retail_products", "product_id")]
    assert r["state"] == "observed"
    assert "joined this way in a view definition" in [e["label"] for e in r["evidence"]]


# ── Reading joins out of SQL ─────────────────────────────────────────────

OBJ = ["retail_orders", "retail_customers", "studio_dev.retail_qa.retail_stores"]


def test_extract_joins_reads_aliases_qualified_names_and_using():
    resolve = mr.resolver(OBJ)
    sql = ("SELECT 1 FROM retail_orders AS o JOIN retail_customers c ON (c.customer_id = "
           "o.customer_id) JOIN studio_dev.retail_qa.retail_stores s USING (store_id)")
    out = mr.extract_joins(sql, resolve)
    assert (("retail_customers", "customer_id"), ("retail_orders", "customer_id")) in out
    assert (("retail_customers", "store_id"),
            ("studio_dev.retail_qa.retail_stores", "store_id")) in out


def test_comments_and_strings_cannot_forge_a_join():
    resolve = mr.resolver(OBJ)
    sql = ("SELECT 'o.customer_id = c.customer_id' FROM retail_orders o, retail_customers c "
           "-- o.customer_id = c.customer_id\n WHERE 1 = 1 /* o.x = c.y */")
    assert mr.extract_joins(sql, resolve) == []


def test_unknown_tables_and_self_joins_are_ignored():
    resolve = mr.resolver(OBJ)
    assert mr.extract_joins("SELECT * FROM retail_orders a JOIN retail_orders b "
                            "ON a.order_id = b.order_id", resolve) == []
    assert mr.extract_joins("SELECT * FROM retail_orders o JOIN secret s "
                            "ON s.id = o.customer_id", resolve) == []


# ── Mirror, migration ────────────────────────────────────────────────────

def test_relationships_reach_the_neo4j_mirror(rel, neo4j):
    discover(rel)
    statements = neo4j.requests[-1]["body"]["statements"]
    merge = next(s for s in statements if "MERGE (a)-[e:RELATES" in s["statement"])
    rels = merge["parameters"]["relationships"]
    assert {(r["from"], r["to"]) for r in rels if r["kind"] == "join"} >= \
        {(a, c) for a, _, c, _ in KNOWN}
    assert all("retail_" not in s["statement"] for s in statements)
    assert any("DELETE e" in s["statement"] for s in statements)


def test_migration_adds_the_relate_columns(rel):
    with rel.db.connect() as c:
        c.execute("DROP TABLE meta_sources")
        c.execute("CREATE TABLE meta_sources (source TEXT PRIMARY KEY, status TEXT NOT NULL)")
        rel.migrations._m19_metagraph_relate_columns(c, rel.db.IS_PG)
        c.commit()
        cols = {r[1] for r in c.execute("PRAGMA table_info(meta_sources)").fetchall()}
    assert {"related_at", "relate_error"} <= cols


def test_a_relationship_failure_never_undoes_the_snapshot(rel):
    def boom():
        raise RuntimeError("catalog said no")
    rel.retail.view_definitions = boom                        # evidence reads fail softly
    out = discover(rel)
    assert out["objects"] == 8 and out["relationships"]["relationships"] >= 4
    rel.mp.setattr(mr, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad")))
    out = discover(rel)
    assert out["objects"] == 8 and out["relationships"] is None
    assert "bad" in rel.mg._state("retail")["relate_error"]
