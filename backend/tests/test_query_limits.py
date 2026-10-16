"""Interactive reads have a time limit, are cancelled when it passes, and
cannot pile up on a source.

The gateway's timeout was off by default, and when set it only stopped
WAITING: the query kept running on the warehouse, and on Databricks — whose
connector runs one statement at a time on a shared connection — every caller
queued behind a runaway query timed out in turn and then ran its own query
anyway. Locked in here:
- unset, interactive reads get 300s and supervised-job reads none; an explicit
  STUDIO_QUERY_TIMEOUT_S applies to every read (0 = none);
- on expiry a cancellable connector's query is cancelled through the driver,
  and the error says so; a connector that cannot cancel is abandoned, and the
  error says that instead;
- per-source concurrency: a slot is held until the warehouse call really
  returns, so abandoned queries still count, and a read that finds no slot
  gets QueryBusy instead of joining the pile;
- Databricks: a timed-out statement is cancelled with cursor.cancel(), never
  retried on a fresh connection, and a statement still queued behind it when
  its caller gave up never runs;
- the worker sees the caller's context (progress binding and the like).
"""
import contextvars
import threading
import time
import types

import pytest

from app import connections, gateway
from app.connectors.base import QueryCancelled, current_cancel, unguarded


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("STUDIO_QUERY_TIMEOUT_S", "STUDIO_QUERY_CONCURRENCY", "STUDIO_QUERY_QUEUE_S"):
        monkeypatch.delenv(k, raising=False)
    gateway._slots.clear()
    yield
    gateway._slots.clear()


def _eventually(cond, within=3.0):
    end = time.time() + within
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


class _Slow:
    """A warehouse query that runs until released — or cancelled, when the
    connector supports cancelling."""

    def __init__(self, name="slow", cancellable=True):
        self.name, self.cancellable = name, cancellable
        self.release, self.cancels, self.finished = threading.Event(), 0, threading.Event()

    def run_query(self, sql):
        token = current_cancel()
        if self.cancellable:
            token.attach(self._cancel)
        try:
            self.release.wait(5)
            if token.cancelled and self.cancellable:
                raise RuntimeError("statement cancelled by the driver")
            return ["n"], [[1]]
        finally:
            self.finished.set()

    def _cancel(self):
        self.cancels += 1
        self.release.set()


# ── the limit ────────────────────────────────────────────────────────────

def test_interactive_reads_get_300s_and_supervised_jobs_none_by_default(monkeypatch):
    for purpose in ("agent_sql", "dashboard_tile", "rerun", "catalog_sample", "verify"):
        assert gateway._timeout_s(purpose) == 300
    assert gateway._timeout_s("supervised_read") == 0
    monkeypatch.setenv("STUDIO_QUERY_TIMEOUT_S", "45")
    assert gateway._timeout_s("agent_sql") == gateway._timeout_s("supervised_read") == 45
    monkeypatch.setenv("STUDIO_QUERY_TIMEOUT_S", "0")
    assert gateway._timeout_s("agent_sql") == 0                  # the operator said none


def test_a_cancellable_query_is_cancelled_when_its_time_runs_out(monkeypatch):
    monkeypatch.setenv("STUDIO_QUERY_TIMEOUT_S", "0.2")
    conn = _Slow()
    with pytest.raises(gateway.QueryTimeout) as e:
        gateway._run(conn, "SELECT 1", "agent_sql")
    assert "exceeded 0.2s and was cancelled" in str(e.value)
    assert conn.cancels == 1
    assert _eventually(conn.finished.is_set)                      # the warehouse call ended
    assert _eventually(lambda: gateway._slot("slow")._value == 8)  # and gave its slot back


def test_one_that_cannot_cancel_is_abandoned_and_keeps_its_slot_until_it_ends(monkeypatch):
    monkeypatch.setenv("STUDIO_QUERY_TIMEOUT_S", "0.2")
    monkeypatch.setenv("STUDIO_QUERY_CONCURRENCY", "1")
    monkeypatch.setenv("STUDIO_QUERY_QUEUE_S", "0.2")
    stuck = _Slow(cancellable=False)
    with pytest.raises(gateway.QueryTimeout) as e:
        gateway._run(stuck, "SELECT 1", "agent_sql")
    assert "abandoned (this source cannot cancel a running query)" in str(e.value)
    # Still running on the warehouse: the next read finds no slot and says so,
    # instead of queueing behind it.
    with pytest.raises(gateway.QueryBusy) as busy:
        gateway._run(_Slow(cancellable=False), "SELECT 2", "agent_sql")
    assert "slow is busy" in str(busy.value)
    stuck.release.set()                                           # the warehouse returns
    assert _eventually(lambda: gateway._slot("slow")._value == 1)
    quick = _Slow()
    quick.release.set()
    assert gateway._run(quick, "SELECT 3", "agent_sql") == (["n"], [[1]])


