"""Metadata graph, phase 05: cross-source and enterprise rollout.

Acceptance: a verified cross-source question succeeds for authorized users and
does not disclose restricted metadata or records.

Two sources, as an enterprise has them: `crm` (customers keyed by id, plus a
private notes table) and `retail` (orders carrying customer_id). The Customer
entity is proposed across them from naming, verified by an administrator, and
only then joins them — for a role that may read both ends. A role that may
read retail only learns nothing about crm: not its tables, its columns, its
keys, or a single row.
"""
import importlib
import json
import logging
import sqlite3
import time
import types

import pytest
from fastapi import HTTPException

from app.connectors.base import Connector

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANALYST = {"id": "u-analyst", "email": "analyst@studio.test", "role": "analyst", "name": "Ana"}
VIEWER = {"id": "u-viewer", "email": "viewer@studio.test", "role": "viewer", "name": "Vi"}

CRM = {"customers": ("id INTEGER, segment TEXT, region TEXT",
                     [(1, "Enterprise", "West"), (2, "SMB", "East"), (3, "Enterprise", "East")]),
       "crm_private_notes": ("customer_id INTEGER, note TEXT", [(1, "do not disclose")])}
RETAIL = {"retail_orders": ("order_id INTEGER, customer_id INTEGER, net_sales REAL",
                            [(10, 1, 100.0), (11, 1, 50.0), (12, 2, 30.0), (13, 3, 20.0)])}


class SqliteSource(Connector):
    dialect = "sqlite"

    def __init__(self, name, path):
        self.name, self.path = name, path

    def configured(self):
        return True

    def list_tables(self):
        with sqlite3.connect(self.path) as c:
            return [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def get_schema(self, table):
        with sqlite3.connect(self.path) as c:
            return [{"name": r[1], "type": r[2]} for r in c.execute(f'PRAGMA table_info("{table}")')]

    def describe_relations(self):
        return {t: {"kind": "table", "namespace": None, "columns": self.get_schema(t)}
                for t in self.list_tables()}

    def run_query(self, sql):
        with sqlite3.connect(self.path) as c:
            cur = c.execute(sql)
            return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]


def _make(path, tables):
    with sqlite3.connect(path) as c:
        for name, (cols, rows) in tables.items():
            c.execute(f"CREATE TABLE {name} ({cols})")
            for row in rows:
                c.execute(f"INSERT INTO {name} VALUES ({','.join('?' for _ in row)})", row)


POLICY = """version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { crm: [customers], retail: [retail_orders] } }
  viewer: { sources: { retail: [retail_orders] } }
"""


