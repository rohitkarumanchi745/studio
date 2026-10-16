"""The one data gate: every row-returning path calls gateway.execute.

Invariant: RBAC → query guard → row limit → execution → governance → audit,
in that order, and nowhere else. Before this module each caller (agent,
chat, dashboards, catalog, freshness, cache replay, supervisor reads, verify)
re-implemented some subset of those steps, and every RBAC/governance bypass we
found was a call site that skipped or reordered one. The gateway is the only
code that may execute a connector's run_query — connectors/base.py enforces
that at runtime (run_query raises outside gateway_scope()) and
tests/test_gateway.py enforces it statically (no `.run_query(` under app/
outside this file).

The gateway never accepts a caller-supplied allowlist. It always derives the
permitted tables itself from rbac.allowed_tables(role, source,
connector.list_tables()): an allowlist passed in by the caller is exactly the
thing a buggy or confused caller gets wrong (a stale list, another user's
list, the source's full list), and the guard is only as strong as the list it
checks against. Callers pass WHO (user), WHERE (source), WHAT (sql) and WHY
(purpose — the audit_log action); the gateway decides what they may see.

The GUARD is dialect-aware; the PATH is not. neo4j speaks Cypher, where the
read shape is `MATCH ... RETURN` and SELECT does not exist, so _guard() picks
app/cypherguard.py for connector.dialect == "cypher" and app/queryguard.py for
everything else. Both return the cleaned text to execute and raise the same
QueryRejected, so RBAC, the row cap, governance and audit are untouched by the
dialect — a second execution path is exactly what this module exists to
prevent.

Three entry points, all applying the same steps in the same order:
  execute(user, source, sql, purpose, ...)  steps (a)-(i): returns rows, audits
  check(user, source, sql, ...)             steps (a)-(e): validate only, no audit
  scope(user, source, ...)                  steps (a)-(d): connector + allowed tables
"""
import concurrent.futures
import contextvars
import threading
import os
import time

from . import cypherguard, db, governance, limits, queryguard, rbac, sources
from .connectors.base import CancelToken, cancel_scope, gateway_scope
from .queryguard import QueryRejected


class QueryTimeout(Exception):
    """Execution exceeded its wall-clock limit (see _timeout_s). The query is
    cancelled when its connector supports it; otherwise the driver call keeps
    running on its worker thread (Python cannot kill it). Either way the
    caller gets a clear error instead of a hung request."""


class QueryBusy(Exception):
    """The source already has its maximum of queries in flight (see _slots)."""


class QueryResult:
    """What execute() returns. `sql` is the cleaned SQL that actually ran (the
    comment-free, LIMIT-bearing text) — callers that persist SQL store this,
    never their input. `row_count` is len(rows) AFTER governance capping."""

    __slots__ = ("columns", "rows", "sql", "row_count", "took_ms", "source", "purpose")

    def __init__(self, columns, rows, sql, took_ms, source, purpose):
        self.columns = columns
        self.rows = rows
        self.sql = sql
        self.row_count = len(rows)
        self.took_ms = took_ms
        self.source = source
        self.purpose = purpose

    def as_dict(self):
        return {"columns": self.columns, "rows": self.rows, "sql": self.sql,
                "row_count": self.row_count, "took_ms": self.took_ms,
                "source": self.source, "purpose": self.purpose}


# ── Steps (a)-(e) ───────────────────────────────────────────────────────

def _role(user):
    return (user or {}).get("role")


def scope(user, source, *, table_label="*"):
    """Steps (a)-(d): RBAC on the source, resolve the connector, list its
    tables, filter to what the role may see. Returns (connector, allowed).
    Raises QueryRejected (no access) or HTTPException (unknown/unconfigured
    source); connector errors from list_tables propagate unwrapped."""
    if not rbac.can_access(_role(user), source, table_label or "*"):
        raise QueryRejected(f"Your role has no access to {source}")
    connector = sources.connector_or_400(source)
    tables = connector.list_tables()
    allowed = rbac.allowed_tables(_role(user), source, tables)
    if not allowed:
        raise QueryRejected(rbac.nothing_to_read(source, tables))
    return connector, allowed


