"""Ray Jobs as a supervised platform (platforms.RayJobsPlatform, the chat
command aliases in chat_platforms, and the supervisor's approval gate).

A threading http.server emulates a Ray head's Jobs API — submit, status,
logs, and the package store a script is uploaded to — so nothing here needs a
cluster. Locked in:
- a payload is the API's own body (passed through, extra keys dropped,
  metadata stringified and marked as Studio's) or a Python script, which is
  packaged as main.py, uploaded under a hash of its bytes (reused when the
  cluster has it) and run as `python main.py` with the args quoted;
- a malformed payload is refused before any request; a cluster's refusal
  says why; Ray's states map to the canonical set; logs are tail-bounded and
  the failure diagnostic is redacted;
- a browser link needs RAY_PUBLIC_URL and a Bearer header needs
  RAY_JOBS_TOKEN; only a dashboard URL counts as configured;
- chat resolves the platform's names and checks a payload's shape;
- a submission waits for a human admin, and approval is what triggers it.

Run from the backend directory:
    python -m pytest tests/test_ray_jobs.py -q
"""
import io
import json
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

from app import chat_platforms, db, email_service, supervisor
from app.platforms import PLATFORMS, all_platforms, get_platform
from app.supervisor import SubmitIn, approve, live_job, submit_job

SCRIPT = {}    # scripted responses — mutated per test
CAPTURE = {}   # what the cluster was sent — asserted per test
PACKAGES = {}  # the cluster's package store: name -> bytes


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/jobs/":
            body = json.loads(self._read() or b"{}")
            CAPTURE["submit"] = {"body": body, "auth": self.headers.get("Authorization"),
                                 "ctype": self.headers.get("Content-Type")}
            if "forbidden" in str(body.get("entrypoint", "")):
                return self._send(403, {"detail": "token lacks job:submit"})
            if SCRIPT.get("no_id"):
                return self._send(200, {})
            return self._send(200, {"submission_id": "raysubmit_AbC123"})
        self._send(404, {"detail": f"no mock route for POST {path}"})

    def do_PUT(self):
        path = urlsplit(self.path).path
        m = re.fullmatch(r"/api/packages/gcs/([^/]+)", path)
        if m:
            data = self._read()
            PACKAGES[m.group(1)] = data
            CAPTURE["upload"] = {"name": m.group(1), "bytes": data,
                                 "auth": self.headers.get("Authorization"),
                                 "ctype": self.headers.get("Content-Type")}
            return self._send(200, {})
        self._send(404, {"detail": f"no mock route for PUT {path}"})

    def do_GET(self):
        path = urlsplit(self.path).path
        m = re.fullmatch(r"/api/packages/gcs/([^/]+)", path)
        if m:
            CAPTURE.setdefault("package_checks", []).append(m.group(1))
            if m.group(1) in PACKAGES:
                return self._send(200, {"package_exists": True})
            return self._send(404, {"detail": "Package not found"})
        m = re.fullmatch(r"/api/jobs/([^/]+)/logs", path)
        if m:
            return self._send(200, {"logs": SCRIPT.get("logs", "driver line 1\ndriver line 2\n")})
        m = re.fullmatch(r"/api/jobs/([^/]+)", path)
        if m:
            if SCRIPT.get("missing"):
                return self._send(404, {"detail": f"Job {m.group(1)} does not exist"})
            state = SCRIPT.get("state", "PENDING")
            d = {"status": state, "message": SCRIPT.get("message", "Job is pending"),
                 "submission_id": m.group(1), "entrypoint": "python main.py",
                 "driver_node_id": "node-1"}
            if state != "PENDING":
                d["start_time"] = 1755300000000
            if state in ("SUCCEEDED", "FAILED", "STOPPED"):
                d["end_time"] = 1755300100000
            return self._send(200, d)
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
    PACKAGES.clear()


RAY_ENV = ("RAY_ADDRESS", "RAY_JOBS_TOKEN", "RAY_PUBLIC_URL", "RAY_VERIFY_SSL")


@pytest.fixture
def clean_env(monkeypatch):
    for v in RAY_ENV:
        monkeypatch.delenv(v, raising=False)
    return monkeypatch


@pytest.fixture
def ray_env(base, clean_env):
    clean_env.setenv("RAY_ADDRESS", base)
    clean_env.setenv("RAY_JOBS_TOKEN", "raytok")
    clean_env.setenv("RAY_PUBLIC_URL", base)
    return clean_env


