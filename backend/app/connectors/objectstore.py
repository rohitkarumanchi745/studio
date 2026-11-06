"""S3 / Azure Blob / GCS as Studio sources, backed by DuckDB.

Object storage has no query engine and no notion of a table, so there is
nothing for an agent to point SQL at until someone says what a table *is*. An
admin therefore registers datasets — (source, name, uri, format) — and each one
becomes a named DuckDB view over those objects. Agents then write ordinary SQL
against the view NAMES, which is the whole point: names flow through the
existing guardrails unchanged (rbac.py allowlists them, queryguard.py rejects
everything else), and a raw object URI in agent SQL simply fails validation.
The view DDL is built here from the vetted registry, never from model output.

Two ways a store is set up. The built-in sources (s3, azure_blob, gcs) read
their credentials from the environment (see .env.example). A connection made
on the Integrations screen (connections.py) carries its own — stored
encrypted with the connection, as every other connector's are — together with
its first dataset; an admin can set up a built-in source the same way. Either
way, credentials never enter the dataset registry, so a registry row leaks
nothing but a bucket path.

Datasets come from the admin table, the connection's own first dataset, and
(built-ins only) a STUDIO_OBJECT_DATASETS env bootstrap; the DB wins a name
collision.

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
    with db.connect() as c:
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


# ── Dataset registry (env bootstrap + DB) ───────────────────────────────

def _esc(v):
    """Escape a SQL string literal. Secret values and reader paths cannot be
    bound as prepared-statement parameters, so they must be interpolated."""
    return str(v).replace("'", "''")


def _valid(kind, name, uri, fmt):
    """A dataset is only usable if its name is an identifier, its format has a
    reader, and its URI belongs to this source and carries no query string —
    DuckDB's legacy `s3://…?s3_access_key_id=…` form lets a URI smuggle its own
    credentials and endpoint. (SET s3_url_compatibility_mode closes that hole
    too, but it also disables globbing on S3 URLs, which every dataset uses.)"""
    if kind not in _SOURCES or fmt not in _FORMATS:
        return False
    if not _NAME_RE.match(name or ""):
        return False
    if not (uri or "").startswith(_SOURCES[kind]["schemes"]):
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


def config_dataset(config):
    """The first dataset a connection was set up with (its form's path, name
    and format), or None."""
    config = config or {}
    uri = str(config.get("path") or "").strip()
    if not uri:
        return None
    return {"name": str(config.get("dataset") or "data").strip(), "uri": uri,
            "format": str(config.get("format") or "parquet").strip().lower()}


def datasets(source, kind=None, config=None):
    """Registered datasets for a source, name-sorted: a built-in's env
    bootstrap first, then the connection's own first dataset, then DB rows on
    top (the DB wins a collision). Validation happens here, not only on the
    add path, so the view builder can never see a name or URI that has not
    been vetted — including a row written before a rule tightened."""
    kind = kind or source
    out = {}
    first = config_dataset(config)
    bootstrap = _env_datasets(source) if source in _SOURCES else []
    for d in bootstrap + ([first] if first else []) + _rows(source):
        name, uri = str((d or {}).get("name", "")), str((d or {}).get("uri", ""))
        fmt = str((d or {}).get("format", "parquet")).lower()
        if _valid(kind, name, uri, fmt):
            out[name] = {"source": source, "name": name, "uri": uri, "format": fmt}
    return [out[n] for n in sorted(out)]


def _rows(source=None):
    with db.connect() as c:
        if source:
            rows = c.execute("SELECT * FROM objectstore_datasets WHERE source=? ORDER BY created_at",
                             (source,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM objectstore_datasets ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def _view_sql(d):
    """CREATE VIEW for one dataset. hive_partitioning keeps key=value folder
    layouts readable as columns (and prunes files on a filter); union_by_name
    tolerates schema drift across a glob."""
    reader = _FORMATS[d["format"]]
    return (f'CREATE OR REPLACE VIEW "{d["name"]}" AS SELECT * FROM '
            f"{reader}('{_esc(d['uri'])}', hive_partitioning=true, union_by_name=true)")


# ── Setting a store up from the Integrations screen ─────────────────────

_EXTENSIONS = {".parquet": "parquet", ".pq": "parquet", ".csv": "csv", ".tsv": "csv",
               ".json": "json", ".jsonl": "json", ".ndjson": "json"}
EXAMPLE_PATH = {"s3": "s3://bucket/sales/*.parquet", "azure_blob": "az://container/sales/*.parquet",
                "gcs": "gs://bucket/sales/*.parquet"}


def guess_format(uri):
    """The format a path's files are in, from their extension, or None."""
    tail = str(uri or "").lower().rsplit("/", 1)[-1].removesuffix(".gz")
    return next((fmt for ext, fmt in _EXTENSIONS.items() if tail.endswith(ext)), None)


