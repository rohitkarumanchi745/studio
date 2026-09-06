"""router._has_access — the ROUTING-time access check on a learned pattern.

`choose()` may only send a requester to a centralized BitNet pattern that the
requester could actually read. That check used to resolve the pattern's tables
with a regex over FROM/JOIN and then throw the qualifier away:

    refs = {r.strip('"').split(".")[-1].lower() for r in TABLE_REF.findall(sql)}

which is the exact defect queryguard._qualified_name was fixed for. Confirmed
against the pre-fix function, with `sales` allowed and nothing else:

    SELECT * FROM secret_schema.sales   -> True   (qualifier dropped)
    SELECT * FROM sales, web_traffic    -> True   (regex misses the 2nd arm)

Neither is a data-access bypass — app/gateway.py re-validates every execution
with the full guard, and that is the boundary. Both are wrong ROUTING
decisions: a BitNet call spent on a pattern whose data the requester cannot
read, then an escalation, and a comment claiming a property the code did not
deliver.

These tests pin the fixed reading: base tables come from the guard's own
tokenizer (queryguard.base_tables — comma joins seen, CTEs understood), a
qualified reference must name the connector's configured namespace, and
anything unreadable fails closed. Where the two must agree, the assertion is
made against the guard call gateway.check() itself would make, not against a
hand-written expectation.

Run from the backend directory:
    python -m pytest tests/test_router_access.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths (nothing here reads
# it: gateway.scope is stubbed and no query is ever executed).
_TMP = tempfile.mkdtemp(prefix="studio-router-access-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import gateway, qcache, queryguard
from app import router as model_router
from app.connectors.base import Connector
from app.queryguard import QueryRejected

ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst"}


# ── Connectors: one with no namespace, one with a real one ──────────────

class _Spy(Connector):
    """A connector that answers the two cheap questions the check is allowed to
    ask and blows up on anything that would touch a warehouse."""

    name = "spy"
    dialect = "sqlite"

    def __init__(self):
        self.calls = []

    def qualifiers(self):
        self.calls.append("qualifiers")
        return frozenset()          # sqlite: no schema/catalog namespace at all

    def list_tables(self):          # scope() is stubbed, so this is a tripwire
        raise AssertionError("routing must not list the warehouse's tables")

    def run_query(self, sql):
        raise AssertionError("routing must never execute SQL")


class _PgSpy(_Spy):
    """Postgres-shaped: a configured schema at one part, `database.schema` at
    two — the arities postgres_conn.qualifiers() declares."""

    name = "pg-spy"
    dialect = "postgres"

    def qualifiers(self):
        self.calls.append("qualifiers")
        return frozenset({"analytics", "warehouse.analytics"})


@pytest.fixture()
def scoped(monkeypatch):
    """Stub gateway.scope — the ONE call the check is allowed to make — with a
    connector and the allowlist RBAC built for this requester's role."""
    state = {}

    def use(connector, allowed):
        state["connector"], state["allowed"] = connector, list(allowed)

        def scope(user, source, **kw):
            state["scoped"] = state.get("scoped", 0) + 1
            return connector, list(allowed)

        monkeypatch.setattr(gateway, "scope", scope)
        return connector

    use.state = state
    return use


def _gateway_would_accept(connector, allowed, sql):
    """The verdict gateway.check() would reach on this SQL, minus execution:
    the same guard, handed the same qualifiers and dialect it passes."""
    try:
        queryguard.validate(sql, allowed, qualifiers=connector.qualifiers(),
                            dialect=getattr(connector, "dialect", None))
        return True
    except QueryRejected:
        return False


# ── The reviewer's repro ────────────────────────────────────────────────

def test_qualified_table_outside_the_namespace_is_refused(scoped):
    """`secret_schema.sales` with only `sales` allowed. The pre-fix check
    returned True here: it split on '.' and kept the last part, so a pattern
    reading a schema the catalog never described looked routable."""
    conn = scoped(_Spy(), ["sales"])
    sql = "SELECT region, SUM(revenue) FROM secret_schema.sales GROUP BY region"

    assert model_router._has_access(ANALYST, "demo", sql) is False
    # …and the guard that will actually run agrees, which is the point: the
    # routing decision now matches the execution the requester would get.
    assert _gateway_would_accept(conn, ["sales"], sql) is False


def test_the_last_part_alone_never_buys_access(scoped):
    """Not just schemas: a 3-part `other_db.public.sales` and a quoted
    qualifier are the same trick wearing different clothes."""
    scoped(_Spy(), ["sales", "customers"])
    for sql in ('SELECT * FROM other_db.public.sales',
                'SELECT * FROM "secret_schema".sales',
                'SELECT * FROM sales JOIN secret_schema.customers USING (id)'):
        assert model_router._has_access(ANALYST, "demo", sql) is False, sql


# ── The tokenizer, not the regex ────────────────────────────────────────

def test_comma_join_naming_a_denied_table_is_refused(scoped):
    """`FROM sales, web_traffic` — the regex only ever saw the first arm, so a
    pattern reading a denied table alongside an allowed one was routed."""
    conn = scoped(_Spy(), ["sales"])
    sql = "SELECT * FROM sales, web_traffic WHERE sales.id = web_traffic.id"

    assert model_router._has_access(ANALYST, "demo", sql) is False
    assert _gateway_would_accept(conn, ["sales"], sql) is False