@pytest.fixture()
def two(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "enterprise.db"))
    monkeypatch.setenv("STUDIO_SECRET", "enterprise-secret")
    for k in ("METAGRAPH_NEO4J_URI", "METAGRAPH_NEO4J_USER", "METAGRAPH_NEO4J_PASSWORD",
              "NEO4J_URI", "NEO4J_DATABASE", "STUDIO_METAGRAPH_TENANT"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import (connections, connectors, governance, jobs, metaentities, metagraph,
                     migrations, queries)
    from app.connectors import demo
    _db.init_db()
    for module in (jobs, metagraph, connections, queries, governance):
        module.init_tables()
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo_warehouse.db"))
    _make(tmp_path / "crm.db", CRM)
    _make(tmp_path / "retail.db", RETAIL)
    monkeypatch.setitem(connectors._REGISTRY, "crm", SqliteSource("crm", str(tmp_path / "crm.db")))
    monkeypatch.setitem(connectors._REGISTRY, "retail",
                        SqliteSource("retail", str(tmp_path / "retail.db")))
    governance._set(POLICY, "test")
    with _db.connect() as c:
        for u in (ADMIN, ANALYST, VIEWER):
            c.execute("INSERT INTO users (id, email, password_hash, name, role, verified, "
                      "created_at) VALUES (?,?,?,?,?,1,?)",
                      (u["id"], u["email"], _db.UNUSABLE_PASSWORD_HASH, u["name"], u["role"],
                       time.time()))
        c.commit()
    handler = jobs.handlers()["metagraph_discover"]
    for source in ("crm", "retail"):
        handler({"source": source, "trigger": "test"}, None)
    metagraph.tick_once()                                   # proposes the entities
    yield types.SimpleNamespace(db=_db, mg=metagraph, me=metaentities, migrations=migrations,
                                mp=monkeypatch, governance=governance)
    governance._STATE.update(doc=None, yaml="", source=None)


def customer(env, user):
    return next(e for e in env.mg.list_entities(user=user)["entities"] if e["name"] == "Customer")


def member(ent, source, obj):
    return next(m for m in ent["members"] if m["source"] == source and m["object"] == obj)


def verify(env, *pairs):
    ent = customer(env, ADMIN)
    for source, obj in pairs:
        env.mg.review_entity_member(member(ent, source, obj)["id"], {"decision": "verified"},
                                    user=ADMIN)


PARTS = [{"name": "crm_customers", "source": "crm", "table": "customers",
          "sql": "SELECT id, segment FROM customers"},
         {"name": "retail_sales", "source": "retail", "table": "retail_orders",
          "sql": "SELECT customer_id, net_sales FROM retail_orders"}]


def blend(user, parts=PARTS):
    from app import blend as b
    return b.blend(user, [dict(p) for p in parts])


# ── Proposals ────────────────────────────────────────────────────────────

def test_customer_is_proposed_across_the_two_sources(two):
    ent = customer(two, ADMIN)
    got = {(m["source"], m["object"], m["column"], m["role"], m["state"]) for m in ent["members"]}
    assert got == {("crm", "customers", "id", "home", "proposed"),
                   ("crm", "crm_private_notes", "customer_id", "reference", "proposed"),
                   ("retail", "retail_orders", "customer_id", "reference", "proposed")}
    labels = member(ent, "crm", "customers")["evidence"]
    assert "table customers is named for customer and keyed by id" in labels
    assert "found in 2 sources" in labels


def test_a_proposal_joins_nothing(two):
    with pytest.raises(HTTPException) as e:
        blend(ANALYST)
    assert e.value.status_code == 400 and "common column" in e.value.detail
    assert two.me.bridge_lines(ANALYST, ["crm", "retail"]) == []


# ── The acceptance ───────────────────────────────────────────────────────

def test_a_verified_cross_source_question_succeeds_for_an_authorized_user(two):
    verify(two, ("crm", "customers"), ("retail", "retail_orders"))
    out = blend(ANALYST)
    assert out["join_basis"] == ("joined on the verified Customer key (crm.crm_customers.id = "
                                 "retail.retail_sales.customer_id)")
    by_segment = {}
    idx = {c: i for i, c in enumerate(out["columns"])}
    for row in out["rows"]:
        by_segment[row[idx["segment"]]] = by_segment.get(row[idx["segment"]], 0) + \
            row[idx["net_sales"]]
    assert by_segment == {"Enterprise": 170.0, "SMB": 30.0}          # net sales by segment
    assert two.me.bridge_lines(ANALYST, ["crm", "retail"]) == [
        "Customer: crm.customers.id = retail.retail_orders.customer_id  (verified)"]


def test_the_planners_are_told_the_verified_key(two, monkeypatch):
    verify(two, ("crm", "customers"), ("retail", "retail_orders"))
    from app import agent, agent_graph
    seen = []

    class FakeLLM:
        def invoke(self, messages):
            seen.append(messages)
            return types.SimpleNamespace(content='{"nodes": [], "combine": "reason"}')
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: FakeLLM())
    sources = [{"connector": types.SimpleNamespace(name=n, dialect="sqlite"), "allowed": a}
               for n, a in (("crm", ["customers"]), ("retail", ["retail_orders"]))]
    agent_graph.plan_graph("net sales by customer segment", sources, ANALYST)
    text = json.dumps([str(m) for msgs in seen for m in msgs])
    assert "Customer: crm.customers.id = retail.retail_orders.customer_id  (verified)" in text


