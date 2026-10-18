"""User-connected databases — add a warehouse from the UI, no env vars needed.

An ADMIN connects a database (Postgres / Snowflake / Databricks / BigQuery /
Neo4j) from the Data connections screen: pick a type, enter credentials, test,
save. The connection becomes a first-class source in the picker, served by the
SAME connector classes the env-configured sources use — so the gateway guard,
RBAC, governance masking and skill files apply to it unchanged.

Security model:
- Credentials are Fernet-encrypted at rest, key derived from STUDIO_SECRET with
  a connections-specific salt (domain-separated from user API keys). They are
  NEVER returned by any endpoint and never logged; listings carry only a
  non-secret hint (host / account / project).
- A rotated STUDIO_SECRET fails closed: the row no longer decrypts, resolve()
  returns None, the source shows unconfigured, and the admin reconnects.
- Visibility follows the existing source model: admin ('*') reaches every
  source; other roles see a new source only once a governance policy grants it.
- Create/delete is admin-only: pointing the server at an arbitrary DSN is
  operator power and stays behind the admin gate.
- The BUILT-IN (env-configured) warehouse sources can be managed here too —
  replaced, disconnected, reconnected or handed back to the environment —
  keeping their names (see "Built-in sources, managed from this screen").
  Same encryption, same admin gate; a disconnected or unreadable one fails
  closed and never falls back to the environment's credential.
- Adding schemas to a live source never widens it: each picked schema becomes
  a NEW source, pinned like any other. From an env-configured source the new
  row stores only a reference ({"from_env": "<source>", "schema": …}), so the
  deployment's secret never lands in this table, and a derived source follows
  its parent: a rotated, replaced or disconnected built-in source takes it
  along at once (resolve() keys its cache on the resolved config).
- Disconnect is reversible, delete is not. A disconnected row (enabled=0) is
  invisible to every read path — resolve(), source_entries() and RBAC all go
  through _rows()/_row_by_name(), which only see enabled rows — so it fails
  closed exactly like a deleted one. It keeps its ciphertext, so reconnecting
  needs no re-typed secret, and keeps its NAME, so no other connection can take
  that name and inherit the governance grants written against it.
"""
import base64
import hashlib
import json
import re
import socket
import threading
import time
import uuid
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import db
from .auth import current_user
from .connectors.base import Connector
from .connectors.bigquery_conn import BigQueryConnector
from .connectors.databricks_conn import PROBE_LIMITS as DATABRICKS_PROBE_LIMITS
from .connectors.databricks_conn import DatabricksConnector
from .connectors.databricks_conn import classify_error as classify_databricks_error
from .connectors.databricks_conn import describe_error as describe_databricks_error
from .connectors.graph_conn import GraphConnector
from .connectors.postgres_conn import PostgresConnector
from .connectors.powerbi_conn import PowerBIConnector, PowerBIError
from .connectors.snowflake_conn import SnowflakeConnector
from .connectors.sql_conn import SqlConnector
from .connectors.sql_conn import check_url as check_sql_url

router = APIRouter(prefix="/connections", tags=["connections"])

_SALT = b"studio-data-connections-v1"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")
_LOCK = threading.Lock()
_CACHE = {}          # name -> ((row id, created_at), connector)


@lru_cache(maxsize=2)
def _fernet_for_secret(secret):
    """Derive once per active Studio secret, not once per connection row.

    Namespace selection can create many independently governed sources.  A
    fresh 200k-round PBKDF2 for every row made catalog/list requests linear in
    both source count *and* KDF cost.  Keying the small cache by the secret
    keeps runtime rotation correct while retaining the intended derivation.
    """
    secret = secret.encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret)))


def _fernet():
    from . import bootstrap
    secret = bootstrap.jwt_secret() or ""
    if not secret:
        raise RuntimeError("STUDIO_SECRET is not set; cannot derive the encryption key")
    return _fernet_for_secret(secret)


# ── Dynamic connectors: the env-configured classes, fed a stored config ──
#
# Each subclass overrides ONLY the config accessors; every query still runs the
# parent's code, which is reachable solely through the gateway guard (base.py
# wraps run_query at class creation and re-wrapping is definition-scoped).

class _DynPostgres(PostgresConnector):
    def __init__(self, name, cfg):
        super().__init__()                      # per-connection pool + lock
        self.name = name
        self._dyn = cfg

    def _dsn(self):
        return (self._dyn.get("dsn") or "").strip()

    def _schema(self):
        return (self._dyn.get("schema") or "public").strip() or "public"


class _DynSnowflake(SnowflakeConnector):
    def __init__(self, name, cfg):
        super().__init__()                      # per-connection pool + lock
        self.name = name
        self._dyn = cfg
        self._browsing = name == "__browse__"

    def _cfg(self):
        base = {"account": "", "user": "", "password": "", "warehouse": "",
                "database": "", "schema": "" if self._browsing else "PUBLIC"}
        base.update({k: v for k, v in self._dyn.items() if v})
        return base


class _DynDatabricks(DatabricksConnector):
    def __init__(self, name, cfg):
        super().__init__()                      # per-connection pool + lock
        self.name = name
        self._dyn = cfg
        self._browsing = name == "__browse__"
        if name in ("__probe__", "__browse__"):
            self.connect_limits = DATABRICKS_PROBE_LIMITS   # a test: about a minute, not 15

    def _cfg(self):
        base = {"server_hostname": "", "http_path": "", "access_token": "",
                "catalog": "", "schema": "" if self._browsing else "default",
                "warehouse_id": ""}
        base.update({k: v for k, v in self._dyn.items() if v})
        return base


class _DynBigQuery(BigQueryConnector):
    def __init__(self, name, cfg):
        super().__init__()                      # cached client + lock
        self.name = name
        self._dyn = cfg

    def _cfg(self):
        base = {"project": "", "dataset": "", "location": "",
                "credentials_json": "", "key_file": ""}
        base.update({k: v for k, v in self._dyn.items() if v})
        return base


class _DynGraph(GraphConnector):
    def __init__(self, name, cfg):
        self.name = name
        self._dyn = cfg

    def _cfg(self):
        base = {"uri": "", "user": "", "password": "", "database": "neo4j"}
        base.update({k: v for k, v in self._dyn.items() if v})
        return base


class _DynSQL(SqlConnector):
    """Any database SQLAlchemy has an installed driver for. The URL names the
    engine; nothing about it is configured here (connectors/sql_conn.py)."""

    def __init__(self, name, cfg):
        super().__init__(name, (cfg or {}).get("url", ""), (cfg or {}).get("schema", ""))


