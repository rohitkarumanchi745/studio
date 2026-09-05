"""Connector interface every data source implements.

Raw execution is unreachable from outside the gateway: `run_query` on every
connector subclass is wrapped at class-creation time and refuses to run unless
the calling context is inside gateway_scope() (set by app/gateway.py, the one
place where RBAC, the query guard, row limits, governance and audit are
applied in order) or inside unguarded() (tests and connector self-tests only).
A forgotten check in a new call site therefore fails loudly at runtime instead
of silently returning ungoverned rows; tests/test_gateway.py adds the static
half of the invariant (no `.run_query(` outside the gateway), and pins
LEGACY_CALL_SITES — the now-empty set of pre-gateway call sites — to the
offenders it finds, so a new direct call cannot be exempted by name.
"""
import contextlib
import contextvars
import datetime
import decimal
import functools

# The execution scope: None outside the gateway, the caller's purpose inside
# it, "unguarded" under the explicit escape hatch. A ContextVar (not a global)
# so a gateway call on one thread never unlocks a stray call on another.
_GATEWAY_SCOPE = contextvars.ContextVar("studio_gateway_scope", default=None)

_UNREACHABLE = ("Connector.run_query is only reachable through gateway.execute "
                "— see app/gateway.py")


@contextlib.contextmanager
def gateway_scope(purpose):
    """Mark the current context as executing on behalf of the gateway. Only
    app/gateway.py should enter this; grep for it stays that short."""
    token = _GATEWAY_SCOPE.set(purpose or "gateway")
    try:
        yield
    finally:
        _GATEWAY_SCOPE.reset(token)


@contextlib.contextmanager
def unguarded():
    """Escape hatch for tests and connector self-tests ONLY: run_query works
    without the gateway inside this block. Deliberately greppable — the static
    invariant test fails if it appears anywhere under app/ except this file."""
    token = _GATEWAY_SCOPE.set("unguarded")
    try:
        yield
    finally:
        _GATEWAY_SCOPE.reset(token)


def current_scope():
    """The active scope label (purpose / "unguarded"), or None."""
    return _GATEWAY_SCOPE.get()


# Pre-gateway call sites were exempted here by (app module, function) while
# they were migrated onto gateway.execute. The migration is complete: the set
# is EMPTY and tests/test_gateway.py pins it to the static offenders (none), so
# a new direct call site cannot be exempted here without failing both tests.
LEGACY_CALL_SITES = frozenset()


def _guard_run_query(fn):
    @functools.wraps(fn)
    def guarded(self, *a, **kw):
        if _GATEWAY_SCOPE.get() is None:
            raise RuntimeError(_UNREACHABLE)
        return fn(self, *a, **kw)
    guarded.__wrapped_run_query__ = fn
    return guarded


def to_jsonable(v):
    """Coerce a warehouse cell value to something JSON-serializable, so results
    survive the tile cache (JSON round-trip) and the API response. Real
    warehouses (Databricks / Snowflake) return `date`/`datetime`/`Decimal`/
    `bytes` that FastAPI's encoder rejects; SQLite already returns str/num.
    Decimals become numbers (charts need numeric columns to stay numeric);
    dates/times become ISO strings."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        try:
            return bytes(v).decode("utf-8", "replace")
        except Exception:
            return str(v)
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_jsonable(x) for k, x in v.items()}
    return str(v)


def jsonify_rows(rows):
    """Apply to_jsonable across a result set (list of row lists)."""
    return [[to_jsonable(v) for v in r] for r in rows]


class Connector:
    name = "base"
    dialect = "ansi"

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        # Wrap only where run_query is DEFINED: a subclass that inherits it
        # (the marketing connectors under ApiReportConnector) already gets the
        # wrapped version, and wrapping twice would be harmless but misleading.
        fn = cls.__dict__.get("run_query")
        if fn is not None and not hasattr(fn, "__wrapped_run_query__"):
            cls.run_query = _guard_run_query(fn)

    def configured(self):
        """True when credentials/config for this source are present."""
        raise NotImplementedError

    def list_tables(self):
        """Return a list of table names."""
        raise NotImplementedError

    def get_schema(self, table):
        """Return [{"name": col, "type": sqltype}, ...] for a table."""
        raise NotImplementedError

    def qualifiers(self):
        """Namespace prefixes a table reference may carry in this source's SQL
        — {"public", "acme.public"} for a Postgres schema, say.

        The gateway hands this to queryguard, which refuses a FROM/JOIN target
        whose qualifier is not in the set: RBAC's allowlist only ever describes
        the namespace this connector is CONFIGURED with, while the credential
        it connects with usually sees more, so `secret_schema.sales` must not
        ride in on an allowlist entry for `sales`. Unqualified names are always
        fine — they are what RBAC keys on.

        ARITY is part of the declaration, not a detail: a one-part prefix and a
        two-part prefix are DIFFERENT questions, because engines resolve them
        differently (Snowflake reads `x.sales` as SCHEMA.object but `a.b.sales`
        as DATABASE.SCHEMA.object). Declare each spelling at the arity the
        vendor actually gives it, and state that rule in the override's
        docstring — declaring a DATABASE name as a ONE-part prefix let
        `<database>.sales` through as a schema reference the catalog had never
        described. The guard matches a whole prefix at its own arity and never
        on a suffix.

        Case follows the ENGINE: the guard canonicalizes both sides for the
        connector's dialect, so a prefix may be reported lower-cased where the
        engine folds bare names (Postgres, Snowflake) but must keep its exact
        spelling where it does not (BigQuery dataset ids).

        Default: EMPTY, i.e. no qualifier is accepted. A connector that has not
        declared its namespace cannot vouch for one, and sqlite / in-memory /
        view-backed sources genuinely have none. Build it from the SAME env the
        connector connects with and keep it cheap: this runs on every query, so
        it must never touch the network.
        """
        return frozenset()

    def run_query(self, sql):
        """Execute a (pre-validated) SELECT. Return (columns, rows).

        Never call this directly — go through gateway.execute. Subclass
        overrides are wrapped by __init_subclass__ and raise RuntimeError
        outside gateway_scope()/unguarded()."""
        raise NotImplementedError

    # ── High-risk capabilities (gated by the supervisor + human-in-the-loop) ──
    # These break the read-only invariant, so they must never be reached
    # except through supervisor.py, which requires human approval first.

    def run_script(self, sql):
        """Execute a write/DDL statement against this environment. Default:
        unsupported — a connector must opt in."""
        raise NotImplementedError(f"{self.name} does not support writes")

    def submit_spark_job(self, config):
        """Submit a Spark / compute job. Return a run handle. Default:
        unsupported."""
        raise NotImplementedError(f"{self.name} does not support Spark jobs")

    def rollback(self, handle):
        """Undo a deployed run after a terminal failure (cancel the run, revert
        the target). Called only by the safe-production flow on a run that keeps
        failing. Default: unsupported — the flow then flags manual intervention."""
        raise NotImplementedError(f"{self.name} does not support rollback")
