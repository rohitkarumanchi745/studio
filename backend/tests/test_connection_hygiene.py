"""Every app-database connection is returned, on every path.

The production symptom was a stream of "connection not properly closed"
warnings: nearly every call site was written `c = db._conn(); ...; c.close()`,
which closes only on the happy path. Any exception in between — a constraint
violation, an HTTPException raised mid-function, a bad row — skipped the close.
On SQLite that leaked a file handle the GC reclaimed; on Postgres the
connection is BORROWED FROM A POOL and stays stranded while the traceback that
pins its frame is alive, so an error storm is pool exhaustion.

Three halves, so to speak. Static: no module under app/ calls _conn() outside a
`with` (the sweep is done and cannot silently regress). Runtime: db.connect()
puts the pooled connection back when the body raises. Regression: a real app
function that raises mid-block leaves the pool full.

Run from the backend directory:
    python -m pytest tests/test_connection_hygiene.py -q
"""
import ast
import gc
import logging
import os
import sys
import types

import pytest
from fastapi import HTTPException

from app import db, flow

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


# ── Static invariant ─────────────────────────────────────────────────────

# db.py owns the raw factory and defines connect() on top of it, so its own
# _conn() uses are the definition, not a call site.
SELF_EXEMPT_FILE = "db.py"

# Connector classes that define their OWN _conn(): a warehouse / object-store
# session (sqlite3, duckdb, psycopg to the CUSTOMER's database, a Databricks or
# Snowflake cursor), NOT the app database, and nothing to do with db.connect().
# Named one by one rather than skipped as a package so the exemption stays
# auditable: the test below asserts each entry really is a Connector subclass
# that defines _conn itself, and only `self._conn()` inside such a class is
# exempt — a db._conn() anywhere in these files is still an offence.
CONNECTOR_OWN_CONN = {
    ("connectors/demo.py", "DemoConnector"),               # sqlite3 demo warehouse
    ("connectors/objectstore.py", "ObjectStoreConnector"),  # duckdb over object storage
    ("connectors/postgres_conn.py", "PostgresConnector"),   # customer Postgres
    ("connectors/databricks_conn.py", "DatabricksConnector"),
    ("connectors/snowflake_conn.py", "SnowflakeConnector"),
}


def _app_files():
    for root, _dirs, files in os.walk(APP_DIR):
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _rel(path):
    return os.path.relpath(path, APP_DIR).replace(os.sep, "/")


def _parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


def _is_conn_call(node):
    """`_conn()` or `<anything>._conn()` — a call, not the definition."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    return (isinstance(f, ast.Name) and f.id == "_conn") or \
           (isinstance(f, ast.Attribute) and f.attr == "_conn")


def _guarded_call_ids(tree):
    """Calls that ARE the context expression of a with statement."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    out.add(id(item.context_expr))
    return out