class _DynPowerBI(PowerBIConnector):
    def __init__(self, name, cfg):
        super().__init__()                      # per-connection metadata cache
        self.name = name
        self._dyn = cfg

    def _cfg(self):
        base = {"tenant_id": "", "client_id": "", "client_secret": "",
                "workspace": "", "dataset": ""}
        base.update({k: str(v).strip() for k, v in self._dyn.items() if k in base and v})
        return base


def _field(key, label, required=False, secret=False, default="", placeholder=""):
    return {"key": key, "label": label, "required": required, "secret": secret,
            "default": default, "placeholder": placeholder}


# ── Form checks, before any sign-in ─────────────────────────────────────
#
# A sign-in with a value in the wrong field (a token in the hostname, a
# browser address as the HTTP path) used to go to the driver, which retried
# for minutes. A check catches the usual mix-ups at once and says which field
# and where the right value is. Messages never repeat what was typed in a
# field: the "hostname" may well be a secret pasted into the wrong box.

_HOSTNAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DBX_WHERE = "the SQL warehouse's Connection details tab"


def _looks_secret(value):
    v = value.lower()
    return v.startswith(("dapi", "dose", "eyj")) or ("." not in v and len(v) >= 20)


def _check_databricks(cfg):
    """(normalized cfg, {field: problem}). A pasted workspace address becomes
    its hostname, and a full URL whose path is the warehouse's /sql/… path
    becomes that path, so copying from the browser just works."""
    cfg = dict(cfg)
    problems = {}
    host = str(cfg.get("server_hostname") or "").strip()
    path = str(cfg.get("http_path") or "").strip()
    token = str(cfg.get("access_token") or "").strip()

    if "://" in host:
        try:
            host = urlsplit(host).hostname or ""
        except ValueError:
            host = ""
    host = host.split("/")[0].split("?")[0].lower()
    if host.endswith(":443"):
        host = host[:-4]
    path_host = ""
    if "://" in path:
        try:
            parts = urlsplit(path)
            path_host = (parts.hostname or "").lower()
            if parts.path.startswith("/sql/"):
                path = parts.path
                if not host and _HOSTNAME.match(path_host):
                    host = path_host
        except ValueError:
            pass
    cfg.update(server_hostname=host, http_path=path, access_token=token)

    if host and not _HOSTNAME.match(host):
        problems["server_hostname"] = (
            ("This looks like a token or secret, not a host name. " if _looks_secret(host)
             else "This is not a host name. ")
            + f"Copy the Server hostname from {_DBX_WHERE} — it looks like "
              "dbc-1234abcd-5678.cloud.databricks.com."
            + (f" Your workspace looks like {path_host}." if _HOSTNAME.match(path_host) else ""))
    if path and not path.startswith("/sql/"):
        problems["http_path"] = (
            ("That is a browser address, not the warehouse's HTTP path. " if "://" in path else "")
            + f"Copy the HTTP path from {_DBX_WHERE} — it looks like "
              "/sql/1.0/warehouses/1a2b3c4d5e6f7a8b.")
    if token:
        low = token.lower()
        if low.startswith("dose"):
            problems["access_token"] = (
                "This is a service principal's OAuth secret (it starts with “dose”). This "
                "connection signs in with a personal access token, which starts with “dapi” "
                "(Settings → Developer → Access tokens).")
        elif _UUID.match(token):
            problems["access_token"] = (
                "This looks like an application (client) ID, not an access token — use a "
                "personal access token, which starts with “dapi”.")
        elif "://" in token or low.endswith((".databricks.com", ".azuredatabricks.net",
                                             ".gcp.databricks.com")):
            problems["access_token"] = ("This looks like a host or URL, not an access token — "
                                        "it starts with “dapi”.")
    return cfg, problems


def _check_neo4j(cfg):
    """Neo4j is reached over its HTTP API; a bolt:// or neo4j:// URI cannot
    work, so it is refused on its field before any sign-in."""
    from .connectors.graph_conn import http_uri_problem
    cfg = {**cfg, "uri": (cfg.get("uri") or "").strip()}
    problem = http_uri_problem(cfg["uri"])
    return cfg, ({"uri": problem} if problem else {})


def _check_sql(cfg):
    """A usable URL: parseable, a driver installed for it, a network host."""
    cfg = {**cfg, "url": (cfg.get("url") or "").strip(), "schema": (cfg.get("schema") or "").strip()}
    _url, problem = check_sql_url(cfg["url"])
    return cfg, ({"url": problem} if problem else {})


def _resolves(host, wait=5.0):
    """Does `host` resolve? Unknown (a slow resolver) counts as yes, so the
    sign-in itself decides. Resolving a name the admin typed is no more reach
    than the sign-in that follows it."""
    out = {}

    def look():
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            out["ok"] = True
        except OSError:
            out["ok"] = False

    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(wait)
    return out.get("ok", True)


def _preflight(ctype, cfg):
    """(cfg, {field: problem}) for one connection type, before any sign-in:
    the type's form check, then — for a type that names its host field — a
    DNS lookup, so a mistyped host fails in a second, not after retries."""
    t = TYPES[ctype]
    problems = {}
    if t.get("check"):
        cfg, problems = t["check"](cfg)
    host_key = t.get("resolve")
    if not problems and host_key and cfg.get(host_key) and not _resolves(cfg[host_key]):
        problems[host_key] = t["resolve_says"]
    return cfg, problems


def _normalized(ctype, cfg):
    """The config as it will be stored: the type's form check's tidy-up."""
    check = (TYPES.get(ctype) or {}).get("check")
    return check(cfg)[0] if check else cfg


def _refused(problems):
    """A probe/browse result for a form that failed its checks — before any
    sign-in (stage "check")."""
    return {"ok": False, "error": " ".join(problems.values()), "fields": problems,
            "stage": "check"}


def _failed(t, exc, cfg):
    """A probe/browse result for a failed sign-in. A type that can tell which
    field the failure points at (and whether the credential was accepted) marks
    that field, so the form shows it where it was typed."""
    out = {"ok": False, "error": _safe_connection_error(exc, t, cfg)}
    found = t["classify"](exc) if t.get("classify") else None
    if found and found.get("field"):
        out.update(fields={found["field"]: out["error"]}, stage=found["stage"])
    return out


