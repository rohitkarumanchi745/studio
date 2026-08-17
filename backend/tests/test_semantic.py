"""Semantic layer — consistency, resolver safety, dialect compilation, and the
governance-through-aliasing invariant. These lock in the fixes from the
adversarial review (a governance bypass + several confident-wrong-answer paths).
"""
import warnings

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")

TEMPLATE = None  # filled from semantic.TEMPLATE in the fixture


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "sem.db"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        tok = c.post("/api/auth/login",
                     json={"email": "admin@studio.local", "password": "admin123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        from app import semantic
        c.put("/api/semantic", json={"yaml": semantic.TEMPLATE})
        yield c


def compile(c, prompt, table="*"):
    return c.post("/api/semantic/compile",
                  json={"source": "demo", "prompt": prompt, "table": table}).json()


# ── The core promise: same meaning → identical SQL ──────────────────────

def test_phrasings_compile_identically(client):
    sqls = {compile(client, p)["sql"] for p in
            ["revenue by region", "sales across geographies",
             "turnover by market", "income by location"]}
    assert len(sqls) == 1


def test_multi_metric_returns_both(client):
    r = compile(client, "revenue and orders by region")
    assert set(r["metrics"]) == {"revenue", "orders"}


def test_time_grain_consistent(client):
    a = compile(client, "revenue trend")
    b = compile(client, "monthly sales")
    assert a["sql"] == b["sql"] and "strftime" in a["sql"]


# ── Resolver false-positives must decline, not force-fit ────────────────

def test_filler_in_order_to_declines(client):
    # "order" appears only as English filler — must NOT fire the orders metric.
    assert compile(client, "in order to proceed with the migration")["resolved"] is False


def test_how_many_orders_still_resolves(client):
    r = compile(client, "how many orders did we get")
    assert r["resolved"] and "orders" in r["metrics"]


def test_aov_phrasing_does_not_leak_orders(client):
    # "average order value" must not also fire `orders` via its lone word "order".
    a = compile(client, "aov by region")
    b = compile(client, "average order value by region")
    assert a["sql"] == b["sql"]
    assert b["metrics"] == ["avg_order_value"]


def test_negation_drops_the_metric(client):
    r = compile(client, "compare our region to theirs, ignore revenue")
    assert (not r["resolved"]) or ("revenue" not in r.get("metrics", []))


# ── Author-time validation fails closed ─────────────────────────────────

def test_duplicate_metric_name_rejected(client):
    dup = ("models:\n  - source: demo\n    table: sales\n    metrics:\n"
           "      - {name: revenue, agg: sum, expr: revenue}\n"
           "      - {name: revenue, agg: count, expr: \"*\"}\n")
    r = client.post("/api/semantic/validate", json={"yaml": dup}).json()
    assert not r["ok"] and any("duplicate" in e for e in r["errors"])


def test_default_time_typo_rejected(client):
    bad = ("models:\n  - source: demo\n    table: sales\n    default_time: week\n"
           "    metrics:\n      - {name: revenue, agg: sum, expr: revenue}\n"
           "    dimensions:\n      - {name: month, expr: order_date, grain: month}\n")
    r = client.post("/api/semantic/validate", json={"yaml": bad}).json()
    assert not r["ok"] and any("default_time" in e for e in r["errors"])


# ── Dialect: median declines uniformly where it can't be expressed ──────

def test_median_declines_on_sqlite(client):
    med = ("models:\n  - source: demo\n    table: sales\n    metrics:\n"
           "      - {name: med_rev, agg: median, expr: revenue, synonyms: [median revenue]}\n"
           "    dimensions:\n      - {name: region, expr: region}\n")
    client.put("/api/semantic", json={"yaml": med})
    # sqlite has no portable median → declines rather than emitting build-dependent SQL.
    assert compile(client, "median revenue by region")["resolved"] is False


def test_median_compiles_on_warehouse_dialect():
    from app import semantic
    metric = {"name": "m", "agg": "median", "expr": "revenue", "filter": None}
    assert "PERCENTILE_CONT(0.5)" in semantic._agg_sql(metric, "duckdb")
    with pytest.raises(ValueError):
        semantic._agg_sql(metric, "sqlite")


# ── Governance: masking/deny keyed on the SOURCE column, not the alias ──

def _apply_governance(c, compliance):
    gov = ("roles:\n  admin: {sources: \"*\"}\n  analyst: {sources: \"*\"}\n"
           "  viewer: {sources: \"*\"}\ncompliance:\n" + compliance)
    assert c.put("/api/governance", json={"yaml": gov}).json()["loaded"]


def test_mask_survives_dimension_alias(client):
    # dimension `geo` aliases the masked column `region`.
    doc = ("models:\n  - source: demo\n    table: sales\n    metrics:\n"
           "      - {name: revenue, agg: sum, expr: revenue, synonyms: [sales]}\n"
           "    dimensions:\n      - {name: geo, expr: region, synonyms: [geography, region]}\n")
    client.put("/api/semantic", json={"yaml": doc})
    _apply_governance(client, "  demo:\n    sales: {mask_columns: [region]}\n")
    d = client.post("/api/chat",
                    json={"prompt": "revenue by geo", "source": "demo", "table": "*"}).json()["message"]
    assert d["served_by"] == "semantic"
    assert d["rows"] and all(row[0] == "***" for row in d["rows"])


def test_deny_survives_dimension_alias():
    from app import governance
    _, _, doc = governance.validate(
        "roles:\n  a: {sources: \"*\"}\ncompliance:\n  demo:\n    customers: {deny_columns: [city]}\n")
    governance._STATE.update(doc=doc, yaml="x", source="test")
    try:
        sql = ('SELECT city AS "location", SUM(lifetime_value) AS "ltv" '
               'FROM customers GROUP BY city LIMIT 5000')
        cols, rows = governance.filter_result("demo", sql, ["location", "ltv"], [["Berlin", 1.0]])
        assert "location" not in cols          # the denied source column is dropped
    finally:
        governance._STATE.update(doc=None, yaml="", source=None)


def test_select_star_still_masks_by_name():
    # Regression: the ordinary path (output name == source column) is unaffected.
    from app import governance
    _, _, doc = governance.validate(
        "roles:\n  a: {sources: \"*\"}\ncompliance:\n  demo:\n    customers: {mask_columns: [city]}\n")
    governance._STATE.update(doc=doc, yaml="x", source="test")
    try:
        cols, rows = governance.filter_result(
            "demo", "SELECT * FROM customers", ["id", "city"], [[1, "Berlin"]])
        assert rows[0][cols.index("city")] == "***"
    finally:
        governance._STATE.update(doc=None, yaml="", source=None)


# ── RBAC: /compile is not an existence oracle for denied tables ─────────

def test_compile_no_oracle_for_denied_table(client):
    doc = ("models:\n  - source: demo\n    table: sales\n    metrics:\n"
           "      - {name: revenue, agg: sum, expr: revenue, synonyms: [sales]}\n"
           "    dimensions:\n      - {name: region, expr: region}\n"
           "  - source: demo\n    table: customers\n    metrics:\n"
           "      - {name: ltv, agg: sum, expr: lifetime_value, synonyms: [lifetime value]}\n"
           "    dimensions:\n      - {name: city, expr: city}\n")
    client.put("/api/semantic", json={"yaml": doc})
    # viewer restricted to sales only — customers is denied.
    gov = ("roles:\n  admin: {sources: \"*\"}\n  analyst: {sources: \"*\"}\n"
           "  viewer: {sources: {demo: [sales]}}\ncompliance: {}\n")
    client.put("/api/governance", json={"yaml": gov})
    from app import db
    db.create_user("v@x.com", "vpw", "v", role="viewer")
    vt = client.post("/api/auth/login", json={"email": "v@x.com", "password": "vpw"}).json()["access_token"]
    r = client.post("/api/semantic/compile",
                    headers={"Authorization": f"Bearer {vt}"},
                    json={"source": "demo", "prompt": "ltv by city"}).json()
    assert r["resolved"] is False   # denied customers table never disclosed
