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
import os
import time

from . import cypherguard, db, governance, limits, queryguard, rbac, sources
from .connectors.base import gateway_scope
from .queryguard import QueryRejected


class QueryTimeout(Exception):
    """Execution exceeded STUDIO_QUERY_TIMEOUT_S. The connector call keeps
    running on its worker thread (Python cannot kill it); the caller gets a
    clear error instead of a hung request."""


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
    allowed = rbac.allowed_tables(_role(user), source, connector.list_tables())
    if not allowed:
        raise QueryRejected("Your role has no access to this source")
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


def check(user, source, sql, *, table_label="*", max_rows=None):
    """Steps (a)-(e): scope() plus the query guard and LIMIT injection, with no
    execution and no audit row — for pin-time / validate-only callers such as
    dashboards and pipelines. Returns (connector, allowed_tables, cleaned_sql);
    cleaned_sql is what execute() would run.

    The guard is handed the connector's own namespace (Connector.qualifiers()):
    the allowlist is keyed on BARE table names, so without it `secret_schema.
    sales` rode in on an entry for `sales` and reached a schema the catalog
    never described."""
    connector, allowed = scope(user, source, table_label=table_label)
    guard = _guard(connector)
    cleaned = guard.validate(sql, allowed, qualifiers=connector.qualifiers())
    cleaned = guard.enforce_limit(cleaned, _cap(max_rows))
    return connector, allowed, cleaned


# ── Execution ───────────────────────────────────────────────────────────

def _timeout_s():
    try:
        return float(os.getenv("STUDIO_QUERY_TIMEOUT_S", "0") or 0)
    except ValueError:
        return 0.0


def _run(connector, sql, purpose):
    """Call run_query inside the gateway scope, on a single-use worker thread
    when a wall-clock timeout is configured. The scope ContextVar is set INSIDE
    the worker: ContextVars do not cross thread boundaries, so setting it here
    and calling on another thread would trip the connector's guard."""
    def go():
        with gateway_scope(purpose):
            return connector.run_query(sql)

    timeout = _timeout_s()
    if timeout <= 0:
        return go()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(go)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise QueryTimeout(
                f"Query on {connector.name} exceeded {timeout:g}s and was abandoned")
    finally:
        pool.shutdown(wait=False)      # never join a runaway query


def _audit(user, purpose, source, table_label, sql, *, row_count=None,
           duration_ms=None, ok=True, error=None):
    db.log_activity(user, purpose, source=source, table=table_label, sql=sql,
                    row_count=row_count, ok=ok, error=error, duration_ms=duration_ms)


def execute(user, source, sql, purpose, *, table_label="*", max_rows=None, audit=True):
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
            user, source, sql, table_label=table_label, max_rows=max_rows)
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
