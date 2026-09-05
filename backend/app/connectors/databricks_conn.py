"""Databricks SQL warehouse connector. Configure via env (see .env.example);
requires `pip install databricks-sql-connector` (commented in requirements.txt).

This module also owns the shape of a Databricks **Jobs run-submit body**
(`build_submission` / `validate_submission`), because the Jobs API is far
pickier than "a dict with tasks in it": every task needs a `task_key`, exactly
one recognized task type, and compute to run on. Building and checking that
body here — next to the credentials it needs — keeps flow.py from hand-rolling
a payload the endpoint would reject with a 400.
"""
import os
import re
import threading

from .base import Connector, jsonify_rows

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


class DatabricksConnector(Connector):
    name = "databricks"
    dialect = "databricks"

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
        schema = (cfg["schema"] or "default").strip().lower()
        catalog = (cfg["catalog"] or "").strip().lower()
        out = {schema}
        if catalog:
            out.add(f"{catalog}.{schema}")
        return frozenset(out)

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
        if cfg["schema"]:
            kwargs["schema"] = cfg["schema"]
        return sql.connect(**kwargs)


    # ── Connection pool (single persistent connection, reconnect on error) ─

    def _execute(self, fn):
        """Run `fn(connection)` on the pooled connection; reconnect once on
        failure (expired/killed sessions). Serialized by a lock — good enough
        for a prototype; swap for a real pool under heavy concurrency."""
        with self._pool_lock:
            for attempt in (1, 2):
                if self._pool_conn is None:
                    self._pool_conn = self._conn()
                try:
                    return fn(self._pool_conn)
                except Exception:
                    try:
                        self._pool_conn.close()
                    except Exception:
                        pass
                    self._pool_conn = None
                    if attempt == 2:
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
        def go(con):
            cur = con.cursor()
            cur.execute("SHOW TABLES")
            # SHOW TABLES → (database, tableName, isTemporary)
            return [r[1] for r in cur.fetchall()]
        return self._execute(go)

    def get_schema(self, table):
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

    def run_query(self, sql_text):
        def go(con):
            cur = con.cursor()
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