def _cap(max_rows):
    """The row ceiling for a call: a caller may lower MAX_ROWS, never raise it."""
    return min(max_rows or limits.MAX_ROWS, limits.MAX_ROWS)


def _guard(connector):
    """The read-only validator for this connector's DIALECT.

    One gate, more than one language. The gateway's guard was SQL-only, so the
    neo4j source (dialect "cypher") was unusable: a valid `MATCH (n:Person)
    RETURN n` was refused for not being a SELECT. The fork is here, in the
    guard — never a second execution path — so RBAC, the row cap, governance
    and the audit row keep running in the same order for every source. Both
    modules expose validate(text, allowed, qualifiers=...) and
    enforce_limit(text, max_rows), raise the same QueryRejected, and return the
    comment-free text the caller must execute."""
    return cypherguard if getattr(connector, "dialect", "") == "cypher" else queryguard


def check(user, source, sql, *, table_label="*", max_rows=None, allow_explain=False):
    """Steps (a)-(e): scope() plus the query guard and LIMIT injection, with no
    execution and no audit row — for pin-time / validate-only callers such as
    dashboards and pipelines. Returns (connector, allowed_tables, cleaned_sql);
    cleaned_sql is what execute() would run.

    The guard is handed the connector's own namespace (Connector.qualifiers())
    and its DIALECT: the allowlist is keyed on BARE table names, so without the
    namespace `secret_schema.sales` rode in on an entry for `sales` and reached
    a schema the catalog never described, and without the dialect a quoted
    `"CUSTOMERS"` CTE stood in for the denied base table `customers`."""
    connector, allowed = scope(user, source, table_label=table_label)
    guard = _guard(connector)
    kw = {"qualifiers": connector.qualifiers()}
    if guard is queryguard:
        # WHICH ENGINE will read this text decides what an identifier MEANS:
        # `"CUSTOMERS"` is a different table from `customers` on Postgres and
        # Snowflake, and the guard cannot judge a CTE or an allowlist entry
        # without knowing that. Cypher has no such folding question, and
        # cypherguard.validate takes no dialect, so only the SQL guard is told.
        kw["dialect"] = getattr(connector, "dialect", None)
        # Opt-in EXPLAIN: a planning call, never an execution. The guard still
        # validates the inner SELECT (tables → RBAC, forbidden keywords, single
        # statement) and refuses EXPLAIN ANALYZE, so the plan request crosses the
        # SAME gate as the read it describes.
        if allow_explain:
            kw["allow_explain"] = True
    cleaned = guard.validate(sql, allowed, **kw)
    cleaned = guard.enforce_limit(cleaned, _cap(max_rows))
    return connector, allowed, cleaned


# ── Execution ───────────────────────────────────────────────────────────

# A person is waiting on an interactive read: give it a limit by default.
INTERACTIVE_TIMEOUT_S = 300.0
# Reads inside an approved supervised job are not interactive and may be long.
_UNBOUNDED_BY_DEFAULT = frozenset({"supervised_read"})


