"""S3 / Azure Blob / GCS as Studio sources, backed by DuckDB.

Object storage has no query engine and no notion of a table, so there is
nothing for an agent to point SQL at until someone says what a table *is*. An
admin therefore registers datasets — (source, name, uri, format) — and each one
becomes a named DuckDB view over those objects. Agents then write ordinary SQL
against the view NAMES, which is the whole point: names flow through the
existing guardrails unchanged (rbac.py allowlists them, queryguard.py rejects
everything else), and a raw object URI in agent SQL simply fails validation.
The view DDL is built here from the vetted registry, never from model output.

Datasets come from the admin table plus a STUDIO_OBJECT_DATASETS env bootstrap,
so a deploy can ship with datasets already present; the DB wins a name
collision. Credentials never enter the registry — they stay in env (see
.env.example), so a registry row leaks nothing but a bucket path.

Requires `pip install duckdb` (commented in requirements.txt). Without it, or
without credentials, or with no datasets registered, the source reports
not-configured and is left out of the catalog exactly like Snowflake is.
"""
import json
import os
import re
import threading
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..auth import current_user
from .base import Connector, jsonify_rows

router = APIRouter(prefix="/settings/objectstore", tags=["objectstore"])

# Each source: the URI schemes it accepts and the DuckDB extensions its
# filesystem needs. parquet/csv/json readers are built into the Python wheel;
# only the filesystems come from extensions.
_SOURCES = {
    "s3": {"schemes": ("s3://",), "extensions": ("httpfs",)},
    "azure_blob": {"schemes": ("az://", "abfss://"), "extensions": ("azure",)},
    "gcs": {"schemes": ("gs://",), "extensions": ("httpfs",)},
}

