"""Identifier IDENTITY is dialect-aware, and quotedness is part of it.

The guard used to reduce every reference part to `.strip('"').lower()`, so
`"CUSTOMERS"` and `customers` were one name. They are not: PostgreSQL folds an
UNQUOTED identifier down and keeps a QUOTED one verbatim, so with only `sales`
allowed

    WITH "CUSTOMERS" AS (SELECT * FROM sales) SELECT * FROM customers

binds a CTE named CUSTOMERS while the outer reference resolves to the BASE
relation `customers` — a denied table the guard believed was the CTE. The same
collapse applied to qualifiers and to the allowlist comparison.

These tests pin the fix: a reference part carries (text, was_quoted), it is
canonicalized the way the ENGINE would read it, and CTE binding, allowlist
matching and qualifier matching all compare canonical identities.

Run from the backend directory:
    python -m pytest tests/test_identifier_canon.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-identcanon-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app import gateway, queryguard
from app.queryguard import QueryRejected, validate

# The `viewer` policy on a lower-case catalog; `customers` is the denied table.
ALLOWED = ["sales", "web_traffic"]
# What a Snowflake catalog reports: information_schema stores folded-UP names.
SNOW_ALLOWED = ["SALES", "WEB_TRAFFIC"]


def rejected(sql, allowed=ALLOWED, dialect=None, match="not permitted for your role", **kw):
    with pytest.raises(QueryRejected, match=match):
        validate(sql, allowed, dialect=dialect, **kw)


# ── The bypass itself: a quoted CTE cannot shadow an unquoted table ─────

def test_a_quoted_cte_does_not_legalize_the_unquoted_base_table():
    """The reproduced P0. On PostgreSQL the CTE is CUSTOMERS and the outer
    reference is the base relation customers — two different tables."""
    rejected('WITH "CUSTOMERS" AS (SELECT * FROM sales) SELECT * FROM customers',
             dialect="postgres")


def test_an_unquoted_cte_does_not_legalize_the_quoted_base_table():
    """The mirror: the CTE is `customers`, the reference names CUSTOMERS."""
    rejected('WITH customers AS (SELECT * FROM sales) SELECT * FROM "CUSTOMERS"',
             dialect="postgres")


@pytest.mark.parametrize("sql", [
    # A quoted CTE that shadows nothing is ordinary SQL and must still work.
    'WITH "Recent" AS (SELECT * FROM sales) SELECT * FROM "Recent"',
    'WITH "sales_2024" AS (SELECT * FROM sales) SELECT * FROM "sales_2024"',
    # …including one that legitimately shadows an ALLOWED table.
    'WITH "sales" AS (SELECT 1 AS x) SELECT * FROM "sales"',
    # A quoted reference to an allowed, lower-case-in-catalog table.
    'SELECT * FROM "sales"',
    'SELECT * FROM "sales" JOIN "web_traffic" ON 1=1',
    # Unquoted, any case: PostgreSQL folds it down onto the catalog name.
    "SELECT * FROM SALES",
    "SELECT * FROM Sales",
])
def test_legitimate_postgres_references_still_validate(sql):
    assert validate(sql, ALLOWED, dialect="postgres")


def test_a_quoted_reference_to_a_differently_cased_table_is_denied():
    """`"SALES"` is not the catalog's `sales` on PostgreSQL — no allowlist
    entry covers it, so it is denied rather than folded onto one."""
    rejected('SELECT * FROM "SALES"', dialect="postgres")


# ── Snowflake folds the other way ──────────────────────────────────────

def test_snowflake_bare_name_reaches_the_upper_cased_catalog_entry():
    """Snowflake folds a BARE identifier UP, so `sales` IS the stored SALES."""
    assert validate("SELECT * FROM sales", SNOW_ALLOWED, dialect="snowflake")
    assert validate("SELECT * FROM SaLeS", SNOW_ALLOWED, dialect="snowflake")


def test_snowflake_quoted_lowercase_does_not_match_the_upper_cased_entry():
    """`"sales"` names a distinct, lower-case object. Matching it against SALES
    is exactly the collapse this fix removes."""
    rejected('SELECT * FROM "sales"', SNOW_ALLOWED, dialect="snowflake")
    assert validate('SELECT * FROM "SALES"', SNOW_ALLOWED, dialect="snowflake")


def test_snowflake_cte_identity_folds_up_too():
    """A bare CTE `customers` binds CUSTOMERS on Snowflake, so a quoted
    `"customers"` reference is the (denied) lower-case base object."""
    assert validate("WITH customers AS (SELECT 1) SELECT * FROM CUSTOMERS",
                    SNOW_ALLOWED, dialect="snowflake")
    rejected('WITH customers AS (SELECT 1) SELECT * FROM "customers"',
             SNOW_ALLOWED, dialect="snowflake")


# ── BigQuery folds nothing ─────────────────────────────────────────────

def test_bigquery_identifiers_are_case_sensitive_as_written():
    """BigQuery table ids are case-SENSITIVE and backticks only escape, so the
    written spelling is the identity, quoted or not."""
    assert validate("SELECT * FROM Orders", ["Orders"], dialect="bigquery")
    assert validate("SELECT * FROM `Orders`", ["Orders"], dialect="bigquery")
    rejected("SELECT * FROM orders", ["Orders"], dialect="bigquery")
    rejected("SELECT * FROM ORDERS", ["Orders"], dialect="bigquery")


# ── Case-insensitive engines keep collapsing, because they do ──────────

@pytest.mark.parametrize("dialect", ["sqlite", "duckdb", "databricks", None])
def test_case_insensitive_engines_still_resolve_a_quoted_cte(dialect):
    """SQLite, DuckDB and Spark/Databricks fold quoted names too, so there the
    quoted CTE really does answer the bare reference — the guard must model the
    engine, not be uniformly strict. dialect=None is the historical reading and
    keeps blend / dashboards (no connector in hand) unaffected."""
    assert validate('WITH "CUSTOMERS" AS (SELECT * FROM sales) SELECT * FROM customers',
                    ALLOWED, dialect=dialect)
    assert validate('SELECT * FROM "SALES"', ALLOWED, dialect=dialect)


def test_the_default_signature_is_unchanged_for_callers_without_a_connector():
    """blend.py and dashboards.py call validate(sql, allowed) positionally."""
    assert validate("SELECT * FROM sales", ALLOWED)
    assert validate("SELECT * FROM sales", ALLOWED, None)


# ── Qualifiers are canonicalized the same way ──────────────────────────

PG = frozenset({"public", "acme.public"})


def test_a_qualifier_is_matched_canonically_not_by_lowercasing():
    # Bare, any case: PostgreSQL folds it onto the configured schema.
    assert validate("SELECT * FROM PUBLIC.sales", ALLOWED, qualifiers=PG, dialect="postgres")
    assert validate('SELECT * FROM "public".sales', ALLOWED, qualifiers=PG, dialect="postgres")
    # Quoted and differently cased: a DIFFERENT schema, so outside the namespace.
    rejected('SELECT * FROM "PUBLIC".sales', qualifiers=PG, dialect="postgres",
             match="outside the configured namespace")


def test_the_qualified_form_still_needs_the_allowlist_canonically():
    """A qualified reference names a real table, never a CTE — and on Snowflake
    the quoted lower-case spelling is not the stored SALES."""
    quals = frozenset({"PUBLIC", "ANALYTICS.PUBLIC"})   # the stored spelling
    assert validate("SELECT * FROM public.sales", SNOW_ALLOWED,
                    qualifiers=quals, dialect="snowflake")
    rejected('SELECT * FROM public."sales"', SNOW_ALLOWED,
             qualifiers=quals, dialect="snowflake")


def test_base_tables_stays_lowercase_and_dialect_free():
    """governance and chat key on lowercase bare names and have no connector."""
    assert queryguard.base_tables('SELECT * FROM "CUSTOMERS" JOIN sales ON 1=1') == {
        "customers", "sales"}


# ── The gateway supplies the dialect ───────────────────────────────────

def test_the_gateway_hands_the_connectors_dialect_to_the_sql_guard(monkeypatch):
    """Without this the guard reads every warehouse as case-insensitive, which
    is what let the quoted-CTE bypass through on Postgres and Snowflake."""
    seen = {}
    real = queryguard.validate

    def spy(sql, allowed, qualifiers=None, dialect=None):
        seen["dialect"] = dialect
        return real(sql, allowed, qualifiers=qualifiers, dialect=dialect)

    monkeypatch.setattr(queryguard, "validate", spy)
    user = {"id": "u", "email": "ana@studio.test", "role": "analyst"}
    gateway.check(user, "demo", "SELECT * FROM sales")
    from app.connectors.demo import DemoConnector
    assert seen["dialect"] == DemoConnector.dialect
