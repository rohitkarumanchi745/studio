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


def test_probe_and_browse_never_reflect_connector_secrets(client, monkeypatch):
    """SDK errors sometimes include their connection kwargs.  Neither API may
    echo a submitted token, DSN, or decoded DSN credential back to the UI."""
    from app import connections

    class LeakyConnector:
        def __init__(self, name, cfg):
            self.cfg = cfg

        def configured(self):
            return True

        def list_tables(self):
            raise RuntimeError(
                "login failed for alice with p@ss at postgresql://alice:p@ss@db.internal/acme")

        def list_namespaces(self):
            raise RuntimeError("driver args included token=" + self.cfg["token"])

    monkeypatch.setitem(connections.TYPES, "leaky", {
        "label": "Leaky", "dialect": "postgres", "build": LeakyConnector,
        "hint_key": "dsn", "ns": {"schema": "schema", "database": ""},
        "fields": [connections._field("dsn", "DSN", required=True, secret=True),
                   connections._field("token", "Token", required=True, secret=True),
                   connections._field("schema", "Schema", required=True)],
    })
    _login(client)
    cfg = {"dsn": "postgresql://alice:p%40ss@db.internal/acme",
           "token": "swordfish-token", "schema": "public"}

    for endpoint in ("test", "browse"):
        body = client.post(f"/api/connections/{endpoint}", json={
            "ctype": "leaky", "config": cfg,
        }).json()
        assert body["ok"] is False
        rendered = body["error"]
        for secret in (cfg["dsn"], cfg["token"], "alice", "p@ss"):
            assert secret not in rendered
        assert "redacted" in rendered


def test_transformed_nested_credentials_are_never_reflected():
    """SDKs decode JSON/PEM material before raising, so exact-string
    replacement is not a sufficient response boundary."""
    from app import connections

    ctype = {
        "fields": [connections._field(
            "credentials_json", "Service-account JSON", secret=True)],
    }
    cfg = {"credentials_json": '{"private_key":"TOP\\nSECRET"}'}
    rendered = connections._safe_connection_error(
        RuntimeError("invalid private key TOP\nSECRET"), ctype, cfg)
    assert rendered == "connection failed (credential details redacted)"
    assert "TOP" not in rendered and "SECRET" not in rendered


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


def test_catalog_hides_dynamic_namespace_from_roles_without_source_access(client, monkeypatch):
    _stub_type(monkeypatch)
    _login(client)
    created = client.post("/api/connections", json={
        "name": "finance", "ctype": "stub",
        "config": {"host": "crm.internal", "token": SECRET,
                   "database": "finance_prod", "schema": "restricted"},
    })
    assert created.status_code == 201, created.text
    assert created.json()["namespace"] == "finance_prod.restricted"

    # An allowed role may use the scope label to distinguish several sources
    # backed by one warehouse.
    admin_source = next(s for s in client.get("/api/catalog/sources").json()
                        if s["name"] == "finance")
    assert admin_source["allowed"] is True
    assert admin_source["namespace"] == "finance_prod.restricted"

    # The source name/status remains visible for the disabled picker, but the
    # ungranted database/schema follows the catalog metadata access boundary.
    _login(client, "viewer@studio.local", "viewer123")
    viewer_source = next(s for s in client.get("/api/catalog/sources").json()
                         if s["name"] == "finance")
    assert viewer_source["allowed"] is False
    assert "namespace" not in viewer_source


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
    # Duplicate/blank rows were fully consumed, not truncated.
    assert body["truncated"] is False