def _connector_own_call_ids(tree, rel):
    """ids of `self._conn()` calls inside an allowlisted connector class."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or (rel, node.name) not in CONNECTOR_OWN_CONN:
            continue
        for sub in ast.walk(node):
            if _is_conn_call(sub) and isinstance(sub.func, ast.Attribute) \
                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "self":
                out.add(id(sub))
    return out


def test_no_conn_call_outside_a_with_statement():
    """`with db.connect() as c:` everywhere: the connection comes back even
    when the body raises. Every offender is listed file:line, so a regression
    is one glance to fix."""
    # The allowlist first — an exemption that no longer describes the code
    # would silently widen the scan's blind spot.
    seen = set()
    for path in _app_files():
        rel = _rel(path)
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ClassDef) or (rel, node.name) not in CONNECTOR_OWN_CONN:
                continue
            bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
            assert "Connector" in bases, f"{rel}:{node.name} is not a Connector subclass"
            assert any(isinstance(b, ast.FunctionDef) and b.name == "_conn" for b in node.body), \
                f"{rel}:{node.name} no longer defines its own _conn()"
            seen.add((rel, node.name))
    assert seen == CONNECTOR_OWN_CONN, \
        f"stale connector _conn allowlist entries: {sorted(CONNECTOR_OWN_CONN - seen)}"

    offenders = []
    for path in _app_files():
        rel = _rel(path)
        if rel == SELF_EXEMPT_FILE:
            continue
        tree = _parse(path)
        exempt = _guarded_call_ids(tree) | _connector_own_call_ids(tree, rel)
        for node in ast.walk(tree):
            if _is_conn_call(node) and id(node) not in exempt:
                offenders.append(f"app/{rel}:{node.lineno}")
    assert not offenders, (
        "_conn() called outside a `with` — use `with db.connect() as c:` so the "
        "connection is returned when the body raises:\n  " + "\n  ".join(sorted(offenders))
    )


# ── Pooled Postgres stubs (same approach as tests/test_pg_pool.py) ───────

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append(sql)


class FakeResult:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeRawConn:
    """SCRIPT is the result sets execute() hands back, in order — enough to
    drive a real app function that reads a row and then raises."""
    SCRIPT = []

    def __init__(self):
        self.executed = []
        self.commits = 0
        self.closed = False
        self.log = []
        self.next_rows = [list(r) for r in FakeRawConn.SCRIPT]

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))
        return FakeResult(self.next_rows.pop(0) if self.next_rows else [])

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def close(self):
        self.closed = True
        self.log.append("close")


class FakePool:
    instances = []

    def __init__(self, conninfo, **kwargs):
        self.conninfo = conninfo
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


@pytest.fixture()
def pg(monkeypatch):
    """Pretend DATABASE_URL points at Postgres, with fake driver modules and a
    pristine pool singleton."""
    psycopg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    psycopg.connect = lambda dsn, **kw: FakeRawConn()
    psycopg.rows = rows
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
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
    return monkeypatch


def _pool():
    assert len(FakePool.instances) == 1, "pool must be a lazily created singleton"
    return FakePool.instances[0]


def _leak_warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "dropped without close()" in r.getMessage()]


# ── Runtime proof ────────────────────────────────────────────────────────

def test_connect_returns_the_pooled_connection_whether_or_not_the_body_raises(pg, caplog):
    """The whole point of connect(): the finally runs while the exception is
    still propagating, so the connection is back in the pool before the caller
    ever sees the error — no reliance on __del__ and its warning."""
    caplog.set_level(logging.WARNING, logger="studio.db")

    with db.connect() as c:                       # clean body
        c.execute("SELECT 1 AS one")
    p = _pool()
    assert (p.getconn_calls, p.putconn_calls) == (1, 1)
    assert len(p.free) == p.created               # pool full again

    with pytest.raises(RuntimeError):             # raising body
        with db.connect() as c:
            c.execute("SELECT 1 AS one")
            raise RuntimeError("boom")
    assert (p.getconn_calls, p.putconn_calls) == (2, 2)
    assert len(p.free) == p.created

    # The traceback pins the frame holding `c`; collecting it must not trip the
    # __del__ safety net, because close() already ran.
    gc.collect()
    assert _leak_warnings(caplog) == []


# ── Regression: the real-world shape ─────────────────────────────────────

def test_app_function_raising_mid_block_leaves_the_pool_full(pg, caplog, monkeypatch):
    """flow.remove() reads the row, finds another user's flow and raises 404
    from INSIDE the block — the exact shape (read, then HTTPException) that used
    to strand a pooled connection for the life of the traceback."""
    caplog.set_level(logging.WARNING, logger="studio.db")
    monkeypatch.setattr(FakeRawConn, "SCRIPT", [[{"user_id": "someone-else"}]])

    with pytest.raises(HTTPException) as e:
        flow.remove("f-1", {"id": "u-owner", "email": "o@studio.test", "role": "analyst"})
    assert e.value.status_code == 404

    p = _pool()
    assert (p.getconn_calls, p.putconn_calls) == (1, 1)
    assert len(p.free) == p.created               # nothing stranded
    assert p.free[0].commits == 0                 # and the DELETE never ran
    gc.collect()
    assert _leak_warnings(caplog) == []
