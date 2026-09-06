"""serving/run_local.py — self-hosting BitNet on the owner's own machine.

WHY THESE TESTS EXIST
─────────────────────
`supervisor.py` was written to be PID 1 of a Railway container: /data, a fixed
layout, a platform-injected PORT, a dual-stack bridge for an IPv6-only private
network. `run_local.py` is the entrypoint that makes the SAME code run on a
laptop, which means the interesting failures are all at the seam:

  1. Path/port resolution. Flags beat env beat defaults, and NOTHING lands in
     /data. A runner that quietly writes to /data on a laptop is a permission
     error at best and a 1.1 GB download into the wrong place at worst.
  2. The missing engine. This is the FIRST thing a new machine hits, because
     bitnet.cpp has to be built by hand. It must produce instructions naming
     bitnet.cpp and a non-zero exit — never a FileNotFoundError traceback out
     of Popen, and never after a model download has already run.
  3. An already-present model is not re-fetched. The download is ~1.1 GB; doing
     it twice is the difference between "start it again" being free and being a
     coffee break. Proven here by pointing the fetch URL at a dead port: if the
     runner tried, the run would fail instead of reaching `ready`.
  4. The printed STUDIO_LLM_BASE_URL is the port it actually binds. That single
     line is what the owner pastes into Studio; if it names a different port
     than the listener, Studio silently never reaches BitNet.
  5. Ctrl-C stops BOTH children. A leaked llama-server holds ~2 GB of RAM and
     the port, so the next run fails for a reason that looks unrelated.

The end-to-end test runs the REAL runner, supervisor and gateway as processes
against a STUB engine binary — the same trick as test_serving_readiness.py, one
level further out — so supervise → gateway → ready is exercised with no model,
no bitnet.cpp build, no Docker and no network.
"""
import importlib.util
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

SERVING = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "serving"))
RUNNER = os.path.join(SERVING, "run_local.py")

# run_local.py imports its sibling supervisor.py by putting serving/ on
# sys.path; loading it here under an explicit module name keeps that import
# working while never colliding with backend/app's own module names.
_spec = importlib.util.spec_from_file_location("studio_serving_run_local", RUNNER)
run_local = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_local)
supervisor = sys.modules["supervisor"]

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="uses a shebang'd stub binary and POSIX signals; the runner itself "
           "supports Windows, this harness does not")
NEEDS_SHEBANG = pytest.mark.skipif(
    " " in sys.executable,
    reason="the stub engine is launched via a shebang, which cannot quote a "
           "python path containing spaces")


def cfg(argv=(), env=None):
    """Resolve exactly as the runner does, from an explicit environment so the
    developer's own shell cannot change the answer."""
    return run_local.resolve(run_local.parse_args(list(argv)), dict(env or {}))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── 1. Resolution: flags → env → defaults, and never /data ───────────────

def test_defaults_are_a_local_directory_not_the_container_volume():
    c = cfg()
    assert c["root"] == os.path.abspath(run_local.DEFAULT_DIR)
    assert c["root"] != "/data" and not c["root"].startswith("/data/")
    assert c["models_dir"] == os.path.join(c["root"], "models")
    assert c["adapters_dir"] == os.path.join(c["root"], "adapters")
    assert c["state_path"] == os.path.join(c["root"], "state.json")
    assert c["model_path"] == os.path.join(c["root"], "models",
                                           "ggml-model-i2_s.gguf")
    assert c["adapter_path"] == os.path.join(c["root"], "adapters",
                                             "tool_call.gguf")
    assert c["host"] == "127.0.0.1", "a laptop must not bind the world by default"
    assert c["port"] == run_local.DEFAULT_PORT
    assert c["api_key"] == ""


