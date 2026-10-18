"""Databricks SQL warehouse connector. Configure via env (see .env.example);
requires `pip install databricks-sql-connector` (commented in requirements.txt).

This module also owns the shape of a Databricks **Jobs run-submit body**
(`build_submission` / `validate_submission`), because the Jobs API is far
pickier than "a dict with tasks in it": every task needs a `task_key`, exactly
one recognized task type, and compute to run on. Building and checking that
body here — next to the credentials it needs — keeps flow.py from hand-rolling
a payload the endpoint would reject with a 400.
"""
import logging
import os
import re
import threading

from .base import Connector, QueryCancelled, current_cancel, jsonify_rows
from ..queryguard import FULL_NAMES

#: Schema value meaning "every schema" — of the configured catalog, or of every
#: catalog the token can see when no catalog is set. Such a source lists its
#: tables by full name (catalog.schema.table) and the query guard matches them
#: whole (queryguard.FULL_NAMES).
ALL_SCHEMAS = "*"
#: Catalogs never listed in an all-schemas source: `system` holds the
#: workspace's audit, billing and access logs, not business data.
_SKIPPED_CATALOGS = {"system"}


def _with_comments(cur, sql):
    """Rows of `sql` with its `comment` column last — `{c}` marks where it
    goes — or, where information_schema has no such column, the same rows with
    None in its place: descriptions are a bonus, never a reason to fail."""
    try:
        cur.execute(sql.replace("{c}", ", comment"))
        return [tuple(r) for r in cur.fetchall()]
    except Exception:
        cur.execute(sql.replace("{c}", ""))
        return [tuple(r) + (None,) for r in cur.fetchall()]
#: Only plain identifiers make a listable name: the catalog endpoints and the
#: query guard both read a name as dot-separated plain parts.
_PLAIN = re.compile(r"^[A-Za-z_][\w$]*$")


def max_tables():
    """Cap on the tables an all-schemas source lists (and the agent is told
    about): a metastore can hold tens of thousands."""
    try:
        return max(1, int(os.getenv("STUDIO_DATABRICKS_MAX_TABLES", "1000")))
    except ValueError:
        return 1000

#: Env var naming the SQL warehouse that a submitted job's sql_tasks run on.
#: There is no default: a Jobs run-submit with a sql_task and no warehouse is
#: rejected by the API, so a missing value must REFUSE the submission (with
#: this name in the message) rather than post something that will 400.
WAREHOUSE_VAR = "DATABRICKS_WAREHOUSE_ID"

#: Task types the Jobs 2.1 run-submit API recognizes. A task carrying none of
#: them has no work to do and is rejected by the endpoint.
TASK_TYPES = (
    "sql_task", "notebook_task", "spark_python_task", "spark_jar_task",
    "spark_submit_task", "python_wheel_task", "dbt_task", "pipeline_task",
    "run_job_task", "condition_task",
)

#: Task-level keys that name compute. A sql_task instead carries its warehouse
#: inside the task body (see _check_task); pipeline/run_job/condition tasks
#: bring their own compute with the thing they invoke.
_COMPUTE_KEYS = ("existing_cluster_id", "new_cluster", "job_cluster_key")
_SELF_COMPUTED = ("pipeline_task", "run_job_task", "condition_task")

#: Studio-private keys that ride in the stored job script but are NOT part of
#: the Jobs API body. `output` is the declared S3 parquet sink the write→read
#: bridge reads back off the job after success (supervisor._bridge_output); it
#: is stripped before the body is posted so the endpoint never sees a field it
#: does not know.
STUDIO_KEYS = ("output",)

#: Databricks accepts letters, digits, underscores and hyphens in a task_key.
_TASK_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def warehouse_id():
    """The configured SQL warehouse id, or "" when unset. Read from the
    environment on every call, like every other Databricks setting."""
    return os.getenv(WAREHOUSE_VAR, "").strip()


