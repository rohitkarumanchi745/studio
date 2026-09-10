"""User-connected databases — admin adds a warehouse from the UI, encrypted at
rest, registered as a first-class source behind the SAME chokepoints as
env-configured sources.

Locked in here:
- create/list/delete are ADMIN-only; secrets never appear in any response and
  the stored row is ciphertext (encryption-at-rest proven against the raw DB);
- a saved connection resolves through connectors.get_connector and appears in
  /catalog/sources; non-admin roles get the standard fail-closed treatment
  (allowed=False, 403 on tables) until governance grants the source;
- a failing probe blocks the save; name rules reject builtins/duplicates/bad
  slugs; a row that no longer decrypts fails CLOSED (unconfigured, no resolve).
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "conn_app.db"))
    monkeypatch.setenv("STUDIO_SECRET", "conn-secret-gamma")
    monkeypatch.setenv("STUDIO_GRAPH_SYNC_TICKER", "0")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import connections
    connections._CACHE.clear()
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    connections._CACHE.clear()


def _login(c, email="admin@studio.local", password="admin123"):
    tok = c.post("/api/auth/login",
                 json={"email": email, "password": password}).json()["access_token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


class _Stub:
    """A connector double: configured/list_tables/get_schema, no network."""
    dialect = "ansi"

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg

    def configured(self):
        return bool(self.cfg.get("token"))

    def list_tables(self):
        if self.cfg.get("token") == "bad":
            raise RuntimeError("auth failed for host " + self.cfg.get("host", ""))
        return ["orders", "users"]

    def get_schema(self, table):
        return [{"name": "id", "type": "int"}]


def _stub_type(monkeypatch):
    from app import connections
    monkeypatch.setitem(connections.TYPES, "stub", {
        "label": "Stub DB", "dialect": "ansi", "build": _Stub, "hint_key": "host",
        "fields": [connections._field("host", "Host", required=True),
                   connections._field("token", "Token", required=True, secret=True)],
    })


SECRET = "s3cret-tok-xyz"


def _create(c, name="crm", token=SECRET):
    return c.post("/api/connections", json={
        "name": name, "ctype": "stub",
        "config": {"host": "crm.internal", "token": token}})


# ── CRUD + encryption at rest ────────────────────────────────────────────

def test_admin_creates_lists_and_secret_never_leaves(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    r = _create(client)
    assert r.status_code == 201, r.text
    assert SECRET not in r.text                      # secret absent from response

    listing = client.get("/api/connections")
    assert listing.status_code == 200
    row = listing.json()[0]
    assert row["name"] == "crm" and row["type_label"] == "Stub DB"
    assert row["hint"] == "crm.internal" and row["configured"] is True
    assert SECRET not in listing.text

    # encryption at rest: the raw DB row is ciphertext, not the secret
    from app import db
    c = db._conn()
    raw = dict(c.execute("SELECT * FROM data_connections").fetchone())
    c.close()
    assert SECRET not in raw["config"] and "crm.internal" not in raw["config"]


def test_probe_failure_blocks_save(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    r = _create(client, token="bad")
    assert r.status_code == 400 and "connection test failed" in r.json()["detail"]
    assert client.get("/api/connections").json() == []


def test_test_endpoint_reports_tables(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    ok = client.post("/api/connections/test",
                     json={"ctype": "stub", "config": {"host": "h", "token": "t"}}).json()
    assert ok["ok"] is True and ok["tables"] == 2 and "orders" in ok["sample"]
    bad = client.post("/api/connections/test",
                      json={"ctype": "stub", "config": {"host": "h"}}).json()
    assert bad["ok"] is False and "missing required fields" in bad["error"]


def test_name_rules(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    assert _create(client, name="demo").status_code == 400        # builtin collision
    assert _create(client, name="Bad Name!").status_code == 400   # slug
    assert _create(client, name="crm").status_code == 201
    assert _create(client, name="crm").status_code == 400         # duplicate


# ── Registry + catalog integration, role fail-closed ─────────────────────

def test_source_registers_and_roles_fail_closed(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    assert _create(client).status_code == 201

    from app import connections
    from app.connectors import get_connector
    assert connections.resolve("crm") is not None
    assert get_connector("crm").list_tables() == ["orders", "users"]

    srcs = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert srcs["crm"]["allowed"] is True and srcs["crm"]["configured"] is True
    assert client.get("/api/catalog/sources/crm/tables").json() == ["orders", "users"]

    # a viewer sees the standard fail-closed treatment until governance grants it
    _login(client, "viewer@studio.local", "viewer123")
    srcs = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert srcs["crm"]["allowed"] is False
    assert client.get("/api/catalog/sources/crm/tables").status_code == 403


def test_delete_unregisters(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    cid = _create(client).json()["id"]
    assert client.delete(f"/api/connections/{cid}").status_code == 200
    from app import connections
    assert connections.resolve("crm") is None
    from app.connectors import get_connector
    with pytest.raises(KeyError):
        get_connector("crm")                       # fully unregistered
    # catalog treats a vanished source like any unknown name (403 at the
    # allowed-sources gate, its existing semantics for every unknown source)
    assert client.get("/api/catalog/sources/crm/tables").status_code == 403


def test_non_admin_cannot_manage(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client, "analyst@studio.local", "analyst123")
    assert client.get("/api/connections").status_code == 403
    assert _create(client).status_code == 403
    assert client.post("/api/connections/test",
                       json={"ctype": "stub", "config": {}}).status_code == 403
    assert client.get("/api/connections/types").status_code == 403


# ── Fail closed when the row no longer decrypts ──────────────────────────

def test_undecryptable_row_fails_closed(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    _create(client)
    from app import connections, db
    c = db._conn()
    c.execute("UPDATE data_connections SET config=?", ("not-a-fernet-token",))
    c.commit()
    c.close()
    connections._CACHE.clear()
    assert connections.resolve("crm") is None                      # no connector
    row = client.get("/api/connections").json()[0]
    assert row["configured"] is False and row["hint"] == ""        # nothing leaks
    srcs = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert srcs["crm"]["configured"] is False                      # picker shows it dark


def test_types_endpoint_shapes_the_form(client, monkeypatch):
    _login(client)
    types = {t["ctype"]: t for t in client.get("/api/connections/types").json()}
    assert "postgres" in types and types["postgres"]["dialect"] == "postgres"
    dsn = next(f for f in types["postgres"]["fields"] if f["key"] == "dsn")
    assert dsn["required"] is True and dsn["secret"] is True