# Dataset name becomes a view name and an RBAC/queryguard table name, so it has
# to be a bare SQL identifier — nothing that needs quoting or could carry SQL.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# format -> reader. Deliberately no delta/iceberg: those need extensions we
# refuse to autoload (see _harden), and their scanners take their own URIs.
_FORMATS = {"parquet": "read_parquet", "csv": "read_csv_auto", "json": "read_json_auto"}


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS objectstore_datasets (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            name TEXT NOT NULL,
            uri TEXT NOT NULL,
            format TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_objectstore_dataset
            ON objectstore_datasets(source, name);
        """
    )
    c.commit()
    c.close()


# ── Dataset registry (env bootstrap + DB) ───────────────────────────────

def _esc(v):
    """Escape a SQL string literal. Secret values and reader paths cannot be
    bound as prepared-statement parameters, so they must be interpolated."""
    return str(v).replace("'", "''")


def _valid(source, name, uri, fmt):
    """A dataset is only usable if its name is an identifier, its format has a
    reader, and its URI belongs to this source and carries no query string —
    DuckDB's legacy `s3://…?s3_access_key_id=…` form lets a URI smuggle its own
    credentials and endpoint. (SET s3_url_compatibility_mode closes that hole
    too, but it also disables globbing on S3 URLs, which every dataset uses.)"""
    if source not in _SOURCES or fmt not in _FORMATS:
        return False
    if not _NAME_RE.match(name or ""):
        return False
    if not (uri or "").startswith(_SOURCES[source]["schemes"]):
        return False
    return "?" not in uri and not re.search(r"""[\s'"\\]""", uri)


def _env_datasets(source):
    """Bootstrap datasets from STUDIO_OBJECT_DATASETS, e.g.
    {"s3": [{"name": "orders", "uri": "s3://b/orders/*.parquet", "format": "parquet"}]}.
    A malformed value yields nothing rather than taking the app down."""
    raw = os.getenv("STUDIO_OBJECT_DATASETS", "").strip()
    if not raw:
        return []
    try:
        return list(json.loads(raw).get(source) or [])
    except Exception:
        return []


def datasets(source):
    """Registered datasets for a source, name-sorted: env bootstrap first, DB
    rows on top (the DB wins a collision). Validation happens here, not only on
    the add path, so the view builder can never see a name or URI that has not
    been vetted — including a row written before a rule tightened."""
    out = {}
    rows = _rows(source)
    for d in _env_datasets(source) + rows:
        name, uri = str((d or {}).get("name", "")), str((d or {}).get("uri", ""))
        fmt = str((d or {}).get("format", "parquet")).lower()
        if _valid(source, name, uri, fmt):
            out[name] = {"source": source, "name": name, "uri": uri, "format": fmt}
    return [out[n] for n in sorted(out)]


def _rows(source=None):
    c = db._conn()
    if source:
        rows = c.execute("SELECT * FROM objectstore_datasets WHERE source=? ORDER BY created_at",
                         (source,)).fetchall()
    else:
        rows = c.execute("SELECT * FROM objectstore_datasets ORDER BY created_at").fetchall()
    c.close()
    return [dict(r) for r in rows]


def _view_sql(d):
    """CREATE VIEW for one dataset. hive_partitioning keeps key=value folder
    layouts readable as columns (and prunes files on a filter); union_by_name
    tolerates schema drift across a glob."""
    reader = _FORMATS[d["format"]]
    return (f'CREATE OR REPLACE VIEW "{d["name"]}" AS SELECT * FROM '
            f"{reader}('{_esc(d['uri'])}', hive_partitioning=true, union_by_name=true)")


# ── Credentials (env only; never in the registry) ───────────────────────

# DuckDB's S3 filesystem ingests AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
# AWS_SESSION_TOKEN straight from the process environment into these settings,
# where current_setting() hands them back to any SELECT — verified, and it
# happens even when the credential we actually use is a secret. Cleared once
# the secret exists. (Azure does not do this; its setting stays NULL.)
_S3_LEAKY_SETTINGS = ["RESET s3_access_key_id", "RESET s3_secret_access_key",
                      "RESET s3_session_token"]


def _credentials(source):
    """{present, secret, legacy, reset} for a source. Two SQL forms because the
    secrets manager only exists from DuckDB 0.10; the legacy SET path is the
    fallback and is strictly worse — its values are readable from any SELECT
    via duckdb_settings(), which is a credential-disclosure channel."""
    if source == "s3":
        key, sec = os.getenv("AWS_ACCESS_KEY_ID", ""), os.getenv("AWS_SECRET_ACCESS_KEY", "")
        region = os.getenv("AWS_REGION", "us-east-1")
        parts = [f"KEY_ID '{_esc(key)}'", f"SECRET '{_esc(sec)}'", f"REGION '{_esc(region)}'"]
        legacy = [f"SET s3_access_key_id='{_esc(key)}'",
                  f"SET s3_secret_access_key='{_esc(sec)}'",
                  f"SET s3_region='{_esc(region)}'"]
        token = os.getenv("AWS_SESSION_TOKEN", "")
        if token:
            parts.append(f"SESSION_TOKEN '{_esc(token)}'")
            legacy.append(f"SET s3_session_token='{_esc(token)}'")
        endpoint = os.getenv("STUDIO_S3_ENDPOINT", "")
        if endpoint:
            # MinIO/Ceph/Tigris: host[:port] with no scheme, and path-style
            # URLs — virtual-host style needs per-bucket DNS they rarely have.
            style = os.getenv("STUDIO_S3_URL_STYLE", "path")
            ssl = "false" if os.getenv("STUDIO_S3_USE_SSL", "true").lower() in ("0", "false", "no") else "true"
            parts += [f"ENDPOINT '{_esc(endpoint)}'", f"URL_STYLE '{_esc(style)}'", f"USE_SSL {ssl}"]
            legacy += [f"SET s3_endpoint='{_esc(endpoint)}'", f"SET s3_url_style='{_esc(style)}'",
                       f"SET s3_use_ssl={ssl}"]
        return {"present": bool(key and sec),
                "secret": f"CREATE OR REPLACE SECRET studio_s3 (TYPE s3, PROVIDER config, {', '.join(parts)})",
                "legacy": legacy, "reset": _S3_LEAKY_SETTINGS}

    if source == "gcs":
        # DuckDB reaches GCS over its S3-compatible endpoint, so these must be
        # HMAC interoperability keys — a service-account JSON key will not work.
        key, sec = os.getenv("GCS_HMAC_KEY_ID", ""), os.getenv("GCS_HMAC_SECRET", "")
        return {"present": bool(key and sec),
                "secret": f"CREATE OR REPLACE SECRET studio_gcs (TYPE gcs, KEY_ID '{_esc(key)}', SECRET '{_esc(sec)}')",
                "legacy": [f"SET s3_access_key_id='{_esc(key)}'",
                           f"SET s3_secret_access_key='{_esc(sec)}'",
                           "SET s3_endpoint='storage.googleapis.com'",
                           "SET s3_url_style='path'"],
                "reset": _S3_LEAKY_SETTINGS}

    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    account = os.getenv("AZURE_STORAGE_ACCOUNT", "")
    if conn_str:
        return {"present": True,
                "secret": f"CREATE OR REPLACE SECRET studio_azure (TYPE azure, CONNECTION_STRING '{_esc(conn_str)}')",
                "legacy": [f"SET azure_storage_connection_string='{_esc(conn_str)}'"],
                "reset": []}
    # Service principal — the form to use when the account key is not shareable.
    tenant = os.getenv("AZURE_STORAGE_TENANT_ID", "")
    client = os.getenv("AZURE_STORAGE_CLIENT_ID", "")
    secret = os.getenv("AZURE_STORAGE_CLIENT_SECRET", "")
    return {"present": bool(account and tenant and client and secret),
            "secret": ("CREATE OR REPLACE SECRET studio_azure (TYPE azure, PROVIDER service_principal, "
                       f"TENANT_ID '{_esc(tenant)}', CLIENT_ID '{_esc(client)}', "
                       f"CLIENT_SECRET '{_esc(secret)}', ACCOUNT_NAME '{_esc(account)}')"),
            "legacy": [f"SET azure_account_name='{_esc(account)}'"],
            "reset": []}


# ── Engine hardening ────────────────────────────────────────────────────

def _set(con, stmt):
    """Apply a hardening SET, tolerating an older engine that lacks the
    option — a missing knob must not cost us the rest of them."""
    try:
        con.execute(stmt)
    except Exception:
        pass


def _harden(con):
    """Lock the instance down, after the views exist.

    What this actually buys: refusing to auto-install/auto-load extensions, so
    a SELECT cannot pull in postgres_scan / iceberg_scan / delta_scan to reach
    a filesystem or database we never configured (they become Catalog Errors);
    no community extensions; a memory cap; and finally lock_configuration, so a
    SELECT cannot re-point s3_endpoint at an attacker's host and exfiltrate.
    It must be last — it freezes every option above it.

    What is deliberately absent: enable_external_access=false and
    disabled_filesystems='LocalFileSystem'. Both are one-way latches that kill
    s3:// / az:// / gs:// reads outright — the second fails at bind time,
    before any network call — so neither is usable on a connection whose entire
    job is remote reads. Local-file reads (read_text('/etc/passwd')) therefore
    stay reachable at the engine level: queryguard's external-function denylist
    and container isolation are what stop them, not DuckDB config.
    """
    for stmt in ("SET autoinstall_known_extensions=false",
                 "SET autoload_known_extensions=false",
                 "SET allow_community_extensions=false",
                 f"SET memory_limit='{_esc(os.getenv('STUDIO_DUCKDB_MEMORY', '2GB'))}'",
                 "SET lock_configuration=true"):
        _set(con, stmt)


class ObjectStoreConnector(Connector):
    """One Studio source per object store; `name` selects the scheme and
    extension spec from _SOURCES. Writes are not implemented on purpose —
    object storage is read-only in Studio, so run_script/submit_spark_job keep
    the base NotImplementedError and the supervisor path stays shut.

    No qualifiers() either: datasets are DuckDB views registered by name in the
    session's own default schema, so there is no namespace to qualify with and
    the base class's empty set makes the guard refuse `x.dataset` outright."""

    dialect = "duckdb"

    def __init__(self, name):
        self.name = name
        self._spec = _SOURCES[name]
        self._pool_conn = None
        self._pool_lock = threading.Lock()
        self._fingerprint = None

    def configured(self):
        # Runs on every /catalog/sources for every source, so the cheap env
        # checks come first and the registry read last. Never raises: any
        # failure here just means "not configured".
        if not _credentials(self.name)["present"]:
            return False
        try:
            import duckdb  # noqa: F401
            return bool(datasets(self.name))
        except Exception:
            return False

    def _conn(self):
        """A fresh DuckDB instance holding this source's extensions,
        credentials and dataset views. INSTALL needs network on the first run
        of a given DuckDB version; afterwards it is served from ~/.duckdb."""
        import duckdb

        con = duckdb.connect(":memory:")
        # Before the secrets manager is touched — DuckDB refuses the change
        # once a secret exists, and persistent secrets would write these
        # credentials unencrypted to ~/.duckdb/stored_secrets.
        _set(con, "SET allow_persistent_secrets=false")
        for ext in self._spec["extensions"]:
            con.execute(f"INSTALL {ext}")
            con.execute(f"LOAD {ext}")
        creds = _credentials(self.name)
        try:
            con.execute(creds["secret"])
            # The secret is now the credential in use, so drop the copies
            # DuckDB scraped out of the environment (see _S3_LEAKY_SETTINGS).
            # Only on this path: on the legacy one below those settings *are*
            # the credential. lock_configuration then stops anything re-setting
            # them from a query.
            for stmt in creds["reset"]:
                _set(con, stmt)
        except duckdb.Error:
            for stmt in creds["legacy"]:  # DuckDB < 0.10: no secrets manager
                con.execute(stmt)
        for d in datasets(self.name):
            # CREATE VIEW binds the reader, so a bad URI or credential fails
            # here rather than on some later agent query.
            con.execute(_view_sql(d))
        _harden(con)
        return con

    def _fp(self):
        """Fingerprint of the registry. Views are baked into the connection at
        build time, so registering or removing a dataset means a rebuild."""
        return json.dumps(datasets(self.name), sort_keys=True)

    def _dispose(self):
        if self._pool_conn is not None:
            try:
                self._pool_conn.close()
            except Exception:
                pass
            self._pool_conn = None
            self._fingerprint = None


    # ── Connection pool (single persistent instance, rebuild on error) ─────

    def _execute(self, fn):
        """Run `fn(connection)` on the pooled instance; rebuild once on an
        instance-level failure, and whenever the dataset registry has changed
        under us. Serialized by a lock — which is also why fn may execute on
        the connection itself: DuckDB's s3/azure settings are SESSION-scoped,
        and a per-call cursor() opens a fresh session that silently drops the
        legacy credentials and re-scrapes AWS_* from the environment. One
        session, one lock, no surprises.

        A wrong QUERY (unknown column, bad syntax) must not pay for a rebuild:
        re-creating the instance re-installs extensions and re-binds every
        dataset view (parquet footers re-fetched from the store) and would run
        the failing query twice. Only errors that poison the instance retry.
        """
        import duckdb
        query_errors = tuple(
            exc for exc in (getattr(duckdb, n, None) for n in (
                "BinderException", "CatalogException", "ParserException",
                "SyntaxException", "InvalidInputException",
                "ConversionException", "OutOfRangeException"))
            if exc is not None)
        fp = self._fp()  # outside the lock: it reads the app DB
        with self._pool_lock:
            if fp != self._fingerprint:
                self._dispose()
            for attempt in (1, 2):
                if self._pool_conn is None:
                    self._pool_conn = self._conn()
                    self._fingerprint = fp
                try:
                    return fn(self._pool_conn)
                except query_errors:
                    raise                      # the SQL is wrong, not the instance
                except Exception:
                    self._dispose()
                    if attempt == 2:
                        raise

    def close(self):
        with self._pool_lock:
            self._dispose()

    def list_tables(self):
        """The registry defines the tables, so this needs no engine round-trip."""
        return [d["name"] for d in datasets(self.name)]

    def get_schema(self, table):
        if table not in {d["name"] for d in datasets(self.name)}:
            raise ValueError(f"Unknown dataset {table}")

        def go(con):
            # duckdb_columns() binds the name; DESCRIBE would need it spliced
            # into the statement as an identifier. Executed on the pooled
            # session itself (see _execute) — a cursor would be a new session
            # without the credentials.
            rows = con.execute(
                "SELECT column_name, data_type FROM duckdb_columns() "
                "WHERE table_name = ? ORDER BY column_index", [table]).fetchall()
            return [{"name": r[0], "type": r[1]} for r in rows]
        return self._execute(go)

    def run_query(self, sql):
        def go(con):
            # Directly on the pooled session — _execute's lock serializes every
            # caller, and only THIS session carries the legacy credentials and
            # the env-scrape RESETs (a cursor() is a fresh session that would
            # re-scrape AWS_* and, on the legacy path, hit AWS anonymously).
            res = con.execute(sql)
            columns = [d[0] for d in res.description]
            # DuckDB returns date/datetime/Decimal — a hive-partitioned `dt`
            # column arrives as datetime.date — so coerce to JSON-safe or the
            # results die in the tile cache and the API response.
            rows = jsonify_rows(res.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000"))))
            return columns, rows
        return self._execute(go)


OBJECT_STORE_CONNECTORS = [ObjectStoreConnector(n) for n in _SOURCES]


# ── Admin registry ──────────────────────────────────────────────────────

class DatasetIn(BaseModel):
    source: str
    name: str
    uri: str
    format: str = "parquet"


def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Object-store datasets are admin-only")


@router.get("")
def list_datasets(user=Depends(current_user)):
    _admin(user)
    rows = _rows()
    stored = {(r["source"], r["name"]) for r in rows}
    # Bootstrap datasets are live but have no row to delete, so the UI needs
    # them listed separately rather than mixed into the removable ones.
    return {"datasets": rows,
            "env_datasets": [d for s in _SOURCES for d in datasets(s)
                             if (s, d["name"]) not in stored],
            "sources": {s: list(v["schemes"]) for s, v in _SOURCES.items()},
            "formats": sorted(_FORMATS)}


def _upsert_dataset(source, name, uri, fmt):
    """Register (source, name) idempotently: DELETE then INSERT under the
    unique index, so a re-register updates the row in place instead of
    duplicating it. Shared by the admin add_dataset endpoint and the Spark
    write→read bridge (register_output); the connector picks the change up via
    its registry fingerprint on the next query. Returns the stored row.

    Validation and any authority/confinement check are the CALLER's job — this
    is the write primitive, deliberately unguarded so both call sites can layer
    their own gate (admin role here, admin-approval + prefix confinement in the
    bridge) without one path silently inheriting the other's."""
    row_id, created = str(uuid.uuid4()), time.time()
    c = db._conn()
    c.execute("DELETE FROM objectstore_datasets WHERE source=? AND name=?", (source, name))
    c.execute("INSERT INTO objectstore_datasets (id, source, name, uri, format, created_at) "
              "VALUES (?,?,?,?,?,?)", (row_id, source, name, uri, fmt, created))
    c.commit()
    c.close()
    return {"id": row_id, "source": source, "name": name, "uri": uri,
            "format": fmt, "created_at": created}


@router.post("", status_code=201)
def add_dataset(body: DatasetIn, user=Depends(current_user)):
    _admin(user)
    source, name = body.source.strip(), body.name.strip()
    uri, fmt = body.uri.strip(), (body.format or "parquet").strip().lower()
    if source not in _SOURCES:
        raise HTTPException(400, f"source must be one of {', '.join(_SOURCES)}")
    if not _NAME_RE.match(name):
        raise HTTPException(400, "name must be a SQL identifier — it becomes the "
                                 "view name agents query and RBAC allowlists")
    if fmt not in _FORMATS:
        raise HTTPException(400, f"format must be one of {', '.join(sorted(_FORMATS))}")
    if not _valid(source, name, uri, fmt):
        raise HTTPException(400, f"uri must start with {' or '.join(_SOURCES[source]['schemes'])} "
                                 "and contain no query string, quotes or whitespace")
    # The connector notices via its registry fingerprint and rebuilds its views
    # on the next query — nothing to invalidate here.
    _upsert_dataset(source, name, uri, fmt)
    db.log_activity(user, "objectstore_register", prompt=f"{source}.{name}")
    return {"datasets": _rows()}


@router.delete("/{source}/{name}")
def remove_dataset(source: str, name: str, user=Depends(current_user)):
    _admin(user)
    c = db._conn()
    c.execute("DELETE FROM objectstore_datasets WHERE source=? AND name=?", (source, name))
    c.commit()
    c.close()
    return {"datasets": _rows()}


# ── Lakehouse write→read bridge (Spark output → queryable dataset) ────────
#
# The agent-built loop is: read a registered S3 source, run Spark, write
# Parquet back to S3, and then *query that output in chat*. The last hop needs
# the fresh Parquet to become an objectstore dataset — but a Spark job that
# gets to name its own output URI is an exfiltration/priv surface: left
# unchecked it could register s3://someone-elses-bucket/* or a URI that smuggles
# credentials, and it would do so with an admin's authority. So registration
# from a job success runs through the SAME shape as a manual register, plus one
# additive control: the output URI must sit under an ALLOWED PREFIX. Nothing
# here fires until a real Databricks/K8s Spark run reports terminal SUCCESS, so
# it stays dormant exactly like the read connectors until infra is configured.


class RegistrationError(Exception):
    """An output URI could not be safely registered — it failed _valid() or
    fell outside the allowed prefix. Raised (never swallowed) so a job-success
    caller surfaces the refusal instead of silently registering a bad URI or
    silently dropping a good one. register_spark_output() below turns it into a
    fail-safe {"error": ...} for callers that would rather branch than catch."""


def _prefix_of(uri):
    """The bucket/prefix a dataset URI lives under: everything up to the first
    glob char, trimmed back to the last real path separator. So
    s3://b/orders/*.parquet -> s3://b/orders/ and s3://b/x/y/z.parquet ->
    s3://b/x/y/. Used only as a confinement anchor, never as a reader path."""
    cut = min((uri.find(c) for c in "*?[" if c in uri), default=len(uri))
    base = uri[:cut]
    scheme = base.find("//")
    slash = base.rfind("/")
    # Keep the scheme's '//' intact — only trim a genuine path separator so a
    # bare bucket root (s3://b) is never mangled into an empty/partial prefix.
    return base[:slash + 1] if scheme != -1 and slash > scheme + 1 else base


def dataset_prefix(source, name):
    """Confinement prefix of a REGISTERED source dataset, or None if no such
    dataset exists. Exposed so a caller can default a Spark output's allowed
    prefix to wherever the job's own SOURCE lives — a job may write back only
    into its input's bucket/prefix. Reads the vetted registry (datasets()),
    so it never returns a prefix derived from an unvalidated URI."""
    src = next((d for d in datasets(source) if d["name"] == name), None)
    return _prefix_of(src["uri"]) if src else None


def _allowed_prefixes(source, allowed_prefix, from_dataset):
    """Resolve the prefixes an output URI may live under, most-explicit first:
    the caller's allowed_prefix, else the STUDIO_SPARK_OUTPUT_PREFIX allowlist
    (comma-separated), else the source dataset's own prefix. An empty result
    means 'nothing is permitted' — the confinement check then refuses every
    URI, which is the safe default when no boundary has been configured."""
    if allowed_prefix:
        pfx = allowed_prefix if isinstance(allowed_prefix, (list, tuple)) else [allowed_prefix]
        return [str(p) for p in pfx if p]
    env = [p.strip() for p in os.getenv("STUDIO_SPARK_OUTPUT_PREFIX", "").split(",") if p.strip()]
    if env:
        return env
    pfx = dataset_prefix(source, from_dataset) if from_dataset else None
    return [pfx] if pfx else []


def _confine(source, name, uri, fmt, allowed_prefix, from_dataset):
    """Validate + confine an output URI, or raise RegistrationError. Returns
    the cleaned (source, name, uri, fmt). Two checks, both required:
    _valid() (right scheme for the source, identifier name, no credential-
    smuggling query string / quotes / whitespace) AND prefix confinement (the
    additive control that stops s3://someone-elses-bucket/*)."""
    source = (source or "").strip()
    name = (name or "").strip()
    uri = (uri or "").strip()
    fmt = (fmt or "parquet").strip().lower()
    if not _valid(source, name, uri, fmt):
        raise RegistrationError(
            "output URI failed objectstore validation "
            f"(source={source!r}, name={name!r}, format={fmt!r}, uri={uri!r})")
    prefixes = _allowed_prefixes(source, allowed_prefix, from_dataset)
    # Separator-aware confinement: normalize every boundary to end with "/" so a
    # prefix matches only on a whole path segment. A bare startswith lets a
    # SIBLING directory slip through — "s3://b/orders-EVIL/x".startswith(
    # "s3://b/orders") is True — which would exfiltrate from a look-alike dir.
    # With the trailing "/", "s3://b/orders/" matches "s3://b/orders/enriched/*"
    # but NOT "s3://b/orders-EVIL/...".
    bounded = [p if p.endswith("/") else p + "/" for p in prefixes]
    if not bounded or not any(uri.startswith(p) for p in bounded):
        raise RegistrationError(
            f"output URI {uri!r} is outside the allowed prefix {bounded}")
    return source, name, uri, fmt


def register_output(user, source, name, uri, fmt="parquet", allowed_prefix=None,
                    from_dataset=None):
    """Register (or idempotently update) a Spark job's declared object-store
    output as a queryable dataset — the write→read half of the lakehouse loop.
    Safe to call from another module when a supervised Spark job reaches
    genuine SUCCESS.

    Gate: admin-only, the SAME authority as the manual add_dataset endpoint
    (_admin on the passed-in user) — no broadening. The new row is an ordinary
    objectstore_datasets row, RBAC-scoped by rbac._OBJECT_STORES identically to
    every other dataset: a role without the object store still cannot see it.

    Confinement: the URI must pass _valid() AND sit under an allowed prefix
    (allowed_prefix, else STUDIO_SPARK_OUTPUT_PREFIX, else the from_dataset
    source's own prefix); anything else raises RegistrationError and writes
    NOTHING — fail-safe. Idempotent: re-registering the same (source, name)
    updates in place via _upsert_dataset, never duplicates. Returns the stored
    dataset row."""
    _admin(user)
    source, name, uri, fmt = _confine(source, name, uri, fmt, allowed_prefix, from_dataset)
    row = _upsert_dataset(source, name, uri, fmt)
    db.log_activity(user, "objectstore_spark_register", prompt=f"{source}.{name}",
                    source=source, table=name)
    return row


def register_spark_output(output, job, user):
    """Fail-safe wrapper for the /live terminal-SUCCESS hook: register a
    succeeded Spark job's declared output. `output` is the DatasetIn-shaped
    contract carried in the job script — {source, name, uri, format,
    from_dataset} — and `job` is the supervised-job row.

    Authority comes from the run itself: a platform_run reaches SUCCESS only
    because a human admin approved it, stamping job['human_by']. So this gates
    on job.get('human_by') (that admin authorized the write-back) rather than
    on whoever happens to poll /live — exactly the manual admin authority,
    without letting a non-admin poller trigger a registration.

    Never raises and never leaves a half-state: returns {'registered': name,
    'dataset': row} on success, or {'error': reason} on any refusal (bad shape,
    not admin-approved, invalid/out-of-prefix URI). The bridge writes that dict
    into the job result so /live and the Flow graph surface either the closed
    loop or the visible refusal."""
    if not isinstance(output, dict):
        return {"error": "no output contract declared on this job"}
    if not (job or {}).get("human_by"):
        return {"error": "job was not admin-approved; refusing to register output"}
    try:
        # SECURITY: the confinement boundary must NEVER come from the
        # (attacker-influenceable) job payload. Pass allowed_prefix=None so a
        # payload-supplied "allowed_prefix" is IGNORED; the boundary is resolved
        # server-side only — STUDIO_SPARK_OUTPUT_PREFIX, else the source
        # dataset's own prefix. from_dataset is a NAME looked up in
        # objectstore_datasets (dataset_prefix), so its boundary is that
        # registry row's URI, never a value the payload can forge. No
        # server-side boundary → _confine refuses (fail-safe).
        row = _confine(output.get("source", "s3"), output.get("name", ""),
                       output.get("uri", ""), output.get("format", "parquet"),
                       None, output.get("from_dataset"))
    except RegistrationError as e:
        return {"error": str(e)}
    source, name, uri, fmt = row
    stored = _upsert_dataset(source, name, uri, fmt)
    if user:
        db.log_activity(user, "objectstore_spark_register", prompt=f"{source}.{name}",
                        source=source, table=name)
    return {"registered": name, "dataset": stored}
