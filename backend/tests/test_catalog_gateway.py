"""Catalog reads go through the gateway — and the catalog describes only what
the gateway would return.

Before this, /sample and /suggestions ran `SELECT * FROM {table}` straight on
the connector with the table name taken from the URL path: no query guard (a
role with a "*" policy could name any object the warehouse exposed), and no
governance filter, so deny/mask columns were bypassed and the raw sample was
then handed to an external LLM by suggest._from_llm. These tests pin the
closed state: governed rows, governed schema metadata, audit rows named after
the catalog purpose, and a 4xx for any forged path segment.

Run from the backend directory:
    python -m pytest tests/test_catalog_gateway.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-catalog-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi.testclient import TestClient

from app import agent, db, governance, suggest

ANALYST = ("analyst@studio.local", "analyst123")
VIEWER = ("viewer@studio.local", "viewer123")

GOV_YAML = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*" } }
  viewer: { sources: { demo: [sales, web_traffic] } }
compliance:
  demo:
    customers:
      deny_columns: [name]
      mask_columns: [lifetime_value]
"""


@pytest.fixture(scope="module")
def client():
    # Imported here, not at collection time: app.main pulls in toolbuilder /
    # sandbox / mcp, which compute their paths from env at import, and other
    # test modules set that env at THEIR import — an early app.main import
    # would freeze the wrong paths into them.
    import app.main as main
    mp = pytest.MonkeyPatch()
    # No LLM key: suggestions take the deterministic path unless a test
    # patches the LLM entry point itself. No background ticker either.
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        mp.delenv(k, raising=False)
    mp.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    with TestClient(main.app) as c:
        yield c
    mp.undo()


@pytest.fixture(autouse=True)
def _governed(client):
    """Every test runs under the deny/mask document above (loaded AFTER app
    startup, which resets governance from the empty DB) and starts with an
    empty suggestion cache; the doc is dropped afterwards so other modules
    see the built-in policies."""
    governance._set(GOV_YAML, "test")
    suggest._CACHE.clear()
    yield
    governance._STATE.update(doc=None, yaml="", source=None)
    suggest._CACHE.clear()