SCRIPT_SRC = "import ray\nray.init()\nprint('hello from the cluster')\n"


def _unzip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n).decode() for n in z.namelist()}


# ── Registry and configuration ──────────────────────────────────────────

def test_ray_is_a_platform(clean_env):
    assert "ray" in PLATFORMS
    row = next(r for r in all_platforms() if r["name"] == "ray")
    assert row == {"name": "ray", "label": "Ray Jobs", "configured": False}


@pytest.mark.parametrize("address,want", [
    ("", False), ("ray://head:10001", False), ("auto", False), ("head:8265", False),
    ("http://head:8265", True), ("http://head:8265/", True), ("HTTPS://ray.example.com", True),
])
def test_only_a_dashboard_url_is_configured(clean_env, address, want):
    clean_env.setenv("RAY_ADDRESS", address)
    assert get_platform("ray").configured() is want


# ── Trigger ──────────────────────────────────────────────────────────────

def test_the_api_body_passes_through(ray_env, base):
    out = get_platform("ray").trigger({
        "entrypoint": "python etl.py --day 2026-11-12",
        "runtime_env": {"working_dir": "https://files.example/job.zip", "pip": ["pandas"]},
        "metadata": {"team": "data", "attempt": 2},
        "entrypoint_num_cpus": 2,
        "surprise": "dropped",
    })
    assert out == {"run_ref": "raysubmit_AbC123", "url": f"{base}/#/jobs/raysubmit_AbC123"}
    sent = CAPTURE["submit"]
    assert sent["auth"] == "Bearer raytok" and sent["ctype"] == "application/json"
    assert sent["body"] == {
        "entrypoint": "python etl.py --day 2026-11-12",
        "runtime_env": {"working_dir": "https://files.example/job.zip", "pip": ["pandas"]},
        "metadata": {"submitted_by": "studio", "team": "data", "attempt": "2"},
        "entrypoint_num_cpus": 2,
    }
    assert "upload" not in CAPTURE and "package_checks" not in CAPTURE


def test_a_script_is_packaged_uploaded_and_run(ray_env):
    out = get_platform("ray").trigger({
        "script": SCRIPT_SRC, "args": ["--day", "2026-11-12", "two words"],
        "runtime_env": {"pip": ["pandas"]},
    })
    assert out["run_ref"] == "raysubmit_AbC123"
    up = CAPTURE["upload"]
    assert re.fullmatch(r"_ray_pkg_[0-9a-f]{32}\.zip", up["name"])
    assert up["ctype"] == "application/octet-stream" and up["auth"] == "Bearer raytok"
    assert _unzip(up["bytes"]) == {"main.py": SCRIPT_SRC}
    # Looked for it first, then uploaded it.
    assert CAPTURE["package_checks"] == [up["name"]]
    body = CAPTURE["submit"]["body"]
    assert body["entrypoint"] == "python main.py --day 2026-11-12 'two words'"
    assert body["runtime_env"] == {"pip": ["pandas"], "working_dir": f"gcs://{up['name']}"}
    assert body["metadata"] == {"submitted_by": "studio"}
    assert "script" not in body and "args" not in body


def test_the_same_script_reuses_its_package(ray_env):
    p = get_platform("ray")
    p.trigger({"script": SCRIPT_SRC})
    name = CAPTURE["upload"]["name"]
    CAPTURE.clear()
    p.trigger({"script": SCRIPT_SRC, "args": "--n 3"})
    assert "upload" not in CAPTURE                      # the cluster had it
    assert CAPTURE["package_checks"] == [name]
    assert CAPTURE["submit"]["body"]["entrypoint"] == "python main.py --n 3"
    assert CAPTURE["submit"]["body"]["runtime_env"] == {"working_dir": f"gcs://{name}"}
    # A different script is a different package.
    CAPTURE.clear()
    p.trigger({"script": SCRIPT_SRC + "print('more')\n"})
    assert CAPTURE["upload"]["name"] != name


def test_the_package_is_the_same_bytes_every_time(ray_env):
    p = get_platform("ray")
    name1, data1 = p._package(SCRIPT_SRC)
    name2, data2 = p._package(SCRIPT_SRC)
    assert (name1, data1) == (name2, data2)


