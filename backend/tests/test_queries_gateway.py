"""queries.verify_sql is a thin wrapper over gateway.execute.

The dict contract ({ok, sql, columns, rows, row_count, took_ms} on success,
{ok: False, error} on any failure, never raising) is what blend, autopilot,
flow, pipelines and semantic branch on, so these tests pin it down while the
enforcement itself — RBAC, guard, LIMIT, governance, audit — is proven to
come from the gateway (the audit row is named "verify", masking applies).

Run from the backend directory:
    python -m pytest tests/test_queries_gateway.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-queries-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app import db, governance, queries
from app.connectors.demo import seed

ANALYST = {"id": "u-q-analyst", "email": "ana@studio.test", "role": "analyst"}
VIEWER = {"id": "u-q-viewer", "email": "view@studio.test", "role": "viewer"}
ADMIN = {"id": "u-q-admin", "email": "admin@studio.test", "role": "admin"}

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
    queries.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac():
    """Built-in policies and no governance doc unless a test loads one."""
    governance._STATE.update(doc=None, yaml="", source=None)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


def _audit_rows(user, action):
    return [r for r in db.list_activity(user["id"]) if r["action"] == action]


# ── Failure mapping: never raises, one error string per cause ───────────

def test_viewer_on_customers_is_rejected_with_the_guard_prefix():
    r = queries.verify_sql(VIEWER, "demo", "customers", "SELECT * FROM customers")
    assert r["ok"] is False
    assert r["error"].startswith("Rejected by the query guard: ")
    # Whole-source label: RBAC on the source passes, the guard's allowlist
    # (derived by the gateway, not by the caller) still stops the table.
    r = queries.verify_sql(VIEWER, "demo", "*", "SELECT * FROM customers")
    assert r["ok"] is False and r["error"].startswith("Rejected by the query guard: ")
    # A rejection is a reward-0.2 outcome, not a hard failure.
    assert queries._verify_reward(r) == 0.2


def test_write_statement_is_rejected_not_raised():
    r = queries.verify_sql(ANALYST, "demo", "sales", "DELETE FROM sales")
    assert r["ok"] is False and r["error"].startswith("Rejected by the query guard: ")


def test_unknown_source_returns_the_404_detail():
    governance._set(GOV_YAML, "test")                 # admin: sources "*"
    r = queries.verify_sql(ADMIN, "nope", "*", "SELECT 1 FROM x")
    assert r["ok"] is False and r["error"] == "Unknown source 'nope'"


def test_unconfigured_source_returns_the_400_detail():
    r = queries.verify_sql(ANALYST, "snowflake", "*", "SELECT 1 FROM x")
    assert r["ok"] is False and "not configured" in r["error"]


def test_execution_error_is_reported_as_query_failed():
    r = queries.verify_sql(ANALYST, "demo", "sales", "SELECT nope_col FROM sales")
    assert r["ok"] is False and r["error"].startswith("Query failed: ")
    assert "no such column" in r["error"]
    assert queries._verify_reward(r) == 0.0


def test_empty_sql_and_missing_source_short_circuit():
    assert queries.verify_sql(ANALYST, "demo", "sales", "   ") == {"ok": False, "error": "SQL is empty."}
    assert queries.verify_sql(ANALYST, "", "sales", "SELECT 1") == {"ok": False, "error": "No source selected."}


# ── Success contract and the audit trail ────────────────────────────────

def test_analyst_on_sales_succeeds_with_limit_and_a_verify_audit_row():
    before = len(_audit_rows(ANALYST, "verify"))
    r = queries.verify_sql(ANALYST, "demo", "sales", "SELECT region FROM sales -- note")
    assert r["ok"] is True
    assert set(r) == {"ok", "sql", "columns", "rows", "row_count", "took_ms"}
    assert "LIMIT" in r["sql"] and "--" not in r["sql"]   # cleaned, LIMIT-bearing SQL
    assert r["columns"] == ["region"]
    assert len(r["rows"]) == queries.PREVIEW_ROWS       # preview, not the full result
    assert r["row_count"] > queries.PREVIEW_ROWS        # true count
    assert isinstance(r["took_ms"], int)
    rows = _audit_rows(ANALYST, "verify")
    assert len(rows) == before + 1
    a = rows[0]
    assert a["ok"] == 1 and a["source"] == "demo" and a["tbl"] == "sales"
    assert a["sql"] == r["sql"] and a["row_count"] == r["row_count"]


def test_rejection_is_audited_under_verify_too():
    before = len(_audit_rows(VIEWER, "verify"))
    queries.verify_sql(VIEWER, "demo", "customers", "SELECT * FROM customers")
    rows = _audit_rows(VIEWER, "verify")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0 and rows[0]["error"]


def test_full_rows_returns_every_row_of_a_big_table():
    r = queries.verify_sql(ANALYST, "demo", "sales", "SELECT * FROM sales", full_rows=True)
    assert r["ok"] is True
    assert len(r["rows"]) == r["row_count"] > queries.PREVIEW_ROWS
    preview = queries.verify_sql(ANALYST, "demo", "sales", "SELECT * FROM sales")
    assert preview["row_count"] == r["row_count"] and len(preview["rows"]) == queries.PREVIEW_ROWS


def test_governance_mask_applies_to_the_preview():
    governance._set(GOV_YAML, "test")
    assert governance.loaded()
    r = queries.verify_sql(ANALYST, "demo", "customers", "SELECT * FROM customers")
    assert r["ok"] is True
    cols = [c.lower() for c in r["columns"]]
    assert "name" not in cols                            # denied column stripped
    lv = cols.index("lifetime_value")
    assert r["rows"] and all(row[lv] == "***" for row in r["rows"])


def test_verify_agent_keeps_the_contract_and_names_the_verifier():
    from app import roster
    r = queries.verify_agent(ANALYST, "demo", "sales", "SELECT region FROM sales")
    assert r["ok"] is True and r["agent"] == roster.SQL_VERIFIER["name"]
    assert "LIMIT" in r["sql"] and len(r["rows"]) <= queries.PREVIEW_ROWS