def test_env_sets_the_paths_and_flags_beat_env():
    c = cfg(env={"STUDIO_DATA_DIR": "/tmp/env-dir"})
    assert c["root"] == "/tmp/env-dir"
    assert c["model_path"] == "/tmp/env-dir/models/ggml-model-i2_s.gguf"

    c = cfg(["--dir", "/tmp/flag-dir"], {"STUDIO_DATA_DIR": "/tmp/env-dir"})
    assert c["root"] == "/tmp/flag-dir", "the flag must win over the environment"

    c = cfg(["--models-dir", "/tmp/m", "--adapters-dir", "/tmp/a",
             "--model-file", "other.gguf", "--adapter-file", "style.gguf"],
            {"STUDIO_DATA_DIR": "/tmp/env-dir"})
    assert c["model_path"] == "/tmp/m/other.gguf"
    assert c["adapter_path"] == "/tmp/a/style.gguf"
    # The state file follows --dir, not --models-dir: it is the supervisor↔
    # gateway channel, not model storage.
    assert c["state_path"] == "/tmp/env-dir/state.json"


def test_a_relative_dir_and_a_tilde_are_expanded():
    c = cfg(["--dir", "~/bn"])
    assert c["root"] == os.path.join(os.path.expanduser("~"), "bn")
    c = cfg(["--dir", "rel"])
    assert c["root"] == os.path.abspath("rel") and os.path.isabs(c["root"])


def test_port_precedence_is_flag_then_studio_var_then_PORT():
    """PORT is honoured last on purpose: a developer shell very often already
    has PORT set for something else, and serving BitNet on someone's React port
    is a confusing half hour."""
    assert cfg(env={"PORT": "7000"})["port"] == 7000
    assert cfg(env={"PORT": "7000", "STUDIO_SERVING_PORT": "7100"})["port"] == 7100
    assert cfg(["--port", "7200"], {"PORT": "7000",
                                    "STUDIO_SERVING_PORT": "7100"})["port"] == 7200


def test_a_junk_number_in_the_environment_is_not_a_traceback(capsys):
    c = cfg(env={"STUDIO_SERVING_PORT": "nine thousand"})
    assert c["port"] == run_local.DEFAULT_PORT
    assert "not a number" in capsys.readouterr().err


def test_thread_default_leaves_the_machine_usable():
    assert run_local.default_threads(16) == 8      # SMT siblings do not help
    assert run_local.default_threads(8) == 4
    assert run_local.default_threads(4) == 4       # too small to give any away
    assert run_local.default_threads(1) == 1
    assert cfg(["--threads", "3"])["threads"] == 3
    assert cfg(env={"LLAMA_THREADS": "5"})["threads"] == 5


# ── 2. The one line the owner pastes into Studio ─────────────────────────

def test_the_studio_env_lines_are_the_three_that_matter():
    c = cfg(["--port", "9001"])
    assert run_local.studio_env_lines(c) == [
        "STUDIO_LLM_BASE_URL=http://127.0.0.1:9001/v1",
        "STUDIO_BITNET_LLM=openai:bitnet",
    ]
    c = cfg(["--port", "9001", "--api-key", "s3cret"])
    assert "STUDIO_LLM_API_KEY=s3cret" in run_local.studio_env_lines(c)


def test_a_wildcard_bind_is_printed_as_a_reachable_address():
    """0.0.0.0 is a bind wildcard, not somewhere Studio can connect to."""
    c = cfg(["--host", "0.0.0.0", "--port", "9000"])
    assert run_local.base_url(c) == "http://127.0.0.1:9000/v1"


# ── 3. The environment handed to the supervisor ──────────────────────────

