"""Autopilot agents — backend behavior + security invariants.

Proves, with TestClient + monkeypatch (no LLM key, so the deterministic read
path is exercised):
  - create an agent scoped to demo; a MANUAL run executes as the owner and
    returns a chart-shaped result, recording a run;
  - a THRESHOLD rule fires only when it crosses (false->true), not on repeats;
  - an owner cannot create or run an agent scoped to a source their role can't
    reach (403 at create; the RBAC re-check shrinks a run to 'failed');
  - a risky action becomes a needs_human supervised job via supervisor.submit
    (monkeypatched to assert it is CALLED, never executed autonomously);
  - a disabled agent does not fire in a ticker pass.

Run from the backend directory:
    python -m pytest tests/test_autopilot.py -q
"""
import warnings

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "autopilot.db"))
    # No LLM key: exercise the dormant-safe read path (deterministic fallback).
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    # Disable the background ticker so tests drive runs deterministically.
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def _login(c, email, password):
    tok = c.post("/api/auth/login",
                 json={"email": email, "password": password}).json()["access_token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _admin(c):
    return _login(c, "admin@studio.local", "admin123")


def _viewer(c):
    return _login(c, "viewer@studio.local", "viewer123")


# ── CRUD + manual run ────────────────────────────────────────────────────

def test_create_and_manual_run_executes_as_owner(client):
    _admin(client)
    r = client.post("/api/autopilot", json={
        "name": "Daily sales pulse", "goal": "show total revenue by region",
        "trigger_type": "manual", "trigger_config": {},
        "source": "demo", "tables": ["sales"]})
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["trigger_type"] == "manual"
    assert agent["enabled"] is True
    assert "owner_id" not in agent
    aid = agent["id"]

    # Manual fire runs immediately as the owner and returns a chart-shaped result.
    run = client.post(f"/api/autopilot/{aid}/run").json()
    assert run["status"] == "done", run
    assert run["result"]["chart"] is not None
    assert run["result"]["columns"]
    assert run["trigger"] == "manual"

    # The run was recorded and is listable.
    runs = client.get(f"/api/autopilot/{aid}/runs").json()["runs"]
    assert len(runs) == 1
    assert runs[0]["status"] == "done"


def test_list_and_delete(client):
    _admin(client)
    aid = client.post("/api/autopilot", json={
        "name": "x", "goal": "count sales rows", "trigger_type": "manual",
        "source": "demo", "tables": ["sales"]}).json()["id"]
    assert any(a["id"] == aid for a in client.get("/api/autopilot").json()["agents"])
    assert client.delete(f"/api/autopilot/{aid}").json() == {"deleted": True}
    assert client.get(f"/api/autopilot/{aid}").status_code == 404


# ── Threshold: fire only on a cross ──────────────────────────────────────

def test_threshold_fires_only_when_crossed(client, monkeypatch):
    _admin(client)
    from app import autopilot
    aid = client.post("/api/autopilot", json={
        "name": "rev guard", "goal": "revenue check",
        "trigger_type": "threshold",
        "trigger_config": {"sql": "SELECT SUM(revenue) AS v FROM sales",
                           "column": "v", "op": "gt", "value": 0},
        "source": "demo", "tables": ["sales"]}).json()["id"]

    # Drive the metric deterministically so we control the cross.
    state = {"val": 5.0}   # starts ABOVE the threshold (op gt 0) -> crosses
    monkeypatch.setattr(autopilot, "_threshold_metric", lambda owner, ad: state["val"])

    d = autopilot._get_agent(aid)
    # First evaluation: false -> true, fires.
    run1 = autopilot._run_agent_now(d, "schedule")
    assert run1["status"] == "done", run1

    # Second evaluation, still above threshold (already latched true): no re-fire.
    d = autopilot._get_agent(aid)
    run2 = autopilot._run_agent_now(d, "schedule")
    assert run2["status"] == "skipped"

    # Drop below, then back above -> a fresh cross fires again.
    state["val"] = -1.0
    d = autopilot._get_agent(aid)
    assert autopilot._run_agent_now(d, "schedule")["status"] == "skipped"
    state["val"] = 9.0
    d = autopilot._get_agent(aid)
    assert autopilot._run_agent_now(d, "schedule")["status"] == "done"


# ── RBAC: never wider than the owner ─────────────────────────────────────

def test_viewer_cannot_create_agent_on_denied_source(client):
    _viewer(client)
    # Viewers have no access to snowflake at all -> 403 at create.
    r = client.post("/api/autopilot", json={
        "name": "nope", "goal": "peek", "trigger_type": "manual",
        "source": "snowflake"})
    assert r.status_code == 403

    # Viewers cannot reach the customers table in demo (PII) -> 403 at create.
    r2 = client.post("/api/autopilot", json={
        "name": "nope2", "goal": "peek", "trigger_type": "manual",
        "source": "demo", "tables": ["customers"]})
    assert r2.status_code == 403


def test_run_rechecks_rbac_after_demotion(client, monkeypatch):
    _admin(client)
    from app import autopilot, db
    aid = client.post("/api/autopilot", json={
        "name": "cust", "goal": "list customers", "trigger_type": "manual",
        "source": "demo", "tables": ["customers"]}).json()["id"]

    # Simulate the owner being demoted to viewer AFTER create: the run-time RBAC
    # re-check must deny the customers scope, failing the run (never widening).
    d = autopilot._get_agent(aid)
    owner_id = d["owner_id"]
    real_get_user = db.get_user
    monkeypatch.setattr(db, "get_user",
                        lambda uid: {**real_get_user(uid), "role": "viewer"}
                        if uid == owner_id else real_get_user(uid))
    run = autopilot._run_agent_now(d, "manual")
    assert run["status"] == "failed"
    assert "RBAC" in (run["summary"] or "") or "access" in (run["summary"] or "")


# ── Act-with-approval: risky action becomes a needs_human job ─────────────

def test_risky_action_goes_through_supervisor_not_executed(client, monkeypatch):
    _admin(client)
    from app import autopilot
    from app import supervisor

    aid = client.post("/api/autopilot", json={
        "name": "writer", "goal": "revenue by region",
        "trigger_type": "manual", "source": "demo", "tables": ["sales"],
        "trigger_config": {"action": {
            "kind": "sql_script", "target": "demo",
            "script": "UPDATE sales SET revenue = 0"}}}).json()["id"]

    calls = {}

    def fake_submit(kind, target, script, user):
        calls["args"] = (kind, target, script)
        calls["user_role"] = user["role"]
        # Mimic supervisor: a write becomes an awaiting_approval job, NOT run.
        return {"id": "job-123", "status": "awaiting_approval"}

    monkeypatch.setattr(supervisor, "submit", fake_submit)

    run = autopilot._run_row_from_dict(autopilot._run_agent_now(
        autopilot._get_agent(aid), "manual"))
    # supervisor.submit was CALLED with the write, and never executed by us.
    assert calls["args"] == ("sql_script", "demo", "UPDATE sales SET revenue = 0")
    assert run["proposed_job_id"] == "job-123"
    assert run["result"]["proposed_job_id"] == "job-123"
    # The demo table was NOT mutated — the write never ran autonomously.
    from app.catalog import _connector_or_400
    from app.connectors.base import unguarded
    with unguarded():      # test-only: app code reads via gateway.execute
        _cols, rows = _connector_or_400("demo").run_query(
            "SELECT COUNT(*) FROM sales WHERE revenue != 0")
    assert rows[0][0] > 0


# ── Disabled agents never fire ───────────────────────────────────────────

def test_disabled_agent_not_claimed_by_ticker(client):
    _admin(client)
    from app import autopilot
    aid = client.post("/api/autopilot", json={
        "name": "sched", "goal": "count sales", "trigger_type": "schedule",
        "trigger_config": {"interval_seconds": 60},
        "source": "demo", "tables": ["sales"]}).json()["id"]
    client.post(f"/api/autopilot/{aid}/disable")

    # Force it due, then tick: a disabled agent is excluded by the claim WHERE.
    autopilot._update_fields(aid, {"next_run_at": 1.0})
    autopilot._tick()
    assert client.get(f"/api/autopilot/{aid}/runs").json()["runs"] == []


def test_enabled_scheduled_agent_runs_in_tick(client):
    _admin(client)
    from app import autopilot
    aid = client.post("/api/autopilot", json={
        "name": "sched2", "goal": "count sales rows", "trigger_type": "schedule",
        "trigger_config": {"interval_seconds": 60},
        "source": "demo", "tables": ["sales"]}).json()["id"]
    autopilot._update_fields(aid, {"next_run_at": 1.0})   # make it due now
    autopilot._tick()
    runs = client.get(f"/api/autopilot/{aid}/runs").json()["runs"]
    assert len(runs) == 1 and runs[0]["status"] == "done"
    # It was rescheduled into the future and the claim released.
    d = autopilot._get_agent(aid)
    assert d["next_run_at"] > 1.0 and d["claimed_at"] is None


# ── Ownership oracle: 404, not 403 ───────────────────────────────────────

def test_other_users_agent_is_404(client):
    _admin(client)
    aid = client.post("/api/autopilot", json={
        "name": "mine", "goal": "count sales", "trigger_type": "manual",
        "source": "demo", "tables": ["sales"]}).json()["id"]
    _viewer(client)   # switch identity
    assert client.get(f"/api/autopilot/{aid}").status_code == 404
    assert client.post(f"/api/autopilot/{aid}/run").status_code == 404


# ── trigger_config validation ────────────────────────────────────────────

def test_invalid_trigger_config_rejected(client):
    _admin(client)
    # schedule needs a bounded integer interval
    assert client.post("/api/autopilot", json={
        "name": "bad", "goal": "g", "trigger_type": "schedule",
        "trigger_config": {"interval_seconds": 5}, "source": "demo"}).status_code == 400
    # threshold needs metric_prompt or sql + a valid op
    assert client.post("/api/autopilot", json={
        "name": "bad2", "goal": "g", "trigger_type": "threshold",
        "trigger_config": {"op": "gt", "value": 1}, "source": "demo"}).status_code == 400
    # event needs a table the owner can access
    assert client.post("/api/autopilot", json={
        "name": "bad3", "goal": "g", "trigger_type": "event",
        "trigger_config": {"table": "no_such_table"}, "source": "demo"}).status_code == 400