def test_a_cte_is_not_mistaken_for_a_denied_table(scoped):
    """`WITH customers AS (...)` binds a NAME, it does not read the table
    `customers`. The gateway resolves it to the CTE and runs the query, so the
    router must route it — the regex refused it and sent a perfectly good
    learned pattern to the frontier."""
    conn = scoped(_Spy(), ["sales"])          # `customers` is DENIED
    sql = ("WITH customers AS (SELECT region, revenue FROM sales) "
           "SELECT region, SUM(revenue) FROM customers GROUP BY region")

    assert _gateway_would_accept(conn, ["sales"], sql) is True
    assert model_router._has_access(ANALYST, "demo", sql) is True


def test_a_cte_name_cannot_launder_a_qualified_base_table(scoped):
    """The other half of the same rule: a CTE named `sales` does not make
    `other.sales` — a real table in another namespace — readable. Both layers
    refuse it."""
    conn = scoped(_Spy(), ["sales"])
    sql = ("WITH sales AS (SELECT 1 AS id) "
           "SELECT * FROM other_schema.sales")

    assert _gateway_would_accept(conn, ["sales"], sql) is False
    assert model_router._has_access(ANALYST, "demo", sql) is False


def test_a_pattern_reading_only_ctes_fails_closed(scoped):
    """No attributable base table. The guard accepts it (every reference
    resolves to a binding), but the router has nothing to check an allowlist
    against, so routing fails closed rather than guessing."""
    scoped(_Spy(), ["sales"])
    assert model_router._has_access(
        ANALYST, "demo", "WITH t AS (SELECT 1 AS x) SELECT * FROM t") is False


# ── The ordinary case still routes ──────────────────────────────────────

def test_an_allowed_pattern_is_still_routed(scoped):
    conn = scoped(_Spy(), ["sales", "customers"])
    sql = ("SELECT c.region, SUM(s.revenue) FROM sales s "
           "JOIN customers c ON c.id = s.customer_id GROUP BY c.region")

    assert model_router._has_access(ANALYST, "demo", sql) is True
    assert _gateway_would_accept(conn, ["sales", "customers"], sql) is True
    # Case is not access: the allowlist spelling and the reference need not match.
    assert model_router._has_access(ANALYST, "demo", "SELECT * FROM SALES") is True


def test_a_declared_qualifier_is_routed_and_an_undeclared_one_is_not(scoped):
    """A connector WITH a namespace: its own schema is fine at the arity it
    declared, another schema is not, and the right name at the wrong arity is
    not either (`warehouse.sales` reads `warehouse` as a schema)."""
    conn = scoped(_PgSpy(), ["sales"])
    cases = {
        "SELECT * FROM analytics.sales": True,
        "SELECT * FROM warehouse.analytics.sales": True,
        "SELECT * FROM sales": True,
        "SELECT * FROM secret_schema.sales": False,
        "SELECT * FROM warehouse.sales": False,
        "SELECT * FROM analytics.sales JOIN other.sales USING (id)": False,
    }
    for sql, expected in cases.items():
        assert model_router._has_access(ANALYST, "demo", sql) is expected, sql
        assert _gateway_would_accept(conn, ["sales"], sql) is expected, sql


# ── Fail closed, and stay cheap ─────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    None, "", "   ", ";;",
    "SELECT * FROM 'unterminated",             # the tokenizer raises on this
    "SELECT * FROM /* unclosed comment sales",
    "SELECT 1",                                # no FROM at all
    "SELECT * FROM",                           # truncated reference
    "MATCH (n:Person) RETURN n",               # cypher: not this router's SQL
])
def test_unreadable_sql_is_refused_never_raised(scoped, sql):
    scoped(_Spy(), ["sales"])
    assert model_router._has_access(ANALYST, "demo", sql) is False


def test_a_scope_failure_is_refused_not_raised(monkeypatch):
    """No access to the source, or an unconfigured one: scope() raises and the
    routing decision must be `frontier`, not a 500 in the middle of a turn."""
    def boom(user, source, **kw):
        raise QueryRejected("Your role has no access to demo")

    monkeypatch.setattr(gateway, "scope", boom)
    assert model_router._has_access(ANALYST, "demo", "SELECT * FROM sales") is False


def test_the_check_touches_nothing_but_scope(scoped):
    """The routing hot path runs this for every candidate prompt: one scope()
    call, then pure lexing. list_tables/run_query on the spy connector raise if
    the check ever reaches for the warehouse."""
    conn = scoped(_Spy(), ["sales"])
    for _ in range(3):
        assert model_router._has_access(ANALYST, "demo", "SELECT * FROM sales") is True
    assert scoped.state["scoped"] == 3
    assert conn.calls == ["qualifiers"] * 3


# ── choose(): the decision the check exists to make ─────────────────────

@pytest.fixture()
def ready(monkeypatch):
    monkeypatch.setattr(model_router, "bitnet_ready", lambda user: True)


def _learned(sql):
    return {"sql": sql, "prompt": "total revenue by region", "seen": 4,
            "similarity": 0.75, "text": "revenue by region"}


def test_choose_refuses_a_pattern_on_an_unreachable_schema(scoped, ready, monkeypatch):
    scoped(_Spy(), ["sales"])
    pattern = _learned("SELECT * FROM secret_schema.sales")
    monkeypatch.setattr(qcache, "learned", lambda *a, **kw: pattern)

    assert model_router.choose(ANALYST, "demo", "sales", "revenue by region") == (
        "frontier", None)


def test_choose_routes_a_reachable_pattern(scoped, ready, monkeypatch):
    scoped(_Spy(), ["sales"])
    pattern = _learned("SELECT region, SUM(revenue) FROM sales GROUP BY region")
    monkeypatch.setattr(qcache, "learned", lambda *a, **kw: pattern)

    assert model_router.choose(ANALYST, "demo", "sales", "revenue by region") == (
        "bitnet", pattern)