def test_supervisor_reads_back_exactly_the_paths_the_runner_resolved(monkeypatch):
    """The seam that matters: run_local sets env, supervisor.load_config()
    reads it. If these two ever drift, the runner prints one path and the
    engine loads another."""
    c = cfg(["--dir", "/tmp/selfhost-x", "--port", "9100", "--ctx", "2048",
             "--threads", "6"])
    c["engine_port"] = 8123
    for k, v in run_local.supervisor_env(c, "/tmp/llama-server").items():
        monkeypatch.setenv(k, v)
    try:
        supervisor.load_config()
        assert supervisor.DATA_DIR == "/tmp/selfhost-x"
        assert supervisor.MODEL_PATH == c["model_path"]
        assert supervisor.ADAPTER_PATH == c["adapter_path"]
        assert supervisor.STATE_PATH == c["state_path"]
        assert supervisor.ENGINE_BIN == "/tmp/llama-server"
        assert supervisor.PUBLIC_PORT == 9100
        assert supervisor.ENGINE_PORT == 8123
        assert supervisor.THREADS == 6 and supervisor.CTX_SIZE == 2048
        assert supervisor.BRIDGE is False, \
            "the dual-stack bridge exists for Railway's IPv6 private network only"
        assert supervisor.LOCAL is True
        # The engine command line the supervisor would run, unchanged code path.
        argv = supervisor.engine_argv(False)
        assert argv[0] == "/tmp/llama-server"
        assert "--lora" not in argv, "no adapter file ⇒ no --lora ⇒ it can boot"
        # And the gateway is pointed at the loopback engine, not the public port.
        assert supervisor.gateway_env()["STUDIO_BACKEND_URL"] == \
            "http://127.0.0.1:8123/v1"
        assert supervisor.gateway_env()["STUDIO_GATEWAY_PORT"] == "9100"
    finally:
        monkeypatch.undo()
        supervisor.load_config()          # restore the container defaults


def test_the_container_defaults_still_win_with_no_local_environment(monkeypatch):
    """RAILWAY.md and Dockerfile.railway depend on these exact defaults; the
    local entrypoint must not have moved them."""
    for k in list(os.environ):
        if k.startswith(("STUDIO_", "LLAMA_")) or k == "PORT":
            monkeypatch.delenv(k, raising=False)
    try:
        supervisor.load_config()
        assert supervisor.DATA_DIR == "/data"
        assert supervisor.MODEL_PATH == "/data/models/ggml-model-i2_s.gguf"
        assert supervisor.STATE_PATH == "/data/state.json"
        assert supervisor.ENGINE_BIN == "/opt/bitnet/bin/llama-server"
        assert supervisor.PUBLIC_PORT == 9000 and supervisor.BRIDGE is True
        assert supervisor.LOCAL is False
    finally:
        monkeypatch.undo()
        supervisor.load_config()


# ── 4. Finding (or not finding) the engine ───────────────────────────────

