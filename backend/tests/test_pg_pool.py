"""Postgres connection pool behind db._conn() (no Postgres needed).

A fake psycopg_pool + psycopg are injected into sys.modules so the pool code
path runs against counting stubs. Proves: close() returns the borrowed
connection with putconn(); sequential _conn() calls reuse one physical
connection; executescript commits; close() ends the implicit transaction
BEFORE returning the connection, so a read-only caller never hands the pool an
INTRANS connection; a connection dropped without close() is returned by the
__del__ safety net with a WARNING; pool_stats() reports the sizes; and when
psycopg_pool cannot be imported the direct-connect fallback is used and warns
exactly once.

Run from the backend directory:
    python -m pytest tests/test_pg_pool.py -q
"""
import gc
import logging
import sys
import types

import pytest

from app import db


# ── Stubs ────────────────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)


class FakeRawConn:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.log = []          # ordered lifecycle events, incl. "putconn"

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        return self

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.log.append("commit")

    def rollback(self):
        self.rollbacks += 1
        self.log.append("rollback")

    def close(self):
        self.closed = True
        self.log.append("close")


class FakePool:
    """Counts getconn/putconn; hands back the most recently returned connection
    so reuse is observable."""
    instances = []

    def __init__(self, conninfo, **kwargs):
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.min_size = kwargs.get("min_size")
        self.max_size = kwargs.get("max_size")
        self.getconn_calls = 0
        self.putconn_calls = 0
        self.free = []
        self.created = 0
        FakePool.instances.append(self)

    def getconn(self):
        self.getconn_calls += 1
        if self.free:
            return self.free.pop()
        self.created += 1
        return FakeRawConn()

    def putconn(self, conn):
        self.putconn_calls += 1
        conn.log.append("putconn")
        self.free.append(conn)

    def get_stats(self):
        return {"pool_size": self.created, "pool_available": len(self.free)}


def _fake_psycopg(connect_log):
    mod = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    def connect(dsn, **kw):
        connect_log.append((dsn, kw))
        return FakeRawConn()

    mod.connect = connect
    mod.rows = rows
    return mod, rows


@pytest.fixture()
def pg(monkeypatch):
    """Pretend DATABASE_URL points at Postgres, with fake driver modules and a
    pristine pool singleton."""
    connect_log = []
    mod, rows = _fake_psycopg(connect_log)
    monkeypatch.setitem(sys.modules, "psycopg", mod)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    pool_mod = types.ModuleType("psycopg_pool")
    pool_mod.ConnectionPool = FakePool
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    FakePool.instances.clear()

    monkeypatch.setattr(db, "IS_PG", True)
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://fake/studio")
    monkeypatch.setattr(db, "_POOL", None)
    monkeypatch.setattr(db, "_POOL_DISABLED", False)
    monkeypatch.setattr(db, "_POOL_WARNED", False)
    monkeypatch.setenv("STUDIO_PG_POOL_SIZE", "7")
    monkeypatch.setenv("STUDIO_PG_POOL_TIMEOUT", "2.5")
    return types.SimpleNamespace(connect_log=connect_log, mp=monkeypatch)


def _pool():
    assert len(FakePool.instances) == 1, "pool must be a lazily created singleton"
    return FakePool.instances[0]


# ── Tests ────────────────────────────────────────────────────────────────

def test_pool_is_lazy_and_configured_from_env(pg):
    assert FakePool.instances == []                       # nothing until first use
    assert db.pool_stats() is None
    c = db._conn()
    p = _pool()
    assert p.conninfo == "postgresql://fake/studio"
    assert p.kwargs["min_size"] == 1 and p.kwargs["max_size"] == 7
    assert p.kwargs["timeout"] == 2.5
    assert p.kwargs["kwargs"] == {"row_factory": sys.modules["psycopg.rows"].dict_row}
    c.close()
    assert pg.connect_log == []                           # never a direct connect