# ── …and discloses nothing restricted ────────────────────────────────────

def test_a_role_without_crm_learns_nothing_about_it(two):
    verify(two, ("crm", "customers"), ("retail", "retail_orders"))
    ent = customer(two, VIEWER)
    assert [(m["source"], m["object"]) for m in ent["members"]] == [("retail", "retail_orders")]
    everything = json.dumps(two.mg.list_entities(user=VIEWER))
    assert "crm" not in everything and "customers" not in everything.replace("customer_id", "")
    assert two.me.bridge_lines(VIEWER, ["crm", "retail"]) == []
    found = json.dumps(two.mg.search(q="customer", user=VIEWER))
    assert "crm" not in found and "segment" not in found
    assert "crm" not in {s["source"] for s in two.mg.list_sources(user=VIEWER)["sources"]}
    for call in (lambda: two.mg.get_source("crm", user=VIEWER),
                 lambda: two.mg.source_relationships("crm", user=VIEWER)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:                 # no records either
        blend(VIEWER)
    assert "Enterprise" not in str(e.value.detail)
    with pytest.raises(HTTPException) as e:
        two.mg.metadata_health(user=VIEWER)
    assert e.value.status_code == 403


def test_an_authorized_role_still_never_sees_what_it_is_not_granted(two):
    verify(two, ("crm", "customers"), ("retail", "retail_orders"))
    shown = json.dumps(two.mg.list_entities(user=ANALYST))
    assert "crm_private_notes" not in shown
    assert two.mg.search(q="note", user=ANALYST)["results"] == []
    with pytest.raises(HTTPException):
        blend(ANALYST, [PARTS[0], {"name": "notes", "source": "crm", "table": "crm_private_notes",
                                   "sql": "SELECT customer_id, note FROM crm_private_notes"}])


def test_a_rejected_member_never_joins(two):
    verify(two, ("crm", "customers"), ("retail", "retail_orders"))
    ent = customer(two, ADMIN)
    two.mg.review_entity_member(member(ent, "retail", "retail_orders")["id"],
                                {"decision": "rejected"}, user=ADMIN)
    with pytest.raises(HTTPException):
        blend(ANALYST)
    assert all(m["state"] != "rejected" for m in customer(two, ANALYST)["members"])


# ── Ownership ────────────────────────────────────────────────────────────

def test_a_domain_owner_reviews_what_they_own_and_nothing_else(two):
    two.mg.set_ownership("retail", {"domain": "Sales", "owner_email": ANALYST["email"]},
                         user=ADMIN)
    ent = customer(two, ADMIN)
    retail_member = member(ent, "retail", "retail_orders")["id"]
    crm_member = member(ent, "crm", "customers")["id"]
    two.mg.review_entity_member(retail_member, {"decision": "verified"}, user=ANALYST)
    with pytest.raises(HTTPException) as e:
        two.mg.review_entity_member(crm_member, {"decision": "verified"}, user=ANALYST)
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        two.mg.review_entity_member(retail_member, {"decision": "rejected"}, user=VIEWER)
    assert e.value.status_code == 403
    orders = next(r for r in two.mg.get_source("retail", user=VIEWER)["relations"]
                  if r["name"] == "retail_orders")
    assert orders["domain"] == "Sales" and orders["owner"] == "Ana"
    with pytest.raises(HTTPException) as e:
        two.mg.set_ownership("retail", {"domain": "x"}, user=ANALYST)
    assert e.value.status_code == 403


def test_entities_can_be_made_by_hand(two):
    eid = two.mg.create_entity({"name": "Account", "domain": "Sales"}, user=ADMIN)["id"]
    two.mg.add_entity_member(eid, {"source": "retail", "object": "retail_orders",
                                   "column": "customer_id"}, user=ADMIN)
    ent = next(e for e in two.mg.list_entities(user=VIEWER)["entities"] if e["name"] == "Account")
    assert ent["members"][0]["state"] == "verified" and ent["domain"] == "Sales"
    with pytest.raises(HTTPException) as e:
        two.mg.add_entity_member(eid, {"source": "crm", "object": "customers", "column": "id"},
                                 user=VIEWER)
    assert e.value.status_code == 404                        # not even told crm exists


# ── Freshness ────────────────────────────────────────────────────────────

def test_freshness_is_monitored_and_every_change_logged(two, caplog):
    health = two.mg.metadata_health(user=ADMIN)
    assert {r["source"]: r["freshness"] for r in health["sources"]}["retail"] == "fresh"
    with two.db.connect() as c:
        c.execute("UPDATE meta_sources SET ok_at=? WHERE source='retail'",
                  (time.time() - 3 * 86400,))
        c.execute("UPDATE meta_sources SET status='failed', error='boom' WHERE source='crm'")
        c.commit()
    caplog.set_level(logging.INFO, logger="studio.metagraph")
    two.mp.setattr(two.mg, "_check", lambda source, now: None)   # watch only, rediscover nothing
    two.mg.tick_once()
    rows = {r["source"]: r for r in two.mg.metadata_health(user=ADMIN)["sources"]}
    assert rows["retail"]["freshness"] == "stale"
    assert rows["retail"]["reasons"][0].startswith("last discovered")
    assert rows["crm"]["freshness"] == "failing"
    assert two.mg.metadata_health(user=ADMIN)["status"] == "failing"
    assert any("retail metadata is stale" in r.getMessage() for r in caplog.records)
    assert any("crm metadata is failing" in r.getMessage() for r in caplog.records)
    retail = next(s for s in two.mg.list_sources(user=VIEWER)["sources"]
                  if s["source"] == "retail")
    assert retail["freshness"] == "stale" and "freshness_reasons" not in retail


# ── Tenant isolation in a shared Neo4j ───────────────────────────────────

def test_the_neo4j_mirror_is_tenant_scoped(two, monkeypatch):
    posted = []

    class Resp:
        def __init__(self):
            self.body = b'{"results": [], "errors": []}'

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(two.mg.urllib.request, "urlopen",
                        lambda req, timeout=None: posted.append(json.loads(req.data)) or Resp())
    for k, v in (("METAGRAPH_NEO4J_URI", "https://shared:7473"), ("METAGRAPH_NEO4J_USER", "u"),
                 ("METAGRAPH_NEO4J_PASSWORD", "p"), ("STUDIO_METAGRAPH_TENANT", "acme-prod")):
        monkeypatch.setenv(k, v)
    two.mg.mirror("retail")
    two.mg.mirror_entities()
    two.mg.mirror_drop("crm")
    statements = [s for body in posted for s in body["statements"]]
    assert statements and all(s["parameters"]["tenant"] == "acme-prod" for s in statements)
    for s in statements:                                   # every node pattern is scoped
        for label in ("StudioSource {", "StudioObject {", "StudioColumn {", "StudioEntity {"):
            for chunk in s["statement"].split(label)[1:]:
                assert chunk.startswith("tenant: $tenant"), s["statement"]


def test_migration_adds_the_freshness_column(two):
    with two.db.connect() as c:
        c.execute("DROP TABLE meta_sources")
        c.execute("CREATE TABLE meta_sources (source TEXT PRIMARY KEY, status TEXT NOT NULL)")
        two.migrations._m20_metagraph_freshness(c, two.db.IS_PG)
        c.commit()
        cols = {r[1] for r in c.execute("PRAGMA table_info(meta_sources)").fetchall()}
    assert "freshness" in cols