def _stub_binary(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """No BitNet checkout in $HOME, in the cwd, or on PATH — so 'not found'
    means not found, on this machine and on the owner's."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-path"))
    return tmp_path


def test_a_built_engine_is_discovered_from_BITNET_HOME(isolated_home):
    built = _stub_binary(
        str(isolated_home / "BitNet" / "build" / "bin" / "llama-server"), "#!/bin/sh\n")
    path, how = run_local.find_engine(
        None, {"BITNET_HOME": str(isolated_home / "BitNet"), "PATH": ""},
        str(isolated_home))
    assert path == built and how == "BITNET_HOME"


def test_an_explicit_engine_flag_wins(isolated_home):
    a = _stub_binary(str(isolated_home / "BitNet" / "build" / "bin" / "llama-server"),
                     "#!/bin/sh\n")
    b = _stub_binary(str(isolated_home / "elsewhere" / "llama-server"), "#!/bin/sh\n")
    path, how = run_local.find_engine(b, {"BITNET_HOME": str(isolated_home / "BitNet")},
                                      str(isolated_home))
    # Reported as the flag, not as the variable it is threaded through: the
    # banner must not send someone hunting for a $STUDIO_ENGINE_BIN they never set.
    assert path == b != a and how == "--engine"


def test_a_non_executable_file_is_not_an_engine(isolated_home):
    p = isolated_home / "notes.txt"
    p.write_text("not a binary")
    assert run_local.find_engine(str(p), {}, str(isolated_home)) == (None, None)


def test_nothing_built_means_no_engine(isolated_home):
    assert run_local.find_engine(None, {"PATH": ""}, str(isolated_home)) == (None, None)


def test_the_missing_engine_message_says_what_to_build(isolated_home):
    msg = run_local.engine_missing_message(str(isolated_home), {"PATH": ""})
    assert "bitnet.cpp" in msg
    assert "github.com/microsoft/BitNet" in msg
    assert "NOT stock llama.cpp" in msg
    assert "git clone" in msg and "cmake --build build" in msg
    assert "--engine" in msg
    assert "Looked in:" in msg


def test_a_path_llama_server_is_flagged_as_probably_stock():
    assert not run_local.looks_like_bitnet("/opt/homebrew/bin/llama-server")
    assert run_local.looks_like_bitnet("/home/me/BitNet/build/bin/llama-server")


# ── 5. The runner as a process ───────────────────────────────────────────

def _run(args, env=None, timeout=60):
    e = {**os.environ, **(env or {})}
    return subprocess.run([sys.executable, RUNNER, *args], env=e, timeout=timeout,
                          capture_output=True, text=True)


def test_print_env_prints_the_port_it_would_bind():
    p = _run(["--print-env", "--port", "9123"])
    assert p.returncode == 0
    assert p.stdout.splitlines()[0] == "STUDIO_LLM_BASE_URL=http://127.0.0.1:9123/v1"


def test_a_missing_engine_is_an_instruction_and_a_non_zero_exit(tmp_path):
    p = _run(["--check", "--dir", str(tmp_path / "bn"),
              "--engine", str(tmp_path / "nope" / "llama-server")],
             env={"PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)})
    assert p.returncode == 2, "a missing engine must fail, not start half a unit"
    assert "Traceback" not in p.stderr and "Traceback" not in p.stdout
    assert "bitnet.cpp" in p.stderr and "microsoft/BitNet" in p.stderr
    assert "is not an executable file" in p.stderr
    # It must fail BEFORE creating the data directory or fetching anything.
    assert not (tmp_path / "bn" / "models").exists()


@POSIX_ONLY
def test_check_creates_nothing_on_disk(tmp_path):
    """--check is documented as preflight. It reports what a real run WOULD do,
    so it must not leave a half-made working directory behind on a machine where
    the operator was only asking a question."""
    engine = _stub_binary(
        str(tmp_path / "bitnet" / "build" / "bin" / "llama-server"), "#!/bin/sh\n")
    p = _run(["--check", "--dir", str(tmp_path / "bn"), "--engine", engine,
              "--port", "0"])
    assert p.returncode == 0, p.stderr
    assert "preflight OK" in p.stdout
    assert not (tmp_path / "bn").exists(), "--check must not create the data dir"


@POSIX_ONLY
def test_an_ipv6_bind_address_is_refused_with_the_reason(tmp_path):
    """gateway.py is http.server, i.e. AF_INET-only. On Railway the supervisor's
    dual-stack bridge hides that; run_local.py turns the bridge OFF, so an IPv6
    --host has to be caught here or it fails deep inside the gateway."""
    engine = _stub_binary(
        str(tmp_path / "bitnet" / "build" / "bin" / "llama-server"), "#!/bin/sh\n")
    p = _run(["--check", "--host", "::1", "--dir", str(tmp_path / "bn"),
              "--engine", engine])
    assert p.returncode == 2
    assert "IPv4-only" in p.stderr and "127.0.0.1" in p.stderr


@POSIX_ONLY
def test_no_download_refuses_rather_than_fetching(tmp_path):
    engine = _stub_binary(
        str(tmp_path / "bitnet" / "build" / "bin" / "llama-server"), "#!/bin/sh\n")
    p = _run(["--check", "--no-download", "--dir", str(tmp_path / "bn"),
              "--engine", engine, "--port", "0"])
    assert p.returncode == 2
    assert "--no-download" in p.stderr
    assert "huggingface.co/microsoft/bitnet-b1.58-2B-4T-gguf" in p.stderr


# ── 6. End to end against a STUB engine ──────────────────────────────────

STUB_ENGINE = '''#!{python}
"""A stand-in for bitnet.cpp's llama-server: it answers the two endpoints the
gateway actually probes, and records that it was started with the argv the
supervisor built. No model, no inference, no 1.1 GB."""
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

argv = sys.argv[1:]
port = int(argv[argv.index("--port") + 1])
host = argv[argv.index("--host") + 1]
if os.getenv("STUB_ENGINE_RECORD"):
    with open(os.environ["STUB_ENGINE_RECORD"], "w") as f:
        json.dump({{"pid": os.getpid(), "argv": argv}}, f)


class H(BaseHTTPRequestHandler):
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
            return self._json(200, {{"data": [{{"id": "bitnet"}}]}})
        if self.path.rstrip("/") == "/health":
            return self._json(200, {{"status": "ok"}})
        self._json(404, {{}})


HTTPServer((host, port), H).serve_forever()
'''


def _get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def local_unit(tmp_path):
    """Start the real run_local.py against a stub engine and a fake model."""
    procs = []

    def start(extra_args=(), extra_env=None, model=True):
        engine = _stub_binary(
            str(tmp_path / "bitnet-stub" / "build" / "bin" / "llama-server"),
            STUB_ENGINE.format(python=sys.executable))
        root = tmp_path / "bn"
        if model:
            # >1 KB and starting with the GGUF magic, which is all
            # supervisor.ensure_model() inspects on an already-present file.
            (root / "models").mkdir(parents=True)
            (root / "models" / "ggml-model-i2_s.gguf").write_bytes(b"GGUF" + b"\0" * 4096)
        record = tmp_path / "engine.json"
        env = {**os.environ,
               # If the runner ever DID try to download, this dead URL makes it
               # fail loudly instead of quietly pulling 1.1 GB in a test run.
               "STUDIO_BITNET_GGUF_URL": "http://127.0.0.1:1/must-not-be-fetched",
               "STUDIO_MODEL_FETCH_RETRIES": "1",
               "STUB_ENGINE_RECORD": str(record),
               **(extra_env or {})}
        proc = subprocess.Popen(
            [sys.executable, RUNNER, "--dir", str(root), "--engine", engine,
             "--port", "0", *extra_args],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append((proc, record))
        return proc, root, record

    yield start
    # TERMINATE, never kill-first: the engine and the gateway are GRANDchildren
    # of this test. SIGKILL cannot be handled, so the supervisor's shutdown
    # handler would never run and both would be orphaned — still holding their
    # ports and a core for the rest of the session, which shows up later as an
    # unrelated test timing out.
    for proc, record in procs:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        try:                                   # belt and braces: no stray engine
            os.kill(json.loads(record.read_text())["pid"], signal.SIGKILL)
        except (OSError, ValueError, json.JSONDecodeError):
            pass


def _await_base_url(proc, lines, deadline=60):
    """Read the runner's output until it prints the line the owner pastes."""
    end = time.time() + deadline
    while time.time() < end:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise AssertionError("runner exited:\n" + "".join(lines))
            continue
        lines.append(line)
        m = re.search(r"STUDIO_LLM_BASE_URL=(http://[^\s]+)/v1", line)
        if m:
            return m.group(1)
    raise AssertionError("no STUDIO_LLM_BASE_URL printed:\n" + "".join(lines))


def _await_ready(base, lines, deadline=60):
    end = time.time() + deadline
    last = None
    while time.time() < end:
        try:
            last = _get(base + "/health")
            if last[0] == 200:
                return last[1]
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"never became ready (last={last}):\n" + "".join(lines))