#: Connectable types: how to build one, its dialect, and the form the UI renders.
TYPES = {
    "postgres": {
        "needs_tables": True,
        "label": "PostgreSQL", "dialect": "postgres", "build": _DynPostgres,
        "hint_key": "dsn",
        "ns": {"schema": "schema", "database": ""},
        "fields": [
            _field("dsn", "Connection string (DSN)", required=True, secret=True,
                   placeholder="postgresql://user:password@host:5432/dbname"),
            _field("schema", "Schema", default="public"),
        ],
    },
    "snowflake": {
        "needs_tables": True,
        "label": "Snowflake", "dialect": "snowflake", "build": _DynSnowflake,
        "hint_key": "account",
        "ns": {"schema": "schema", "database": "database"},
        "fields": [
            _field("account", "Account", required=True, placeholder="org-account"),
            _field("user", "User", required=True),
            _field("password", "Password", required=True, secret=True),
            _field("warehouse", "Warehouse"),
            _field("database", "Database", required=True),
            _field("schema", "Schema", default="PUBLIC"),
        ],
    },
    "databricks": {
        "needs_tables": True,
        "label": "Databricks SQL", "dialect": "databricks", "build": _DynDatabricks,
        "check": _check_databricks, "explain": describe_databricks_error,
        "classify": classify_databricks_error,
        "resolve": "server_hostname",
        "resolve_says": ("That Server hostname does not resolve — copy it from "
                         f"{_DBX_WHERE} (it looks like dbc-1234abcd-5678.cloud.databricks.com)."),
        "hint_key": "server_hostname",
        "ns": {"schema": "schema", "database": "catalog"},
        "fields": [
            _field("server_hostname", "Server hostname", required=True,
                   placeholder="dbc-xxxx.cloud.databricks.com"),
            _field("http_path", "HTTP path", required=True,
                   placeholder="/sql/1.0/warehouses/xxxx"),
            _field("access_token", "Access token", required=True, secret=True),
            _field("catalog", "Catalog"),
            _field("schema", "Schema", default="default"),
        ],
    },
    "bigquery": {
        "needs_tables": True,
        "label": "BigQuery", "dialect": "bigquery", "build": _DynBigQuery,
        "hint_key": "project",
        "ns": {"schema": "dataset", "database": "project"},
        "fields": [
            _field("project", "Project", required=True),
            _field("dataset", "Dataset", required=True),
            _field("location", "Location", placeholder="US"),
            _field("credentials_json", "Service-account JSON", secret=True),
        ],
    },
    "neo4j": {
        "label": "Neo4j (Cypher)", "dialect": "cypher", "build": _DynGraph,
        "check": _check_neo4j,
        "hint_key": "uri",
        "fields": [
            _field("uri", "HTTP API URI", required=True,
                   placeholder="https://host:7473 (Neo4j's HTTP API, not bolt://)"),
            _field("user", "User", required=True),
            _field("password", "Password", required=True, secret=True),
            _field("database", "Database", default="neo4j"),
        ],
    },
    # Any other database: one SQLAlchemy URL, any installed driver. Its kind of
    # engine, identifier rules and limit syntax are learned, not listed here.
    "sql": {
        "needs_tables": True,
        "label": "Any SQL database", "dialect": "sql", "build": _DynSQL,
        "check": _check_sql,
        # Blank still pins ONE namespace: the login's default schema, which is
        # where the engine resolves a bare name.
        "blank_schema_is_default": True,
        "hint_key": "url",
        "ns": {"schema": "schema", "database": ""},
        "fields": [
            _field("url", "Connection URL", required=True, secret=True,
                   placeholder="mysql+pymysql://user:password@host:3306/db — any "
                               "dialect+driver installed here"),
            _field("schema", "Schema", placeholder="the login's default schema"),
        ],
    },
    # A service principal (Entra app registration) that the workspace has been
    # shared with. The credential alone signs in and lists every semantic
    # model the app can see; each one picked becomes its own source.
    "powerbi": {
        "label": "Power BI", "dialect": "sqlite", "build": _DynPowerBI,
        "hint_key": "workspace",
        "ns": {"schema": "dataset", "database": "workspace"},
        "fields": [
            _field("tenant_id", "Tenant (directory) ID", required=True,
                   placeholder="GUID or contoso.onmicrosoft.com"),
            _field("client_id", "App (client) ID", required=True),
            _field("client_secret", "Client secret", required=True, secret=True),
            _field("workspace", "Workspace", placeholder="name or ID — or pick below"),
            _field("dataset", "Semantic model", placeholder="name or ID — or pick below"),
        ],
    },
}


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS data_connections (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            ctype TEXT NOT NULL,
            label TEXT,
            config TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at REAL,
            enabled INTEGER NOT NULL DEFAULT 1
        );
        -- What an admin set on the Data connections screen for a BUILT-IN
        -- (env-configured) source: a replacement credential (config, encrypted
        -- exactly like a connection's) and/or a switch that takes it offline.
        -- No row = the deployment's environment decides.
        CREATE TABLE IF NOT EXISTS env_source_settings (
            name TEXT PRIMARY KEY,
            config TEXT,
            disabled INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT NOT NULL,
            updated_at REAL
        );
        """)
        c.commit()


# ── Store ────────────────────────────────────────────────────────────────

def _decrypt(token):
    """The stored config dict, or None when it cannot be recovered (rotated
    secret, missing secret, corrupt row) — callers treat None as unconfigured."""
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, RuntimeError, Exception):
        return None


#: Env-configured connector classes that can seed sources pinned to another
#: schema, and the connection type each one corresponds to.
_ENV_TYPES = ((PostgresConnector, "postgres"), (SnowflakeConnector, "snowflake"),
              (DatabricksConnector, "databricks"), (BigQueryConnector, "bigquery"))


def _env_parent(name):
    """(ctype, live config) for a configured env source whose type has
    namespaces, else None. Read from the connector itself, so it is exactly
    the credential the deployment runs with right now."""
    from . import connectors
    conn = connectors._REGISTRY.get(name)
    if conn is None:
        return None
    # As the source is set NOW: a replacement made on this screen wins, and one
    # switched off here (an _Offline, none of these classes) takes its derived
    # sources offline with it.
    conn = managed_sources([name]).get(name, conn)
    for cls, ctype in _ENV_TYPES:
        if isinstance(conn, cls):
            if not conn.configured():
                return None
            cfg = ({"dsn": conn._dsn(), "schema": conn._schema()} if ctype == "postgres"
                   else conn._cfg())
            return ctype, {k: str(v) for k, v in cfg.items() if v}
    return None


def _config(row):
    """The config a row runs with: its decrypted dict, with a from_env
    reference resolved against the deployment's current credential. None
    when either half is unavailable — callers treat that as unconfigured."""
    cfg = _decrypt(row["config"])
    if cfg is None or "from_env" not in cfg:
        return cfg
    parent = _env_parent(cfg["from_env"])
    if parent is None or parent[0] != row["ctype"]:
        return None
    return {**parent[1], **{k: v for k, v in cfg.items() if k != "from_env"}}


def _rows():
    with db.connect() as c:
        rows = c.execute("SELECT * FROM data_connections WHERE enabled=1 ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def _row_by_name(name):
    with db.connect() as c:
        r = c.execute("SELECT * FROM data_connections WHERE name=? AND enabled=1", (name,)).fetchone()
    return dict(r) if r else None


def _close_connector(conn):
    if conn is not None and callable(getattr(conn, "close", None)):
        try:
            conn.close()
        except Exception:
            pass


def _evict_cached(name):
    """Remove and close a cached connector without holding the registry lock
    while a vendor SDK tears down sockets."""
    with _LOCK:
        hit = _CACHE.pop(name, None)
    if hit:
        _close_connector(hit[1])


def _row_by_id(cid):
    """Any row, enabled or not — for the admin routes that act on one."""
    with db.connect() as c:
        r = c.execute("SELECT * FROM data_connections WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def resolve(name):
    """The live connector for a user-connected source, or None. Cached per row;
    a row that no longer decrypts resolves to None (fail closed)."""
    row = _row_by_name(name)
    if row is None or row["ctype"] not in TYPES:
        _evict_cached(name)
        return None
    # Validate the encrypted row against the CURRENT Studio secret before a
    # cache hit. Runtime secret rotation must fail closed immediately; an
    # already-open vendor session is not authority to keep using credentials
    # that this process can no longer decrypt. PBKDF derivation is cached, so
    # this per-resolve Fernet authentication is cheap. A from_env row resolves
    # against its parent's CURRENT credential, so the key carries the resolved
    # config (hashed): a rotated, replaced or disconnected parent never leaves
    # a derived source on the old connection.
    cfg = _config(row)
    if cfg is None:
        _evict_cached(name)
        return None
    key = (row["id"], row["created_at"],
           hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest())
    with _LOCK:
        hit = _CACHE.get(name)
        if hit and hit[0] == key:
            return hit[1]
    conn = TYPES[row["ctype"]]["build"](row["name"], cfg)
    old = None
    winner = None
    with _LOCK:
        hit = _CACHE.get(name)
        if hit and hit[0] == key:
            # Another request populated this row while we were constructing.
            winner = hit[1]
        else:
            old = hit[1] if hit else None
            _CACHE[name] = (key, conn)
    if winner is not None:
        _close_connector(conn)
        return winner
    _close_connector(old)
    return conn


def source_entries():
    """Rows for connectors.all_sources(): every enabled user connection, marked
    unconfigured when its config no longer decrypts. Never exposes the config."""
    out = []
    for row in _rows():
        t = TYPES.get(row["ctype"])
        if not t:
            continue
        cfg = _config(row)
        dialect = t["dialect"]
        if row["ctype"] == "sql" and cfg:
            try:                     # the engine the URL names; no connection made
                from sqlalchemy.engine import make_url
                dialect = make_url(cfg.get("url", "")).get_backend_name()
            except Exception:
                pass
        out.append({"name": row["name"], "dialect": dialect,
                    "configured": cfg is not None,
                    "type_label": t["label"], "ctype": row["ctype"],
                    "namespace": _namespace(row["ctype"], cfg) if cfg else ""})
    return out


# ── Built-in sources, managed from this screen ───────────────────────────
#
# The env-configured warehouse sources (`databricks`, `postgres`, …) used to be
# changeable only in the deployment's environment. An admin can now replace one
# with a credential entered here, switch it off and on, or hand it back to the
# environment. It keeps its NAME throughout — governance grants, dashboards and
# pipelines written against `databricks` follow it — and connectors.get_connector
# applies the setting ahead of the environment, so every path that reaches a
# source (gateway, RBAC, catalog, agents) sees the same one.

class _Offline(Connector):
    """A built-in source switched off here, or replaced by a credential this
    process can no longer decrypt. Unconfigured on purpose: every read path
    already fails closed on an unconfigured source, and
    sources.connector_or_400 reports `unconfigured_reason`. It never falls back
    to the environment's credential — that would silently re-point a source an
    admin had moved or turned off."""

    def __init__(self, name, dialect, reason):
        self.name = name
        self.dialect = dialect
        self.unconfigured_reason = reason

    def configured(self):
        return False

    def list_tables(self):
        return []

    def get_schema(self, table):
        raise ValueError(self.unconfigured_reason)

    def run_query(self, sql):
        raise RuntimeError(self.unconfigured_reason)


_ENV_CACHE = {}      # name -> ((updated_at, ciphertext), connector)


def _env_type(name):
    """The connection type a built-in source can be managed as, or None. The
    warehouse sources share their type's key — `databricks` is a Databricks SQL
    source — so a form exists for exactly those; demo, the object stores and
    the API connectors stay environment-only."""
    from . import connectors
    return name if name in TYPES and name in connectors._REGISTRY else None


def _env_rows(names):
    names = tuple(n for n in names if _env_type(n))
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    with db.connect() as c:
        rows = c.execute(f"SELECT * FROM env_source_settings WHERE name IN ({marks})",
                         names).fetchall()
    return {r["name"]: dict(r) for r in rows}


def _evict_env(name):
    with _LOCK:
        hit = _ENV_CACHE.pop(name, None)
    if hit:
        _close_connector(hit[1])


def _env_connector(name, row, cfg):
    """The replacement connector, built once per setting version."""
    key = (row["updated_at"], row["config"])
    with _LOCK:
        hit = _ENV_CACHE.get(name)
        if hit and hit[0] == key:
            return hit[1]
    conn = TYPES[name]["build"](name, cfg)
    with _LOCK:
        hit = _ENV_CACHE.get(name)
        if hit and hit[0] == key:
            winner, old = hit[1], None
        else:
            winner, old = None, (hit[1] if hit else None)
            _ENV_CACHE[name] = (key, conn)
    if winner is not None:
        _close_connector(conn)
        return winner
    _close_connector(old)
    return conn


def managed_sources(names):
    """{name: connector} for the built-in sources among `names` that an admin
    replaced or switched off here — what the registry uses INSTEAD of the
    environment. A source with no setting is absent: the environment applies.
    Read on every call, like resolve(): a change made on another replica, or a
    rotated STUDIO_SECRET, takes effect at once; only the built connector is
    cached, per setting version."""
    out = {}
    for name, row in _env_rows(names).items():
        dialect = TYPES[name]["dialect"]
        if row["disabled"]:
            out[name] = _Offline(name, dialect, (
                f"Source '{name}' was disconnected on the Data connections screen — "
                "reconnect it there."))
            continue
        if not row["config"]:
            continue
        cfg = _decrypt(row["config"])
        if cfg is None:
            _evict_env(name)
            out[name] = _Offline(name, dialect, (
                f"Source '{name}' was set up on the Data connections screen, but its "
                "stored credential can no longer be read (STUDIO_SECRET changed) — set it "
                "up again there, or switch it back to the environment."))
            continue
        out[name] = _env_connector(name, row, cfg)
    return out


def _namespace(ctype, cfg):
    """The one namespace this connection is pinned to, as the engine spells it
    — "acme_db.public", or just "public" when the database is unknown. Display
    only: qualifiers() remains the thing that enforces it."""
    t = TYPES.get(ctype) or {}
    cfg = cfg or {}
    schema = (cfg.get("schema") or cfg.get("dataset") or "").strip()
    database = (cfg.get("database") or cfg.get("catalog") or cfg.get("project")
                or cfg.get("workspace") or "").strip()
    if not schema and not database:
        # Postgres carries its database inside the DSN and defaults the schema.
        if ctype == "postgres":
            schema = "public"
        else:
            return ""
    for f in t.get("fields", []):
        if f["key"] in ("schema", "dataset") and not schema:
            schema = (f.get("default") or "").strip()
    if schema == "*":                       # a Databricks source spanning schemas
        return f"{database} · all schemas" if database else "all catalogs"
    return f"{database}.{schema}" if database and schema else (schema or database)


def _hint(ctype, cfg):
    """A non-secret identifier for listings — host/account/project, never
    credentials. Postgres DSNs embed a password, so only the hostname survives."""
    raw = (cfg or {}).get(TYPES[ctype]["hint_key"], "") or ""
    if ctype in ("postgres", "neo4j"):
        try:
            return urlsplit(raw).hostname or ""
        except ValueError:
            return ""
    if ctype == "sql":
        # The engine and host only: the URL carries the password.
        try:
            from sqlalchemy.engine import make_url
            url = make_url(raw)
            return f"{url.get_backend_name()} · {url.host}" if url.host else url.get_backend_name()
        except Exception:
            return ""
    return raw


def _public(row):
    cfg = _config(row)
    t = TYPES.get(row["ctype"], {})
    return {"id": row["id"], "name": row["name"], "ctype": row["ctype"],
            "type_label": t.get("label", row["ctype"]), "label": row.get("label") or "",
            "hint": _hint(row["ctype"], cfg) if cfg else "",
            "namespace": _namespace(row["ctype"], cfg) if cfg else "",
            "configured": cfg is not None, "enabled": bool(row.get("enabled", 1)),
            "created_at": row.get("created_at")}


# ── Probe ────────────────────────────────────────────────────────────────

def _probe(ctype, cfg):
    """Build a throwaway connector and prove it can list tables. Errors come
    back as data (capped), never a 500 — a wrong password is a normal outcome."""
    t = TYPES.get(ctype)
    if t is None:
        return {"ok": False, "error": f"unknown connection type '{ctype}'"}
    missing = [f["key"] for f in t["fields"] if f["required"] and not (cfg.get(f["key"]) or "").strip()]
    if missing:
        return {"ok": False, "error": "missing required fields: " + ", ".join(missing)}
    cfg, problems = _preflight(ctype, cfg)
    if problems:
        return _refused(problems)
    conn = None
    try:
        conn = t["build"]("__probe__", cfg)
        if not conn.configured():
            return {"ok": False, "error": "the connector reports itself unconfigured — check the fields"}
        tables = conn.list_tables()
    except Exception as e:
        return _failed(t, e, cfg)
    finally:
        _close_connector(conn)
    if not tables and t.get("needs_tables"):
        # Signed in, but nothing to read: saved, every question would fail.
        said = _empty_scope(ctype, cfg)
        field = (t.get("ns") or {}).get("schema")
        return {"ok": False, "error": said, "stage": "signed_in",
                **({"fields": {field: said}} if field else {})}
    return {"ok": True, "tables": len(tables), "sample": list(tables)[:8]}


def _empty_scope(ctype, cfg):
    """What to say when a warehouse signs in but its schema shows no tables."""
    t = TYPES[ctype]
    ns = t.get("ns") or {}
    label = next((f["label"].lower() for f in t["fields"] if f["key"] == ns.get("schema")),
                 "schema")
    said = f"Signed in, but this {label} has no tables the credential can see"
    database = ns.get("database")
    if database and not str(cfg.get(database) or "").strip():
        db_label = next(f["label"].lower() for f in t["fields"] if f["key"] == database)
        said += f" (no {db_label} is set, so the default one was used)"
    return (said + ". Use “Sign in and list schemas” to pick the one that holds your data, "
            "or grant the credential read access to its tables.")


def _safe_connection_error(exc, connection_type, cfg):
    """Return a useful connector error without reflecting submitted secrets.

    Warehouse SDKs routinely include transformed connection kwargs in
    authentication and transport errors.  Whenever a secret field was posted,
    return a generic detail rather than trying to recognize every decoded form.
    Credential-less errors retain their useful text, with URL user-info still
    removed defensively.
    """
    # Do not attempt substring redaction when credentials were submitted. SDKs
    # may decode URI escapes, base64 service-account JSON, PEM newlines, or
    # nested JSON before interpolating a value into an exception, so there is
    # no finite set of byte spellings we can safely scrub.  Preserve useful
    # vendor details only for credential-less flows such as BigQuery ADC.
    #
    # The one exception is Power BI's own error type. Its messages are built in
    # powerbi_conn.py from HTTP status codes, Microsoft's error text and the
    # workspace/model names the admin typed; the secret is only ever sent to
    # Entra's token endpoint, whose errors do not echo it. Hiding them would
    # hide the setup fixes (an AADSTS code, a tenant setting) that service-
    # principal sign-in nearly always needs on the first try.
    if isinstance(exc, PowerBIError):
        return str(exc)[:300]
    # A type may name the failure from its status code and exception types
    # alone (Databricks: a rejected token, a wrong HTTP path, a timeout) —
    # fixed sentences that cannot carry a submitted secret.
    explain = connection_type.get("explain")
    said = explain(exc) if explain else None
    if said:
        return said
    if any(field.get("secret") and str((cfg or {}).get(field["key"], "") or "")
           for field in connection_type.get("fields", [])):
        return "connection failed (credential details redacted)"

    message = str(exc)
    # Defensive catch for a driver-rendered DSN that is not byte-for-byte the
    # submitted value (for example after percent decoding).
    message = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@",
        r"\1[redacted]@",
        message,
    )
    return message[:300]


def _missing_fields(connection_type, cfg, *, for_browse=False):
    """Required form fields absent from this operation.

    Browsing authenticates first and discovers the namespace second, so the
    schema/database fields populated by a picker are intentionally not browse
    prerequisites.  The normal probe still requires every field; create calls
    that probe and therefore retains the strict saved-source invariant.
    """
    namespace_fields = set()
    if for_browse:
        ns = connection_type.get("ns") or {}
        namespace_fields = {key for key in (ns.get("schema"), ns.get("database")) if key}
    return [field["key"] for field in connection_type["fields"]
            if field["required"] and field["key"] not in namespace_fields
            and not str((cfg or {}).get(field["key"], "") or "").strip()]


def _browse(ctype, cfg):
    """Namespaces the given credential can see, for the connect screen's
    picker. Shaped like _probe: errors come back as data, never a 500, because
    "this role cannot list schemas" is a normal answer and the admin can still
    type the name. Never saved and never cached — the throwaway connector is
    built from the posted config and dropped."""
    t = TYPES.get(ctype)
    if t is None:
        return {"ok": False, "error": f"unknown connection type '{ctype}'", "namespaces": []}
    missing = _missing_fields(t, cfg, for_browse=True)
    if missing:
        return {"ok": False, "error": "missing required fields: " + ", ".join(missing),
                "namespaces": []}
    cfg, problems = _preflight(ctype, cfg)
    if problems:
        return {**_refused(problems), "namespaces": []}
    conn = None
    try:
        # The leaf namespace is deliberately cleared even when its input has a
        # UI default (PUBLIC/default).  Selecting it as the session schema can
        # fail before SHOW/LIST has a chance to discover the schemas the role
        # actually sees.  A database/catalog/project, when supplied, remains a
        # useful discovery scope.
        discovery_cfg = dict(cfg)
        schema_field = (t.get("ns") or {}).get("schema")
        if schema_field:
            discovery_cfg[schema_field] = ""
        conn = t["build"]("__browse__", discovery_cfg)
        # Do not call configured(): for several connectors it deliberately
        # means "credentials AND a table namespace are ready".  list_namespaces
        # is the credential probe that discovers that namespace in the first
        # place, and imports/connect failures are safely returned below.
        found = conn.list_namespaces()
        connector_truncated = bool(getattr(conn, "namespaces_truncated", False))
    except Exception as e:
        return {**_failed(t, e, cfg), "namespaces": []}
    finally:
        _close_connector(conn)
    # Cap the list: a metastore can hold thousands, and the picker is a picker,
    # not a catalog browser. Deduplicated because SHOW output can repeat a pair.
    seen, out = set(), []
    truncated = connector_truncated
    for n in found:
        pair = ((n.get("database") or "").strip(), (n.get("schema") or "").strip())
        if not pair[1] or pair in seen:
            continue
        seen.add(pair)
        if len(out) >= 500:
            truncated = True
            continue
        item = {"database": pair[0], "schema": pair[1]}
        if isinstance(n.get("tables"), int):
            item["tables"] = n["tables"]          # how many tables it holds, when known
        out.append(item)
    return {"ok": True, "namespaces": out, "truncated": truncated}


# ── Routes (admin-only) ──────────────────────────────────────────────────

def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Data connections are admin-only")


class ConnIn(BaseModel):
    name: str
    ctype: str
    label: str | None = None
    config: dict


class TestIn(BaseModel):
    ctype: str
    config: dict


@router.get("/types")
def types(user=Depends(current_user)):
    _admin(user)
    # `ns` tells the connect screen which fields a picked namespace writes
    # into — the schema field is "dataset" on BigQuery, the database field is
    # "catalog" on Databricks and lives inside the DSN on Postgres. Declaring
    # it here keeps that mapping in one place instead of in the frontend.
    return [{"ctype": k, "label": t["label"], "dialect": t["dialect"],
             "fields": t["fields"], "ns": t.get("ns") or {}}
            for k, t in TYPES.items()]


@router.get("")
def list_connections(user=Depends(current_user)):
    """Every connection, disconnected ones included (enabled=False) — this is
    the only place a disconnected row is visible, so the admin can reconnect or
    remove it."""
    _admin(user)
    with db.connect() as c:
        rows = c.execute("SELECT * FROM data_connections ORDER BY created_at").fetchall()
    return [_public(dict(r)) for r in rows]


@router.post("/test")
def test_connection(body: TestIn, user=Depends(current_user)):
    _admin(user)
    return _probe(body.ctype, {k: str(v) for k, v in (body.config or {}).items()})


@router.post("/browse")
def browse_connection(body: TestIn, user=Depends(current_user)):
    """Which namespaces could this credential bind a source to? Admin-only for
    the same reason /test is: it proves what an arbitrary DSN can reach."""
    _admin(user)
    return _browse(body.ctype, {k: str(v) for k, v in (body.config or {}).items()})


def _require_pin(ctype, cfg, why):
    """A source is pinned to ONE namespace. Discovery signs in with it blank
    (Power BI: tenant + app, no semantic model yet); saved that way it would be
    a source with nothing in it, so say which field is missing instead of
    letting the probe report a generic "unconfigured"."""
    pin = (TYPES[ctype].get("ns") or {}).get("schema")
    if pin and not TYPES[ctype].get("blank_schema_is_default"):
        field = next((f for f in TYPES[ctype]["fields"] if f["key"] == pin), {})
        if not str(cfg.get(pin) or field.get("default") or "").strip():
            raise HTTPException(400, f"pick a {field.get('label', pin).lower()} to connect {why}")


def _refuse_save(probe):
    """A failed probe blocks a save. A form that failed its checks says which
    fields, so the screen can mark them; a failed sign-in stays one sentence."""
    said = f"connection test failed: {probe['error']}"
    if probe.get("fields"):
        raise HTTPException(400, {"error": said, "fields": probe["fields"],
                                  "stage": probe.get("stage", "check")})
    raise HTTPException(400, said)


# Listeners told about a source's lifecycle — metagraph.py queues its
# discovery here. Each is called as fn(name, trigger, conn=...) with trigger
# "connection_saved", "reconnected", "disconnected" or "removed". On a save,
# `conn` is the transaction holding the new row, so whatever a listener writes
# lands (or rolls back) with it; the others are announced after their commit.
_SOURCE_LISTENERS = []


def on_source_saved(fn):
    _SOURCE_LISTENERS.append(fn)
    return fn


def _announce(name, trigger, *, conn=None):
    for fn in list(_SOURCE_LISTENERS):
        fn(name, trigger, conn=conn)


def _create(user, name, ctype, stored, effective, label=""):
    """Validate, probe with the `effective` config, then store `stored` —
    the same dict for a typed credential, a from_env reference for a source
    derived from the deployment's own."""
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(400, "name must be 2-31 chars: lowercase letters, digits, - or _")
    from . import connectors
    if name in connectors._REGISTRY or _row_by_name(name):
        raise HTTPException(400, f"a source named '{name}' already exists")
    with db.connect() as c:
        parked = c.execute("SELECT 1 FROM data_connections WHERE name=?", (name,)).fetchone()
    if parked:
        # A disconnected row still owns the name (and any grants written
        # against it) — the UNIQUE constraint would say so as a 500.
        raise HTTPException(400, f"a disconnected source named '{name}' exists — "
                                 "reconnect it or remove it first")
    if ctype not in TYPES:
        raise HTTPException(400, f"unknown connection type '{ctype}'")
    effective = _normalized(ctype, effective)
    if "from_env" not in stored:
        stored = _normalized(ctype, stored)
    _require_pin(ctype, effective, "— each one becomes its own source")
    probe = _probe(ctype, effective)
    if not probe["ok"]:
        _refuse_save(probe)
    row = {"id": str(uuid.uuid4()), "name": name, "ctype": ctype,
           "label": (label or "").strip()[:80],
           "config": _fernet().encrypt(json.dumps(stored).encode()).decode(),
           "created_by": user["id"], "created_at": time.time(), "enabled": 1}
    with db.connect() as c:
        c.execute("INSERT INTO data_connections (id, name, ctype, label, config, "
                  "created_by, created_at, enabled) VALUES (?,?,?,?,?,?,?,1)",
                  (row["id"], row["name"], row["ctype"], row["label"], row["config"],
                   row["created_by"], row["created_at"]))
        # Listeners (metagraph.py's discovery) write in this transaction, so
        # the connection and its discovery job land together.
        _announce(name, "connection_saved", conn=c)
        c.commit()
    _evict_cached(name)
    db.log_activity(user, "connection_create", prompt=name, source=ctype)
    return _public(row)


@router.post("", status_code=201)
def create_connection(body: ConnIn, user=Depends(current_user)):
    _admin(user)
    # from_env is how a derived source points at the deployment's credential;
    # it is minted only by add_schema below, never accepted from a client.
    cfg = {k: str(v) for k, v in (body.config or {}).items() if k != "from_env"}
    return _create(user, body.name, body.ctype, cfg, cfg, body.label)


# ── Add schemas to a live source ─────────────────────────────────────────
#
# The connect form lists schemas only while the admin holds the credential
# they just typed. A live source — above all an env-configured one, whose
# secret the admin never sees — gets the same step without re-entering it.

def _parent(name):
    """(ctype, effective config, template, ns) for a live source that can have
    schemas added. `template` is what a derived row stores before its schema
    is set: a from_env reference when the credential is the deployment's, or
    a copy of a typed credential (independent of its parent afterwards, like
    picking several schemas at connect time)."""
    env = _env_parent(name)
    if env is not None:
        ctype, cfg = env
        template = {"from_env": name}
    else:
        row = _row_by_name(name)
        cfg = _config(row) if row else None
        if cfg is None:
            raise HTTPException(404, f"no live source named '{name}' can list schemas")
        ctype = row["ctype"]
        raw = _decrypt(row["config"]) or {}
        template = {"from_env": raw["from_env"]} if "from_env" in raw else dict(raw)
    ns = (TYPES.get(ctype) or {}).get("ns") or {}
    if not ns.get("schema"):
        raise HTTPException(400, f"{TYPES[ctype]['label']} has no schemas to add")
    return ctype, cfg, template, ns


def _pinned(ns, schema, database=""):
    return {ns["schema"]: schema,
            **({ns["database"]: database} if ns.get("database") and database else {})}


def _current(ctype, cfg, ns):
    """The namespace a source is pinned to, with the type's default schema
    filled in — a Postgres row saved without one runs on `public`."""
    field = next((f for f in TYPES[ctype]["fields"] if f["key"] == ns["schema"]), {})
    return {"schema": cfg.get(ns["schema"]) or field.get("default", ""),
            "database": cfg.get(ns["database"], "") if ns.get("database") else ""}


@router.get("/sources/{name}/namespaces")
def browse_source(name: str, user=Depends(current_user)):
    """Schemas a live source's own credential can see, plus the one it is
    already pinned to (the picker shows that one as taken)."""
    _admin(user)
    ctype, cfg, _template, ns = _parent(name)
    return {**_browse(ctype, cfg), "current": _current(ctype, cfg, ns)}


class SchemaIn(BaseModel):
    name: str
    schema_: str = Field(alias="schema")
    database: str | None = None


@router.post("/sources/{name}/schemas", status_code=201)
def add_schema(name: str, body: SchemaIn, user=Depends(current_user)):
    """Bind ONE more schema of a live source as a new source. One per call so
    the screen can report exactly which of several picks failed."""
    _admin(user)
    ctype, cfg, template, ns = _parent(name)
    schema = body.schema_.strip()
    database = (body.database or "").strip()
    if not schema:
        raise HTTPException(400, "pick a schema")
    pin = _pinned(ns, schema, database)
    current = _current(ctype, cfg, ns)
    if schema == current["schema"] and (not ns.get("database") or database == current["database"]):
        raise HTTPException(400, f"'{name}' is already pinned to {schema}")
    return _create(user, body.name, ctype, {**template, **pin}, {**cfg, **pin},
                   label=f"{name} · {schema}")


def _set_enabled(cid, enabled):
    with db.connect() as c:
        c.execute("UPDATE data_connections SET enabled=? WHERE id=?", (1 if enabled else 0, cid))
        c.commit()


@router.post("/{cid}/disconnect")
def disconnect_connection(cid: str, user=Depends(current_user)):
    """Take the source offline without forgetting it: it leaves the picker and
    every query path, but the encrypted credential and the name stay, so a
    reconnect is one click and governance grants still mean the same source."""
    _admin(user)
    row = _row_by_id(cid)
    if row is None:
        raise HTTPException(404, "Not found")
    if row["enabled"]:
        _set_enabled(cid, False)
        _evict_cached(row["name"])
        _announce(row["name"], "disconnected")
        db.log_activity(user, "connection_disconnect", prompt=row["name"], source=row["ctype"])
    row["enabled"] = 0
    return _public(row)


@router.post("/{cid}/reconnect")
def reconnect_connection(cid: str, user=Depends(current_user)):
    """Bring a disconnected source back on its stored credential. Re-probed
    first, like create: the password may have rotated while it was parked, and
    a source that comes back dead is worse than one that says why it can't."""
    _admin(user)
    row = _row_by_id(cid)
    if row is None:
        raise HTTPException(404, "Not found")
    if not row["enabled"]:
        cfg = _config(row)
        if cfg is None:
            raise HTTPException(400, "the stored credential can no longer be read "
                                     "(STUDIO_SECRET changed) — remove it and connect again")
        probe = _probe(row["ctype"], cfg)
        if not probe["ok"]:
            raise HTTPException(400, f"connection test failed: {probe['error']}")
        _set_enabled(cid, True)
        _evict_cached(row["name"])
        _announce(row["name"], "reconnected")
        db.log_activity(user, "connection_reconnect", prompt=row["name"], source=row["ctype"])
    row["enabled"] = 1
    return _public(row)


# ── Routes: built-in sources ─────────────────────────────────────────────

class EnvIn(BaseModel):
    config: dict


def _env_or_404(name):
    ctype = _env_type(name)
    if ctype is None:
        raise HTTPException(404, f"'{name}' is not a built-in source that can be managed here")
    return ctype


def _env_write(name, user, *, config, disabled):
    with db.connect() as c:
        c.execute("INSERT INTO env_source_settings (name, config, disabled, updated_by, "
                  "updated_at) VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                  "config=excluded.config, disabled=excluded.disabled, "
                  "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                  (name, config, 1 if disabled else 0, user["id"], time.time()))
        c.commit()
    _evict_env(name)


def _env_delete(name):
    with db.connect() as c:
        c.execute("DELETE FROM env_source_settings WHERE name=?", (name,))
        c.commit()
    _evict_env(name)


def _env_public(name):
    """How a built-in source is set now — never its credential."""
    from . import connectors
    row = _env_rows([name]).get(name)
    cfg = _decrypt(row["config"]) if row and row["config"] else None
    replaced = bool(row and row["config"])
    mode = ("disconnected" if row and row["disabled"]
            else "replaced" if replaced else "environment")
    return {"name": name, "ctype": name, "type_label": TYPES[name]["label"], "mode": mode,
            "replaced": replaced, "readable": (cfg is not None) if replaced else None,
            "env_configured": bool(connectors._REGISTRY[name].configured()),
            "hint": _hint(name, cfg) if cfg else "",
            "namespace": _namespace(name, cfg) if cfg else "",
            "updated_at": row["updated_at"] if row else None}


@router.get("/env")
def env_sources(user=Depends(current_user)):
    """The built-in sources this screen can manage and how each is set:
    by the environment, replaced here, or disconnected here."""
    _admin(user)
    from . import connectors
    return [_env_public(n) for n in TYPES if n in connectors._REGISTRY]


@router.put("/env/{name}")
def replace_env_source(name: str, body: EnvIn, user=Depends(current_user)):
    """Point a built-in source at a credential entered here. It keeps its name
    — that is the point: grants, dashboards and pipelines written against it
    follow it to the new connection — so this is the same operator power as
    editing the deployment's environment. Probed before it is saved."""
    _admin(user)
    ctype = _env_or_404(name)
    cfg = _normalized(ctype, {k: str(v) for k, v in (body.config or {}).items()})
    _require_pin(ctype, cfg, f"— '{name}' reads exactly one")
    probe = _probe(ctype, cfg)
    if not probe["ok"]:
        _refuse_save(probe)
    _env_write(name, user, config=_fernet().encrypt(json.dumps(cfg).encode()).decode(),
               disabled=False)
    db.log_activity(user, "connection_env_replace", prompt=name, source=ctype)
    return _env_public(name)


@router.post("/env/{name}/disconnect")
def disconnect_env_source(name: str, user=Depends(current_user)):
    """Take a built-in source offline. A replacement credential is kept, so
    reconnecting needs no re-typed secret."""
    _admin(user)
    ctype = _env_or_404(name)
    row = _env_rows([name]).get(name)
    _env_write(name, user, config=(row or {}).get("config"), disabled=True)
    db.log_activity(user, "connection_env_disconnect", prompt=name, source=ctype)
    return _env_public(name)


@router.post("/env/{name}/reconnect")
def reconnect_env_source(name: str, user=Depends(current_user)):
    """Back online: on the replacement if there is one — re-probed first, since
    its password may have rotated while it was off — else on the environment."""
    _admin(user)
    ctype = _env_or_404(name)
    row = _env_rows([name]).get(name)
    if row and row["config"]:
        cfg = _decrypt(row["config"])
        if cfg is None:
            raise HTTPException(400, "the stored credential can no longer be read "
                                     "(STUDIO_SECRET changed) — set the source up again, "
                                     "or switch it back to the environment")
        probe = _probe(ctype, cfg)
        if not probe["ok"]:
            raise HTTPException(400, f"connection test failed: {probe['error']}")
        _env_write(name, user, config=row["config"], disabled=False)
    else:
        _env_delete(name)            # only the switch was set here: the environment applies
    db.log_activity(user, "connection_env_reconnect", prompt=name, source=ctype)
    return _env_public(name)


@router.delete("/env/{name}")
def revert_env_source(name: str, user=Depends(current_user)):
    """Forget everything this screen set for a built-in source — replacement
    and switch alike — so the deployment's environment decides again."""
    _admin(user)
    ctype = _env_or_404(name)
    _env_delete(name)
    db.log_activity(user, "connection_env_revert", prompt=name, source=ctype)
    return _env_public(name)


@router.delete("/{cid}")
def delete_connection(cid: str, user=Depends(current_user)):
    _admin(user)
    with db.connect() as c:
        r = c.execute("SELECT * FROM data_connections WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Not found")
        c.execute("DELETE FROM data_connections WHERE id=?", (cid,))
        c.commit()
    _evict_cached(r["name"])
    _announce(r["name"], "removed")
    db.log_activity(user, "connection_delete", prompt=r["name"], source=r["ctype"])
    return {"deleted": r["name"]}
