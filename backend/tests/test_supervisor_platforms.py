"""Supervisor platform_run tests — role policy, human approval, trigger, and
the /live feedback loop. No network: a threading http.server emulates Airflow
(the same mock pattern as test_platforms.py) and env vars point the adapter at
127.0.0.1. Run from the backend directory:

    python -m pytest tests/test_supervisor_platforms.py -q
"""
import json
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

# Point the app at a throwaway SQLite file BEFORE app.db computes DB_PATH.
os.environ["STUDIO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="studio-supervisor-test-"), "studio.db")

import pytest
from fastapi import HTTPException

from app import db, email_service, supervisor
from app.supervisor import (SubmitIn, approve, list_platforms, live_job,
                            submit_job)

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}
VIEWER = {"id": "u-viewer", "email": "view@studio.test", "role": "viewer", "name": "View"}

SCRIPT = {}  # scripted mock responses, mutated per test

TASK_INSTANCES = {"task_instances": [
    {"task_id": "extract", "state": "success", "duration": 12.5, "try_number": 1},
    {"task_id": "load", "state": "running", "duration": None, "try_number": 1},
], "total_entries": 2}


class Handler(BaseHTTPRequestHandler):
    """Airflow-shaped mock: trigger + dag-run status + task instances."""

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlsplit(self.path).path
        if re.fullmatch(r"/api/v1/dags/[^/]+/dagRuns", path):
            dag_id = unquote(path.split("/")[4])
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if dag_id == "forbidden":
                return self._send(403, {"detail": "User lacks DAG-level access"})
            return self._send(200, {"dag_run_id": body.get("dag_run_id"),
                                    "dag_id": dag_id, "state": "queued"})
        self._send(404, {"detail": f"no mock route for POST {path}"})

    def do_GET(self):
        path = urlsplit(self.path).path
        if re.search(r"/dagRuns/[^/]+/taskInstances$", path):
            return self._send(200, TASK_INSTANCES)
        if re.search(r"/dagRuns/[^/]+$", path):
            return self._send(200, {"state": SCRIPT.get("state", "queued"),
                                    "run_type": "manual"})
        self._send(404, {"detail": f"no mock route for GET {path}"})