def test_a_read_without_a_limit_still_takes_a_slot(monkeypatch):
    monkeypatch.setenv("STUDIO_QUERY_CONCURRENCY", "1")
    monkeypatch.setenv("STUDIO_QUERY_QUEUE_S", "0.2")
    seen = []

    class Peek:
        name = "peek"

        def run_query(self, sql):
            seen.append(gateway._slot("peek")._value)
            return ["n"], [[1]]

    assert gateway._run(Peek(), "SELECT 1", "supervised_read") == (["n"], [[1]])
    assert seen == [0] and gateway._slot("peek")._value == 1


def test_the_worker_sees_the_callers_context(monkeypatch):
    marker = contextvars.ContextVar("caller_marker", default=None)
    marker.set("bound-task-42")

    class Peek:
        name = "ctx"

        def run_query(self, sql):
            return ["v"], [[marker.get()]]

    assert gateway._run(Peek(), "SELECT 1", "agent_sql") == (["v"], [["bound-task-42"]])


# ── Databricks: cursor.cancel(), no retry, queued ones never start ───────

class _Cursor:
    def __init__(self, warehouse):
        self.w, self.description, self._cancelled = warehouse, [("n",)], threading.Event()

    def execute(self, sql, params=None):
        self.w.executed.append(sql)
        if self._cancelled.wait(5 if "slow" in sql else 0):
            raise RuntimeError("[CANCELLED] statement cancelled")

    def cancel(self):
        self.w.cancels += 1
        self._cancelled.set()

    def fetchmany(self, n):
        return [(1,)]


class _Warehouse:
    def __init__(self):
        self.executed, self.cancels, self.connections, self.closed = [], 0, 0, 0

    def connect(self):
        self.connections += 1
        return types.SimpleNamespace(cursor=lambda: _Cursor(self), close=self._close)

    def _close(self):
        self.closed += 1


def _databricks():
    conn = connections._DynDatabricks("dbx", {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", "catalog": "main", "schema": "sales"})
    wh = _Warehouse()
    conn._conn = wh.connect
    return conn, wh


def test_databricks_cancels_a_timed_out_statement_and_never_retries_it(monkeypatch):
    monkeypatch.setenv("STUDIO_QUERY_TIMEOUT_S", "0.2")
    conn, wh = _databricks()
    with pytest.raises(gateway.QueryTimeout) as e:
        gateway._run(conn, "SELECT slow FROM t", "agent_sql")
    assert "was cancelled" in str(e.value)
    assert _eventually(lambda: wh.cancels == 1 and wh.closed == 1)
    time.sleep(0.2)
    # Run once; not re-run on a reconnected session after the cancel.
    assert wh.executed == ["SELECT slow FROM t"] and wh.connections == 1


def test_a_statement_queued_behind_it_when_its_caller_gave_up_never_runs(monkeypatch):
    """Databricks runs one statement at a time per source; a caller that timed
    out while queued behind a runaway statement must not run later anyway."""
    monkeypatch.setenv("STUDIO_QUERY_TIMEOUT_S", "0.3")
    conn, wh = _databricks()
    errors = []

    def ask(sql):
        try:
            gateway._run(conn, sql, "agent_sql")
        except Exception as exc:
            errors.append(str(exc))

    first = threading.Thread(target=ask, args=("SELECT slow FROM t",))
    first.start()
    assert _eventually(lambda: wh.executed == ["SELECT slow FROM t"])
    ask("SELECT queued FROM t")                    # waits on the connection lock, times out
    first.join(5)
    assert len(errors) == 2 and all("exceeded" in e for e in errors)
    time.sleep(0.3)                                # the queued worker gets the lock now
    assert wh.executed == ["SELECT slow FROM t"]   # …and does not run its statement
    assert wh.closed == 1                          # only the cancelled one closed its session


def test_a_cancel_switch_pulled_early_stops_the_attach():
    from app.connectors.base import CancelToken
    token = CancelToken()
    assert token.cancel() is False                 # nothing attached yet: nothing to cancel
    with pytest.raises(QueryCancelled):
        token.attach(lambda: None)


def test_an_ordinary_databricks_read_is_unchanged(monkeypatch):
    conn, wh = _databricks()
    with unguarded():
        assert conn.run_query("SELECT quick FROM t") == (["n"], [[1]])
    assert wh.executed == ["SELECT quick FROM t"] and wh.cancels == 0
