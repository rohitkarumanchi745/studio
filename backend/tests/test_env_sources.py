"""Built-in (env-configured) sources, managed from the Data connections screen.

A source like `databricks` used to change only in the deployment's environment.
An admin can now replace it with a credential entered on the screen, switch it
off and on, or hand it back to the environment — and it keeps its NAME, so the
grants, dashboards and pipelines written against it follow it.

Locked in here:
- the setting wins over the environment on EVERY path: connectors.get_connector
  and all_sources, hence the gateway, RBAC and the catalog;
- replacing is probed before it is saved, stored encrypted, never returned;
- a disconnected source — or a replacement that no longer decrypts — fails
  CLOSED with its reason, and never falls back to the environment's credential;
- reconnect re-probes a stored replacement; "back to environment" forgets both;
- admin-only; only the warehouse built-ins are manageable (not demo).
"""
import importlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

SECRET = "env-s3cret-tok"
NEW_SECRET = "replacement-s3cret-tok"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "env_app.db"))
    monkeypatch.setenv("STUDIO_SECRET", "env-conn-secret-epsilon")
    monkeypatch.setenv("STUDIO_GRAPH_SYNC_TICKER", "0")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import connections
    connections._CACHE.clear()
    connections._ENV_CACHE.clear()
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    connections._CACHE.clear()
    connections._ENV_CACHE.clear()