def test_browse_marks_only_real_unique_or_connector_caps_as_truncated(monkeypatch):
    from app import connections

    class ManyNamespaces(_Stub):
        def list_namespaces(self):
            self.namespaces_truncated = self.cfg.get("upstream") == "truncated"
            count = int(self.cfg.get("count", 2))
            return [{"database": "d", "schema": f"s{i}"} for i in range(count)]

    monkeypatch.setitem(connections.TYPES, "many", {
        "label": "Many", "dialect": "ansi", "build": ManyNamespaces,
        "hint_key": "host", "fields": [], "ns": {"schema": "schema", "database": "database"},
    })
    capped = connections._browse("many", {"count": "501"})
    assert len(capped["namespaces"]) == 500 and capped["truncated"] is True
    upstream = connections._browse("many", {"count": "2", "upstream": "truncated"})
    assert len(upstream["namespaces"]) == 2 and upstream["truncated"] is True


def test_fernet_derivation_is_cached_per_studio_secret(monkeypatch):
    from app import bootstrap, connections

    connections._fernet_for_secret.cache_clear()
    monkeypatch.setattr(bootstrap, "jwt_secret", lambda: "one-secret")
    first = connections._fernet()
    assert connections._fernet() is first
    assert connections._fernet_for_secret.cache_info().misses == 1

    monkeypatch.setattr(bootstrap, "jwt_secret", lambda: "rotated-secret")
    rotated = connections._fernet()
    assert rotated is not first
    assert connections._fernet_for_secret.cache_info().misses == 2


def test_browse_discovers_namespace_before_strict_table_probe(client, monkeypatch):
    """The picker must authenticate without already knowing the namespace it
    exists to discover; save still runs the full namespace/table probe."""
    from app import connections

    class ScopedStub(_Stub):
        def configured(self):
            return bool(self.cfg.get("token") and self.cfg.get("database")
                        and self.cfg.get("schema"))

    monkeypatch.setitem(connections.TYPES, "scoped", {
        "label": "Scoped DB", "dialect": "ansi", "build": ScopedStub,
        "hint_key": "host", "ns": {"schema": "schema", "database": "database"},
        "fields": [connections._field("host", "Host", required=True),
                   connections._field("token", "Token", required=True, secret=True),
                   connections._field("database", "Database", required=True),
                   connections._field("schema", "Schema", required=True)],
    })
    _login(client)
    credentials = {"host": "crm.internal", "token": SECRET}

    browse = client.post("/api/connections/browse", json={
        "ctype": "scoped", "config": credentials,
    }).json()
    assert browse["ok"] is True
    assert browse["namespaces"][0] == {"database": "acme", "schema": "public"}

    strict = client.post("/api/connections/test", json={
        "ctype": "scoped", "config": credentials,
    }).json()
    assert strict["ok"] is False
    assert "database" in strict["error"] and "schema" in strict["error"]
    refused = client.post("/api/connections", json={
        "name": "scoped-crm", "ctype": "scoped", "config": credentials,
    })
    assert refused.status_code == 400

    selected = {**credentials, "database": "acme", "schema": "public"}
    saved = client.post("/api/connections", json={
        "name": "scoped-crm", "ctype": "scoped", "config": selected,
    })
    assert saved.status_code == 201, saved.text


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


# ── Dynamic connector lifecycle ─────────────────────────────────────────