def _timeout_s(purpose=None):
    """Seconds a read may run. STUDIO_QUERY_TIMEOUT_S, when set, is the
    operator's choice for every read (0 = none); unset, interactive reads get
    INTERACTIVE_TIMEOUT_S and supervised-job reads none."""
    raw = os.getenv("STUDIO_QUERY_TIMEOUT_S", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return 0.0 if purpose in _UNBOUNDED_BY_DEFAULT else INTERACTIVE_TIMEOUT_S


def _int_env(name, default):
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


# Per-source concurrency: at most STUDIO_QUERY_CONCURRENCY reads in flight on
# one source in this process. A slot is held until the warehouse call really
# returns — not when its caller gives up — so queries abandoned on a source
# that cannot cancel still count, and cannot pile up behind a runaway one.
_slots = {}
_slots_lock = threading.Lock()


def _slot(source):
    with _slots_lock:
        size = _int_env("STUDIO_QUERY_CONCURRENCY", 8)
        sem = _slots.get(source)
        if sem is None or sem[0] != size:
            sem = _slots[source] = (size, threading.BoundedSemaphore(size))
        return sem[1]


def _run(connector, sql, purpose):
    """Call run_query inside the gateway scope: in a free concurrency slot, on
    a single-use worker thread when the read has a time limit, and cancelled
    — not just abandoned — when the limit passes and the connector supports
    it. The worker runs in a copy of the caller's context; the scope and the
    cancel token are set inside it, since ContextVars do not cross threads."""
    timeout = _timeout_s(purpose)
    slot = _slot(connector.name)
    try:
        wait = max(0.0, float(os.getenv("STUDIO_QUERY_QUEUE_S", "") or 60))
    except ValueError:
        wait = 60.0
    if not slot.acquire(timeout=min(wait, timeout) if timeout > 0 else wait):
        raise QueryBusy(f"{connector.name} is busy: {_int_env('STUDIO_QUERY_CONCURRENCY', 8)} "
                        "queries are already running there. Try again in a moment.")
    token = CancelToken()

    def go():
        try:
            with gateway_scope(purpose), cancel_scope(token):
                if token.cancelled:
                    raise QueryTimeout(f"Query on {connector.name} was cancelled before it started")
                return connector.run_query(sql)
        finally:
            slot.release()             # when the warehouse call really ends

    if timeout <= 0:
        return go()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        try:
            fut = pool.submit(contextvars.copy_context().run, go)
        except BaseException:
            slot.release()
            raise
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            if token.cancel():
                raise QueryTimeout(f"Query on {connector.name} exceeded {timeout:g}s and was "
                                   "cancelled")
            raise QueryTimeout(f"Query on {connector.name} exceeded {timeout:g}s and was "
                               "abandoned (this source cannot cancel a running query)")
    finally:
        pool.shutdown(wait=False)      # never join a runaway query


def _audit(user, purpose, source, table_label, sql, *, row_count=None,
           duration_ms=None, ok=True, error=None):
    db.log_activity(user, purpose, source=source, table=table_label, sql=sql,
                    row_count=row_count, ok=ok, error=error, duration_ms=duration_ms)


def execute(user, source, sql, purpose, *, table_label="*", max_rows=None, audit=True,
            allow_explain=False):
    """Steps (a)-(i). `purpose` is a short snake_case name for the caller
    (agent_sql, rerun, dashboard_tile, ...) and becomes the audit_log action.

    Raises QueryRejected (RBAC / guard), HTTPException (unknown or unconfigured
    source), QueryTimeout, or whatever the connector raised — after writing an
    ok=False audit row when audit=True. A successful read is audited before it
    is returned; if the audit write itself fails the rows do not leave the
    gateway (an unrecorded read is not a governed read)."""
    t0 = time.time()
    cleaned = None
    try:
        connector, _allowed, cleaned = check(
            user, source, sql, table_label=table_label, max_rows=max_rows,
            allow_explain=allow_explain)
        columns, rows = _run(connector, cleaned, purpose)
        rows = list(rows)[:_cap(max_rows)]
        columns, rows = governance.filter_result(source, cleaned, columns, rows)
    except Exception as e:
        if audit:
            try:
                _audit(user, purpose, source, table_label, cleaned or sql,
                       duration_ms=int((time.time() - t0) * 1000),
                       ok=False, error=str(e)[:300])
            except Exception:
                pass                    # never mask the real failure
        raise
    took_ms = int((time.time() - t0) * 1000)
    if audit:
        _audit(user, purpose, source, table_label, cleaned,
               row_count=len(rows), duration_ms=took_ms, ok=True)
    return QueryResult(columns, rows, cleaned, took_ms, source, purpose)