def _task_key(name, used):
    """A stable, API-legal task_key slugged from a step name, made unique
    within the run. Two steps that slug identically (or repeat a name) must
    still get DIFFERENT keys — the Jobs API rejects a duplicate task_key, and
    silently merging two steps into one task would drop a verified statement."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_-")[:90]
    base = base or "step"
    key = base
    n = 1
    while key in used:
        n += 1
        key = f"{base}_{n}"[:100]
    used.add(key)
    return key


def build_submission(steps, run_name=None, warehouse=None):
    """Build a Jobs 2.1 run-submit body from verified SQL steps.

    `steps` is a sequence of (name, sql) pairs — the flow's verified steps.
    Each becomes a `sql_task` with a unique task_key and the warehouse that
    runs it, which is the minimum the API accepts.

    The statement rides inline under sql_task.query.query_text; a workspace
    that only runs SAVED queries would need the query registered first and its
    id substituted in the same slot. Either way `warehouse_id` is the compute
    and is mandatory — so with no warehouse configured this REFUSES (ValueError
    naming DATABRICKS_WAREHOUSE_ID) instead of returning a body that 400s.
    """
    wid = (warehouse if warehouse is not None else warehouse_id()).strip()
    if not wid:
        raise ValueError(
            f"No Databricks SQL warehouse is configured — set {WAREHOUSE_VAR} to the "
            "warehouse that should run the job's SQL tasks. Refusing to submit a "
            "Jobs run the API would reject.")

    used = set()
    tasks = []
    for name, sql in steps:
        statement = (sql or "").strip()
        if not statement:
            continue
        tasks.append({
            "task_key": _task_key(name, used),
            "sql_task": {"warehouse_id": wid, "query": {"query_text": statement}},
        })
    if not tasks:
        raise ValueError("No verified SQL steps to submit — nothing to build a job from.")

    body = {"tasks": tasks}
    if run_name:
        body["run_name"] = str(run_name)[:100]
    validate_submission(body)      # never hand out a body we would refuse to post
    return body


def _check_task(i, task, seen):
    if not isinstance(task, dict):
        raise ValueError(f"Databricks job task {i} is not an object")
    key = task.get("task_key")
    if not isinstance(key, str) or not _TASK_KEY_RE.match(key):
        raise ValueError(
            f"Databricks job task {i} has no valid task_key "
            "(letters, digits, _ or -, 1-100 chars) — the Jobs API requires one per task")
    if key in seen:
        raise ValueError(f"Databricks job task_key '{key}' is duplicated — keys must be unique")
    seen.add(key)

    types = [t for t in TASK_TYPES if t in task]
    if not types:
        raise ValueError(
            f"Databricks job task '{key}' has no recognized task type — expected one of: "
            + ", ".join(TASK_TYPES))
    if len(types) > 1:
        raise ValueError(
            f"Databricks job task '{key}' sets several task types ({', '.join(types)}) — "
            "a task runs exactly one")

    ttype = types[0]
    if ttype == "sql_task":
        body = task.get("sql_task")
        if not isinstance(body, dict) or not str(body.get("warehouse_id") or "").strip():
            raise ValueError(
                f"Databricks sql_task '{key}' has no warehouse_id — set {WAREHOUSE_VAR}")
    elif ttype not in _SELF_COMPUTED:
        if not any(task.get(k) for k in _COMPUTE_KEYS):
            raise ValueError(
                f"Databricks job task '{key}' has no compute — set one of: "
                + ", ".join(_COMPUTE_KEYS))


def validate_submission(config):
    """Raise ValueError unless `config` is a Jobs run-submit body the API can
    accept: a non-empty `tasks` list where every task has a unique task_key, a
    single recognized task type, and compute. Called before ANY post, so an
    ill-formed body never reaches the workspace (and never counts as a
    deploy attempt). Returns None; raises with a precise message."""
    if not isinstance(config, dict):
        raise ValueError("Databricks job payload must be a JSON object")
    tasks = config.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Databricks job payload has no tasks — nothing would run")
    seen = set()
    for i, task in enumerate(tasks):
        _check_task(i, task, seen)


def api_body(config):
    """The body actually posted: the submission minus Studio-private keys
    (STUDIO_KEYS). Those exist only so the supervisor can read the declared
    output back off the stored script; the Jobs API rejects fields it does not
    know, so they must not be sent."""
    return {k: v for k, v in config.items() if k not in STUDIO_KEYS}


#: Bounds for a sign-in TEST on the connect screen: give up within about a
#: minute. The driver's defaults (30 attempts over 15 minutes, a 900 s socket
#: timeout) left "connecting…" spinning on a mistyped host. The driver waits
#: at least _retry_delay_max between retries (60 s by default), which would
#: leave no retry inside a minute; 10 s gives a starting warehouse a few.
#: One that is still starting after that gets "try again shortly".
PROBE_LIMITS = {
    "_retry_stop_after_attempts_count": 6,
    "_retry_stop_after_attempts_duration": 60,
    "_retry_delay_max": 10,
    "_socket_timeout": 60,
}

# Fixed sentences for a failed sign-in, keyed on the HTTP status or the kind of
# network failure. Never built from the exception's text, which can carry the
# submitted host or token — so they are safe to show as they are.
_SAY_STATUS = {
    400: ("The workspace refused the sign-in request — check the HTTP path (it looks "
          "like /sql/1.0/warehouses/1a2b3c4d5e6f7a8b)."),
    401: "The workspace rejected the access token — it may be mistyped, expired or revoked.",
    403: ("The token signed in but may not use this SQL warehouse (it needs Can Use on it), "
          "or the workspace's IP access list blocks this server."),
    404: ("There is no SQL warehouse at that HTTP path — copy it again from the "
          "warehouse's Connection details tab."),
    429: "The workspace is busy right now — try again in a minute.",
    503: "The SQL warehouse is starting or unavailable — try again in a minute.",
}
_SAY_RESOLVE = ("That Server hostname does not resolve — copy it from the SQL warehouse's "
                "Connection details tab (it looks like dbc-1234abcd-5678.cloud.databricks.com).")
_SAY_TIMEOUT = ("No answer from the workspace within a minute. If the SQL warehouse was "
                "stopped it may still be starting — try again shortly.")
_SAY_GAVE_UP = ("The workspace did not accept the sign-in in time. If the SQL warehouse was "
                "stopped it may still be starting — try again shortly; otherwise check the "
                "Server hostname and HTTP path.")
_SAY_CONNECT = "Could not open a connection to that host on port 443."
# The driver's own fixed phrases, for when it hands over only text (its retry
# policy reports a refused status as a sentence, not a code). Whole phrases
# only: a bare "NOT_FOUND" also matched Databricks' SCHEMA_NOT_FOUND, and a
# schema that is not there was reported as a missing warehouse.
_DRIVER_SAYS = (
    ("Received 400 - BAD_REQUEST", 400), ("Received 401 - UNAUTHORIZED", 401),
    ("403 codes are not retried", 403), ("Received 404 - NOT_FOUND", 404),
)
# Server errors that name the problem once signed in: a catalog or schema that
# is not there (or not visible to this token), or a permission it lacks. Checked
# before any status code — they arrive inside otherwise ordinary errors.
_NAMESPACE = (
    ("catalog", re.compile(r"NO_SUCH_CATALOG|CATALOG_NOT_FOUND|"
                           r"catalog\s+'[^']*'\s+(?:was\s+)?not\s+found", re.I)),
    ("schema", re.compile(r"SCHEMA_NOT_FOUND|NO_SUCH_SCHEMA|NO_SUCH_DATABASE|"
                          r"database\s+'[^']*'\s+not\s+found", re.I)),
    ("permission", re.compile(r"INSUFFICIENT_PERMISSIONS|PERMISSION_DENIED|"
                              r"does\s+not\s+have\s+(?:USE|USAGE|SELECT)", re.I)),
)
_SAY_NAMESPACE = {
    "catalog": ("Signed in, but that catalog does not exist or this token cannot see it — use "
                "“Sign in and list schemas” to pick one, or leave Catalog blank for the "
                "workspace default."),
    "schema": ("Signed in, but that schema does not exist in the catalog, or this token cannot "
               "see it — use “Sign in and list schemas” to pick one."),
    "permission": ("Signed in, but this token lacks permission on that catalog or schema — it "
                   "needs USE CATALOG, USE SCHEMA and SELECT on the tables."),
}
# Which form field a failure points at, and how far the sign-in got.
_FIELD_FOR_STATUS = {400: "http_path", 401: "access_token", 404: "http_path"}
_SAY_TLS = "The secure (TLS) handshake with that host failed — is it a Databricks workspace?"


def describe_error(exc):
    """A plain sentence for why a Databricks sign-in failed, or None."""
    found = classify_error(exc)
    return found["said"] if found else None


def classify_error(exc):
    """{said, field, stage} for a failed Databricks sign-in, or None. Walks the
    exception chain (the driver wraps urllib3's errors, which wrap the socket's)
    and decides from the HTTP status, the exception TYPES and the server's
    named error classes. `said` is a fixed sentence, never built from the
    exception text; `field` is the form field to fix (or None); `stage` is
    "signed_in" when the workspace accepted the credential, else "sign_in"."""
    seen, stack, codes, kinds, texts = set(), [exc], set(), set(), []
    while stack:
        e = stack.pop()
        if isinstance(e, str):
            texts.append(e)
            continue
        if e is None or id(e) in seen or not isinstance(e, BaseException):
            continue
        seen.add(id(e))
        kinds.update(t.__name__ for t in type(e).__mro__)
        texts.append(str(e))
        ctx = getattr(e, "context", None)
        if isinstance(ctx, dict):
            code = ctx.get("http-code")
            if isinstance(code, int):
                codes.add(code)
            stack += [ctx.get("original-exception"), ctx.get("no-retry-reason")]
        stack += [e.__cause__, e.__context__, getattr(e, "reason", None)]
    text = " ".join(t for t in texts if t)

    def said(sentence, field=None, stage="sign_in"):
        return {"said": sentence, "field": field, "stage": stage}

    for what, pattern in _NAMESPACE:
        if pattern.search(text):
            return said(_SAY_NAMESPACE[what], None if what == "permission" else what,
                        "signed_in")
    codes.update(code for phrase, code in _DRIVER_SAYS if phrase in text)
    for code in (401, 403, 404, 400, 429, 503):
        if code in codes:
            return said(_SAY_STATUS[code], _FIELD_FOR_STATUS.get(code))
    if kinds & {"NameResolutionError", "gaierror"}:
        return said(_SAY_RESOLVE, "server_hostname")
    if "MaxRetryDurationError" in kinds or "max retry duration" in text:
        return said(_SAY_GAVE_UP)
    if kinds & {"ConnectTimeoutError", "ReadTimeoutError", "TimeoutError", "timeout"}:
        return said(_SAY_TIMEOUT)
    if kinds & {"SSLError", "SSLCertVerificationError"}:
        return said(_SAY_TLS)
    if kinds & {"NewConnectionError", "ConnectionRefusedError"}:
        return said(_SAY_CONNECT)
    return None


class DatabricksConnector(Connector):
    name = "databricks"
    dialect = "databricks"
    #: Extra sql.connect() settings; PROBE_LIMITS for a connect-screen test.
    connect_limits = {}

    def __init__(self):
        self._pool_conn = None
        self._pool_lock = threading.Lock()

    def _cfg(self):
        return {
            "server_hostname": os.getenv("DATABRICKS_SERVER_HOSTNAME", ""),
            "http_path": os.getenv("DATABRICKS_HTTP_PATH", ""),
            "access_token": os.getenv("DATABRICKS_TOKEN", ""),
            "catalog": os.getenv("DATABRICKS_CATALOG", ""),
            "schema": os.getenv("DATABRICKS_SCHEMA", "default"),
            "warehouse_id": warehouse_id(),
        }

    def qualifiers(self):
        """The configured catalog/schema — the only namespace RBAC describes.

        ARITY MATTERS. Unity Catalog / Spark resolve `x.sales` as SCHEMA.table
        in the CURRENT catalog and `a.b.sales` as CATALOG.SCHEMA.table, so the
        bare catalog is NOT a legal one-part qualifier: accepting it would let
        `<catalog>.sales` through as a reference to a schema merely named after
        the catalog — a namespace the catalog listing never described. Only the
        schema (one part) and catalog.schema (two parts) are declared.

        Unity Catalog gives a warehouse token visibility over many catalogs, so
        `other_catalog.default.sales` is outside what list_tables() (and hence
        the allowlist) was built from and the query guard refuses it. Env only,
        no connection: this runs per query.

        Lower-cased on purpose: Spark SQL folds identifiers case-insensitively
        even when quoted, so the guard reads databricks names bare-folded and
        the declared prefixes must match that reading.
        """
        cfg = self._cfg()
        if self.all_schemas:
            return FULL_NAMES            # every reference written, and matched, in full
        schema = (cfg["schema"] or "default").strip().lower()
        catalog = (cfg["catalog"] or "").strip().lower()
        out = {schema}
        if catalog:
            out.add(f"{catalog}.{schema}")
        return frozenset(out)

    @property
    def all_schemas(self):
        """Spans every schema (of its catalog, or of every catalog)."""
        return (self._cfg()["schema"] or "").strip() == ALL_SCHEMAS

    @property
    def full_names(self):
        """Its tables are named catalog.schema.table (skills tell the agent)."""
        return self.all_schemas

    def full_name_in_scope(self, name):
        """Whether a catalog.schema.table name lies where list_tables() looks:
        the pinned catalog when there is one, else any catalog but `system`,
        and never information_schema. Read-time checks of stored SQL use it
        in place of a live listing, so a policy of "*" still cannot release
        an answer that names a table this source would never have listed."""
        parts = str(name).lower().split(".")
        if len(parts) != 3 or not all(parts):
            return False
        catalog = (self._cfg()["catalog"] or "").strip().lower()
        if catalog and parts[0] != catalog:
            return False
        if parts[0] in {c.lower() for c in _SKIPPED_CATALOGS}:
            return False
        return parts[1] != "information_schema"

    def configured(self):
        cfg = self._cfg()
        if not (cfg["server_hostname"] and cfg["http_path"] and cfg["access_token"]):
            return False
        try:
            from databricks import sql  # noqa: F401
            return True
        except ImportError:
            return False

    def _conn(self):
        from databricks import sql
        cfg = self._cfg()
        kwargs = {
            "server_hostname": cfg["server_hostname"],
            "http_path": cfg["http_path"],
            "access_token": cfg["access_token"],
        }
        if cfg["catalog"]:
            kwargs["catalog"] = cfg["catalog"]
        if cfg["schema"] and cfg["schema"].strip() != ALL_SCHEMAS:
            kwargs["schema"] = cfg["schema"]
        kwargs.update(self.connect_limits)
        return sql.connect(**kwargs)


    # ── Connection pool (single persistent connection, reconnect on error) ─

    def _execute(self, fn):
        """Run `fn(connection)` on the pooled connection; reconnect once on
        failure (expired/killed sessions). Serialized by a lock — good enough
        for a prototype; swap for a real pool under heavy concurrency."""
        # A sign-in test does not reconnect: it would only double the wait.
        attempts = (1,) if self.connect_limits else (1, 2)
        with self._pool_lock:
            for attempt in attempts:
                if self._pool_conn is None:
                    self._pool_conn = self._conn()
                try:
                    return fn(self._pool_conn)
                except QueryCancelled:
                    raise                   # never started: the connection is fine
                except Exception:
                    token = current_cancel()
                    cancelled = token is not None and token.cancelled
                    try:
                        if self._pool_conn is not None:
                            self._pool_conn.close()
                    except Exception:
                        pass
                    self._pool_conn = None
                    # A query the gateway cancelled is over: retrying it would
                    # run the very query whose time ran out a second time.
                    if cancelled or attempt == attempts[-1]:
                        raise

    def close(self):
        with self._pool_lock:
            if self._pool_conn is not None:
                try:
                    self._pool_conn.close()
                except Exception:
                    pass
                self._pool_conn = None

    def list_tables(self):
        if self.all_schemas:
            return self._execute(self._all_tables)

        def go(con):
            cur = con.cursor()
            cur.execute("SHOW TABLES")
            # SHOW TABLES → (database, tableName, isTemporary)
            return [r[1] for r in cur.fetchall()]
        return self._execute(go)

    #: Set by an all-schemas listing that hit max_tables().
    tables_truncated = False

    def _all_tables(self, con):
        """catalog.schema.table for every table this token can see, in its
        catalog (or every catalog but `system`). One information_schema query
        where Unity Catalog has it — it lists only what the caller has
        privileges on, as SHOW TABLES does — else a SHOW walk."""
        catalog = (self._cfg()["catalog"] or "").strip()
        cap = max_tables()
        cur = con.cursor()
        if catalog and "`" in catalog:
            return []
        src = f"`{catalog}`.information_schema.tables" if catalog \
            else "system.information_schema.tables"
        skipped = ", ".join(f"'{c}'" for c in sorted(_SKIPPED_CATALOGS))
        try:
            cur.execute(
                f"SELECT table_catalog, table_schema, table_name FROM {src} "
                f"WHERE table_schema <> 'information_schema' AND table_catalog NOT IN ({skipped}) "
                f"ORDER BY table_catalog, table_schema, table_name LIMIT {cap + 1}")
            rows = [tuple(r[:3]) for r in cur.fetchall()]
        except Exception:
            rows = self._walk_tables(cur, catalog, cap)
        names = [".".join(p for p in r if p) for r in rows
                 if all(_PLAIN.match(str(p)) for p in r if p) and r[-1]]
        self.tables_truncated = len(names) > cap
        return names[:cap]

    def _walk_tables(self, cur, catalog, cap):
        """(catalog, schema, table) by SHOW statements — a workspace without
        Unity Catalog, or a token that cannot read information_schema."""
        if catalog:
            catalogs = [catalog]
        else:
            try:
                cur.execute("SHOW CATALOGS")
                catalogs = [r[0] for r in cur.fetchall()
                            if r[0] and r[0].lower() not in _SKIPPED_CATALOGS]
            except Exception:
                catalogs = [""]             # no catalogs at all: the Hive metastore
        out = []
        for c in catalogs[:self.BROWSE_CATALOG_LIMIT]:
            if "`" in c:
                continue
            try:
                cur.execute(f"SHOW SCHEMAS IN `{c}`" if c else "SHOW SCHEMAS")
                schemas = [r[0] for r in cur.fetchall()
                           if r[0] and r[0].lower() != "information_schema" and "`" not in r[0]]
            except Exception:
                continue
            for sch in schemas:
                where = f"`{c}`.`{sch}`" if c else f"`{sch}`"
                try:
                    cur.execute(f"SHOW TABLES IN {where}")
                except Exception:
                    continue
                out += [(c, sch, r[1]) for r in cur.fetchall()]
                if len(out) > cap:
                    return out
        return out

    #: Cap on catalogs walked by list_namespaces — one round trip each, and a
    #: metastore with hundreds of catalogs must not hang the connect screen.
    BROWSE_CATALOG_LIMIT = 25

    def list_namespaces(self):
        """Every catalog.schema pair this token can see, for the connect
        screen's picker.

        SHOW CATALOGS fails on a workspace without Unity Catalog, which is a
        normal configuration, not an error — fall back to the schemas of the
        current catalog. Catalog names come back from the server and go back in
        as an identifier, so they are backtick-quoted and any name carrying a
        backtick is skipped rather than escaped: there is no legitimate catalog
        that needs one, and quoting it wrongly would be an injection."""
        current = self._cfg()["catalog"]
        self.namespaces_truncated = False

        def go(con):
            cur = con.cursor()
            try:
                cur.execute("SHOW CATALOGS")
                catalogs = [r[0] for r in cur.fetchall() if r[0]]
            except Exception:
                catalogs = [current] if current else [""]
            self.namespaces_truncated = len(catalogs) > self.BROWSE_CATALOG_LIMIT
            out = []
            for catalog in catalogs[:self.BROWSE_CATALOG_LIMIT]:
                if "`" in catalog:
                    continue
                try:
                    cur.execute(f"SHOW SCHEMAS IN `{catalog}`" if catalog else "SHOW SCHEMAS")
                except Exception:
                    continue
                # SHOW SCHEMAS → (databaseName,) on Spark, (namespace,) on UC.
                schemas = [r[0] for r in cur.fetchall()
                           if r[0] and r[0].lower() != "information_schema"]
                counts = self._table_counts(cur, catalog)
                listed = []
                for schema in schemas:
                    item = {"database": catalog, "schema": schema}
                    if counts is not None:
                        item["tables"] = counts.get(schema.lower(), 0)
                    listed.append(item)
                if catalog and catalog.lower() not in _SKIPPED_CATALOGS and len(listed) > 1:
                    # The whole catalog, as one source (with one schema, that
                    # schema already is the whole catalog).
                    every = {"database": catalog, "schema": ALL_SCHEMAS}
                    if counts is not None:
                        every["tables"] = sum(i["tables"] for i in listed)
                    out.append(every)
                out += listed
            if len([c for c in catalogs if c and c.lower() not in _SKIPPED_CATALOGS]) > 1:
                # Everything the token can see, as one source.
                schemas_seen = [i for i in out if i["schema"] != ALL_SCHEMAS
                                and (i["database"] or "").lower() not in _SKIPPED_CATALOGS]
                every = {"database": "", "schema": ALL_SCHEMAS}
                if schemas_seen and all("tables" in i for i in schemas_seen):
                    every["tables"] = sum(i["tables"] for i in schemas_seen)
                out.insert(0, every)
            return out

        return self._execute(go)

    @staticmethod
    def _table_counts(cur, catalog):
        """{schema: tables this token can see} for one Unity Catalog catalog —
        one query, so the picker can tell an empty schema from a full one before
        anything is saved. information_schema lists only what the caller has
        privileges on, as SHOW TABLES does. None when unknown: the Hive
        metastore has no information_schema, and a failure here must not cost
        the listing itself."""
        if not catalog:
            return None
        try:
            cur.execute(f"SELECT table_schema, COUNT(*) FROM `{catalog}`.information_schema.tables "
                        "WHERE table_schema <> 'information_schema' GROUP BY table_schema")
            return {str(r[0]).lower(): int(r[1]) for r in cur.fetchall() if r[0] is not None}
        except Exception:
            return None

    def get_schema(self, table):
        if self.all_schemas:
            parts = str(table).split(".")
            if not all(_PLAIN.match(p) for p in parts):
                raise ValueError("Invalid table name")
            table = ".".join(f"`{p}`" for p in parts)

        def go(con):
            cur = con.cursor()
            cur.execute(f"DESCRIBE TABLE {table}")
            out = []
            for r in cur.fetchall():
                col, dtype = r[0], r[1]
                if not col or col.startswith("#"):
                    break  # partition/metadata section
                out.append({"name": col, "type": dtype})
            return out
        return self._execute(go)

    #: information_schema.tables.table_type spellings, by kind.
    _TABLE_TYPES = {"MANAGED", "EXTERNAL", "BASE TABLE", "STREAMING_TABLE", "FOREIGN"}
    _VIEW_TYPES = {"VIEW", "MATERIALIZED_VIEW", "MATERIALIZED VIEW", "METRIC_VIEW"}

    def describe_relations(self):
        """Kind and columns for every relation in this source's scope, from
        information_schema in two queries rather than a DESCRIBE per table.
        Keyed as list_tables() names them: bare names when pinned to a schema
        (current_catalog()/current_schema() are the pinned ones, so nothing is
        interpolated), catalog.schema.table when the source spans schemas.
        None where information_schema is unavailable (the Hive metastore) —
        discovery then DESCRIBEs table by table."""
        catalog = (self._cfg()["catalog"] or "").strip()
        if catalog and not _PLAIN.match(catalog):
            return None
        if self.all_schemas:
            prefix = f"`{catalog}`.information_schema" if catalog else "system.information_schema"
            skipped = ", ".join(f"'{c}'" for c in sorted(_SKIPPED_CATALOGS))
            where = (f"WHERE table_schema <> 'information_schema' "
                     f"AND table_catalog NOT IN ({skipped})")

            def key(cat, sch, name):
                return f"{cat}.{sch}.{name}"
        else:
            prefix = "information_schema"
            where = "WHERE table_catalog = current_catalog() AND table_schema = current_schema()"

            def key(cat, sch, name):
                return name

        def go(con):
            cur = con.cursor()
            out = {}
            for cat, sch, name, ttype, note in _with_comments(
                    cur, f"SELECT table_catalog, table_schema, table_name, table_type{{c}} "
                         f"FROM {prefix}.tables {where}"):
                kind = str(ttype or "").upper()
                kind = ("view" if kind in self._VIEW_TYPES
                        else "table" if kind in self._TABLE_TYPES else "other")
                out[key(cat, sch, name)] = {"kind": kind, "namespace": f"{cat}.{sch}",
                                            "columns": [],
                                            **({"description": note} if note else {})}
            for type_col in ("full_data_type", "data_type"):
                try:
                    rows = _with_comments(
                        cur, f"SELECT table_catalog, table_schema, table_name, column_name, "
                             f"{type_col}{{c}} FROM {prefix}.columns {where} "
                             f"ORDER BY table_catalog, table_schema, table_name, "
                             f"ordinal_position")
                    break
                except Exception:
                    if type_col == "data_type":
                        raise
            for cat, sch, name, col, ctype, note in rows:
                entry = out.get(key(cat, sch, name))
                if entry is not None:
                    entry["columns"].append({"name": col, "type": ctype,
                                             **({"description": note} if note else {})})
            return out
        try:
            return self._execute(go)
        except Exception:
            return None

    def describe_relation(self, name):
        """describe_relations() for one relation: information_schema filtered
        to it, the name checked as plain identifiers before it is quoted. None
        where information_schema is unavailable or the name is not plain."""
        scope = self._info_scope()
        if scope is None:
            return None
        prefix, where, _key = scope
        parts = str(name).split(".")
        if len(parts) != (3 if self.all_schemas else 1) \
                or not all(_PLAIN.match(p) for p in parts):
            return None
        if self.all_schemas:
            where += (f" AND table_catalog = '{parts[0]}' AND table_schema = '{parts[1]}' "
                      f"AND table_name = '{parts[2]}'")
        else:
            where += f" AND table_name = '{parts[0]}'"

        def go(con):
            cur = con.cursor()
            found = _with_comments(cur, f"SELECT table_catalog, table_schema, table_type{{c}} "
                                        f"FROM {prefix}.tables WHERE {where}")
            if not found:
                return None
            row = found[0]
            kind = str(row[2] or "").upper()
            kind = ("view" if kind in self._VIEW_TYPES
                    else "table" if kind in self._TABLE_TYPES else "other")
            for type_col in ("full_data_type", "data_type"):
                try:
                    rows = _with_comments(cur, f"SELECT column_name, {type_col}{{c}} FROM "
                                               f"{prefix}.columns WHERE {where} "
                                               f"ORDER BY ordinal_position")
                    break
                except Exception:
                    if type_col == "data_type":
                        raise
            out = {"kind": kind, "namespace": f"{row[0]}.{row[1]}",
                   "columns": [{"name": r[0], "type": r[1],
                                **({"description": r[2]} if r[2] else {})} for r in rows]}
            if row[3]:
                out["description"] = row[3]
            return out
        return self._execute(go)

    def _info_scope(self):
        """(information_schema prefix, table filter, name of a relation from its
        catalog/schema/table) for this source's scope — the same scoping as
        describe_relations()."""
        catalog = (self._cfg()["catalog"] or "").strip()
        if catalog and not _PLAIN.match(catalog):
            return None
        if self.all_schemas:
            prefix = f"`{catalog}`.information_schema" if catalog else "system.information_schema"
            skipped = ", ".join(f"'{c}'" for c in sorted(_SKIPPED_CATALOGS))
            return (prefix, f"table_schema <> 'information_schema' "
                            f"AND table_catalog NOT IN ({skipped})",
                    lambda cat, sch, name: f"{cat}.{sch}.{name}")
        return ("information_schema",
                "table_catalog = current_catalog() AND table_schema = current_schema()",
                lambda cat, sch, name: name)

    def describe_constraints(self):
        """Unity Catalog primary and foreign keys (informational constraints)
        from information_schema. A foreign key whose referenced table lies
        outside this source's scope is left out."""
        scope = self._info_scope()
        if scope is None:
            return None
        prefix, where, key = scope

        def go(con):
            cur = con.cursor()
            cur.execute(f"SELECT constraint_name, constraint_type, table_catalog, table_schema, "
                        f"table_name FROM {prefix}.table_constraints WHERE {where} "
                        f"AND constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY')")
            constraints = {r[0]: (r[1], key(r[2], r[3], r[4])) for r in cur.fetchall()}
            cur.execute(f"SELECT constraint_name, column_name, ordinal_position "
                        f"FROM {prefix}.key_column_usage WHERE {where} "
                        f"ORDER BY constraint_name, ordinal_position")
            columns = {}
            for name, col, _pos in cur.fetchall():
                columns.setdefault(name, []).append(col)
            cur.execute(f"SELECT constraint_name, unique_constraint_name "
                        f"FROM {prefix}.referential_constraints")
            refs = dict(cur.fetchall())
            out = {"primary_keys": {}, "foreign_keys": []}
            for name, (ctype, table) in constraints.items():
                if ctype == "PRIMARY KEY" and columns.get(name):
                    out["primary_keys"][table] = columns[name]
            for name, (ctype, table) in constraints.items():
                target = refs.get(name)
                if ctype != "FOREIGN KEY" or target not in constraints:
                    continue
                _t, to_table = constraints[target]
                if columns.get(name) and columns.get(target):
                    out["foreign_keys"].append({
                        "name": name, "from": table, "from_columns": columns[name],
                        "to": to_table, "to_columns": columns[target]})
            return out
        try:
            return self._execute(go)
        except Exception:
            logging.getLogger("studio.connectors").warning(
                "Databricks describe_constraints failed", exc_info=True)
            return None

    def view_definitions(self):
        scope = self._info_scope()
        if scope is None:
            return None
        prefix, where, key = scope

        def go(con):
            cur = con.cursor()
            cur.execute(f"SELECT table_catalog, table_schema, table_name, view_definition "
                        f"FROM {prefix}.views WHERE {where}")
            return {key(cat, sch, name): sql for cat, sch, name, sql in cur.fetchall() if sql}
        try:
            return self._execute(go)
        except Exception:
            return None

    def lineage(self):
        """Table lineage Unity Catalog recorded (system.access.table_lineage) for
        the last 90 days, between tables in this source's scope. None when the
        system tables are not readable with this token."""
        scope = self._info_scope()
        if scope is None:
            return None
        _prefix, _where, key = scope
        catalog = (self._cfg()["catalog"] or "").strip()
        if self.all_schemas:
            filt = (f"target_table_catalog = '{catalog}'" if catalog
                    else "target_table_catalog <> 'system'")
        else:
            filt = ("target_table_catalog = current_catalog() "
                    "AND target_table_schema = current_schema() "
                    "AND source_table_catalog = current_catalog() "
                    "AND source_table_schema = current_schema()")

        def go(con):
            cur = con.cursor()
            cur.execute("SELECT DISTINCT source_table_catalog, source_table_schema, "
                        "source_table_name, target_table_catalog, target_table_schema, "
                        "target_table_name FROM system.access.table_lineage "
                        f"WHERE {filt} AND source_table_name IS NOT NULL "
                        "AND event_time > current_timestamp() - INTERVAL 90 DAYS LIMIT 5000")
            return [(key(*r[:3]), key(*r[3:])) for r in cur.fetchall()
                    if all(r) and r[:3] != r[3:]]
        try:
            return self._execute(go)
        except Exception:
            return None

    def table_signals(self):
        """{relation: "type|last_altered"} per relation in scope, from
        information_schema.tables in one query, named as list_tables() names
        them. None where last_altered is not available."""
        scope = self._info_scope()
        if scope is None:
            return None
        prefix, where, key = scope

        def go(con):
            cur = con.cursor()
            cur.execute(f"SELECT table_catalog, table_schema, table_name, table_type, "
                        f"last_altered FROM {prefix}.tables WHERE {where}")
            return {key(cat, sch, name): f"{ttype}|{altered}"
                    for cat, sch, name, ttype, altered in cur.fetchall()}
        try:
            return self._execute(go)
        except Exception:
            logging.getLogger("studio.connectors").warning(
                "Databricks table_signals failed; falling back to the source signal",
                exc_info=True)
            return None

    def change_signal(self):
        """Count and newest last_altered of the relations in scope: a create,
        drop, rename or ALTER moves one of them. None where information_schema
        (or last_altered) is unavailable."""
        catalog = (self._cfg()["catalog"] or "").strip()
        if catalog and not _PLAIN.match(catalog):
            return None
        if self.all_schemas:
            prefix = f"`{catalog}`.information_schema" if catalog else "system.information_schema"
            skipped = ", ".join(f"'{c}'" for c in sorted(_SKIPPED_CATALOGS))
            where = (f"WHERE table_schema <> 'information_schema' "
                     f"AND table_catalog NOT IN ({skipped})")
        else:
            prefix = "information_schema"
            where = "WHERE table_catalog = current_catalog() AND table_schema = current_schema()"

        def go(con):
            cur = con.cursor()
            cur.execute(f"SELECT COUNT(*), MAX(last_altered) FROM {prefix}.tables {where}")
            row = cur.fetchone()
            return f"{row[0]}|{row[1]}"
        try:
            return self._execute(go)
        except Exception:
            # No signal means "rediscover on the full-sync cadence" — say why,
            # or a broken query would look like an engine without a signal.
            logging.getLogger("studio.connectors").warning(
                "Databricks change_signal failed; falling back to the full-sync cadence",
                exc_info=True)
            return None

    def relation_kind(self, namespace, table):
        """Classify an output relation from metadata without reading its data.

        ``SHOW TABLES`` includes views, so its presence bit is not sufficient
        for deciding whether an ``INSERT SELECT`` destination is writable.
        Information schema exposes the relation type and lets this gate fail
        closed for views and every newer/unknown Databricks relation kind.
        """
        identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,127}")
        if not isinstance(namespace, str) or not identifier.fullmatch(namespace) \
                or not isinstance(table, str) or not identifier.fullmatch(table):
            raise ValueError("Invalid relation identity")

        def go(con):
            cur = con.cursor()
            cur.execute(
                "SELECT table_type FROM information_schema.tables "
                "WHERE table_schema = ? AND table_name = ? LIMIT 1",
                (namespace, table),
            )
            row = cur.fetchone()
            if row is None:
                return "missing"
            # Unity Catalog reports MANAGED/EXTERNAL, while some Spark SQL
            # deployments expose the standard BASE TABLE spelling.
            return "table" if str(row[0]).upper() in {
                "BASE TABLE", "MANAGED", "EXTERNAL",
            } else "other"
        return self._execute(go)

    def run_query(self, sql_text):
        def go(con):
            cur = con.cursor()
            token = current_cancel()
            if token is not None:
                # The driver's documented cancel: safe from another thread,
                # it stops the statement on the warehouse, not just here.
                token.attach(cur.cancel)
            cur.execute(sql_text)
            columns = [d[0] for d in cur.description]
            # Databricks returns date/datetime/Decimal — coerce to JSON-safe so
            # results survive the tile cache and the API response.
            rows = jsonify_rows(cur.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000"))))
            return columns, rows
        return self._execute(go)

    def run_script(self, sql_text):
        """Execute a write/DDL statement (supervisor + human-approved only)."""
        def go(con):
            cur = con.cursor()
            cur.execute(sql_text)
            return {"rowcount": getattr(cur, "rowcount", None)}
        return self._execute(go)

    def submit_spark_job(self, config):
        """Submit a Databricks Jobs run (Spark). `config` is a Jobs 2.1
        run-submit body; returns {run_id}. Requires DATABRICKS_TOKEN + host.

        The body is VALIDATED before anything is sent (validate_submission):
        a payload missing task_keys, a task type or compute is rejected by the
        API anyway, and refusing it here means a malformed submission never
        counts as a deploy attempt and never reaches the workspace. Studio's
        private keys (the declared output sink) are stripped — the API rejects
        fields it does not know."""
        import json
        import urllib.request

        validate_submission(config)          # nothing is posted on an invalid body
        cfg = self._cfg()
        host = cfg["server_hostname"]
        url = f"https://{host}/api/2.1/jobs/runs/submit"
        body = json.dumps(api_body(config)).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {cfg['access_token']}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
