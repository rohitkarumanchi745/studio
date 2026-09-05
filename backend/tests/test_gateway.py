"""The one data gate — gateway.execute / check / scope and the invariant that
raw connector execution is unreachable outside it.

Two halves. Behavioural: RBAC, guard, row cap, governance masking and the
audit row all happen inside execute(), and the connector guard refuses a
direct run_query. Static: no module under app/ (outside gateway.py and the
connectors package) calls `.run_query(` and nothing but connectors/base.py
mentions the unguarded() escape hatch — so a new call site cannot quietly
re-implement half the pipeline.

Run from the backend directory:
    python -m pytest tests/test_gateway.py -q
"""
import os
import re
import tempfile
import threading
import time

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-gateway-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException

from app import db, gateway, governance, limits, queryguard
from app.connectors.base import unguarded
from app.connectors.demo import DemoConnector, seed
from app.queryguard import QueryRejected

ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst"}
VIEWER = {"id": "u-viewer", "email": "view@studio.test", "role": "viewer"}

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

GOV_YAML = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*" } }
  viewer: { sources: { demo: [sales, web_traffic] } }
compliance:
  demo:
    customers:
      deny_columns: [name]
      mask_columns: [lifetime_value]
"""


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    seed()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac(monkeypatch):
    """Each test starts on the built-in policies with no governance doc and no
    query timeout; tests that need a doc load one and it is dropped after."""
    monkeypatch.delenv("STUDIO_QUERY_TIMEOUT_S", raising=False)
    governance._STATE.update(doc=None, yaml="", source=None)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


def _audit_rows(user, action=None):
    rows = [r for r in db.list_activity(user["id"]) if action is None or r["action"] == action]
    return rows


# ── RBAC, guard, governance, cap ────────────────────────────────────────

def test_viewer_cannot_read_customers_and_rejection_is_audited():
    before = len(_audit_rows(VIEWER, "viewer_probe"))
    with pytest.raises(QueryRejected):
        gateway.execute(VIEWER, "demo", "SELECT * FROM customers", "viewer_probe")
    rows = _audit_rows(VIEWER, "viewer_probe")
    assert len(rows) == before + 1
    assert rows[0]["ok"] == 0 and "customers" in (rows[0]["error"] or "")


def test_viewer_has_no_access_to_a_source_outside_its_policy():
    with pytest.raises(QueryRejected, match="no access to snowflake"):
        gateway.execute(VIEWER, "snowflake", "SELECT 1 FROM x", "viewer_probe")


def test_unknown_and_unconfigured_sources_raise_http_errors():
    # RBAC runs first: under the built-in policies nobody is granted "nope",
    # so the unknown name is a rejection, not a 404, unless the role has "*".
    with pytest.raises(QueryRejected):
        gateway.execute(ANALYST, "nope", "SELECT 1 FROM x", "probe")
    with pytest.raises(HTTPException) as e:                # granted but unconfigured
        gateway.execute(ANALYST, "snowflake", "SELECT 1 FROM x", "probe")
    assert e.value.status_code == 400
    governance._set(GOV_YAML, "test")                      # admin: sources "*"
    admin = {"id": "u-admin", "email": "admin@studio.test", "role": "admin"}
    with pytest.raises(HTTPException) as e:
        gateway.execute(admin, "nope", "SELECT 1 FROM x", "probe")
    assert e.value.status_code == 404


def test_governance_deny_and_mask_apply_inside_execute():
    governance._set(GOV_YAML, "test")
    assert governance.loaded()
    r = gateway.execute(ANALYST, "demo", "SELECT * FROM customers", "gov_probe")
    assert "name" not in [c.lower() for c in r.columns]
    lv = [c.lower() for c in r.columns].index("lifetime_value")
    assert r.rows and all(row[lv] == "***" for row in r.rows)
    assert r.row_count == len(r.rows)
    assert r.source == "demo" and r.purpose == "gov_probe"
    d = r.as_dict()
    assert d["columns"] == r.columns and d["sql"] == r.sql and d["row_count"] == r.row_count


# The four shapes that walked denied/masked values past the gate: a comma
# join (invisible to the FROM/JOIN regex), a qualified column over one, and a
# rename hidden inside a derived table or a CTE. The first two are governed by
# base column; the last two are OPAQUE and fail closed (a denied column named
# anywhere inside is a rejection; a masked one masks every output column).
@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales, customers",
    "SELECT customers.name, customers.lifetime_value FROM sales, customers",
])
def test_comma_join_cannot_dodge_deny_or_mask(sql):
    governance._set(GOV_YAML, "test")
    r = gateway.execute(ANALYST, "demo", sql, "gov_probe", max_rows=3)
    cols = [c.lower() for c in r.columns]
    assert "name" not in cols
    lv = cols.index("lifetime_value")
    assert r.rows and all(row[lv] == "***" for row in r.rows)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM (SELECT name AS n, lifetime_value AS lv FROM customers) sub",
    "WITH x AS (SELECT name AS n, lifetime_value AS lv FROM customers) SELECT * FROM x",
    "SELECT sub.n FROM (SELECT name AS n FROM customers) sub",
    "SELECT city FROM customers UNION ALL SELECT name FROM customers",
])
def test_denied_column_inside_a_derived_table_or_cte_is_rejected(sql):
    governance._set(GOV_YAML, "test")
    before = len(_audit_rows(ANALYST, "gov_probe"))
    with pytest.raises(QueryRejected) as ei:
        gateway.execute(ANALYST, "demo", sql, "gov_probe", max_rows=3)
    assert "name" in str(ei.value)
    rows = _audit_rows(ANALYST, "gov_probe")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0       # refusal is audited


@pytest.mark.parametrize("sql", [
    "SELECT * FROM (SELECT city, lifetime_value AS l FROM customers) x",
    "WITH t AS (SELECT city, lifetime_value AS l FROM customers) SELECT * FROM t",
    "SELECT region, (SELECT MAX(lifetime_value) FROM customers) AS m FROM sales",
])
def test_masked_column_inside_a_derived_table_masks_every_output_column(sql):
    governance._set(GOV_YAML, "test")
    r = gateway.execute(ANALYST, "demo", sql, "gov_probe", max_rows=3)
    assert r.rows and all(v == "***" for row in r.rows for v in row)


def test_simple_shapes_are_still_governed_per_column():
    governance._set(GOV_YAML, "test")
    # A top-level alias maps to its base column; other columns stay in the clear.
    r = gateway.execute(ANALYST, "demo", "SELECT city, lifetime_value AS l FROM customers",
                        "gov_probe", max_rows=2)
    assert r.columns == ["city", "l"] and all(row[0] != "***" and row[1] == "***" for row in r.rows)
    # A star mixed with an alias of a denied column is an unmappable projection
    # that names the column: rejected, not guessed at.
    with pytest.raises(QueryRejected):
        gateway.execute(ANALYST, "demo", "SELECT *, name AS n FROM customers", "gov_probe",
                        max_rows=2)
    # ...while a star mixed with an innocent alias is governed by name.
    r = gateway.execute(ANALYST, "demo", "SELECT *, city AS c2 FROM customers", "gov_probe",
                        max_rows=2)
    assert "name" not in r.columns and "c2" in r.columns
    # A CTE that does not name a governed column is governed on output names.
    r = gateway.execute(ANALYST, "demo", "WITH t AS (SELECT * FROM customers) SELECT * FROM t",
                        "gov_probe", max_rows=2)
    cols = [c.lower() for c in r.columns]
    assert "name" not in cols and all(row[cols.index("lifetime_value")] == "***" for row in r.rows)
    # A frame whose denied column was already dropped (a cache hit, a stored
    # message) re-filters by name instead of failing closed a second time.
    cols, rows = governance.filter_result(
        "demo", "SELECT name, lifetime_value FROM customers", ["lifetime_value"], [[4200.0]])
    assert cols == ["lifetime_value"] and rows == [["***"]]
    # No SQL at all (rows with no provenance) fails closed to every governed table.
    cols, rows = governance.filter_result("demo", "", ["name", "x"], [["Ada", 1]])
    assert cols == ["x"] and rows == [[1]]


def test_max_rows_cap_and_limit_injection():
    r = gateway.execute(ANALYST, "demo", "SELECT * FROM sales", "cap_probe", max_rows=7)
    assert r.sql.endswith("LIMIT 7") and r.row_count == 7 and len(r.rows) == 7
    # A caller can lower the ceiling but never raise it above MAX_ROWS.
    r = gateway.execute(ANALYST, "demo", "SELECT * FROM sales", "cap_probe",
                        max_rows=limits.MAX_ROWS * 10)
    assert r.sql.endswith(f"LIMIT {limits.MAX_ROWS}")
    # An explicit LIMIT in the SQL is kept, but rows are still capped.
    r = gateway.execute(ANALYST, "demo", "SELECT * FROM sales LIMIT 50", "cap_probe", max_rows=3)
    assert r.sql.endswith("LIMIT 50") and r.row_count == 3


def test_success_writes_an_audit_row_named_after_purpose():
    before = len(_audit_rows(ANALYST, "unit_purpose"))
    r = gateway.execute(ANALYST, "demo", "SELECT region FROM sales -- note", "unit_purpose",
                        table_label="sales", max_rows=5)
    rows = _audit_rows(ANALYST, "unit_purpose")
    assert len(rows) == before + 1
    a = rows[0]
    assert a["ok"] == 1 and a["source"] == "demo" and a["tbl"] == "sales"
    assert a["sql"] == r.sql and "--" not in a["sql"]      # the cleaned SQL, not the input
    assert a["row_count"] == r.row_count == 5
    assert isinstance(r.took_ms, int)


def test_audit_false_writes_nothing():
    before = len(_audit_rows(ANALYST))
    gateway.execute(ANALYST, "demo", "SELECT 1 FROM sales", "silent", max_rows=1, audit=False)
    assert len(_audit_rows(ANALYST)) == before


def test_guard_rejection_is_audited_with_ok_false():
    before = len(_audit_rows(ANALYST, "bad_sql"))
    with pytest.raises(QueryRejected):
        gateway.execute(ANALYST, "demo", "DELETE FROM sales", "bad_sql")
    rows = _audit_rows(ANALYST, "bad_sql")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0 and rows[0]["error"]


def test_execution_error_is_audited_and_reraised():
    before = len(_audit_rows(ANALYST, "boom"))
    with pytest.raises(Exception, match="no such column"):
        gateway.execute(ANALYST, "demo", "SELECT nope_col FROM sales", "boom")
    rows = _audit_rows(ANALYST, "boom")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0 and "no such column" in rows[0]["error"]


# ── check() / scope() ───────────────────────────────────────────────────

def test_check_returns_cleaned_sql_without_auditing():
    before = len(_audit_rows(ANALYST))
    connector, allowed, cleaned = gateway.check(
        ANALYST, "demo", "SELECT region FROM sales /* c */", max_rows=9)
    assert connector.name == "demo" and "sales" in allowed and "customers" in allowed
    assert cleaned == "SELECT region FROM sales LIMIT 9"
    assert len(_audit_rows(ANALYST)) == before
    with pytest.raises(QueryRejected):
        gateway.check(VIEWER, "demo", "SELECT * FROM customers")


def test_the_gateway_passes_the_connectors_namespace_to_the_guard():
    """demo is one SQLite file: it declares no namespace, so a qualified
    reference is outside it however innocent the bare name looks. Without this
    the allowlist entry for `sales` also admitted `other_schema.sales` on every
    warehouse whose credential can see a second schema."""
    before = len(_audit_rows(ANALYST, "qualified_probe"))
    with pytest.raises(QueryRejected, match="outside the configured namespace"):
        gateway.execute(ANALYST, "demo", "SELECT * FROM secret_schema.sales",
                        "qualified_probe")
    rows = _audit_rows(ANALYST, "qualified_probe")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0     # refusal is audited
    with pytest.raises(QueryRejected, match="outside the configured namespace"):
        gateway.check(ANALYST, "demo", 'SELECT * FROM "main"."sales"')
    # The bare name — what RBAC keys on — still works.
    assert gateway.execute(ANALYST, "demo", "SELECT * FROM sales", "qualified_probe",
                           max_rows=1).row_count == 1


def test_scope_returns_only_what_the_role_can_see():
    connector, allowed = gateway.scope(VIEWER, "demo")
    assert connector.name == "demo" and set(allowed) == {"sales", "web_traffic"}
    with pytest.raises(QueryRejected):
        gateway.scope(VIEWER, "snowflake")


# ── Runtime guard on the connectors ─────────────────────────────────────

def test_direct_run_query_is_unreachable_outside_the_gateway():
    with pytest.raises(RuntimeError, match="gateway.execute"):
        DemoConnector().run_query("SELECT 1")
    with unguarded():
        columns, rows = DemoConnector().run_query("SELECT 1 AS one")
    assert columns == ["one"] and rows == [[1]]
    # The scope does not leak out of the block.
    with pytest.raises(RuntimeError):
        DemoConnector().run_query("SELECT 1")


def test_every_registered_connector_is_guarded():
    from app.connectors import _REGISTRY
    for name, conn in _REGISTRY.items():
        assert hasattr(type(conn).run_query, "__wrapped_run_query__"), name
        with pytest.raises(RuntimeError, match="gateway.execute"):
            conn.run_query("SELECT 1")


def test_scope_is_per_thread():
    """A gateway call on one thread must not unlock a stray call on another."""
    seen = {}

    def other():
        try:
            DemoConnector().run_query("SELECT 1")
            seen["err"] = None
        except RuntimeError as e:
            seen["err"] = e

    with unguarded():
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert isinstance(seen["err"], RuntimeError)


def test_query_timeout(monkeypatch):
    monkeypatch.setenv("STUDIO_QUERY_TIMEOUT_S", "0.2")
    r = gateway.execute(ANALYST, "demo", "SELECT 1 FROM sales", "fast", max_rows=1)
    assert r.row_count == 1                       # the worker thread carries the scope
    real = DemoConnector.run_query

    def slow(self, sql):
        time.sleep(1.0)
        return real(self, sql)

    monkeypatch.setattr(DemoConnector, "run_query", slow)
    before = len(_audit_rows(ANALYST, "slow"))
    with pytest.raises(gateway.QueryTimeout, match="exceeded"):
        gateway.execute(ANALYST, "demo", "SELECT 1 FROM sales", "slow", max_rows=1)
    assert _audit_rows(ANALYST, "slow")[0]["ok"] == 0 and len(_audit_rows(ANALYST, "slow")) == before + 1


# ── queryguard.base_tables ──────────────────────────────────────────────

def test_base_tables_ignores_cte_names_and_is_lexical():
    assert queryguard.base_tables(
        "WITH t AS (SELECT * FROM sales) SELECT * FROM t JOIN customers c ON 1=1"
    ) == {"sales", "customers"}
    assert queryguard.base_tables('SELECT * FROM "Sales", web_traffic') == {"sales", "web_traffic"}
    assert queryguard.base_tables("") == set()
    assert queryguard.base_tables("SELECT * FROM 's3://b/x.parquet'") == set()
    assert queryguard.base_tables("SELECT 1 /* open") == set()


# ── Static invariant ────────────────────────────────────────────────────

def _app_files():
    for root, _dirs, files in os.walk(APP_DIR):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _offenders(pattern, skip):
    out = []
    for path in _app_files():
        rel = os.path.relpath(path, APP_DIR)
        if skip(rel):
            continue
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if pattern.search(line):
                    out.append(f"app/{rel}:{n}")
    return out


def test_no_direct_run_query_outside_the_gateway():
    """Every row-returning path goes through gateway.execute. Lists each
    offending file:line so a migration can be checked off one by one."""
    hits = _offenders(re.compile(r"\.run_query\("),
                      lambda rel: rel == "gateway.py" or rel.startswith("connectors" + os.sep))
    assert not hits, "direct connector.run_query calls outside app/gateway.py:\n  " + "\n  ".join(hits)


def test_legacy_call_sites_match_the_static_offenders_exactly():
    """The transitional runtime exemption (connectors/base.LEGACY_CALL_SITES)
    may cover ONLY files that still contain a direct call, and every such file
    must be listed — so migrating a site forces its entry to be deleted, and
    the set is empty when the static test goes green."""
    from app.connectors import base
    hits = _offenders(re.compile(r"\.run_query\("),
                      lambda rel: rel == "gateway.py" or rel.startswith("connectors" + os.sep))
    offending_files = {h.split(":")[0].removeprefix("app/") for h in hits}
    assert {f for f, _fn in base.LEGACY_CALL_SITES} == offending_files


def test_no_runtime_exemption_by_app_file_or_function():
    """The by-name legacy exemption is gone: a direct call refuses even from a
    function compiled as if it lived in app/agent.py (the file/function that
    used to be exempt), and LEGACY_CALL_SITES cannot bring it back."""
    from app.connectors import base
    assert base.LEGACY_CALL_SITES == frozenset()
    assert not hasattr(base, "_legacy_caller")
    ns = {}
    exec(compile("def _run(connector, sql):\n    return connector.run_query(sql)\n",
                 os.path.join(APP_DIR, "agent.py"), "exec"), ns)
    with pytest.raises(RuntimeError, match="gateway.execute"):
        ns["_run"](DemoConnector(), "SELECT 1 AS one")


def test_unguarded_escape_hatch_is_not_used_in_app_code():
    hits = _offenders(re.compile(r"unguarded\("),
                      lambda rel: rel == os.path.join("connectors", "base.py"))
    assert not hits, "unguarded() must only exist in connectors/base.py:\n  " + "\n  ".join(hits)