def test_dynamic_warehouse_connectors_initialize_parent_runtime_state():
    """Stored-config subclasses must retain the pools/locks owned by their
    parent connectors.  This exercises actual metadata calls, which used to
    fail with AttributeError before reaching a vendor SDK."""
    from types import SimpleNamespace
    from app.connections import _DynBigQuery, _DynDatabricks, _DynPostgres, _DynSnowflake

    class PostgresConnection:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    postgres = _DynPostgres("pg", {
        "dsn": "postgresql://user:secret@db.internal/acme", "schema": "public",
    })
    pg_connection = PostgresConnection()
    postgres._pool_conn = pg_connection
    postgres.close()
    postgres.close()                 # idempotent: do not close/release twice
    assert pg_connection.close_calls == 1
    assert postgres._pool_conn is None

    class SnowCursor:
        description = [("name",), ("database_name",)]

        def execute(self, statement):
            assert statement == "SHOW SCHEMAS IN ACCOUNT"

        def fetchall(self):
            return [("PUBLIC", "ACME"), ("INFORMATION_SCHEMA", "ACME")]

    class SnowConnection:
        def cursor(self):
            return SnowCursor()

        def close(self):
            pass

    snow = _DynSnowflake("snow", {
        "account": "acct", "user": "user", "password": "secret", "database": "ACME",
    })
    assert snow._pool_conn is None and snow._pool_lock is not None
    snow._pool_conn = SnowConnection()
    assert snow.list_namespaces() == [{"database": "ACME", "schema": "PUBLIC"}]
    snow.close()

    class DatabricksCursor:
        def __init__(self):
            self.statement = ""

        def execute(self, statement):
            self.statement = statement

        def fetchall(self):
            if self.statement == "SHOW CATALOGS":
                return [("main",)]
            assert self.statement == "SHOW SCHEMAS IN `main`"
            return [("default",), ("information_schema",)]

    class DatabricksConnection:
        def cursor(self):
            return DatabricksCursor()

        def close(self):
            pass

    databricks = _DynDatabricks("dbx", {
        "server_hostname": "workspace", "http_path": "/sql/warehouse",
        "access_token": "secret",
    })
    assert databricks._pool_conn is None and databricks._pool_lock is not None
    databricks._pool_conn = DatabricksConnection()
    assert databricks.list_namespaces() == [{"database": "main", "schema": "default"}]
    databricks.close()

    class BigQueryClient:
        project = "credential-project"

        def list_datasets(self, project=None):
            assert project == "credential-project"
            return [SimpleNamespace(dataset_id="Events"),
                    SimpleNamespace(dataset_id="analytics")]

        def close(self):
            pass

    bigquery = _DynBigQuery("bq", {"credentials_json": "{}"})
    assert bigquery._client_obj is None and bigquery._client_lock is not None
    bigquery._client_obj = BigQueryClient()
    assert bigquery.list_namespaces() == [
        {"database": "credential-project", "schema": "Events"},
        {"database": "credential-project", "schema": "analytics"},
    ]
    bigquery.close()


def test_throwaway_and_cached_connectors_are_closed(client, monkeypatch):
    from app import bootstrap, connections

    class ClosableStub(_Stub):
        instances = []

        def __init__(self, name, cfg):
            super().__init__(name, cfg)
            self.closed = False
            self.__class__.instances.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setitem(connections.TYPES, "closable", {
        "label": "Closable", "dialect": "ansi", "build": ClosableStub,
        "hint_key": "host", "fields": [connections._field("host", "Host", required=True),
                                         connections._field("token", "Token", required=True,
                                                            secret=True)],
    })
    _login(client)

    browsed = client.post("/api/connections/browse", json={
        "ctype": "closable", "config": {"host": "h", "token": SECRET},
    }).json()
    assert browsed["ok"] is True
    assert ClosableStub.instances[-1].closed is True

    created = client.post("/api/connections", json={
        "name": "closable-source", "ctype": "closable",
        "config": {"host": "h", "token": SECRET},
    })
    assert created.status_code == 201, created.text
    assert ClosableStub.instances[-1].name == "__probe__"
    assert ClosableStub.instances[-1].closed is True

    cached = connections.resolve("closable-source")
    assert cached.closed is False
    deleted = client.delete(f"/api/connections/{created.json()['id']}")
    assert deleted.status_code == 200
    assert cached.closed is True

    # A runtime Studio-secret rotation invalidates encrypted rows immediately,
    # even when resolve() already holds an open connector for that row.
    rotated = client.post("/api/connections", json={
        "name": "rotating-source", "ctype": "closable",
        "config": {"host": "h", "token": SECRET},
    })
    assert rotated.status_code == 201, rotated.text
    cached_after_create = connections.resolve("rotating-source")
    assert cached_after_create.closed is False
    monkeypatch.setattr(bootstrap, "jwt_secret", lambda: "a-new-runtime-secret")
    assert connections.resolve("rotating-source") is None
    assert cached_after_create.closed is True