def test_close_returns_connection_and_sequential_calls_reuse_it(pg):
    c1 = db._conn()
    raw1 = c1._c
    c1.close()
    p = _pool()
    assert (p.getconn_calls, p.putconn_calls) == (1, 1)
    assert raw1.closed is False                           # returned, not closed

    c2 = db._conn()
    assert c2._c is raw1                                  # reused
    c2.close()
    assert (p.getconn_calls, p.putconn_calls, p.created) == (2, 2, 1)

    # close() twice returns once — a double putconn would corrupt the pool.
    c2.close()
    assert p.putconn_calls == 2


def test_close_ends_the_transaction_before_returning_the_connection(pg):
    """A read-only call commits nothing, and psycopg opens a transaction on the
    first execute: without an explicit rollback the connection went back
    INTRANS and the pool reset it itself (a warning + a round trip per call)."""
    c = db._conn()
    c.execute("SELECT 1 AS one")
    raw = c._c
    c.close()
    assert raw.log == ["rollback", "putconn"]    # idle BEFORE it is handed back
    assert raw.closed is False

    # A writer's commit stands; the trailing rollback is a no-op on an already
    # idle connection and must not discard anything.
    c2 = db._conn()
    c2.execute("INSERT INTO t VALUES (?)", (1,))
    c2.commit()
    c2.close()
    assert c2._c.log == ["rollback", "putconn", "commit", "rollback", "putconn"]
    assert c2._c.commits == 1

    c2.close()                                   # idempotent: no second reset
    assert _pool().putconn_calls == 2


def test_execute_translates_and_executescript_commits(pg):
    c = db._conn()
    c.execute("SELECT * FROM users WHERE id=?", ("u1",))
    assert c._c.executed[-1] == ("SELECT * FROM users WHERE id=%s", ("u1",))
    c.executescript("CREATE TABLE IF NOT EXISTS t (x REAL NOT NULL);")
    assert c._c.executed[-1] == "CREATE TABLE IF NOT EXISTS t (x DOUBLE PRECISION NOT NULL);"
    assert c._c.commits == 1
    c.commit()
    assert c._c.commits == 2
    c.rollback()
    assert c._c.rollbacks == 1
    c.close()


def test_dropped_without_close_is_returned_by_del(pg, caplog):
    caplog.set_level(logging.WARNING, logger="studio.db")
    c = db._conn()
    p = _pool()
    assert p.putconn_calls == 0
    del c
    gc.collect()
    assert p.putconn_calls == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "close()" in warnings[0].getMessage()
    # And it is really back in circulation.
    c2 = db._conn()
    assert p.created == 1
    c2.close()


def test_direct_connect_close_also_resets_first(pg):
    """No pool: the connection is really closed, but the transaction is still
    ended first so the server sees a clean session teardown."""
    pg.mp.setitem(sys.modules, "psycopg_pool", None)
    c = db._conn()
    c.execute("SELECT 1 AS one")
    c.close()
    assert c._c.log == ["rollback", "close"]


def test_pool_stats_reports_sizes(pg):
    c = db._conn()
    st = db.pool_stats()
    assert st["min_size"] == 1 and st["max_size"] == 7
    assert st["pool_size"] == 1 and st["pool_available"] == 0
    c.close()
    assert db.pool_stats()["pool_available"] == 1


def test_missing_psycopg_pool_falls_back_to_direct_connect_and_warns_once(pg, caplog):
    caplog.set_level(logging.WARNING, logger="studio.db")
    # None in sys.modules makes `import psycopg_pool` raise ImportError.
    pg.mp.setitem(sys.modules, "psycopg_pool", None)

    c1 = db._conn()
    c1.close()
    c2 = db._conn()
    c2.close()
    assert FakePool.instances == []
    assert len(pg.connect_log) == 2
    assert pg.connect_log[0][0] == "postgresql://fake/studio"
    assert pg.connect_log[0][1] == {"row_factory": sys.modules["psycopg.rows"].dict_row}
    assert c1._c.closed and c2._c.closed                  # direct path closes for real
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "psycopg_pool" in warnings[0].getMessage()
    assert db.pool_stats() is None


def test_sqlite_path_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "plain.db"))
    c = db._conn()
    assert c.execute("SELECT 1 AS one").fetchone()["one"] == 1
    c.close()
    assert db.pool_stats() is None
