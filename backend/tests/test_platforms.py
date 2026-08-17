"""Platform adapter tests — no network. A threading http.server emulates each
platform's API (trigger + status + logs + quality artifacts); env vars point
the adapters at 127.0.0.1. Run from the backend directory:

    python -m pytest tests/test_platforms.py -q
"""
import base64
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from app.platforms import PLATFORMS, all_platforms, get_platform

SCRIPT = {}   # scripted responses (states, error toggles) — mutated per test
CAPTURE = {}  # last request per route — asserted per test

TASK_INSTANCES = {"task_instances": [
    {"task_id": "extract", "state": "success", "duration": 12.5, "try_number": 1},
    {"task_id": "load", "state": "failed", "duration": 3.1, "try_number": 2},
    {"task_id": "notify", "state": "skipped", "duration": None, "try_number": 0},
], "total_entries": 3}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj, ctype="application/json"):
        body = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        path = urlsplit(self.path).path
        # Airflow: JWT exchange (v2 password flow)
        if path == "/auth/token":
            CAPTURE["airflow_login"] = self._body()
            return self._send(200, {"access_token": "jwt-from-login"})
        # Airflow: trigger DAG run (v1 and v2)
        if re.fullmatch(r"/api/v[12]/dags/[^/]+/dagRuns", path):
            dag_id = unquote(path.split("/")[4])
            body = self._body()
            CAPTURE["airflow_trigger"] = {"dag_id": dag_id, "body": body,
                                          "auth": self.headers.get("Authorization")}
            if dag_id == "forbidden":
                return self._send(403, {"detail": "User lacks DAG-level access to forbidden"})
            return self._send(200, {"dag_run_id": body.get("dag_run_id"),
                                    "dag_id": dag_id, "state": "queued"})
        # dbt Cloud: trigger job run (trailing slash per spec)
        if path == "/api/v2/accounts/42/jobs/7/run/":
            CAPTURE["dbt_trigger"] = {"body": self._body(),
                                      "auth": self.headers.get("Authorization")}
            return self._send(200, {"data": {
                "id": 123,
                "href": "https://cloud.getdbt.com/deploy/42/projects/9/runs/123/"},
                "status": {"code": 200}})
        # Databricks: runs/submit
        if path == "/api/2.1/jobs/runs/submit":
            CAPTURE["dbx_trigger"] = {"body": self._body(),
                                      "auth": self.headers.get("Authorization")}
            return self._send(200, {"run_id": 999})
        # K8s: create SparkApplication
        if path == "/apis/sparkoperator.k8s.io/v1beta2/namespaces/ns1/sparkapplications":
            body = self._body()
            CAPTURE["k8s_trigger"] = {"body": body,
                                      "auth": self.headers.get("Authorization")}
            return self._send(201, body)
        self._send(404, {"detail": f"no mock route for POST {path}"})

    def do_GET(self):
        u = urlsplit(self.path)
        path, q = u.path, parse_qs(u.query)
        # Airflow: task instances / dag run
        if re.search(r"/api/v[12]/dags/[^/]+/dagRuns/[^/]+/taskInstances$", path):
            return self._send(200, TASK_INSTANCES)
        if re.search(r"/api/v[12]/dags/[^/]+/dagRuns/[^/]+$", path):
            if SCRIPT.get("airflow_404"):
                return self._send(404, {"detail": "DAGRun not found"})
            return self._send(200, {"state": SCRIPT.get("airflow_state", "queued"),
                                    "run_type": "manual",
                                    "start_date": "2026-08-16T12:00:00+00:00"})
        # dbt Cloud: retrieve run (+ run_steps), artifact
        if path == "/api/v2/accounts/42/runs/123/":
            code = SCRIPT.get("dbt_status", 1)
            run = {"id": 123, "status": code, "status_message": "status msg",
                   "status_humanized": "Humanized", "href": "https://dbt/runs/123/",
                   "is_complete": code in (10, 20, 30), "duration": "00:03:10",
                   "run_duration": "00:02:55", "artifacts_saved": code == 10}
            if "run_steps" in q.get("include_related", [""])[0]:
                run["run_steps"] = [  # deliberately out of order
                    {"index": 2, "name": "dbt build", "logs": "BUILD LOGS", "status": 10},
                    {"index": 1, "name": "clone repo", "logs": "CLONE LOGS", "status": 10},
                ]
            return self._send(200, {"data": run, "status": {"code": 200}})
        if path == "/api/v2/accounts/42/runs/123/artifacts/run_results.json":
            if SCRIPT.get("dbt_artifact_404"):
                return self._send(404, {"status": {"code": 404}})
            return self._send(200, {"metadata": {}, "results": [
                {"unique_id": "test.proj.not_null_orders_id", "status": "pass",
                 "message": None, "failures": 0},
                {"unique_id": "test.proj.unique_orders_id", "status": "fail",
                 "message": "Got 5 results", "failures": 5},
                {"unique_id": "model.proj.orders", "status": "success", "message": None},
            ]})
        # Databricks: runs/get, runs/get-output
        if path == "/api/2.1/jobs/runs/get":
            life, result = SCRIPT.get("dbx_state", ("PENDING", None))
            state = {"life_cycle_state": life, "state_message": "state msg"}
            if result:
                state["result_state"] = result
            return self._send(200, {
                "run_id": 999, "state": state,
                "run_page_url": "https://dbx.example/#job/1/run/999",
                "tasks": [{"run_id": 1000, "task_key": "main"}],
                "setup_duration": 1000, "execution_duration": 2000,
                "cleanup_duration": 500,
                "start_time": 1755300000000, "end_time": 1755300100000})
        if path == "/api/2.1/jobs/runs/get-output":
            CAPTURE["dbx_output_rid"] = q.get("run_id", [""])[0]
            return self._send(200, {"logs": "stdout from spark", "error": "boom",
                                    "error_trace": "Trace: line 1"})
        # K8s: get SparkApplication, driver pod logs
        if path.startswith("/apis/sparkoperator.k8s.io/v1beta2/namespaces/ns1/sparkapplications/"):
            name = path.rsplit("/", 1)[1]
            app_state = {"state": SCRIPT.get("k8s_state", "SUBMITTED")}
            if SCRIPT.get("k8s_error"):
                app_state["errorMessage"] = SCRIPT["k8s_error"]
            return self._send(200, {
                "metadata": {"name": name, "namespace": "ns1"},
                "status": {"applicationState": app_state,
                           "driverInfo": {"podName": f"{name}-driver"},
                           "sparkApplicationId": "spark-abc123",
                           "executionAttempts": 1}})
        if path.startswith("/api/v1/namespaces/ns1/pods/") and path.endswith("/log"):
            CAPTURE["k8s_log_pod"] = path.split("/")[6]
            return self._send(200, "driver stdout line 1\ndriver stdout line 2",
                              ctype="text/plain")
        self._send(404, {"detail": f"no mock route for GET {path}"})


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _reset():
    SCRIPT.clear()
    CAPTURE.clear()


