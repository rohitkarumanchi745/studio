"""PostgreSQL data source — registered, RBAC-granted, dormant until configured.

The live part runs only when POSTGRES_DSN points at a reachable database
(e.g. a local `pg_ctl` instance); otherwise it is skipped, never failed.

Run from the backend directory:
    python -m pytest tests/test_postgres_source.py -q
    POSTGRES_DSN=postgresql://studio@localhost:5433/warehouse python -m pytest tests/test_postgres_source.py -q
"""
import os

import pytest

from app import rbac
from app.connectors import all_sources, get_connector
from app.connectors.base import unguarded


def test_registered_and_granted_but_dormant_without_dsn(monkeypatch):
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    names = [s["name"] for s in all_sources()]
    assert "postgres" in names
    conn = get_connector("postgres")
    assert conn.dialect == "postgres" and conn.configured() is False   # dormant
    assert "postgres" in rbac.allowed_sources("admin")
    assert "postgres" in rbac.allowed_sources("analyst")
    assert "postgres" not in rbac.allowed_sources("viewer")           # fail closed


def test_live_roundtrip_when_a_dsn_is_present():
    dsn = os.getenv("POSTGRES_DSN")
    if not dsn:
        pytest.skip("POSTGRES_DSN not set — no live Postgres to talk to")
    conn = get_connector("postgres")
    assert conn.configured()
    tables = conn.list_tables()
    assert tables, "expected at least one table in the configured schema"
    t = tables[0]
    cols = conn.get_schema(t)
    assert cols and {"name", "type"} <= set(cols[0])
    # Direct connector execution is a self-test here; app code goes through
    # gateway.execute, which is the only place the guard opens on its own.
    with unguarded():
        columns, rows = conn.run_query(f"SELECT * FROM {t} LIMIT 3")
    assert columns == [c["name"] for c in cols] and len(rows) <= 3