def _login(c, email="admin@studio.local", password="admin123"):
    tok = c.post("/api/auth/login",
                 json={"email": email, "password": password}).json()["access_token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


class _Stub:
    """A warehouse double: what it reads is visible in its table names."""
    dialect = "ansi"

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg

    def configured(self):
        return bool(self.cfg.get("token"))

    def list_tables(self):
        if self.cfg.get("token") == "bad":
            raise RuntimeError("auth failed")
        return [f"{self.cfg.get('host', '?').split('.')[0]}_orders"]

    def get_schema(self, table):
        return [{"name": "id", "type": "int"}]

    def list_namespaces(self):
        return [{"database": "", "schema": "public"}]

    def qualifiers(self):
        return frozenset()


@pytest.fixture()
def wh(monkeypatch):
    """A built-in source named `wh`, configured by the 'environment', whose
    connection type is also called `wh` — the shape databricks/postgres have."""
    from app import connections, connectors, policies
    env = _Stub("wh", {"host": "envwh.internal", "token": SECRET})
    monkeypatch.setitem(connectors._REGISTRY, "wh", env)
    # Granted like the real built-ins are (policies.POLICIES), so RBAC lets the
    # admin through and the test sees what the SOURCE does, not a 403.
    monkeypatch.setitem(policies.POLICIES["admin"], "wh", "*")
    monkeypatch.setitem(connections.TYPES, "wh", {
        "label": "Stub WH", "dialect": "ansi", "build": _Stub, "hint_key": "host",
        "ns": {"schema": "schema", "database": ""},
        "fields": [connections._field("host", "Host", required=True),
                   connections._field("token", "Token", required=True, secret=True),
                   connections._field("schema", "Schema", default="public")],
    })
    return env


def _status(c, name="wh"):
    return next(s for s in c.get("/api/connections/env").json() if s["name"] == name)


def _tables(name="wh"):
    from app import connectors
    return connectors.get_connector(name).list_tables()


def test_a_built_in_source_starts_on_the_environment(client, wh):
    _login(client)
    st = _status(client)
    assert st["mode"] == "environment" and st["env_configured"] is True
    assert _tables() == ["envwh_orders"]


def test_replacing_keeps_the_name_and_wins_everywhere(client, wh):
    _login(client)
    r = client.put("/api/connections/env/wh",
                   json={"config": {"host": "newwh.example", "token": NEW_SECRET}})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "replaced" and r.json()["hint"] == "newwh.example"
    assert NEW_SECRET not in r.text and NEW_SECRET not in client.get("/api/connections/env").text
    # The registry — and so the gateway, RBAC and the catalog — reads the new one.
    assert _tables() == ["newwh_orders"]
    from app import connectors
    assert next(s for s in connectors.all_sources() if s["name"] == "wh")["configured"] is True
    tables = client.get("/api/catalog/sources/wh/tables")
    assert tables.status_code == 200 and tables.json() == ["newwh_orders"]
    # Encrypted at rest.
    from app import db
    c = db._conn()
    raw = c.execute("SELECT config FROM env_source_settings WHERE name='wh'").fetchone()[0]
    c.close()
    assert NEW_SECRET not in raw and "newwh" not in raw


def test_a_replacement_is_probed_before_it_is_saved(client, wh):
    _login(client)
    r = client.put("/api/connections/env/wh", json={"config": {"host": "x.example", "token": "bad"}})
    assert r.status_code == 400 and "connection test failed" in r.json()["detail"]
    assert _status(client)["mode"] == "environment" and _tables() == ["envwh_orders"]


def test_disconnect_fails_closed_with_its_reason_and_reconnect_restores(client, wh):
    _login(client)
    assert client.post("/api/connections/env/wh/disconnect").json()["mode"] == "disconnected"
    from app import connectors, sources
    assert next(s for s in connectors.all_sources() if s["name"] == "wh")["configured"] is False
    with pytest.raises(HTTPException) as e:
        sources.connector_or_400("wh")
    assert e.value.status_code == 400 and "disconnected on the Data connections" in e.value.detail
    assert client.get("/api/catalog/sources/wh/tables").status_code == 400
    # Only the switch was set: reconnecting hands it back to the environment.
    assert client.post("/api/connections/env/wh/reconnect").json()["mode"] == "environment"
    assert _tables() == ["envwh_orders"]


def test_a_disconnected_replacement_comes_back_on_its_own_credential(client, wh):
    _login(client)
    client.put("/api/connections/env/wh", json={"config": {"host": "newwh.example", "token": NEW_SECRET}})
    client.post("/api/connections/env/wh/disconnect")
    st = _status(client)
    assert st["mode"] == "disconnected" and st["replaced"] is True
    assert client.post("/api/connections/env/wh/reconnect").json()["mode"] == "replaced"
    assert _tables() == ["newwh_orders"]


def test_back_to_environment_forgets_the_replacement(client, wh):
    _login(client)
    client.put("/api/connections/env/wh", json={"config": {"host": "newwh.example", "token": NEW_SECRET}})
    assert client.delete("/api/connections/env/wh").json()["mode"] == "environment"
    assert _tables() == ["envwh_orders"]


def test_an_unreadable_replacement_fails_closed_never_back_to_the_environment(client, wh, monkeypatch):
    _login(client)
    client.put("/api/connections/env/wh", json={"config": {"host": "newwh.example", "token": NEW_SECRET}})
    from app import connections, connectors
    monkeypatch.setattr(connections, "_decrypt", lambda token: None)     # STUDIO_SECRET rotated
    conn = connectors.get_connector("wh")
    assert conn.configured() is False and "can no longer be read" in conn.unconfigured_reason
    r = client.post("/api/connections/env/wh/reconnect")
    assert r.status_code == 400 and "can no longer be read" in r.json()["detail"]


def test_only_admins_and_only_warehouse_built_ins(client, wh):
    _login(client)
    assert client.put("/api/connections/env/demo",
                      json={"config": {"host": "x", "token": "y"}}).status_code == 404
    assert client.post("/api/connections/env/nope/disconnect").status_code == 404
    assert "demo" not in {s["name"] for s in client.get("/api/connections/env").json()}
    _login(client, "analyst@studio.local", "analyst123")
    assert client.get("/api/connections/env").status_code == 403
    assert client.post("/api/connections/env/wh/disconnect").status_code == 403
    assert client.put("/api/connections/env/wh",
                      json={"config": {"host": "x", "token": "y"}}).status_code == 403
    assert client.delete("/api/connections/env/wh").status_code == 403


def test_a_replacement_must_name_its_one_namespace(client, wh, monkeypatch):
    from app import connections
    connections.TYPES["wh"]["fields"][2] = connections._field("schema", "Schema")   # no default
    _login(client)
    r = client.put("/api/connections/env/wh", json={"config": {"host": "x.example", "token": NEW_SECRET}})
    assert r.status_code == 400 and "pick a schema" in r.json()["detail"]