ALL_ENV = ["AIRFLOW_URL", "AIRFLOW_TOKEN", "AIRFLOW_USERNAME", "AIRFLOW_PASSWORD",
           "AIRFLOW_API_VERSION", "DATABRICKS_SERVER_HOSTNAME", "DATABRICKS_TOKEN",
           "DBT_CLOUD_ACCOUNT_ID", "DBT_CLOUD_API_TOKEN", "DBT_CLOUD_JOB_ID",
           "DBT_CLOUD_BASE_URL", "K8S_API_URL", "K8S_TOKEN", "K8S_NAMESPACE",
           "K8S_VERIFY_SSL"]


@pytest.fixture
def clean_env(monkeypatch):
    for v in ALL_ENV:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


@pytest.fixture
def airflow_env(base, clean_env):
    clean_env.setenv("AIRFLOW_URL", base)
    clean_env.setenv("AIRFLOW_USERNAME", "amy")
    clean_env.setenv("AIRFLOW_PASSWORD", "pw")
    return clean_env


@pytest.fixture
def dbt_env(base, clean_env):
    clean_env.setenv("DBT_CLOUD_BASE_URL", base)
    clean_env.setenv("DBT_CLOUD_ACCOUNT_ID", "42")
    clean_env.setenv("DBT_CLOUD_API_TOKEN", "tok123")
    clean_env.setenv("DBT_CLOUD_JOB_ID", "7")
    return clean_env


@pytest.fixture
def dbx_env(base, clean_env):
    clean_env.setenv("DATABRICKS_SERVER_HOSTNAME", base)  # scheme included -> used as-is
    clean_env.setenv("DATABRICKS_TOKEN", "dbxtok")
    return clean_env


@pytest.fixture
def k8s_env(base, clean_env):
    clean_env.setenv("K8S_API_URL", base)
    clean_env.setenv("K8S_TOKEN", "k8stok")
    clean_env.setenv("K8S_NAMESPACE", "ns1")
    return clean_env