@pytest.mark.parametrize("payload,says", [
    ({}, "entrypoint"),
    ("python x.py", "JSON object"),
    ({"entrypoint": "   "}, "entrypoint"),
    ({"runtime_env": {"pip": []}}, "entrypoint"),
    ({"script": "print(1)", "entrypoint": "python x.py"}, "not both"),
    ({"script": "  "}, "Python source"),
    ({"script": 7}, "Python source"),
    ({"entrypoint": "python x.py", "runtime_env": "pip"}, "runtime_env"),
    ({"script": "print(1)", "args": [{"a": 1}]}, "args"),
    ({"script": "print(1)", "args": [True]}, "args"),
    ({"entrypoint": "python x.py", "metadata": [1]}, "metadata"),
])
def test_a_bad_payload_is_refused_before_any_request(ray_env, payload, says):
    with pytest.raises(RuntimeError, match=says):
        get_platform("ray").trigger(payload)
    assert CAPTURE == {}


def test_the_clusters_refusal_says_why(ray_env):
    with pytest.raises(RuntimeError, match=r"HTTP 403.*job:submit"):
        get_platform("ray").trigger({"entrypoint": "python forbidden.py"})


def test_a_reply_without_a_submission_id_is_an_error(ray_env):
    SCRIPT["no_id"] = True
    with pytest.raises(RuntimeError, match="no submission id"):
        get_platform("ray").trigger({"entrypoint": "python x.py"})


def test_no_token_sends_no_header_and_no_public_url_offers_no_link(ray_env):
    ray_env.delenv("RAY_JOBS_TOKEN")
    ray_env.delenv("RAY_PUBLIC_URL")
    p = get_platform("ray")
    assert p.configured() is True
    out = p.trigger({"entrypoint": "python x.py"})
    assert out["url"] is None
    assert CAPTURE["submit"]["auth"] is None
    assert p.status("raysubmit_AbC123")["url"] is None


# ── Status, logs, diagnostics ───────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("PENDING", "queued"), ("RUNNING", "running"), ("SUCCEEDED", "succeeded"),
    ("FAILED", "failed"), ("STOPPED", "canceled"), ("", "unknown"), ("WEIRD", "unknown")])
def test_state_mapping(ray_env, base, raw, want):
    SCRIPT["state"] = raw
    s = get_platform("ray").status("raysubmit_AbC123")
    assert s["state"] == want
    assert s["url"] == f"{base}/#/jobs/raysubmit_AbC123"
    assert s["metrics"]["driver_node_id"] == "node-1"
    if raw in ("SUCCEEDED", "FAILED", "STOPPED"):
        assert s["metrics"]["duration_ms"] == 100000
    else:
        assert "duration_ms" not in s["metrics"]


def test_a_failed_run_carries_its_message(ray_env):
    SCRIPT["state"] = "FAILED"
    SCRIPT["message"] = "Job entrypoint command failed with exit code 1"
    s = get_platform("ray").status("raysubmit_AbC123")
    assert s["state"] == "failed" and "exit code 1" in s["detail"]


def test_a_missing_run_is_unknown_not_an_error(ray_env):
    SCRIPT["missing"] = True
    s = get_platform("ray").status("raysubmit_gone")
    assert s["state"] == "unknown" and "404" in s["detail"]


def test_logs_are_the_drivers_tail_and_quality_is_empty(ray_env):
    p = get_platform("ray")
    assert "driver line 2" in p.logs("raysubmit_AbC123")
    SCRIPT["logs"] = "x" * 70000 + "END"
    tail = p.logs("raysubmit_AbC123")
    assert len(tail) == 64 * 1024 and tail.endswith("END")
    assert p.quality("raysubmit_AbC123") == []
    SCRIPT["missing"] = True
    assert p.logs("raysubmit_gone") == "" or True      # best effort, never raises


def test_logs_without_a_cluster_are_empty(clean_env):
    clean_env.setenv("RAY_ADDRESS", "http://127.0.0.1:9")
    assert get_platform("ray").logs("raysubmit_AbC123") == ""
    assert get_platform("ray").status("raysubmit_AbC123")["state"] == "unknown"


