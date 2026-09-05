"""A qualifier is part of the table name, not decoration.

The guard used to reduce `secret_schema.sales` to `sales` and match that
against the RBAC allowlist, so an allowlist built from ONE schema also admitted
every same-named table in every other schema the warehouse credential could
see — the catalog/RBAC boundary bypassed with a prefix. These tests pin the
fix: the bare name stays the RBAC key, but a qualifier outside the connector's
own configured namespace is refused.

Run from the backend directory:
    python -m pytest tests/test_qualified_names.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-qualnames-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app.connectors.bigquery_conn import BigQueryConnector
from app.connectors.databricks_conn import DatabricksConnector
from app.connectors.demo import DemoConnector
from app.connectors.graph_conn import GraphConnector
from app.connectors.postgres_conn import PostgresConnector
from app.connectors.snowflake_conn import SnowflakeConnector
from app.queryguard import QueryRejected, validate

ALLOWED = ["sales", "web_traffic"]
# What a Postgres connector with POSTGRES_SCHEMA=public reports.
PG = frozenset({"public", "acme.public"})


def rejected(sql, allowed=ALLOWED, qualifiers=PG, match="outside the configured namespace",
             dialect=None):
    with pytest.raises(QueryRejected, match=match):
        validate(sql, allowed, qualifiers=qualifiers, dialect=dialect)


# ── The bypass itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT * FROM secret_schema.sales",
    'SELECT * FROM "secret_schema"."sales"',
    "SELECT * FROM SECRET_SCHEMA.SALES",
    'SELECT * FROM "SECRET_SCHEMA".sales',
    "SELECT * FROM secret_schema . sales",
    "SELECT * FROM/**/secret_schema.sales",
    # Every arm of a comma join is a data source, not just the first.
    "SELECT * FROM sales, secret_schema.sales",
    "SELECT * FROM sales JOIN secret_schema.sales USING (id)",
    # A CTE named `sales` must not launder the permission onto a base table in
    # another schema: the engine resolves a QUALIFIED ref to the real table.
    "WITH sales AS (SELECT 1) SELECT * FROM secret_schema.sales",
    "WITH sales AS (SELECT 1) SELECT * FROM sales UNION ALL SELECT * FROM secret_schema.sales",
    # A different database, same schema name — the "last part matches" reading
    # of the rule would let this through.
    "SELECT * FROM other_db.public.sales",
])
def test_a_qualifier_outside_the_namespace_is_rejected(sql):
    rejected(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales",                              # unqualified: the RBAC key
    "SELECT * FROM public.sales",                       # the configured schema
    "SELECT * FROM PUBLIC.SALES",                       # case-insensitive
    'SELECT * FROM "public"."sales"',                   # quote-insensitive
    "SELECT * FROM acme.public.sales",                  # database.schema, declared
    "SELECT * FROM public.sales JOIN web_traffic ON 1=1",
    "WITH t AS (SELECT * FROM public.sales) SELECT * FROM t",
])
def test_the_connectors_own_namespace_is_accepted(sql):
    assert validate(sql, ALLOWED, qualifiers=PG)


def test_a_denied_table_is_still_denied_inside_the_namespace():
    """The qualifier check is an EXTRA gate, never a replacement for RBAC."""
    with pytest.raises(QueryRejected, match="not permitted for your role"):
        validate("SELECT * FROM public.customers", ALLOWED, qualifiers=PG)


def test_no_qualifiers_argument_behaves_exactly_as_before():
    """Callers without a connector in hand (blend, dashboards) are unchanged:
    the historical behaviour is that a prefix is not checked at all."""
    assert validate("SELECT * FROM secret_db.main.sales", ALLOWED)
    assert validate("SELECT * FROM sales", ALLOWED)
    assert validate("SELECT * FROM secret_db.main.sales", ALLOWED, qualifiers=None)


def test_base_tables_still_returns_bare_names():
    """Governance and chat key their rules on bare names; only validate() cares
    about the qualifier."""
    from app.queryguard import base_tables
    assert base_tables("SELECT * FROM public.sales JOIN acme.public.web_traffic ON 1=1") == {
        "sales", "web_traffic"}


# ── What each connector declares ────────────────────────────────────────

def test_demo_declares_no_namespace_and_rejects_every_qualified_reference():
    """One SQLite file has no schema, so ANY qualifier is outside it — the
    empty set must mean "no qualifier", never "no checking"."""
    quals = DemoConnector().qualifiers()
    assert quals == frozenset()
    assert validate("SELECT * FROM sales", ALLOWED, qualifiers=quals)
    rejected("SELECT * FROM main.sales", qualifiers=quals)
    rejected("SELECT * FROM other.sales", qualifiers=quals)


def test_a_prefix_is_matched_at_its_own_arity_only():
    """One part and two parts are different questions. `main` declared as a
    one-part schema must not answer a two-part `secret_db.main` prefix, and a
    declared `acme.public` must not answer a one-part `acme` or `public`... —
    the whole prefix matches, at its own arity, or nothing does."""
    rejected("SELECT * FROM secret_db.main.sales", qualifiers={"main"})
    assert validate("SELECT * FROM main.sales", ALLOWED, qualifiers={"main"})
    rejected("SELECT * FROM acme.sales", qualifiers={"acme.public"})
    assert validate("SELECT * FROM acme.public.sales", ALLOWED, qualifiers={"acme.public"})


def test_postgres_qualifiers_come_from_the_env_it_connects_with(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@host:5432/acme?sslmode=require")
    monkeypatch.setenv("POSTGRES_SCHEMA", "Analytics")
    # The CONFIGURED SPELLING, verbatim: list_tables() matches
    # information_schema.table_schema against it as written and the
    # search_path pin double-quotes it, so a lower-cased declaration would
    # vouch for a schema neither of those ever reaches.
    assert PostgresConnector().qualifiers() == frozenset({"Analytics", "acme.Analytics"})
    # key=value DSN form, and the documented default schema.
    monkeypatch.setenv("POSTGRES_DSN", "host=db.internal dbname=warehouse user=svc")
    monkeypatch.delenv("POSTGRES_SCHEMA", raising=False)
    assert PostgresConnector().qualifiers() == frozenset({"public", "warehouse.public"})
    # An unparseable DSN drops the db.schema spelling but never the schema.
    monkeypatch.setenv("POSTGRES_DSN", "???")
    assert PostgresConnector().qualifiers() == frozenset({"public"})


def test_snowflake_declares_the_schema_and_database_schema_only(monkeypatch):
    """ARITY is the rule. Snowflake resolves a two-part `x.sales` as
    SCHEMA.object, so the DATABASE name must not be declared as a one-part
    qualifier — it was, and `<database>.sales` was accepted as a schema
    reference to a namespace the catalog never described."""
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "ANALYTICS")
    monkeypatch.setenv("SNOWFLAKE_SCHEMA", "PUBLIC")
    quals = SnowflakeConnector().qualifiers()
    # UPPER, the name Snowflake STORES for an unquoted env value — the same
    # reading list_tables() uses (cfg["schema"].upper()).
    assert quals == frozenset({"PUBLIC", "ANALYTICS.PUBLIC"})
    rejected("SELECT * FROM analytics.sales", qualifiers=quals, dialect="snowflake",
             allowed=["SALES"])
    assert validate("SELECT * FROM public.sales", ["SALES"],
                    qualifiers=quals, dialect="snowflake")
    assert validate("SELECT * FROM analytics.public.sales", ["SALES"],
                    qualifiers=quals, dialect="snowflake")


def test_databricks_declares_the_schema_and_catalog_schema_only(monkeypatch):
    """Same ARITY rule as Snowflake. Unity Catalog / Spark read a two-part
    `x.sales` as SCHEMA.table in the current catalog, so the bare CATALOG must
    not be declared as a one-part qualifier — it was, and `main.sales` was
    accepted as a reference to a schema merely named after the catalog."""
    monkeypatch.setenv("DATABRICKS_CATALOG", "main")
    monkeypatch.setenv("DATABRICKS_SCHEMA", "sales_db")
    quals = DatabricksConnector().qualifiers()
    assert quals == frozenset({"sales_db", "main.sales_db"})
    rejected("SELECT * FROM main.sales", qualifiers=quals, dialect="databricks")
    assert validate("SELECT * FROM sales_db.sales", ALLOWED,
                    qualifiers=quals, dialect="databricks")
    assert validate("SELECT * FROM main.sales_db.sales", ALLOWED,
                    qualifiers=quals, dialect="databricks")


def test_bigquery_declares_dataset_and_project_dataset(monkeypatch):
    """BigQuery's arities are already right — one part is a DATASET in the
    default project, two are PROJECT.DATASET — but its ids are case-SENSITIVE,
    so the configured spelling is kept rather than lower-cased: `Analytics` and
    `analytics` are different datasets to BigQuery."""
    monkeypatch.setenv("BIGQUERY_PROJECT", "acme_prod")
    monkeypatch.setenv("BIGQUERY_DATASET", "Analytics")
    monkeypatch.delenv("BIGQUERY_CREDENTIALS_JSON", raising=False)
    quals = BigQueryConnector().qualifiers()
    assert quals == frozenset({"Analytics", "acme_prod.Analytics"})
    assert validate("SELECT * FROM Analytics.sales", ALLOWED,
                    qualifiers=quals, dialect="bigquery")
    assert validate("SELECT * FROM acme_prod.Analytics.sales", ALLOWED,
                    qualifiers=quals, dialect="bigquery")
    rejected("SELECT * FROM other_dataset.sales", qualifiers=quals, dialect="bigquery")
    # Two parts is PROJECT.DATASET, never DATASET.<something else>.
    rejected("SELECT * FROM Analytics.acme_prod.sales", qualifiers=quals,
             dialect="bigquery")
    monkeypatch.delenv("BIGQUERY_DATASET", raising=False)
    assert BigQueryConnector().qualifiers() == frozenset()


def test_postgres_pins_search_path_to_the_configured_schema(monkeypatch):
    """An UNQUALIFIED allowed name must only ever mean the configured schema.
    Without a pinned search_path it resolves to whichever schema comes first on
    the server's path — a relation the catalog never described, and one the
    guard cannot see (a bare name carries no namespace to check)."""
    import sys
    import types

    seen = {}

    fake = types.ModuleType("psycopg")
    fake.connect = lambda dsn, **kw: seen.update(dsn=dsn, **kw) or object()
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@host:5432/acme")
    monkeypatch.setenv("POSTGRES_SCHEMA", "analytics")

    conn = PostgresConnector()
    conn._conn()
    assert seen["options"] == '-c search_path="analytics"'
    assert seen["autocommit"] is True
    # The same schema qualifiers() reports, and quoted so PostgreSQL does not
    # re-fold the value into a different schema name.
    assert "analytics" in conn.qualifiers()
    # A schema that would need escaping is a misconfiguration: fail closed
    # rather than paste it into libpq's options string.
    monkeypatch.setenv("POSTGRES_SCHEMA", 'pub"lic x')
    with pytest.raises(ValueError, match="search_path"):
        PostgresConnector()._conn()


def test_every_registered_connector_declares_a_cheap_arity_keyed_namespace():
    """qualifiers() runs on every query, so it must never touch the network.
    Every prefix must be a non-empty dotted string of at most three parts —
    more than that is not a namespace any target engine can resolve."""
    from app.connectors import _REGISTRY
    for name, conn in _REGISTRY.items():
        quals = conn.qualifiers()
        assert isinstance(quals, frozenset), name
        for q in quals:
            parts = q.split(".")
            assert 1 <= len(parts) <= 3 and all(parts), (name, q)
        # A declared prefix is a CATALOG spelling — the name the engine
        # STORES — so each connector folds the env value the way its own
        # vendor does, and the guard compares it the way it compares an
        # allowlist entry. Snowflake stores an unquoted name UPPER; Databricks
        # (Spark) folds everything down; BigQuery and PostgreSQL store what was
        # written, so their declarations keep the configured spelling.
        if conn.dialect == "snowflake":
            assert all(q == q.upper() for q in quals), name
        elif conn.dialect not in ("bigquery", "postgres"):
            assert all(q == q.lower() for q in quals), name


def test_graph_and_api_sources_declare_no_namespace():
    """Cypher labels and in-memory report tables have no schema to qualify."""
    assert GraphConnector().qualifiers() == frozenset()
    from app.connectors.marketing import AlgoliaConnector
    assert AlgoliaConnector().qualifiers() == frozenset()