# ── Registry & configured() ─────────────────────────────────────────────

def test_registry_and_lookup():
    assert set(PLATFORMS) == {"airflow", "databricks_jobs", "dbt_cloud", "k8s_spark"}
    rows = all_platforms()
    assert {r["name"] for r in rows} == set(PLATFORMS)
    assert all(set(r) == {"name", "label", "configured"} for r in rows)
    with pytest.raises(KeyError):
        get_platform("nope")


def test_unconfigured_without_env(clean_env):
    for p in PLATFORMS.values():
        assert p.configured() is False
    assert all(r["configured"] is False for r in all_platforms())


# ── Airflow ─────────────────────────────────────────────────────────────

def test_airflow_trigger_roundtrip(airflow_env):
    p = get_platform("airflow")
    assert p.configured() is True
    out = p.trigger({"dag_id": "etl_daily", "conf": {"day": "2026-08-16"}})
    dag_id, run_id = out["run_ref"].split(":", 1)
    assert dag_id == "etl_daily"
    assert run_id.startswith("studio__")  # our own URL-safe run id
    cap = CAPTURE["airflow_trigger"]
    assert cap["body"]["conf"] == {"day": "2026-08-16"}
    assert cap["auth"] == "Basic " + base64.b64encode(b"amy:pw").decode()
    assert "etl_daily" in out["url"] and run_id in out["url"]


def test_airflow_v2_bearer_token(airflow_env, base):
    airflow_env.setenv("AIRFLOW_API_VERSION", "v2")
    airflow_env.setenv("AIRFLOW_TOKEN", "jwt123")
    get_platform("airflow").trigger({"dag_id": "etl_daily"})
    cap = CAPTURE["airflow_trigger"]
    assert cap["auth"] == "Bearer jwt123"
    assert cap["body"]["logical_date"] is None  # required-but-nullable in v2


def test_airflow_v2_password_jwt_exchange(airflow_env):
    airflow_env.setenv("AIRFLOW_API_VERSION", "v2")
    get_platform("airflow").trigger({"dag_id": "etl_daily"})
    assert CAPTURE["airflow_login"] == {"username": "amy", "password": "pw"}
    assert CAPTURE["airflow_trigger"]["auth"] == "Bearer jwt-from-login"


def test_airflow_trigger_requires_dag_id(airflow_env):
    with pytest.raises(RuntimeError, match="dag_id"):
        get_platform("airflow").trigger({})


def test_airflow_403_surfaces_error_body(airflow_env):
    with pytest.raises(RuntimeError, match="403") as e:
        get_platform("airflow").trigger({"dag_id": "forbidden"})
    assert "DAG-level access" in str(e.value)


@pytest.mark.parametrize("raw,want", [
    ("queued", "queued"), ("running", "running"),
    ("success", "succeeded"), ("failed", "failed"),
    ("some_future_state", "unknown")])
def test_airflow_state_mapping(airflow_env, raw, want):
    SCRIPT["airflow_state"] = raw
    s = get_platform("airflow").status("etl_daily:studio__x")
    assert s["state"] == want
    if want != "unknown":
        assert s["metrics"]["run_type"] == "manual"


def test_airflow_missing_run_is_unknown(airflow_env):
    SCRIPT["airflow_404"] = True
    s = get_platform("airflow").status("etl_daily:gone")
    assert s["state"] == "unknown"
    assert "404" in s["detail"]


def test_airflow_logs_task_summary(airflow_env):
    text = get_platform("airflow").logs("etl_daily:studio__x")
    assert "extract" in text and "load" in text and "failed" in text


def test_airflow_quality_from_task_instances(airflow_env):
    checks = get_platform("airflow").quality("etl_daily:studio__x")
    assert {c["name"]: c["status"] for c in checks} == {
        "extract": "pass", "load": "fail", "notify": "pending"}


def test_airflow_logs_quality_best_effort_without_server(clean_env):
    clean_env.setenv("AIRFLOW_URL", "http://127.0.0.1:9")  # nothing listens
    clean_env.setenv("AIRFLOW_TOKEN", "x")
    assert get_platform("airflow").logs("d:r") == ""
    assert get_platform("airflow").quality("d:r") == []


# ── dbt Cloud ───────────────────────────────────────────────────────────