@POSIX_ONLY
@NEEDS_SHEBANG
def test_the_printed_url_is_the_port_it_binds_and_the_unit_becomes_ready(local_unit):
    """The whole path in one test: runner → supervisor → engine + gateway →
    an honest /health, reached at exactly the URL it told the owner to paste."""
    proc, root, record = local_unit()
    lines = []
    base = _await_base_url(proc, lines)
    port = int(base.rsplit(":", 1)[1])

    body = _await_ready(base, lines)
    assert body["ok"] is True and body["stage"] == "ready"
    assert body["kind"] == "llama" and body["base_model"] == "bitnet"
    assert body["mounted_adapter"] is None, "no adapter yet is the day-one state"
    assert body["priority"] == ["tool_call"], \
        "the CPU path serves the single global tool_call adapter (README §7)"

    # The URL printed is the one that answers — the actual point of the line.
    with socket.create_connection(("127.0.0.1", port), timeout=5):
        pass
    status, models = _get(base + "/v1/models")
    assert status == 200 and models["data"][0]["id"] == "bitnet"

    # The engine got the supervisor's real command line, on a loopback port of
    # its own, with no --lora (there is no adapter file).
    started = json.loads(record.read_text())
    assert started["argv"][started["argv"].index("--host") + 1] == "127.0.0.1"
    assert "--lora" not in started["argv"]
    assert int(started["argv"][started["argv"].index("--port") + 1]) != port, \
        "the engine must NOT be on the public port; only the gateway is"

    # And it never touched the network: the model was already there.
    assert not list((root / "models").glob("*.part")), "a download was attempted"


