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


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    supervisor.init_tables()


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
    assert out["status"] == "succeeded"
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


def test_trigger_failure_retries_then_escalates(airflow_env):
    job = _submit(ANALYST, dag_id="forbidden")
    before = len(_platform_traces())
    out = approve(job["id"], user=ADMIN)
    assert out["status"] == "escalated"
    assert out["attempts"] == supervisor.MAX_RETRIES + 1
    assert "403" in out["last_error"]
    assert len(_platform_traces()) == before  # failed trigger -> no rollout


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
    assert out["job"]["status"] == "succeeded"  # non-terminal poll: no flip

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
