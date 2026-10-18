"""Postgres metadata discovery against a REAL server — the catalog SQL is only
proven by running it (a text || "char" ambiguity shipped once because nothing
did). Skipped unless STUDIO_TEST_POSTGRES_DSN names a database this test may
create and drop a scratch schema in, e.g.
    STUDIO_TEST_POSTGRES_DSN=postgresql://studio@localhost:55432/scratch
"""
import os
import uuid

import pytest

DSN = os.getenv("STUDIO_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="STUDIO_TEST_POSTGRES_DSN not set")


@pytest.fixture()
def pg():
    import psycopg
    from app import connections
    schema = "mg_" + uuid.uuid4().hex[:10]
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(f"CREATE TABLE {schema}.retail_orders (order_id int, customer_id int, "
                      f"order_date date, status text)")
        admin.execute(f"CREATE TABLE {schema}.retail_customers (customer_id int, segment text)")
        admin.execute(f"CREATE VIEW {schema}.retail_sales AS SELECT o.order_id, c.segment "
                      f"FROM {schema}.retail_orders o JOIN {schema}.retail_customers c "
                      f"USING (customer_id)")
        conn = connections._DynPostgres("pg-test", {"dsn": DSN, "schema": schema})

        def run(sql):
            admin.execute(sql.replace("{s}", schema))
        try:
            yield conn, run
        finally:
            conn.close()
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def test_describe_relations_reads_kinds_and_typed_columns(pg):
    conn, _ = pg
    out = conn.describe_relations()
    assert out["retail_orders"]["kind"] == "table"
    assert out["retail_sales"]["kind"] == "view"
    assert out["retail_orders"]["columns"] == [
        {"name": "order_id", "type": "integer"}, {"name": "customer_id", "type": "integer"},
        {"name": "order_date", "type": "date"}, {"name": "status", "type": "text"}]


def test_the_change_signal_moves_with_every_kind_of_ddl(pg):
    conn, run = pg
    base = conn.change_signal()
    assert base and conn.change_signal() == base                 # stable when nothing changes
    previous = base
    for ddl in ("CREATE TABLE {s}.retail_promotions (promo_id int)",
                "ALTER TABLE {s}.retail_promotions ADD COLUMN starts_on date",
                "ALTER TABLE {s}.retail_promotions ALTER COLUMN promo_id TYPE bigint",
                "ALTER TABLE {s}.retail_promotions RENAME TO retail_offers",
                "DROP TABLE {s}.retail_offers"):
        run(ddl)
        signal = conn.change_signal()
        assert signal != previous, ddl
        previous = signal
    # It is a function of the schema: back where it started, the signal is too.
    assert previous == base
