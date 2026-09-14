"""The serving gateway must not lie about being ready, or about what it serves.

Two production blockers this pins, both found by review after the unit was
written and before it was ever deployed:

1. READINESS FALSE-POSITIVE. serving/gateway.py starts BEFORE the model is
   downloaded and before the engine exists (deliberately — a platform
   healthcheck has to pass during a 1.1 GB first-boot download). Its /health
   returned {"ok": true} unconditionally, and a platform reads a passing
   healthcheck as permission to route traffic. The deployment went live over a
   box with no engine.

2. UNPROVABLE ADAPTER. llama-server cannot hot-load an adapter FILE, so the
   gateway re-scaled whatever was mounted at startup and reported success for
   ANY uri — including one that never existed. Studio then believed the trained
   adapter was serving while the box ran the base model.

The gateway is a stdlib http.server with no app imports, so these tests run it
as a REAL subprocess against a stub engine and a state file the supervisor
would have written. Nothing here needs docker, a model, or the network.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SERVING = os.path.join(os.path.dirname(__file__), "..", "..", "serving")
GATEWAY = os.path.realpath(os.path.join(SERVING, "gateway.py"))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _StubEngine(BaseHTTPRequestHandler):
    """Minimal llama-server: /v1/models proves it answers, /lora-adapters
    records the scale calls the gateway makes."""

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self._json(200, {"data": [{"id": "bitnet"}]})
        if self.path.rstrip("/") == "/lora-adapters":
            self.server.get_calls.append(self.path)
            return self._json(200, self.server.adapter_rows)
        self._json(404, {})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b"{}"
        self.server.calls.append((self.path, json.loads(body or b"{}")))
        if self.path.rstrip("/") == "/lora-adapters":
            return self._json(200, {"ok": True})
        if self.path.startswith("/v1/chat/completions"):
            if json.loads(body or b"{}").get("stream"):
                payload = b"data: " + b"x" * 2048 + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            return self._json(200, {"id": "x", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "hi"},
                 "finish_reason": "stop"}]})
        self._json(404, {})


@pytest.fixture
def engine():
    """A stub engine that can be started and stopped independently of the
    gateway — 'engine down' is one of the states under test."""
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _StubEngine)
    srv.calls = []
    srv.get_calls = []
    srv.adapter_rows = [{"id": 0, "path": "/data/adapters/tool_call.gguf",
                         "scale": 1.0}]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.port = port
    yield srv
    srv.shutdown()


def _start_gateway(tmp_path, engine_port, state=None, extra=None):
    """Run the REAL serving/gateway.py as a subprocess, pointed at a state file
    we control — exactly the arrangement the supervisor sets up."""
    state_path = tmp_path / "state.json"
    if state is not None:
        state_path.write_text(json.dumps(state))
    port = _free_port()
    env = {**os.environ,
           "STUDIO_BACKEND_URL": f"http://127.0.0.1:{engine_port}/v1",
           "STUDIO_BACKEND_KIND": "llama",
           "STUDIO_BASE_MODEL_NAME": "bitnet",
           "STUDIO_GATEWAY_PORT": str(port),
           "STUDIO_STATE_PATH": str(state_path),
           "STUDIO_GATEWAY_API_KEY": "",
           **(extra or {})}
    proc = subprocess.Popen([sys.executable, GATEWAY], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):                       # wait for the listener
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError(f"gateway died: {proc.stdout.read().decode()[:800]}")
            time.sleep(0.05)
    return proc, port, state_path


def _health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def gateway(tmp_path, engine):
    procs = []

    def start(state=None, extra=None, engine_port=None):
        proc, port, sp = _start_gateway(tmp_path, engine_port or engine.port, state, extra)
        procs.append(proc)
        return port, sp

    yield start
    for p in procs:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


# ── 1. Readiness is staged and honest ───────────────────────────────────

@pytest.mark.parametrize("stage", ["starting", "downloading_model",
                                   "starting_engine", "model_failed", "engine_down"])
def test_health_is_503_until_the_unit_can_actually_serve(gateway, stage):
    """Every pre-ready stage must be a 503 naming the stage. A 200 here is the
    bug: the platform activates the deployment and routes traffic into a box
    with no engine."""
    port, _ = gateway(state={"stage": stage, "adapter": None})
    status, body = _health(port)
    assert status == 503, f"{stage} reported ready"
    assert body["ok"] is False
    assert body["stage"] == stage


def test_health_is_200_only_when_the_engine_answers(gateway):
    port, _ = gateway(state={"stage": "ready", "adapter": None})
    status, body = _health(port)
    assert status == 200
    assert body["ok"] is True
    assert body["stage"] == "ready"


def test_ready_but_dead_engine_is_reported_not_claimed(tmp_path, engine, gateway):
    """The supervisor can say 'ready' while the engine has stopped answering —
    a running process that cannot serve is exactly what this endpoint exists to
    expose, so /health probes the engine rather than trusting the stage."""
    port, _ = gateway(state={"stage": "ready", "adapter": None})
    assert _health(port)[0] == 200
    engine.shutdown()                          # engine goes away underneath it
    status, body = _health(port)
    assert status == 503
    assert body["stage"] == "engine_not_answering"
    assert body["ok"] is False


def test_a_missing_state_file_does_not_read_as_ready_without_a_live_engine(
        tmp_path, engine, gateway):
    """No state file means either 'the supervisor has not got there' or
    'standalone compose'. It is distinguished by name, and still has to prove
    the engine answers before claiming readiness."""
    port, _ = gateway(state=None)              # no file written at all
    status, body = _health(port)
    assert status == 200 and body["stage"] == "unsupervised"
    engine.shutdown()
    status, body = _health(port)
    assert status == 503 and body["stage"] == "engine_not_answering"


def test_health_needs_no_credential(gateway):
    """The platform healthcheck is unauthenticated; it must stay that way even
    with a gateway key set, or the deploy can never go healthy."""
    port, _ = gateway(state={"stage": "ready", "adapter": None},
                      extra={"STUDIO_GATEWAY_API_KEY": "secret"})
    assert _health(port)[0] == 200


# ── 2. The gateway cannot claim an adapter it has not got ───────────────

def _chat(port, adapters, extra=None):
    payload = {"model": "bitnet", "messages": [{"role": "user", "content": "hi"}],
               "studio_adapters": adapters, **(extra or {})}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions",
                                 data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


MOUNTED = {"path": "/data/adapters/tool_call.gguf", "uri": "/adapters/tool_call/v7",
           "size": 10, "mtime_ns": 1}
STRICT_SHA = "a" * 64
STRICT_MOUNTED = {**MOUNTED, "version": 7, "sha256": STRICT_SHA}
STRICT_ENV = {
    "STUDIO_GATEWAY_REQUIRE_TOOL_ADAPTER": "1",
    "STUDIO_GATEWAY_TOOL_ADAPTER_URI": MOUNTED["uri"],
    "STUDIO_GATEWAY_TOOL_ADAPTER_VERSION": "7",
    "STUDIO_GATEWAY_TOOL_ADAPTER_SHA256": STRICT_SHA,
}


def test_a_matching_adapter_is_scaled_and_served(gateway, engine):
    port, _ = gateway(state={"stage": "ready", "adapter": MOUNTED})
    status, _ = _chat(port, {"tool_call": {"uri": "/adapters/tool_call/v7", "version": 7}})
    assert status == 200
    assert ("/lora-adapters", [{"id": 0, "scale": 1.0}]) in engine.calls, \
        "the verified adapter was never enabled"


def test_a_mismatched_adapter_is_refused_not_silently_claimed(gateway, engine):
    """The reported blocker: publishing v8 while v7 is mounted must NOT be
    reported as loaded. The request still succeeds — on the base model — but
    the adapter is not claimed and the engine is never told to scale it."""
    port, _ = gateway(state={"stage": "ready", "adapter": MOUNTED})
    status, _ = _chat(port, {"tool_call": {"uri": "/adapters/tool_call/v8", "version": 8}})
    assert status == 200, "a mismatch must degrade to the base model, not error"
    assert not [c for c in engine.calls if c[0].rstrip("/") == "/lora-adapters"], \
        "the gateway scaled an adapter it could not prove was mounted"


def test_an_adapter_that_never_existed_is_refused(gateway, engine):
    port, _ = gateway(state={"stage": "ready", "adapter": None})
    _chat(port, {"tool_call": {"uri": "/adapters/never/existed", "version": 1}})
    assert not [c for c in engine.calls if c[0].rstrip("/") == "/lora-adapters"]


def test_an_anonymous_mounted_adapter_never_matches(gateway, engine):
    """A mounted file with no recorded uri is anonymous. 'A file is mounted' is
    not evidence it is the RIGHT file, so it must not satisfy any request."""
    port, _ = gateway(state={"stage": "ready",
                             "adapter": {**MOUNTED, "uri": None}})
    _chat(port, {"tool_call": {"uri": "/adapters/tool_call/v7", "version": 7}})
    assert not [c for c in engine.calls if c[0].rstrip("/") == "/lora-adapters"]


def test_health_surfaces_the_mismatch(gateway):
    """A mismatch must be visible to an operator, not just logged."""
    port, _ = gateway(state={"stage": "ready", "adapter": MOUNTED})
    _chat(port, {"tool_call": {"uri": "/adapters/tool_call/v8", "version": 8}})
    _, body = _health(port)
    assert body["mounted_adapter"]["uri"] == "/adapters/tool_call/v7"
    assert body["adapter_mismatch"] == {"requested": "/adapters/tool_call/v8",
                                        "mounted": "/adapters/tool_call/v7"}


def test_a_plain_request_still_works(gateway, engine):
    """None of the above may break the ordinary OpenAI contract."""
    port, _ = gateway(state={"stage": "ready", "adapter": None})
    status, body = _chat(port, {})
    assert status == 200
    assert body["choices"][0]["message"]["content"] == "hi"
    sent = [c for c in engine.calls if c[0].startswith("/v1/chat/completions")]
    assert sent and "studio_adapters" not in sent[0][1], \
        "the private field must be stripped before the engine sees it"


def test_strict_health_applies_and_confirms_exact_adapter(gateway, engine):
    port, _ = gateway(state={"stage": "ready", "adapter": STRICT_MOUNTED,
                             "engine_epoch": 1}, extra=STRICT_ENV)
    status, body = _health(port)
    assert status == 200
    assert body["applied_adapter"] == {
        "uri": MOUNTED["uri"], "version": 7, "sha256": STRICT_SHA,
    }
    assert ("/lora-adapters", [{"id": 0, "scale": 1.0}]) in engine.calls
    assert "/lora-adapters" in engine.get_calls

    status, _ = _chat(port, {"tool_call": {
        "uri": MOUNTED["uri"], "version": 7, "sha256": STRICT_SHA,
    }}, extra={"lora": [{"id": 0, "scale": 0.0}]})
    assert status == 200
    completion = [body for path, body in engine.calls
                  if path.startswith("/v1/chat/completions")][-1]
    assert "lora" not in completion


def test_strict_health_rejects_uri_match_with_wrong_digest(gateway, engine):
    wrong = {**STRICT_MOUNTED, "sha256": "b" * 64}
    port, _ = gateway(state={"stage": "ready", "adapter": wrong}, extra=STRICT_ENV)
    status, body = _health(port)
    assert status == 503
    assert body["stage"] == "adapter_identity_mismatch"
    assert not [call for call in engine.calls
                if call == ("/lora-adapters", [{"id": 0, "scale": 1.0}])]


def test_strict_health_rejects_unconfirmed_scale(gateway, engine):
    engine.adapter_rows = [{"id": 0, "path": MOUNTED["path"], "scale": 0.0}]
    port, _ = gateway(state={"stage": "ready", "adapter": STRICT_MOUNTED},
                      extra=STRICT_ENV)
    status, body = _health(port)
    assert status == 503
    assert body["stage"] == "adapter_not_applied"


def test_strict_chat_never_falls_back_to_base(gateway, engine):
    port, _ = gateway(state={"stage": "ready", "adapter": STRICT_MOUNTED},
                      extra=STRICT_ENV)
    status, _ = _chat(port, {"tool_call": {
        "uri": MOUNTED["uri"], "version": 7, "sha256": "b" * 64,
    }})
    assert status == 503
    assert not [call for call in engine.calls if call[0].startswith("/v1/chat/completions")]


def test_strict_sha_attestation_refuses_vllm_backend(gateway):
    port, _ = gateway(state={"stage": "ready", "adapter": STRICT_MOUNTED},
                      extra={**STRICT_ENV, "STUDIO_BACKEND_KIND": "vllm"})
    status, body = _health(port)
    assert status == 503
    assert body["stage"] == "adapter_attestation_unsupported"


def test_gateway_rejects_oversized_request_before_proxying(gateway, engine):
    port, _ = gateway(state={"stage": "ready", "adapter": None},
                      extra={"STUDIO_GATEWAY_MAX_REQUEST_BYTES": "1024"})
    raw = b"x" * 1025
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=raw, method="POST",
        headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 413
    assert not [call for call in engine.calls if call[0].startswith("/v1/chat/completions")]


def test_gateway_rejects_chunked_request_bodies_before_proxying(gateway, engine):
    port, _ = gateway(state={"stage": "ready", "adapter": None})
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n\r\n"
        b"2\r\n{}\r\n0\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall(request)
        response = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                break
            response += chunk
    assert response.startswith(b"HTTP/1.1 400")
    assert not [call for call in engine.calls if call[0].startswith("/v1/chat/completions")]


def test_gateway_bounds_streamed_backend_responses(gateway, engine):
    port, _ = gateway(state={"stage": "ready", "adapter": None},
                      extra={"STUDIO_GATEWAY_MAX_RESPONSE_BYTES": "1024"})
    body = json.dumps({"model": "bitnet", "messages": [{"role": "user", "content": "hi"}],
                       "stream": True}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert len(response.read()) <= 1024


# ── 3. The enabled-adapter cache must not outlive the engine ────────────

def test_an_engine_restart_drops_the_enabled_adapter_cache(gateway, engine, tmp_path):
    """The reported lifecycle bug. The gateway outlives the engine: the
    supervisor restarts llama-server when an adapter lands or the process dies,
    while this process keeps running. A restarted engine has enabled NOTHING
    (llama mounts at scale 0 via --lora-init-without-apply), so a cache that
    survives the restart makes `if name in _LOADED: return True` skip the very
    POST /lora-adapters that applies the adapter — the box serves the base
    model while both sides report success.
    """
    port, state_path = gateway(state={"stage": "ready", "adapter": MOUNTED,
                                      "engine_epoch": 1})
    _chat(port, {"tool_call": {"uri": MOUNTED["uri"], "version": 7}})
    scales = [c for c in engine.calls if c[0].rstrip("/") == "/lora-adapters"]
    assert len(scales) == 1, "the first request should enable the adapter"

    # Same epoch: the cache is legitimately warm, no second call needed.
    _chat(port, {"tool_call": {"uri": MOUNTED["uri"], "version": 7}})
    assert len([c for c in engine.calls if c[0].rstrip("/") == "/lora-adapters"]) == 1

    # The supervisor restarts the engine and bumps the epoch.
    state_path.write_text(json.dumps({"stage": "ready", "adapter": MOUNTED,
                                      "engine_epoch": 2}))
    _chat(port, {"tool_call": {"uri": MOUNTED["uri"], "version": 7}})
    assert len([c for c in engine.calls if c[0].rstrip("/") == "/lora-adapters"]) == 2, \
        "after an engine restart the adapter must be re-enabled, not assumed"


def test_health_reflects_the_dropped_cache_after_a_restart(gateway, engine, tmp_path):
    port, state_path = gateway(state={"stage": "ready", "adapter": MOUNTED,
                                      "engine_epoch": 1})
    _chat(port, {"tool_call": {"uri": MOUNTED["uri"], "version": 7}})
    assert _health(port)[1]["loaded_adapters"], "adapter should be reported loaded"
    state_path.write_text(json.dumps({"stage": "ready", "adapter": MOUNTED,
                                      "engine_epoch": 2}))
    assert _health(port)[1]["loaded_adapters"] == [], \
        "a restarted engine has nothing loaded; /health must not claim otherwise"