def _auth(client, creds):
    email, password = creds
    tok = client.post("/api/auth/login",
                      json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _audit(email, action):
    return [r for r in db.list_activity()
            if r["email"] == email and r["action"] == action]


# ── /sample ─────────────────────────────────────────────────────────────

def test_sample_is_governed_and_audited(client):
    h = _auth(client, ANALYST)
    r = client.get("/api/catalog/sources/demo/tables/customers/sample", headers=h)
    assert r.status_code == 200, r.text
    panels = r.json()["panels"]
    assert panels, "the sample table panel always renders"
    for p in panels:
        assert "name" not in [c.lower() for c in p["columns"]], p["sql"]
        # A masked column must never be the bar's measure or dimension either.
        assert "lifetime_value" not in p["sql"] or p["chart"]["type"] == "table"
    table = [p for p in panels if p["chart"]["type"] == "table"][0]
    lv = table["columns"].index("lifetime_value")
    assert table["rows"] and all(row[lv] == "***" for row in table["rows"])
    assert len(table["rows"]) <= 50

    rows = _audit(ANALYST[0], "catalog_sample")
    assert rows, "the governed read is audited under the catalog's purpose"
    assert rows[0]["ok"] == 1 and rows[0]["source"] == "demo" and rows[0]["tbl"] == "customers"
    assert "LIMIT 50" in rows[0]["sql"]


def test_sample_still_builds_the_bar_panel_for_an_ungoverned_table(client):
    """Behaviour pinned from before the migration: two panels, bar first."""
    h = _auth(client, ANALYST)
    r = client.get("/api/catalog/sources/demo/tables/sales/sample", headers=h)
    assert r.status_code == 200, r.text
    panels = r.json()["panels"]
    assert [p["chart"]["type"] for p in panels] == ["bar", "table"]
    assert "GROUP BY" in panels[0]["sql"] and len(panels[0]["rows"]) <= 12
    assert panels[0]["chart"]["x"] not in ("order_id",)


def test_viewer_cannot_sample_a_denied_table(client):
    h = _auth(client, VIEWER)
    r = client.get("/api/catalog/sources/demo/tables/customers/sample", headers=h)
    assert r.status_code == 403
    assert client.get("/api/catalog/sources/demo/tables/sales/sample",
                      headers=h).status_code == 200


@pytest.mark.parametrize("forged", [
    "sales%20UNION%20SELECT",   # a second statement grafted onto the FROM
    "sales;--",                 # statement terminator + comment
    "sales%2C%20customers",     # a comma-joined denied table
])
def test_forged_table_path_is_rejected_not_executed(client, forged):
    for creds in (ANALYST, VIEWER):
        r = client.get(f"/api/catalog/sources/demo/tables/{forged}/sample",
                       headers=_auth(client, creds))
        assert 400 <= r.status_code < 500, (forged, r.status_code, r.text)


def test_a_well_formed_name_outside_the_catalog_is_refused_by_the_gateway(client):
    """`sqlite_master` is a real object the warehouse would happily read; it
    is not in list_tables(), so the gateway's guard refuses it — and the
    refusal is audited, unlike the silent read it replaced."""
    h = _auth(client, ANALYST)
    r = client.get("/api/catalog/sources/demo/tables/sqlite_master/sample", headers=h)
    assert r.status_code == 403, r.text
    denied = [a for a in _audit(ANALYST[0], "catalog_sample") if a["tbl"] == "sqlite_master"]
    assert denied and denied[0]["ok"] == 0


# ── schema metadata ─────────────────────────────────────────────────────

def test_schema_omits_denied_columns_but_lists_masked_ones(client):
    h = _auth(client, ANALYST)
    r = client.get("/api/catalog/sources/demo/tables/customers/schema", headers=h)
    assert r.status_code == 200, r.text
    names = [c["name"] for c in r.json()]
    assert "name" not in names
    assert "lifetime_value" in names and "city" in names


def test_governance_column_rules_are_empty_without_a_doc():
    governance._STATE.update(doc=None, yaml="", source=None)
    assert governance.column_rules("demo", "customers") == {"deny": set(), "mask": set()}
    governance._set(GOV_YAML, "test")
    assert governance.column_rules("demo", "CUSTOMERS") == {"deny": {"name"},
                                                            "mask": {"lifetime_value"}}
    assert governance.column_rules("demo", "sales") == {"deny": set(), "mask": set()}


def test_skill_is_built_from_the_governed_schema(client, monkeypatch):
    from app import catalog, skills
    seen = {}

    def fake_skill(connector, role, allowed, schemas):
        seen.update(schemas)
        return "skill"
    monkeypatch.setattr(skills, "get_skill", fake_skill)
    h = _auth(client, ANALYST)
    r = client.get("/api/catalog/sources/demo/skill", headers=h)
    assert r.status_code == 200, r.text
    assert "customers" in seen
    assert "name" not in [c["name"] for c in seen["customers"]]
    assert catalog._connector_or_400 is catalog.connector_or_400   # alias kept


# ── /suggestions ────────────────────────────────────────────────────────

def test_suggestions_receive_governed_schema_and_rows(client, monkeypatch):
    seen = []
    real = suggest.suggestions_for

    def spy(connector, table, columns, rows=None):
        seen.append((table, columns, rows))
        return real(connector, table, columns, rows)
    monkeypatch.setattr(suggest, "suggestions_for", spy)

    h = _auth(client, ANALYST)
    r = client.get("/api/catalog/sources/demo/suggestions", params={"table": "customers"},
                   headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["suggestions"], "the deterministic path never returns nothing"
    assert [t for t, _c, _r in seen] == ["customers"]
    _t, cols, rows = seen[0]
    names = [c["name"] for c in cols]
    assert "name" not in names
    # Schema and rows agree column-for-column: the suggester reads by position.
    assert rows and all(len(row) == len(names) for row in rows)
    assert all(row[names.index("lifetime_value")] == "***" for row in rows)
    assert len(rows) <= 3
    assert _audit(ANALYST[0], "catalog_suggest")


def test_no_denied_value_reaches_the_llm(client, monkeypatch):
    captured = {}

    def fake_llm(table, columns, rows=None, n=suggest.MAX_SUGGESTIONS):
        captured["table"], captured["columns"], captured["rows"] = table, columns, rows
        return ["How has lifetime_value trended by month?"]
    monkeypatch.setattr(suggest, "_from_llm", fake_llm)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)

    h = _auth(client, ANALYST)
    r = client.get("/api/catalog/sources/demo/suggestions", params={"table": "customers"},
                   headers=h)
    assert r.status_code == 200, r.text
    assert captured["table"] == "customers"
    assert "name" not in [c["name"] for c in captured["columns"]]
    cells = [str(v) for row in captured["rows"] for v in row]
    assert cells and not any(v.startswith("Customer ") for v in cells)
    assert "***" in cells                     # masked, not raw, lifetime values


def test_whole_source_suggestions_round_robin_across_tables(client):
    h = _auth(client, VIEWER)
    r = client.get("/api/catalog/sources/demo/suggestions", headers=h)
    assert r.status_code == 200, r.text
    tables = {s["table"] for s in r.json()["suggestions"]}
    assert tables <= {"sales", "web_traffic"} and len(tables) == 2
