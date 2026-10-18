"""Certified metrics in the metadata graph — on a schema that is not retail.

The semantic YAML stays the source of truth for what a metric means; the graph
says whether the warehouse still holds what it reads. Every definition is
linked to its table and the columns its expression and filters use, checked
(valid / broken / unchecked), versioned from the applied YAML, shown only to
roles that may read its table, used to ground questions by its synonyms, and
never answered from once broken.
"""
import importlib
import json
import sqlite3
import types

import pytest
from fastapi import HTTPException

from app import metametrics as mm
from test_metagraph_any_schema import SqliteSource

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
VIEWER = {"id": "u-viewer", "email": "viewer@studio.test", "role": "viewer", "name": "Vi"}

ERP = {
    "Customer": '"CustomerID" INTEGER, "Segment" TEXT',
    "SalesOrderHeader": '"SalesOrderID" INTEGER, "CustomerID" INTEGER, "TerritoryID" INTEGER, '
                        '"Status" TEXT',
    "SalesOrderDetail": '"SalesOrderDetailID" INTEGER, "SalesOrderID" INTEGER, '
                        '"OrderQty" INTEGER, "UnitPrice" REAL, "UnitPriceDiscount" REAL',
    "SalesTerritory": '"TerritoryID" INTEGER, "Region" TEXT',
}
ROWS = {
    "SalesOrderHeader": [(1, 10, 100, "Shipped"), (2, 11, 101, "Cancelled")],
    "SalesOrderDetail": [(1, 1, 2, 10.0, 0.1), (2, 1, 1, 5.0, None), (3, 2, 4, 3.0, 0.0)],
    "SalesTerritory": [(100, "West"), (101, "East")],
}

YAML_V1 = """
models:
  - source: erp
    table: SalesOrderDetail
    metrics:
      - name: discounted_line_revenue
        agg: sum
        expr: '"OrderQty" * "UnitPrice" * (1 - COALESCE("UnitPriceDiscount", 0))'
        synonyms: [turnover, net line revenue]
        description: Line revenue after discount
        owner: finance@example.com
        certified: true
        version: "1"
      - name: units
        agg: sum
        expr: OrderQty
  - source: erp
    table: SalesTerritory
    metrics:
      - name: territories
        agg: count
        expr: '*'
    dimensions:
      - name: region
        expr: Region
"""


@pytest.fixture()
def erp(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "metrics.db"))
    monkeypatch.setenv("STUDIO_SECRET", "metrics-secret")
    for k in ("METAGRAPH_NEO4J_URI", "METAGRAPH_NEO4J_USER", "METAGRAPH_NEO4J_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, governance, jobs, metagraph, queries, semantic
    from app.connectors import demo
    _db.init_db()
    for module in (jobs, metagraph, connections, queries, governance, semantic):
        module.init_tables()
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo_warehouse.db"))
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n'
                    '  viewer: { sources: { erp: [SalesTerritory] } }\n', "test")
    path = tmp_path / "erp.db"
    with sqlite3.connect(path) as c:
        for table, cols in ERP.items():
            c.execute(f'CREATE TABLE "{table}" ({cols})')
        for table, rows in ROWS.items():
            for row in rows:
                c.execute(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in row)})', row)
    monkeypatch.setitem(connectors._REGISTRY, "erp", SqliteSource("erp", str(path)))
    env = types.SimpleNamespace(mg=metagraph, sem=semantic, db=_db, mp=monkeypatch, path=path,
                                jobs=jobs)
    env.discover = lambda: jobs.handlers()["metagraph_discover"]({"source": "erp"}, None)
    env.apply = lambda text: semantic.apply_yaml(semantic.YamlIn(yaml=text), user=ADMIN)
    env.discover()
    env.apply(YAML_V1)
    yield env
    semantic._STATE.update(doc=None, yaml="", source=None)
    governance._STATE.update(doc=None, yaml="", source=None)


def metric(env, name, user=ADMIN):
    return next(m for m in env.mg.list_metrics(user=user)["metrics"] if m["name"] == name)