def _persist_user(user):
    with db.connect() as c:
        c.execute(
            "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (user["id"], user["email"], db.UNUSABLE_PASSWORD_HASH, user["name"],
             user["role"], 1, time.time()))
        c.commit()


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    supervisor.init_tables()
    for user in (ADMIN, ANALYST, VIEWER):
        _persist_user(user)


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No scripted state bleed, no LLM review, no outbox files."""
    SCRIPT.clear()
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: {"mode": "test"})


@pytest.fixture
def airflow_env(base, monkeypatch):
    for v in ("AIRFLOW_USERNAME", "AIRFLOW_PASSWORD", "AIRFLOW_API_VERSION"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AIRFLOW_URL", base)
    monkeypatch.setenv("AIRFLOW_TOKEN", "tok")
    return monkeypatch


def _submit(user, dag_id="etl_daily", target="airflow"):
    return submit_job(SubmitIn(kind="platform_run", target=target,
                               script=json.dumps({"dag_id": dag_id})), user=user)


def _platform_traces():
    c = db._conn()
    rows = c.execute("SELECT * FROM agent_traces WHERE mode='platform_run' "
                     "ORDER BY created_at").fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── Submission policy ───────────────────────────────────────────────────

def test_submit_validation():
    with pytest.raises(HTTPException) as e:
        submit_job(SubmitIn(kind="bogus", target="airflow", script="{}"), user=ANALYST)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        submit_job(SubmitIn(kind="platform_run", target="airflow",
                            script="not json"), user=ANALYST)
    assert e.value.status_code == 400
    assert "JSON" in e.value.detail


def test_viewer_platform_run_rejected(airflow_env):
    job = _submit(VIEWER)
    assert job["status"] == "rejected"
    assert job["supervisor_decision"] == "reject"
    assert job["risk"] == "job"
    assert any("role" in r for r in job["supervisor_reasons"])


def test_unknown_platform_rejected(airflow_env):
    job = _submit(ANALYST, target="jenkins")
    assert job["status"] == "rejected"
    assert any("Unknown platform" in r for r in job["supervisor_reasons"])


def test_unconfigured_platform_rejected(monkeypatch):
    for v in ("AIRFLOW_URL", "AIRFLOW_TOKEN", "AIRFLOW_USERNAME", "AIRFLOW_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    job = _submit(ANALYST)
    assert job["status"] == "rejected"
    assert any("not configured" in r for r in job["supervisor_reasons"])


def test_analyst_waits_for_human(airflow_env):
    job = _submit(ANALYST)
    assert job["status"] == "awaiting_approval"
    assert job["supervisor_decision"] == "needs_human"
    assert job["risk"] == "job"
    assert job["result"] is None  # nothing ran yet


# ── Approval → trigger → rollout ────────────────────────────────────────

def test_admin_approval_triggers_and_stores_run_ref(airflow_env):
    job = _submit(ANALYST)
    before = len(_platform_traces())

    out = approve(job["id"], user=ADMIN)
    assert out["status"] == "running"   # trigger-success; /live owns the terminal state
    assert out["human_by"] == "admin@studio.test"
    res = out["result"]
    assert res["run_ref"].startswith("etl_daily:studio__")
    assert "etl_daily" in res["url"]

    traces = _platform_traces()
    assert len(traces) == before + 1  # exactly one rollout per trigger
    t = traces[-1]
    assert t["prompt"] == "platform:airflow"
    assert t["source"] == "airflow"
    assert t["reward"] is None and t["reward_source"] == "pending"
    meta = json.loads(t["meta"])
    assert meta["agents"] == ["airflow executor"]
    assert meta["run_ref"] == res["run_ref"]


def test_trigger_failure_escalates_without_automatic_retry(airflow_env):
    job = _submit(ANALYST, dag_id="forbidden")
    before = len(_platform_traces())
    out = approve(job["id"], user=ADMIN)
    assert out["status"] == "escalated"
    assert out["attempts"] == 1
    assert "uncertain" in out["last_error"]
    assert "403" in out["last_error"]
    assert len(_platform_traces()) == before  # failed trigger -> no rollout


def test_platform_request_id_is_idempotent_after_approval(airflow_env, monkeypatch):
    sent = []
    monkeypatch.setattr(email_service, "send", lambda *a, **k: sent.append(a))
    jid = str(uuid.uuid4())
    script = json.dumps({"dag_id": "etl_daily"})
    first = supervisor.submit("platform_run", "airflow", script, ANALYST, job_id=jid)
    second = supervisor.submit("platform_run", "airflow", script, ANALYST, job_id=jid)
    assert first == second
    assert len(sent) == 1
    out = approve(jid, user=ADMIN)
    replay = supervisor.submit("platform_run", "airflow", script, ANALYST, job_id=jid)
    assert replay["status"] == "running"
    assert json.loads(replay["result"]) == out["result"]
    assert len(sent) == 1


def test_platform_submission_can_suppress_email(airflow_env, monkeypatch):
    monkeypatch.setattr(email_service, "send", lambda *a, **k: pytest.fail("sent email"))
    out = supervisor.submit("platform_run", "airflow", '{"dag_id":"etl_daily"}',
                            ANALYST, job_id=str(uuid.uuid4()), notify=False)
    assert out["status"] == "awaiting_approval"


@pytest.mark.parametrize("changed", ["user", "target", "script"])
def test_request_id_cannot_be_reused_for_different_request(airflow_env, changed):
    jid = str(uuid.uuid4())
    script = '{"dag_id":"etl_daily"}'
    supervisor.submit("platform_run", "airflow", script, ANALYST, job_id=jid, notify=False)
    with pytest.raises(HTTPException) as e:
        supervisor.submit("platform_run", "dbt_cloud" if changed == "target" else "airflow",
                          '{}' if changed == "script" else script,
                          ADMIN if changed == "user" else ANALYST, job_id=jid)
    assert e.value.status_code == 409


def test_nonplatform_cannot_supply_request_id():
    with pytest.raises(HTTPException) as e:
        supervisor.submit("sql_script", "demo", "SELECT 1", ANALYST, job_id="cannot-replay")
    assert e.value.status_code == 400


def test_reclaimed_worker_cannot_submit(airflow_env, monkeypatch):
    def lost():
        raise supervisor.jobs.ClaimLost("reclaimed")
    monkeypatch.setattr(supervisor.jobs, "check_claim", lost)
    jid = str(uuid.uuid4())
    with pytest.raises(supervisor.jobs.ClaimLost):
        supervisor.submit("platform_run", "airflow", '{"dag_id":"etl_daily"}',
                          ANALYST, job_id=jid, notify=False)
    assert supervisor._get(jid) is None


def test_concurrent_submissions_share_one_approval_and_email(airflow_env, monkeypatch):
    barrier = threading.Barrier(2)
    original = supervisor.supervise
    sent = []
    def together(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(supervisor, "supervise", together)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: sent.append(a))
    jid = str(uuid.uuid4())
    def submit_once():
        return supervisor.submit("platform_run", "airflow", '{"dag_id":"etl_daily"}',
                                 ANALYST, job_id=jid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [f.result() for f in [pool.submit(submit_once), pool.submit(submit_once)]]
    assert first == second
    assert len(sent) == 1


@pytest.mark.parametrize("other_decision", ["approve", "reject"])
def test_concurrent_human_decisions_only_one_wins(airflow_env, monkeypatch, other_decision):
    jid = _submit(ANALYST)["id"]
    barrier = threading.Barrier(2)
    original = supervisor._need_approver
    triggered = []
    def together(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(supervisor, "_need_approver", together)
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "trigger",
                        lambda payload: triggered.append(payload) or {"run_ref": "d:r", "url": None})
    def decide(fn):
        try:
            return fn(jid, user=ADMIN)
        except HTTPException as exc:
            return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(decide, approve),
                   pool.submit(decide, getattr(supervisor, other_decision))]]
    assert sum(r == 409 for r in results) == 1
    winner = next(r for r in results if isinstance(r, dict))
    assert len(triggered) == (1 if winner["status"] == "running" else 0)


def test_timeout_triggers_once_and_requires_explicit_reapproval(airflow_env, monkeypatch):
    calls = []
    def timeout(payload):
        calls.append(payload)
        raise TimeoutError("response lost")
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "trigger", timeout)
    jid = _submit(ANALYST)["id"]
    first = approve(jid, user=ADMIN)
    assert first["status"] == "escalated" and len(calls) == 1
    second = approve(jid, user=ADMIN)
    assert second["status"] == "escalated" and len(calls) == 2


def test_stale_escalated_revision_cannot_reapprove(airflow_env, monkeypatch):
    def timeout(payload):
        raise TimeoutError("response lost")
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "trigger", timeout)
    jid = _submit(ANALYST)["id"]
    approve(jid, user=ADMIN)
    old_revision = supervisor._get(jid)
    approve(jid, user=ADMIN)
    with pytest.raises(HTTPException) as e:
        supervisor._decide(old_revision, "running", ADMIN)
    assert e.value.status_code == 409


def test_execution_requires_human_approval(airflow_env, monkeypatch):
    jid = _submit(ANALYST)["id"]
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "trigger",
                        lambda payload: pytest.fail("unapproved trigger"))
    with pytest.raises(RuntimeError, match="human approver"):
        supervisor._execute(supervisor._get(jid))


def test_approval_rechecks_requester_role(airflow_env, monkeypatch):
    jid = _submit(ANALYST)["id"]
    monkeypatch.setattr(db, "get_user", lambda uid: {**ANALYST, "role": "viewer"})
    with pytest.raises(HTTPException) as e:
        approve(jid, user=ADMIN)
    assert e.value.status_code == 403
    assert supervisor._get(jid)["status"] == "awaiting_approval"


def test_deleted_requester_cannot_have_a_platform_run_approved(airflow_env, monkeypatch):
    user_id = str(uuid.uuid4())
    requester = {**ANALYST, "id": user_id, "email": f"{user_id}@studio.test"}
    _persist_user(requester)
    jid = _submit(requester)["id"]
    with db.connect() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))
        c.commit()
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "trigger",
                        lambda payload: pytest.fail("Deleted requester's job was triggered"))
    with pytest.raises(HTTPException) as e:
        approve(jid, user=ADMIN)
    assert e.value.status_code == 403
    stored = supervisor._get(jid)
    assert stored["status"] == "awaiting_approval"
    assert stored["human_by"] is None


# ── /jobs/platforms picker ──────────────────────────────────────────────

def test_platforms_picker_role_gate(airflow_env):
    with pytest.raises(HTTPException) as e:
        list_platforms(user=VIEWER)
    assert e.value.status_code == 403
    rows = list_platforms(user=ANALYST)
    assert {r["name"] for r in rows} == {"airflow", "databricks_jobs",
                                         "dbt_cloud", "k8s_spark"}
    assert next(r for r in rows if r["name"] == "airflow")["configured"] is True


# ── /jobs/{id}/live feedback loop ───────────────────────────────────────

def test_live_visibility_owner_or_admin(airflow_env):
    jid = approve(_submit(ANALYST)["id"], user=ADMIN)["id"]
    with pytest.raises(HTTPException) as e:
        live_job(jid, user=VIEWER)  # neither owner nor admin -> no oracle
    assert e.value.status_code == 404
    assert live_job(jid, user=ADMIN)["job"]["id"] == jid


def test_live_polls_persists_and_flips_failed(airflow_env):
    jid = approve(_submit(ANALYST)["id"], user=ADMIN)["id"]

    SCRIPT["state"] = "running"
    out = live_job(jid, user=ANALYST)
    assert out["state"] == "running"
    assert "extract" in out["logs"] and "load" in out["logs"]
    assert {q["name"]: q["status"] for q in out["quality"]} == {
        "extract": "pass", "load": "pending"}
    res = out["job"]["result"]
    assert res["state"] == "running"          # persisted merge...
    assert res["run_ref"].startswith("etl_daily:")  # ...kept the run_ref
    assert out["job"]["status"] == "running"  # non-terminal poll: no flip

    before = len(_platform_traces())
    SCRIPT["state"] = "failed"
    out = live_job(jid, user=ANALYST)
    assert out["state"] == "failed"
    assert out["job"]["status"] == "failed"
    assert out["job"]["last_error"]
    assert len(_platform_traces()) == before  # terminal poll adds no 2nd trace

    # and the flip is persisted, not just in the response
    assert supervisor._get(jid)["status"] == "failed"


def test_live_succeeded_stays_succeeded(airflow_env):
    jid = approve(_submit(ANALYST)["id"], user=ADMIN)["id"]
    SCRIPT["state"] = "success"
    out = live_job(jid, user=ANALYST)
    assert out["state"] == "succeeded"
    assert out["job"]["status"] == "succeeded"


def test_live_non_platform_job_reports_stored_state(airflow_env):
    job = submit_job(SubmitIn(kind="sql_script", target="demo",
                              script="DELETE FROM sales"), user=ANALYST)
    assert job["status"] == "awaiting_approval"  # a write needs a human
    out = live_job(job["id"], user=ANALYST)
    assert out["state"] == "awaiting_approval"
    assert out["logs"] == "" and out["quality"] == [] and out["metrics"] == {}


def test_live_platform_job_without_run_ref(airflow_env):
    job = _submit(ANALYST)  # awaiting approval -> no run_ref yet
    out = live_job(job["id"], user=ANALYST)
    assert out["state"] == "awaiting_approval"
    assert out["logs"] == "" and out["quality"] == []