def test_dbt_trigger_roundtrip(dbt_env):
    p = get_platform("dbt_cloud")
    assert p.configured() is True
    out = p.trigger({})  # job_id from env, default cause
    assert out["run_ref"] == "123"
    assert out["url"].endswith("/runs/123/")
    cap = CAPTURE["dbt_trigger"]
    assert cap["body"]["cause"]  # required by the API
    assert cap["auth"] == "Bearer tok123"


def test_dbt_trigger_passthrough_and_job_id_required(dbt_env):
    get_platform("dbt_cloud").trigger({"job_id": 7, "cause": "backfill",
                                       "steps_override": ["dbt build"]})
    body = CAPTURE["dbt_trigger"]["body"]
    assert body["cause"] == "backfill"
    assert body["steps_override"] == ["dbt build"]
    assert "job_id" not in body  # path param, not body
    dbt_env.delenv("DBT_CLOUD_JOB_ID")
    with pytest.raises(RuntimeError, match="job_id"):
        get_platform("dbt_cloud").trigger({})


@pytest.mark.parametrize("code,want", [
    (1, "queued"), (2, "running"), (3, "running"),
    (10, "succeeded"), (20, "failed"), (30, "canceled"), (99, "unknown")])
def test_dbt_state_mapping(dbt_env, code, want):
    SCRIPT["dbt_status"] = code
    s = get_platform("dbt_cloud").status("123")
    assert s["state"] == want
    assert s["detail"] == "status msg"
    assert s["metrics"]["duration"] == "00:03:10"


def test_dbt_logs_steps_in_index_order(dbt_env):
    text = get_platform("dbt_cloud").logs("123")
    assert text.index("clone repo") < text.index("dbt build")
    assert "CLONE LOGS" in text and "BUILD LOGS" in text


def test_dbt_quality_parses_test_entries_only(dbt_env):
    SCRIPT["dbt_status"] = 10
    checks = get_platform("dbt_cloud").quality("123")
    assert [c["name"] for c in checks] == [
        "test.proj.not_null_orders_id", "test.proj.unique_orders_id"]
    assert checks[0]["status"] == "pass"
    assert checks[1]["status"] == "fail" and "5" in checks[1]["detail"]


def test_dbt_quality_incomplete_run_and_missing_artifact(dbt_env):
    SCRIPT["dbt_status"] = 3  # still running -> no artifact fetch
    assert get_platform("dbt_cloud").quality("123") == []
    SCRIPT["dbt_status"] = 20  # complete but artifact 404s (never written)
    SCRIPT["dbt_artifact_404"] = True
    assert get_platform("dbt_cloud").quality("123") == []


# ── Databricks Jobs ─────────────────────────────────────────────────────

def test_dbx_trigger_roundtrip(dbx_env):
    p = get_platform("databricks_jobs")
    assert p.configured() is True
    body = {"run_name": "studio run",
            "tasks": [{"task_key": "main",
                       "spark_python_task": {"python_file": "dbfs:/x.py"}}]}
    out = p.trigger(body)
    assert out["run_ref"] == "999"
    assert CAPTURE["dbx_trigger"]["body"] == body
    assert CAPTURE["dbx_trigger"]["auth"] == "Bearer dbxtok"


def test_dbx_trigger_requires_tasks(dbx_env):
    with pytest.raises(RuntimeError, match="tasks"):
        get_platform("databricks_jobs").trigger({"run_name": "no tasks"})


@pytest.mark.parametrize("life,result,want", [
    ("QUEUED", None, "queued"), ("PENDING", None, "queued"),
    ("BLOCKED", None, "queued"), ("WAITING_FOR_RETRY", None, "queued"),
    ("RUNNING", None, "running"), ("TERMINATING", None, "running"),
    ("TERMINATED", "SUCCESS", "succeeded"),
    ("TERMINATED", "SUCCESS_WITH_FAILURES", "succeeded"),
    ("TERMINATED", "FAILED", "failed"), ("TERMINATED", "TIMEDOUT", "failed"),
    ("TERMINATED", "UPSTREAM_FAILED", "failed"),
    ("TERMINATED", "CANCELED", "canceled"),
    ("TERMINATED", "UPSTREAM_CANCELED", "canceled"),
    ("INTERNAL_ERROR", None, "failed"),  # no result_state -> life cycle rules
    ("SKIPPED", None, "canceled"),
    ("SOME_FUTURE_STATE", None, "unknown")])
def test_dbx_state_mapping(dbx_env, life, result, want):
    SCRIPT["dbx_state"] = (life, result)
    s = get_platform("databricks_jobs").status("999")
    assert s["state"] == want
    assert s["url"] == "https://dbx.example/#job/1/run/999"
    assert s["detail"] == "state msg"