# ── Linked to the record ─────────────────────────────────────────────────

def test_a_metric_is_linked_to_its_table_and_the_columns_it_reads(erp):
    m = metric(erp, "discounted_line_revenue")
    assert m["status"] == "valid" and m["object"] == "SalesOrderDetail"
    assert m["columns"] == ["OrderQty", "UnitPrice", "UnitPriceDiscount"]
    assert (m["owner"], m["certified"], m["declared_version"], m["version"]) == \
        ("finance@example.com", True, "1", 1)
    region = next(d for d in erp.mg.list_metrics(user=ADMIN)["metrics"]
                  if d["kind"] == "dimension")
    assert (region["name"], region["columns"], region["status"]) == ("region", ["Region"], "valid")
    assert metric(erp, "territories")["columns"] == []                    # COUNT(*)


def test_tables_list_their_metrics_and_the_graph_draws_them(erp):
    detail = next(r for r in erp.mg.get_source("erp", user=ADMIN)["relations"]
                  if r["name"] == "SalesOrderDetail")
    assert {m["name"] for m in detail["metrics"]} == {"discounted_line_revenue", "units"}
    g = erp.mg.source_graph("erp", user=ADMIN)
    mid = "metric:erp:SalesOrderDetail:discounted_line_revenue"
    assert {"from": mid, "to": "object:erp:SalesOrderDetail", "type": "MEASURES"} in g["edges"]
    assert {"from": mid, "to": "column:erp:SalesOrderDetail:UnitPriceDiscount",
            "type": "USES"} in g["edges"]


# ── Broken when the warehouse changes ────────────────────────────────────

def test_a_dropped_column_breaks_the_metric_and_it_is_not_answered_from(erp, monkeypatch):
    from app import progress
    said = []
    monkeypatch.setattr(progress, "emit", lambda text, **k: said.append(text))
    assert erp.sem.answer(ADMIN, "erp", "turnover") is not None           # works while valid
    with sqlite3.connect(erp.path) as c:
        c.execute('ALTER TABLE "SalesOrderDetail" DROP COLUMN "UnitPriceDiscount"')
    erp.discover()
    m = metric(erp, "discounted_line_revenue")
    assert m["status"] == "broken"
    assert m["reasons"] == ["column UnitPriceDiscount not found in SalesOrderDetail"]
    assert metric(erp, "units")["status"] == "valid"                     # only what it reads
    assert erp.sem.answer(ADMIN, "erp", "turnover") is None
    assert any("discounted_line_revenue is broken" in s for s in said)
    health = {r["source"]: r for r in erp.mg.metadata_health(user=ADMIN)["sources"]}["erp"]
    assert health["freshness"] == "failing" and health["broken_metrics"] == 1


def test_a_missing_table_or_an_undiscovered_source(erp):
    erp.apply(YAML_V1 + """
  - source: erp
    table: Returns
    metrics:
      - name: returns
        agg: count
        expr: '*'
  - source: elsewhere
    table: anything
    metrics:
      - name: other
        agg: sum
        expr: amount
""")
    assert metric(erp, "returns")["reasons"] == ["table Returns not found in erp"]
    d = next(d for d in mm.definitions() if d["name"] == "other")
    assert mm.check(d)["status"] == "unchecked"


# ── Versions ─────────────────────────────────────────────────────────────

def test_each_new_definition_is_a_new_version(erp):
    erp.apply(YAML_V1.replace('COALESCE("UnitPriceDiscount", 0)', '"UnitPriceDiscount"'))
    erp.apply(YAML_V1.replace('COALESCE("UnitPriceDiscount", 0)', '"UnitPriceDiscount"')
              .replace("Line revenue after discount", "Reworded"))          # same definition
    erp.apply(YAML_V1.replace("      - name: discounted_line_revenue",
                              "      - name: gross_line_revenue"))          # taken out
    hist = erp.mg.get_metric("erp", "units", user=ADMIN)["history"]
    assert [v["version"] for v in hist] == [1]                         # never changed
    with pytest.raises(HTTPException):                                  # no longer defined
        erp.mg.get_metric("erp", "discounted_line_revenue", user=ADMIN)
    hist = mm.history("erp", "SalesOrderDetail", "discounted_line_revenue")
    assert [v.get("version") for v in hist] == [1, 2, None]
    assert hist[-1]["removed"] and "COALESCE" in hist[0]["expr"] and "COALESCE" not in hist[1]["expr"]
    assert hist[0]["applied_by"] == ADMIN["email"]