@POSIX_ONLY
@NEEDS_SHEBANG
def test_an_existing_model_is_not_downloaded_again(local_unit):
    """The runner reaching `ready` while the fetch URL points at a dead port IS
    the proof: any download attempt fails and the supervisor exits 1."""
    proc, root, _ = local_unit()
    lines = []
    base = _await_base_url(proc, lines)
    _await_ready(base, lines)
    for _ in range(20):                    # drain what the supervisor logged
        line = proc.stdout.readline()
        lines.append(line)
        if "model present" in line or not line:
            break
    log = "".join(lines)
    assert "model present" in log
    assert "downloading" not in log
    assert not list((root / "models").glob("*.part"))


@POSIX_ONLY
@NEEDS_SHEBANG
def test_ctrl_c_stops_the_engine_and_the_gateway(local_unit):
    proc, _root, record = local_unit()
    lines = []
    base = _await_base_url(proc, lines)
    _await_ready(base, lines)
    engine_pid = json.loads(record.read_text())["pid"]
    os.kill(engine_pid, 0)                 # alive before the interrupt

    proc.send_signal(signal.SIGINT)        # exactly what Ctrl-C sends
    proc.wait(timeout=30)

    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            os.kill(engine_pid, 0)
            time.sleep(0.2)
        except OSError:
            break
    else:
        os.kill(engine_pid, signal.SIGKILL)
        raise AssertionError("the engine survived Ctrl-C — it holds ~2 GB and "
                             "the port, so the next run fails for an unrelated-"
                             "looking reason")

    # The gateway went with it: nothing is listening on the public port.
    port = int(base.rsplit(":", 1)[1])
    with pytest.raises(OSError):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass


@POSIX_ONLY
@NEEDS_SHEBANG
def test_sigterm_stops_it_too(local_unit):
    proc, _root, record = local_unit()
    lines = []
    _await_ready(_await_base_url(proc, lines), lines)
    engine_pid = json.loads(record.read_text())["pid"]
    proc.terminate()
    proc.wait(timeout=30)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            os.kill(engine_pid, 0)
            time.sleep(0.2)
        except OSError:
            return
    os.kill(engine_pid, signal.SIGKILL)
    raise AssertionError("the engine survived SIGTERM")
