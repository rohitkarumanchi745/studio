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
"""
import base64
import json
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user
from .connectors.bigquery_conn import BigQueryConnector
from .connectors.databricks_conn import DatabricksConnector
from .connectors.graph_conn import GraphConnector
from .connectors.postgres_conn import PostgresConnector
from .connectors.snowflake_conn import SnowflakeConnector

router = APIRouter(prefix="/connections", tags=["connections"])

_SALT = b"studio-data-connections-v1"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")
_LOCK = threading.Lock()
_CACHE = {}          # name -> ((row id, created_at), connector)


def _fernet():
    from . import bootstrap
    secret = (bootstrap.jwt_secret() or "").encode()
    if not secret:
        raise RuntimeError("STUDIO_SECRET is not set; cannot derive the encryption key")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret)))


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
        self.name = name
        self._dyn = cfg

    def _cfg(self):
        base = {"account": "", "user": "", "password": "", "warehouse": "",
                "database": "", "schema": "PUBLIC"}
        base.update({k: v for k, v in self._dyn.items() if v})
        return base


class _DynDatabricks(DatabricksConnector):
    def __init__(self, name, cfg):
        self.name = name
        self._dyn = cfg

    def _cfg(self):
        base = {"server_hostname": "", "http_path": "", "access_token": "",
                "catalog": "", "schema": "default", "warehouse_id": ""}
        base.update({k: v for k, v in self._dyn.items() if v})
        return base


class _DynBigQuery(BigQueryConnector):
    def __init__(self, name, cfg):
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


def _field(key, label, required=False, secret=False, default="", placeholder=""):
    return {"key": key, "label": label, "required": required, "secret": secret,
            "default": default, "placeholder": placeholder}


#: Connectable types: how to build one, its dialect, and the form the UI renders.
TYPES = {
    "postgres": {
        "label": "PostgreSQL", "dialect": "postgres", "build": _DynPostgres,
        "hint_key": "dsn",
        "fields": [
            _field("dsn", "Connection string (DSN)", required=True, secret=True,
                   placeholder="postgresql://user:password@host:5432/dbname"),
            _field("schema", "Schema", default="public"),
        ],
    },
    "snowflake": {
        "label": "Snowflake", "dialect": "snowflake", "build": _DynSnowflake,
        "hint_key": "account",
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
        "label": "Databricks SQL", "dialect": "databricks", "build": _DynDatabricks,
        "hint_key": "server_hostname",
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
        "label": "BigQuery", "dialect": "bigquery", "build": _DynBigQuery,
        "hint_key": "project",
        "fields": [
            _field("project", "Project", required=True),
            _field("dataset", "Dataset", required=True),
            _field("location", "Location", placeholder="US"),
            _field("credentials_json", "Service-account JSON", secret=True),
        ],
    },
    "neo4j": {
        "label": "Neo4j (Cypher)", "dialect": "cypher", "build": _DynGraph,
        "hint_key": "uri",
        "fields": [
            _field("uri", "URI", required=True, placeholder="bolt://host:7687"),
            _field("user", "User", required=True),
            _field("password", "Password", required=True, secret=True),
            _field("database", "Database", default="neo4j"),
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


def _rows():
    with db.connect() as c:
        rows = c.execute("SELECT * FROM data_connections WHERE enabled=1 ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def _row_by_name(name):
    with db.connect() as c:
        r = c.execute("SELECT * FROM data_connections WHERE name=? AND enabled=1", (name,)).fetchone()
    return dict(r) if r else None


def resolve(name):
    """The live connector for a user-connected source, or None. Cached per row;
    a row that no longer decrypts resolves to None (fail closed)."""
    row = _row_by_name(name)
    if row is None or row["ctype"] not in TYPES:
        return None
    key = (row["id"], row["created_at"])
    with _LOCK:
        hit = _CACHE.get(name)
        if hit and hit[0] == key:
            return hit[1]
    cfg = _decrypt(row["config"])
    if cfg is None:
        return None
    conn = TYPES[row["ctype"]]["build"](row["name"], cfg)
    with _LOCK:
        _CACHE[name] = (key, conn)
    return conn


def source_entries():
    """Rows for connectors.all_sources(): every enabled user connection, marked
    unconfigured when its config no longer decrypts. Never exposes the config."""
    out = []
    for row in _rows():
        t = TYPES.get(row["ctype"])
        if not t:
            continue
        out.append({"name": row["name"], "dialect": t["dialect"],
                    "configured": _decrypt(row["config"]) is not None})
    return out


def _hint(ctype, cfg):
    """A non-secret identifier for listings — host/account/project, never
    credentials. Postgres DSNs embed a password, so only the hostname survives."""
    raw = (cfg or {}).get(TYPES[ctype]["hint_key"], "") or ""
    if ctype in ("postgres", "neo4j"):
        try:
            return urlsplit(raw).hostname or ""
        except ValueError:
            return ""
    return raw


def _public(row):
    cfg = _decrypt(row["config"])
    t = TYPES.get(row["ctype"], {})
    return {"id": row["id"], "name": row["name"], "ctype": row["ctype"],
            "type_label": t.get("label", row["ctype"]), "label": row.get("label") or "",
            "hint": _hint(row["ctype"], cfg) if cfg else "",
            "configured": cfg is not None, "created_at": row.get("created_at")}


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
    try:
        conn = t["build"]("__probe__", cfg)
        if not conn.configured():
            return {"ok": False, "error": "the connector reports itself unconfigured — check the fields"}
        tables = conn.list_tables()
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "tables": len(tables), "sample": list(tables)[:8]}


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
    return [{"ctype": k, "label": t["label"], "dialect": t["dialect"], "fields": t["fields"]}
            for k, t in TYPES.items()]


@router.get("")
def list_connections(user=Depends(current_user)):
    _admin(user)
    return [_public(r) for r in _rows()]


@router.post("/test")
def test_connection(body: TestIn, user=Depends(current_user)):
    _admin(user)
    return _probe(body.ctype, {k: str(v) for k, v in (body.config or {}).items()})


@router.post("", status_code=201)
def create_connection(body: ConnIn, user=Depends(current_user)):
    _admin(user)
    name = (body.name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(400, "name must be 2-31 chars: lowercase letters, digits, - or _")
    from . import connectors
    if name in connectors._REGISTRY or _row_by_name(name):
        raise HTTPException(400, f"a source named '{name}' already exists")
    if body.ctype not in TYPES:
        raise HTTPException(400, f"unknown connection type '{body.ctype}'")
    cfg = {k: str(v) for k, v in (body.config or {}).items()}
    probe = _probe(body.ctype, cfg)
    if not probe["ok"]:
        raise HTTPException(400, f"connection test failed: {probe['error']}")
    row = {"id": str(uuid.uuid4()), "name": name, "ctype": body.ctype,
           "label": (body.label or "").strip()[:80],
           "config": _fernet().encrypt(json.dumps(cfg).encode()).decode(),
           "created_by": user["id"], "created_at": time.time(), "enabled": 1}
    with db.connect() as c:
        c.execute("INSERT INTO data_connections (id, name, ctype, label, config, "
                  "created_by, created_at, enabled) VALUES (?,?,?,?,?,?,?,1)",
                  (row["id"], row["name"], row["ctype"], row["label"], row["config"],
                   row["created_by"], row["created_at"]))
        c.commit()
    with _LOCK:
        _CACHE.pop(name, None)
    db.log_activity(user, "connection_create", prompt=name, source=body.ctype)
    return _public(row)


@router.delete("/{cid}")
def delete_connection(cid: str, user=Depends(current_user)):
    _admin(user)
    with db.connect() as c:
        r = c.execute("SELECT * FROM data_connections WHERE id=?", (cid,)).fetchone()
        if r is None:
            raise HTTPException(404, "Not found")
        c.execute("DELETE FROM data_connections WHERE id=?", (cid,))
        c.commit()
    with _LOCK:
        _CACHE.pop(r["name"], None)
    db.log_activity(user, "connection_delete", prompt=r["name"], source=r["ctype"])
    return {"deleted": r["name"]}
