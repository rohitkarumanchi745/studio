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
  slugs; a row that no longer decrypts fails CLOSED (unconfigured, no resolve);
- disconnect is reversible: the source is offline on every read path but keeps
  its credential and its name; reconnect re-probes before bringing it back.
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

    def list_namespaces(self):
        if self.cfg.get("token") == "bad":
            raise RuntimeError("auth failed for host " + self.cfg.get("host", ""))
        # Duplicated and blank-schema rows on purpose: SHOW output really does
        # repeat pairs, and the endpoint has to fold them.
        return [{"database": "acme", "schema": "public"},
                {"database": "acme", "schema": "public"},
                {"database": "acme", "schema": "analytics"},
                {"database": "acme", "schema": ""}]


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


def test_disconnect_takes_the_source_offline_and_reconnect_restores_it(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    cid = _create(client).json()["id"]
    from app import connections, rbac
    from app.connectors import get_connector

    r = client.post(f"/api/connections/{cid}/disconnect")
    assert r.status_code == 200 and r.json()["enabled"] is False
    # Offline on every read path, exactly like a deleted source…
    assert connections.resolve("crm") is None
    with pytest.raises(KeyError):
        get_connector("crm")
    assert "crm" not in {s["name"] for s in client.get("/api/catalog/sources").json()}
    assert "crm" not in rbac.allowed_sources("admin")
    assert client.get("/api/catalog/sources/crm/tables").status_code == 403
    # …but still listed for the admin, and still owning its name.
    row = next(c for c in client.get("/api/connections").json() if c["id"] == cid)
    assert row["enabled"] is False and row["hint"] == "crm.internal"
    taken = _create(client)
    assert taken.status_code == 400 and "disconnected" in taken.json()["detail"]
    # Idempotent: a second disconnect is not an error.
    assert client.post(f"/api/connections/{cid}/disconnect").status_code == 200

    r = client.post(f"/api/connections/{cid}/reconnect")
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert get_connector("crm").list_tables() == ["orders", "users"]
    assert "crm" in {s["name"] for s in client.get("/api/catalog/sources").json()}


def test_reconnect_reprobes_the_stored_credential(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    cid = _create(client).json()["id"]
    client.post(f"/api/connections/{cid}/disconnect")

    # The warehouse starts refusing the credential while the source is parked.
    monkeypatch.setattr(_Stub, "list_tables",
                        lambda self: (_ for _ in ()).throw(RuntimeError("password expired")))
    r = client.post(f"/api/connections/{cid}/reconnect")
    assert r.status_code == 400 and "password expired" in r.json()["detail"]
    assert SECRET not in r.text
    from app import connections
    assert connections.resolve("crm") is None                    # stays offline

    # A row that no longer decrypts cannot come back in place.
    from app import db
    c = db._conn()
    c.execute("UPDATE data_connections SET config=?", ("not-a-fernet-token",))
    c.commit()
    c.close()
    r = client.post(f"/api/connections/{cid}/reconnect")
    assert r.status_code == 400 and "remove it and connect again" in r.json()["detail"]

    # A disconnected source can still be removed outright, freeing the name.
    assert client.delete(f"/api/connections/{cid}").status_code == 200
    monkeypatch.setattr(_Stub, "list_tables", lambda self: ["orders", "users"])
    assert _create(client).status_code == 201


def test_non_admin_cannot_manage(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    cid = _create(client).json()["id"]
    _login(client, "analyst@studio.local", "analyst123")
    assert client.get("/api/connections").status_code == 403
    assert _create(client, name="crm2").status_code == 403
    assert client.post("/api/connections/test",
                       json={"ctype": "stub", "config": {}}).status_code == 403
    assert client.get("/api/connections/types").status_code == 403
    assert client.post(f"/api/connections/{cid}/disconnect").status_code == 403
    assert client.post(f"/api/connections/{cid}/reconnect").status_code == 403
    assert client.delete(f"/api/connections/{cid}").status_code == 403


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


# ── Browsing namespaces before binding a source ──────────────────────────

def test_browse_lists_namespaces_and_folds_duplicates(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    r = client.post("/api/connections/browse", json={
        "ctype": "stub", "config": {"host": "crm.internal", "token": SECRET}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # Deduplicated, and the blank schema is dropped — it names no namespace.
    assert body["namespaces"] == [{"database": "acme", "schema": "public"},
                                  {"database": "acme", "schema": "analytics"}]


def test_browse_reports_failure_as_data_not_a_500(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    r = client.post("/api/connections/browse", json={
        "ctype": "stub", "config": {"host": "crm.internal", "token": "bad"}})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert r.json()["namespaces"] == []
    # A listing failure must not block connecting — the admin can still type it.
    assert _create(client).status_code == 201


def test_browse_is_admin_only_and_never_echoes_the_credential(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    body = client.post("/api/connections/browse", json={
        "ctype": "stub", "config": {"host": "crm.internal", "token": SECRET}}).text
    assert SECRET not in body
    _login(client, "analyst@studio.local", "analyst123")
    assert client.post("/api/connections/browse", json={
        "ctype": "stub", "config": {}}).status_code == 403


def test_browse_never_widens_what_a_saved_source_may_read(client, monkeypatch):
    """Browsing shows several schemas; the source that gets saved is still
    pinned to the one it was configured with. This is the whole safety story
    for the connect screen's schema picker."""
    _stub_type(monkeypatch)
    _login(client)
    seen = {n["schema"] for n in client.post("/api/connections/browse", json={
        "ctype": "stub", "config": {"host": "crm.internal", "token": SECRET}}).json()["namespaces"]}
    assert seen == {"public", "analytics"}

    # The invariant, on a REAL connector: qualifiers() — the set the query
    # guard enforces — reports only the schema the connection was CONFIGURED
    # with, no matter what browsing turned up. Reading it needs no network.
    from app.connections import _DynPostgres
    pinned = _DynPostgres("pg", {"dsn": "postgresql://u:p@h:5432/acme",
                                 "schema": "public"}).qualifiers()
    assert pinned == frozenset({"public", "acme.public"})
    assert not any("analytics" in q for q in pinned)


def test_each_saved_source_reports_its_own_namespace(client, monkeypatch):
    _stub_type(monkeypatch)
    monkeypatch.setitem(connections_types(), "stub", dict(
        connections_types()["stub"],
        fields=[connections_field("host", "Host", required=True),
                connections_field("token", "Token", required=True, secret=True),
                connections_field("schema", "Schema", default="public"),
                connections_field("database", "Database")]))
    _login(client)
    for name, schema in (("crm_public", "public"), ("crm_analytics", "analytics")):
        r = client.post("/api/connections", json={
            "name": name, "ctype": "stub",
            "config": {"host": "crm.internal", "token": SECRET,
                       "database": "acme", "schema": schema}})
        assert r.status_code == 201, r.text
        assert r.json()["namespace"] == f"acme.{schema}"

    # Two schemas of one warehouse are two sources, each independently listed.
    by_name = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert by_name["crm_public"]["namespace"] == "acme.public"
    assert by_name["crm_analytics"]["namespace"] == "acme.analytics"
    assert by_name["crm_public"]["type_label"] == "Stub DB"


def connections_types():
    from app import connections
    return connections.TYPES


def connections_field(*a, **kw):
    from app import connections
    return connections._field(*a, **kw)


# ── Adding schemas to a live source ──────────────────────────────────────

def _ns_stub_type(monkeypatch):
    """The stub type, with a schema + database the picker can write into."""
    from app import connections
    monkeypatch.setitem(connections.TYPES, "stub", {
        "label": "Stub DB", "dialect": "ansi", "build": _Stub, "hint_key": "host",
        "ns": {"schema": "schema", "database": "database"},
        "fields": [connections._field("host", "Host", required=True),
                   connections._field("token", "Token", required=True, secret=True),
                   connections._field("database", "Database"),
                   connections._field("schema", "Schema", default="public")],
    })


class _EnvStub(_Stub):
    """An env-configured source: its credential lives in the deployment."""
    def __init__(self, token=SECRET):
        super().__init__("stubenv", {"host": "wh.internal", "token": token,
                                     "database": "acme", "schema": "public"})

    def _cfg(self):
        return dict(self.cfg)


def _env_source(monkeypatch, token=SECRET):
    from app import connections, connectors
    env = _EnvStub(token)
    monkeypatch.setitem(connectors._REGISTRY, "stubenv", env)
    monkeypatch.setattr(connections, "_ENV_TYPES", ((_EnvStub, "stub"),))
    return env


def _stored(name):
    from app import connections, db
    c = db._conn()
    raw = c.execute("SELECT config FROM data_connections WHERE name=?", (name,)).fetchone()[0]
    c.close()
    return connections._decrypt(raw)


def test_a_connection_lists_its_schemas_without_the_credential(client, monkeypatch):
    _ns_stub_type(monkeypatch)
    _login(client)
    assert client.post("/api/connections", json={
        "name": "crm", "ctype": "stub",
        "config": {"host": "crm.internal", "token": SECRET, "database": "acme"}}).status_code == 201
    r = client.get("/api/connections/sources/crm/namespaces")
    assert r.status_code == 200 and SECRET not in r.text
    body = r.json()
    assert {n["schema"] for n in body["namespaces"]} == {"public", "analytics"}
    assert body["current"] == {"schema": "public", "database": "acme"}


def test_adding_a_schema_creates_an_independent_pinned_source(client, monkeypatch):
    _ns_stub_type(monkeypatch)
    _login(client)
    client.post("/api/connections", json={
        "name": "crm", "ctype": "stub",
        "config": {"host": "crm.internal", "token": SECRET, "database": "acme"}})
    r = client.post("/api/connections/sources/crm/schemas",
                    json={"name": "crm-analytics", "schema": "analytics", "database": "acme"})
    assert r.status_code == 201, r.text
    assert r.json()["namespace"] == "acme.analytics" and SECRET not in r.text
    assert r.json()["label"] == "crm · analytics"
    # A copy, like picking two schemas at connect time: removing the parent
    # leaves the new source working.
    parent = next(c for c in client.get("/api/connections").json() if c["name"] == "crm")
    client.delete(f"/api/connections/{parent['id']}")
    from app import connectors
    assert connectors.get_connector("crm-analytics").cfg["schema"] == "analytics"


def test_the_parents_own_schema_cannot_be_added_again(client, monkeypatch):
    _ns_stub_type(monkeypatch)
    _login(client)
    client.post("/api/connections", json={
        "name": "crm", "ctype": "stub",
        "config": {"host": "crm.internal", "token": SECRET, "database": "acme"}})
    r = client.post("/api/connections/sources/crm/schemas",
                    json={"name": "crm-public", "schema": "public", "database": "acme"})
    assert r.status_code == 400 and "already pinned" in r.json()["detail"]


def test_an_env_source_binds_schemas_by_reference_never_by_copy(client, monkeypatch):
    _ns_stub_type(monkeypatch)
    env = _env_source(monkeypatch)
    _login(client)
    assert client.get("/api/connections/sources/stubenv/namespaces").json()["current"] == {
        "schema": "public", "database": "acme"}
    r = client.post("/api/connections/sources/stubenv/schemas",
                    json={"name": "wh-analytics", "schema": "analytics", "database": "acme"})
    assert r.status_code == 201, r.text
    # The deployment's secret is not in the table — only where to find it.
    assert _stored("wh-analytics") == {"from_env": "stubenv", "schema": "analytics",
                                       "database": "acme"}
    from app import connections, connectors
    assert connectors.get_connector("wh-analytics").cfg["token"] == SECRET

    # Rotating the env credential reaches the derived source (on the restart
    # that rotation implies — here, a cleared cache).
    env.cfg["token"] = "rotated-tok"
    connections._CACHE.clear()
    assert connectors.get_connector("wh-analytics").cfg["token"] == "rotated-tok"

    # An env source that is switched off takes its derived sources with it.
    env.cfg["token"] = ""
    connections._CACHE.clear()
    assert connections.resolve("wh-analytics") is None
    srcs = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert srcs["wh-analytics"]["configured"] is False


def test_a_source_derived_from_env_passes_on_the_reference(client, monkeypatch):
    _ns_stub_type(monkeypatch)
    _env_source(monkeypatch)
    _login(client)
    client.post("/api/connections/sources/stubenv/schemas",
                json={"name": "wh-analytics", "schema": "analytics", "database": "acme"})
    r = client.post("/api/connections/sources/wh-analytics/schemas",
                    json={"name": "wh-staging", "schema": "staging", "database": "acme"})
    assert r.status_code == 201, r.text
    assert _stored("wh-staging") == {"from_env": "stubenv", "schema": "staging", "database": "acme"}


def test_clients_cannot_mint_an_env_reference(client, monkeypatch):
    _ns_stub_type(monkeypatch)
    _env_source(monkeypatch)
    _login(client)
    r = client.post("/api/connections", json={
        "name": "sneaky", "ctype": "stub",
        "config": {"host": "x.internal", "token": "own-tok", "from_env": "stubenv"}})
    assert r.status_code == 201
    assert "from_env" not in _stored("sneaky")


def test_adding_schemas_is_admin_only_and_needs_a_namespaced_live_source(client, monkeypatch):
    _stub_type(monkeypatch)          # no `ns`: this type has no schemas
    _login(client)
    _create(client)
    assert client.get("/api/connections/sources/crm/namespaces").status_code == 400
    assert client.get("/api/connections/sources/nope/namespaces").status_code == 404
    _login(client, "viewer@studio.local", "viewer123")
    assert client.get("/api/connections/sources/crm/namespaces").status_code == 403
    assert client.post("/api/connections/sources/crm/schemas",
                       json={"name": "crm-x", "schema": "x"}).status_code == 403
