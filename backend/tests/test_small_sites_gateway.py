"""The three small former direct-run_query sites now route through the
gateway: freshness probes, cache replay, and supervised reads.

Each pins the behaviour the migration must preserve — freshness never raises
and writes no audit rows; a cache replay is a silent miss on a denied table
and a real (audited) read on an allowed one; a supervised read runs as the
requester with governance applied and an audit row named after its purpose.

Run from the backend directory:
    python -m pytest tests/test_small_sites_gateway.py -q
"""
import json
import os
import tempfile
import time
import uuid

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-small-sites-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import db, freshness, gateway, governance, qcache, supervisor
from app.connectors.demo import seed

ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}
VIEWER = {"id": "u-viewer", "email": "view@studio.test", "role": "viewer", "name": "View"}

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
      max_rows: 5
"""


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    supervisor.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac():
    """Built-in policies and no governance doc unless a test loads one."""
    governance._STATE.update(doc=None, yaml="", source=None)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


def _audit_rows(user, action=None):
    return [r for r in db.list_activity(user["id"])
            if action is None or r["action"] == action]


# ── freshness ───────────────────────────────────────────────────────────

def test_freshness_for_source_viewer_sees_only_allowed_tables_and_no_audit():
    before = len(_audit_rows(VIEWER))
    out = freshness.for_source(VIEWER, "demo")
    assert out["source"] == "demo"
    names = sorted(t["table"] for t in out["tables"])
    assert names == ["sales", "web_traffic"]           # never customers
    for t in out["tables"]:
        assert not t.get("error"), t                 # both probes actually ran
        assert t["column"] and t["latest"] is not None and t["rows"] > 0
    # A metadata probe writes no audit rows — one per table would swamp the log.
    assert len(_audit_rows(VIEWER)) == before


def test_freshness_for_table_denied_never_raises_and_never_queries():
    r = freshness.for_table(VIEWER, "demo", "customers")
    assert r == {"table": "customers", "error": "no access"}
    r = freshness.for_table(ANALYST, "demo", "customers")   # analyst may: real probe
    assert r["column"] == "signup_date" and r["latest"] and r["rows"] > 0
    r = freshness.for_table(ANALYST, "demo", "no_such_table")
    assert r["table"] == "no_such_table" and "schema error" in r["error"]


def test_freshness_for_source_viewer_denied_source_is_403():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        freshness.for_source(VIEWER, "snowflake")
    assert e.value.status_code == 403


# ── qcache replay ───────────────────────────────────────────────────────

def test_qcache_exec_full_denied_table_is_none_and_allowed_returns_rows():
    assert qcache._exec_full(VIEWER, "demo", "SELECT * FROM customers") is None
    assert qcache._exec_full(VIEWER, "demo", "SELECT 1 FROM nope") is None

    before = len(_audit_rows(VIEWER, "cache_replay"))
    got = qcache._exec_full(VIEWER, "demo", "SELECT region, revenue FROM sales LIMIT 3")
    assert got is not None
    cols, rows = got
    assert [c.lower() for c in cols] == ["region", "revenue"] and len(rows) == 3
    # A replay IS a user read — it is audited under its own purpose.
    rows_after = _audit_rows(VIEWER, "cache_replay")
    assert len(rows_after) == before + 1 and rows_after[0]["ok"] == 1


# ── supervised reads ────────────────────────────────────────────────────

def _job(user, script, target="demo"):
    """A minimal sql_script job as submit() would persist it (the row must
    exist for the _save in _run; _execute itself only reads the dict)."""
    now = time.time()
    return {
        "id": str(uuid.uuid4()), "user_id": user["id"],
        "requester_role": user["role"], "requester_email": user["email"],
        "kind": "sql_script", "target": target, "script": script, "risk": "read",
        "supervisor_decision": "approve", "supervisor_reasons": json.dumps([]),
        "attempts": 0, "max_retries": supervisor.MAX_RETRIES, "last_error": None,
        "result": None, "human_by": None, "created_at": now, "updated_at": now,
        "status": "running",
    }


def test_supervised_read_audits_purpose_and_applies_governance(monkeypatch):
    governance._set(GOV_YAML, "test")
    assert governance.loaded()
    seen = []
    real = gateway.execute

    def spy(*a, **k):
        r = real(*a, **k)
        seen.append(r)
        return r
    monkeypatch.setattr(gateway, "execute", spy)

    before = len(_audit_rows(ANALYST, "supervised_read"))
    out = supervisor._execute(_job(ANALYST, "SELECT * FROM customers; SELECT region FROM sales LIMIT 2"))

    # Output shape is unchanged: one entry per statement, reads carry a count.
    assert [o["type"] for o in out] == ["read", "read"]
    assert out[0]["statement"].startswith("SELECT * FROM customers")
    assert out[0]["rows"] == 5 and out[1]["rows"] == 2      # governance max_rows cap
    # The masked / denied columns never reached the job.
    r = seen[0]
    lower = [c.lower() for c in r.columns]
    assert "name" not in lower
    assert all(row[lower.index("lifetime_value")] == "***" for row in r.rows)
    assert r.purpose == "supervised_read" and r.source == "demo"
    # Audited as the REQUESTER under the read's purpose, one row per statement.
    rows = _audit_rows(ANALYST, "supervised_read")
    assert len(rows) == before + 2
    assert all(a["ok"] == 1 and a["source"] == "demo" for a in rows[:2])


def test_supervised_read_denied_table_raises_and_is_audited_ok_false():
    from app.queryguard import QueryRejected
    before = len(_audit_rows(VIEWER, "supervised_read"))
    with pytest.raises(QueryRejected):
        supervisor._execute(_job(VIEWER, "SELECT * FROM customers"))
    rows = _audit_rows(VIEWER, "supervised_read")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0


def test_supervised_write_branch_stays_outside_the_gateway(monkeypatch):
    """The approved-write path calls the connector's run_script directly; the
    gateway must not see it. The demo sandbox refuses writes, which is the
    observable proof the write hook (not the gateway) was reached."""
    calls = []
    monkeypatch.setattr(gateway, "execute", lambda *a, **k: calls.append(a))
    with pytest.raises(PermissionError, match="read-only sandbox"):
        supervisor._execute(_job(ANALYST, "DELETE FROM sales"))
    assert calls == []