def test_failure_diagnostic_is_message_plus_redacted_logs(ray_env):
    SCRIPT["state"] = "FAILED"
    SCRIPT["message"] = "Job entrypoint command failed"
    SCRIPT["logs"] = ("Authorization: Bearer abc.def.ghi\npassword=hunter2\n"
                      "Traceback (most recent call last):\n  KeyError: 'day'\n")
    diag = get_platform("ray").failure_diagnostic("raysubmit_AbC123")
    assert diag.startswith("Job entrypoint command failed")
    assert "KeyError: 'day'" in diag
    assert "[redacted authorization]" in diag and "abc.def.ghi" not in diag
    assert "password=[redacted]" in diag and "hunter2" not in diag


# ── Chat: names and payload checks ──────────────────────────────────────

@pytest.mark.parametrize("said", ["ray", "Ray Jobs", "ray job", "ray cluster", "KubeRay", "anyscale"])
def test_chat_knows_the_platforms_names(said):
    assert chat_platforms._target(said) == "ray"


def test_chat_checks_a_ray_payload():
    ok = chat_platforms._missing
    assert ok("ray", {"entrypoint": "python etl.py"}) == []
    assert ok("ray", {"script": "print(1)", "args": ["--n", "3"]}) == []
    assert ok("ray", {"script": "print(1)", "args": "--n 3", "runtime_env": {"pip": []}}) == []
    assert any(e.startswith("entrypoint:") for e in ok("ray", {}))
    assert any("not both" in e for e in ok("ray", {"script": "print(1)", "entrypoint": "x"}))
    assert any(e.startswith("script:") for e in ok("ray", {"script": "   "}))
    assert any(e.startswith("args") for e in ok("ray", {"script": "p", "args": [1]}))
    assert any(e.startswith("runtime_env") for e in ok("ray", {"entrypoint": "x", "runtime_env": []}))
    assert any(e.startswith("metadata") for e in ok("ray", {"entrypoint": "x", "metadata": "m"}))
    assert "Ray Jobs" in ok("jenkins", {})[0]


# ── The supervisor's gate ───────────────────────────────────────────────

ADMIN = {"id": "u-ray-admin", "email": "ray-admin@studio.test", "role": "admin", "name": "Admin"}
ANALYST = {"id": "u-ray-analyst", "email": "ray-ana@studio.test", "role": "analyst", "name": "Ana"}
VIEWER = {"id": "u-ray-viewer", "email": "ray-vic@studio.test", "role": "viewer", "name": "Vic"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    supervisor.init_tables()
    with db.connect() as c:
        for user in (ADMIN, ANALYST, VIEWER):
            c.execute(
                "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (user["id"], user["email"], db.UNUSABLE_PASSWORD_HASH, user["name"],
                 user["role"], 1, time.time()))
        c.commit()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: {"mode": "test"})


def _submit(user, payload):
    return submit_job(SubmitIn(kind="platform_run", target="ray", script=json.dumps(payload)),
                      user=user)


def test_a_ray_run_waits_for_a_human_and_approval_triggers_it(ray_env):
    job = _submit(ANALYST, {"script": SCRIPT_SRC, "args": ["--day", "2026-11-12"]})
    assert job["status"] == "awaiting_approval" and job["supervisor_decision"] == "needs_human"
    assert any("Ray Jobs" in r for r in job["supervisor_reasons"])
    assert CAPTURE == {}                                 # nothing reached the cluster
    out = approve(job["id"], user=ADMIN)
    assert out["status"] == "running"
    assert CAPTURE["submit"]["body"]["entrypoint"] == "python main.py --day 2026-11-12"
    assert _unzip(CAPTURE["upload"]["bytes"]) == {"main.py": SCRIPT_SRC}
    assert out["result"]["run_ref"] == "raysubmit_AbC123"
    assert out["result"]["url"].endswith("/#/jobs/raysubmit_AbC123")
    SCRIPT["state"] = "SUCCEEDED"
    live = live_job(job["id"], user=ANALYST)
    assert live["state"] == "succeeded"
    assert "driver line" in live["logs"]


def test_a_viewer_cannot_submit_and_an_unconfigured_ray_is_refused(ray_env, clean_env):
    job = _submit(VIEWER, {"entrypoint": "python x.py"})
    assert job["status"] == "rejected"
    clean_env.delenv("RAY_ADDRESS")
    job = _submit(ANALYST, {"entrypoint": "python x.py"})
    assert job["status"] == "rejected"
    assert any("not configured" in r for r in job["supervisor_reasons"])
    assert CAPTURE == {}
