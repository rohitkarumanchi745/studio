"""Dashboards read through the gateway — pin-time guard, view-time RBAC,
governance on the cached frame, and the audit trail.

Proves, over HTTP with the seeded demo accounts: a tile's rows come back
governed (a deny_columns column is gone from the tile, the slicer catalog and
the unfiltered base frame) and the read is audited as `dashboard_tile`; the
cached second read hands back the same governed frame and writes no second
audit row; a viewer opening an org dashboard sees denied=True on a customers
tile and never a row; a pin whose SQL names a denied table — in a FROM or in
ANY identifier position — is refused before anything is stored; a warm cache
is not an RBAC bypass when the policy narrows under it; and a policy another
PROCESS applied reaches this one's tiles within one governance refresh.

Run from the backend directory:
    python -m pytest tests/test_dashboards_gateway.py -q
"""
import os
import tempfile
import time
import uuid
import warnings

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-dash-gateway-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi.testclient import TestClient

from app import dashboards, db, governance

warnings.filterwarnings("ignore")

GOV_YAML = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*", snowflake: "*" } }
  viewer: { sources: { demo: [sales, web_traffic] } }
compliance:
  demo:
    customers:
      deny_columns: [name]
"""

# Analyst keeps demo but loses customers — a policy narrowing under a warm cache.
NARROWED_YAML = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: [sales, web_traffic] } }
  viewer: { sources: { demo: [sales, web_traffic] } }
"""

# What another replica applies straight to the store: city joins name in the
# deny list. Nothing tells this process about it.
STRICTER_YAML = GOV_YAML.replace("deny_columns: [name]", "deny_columns: [name, city]")

CUSTOMERS_SQL = "SELECT * FROM customers"


def _apply_elsewhere(text):
    """An apply by a DIFFERENT process: the row lands in governance_docs and
    this process is never told. Only its freshness check can notice."""
    c = db._conn()
    c.execute("INSERT INTO governance_docs (id, yaml, applied_by, applied_at) VALUES (?,?,?,?)",
              (uuid.uuid4().hex, text, "other-replica@studio.test", time.time()))
    c.commit()
    c.close()


def _clear_store():
    try:
        c = db._conn()
        c.execute("DELETE FROM governance_docs")
        c.commit()
        c.close()
    except Exception:
        pass          # the table only exists once startup has run


@pytest.fixture(scope="module")
def client():
    mp = pytest.MonkeyPatch()
    # Another test module may have imported app.db first with its own path;
    # pin this module's DB explicitly so the two never share tables.
    mp.setattr(db, "DB_PATH", os.environ["STUDIO_DB_PATH"])
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL",
              "REDIS_URL", "STUDIO_QUERY_TIMEOUT_S"):
        mp.delenv(k, raising=False)
    mp.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    import app.main as main
    with TestClient(main.app) as c:     # startup: init tables + seed accounts
        governance._set(GOV_YAML, "test")
        yield c
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)
    mp.undo()


@pytest.fixture(autouse=True)
def _cold_cache():
    """Every test starts with an empty tile cache, the module's doc, and an
    EMPTY governance store: a document left in governance_docs would otherwise
    reach the next test through the freshness refresh."""
    _clear_store()
    dashboards._CACHE.clear()
    governance._set(GOV_YAML, "test")
    yield
    _clear_store()
    governance._FRESH.update(at=0.0, ident=None)
    dashboards._CACHE.clear()


