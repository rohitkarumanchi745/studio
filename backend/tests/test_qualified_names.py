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


def rejected(sql, allowed=ALLOWED, qualifiers=PG, match="outside the configured namespace"):
    with pytest.raises(QueryRejected, match=match):
        validate(sql, allowed, qualifiers=qualifiers)


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


def test_postgres_qualifiers_come_from_the_env_it_connects_with(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@host:5432/acme?sslmode=require")
    monkeypatch.setenv("POSTGRES_SCHEMA", "Analytics")
    assert PostgresConnector().qualifiers() == frozenset({"analytics", "acme.analytics"})
    # key=value DSN form, and the documented default schema.
    monkeypatch.setenv("POSTGRES_DSN", "host=db.internal dbname=warehouse user=svc")
    monkeypatch.delenv("POSTGRES_SCHEMA", raising=False)
    assert PostgresConnector().qualifiers() == frozenset({"public", "warehouse.public"})
    # An unparseable DSN drops the db.schema spelling but never the schema.
    monkeypatch.setenv("POSTGRES_DSN", "???")
    assert PostgresConnector().qualifiers() == frozenset({"public"})


def test_snowflake_and_databricks_declare_database_catalog_and_schema(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "ANALYTICS")
    monkeypatch.setenv("SNOWFLAKE_SCHEMA", "PUBLIC")
    assert SnowflakeConnector().qualifiers() == frozenset(
        {"public", "analytics", "analytics.public"})
    monkeypatch.setenv("DATABRICKS_CATALOG", "main")
    monkeypatch.setenv("DATABRICKS_SCHEMA", "sales_db")
    assert DatabricksConnector().qualifiers() == frozenset(
        {"sales_db", "main", "main.sales_db"})


def test_bigquery_declares_dataset_and_project_dataset(monkeypatch):
    monkeypatch.setenv("BIGQUERY_PROJECT", "acme_prod")
    monkeypatch.setenv("BIGQUERY_DATASET", "Analytics")
    monkeypatch.delenv("BIGQUERY_CREDENTIALS_JSON", raising=False)
    quals = BigQueryConnector().qualifiers()
    assert quals == frozenset({"analytics", "acme_prod.analytics"})
    assert validate("SELECT * FROM analytics.sales", ALLOWED, qualifiers=quals)
    rejected("SELECT * FROM other_dataset.sales", qualifiers=quals)
    monkeypatch.delenv("BIGQUERY_DATASET", raising=False)
    assert BigQueryConnector().qualifiers() == frozenset()


def test_every_registered_connector_declares_a_cheap_namespace():
    """qualifiers() runs on every query, so it must never touch the network —
    and every connector must answer with a set of lowercase strings."""
    from app.connectors import _REGISTRY
    for name, conn in _REGISTRY.items():
        quals = conn.qualifiers()
        assert isinstance(quals, frozenset), name
        assert all(q == q.lower() for q in quals), name


def test_graph_and_api_sources_declare_no_namespace():
    """Cypher labels and in-memory report tables have no schema to qualify."""
    assert GraphConnector().qualifiers() == frozenset()
    from app.connectors.marketing import AlgoliaConnector
    assert AlgoliaConnector().qualifiers() == frozenset()