def test_dbx_metrics(dbx_env):
    SCRIPT["dbx_state"] = ("TERMINATED", "SUCCESS")
    m = get_platform("databricks_jobs").status("999")["metrics"]
    assert m["duration_ms"] == 3500  # setup + execution + cleanup
    assert m["task_count"] == 1
    assert m["start_time"] == 1755300000000


def test_dbx_logs_per_task_output(dbx_env):
    text = get_platform("databricks_jobs").logs("999")
    assert "== main ==" in text
    assert "stdout from spark" in text
    assert "ERROR: boom" in text and "Trace: line 1" in text
    assert CAPTURE["dbx_output_rid"] == "1000"  # task run_id, never the parent


def test_dbx_quality_empty(dbx_env):
    assert get_platform("databricks_jobs").quality("999") == []


# ── Spark on Kubernetes ─────────────────────────────────────────────────

def test_k8s_shorthand_expands_manifest(k8s_env):
    p = get_platform("k8s_spark")
    assert p.configured() is True
    out = p.trigger({"main_file": "local:///opt/app/job.py"})
    ns, name = out["run_ref"].split("/")
    assert ns == "ns1"
    m = CAPTURE["k8s_trigger"]["body"]
    assert m["apiVersion"] == "sparkoperator.k8s.io/v1beta2"
    assert m["kind"] == "SparkApplication"
    assert m["metadata"]["name"] == name and m["metadata"]["namespace"] == "ns1"
    assert m["spec"]["type"] == "Python" and m["spec"]["mode"] == "cluster"
    assert m["spec"]["mainApplicationFile"] == "local:///opt/app/job.py"
    assert m["spec"]["driver"]["serviceAccount"] == "spark-operator-spark"
    # generated name is a DNS-1035 label, short enough for -driver-svc suffixes
    assert re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", name) and len(name) <= 47
    assert name.startswith("studio-")
    assert CAPTURE["k8s_trigger"]["auth"] == "Bearer k8stok"


def test_k8s_full_manifest_name_sanitized(k8s_env):
    out = get_platform("k8s_spark").trigger({
        "apiVersion": "sparkoperator.k8s.io/v1beta2", "kind": "SparkApplication",
        "metadata": {"name": "My_ETL Job!"},
        "spec": {"type": "Python", "mode": "cluster",
                 "mainApplicationFile": "local:///x.py", "sparkVersion": "4.0.4",
                 "driver": {}, "executor": {}}})
    name = out["run_ref"].split("/")[1]
    assert name == "my-etl-job"


def test_k8s_shorthand_requires_main_file(k8s_env):
    with pytest.raises(RuntimeError, match="main_file"):
        get_platform("k8s_spark").trigger({"type": "Python"})
    with pytest.raises(RuntimeError, match="main_class"):
        get_platform("k8s_spark").trigger({"main_file": "local:///x.jar",
                                           "type": "Scala"})


@pytest.mark.parametrize("raw,want", [
    ("", "queued"), ("SUBMITTED", "queued"), ("PENDING_RERUN", "queued"),
    ("RUNNING", "running"), ("SUCCEEDING", "running"), ("FAILING", "running"),
    ("COMPLETED", "succeeded"),
    ("FAILED", "failed"), ("SUBMISSION_FAILED", "failed"),
    ("SUSPENDED", "canceled"), ("UNKNOWN", "unknown")])
def test_k8s_state_mapping(k8s_env, raw, want):
    SCRIPT["k8s_state"] = raw
    s = get_platform("k8s_spark").status("ns1/studio-abc")
    assert s["state"] == want
    assert s["metrics"]["sparkApplicationId"] == "spark-abc123"


def test_k8s_failed_carries_error_message(k8s_env):
    SCRIPT["k8s_state"] = "FAILED"
    SCRIPT["k8s_error"] = "driver pod OOMKilled"
    s = get_platform("k8s_spark").status("ns1/studio-abc")
    assert s["state"] == "failed"
    assert "OOMKilled" in s["detail"]


def test_k8s_logs_from_driver_pod(k8s_env):
    text = get_platform("k8s_spark").logs("ns1/studio-abc")
    assert "driver stdout" in text
    assert CAPTURE["k8s_log_pod"] == "studio-abc-driver"


def test_k8s_quality_empty(k8s_env):
    assert get_platform("k8s_spark").quality("ns1/studio-abc") == []