def _auth(c, email, password):
    tok = c.post("/api/auth/login",
                 json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _analyst(c):
    return _auth(c, "analyst@studio.local", "analyst123")


def _viewer(c):
    return _auth(c, "viewer@studio.local", "viewer123")


def _audit(action, email=None):
    rows = [r for r in db.list_activity(None, limit=1000) if r["action"] == action]
    return [r for r in rows if email is None or r["email"] == email]


def _pin(c, headers, **body):
    payload = {"source": "demo", "table_label": "customers", "sql": CUSTOMERS_SQL,
               "spec": {"type": "table"}, "title": "Customers"}
    payload.update(body)
    return c.post("/api/dashboards/pin", json=payload, headers=headers)


# ── (a) governed rows + audit, (b) cached read ──────────────────────────

def test_tile_read_is_governed_and_audited_once_per_warehouse_trip(client):
    ana = _analyst(client)
    r = _pin(client, ana)
    assert r.status_code == 201, r.text
    did, tid = r.json()["dashboard_id"], r.json()["tile"]["id"]

    before = len(_audit("dashboard_tile", "analyst@studio.local"))
    r = client.post(f"/api/dashboards/{did}/data", headers=ana)
    assert r.status_code == 200
    tile = r.json()["tiles"][0]
    assert tile["error"] is None and tile["denied"] is False and tile["cached"] is False
    cols = [c.lower() for c in tile["columns"]]
    assert "name" not in cols and "city" in cols and tile["rows"]
    # The slicer catalog is built from the UNFILTERED base frame — governed too.
    assert "name" not in {f["col"].lower() for f in r.json()["fields"]}

    # The base frame itself (popped before the wire) carries no denied column.
    user = db.get_user_by_email("analyst@studio.local")
    data = dashboards.tile_data(dashboards.get_tile(did, tid), user)
    assert "name" not in [c.lower() for c in data["_base"]["columns"]]
    assert data["_base"]["rows"] and all(len(row) == len(data["_base"]["columns"])
                                         for row in data["_base"]["rows"])

    rows = _audit("dashboard_tile", "analyst@studio.local")
    assert len(rows) == before + 1              # the direct tile_data call was a cache hit
    a = rows[0]
    assert a["ok"] == 1 and a["source"] == "demo" and a["tbl"] == "customers"
    assert a["sql"].upper().startswith("SELECT") and "LIMIT" in a["sql"].upper()
    assert a["row_count"] == len(data["_base"]["rows"])

    # (b) The cached second read: same governed frame, no second audit row.
    r2 = client.post(f"/api/dashboards/{did}/data", headers=ana)
    tile2 = r2.json()["tiles"][0]
    assert tile2["cached"] is True and tile2["error"] is None
    assert tile2["columns"] == tile["columns"] and tile2["rows"] == tile["rows"]
    assert len(_audit("dashboard_tile", "analyst@studio.local")) == before + 1

    # refresh=True bypasses the cache and is a real (audited) gateway read again.
    r3 = client.post(f"/api/dashboards/{did}/data", json={"refresh": True}, headers=ana)
    assert r3.json()["tiles"][0]["cached"] is False
    assert len(_audit("dashboard_tile", "analyst@studio.local")) == before + 2


# ── (c) a viewer on an org dashboard ────────────────────────────────────

def test_viewer_gets_denied_tile_not_rows_even_with_a_warm_cache(client):
    ana, view = _analyst(client), _viewer(client)
    r = client.post("/api/dashboards", json={"title": "Org board", "visibility": "org"},
                    headers=ana)
    assert r.status_code == 201
    did = r.json()["id"]
    r = _pin(client, ana, dashboard_id=did)
    assert r.status_code == 201, r.text
    tid = r.json()["tile"]["id"]

    # The analyst renders first, so the analyst's frame is cached — the role is
    # part of the cache key, so the viewer can never be handed that frame.
    assert client.post(f"/api/dashboards/{did}/data", headers=ana).json()["tiles"][0]["rows"]

    before = len(_audit("dashboard_tile", "viewer@studio.local"))
    r = client.post(f"/api/dashboards/{did}/data", headers=view)
    assert r.status_code == 200
    tile = r.json()["tiles"][0]
    assert tile["denied"] is True and tile["rows"] == [] and tile["columns"] == []
    assert tile["error"]["code"] == "forbidden" and "no access" in tile["error"]["message"]
    assert r.json()["fields"] == []             # nothing lifted into the slicer catalog
    # The refusal is audited as a failed dashboard_tile read for the viewer.
    denied_rows = _audit("dashboard_tile", "viewer@studio.local")
    assert len(denied_rows) == before + 1 and denied_rows[0]["ok"] == 0

    # The single-tile endpoint is equally closed.
    one = client.post(f"/api/dashboards/{did}/tiles/{tid}/data", headers=view).json()
    assert one["denied"] is True and one["rows"] == []


# ── (d) pin-time refusal ────────────────────────────────────────────────

def test_pin_referencing_a_denied_table_is_refused(client):
    view = _viewer(client)
    dashboards_before = len(client.get("/api/dashboards", headers=view).json()["dashboards"])

    # A denied table in FROM.
    r = _pin(client, view, table_label=None)
    assert r.status_code == 400 and "customers" in r.text
    # A denied table's name in ANY identifier position — the strict tokenizer
    # scan that only dashboards apply, because pinned SQL is persisted.
    r = _pin(client, view, table_label=None, sql="SELECT region AS customers FROM sales")
    assert r.status_code == 400 and "customers" in r.text
    # RBAC on the tile's table label comes first: 403, not 400.
    r = _pin(client, view, table_label="customers", sql="SELECT region FROM sales")
    assert r.status_code == 403
    # A source the role may reach but which is not configured: the gateway's 400.
    r = _pin(client, _analyst(client), source="snowflake", table_label=None,
             sql="SELECT 1 FROM sales")
    assert r.status_code == 400 and "not configured" in r.text
    # Write statements never pin.
    r = _pin(client, _analyst(client), table_label=None, sql="DELETE FROM sales")
    assert r.status_code == 400

    # Nothing was created by a refused pin.
    assert len(client.get("/api/dashboards", headers=view).json()["dashboards"]) == dashboards_before

    # What a viewer MAY query still pins, and renders.
    r = _pin(client, view, table_label="sales", sql="SELECT region, revenue FROM sales")
    assert r.status_code == 201, r.text
    did = r.json()["dashboard_id"]
    tile = client.post(f"/api/dashboards/{did}/data", headers=view).json()["tiles"][0]
    assert tile["error"] is None and tile["rows"]


# ── the cache is not an RBAC bypass ─────────────────────────────────────

def test_warm_cache_is_rechecked_when_the_policy_narrows(client):
    ana = _analyst(client)
    r = _pin(client, ana)
    assert r.status_code == 201, r.text
    did = r.json()["dashboard_id"]
    assert client.post(f"/api/dashboards/{did}/data", headers=ana).json()["tiles"][0]["rows"]

    governance._set(NARROWED_YAML, "test")      # analyst loses customers mid-TTL
    before = len(_audit("dashboard_tile", "analyst@studio.local"))
    tile = client.post(f"/api/dashboards/{did}/data", headers=ana).json()["tiles"][0]
    assert tile["denied"] is True and tile["rows"] == [] and tile["cached"] is False
    # A policy change is a cache MISS (the governance version is in the key and
    # on_change dropped the frame), so the refusal comes from gateway.execute
    # and is audited as a failed read — never served from the warm frame.
    rows = _audit("dashboard_tile", "analyst@studio.local")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0


def test_warm_cache_never_serves_pre_policy_rows_after_a_governance_change(client):
    """A governance change (a new deny + mask) reaches a tile within the same
    TTL: the cache key carries the governance version and a hit is re-filtered,
    on this instance and on any other sharing a Redis."""
    ana = _analyst(client)
    governance._STATE.update(doc=None, yaml="", source=None)     # no rules yet
    r = _pin(client, ana, sql="SELECT name, lifetime_value FROM customers LIMIT 2")
    assert r.status_code == 201, r.text
    did = r.json()["dashboard_id"]
    tile = client.post(f"/api/dashboards/{did}/data", headers=ana).json()["tiles"][0]
    assert tile["cached"] is False and tile["columns"] == ["name", "lifetime_value"]
    assert tile["rows"] and tile["rows"][0][1] != "***"
    user = db.get_user_by_email("analyst@studio.local")
    key_before, rkey_before = dashboards._cache_key("analyst", "demo", tile["columns"] and
                                                    "SELECT name, lifetime_value FROM customers LIMIT 2")
    assert key_before in dashboards._CACHE

    governance._set(GOV_YAML.replace("deny_columns: [name]",
                                     "deny_columns: [name]\n      mask_columns: [lifetime_value]"),
                    "test")
    # on_change dropped the frame cached under the old document...
    assert key_before not in dashboards._CACHE
    key_after, rkey_after = dashboards._cache_key("analyst", "demo",
                                                  "SELECT name, lifetime_value FROM customers LIMIT 2")
    assert key_after != key_before and rkey_after != rkey_before   # ...and the keys moved
    tile = client.post(f"/api/dashboards/{did}/data", headers=ana).json()["tiles"][0]
    assert tile["columns"] == ["lifetime_value"] and all(row == ["***"] for row in tile["rows"])

    # Even a frame that somehow survives under the SAME key is re-filtered on
    # the hit: plant the pre-policy rows and read again.
    dashboards._remember(key_after, __import__("time").time(),
                         ["name", "lifetime_value"], [["Ada", 4200.0]])
    cols, rows, cached = dashboards._cached_query(user, "demo",
                                                  "SELECT name, lifetime_value FROM customers LIMIT 2")
    assert cached is True and cols == ["lifetime_value"] and rows == [["***"]]


def test_unconfigured_source_tile_is_a_source_error_not_a_denial(client):
    ana = _analyst(client)
    user = db.get_user_by_email("analyst@studio.local")
    dash = dashboards.create_dashboard(user, "Snow")
    tile, _ = dashboards.add_tile(dash["id"], {"source": "snowflake", "sql": "SELECT 1 FROM t",
                                               "spec": {}})
    out = client.post(f"/api/dashboards/{dash['id']}/tiles/{tile['id']}/data",
                      headers=ana).json()
    assert out["denied"] is False and out["error"]["code"] == "source_error"
    assert "not configured" in out["error"]["message"]


def test_a_stale_replica_stops_serving_the_pre_policy_tile_after_the_refresh(client):
    """The multi-process case. Another replica tightens the document; this one
    is holding a tile frame cached under the looser one and was never told.

    Inside the refresh TTL it still serves that frame — the window is bounded,
    not zero. One refresh later the tile is re-read under the new document:
    governance.version() moved, so the cache key moved with it, the frame
    cached under the old document is gone, and the newly denied column is not
    in the tile, the base frame or the slicer catalog."""
    ana = _analyst(client)
    r = _pin(client, ana)
    assert r.status_code == 201, r.text
    did = r.json()["dashboard_id"]

    body = client.post(f"/api/dashboards/{did}/data", headers=ana).json()
    tile = body["tiles"][0]
    assert tile["cached"] is False
    assert "city" in [c.lower() for c in tile["columns"]] and tile["rows"]
    key_loose, _ = dashboards._cache_key("analyst", "demo", CUSTOMERS_SQL)
    assert key_loose in dashboards._CACHE

    _apply_elsewhere(STRICTER_YAML)
    tile = client.post(f"/api/dashboards/{did}/data", headers=ana).json()["tiles"][0]
    assert tile["cached"] is True                       # inside this process's TTL
    assert "city" in [c.lower() for c in tile["columns"]]

    governance._FRESH["at"] -= 3600                     # one refresh interval later
    body = client.post(f"/api/dashboards/{did}/data", headers=ana).json()
    tile = body["tiles"][0]
    cols = [c.lower() for c in tile["columns"]]
    assert "city" not in cols and "name" not in cols and tile["rows"]
    assert tile["cached"] is False                      # a new version = a new key
    assert key_loose not in dashboards._CACHE           # on_change dropped the old frame
    assert "city" not in {f["col"].lower() for f in body["fields"]}
    user = db.get_user_by_email("analyst@studio.local")
    tid = body["tiles"][0]["tile_id"]
    base = dashboards.tile_data(dashboards.get_tile(did, tid), user)["_base"]
    assert "city" not in [c.lower() for c in base["columns"]]