# Instance-metadata services: AWS (also over IPv6), GCP, Azure, Oracle, and
# Alibaba's — besides the link-local range most of them sit in.
_METADATA_ADDRS = {"169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


def _host_of(endpoint):
    """The host in `host[:port]`, `[v6]:port` or a URL."""
    rest = re.sub(r"^[a-z][a-z0-9+.-]*://", "", str(endpoint or "").strip(), flags=re.I)
    rest = rest.split("/")[0].rsplit("@", 1)[-1]
    bracketed = re.match(r"^\[([^\]]+)\]", rest)
    if bracketed:
        return bracketed.group(1)
    return rest.rsplit(":", 1)[0] if rest.count(":") == 1 else rest


def _metadata_host(host):
    """A cloud instance-metadata address (169.254.169.254, metadata.google.
    internal…): never a storage endpoint, and the one address an endpoint
    field must not be able to point the server at. Names are resolved, and an
    IPv4 address written as IPv6 is read as the IPv4 it is."""
    import ipaddress
    import socket
    host = str(host or "").strip().rstrip(".").lower()
    if host in ("metadata", "metadata.google.internal"):
        return True

    def addr(text):
        a = ipaddress.ip_address(text.split("%")[0])      # an IPv6 scope is not part of it
        return (a.ipv4_mapped or a) if a.version == 6 else a
    try:
        addrs = {addr(host)}
    except ValueError:
        try:
            addrs = {addr(a[4][0]) for a in socket.getaddrinfo(host, None)}
        except (OSError, ValueError, UnicodeError):
            return False
    return any(a.is_link_local or str(a) in _METADATA_ADDRS for a in addrs)


def _azure_endpoints(conn_str):
    """The hosts an Azure connection string names (BlobEndpoint=…, …)."""
    pairs = (part.split("=", 1) for part in str(conn_str or "").split(";") if "=" in part)
    return [_host_of(v) for k, v in pairs if k.strip().lower().endswith("endpoint") and v.strip()]


def check_config(kind, cfg):
    """(tidied config, {field: problem}) for a store's connect form, before
    anything signs in. Fills what has an obvious answer (the format from the
    files' extension, "data" as the name) and says, per field, what is wrong
    — never repeating a value typed, which may be a secret in the wrong box."""
    cfg = {k: str(v).strip() for k, v in (cfg or {}).items() if v is not None}
    problems = {}
    path = cfg.get("path", "")
    cfg["format"] = (cfg.get("format") or guess_format(path) or "parquet").lower()
    cfg["dataset"] = cfg.get("dataset") or "data"
    schemes = _SOURCES[kind]["schemes"]
    if not path.startswith(schemes):
        problems["path"] = (f"A path here starts with {' or '.join(schemes)} — for example "
                            f"{EXAMPLE_PATH[kind]}.")
    elif "?" in path or re.search(r"""[\s'"\\]""", path):
        problems["path"] = "A path cannot hold spaces, quotes or a query string (?…)."
    if not _NAME_RE.match(cfg["dataset"]):
        problems["dataset"] = ("Letters, digits and _ only, starting with a letter — it becomes the "
                               "table agents query.")
    if cfg["format"] not in _FORMATS:
        problems["format"] = f"One of {', '.join(sorted(_FORMATS))}."
    if kind == "s3" and cfg.get("endpoint"):
        endpoint = cfg["endpoint"]
        if endpoint.lower().startswith("http://"):
            cfg["use_ssl"] = "false"
        endpoint = re.sub(r"^https?://", "", endpoint, flags=re.I).rstrip("/")
        cfg["endpoint"] = endpoint
        if "/" in endpoint:
            problems["endpoint"] = "Just the host and port — for example minio.example.com:9000."
        elif _metadata_host(_host_of(endpoint)):
            problems["endpoint"] = "That is a cloud metadata address, not a storage endpoint."
    if kind == "azure_blob":
        principal = all(cfg.get(k) for k in ("account_name", "tenant_id", "client_id", "client_secret"))
        if cfg.get("connection_string"):
            if "accountname=" not in cfg["connection_string"].lower():
                problems["connection_string"] = ("A connection string has AccountName=…;AccountKey=… in "
                                                 "it — copy it from the storage account's Access keys.")
            elif any(_metadata_host(h) for h in _azure_endpoints(cfg["connection_string"])):
                # BlobEndpoint=… is how Azurite and sovereign clouds are named —
                # and how a string could point the server at its metadata service.
                problems["connection_string"] = ("It names a cloud metadata address as an endpoint, "
                                                 "not a storage account.")
        elif not principal:
            problems["connection_string"] = (
                "Paste the storage account's connection string (Access keys, in the Azure portal), "
                "or fill in all four service-principal fields.")
    return cfg, problems


def classify_error(exc, kind):
    """{said, field, stage} for a store that would not read, or None. The
    error text is only matched, never returned: `said` is a fixed sentence, so
    nothing a driver echoed (a key, a signed URL) can reach the screen."""
    seen, stack, texts = set(), [exc], []
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        texts.append(str(e))
        stack += [getattr(e, "__cause__", None), getattr(e, "__context__", None)]
    text = " ".join(texts).lower()
    has = lambda *words: any(w in text for w in words)
    if "extension" in text and has("install", "download", "failed to load"):
        return {"said": "Studio could not load DuckDB's storage extension — it is downloaded once, "
                        "from extensions.duckdb.org, so the server has to be able to reach it.",
                "field": None, "stage": "check"}
    if has("403", "accessdenied", "access denied", "invalidaccesskeyid", "signaturedoesnotmatch",
           "authorizationfailure", "authenticationfailed", "forbidden", "401", "unauthorized",
           "invalid credentials", "invalid_grant"):
        field = {"s3": "secret_access_key", "gcs": "hmac_secret", "azure_blob": "connection_string"}[kind]
        return {"said": "The store refused these credentials — check them, and that they may read "
                        "this bucket or container.", "field": field, "stage": "sign_in"}
    if has("no files found", "nosuchbucket", "nosuchkey", "404", "not found", "does not exist",
           "containernotfound", "blobnotfound", "resourcenotfound"):
        return {"said": "Signed in, but there are no files at that path — check the bucket and the "
                        "pattern.", "field": "path", "stage": "signed_in"}
    if has("could not resolve", "could not establish", "connection refused", "timed out", "timeout",
           "name or service not known", "ssl", "certificate"):
        return {"said": "Could not reach the store — check the region" +
                        (" and the endpoint." if kind == "s3" else "."),
                "field": "endpoint" if kind == "s3" else None, "stage": "sign_in"}
    if has("magic bytes", "not a parquet", "invalid parquet", "no magic", "error when sniffing",
           "sniffer", "malformed json", "invalid json", "json parse", "could not convert"):
        return {"said": "Signed in and found the files, but they are not in the format chosen — pick "
                        "the format they are in.", "field": "format", "stage": "signed_in"}
    return None


# ── Credentials (the environment, or a connection's own) ────────────────

# DuckDB's S3 filesystem ingests AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
# AWS_SESSION_TOKEN straight from the process environment into these settings,
# where current_setting() hands them back to any SELECT — verified, and it
# happens even when the credential we actually use is a secret. Cleared once
# the secret exists. (Azure does not do this; its setting stays NULL.)
_S3_LEAKY_SETTINGS = ["RESET s3_access_key_id", "RESET s3_secret_access_key",
                      "RESET s3_session_token"]


#: A connection's config keys for what the environment calls by these names.
_FROM_CONFIG = {
    "AWS_ACCESS_KEY_ID": "access_key_id", "AWS_SECRET_ACCESS_KEY": "secret_access_key",
    "AWS_SESSION_TOKEN": "session_token", "AWS_REGION": "region",
    "STUDIO_S3_ENDPOINT": "endpoint", "STUDIO_S3_URL_STYLE": "url_style", "STUDIO_S3_USE_SSL": "use_ssl",
    "GCS_HMAC_KEY_ID": "hmac_key_id", "GCS_HMAC_SECRET": "hmac_secret",
    "AZURE_STORAGE_CONNECTION_STRING": "connection_string", "AZURE_STORAGE_ACCOUNT": "account_name",
    "AZURE_STORAGE_TENANT_ID": "tenant_id", "AZURE_STORAGE_CLIENT_ID": "client_id",
    "AZURE_STORAGE_CLIENT_SECRET": "client_secret",
}


def _reader(config):
    """getenv, or — for a connection with its own credentials — its config."""
    if config is None:
        return os.getenv
    return lambda name, default="": (str(config.get(_FROM_CONFIG.get(name, ""), "") or "").strip()
                                     or default)


def _credentials(source, config=None):
    """{present, secret, legacy, reset} for a source — from the environment,
    or from `config` (a connection's stored credentials). Two SQL forms because
    the secrets manager only exists from DuckDB 0.10; the legacy SET path is
    the fallback and is strictly worse — its values are readable from any
    SELECT via duckdb_settings(), which is a credential-disclosure channel."""
    getenv = _reader(config)
    if source == "s3":
        key, sec = getenv("AWS_ACCESS_KEY_ID", ""), getenv("AWS_SECRET_ACCESS_KEY", "")
        region = getenv("AWS_REGION", "us-east-1")
        parts = [f"KEY_ID '{_esc(key)}'", f"SECRET '{_esc(sec)}'", f"REGION '{_esc(region)}'"]
        legacy = [f"SET s3_access_key_id='{_esc(key)}'",
                  f"SET s3_secret_access_key='{_esc(sec)}'",
                  f"SET s3_region='{_esc(region)}'"]
        token = getenv("AWS_SESSION_TOKEN", "")
        if token:
            parts.append(f"SESSION_TOKEN '{_esc(token)}'")
            legacy.append(f"SET s3_session_token='{_esc(token)}'")
        endpoint = getenv("STUDIO_S3_ENDPOINT", "")
        if endpoint:
            # MinIO/Ceph/Tigris: host[:port] with no scheme, and path-style
            # URLs — virtual-host style needs per-bucket DNS they rarely have.
            style = getenv("STUDIO_S3_URL_STYLE", "path")
            ssl = "false" if getenv("STUDIO_S3_USE_SSL", "true").lower() in ("0", "false", "no") else "true"
            parts += [f"ENDPOINT '{_esc(endpoint)}'", f"URL_STYLE '{_esc(style)}'", f"USE_SSL {ssl}"]
            legacy += [f"SET s3_endpoint='{_esc(endpoint)}'", f"SET s3_url_style='{_esc(style)}'",
                       f"SET s3_use_ssl={ssl}"]
        return {"present": bool(key and sec),
                "secret": f"CREATE OR REPLACE SECRET studio_s3 (TYPE s3, PROVIDER config, {', '.join(parts)})",
                "legacy": legacy, "reset": _S3_LEAKY_SETTINGS}

    if source == "gcs":
        # DuckDB reaches GCS over its S3-compatible endpoint, so these must be
        # HMAC interoperability keys — a service-account JSON key will not work.
        key, sec = getenv("GCS_HMAC_KEY_ID", ""), getenv("GCS_HMAC_SECRET", "")
        return {"present": bool(key and sec),
                "secret": f"CREATE OR REPLACE SECRET studio_gcs (TYPE gcs, KEY_ID '{_esc(key)}', SECRET '{_esc(sec)}')",
                "legacy": [f"SET s3_access_key_id='{_esc(key)}'",
                           f"SET s3_secret_access_key='{_esc(sec)}'",
                           "SET s3_endpoint='storage.googleapis.com'",
                           "SET s3_url_style='path'"],
                "reset": _S3_LEAKY_SETTINGS}

    conn_str = getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    account = getenv("AZURE_STORAGE_ACCOUNT", "")
    if conn_str:
        return {"present": True,
                "secret": f"CREATE OR REPLACE SECRET studio_azure (TYPE azure, CONNECTION_STRING '{_esc(conn_str)}')",
                "legacy": [f"SET azure_storage_connection_string='{_esc(conn_str)}'"],
                "reset": []}
    # Service principal — the form to use when the account key is not shareable.
    tenant = getenv("AZURE_STORAGE_TENANT_ID", "")
    client = getenv("AZURE_STORAGE_CLIENT_ID", "")
    secret = getenv("AZURE_STORAGE_CLIENT_SECRET", "")
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

    def __init__(self, name, kind=None, config=None, only=None):
        self.name = name
        # The store it reads (s3 / azure_blob / gcs): a built-in's own name, or
        # a connection's type. `config` is a connection's stored credentials
        # and first dataset; None reads the environment (the built-ins).
        # `only` pins the datasets (try_read: files not registered yet).
        self.kind = kind or name
        self._spec = _SOURCES[self.kind]
        self._config = config
        self._only = only
        self._pool_conn = None
        self._pool_lock = threading.Lock()
        self._fingerprint = None

    def configured(self):
        # Runs on every /catalog/sources for every source, so the cheap env
        # checks come first and the registry read last. Never raises: any
        # failure here just means "not configured".
        if not _credentials(self.kind, self._config)["present"]:
            return False
        try:
            import duckdb  # noqa: F401
            return bool(self._datasets())
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
        creds = _credentials(self.kind, self._config)
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
        for d in self._datasets():
            # CREATE VIEW binds the reader, so a bad URI or credential fails
            # here rather than on some later agent query.
            con.execute(_view_sql(d))
        _harden(con)
        return con

    def _datasets(self):
        if self._only is not None:
            return [d for d in self._only if _valid(self.kind, d["name"], d["uri"], d["format"])]
        return datasets(self.name, self.kind, self._config)

    def _cfg(self):
        """The keys it signs in with, by a connection's config names — to tell
        a connection made with the same keys apart (connections.same_sign_in).
        Server-side only; never returned to a client."""
        getenv = _reader(self._config)
        cfg = {key: getenv(env, "") for env, key in _FROM_CONFIG.items()}
        cfg["endpoint"] = re.sub(r"^https?://", "", cfg.get("endpoint", ""), flags=re.I).rstrip("/")
        return cfg

    def _fp(self):
        """Fingerprint of the registry. Views are baked into the connection at
        build time, so registering or removing a dataset means a rebuild."""
        return json.dumps(self._datasets(), sort_keys=True)

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
        return [d["name"] for d in self._datasets()]

    def get_schema(self, table):
        if table not in {d["name"] for d in self._datasets()}:
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
    format: str = ""              # empty: from the files' extension, else parquet
    # Read the files with the source's own keys before registering them, so a
    # wrong path or format is refused now, not on some later question.
    check: bool = False


def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Object-store datasets are admin-only")


def store_sources():
    """{source name: kind} for every object-store source: the built-ins and
    the object-store connections made on the Integrations screen."""
    from .. import connections          # lazy: connections builds on this module
    return {**{s: s for s in _SOURCES}, **connections.object_store_connections()}


def kind_of(source):
    """The store a source reads (s3 / azure_blob / gcs), or None."""
    return store_sources().get(source)


def _live(source):
    """The store connector a source runs on now, or None (unknown, or switched
    off on the Integrations screen)."""
    from . import get_connector
    try:
        conn = get_connector(source)
    except KeyError:
        return None
    return conn if isinstance(conn, ObjectStoreConnector) else None


def live_datasets(source):
    """Every dataset a source reads now: a built-in's env bootstrap, the
    connection's own first one, and the registry's."""
    conn = _live(source)
    return conn._datasets() if conn else []


def try_read(source, kind, d):
    """None when dataset `d` reads with `source`'s own keys, else {said,
    field, stage} — classify_error's fixed sentences, never the driver's text."""
    conn = _live(source)
    if conn is None:
        return {"said": f"“{source}” is not connected — reconnect it first.", "field": None,
                "stage": "check"}
    if not _credentials(kind, conn._config)["present"]:
        return {"said": f"“{source}” has no keys yet — set it up first.", "field": None,
                "stage": "check"}
    probe = ObjectStoreConnector(source, kind, conn._config, only=[d])
    try:
        probe.get_schema(d["name"])
    except Exception as exc:
        return classify_error(exc, kind) or {
            "said": "Studio could not read those files — check the path and the format.",
            "field": None, "stage": "signed_in"}
    finally:
        probe.close()
    return None


@router.get("")
def list_datasets(user=Depends(current_user)):
    _admin(user)
    rows = _rows()
    stored = {(r["source"], r["name"]) for r in rows}
    # Bootstrap datasets are live but have no row to delete, so the UI needs
    # them listed separately rather than mixed into the removable ones.
    sources = store_sources()
    return {"datasets": rows,
            "env_datasets": [d for s in _SOURCES for d in datasets(s)
                             if (s, d["name"]) not in stored],
            "sources": {s: list(_SOURCES[k]["schemes"]) for s, k in sources.items()},
            "kinds": sources,
            # What each source reads right now, its setup's own dataset
            # included — only the registry's rows (`datasets`) can be removed.
            "live": {s: live_datasets(s) for s in sources},
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
    with db.connect() as c:
        c.execute("DELETE FROM objectstore_datasets WHERE source=? AND name=?", (source, name))
        c.execute("INSERT INTO objectstore_datasets (id, source, name, uri, format, created_at) "
                  "VALUES (?,?,?,?,?,?)", (row_id, source, name, uri, fmt, created))
        c.commit()
    return {"id": row_id, "source": source, "name": name, "uri": uri,
            "format": fmt, "created_at": created}


@router.post("", status_code=201)
def add_dataset(body: DatasetIn, user=Depends(current_user)):
    _admin(user)
    source, name = body.source.strip(), body.name.strip()
    uri = body.uri.strip()
    fmt = (body.format.strip() or guess_format(uri) or "parquet").lower()
    kind = kind_of(source)
    if kind is None:
        raise HTTPException(400, f"'{source}' is not an object-store source")
    if not _NAME_RE.match(name):
        raise HTTPException(400, "name must be a SQL identifier — it becomes the "
                                 "view name agents query and RBAC allowlists")
    if fmt not in _FORMATS:
        raise HTTPException(400, f"format must be one of {', '.join(sorted(_FORMATS))}")
    if not _valid(kind, name, uri, fmt):
        raise HTTPException(400, f"uri must start with {' or '.join(_SOURCES[kind]['schemes'])} "
                                 "and contain no query string, quotes or whitespace")
    if body.check:
        problem = try_read(source, kind, {"source": source, "name": name, "uri": uri, "format": fmt})
        if problem:
            # The form's own names: the files are its "uri"; a refused key is
            # the source's, not this form's.
            field = {"path": "uri", "format": "format"}.get(problem["field"])
            raise HTTPException(400, {"error": problem["said"], "stage": problem["stage"],
                                      "fields": {field: problem["said"]} if field else {}})
    # The connector notices via its registry fingerprint and rebuilds its views
    # on the next query — nothing to invalidate here.
    _upsert_dataset(source, name, uri, fmt)
    db.log_activity(user, "objectstore_register", prompt=f"{source}.{name}")
    return {"datasets": _rows()}


@router.delete("/{source}/{name}")
def remove_dataset(source: str, name: str, user=Depends(current_user)):
    _admin(user)
    with db.connect() as c:
        c.execute("DELETE FROM objectstore_datasets WHERE source=? AND name=?", (source, name))
        c.commit()
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