# ── Permissions ──────────────────────────────────────────────────────────

def test_a_role_sees_only_definitions_on_tables_it_may_read(erp):
    names = {m["name"] for m in erp.mg.list_metrics(user=VIEWER)["metrics"]}
    assert names == {"territories", "region"}
    with pytest.raises(HTTPException) as e:
        erp.mg.get_metric("erp", "discounted_line_revenue", user=VIEWER)
    assert e.value.status_code == 404
    assert "reasons" not in metric(erp, "territories", VIEWER)


# ── Grounding uses certified definitions ─────────────────────────────────

def test_a_synonym_grounds_the_question_and_the_agent_gets_the_definition(erp):
    from app import metaground
    allowed = list(ERP)
    g = metaground.ground("erp", "turnover by region", allowed)
    assert {"SalesOrderDetail", "SalesTerritory"} <= set(g["tables"])
    assert "SalesOrderHeader" in g["bridges"]                          # joins them
    lines = metaground.join_hint_lines(g["joins"], g["views"], g["metrics"])
    assert any(line.startswith("metric discounted_line_revenue on SalesOrderDetail (certified): "
                               'SUM("OrderQty" * "UnitPrice"') and line.endswith("use this definition")
               for line in lines)


def test_a_broken_definition_never_steers_grounding(erp):
    from app import metaground
    with sqlite3.connect(erp.path) as c:
        c.execute('ALTER TABLE "SalesOrderDetail" DROP COLUMN "UnitPriceDiscount"')
    erp.discover()
    g = metaground.ground("erp", "turnover by region", list(ERP))
    assert [d["name"] for d in g["metrics"]] == ["region"]           # the valid one only
    assert "SalesOrderDetail" not in g["matched"]                     # only its synonym matched


# ── Reading references in any expression ─────────────────────────────────

@pytest.mark.parametrize("expr,refs", [
    ('"OrderQty" * "UnitPrice"', ["OrderQty", "UnitPrice"]),
    ("SUM(o.amount) / NULLIF(COUNT(DISTINCT o.order_id), 0)", ["amount", "order_id"]),
    ("CASE WHEN status <> 'test' THEN net ELSE 0 END", ["status", "net"]),
    ("CAST(ordered_at AS date) >= DATE '2024-01-01'", ["ordered_at"]),
    ("EXTRACT(year FROM ordered_at)", ["ordered_at"]),
    ("*", []),
])
def test_references_are_columns_not_sql(expr, refs):
    assert mm.references(expr) == refs


def test_stewardship_fields_are_checked(erp):
    bad = YAML_V1.replace('certified: true', 'certified: "yes"')
    ok, errors, _ = erp.sem.validate(bad)
    assert not ok and "certified must be true or false" in errors[0]


def test_metrics_reach_the_neo4j_mirror(erp, monkeypatch):
    posted = []

    class Resp:
        def read(self):
            return b'{"results": [], "errors": []}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(erp.mg.urllib.request, "urlopen",
                        lambda req, timeout=None: posted.append(json.loads(req.data)) or Resp())
    for k, v in (("METAGRAPH_NEO4J_URI", "https://g:7473"), ("METAGRAPH_NEO4J_USER", "u"),
                 ("METAGRAPH_NEO4J_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    erp.mg.mirror("erp")
    statements = posted[-1]["statements"]
    merge = next(s for s in statements if "MERGE (x:StudioMetric" in s["statement"])
    assert {m["name"] for m in merge["parameters"]["metrics"]} >= {"discounted_line_revenue"}
    assert "tenant: $tenant" in merge["statement"]
    assert not any("discounted_line_revenue" in s["statement"] for s in statements)
