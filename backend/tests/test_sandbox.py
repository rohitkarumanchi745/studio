"""Sandbox tests — how an approved, model-generated MCP server is launched.

Proves: the process runner's spec carries a MINIMAL EXPLICIT environment (a
secret in the app's env never reaches a tool unless named in
STUDIO_TOOL_ENV_ALLOW, and the confinement keys cannot be overridden); the
docker runner's argv carries the isolation flags and a read-only mount; an
unknown runner, an out-of-sandbox path, or the process runner in production
without STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1 all fail closed (and mcp.registered()
then SKIPS the row instead of launching it less isolated); that apply_limits sets
the per-process rlimits but never the uid-scoped RLIMIT_NPROC; and, run for
real through the interpreter, sandbox_runner refuses a path outside
STUDIO_TOOL_SANDBOX, runs a confined script with the sandbox as cwd and no
leaked env, lets that script still start a child process, and its watchdog
kills a script that overruns STUDIO_TOOL_MAX_SECONDS.

Run from the backend directory:
    python -m pytest tests/test_sandbox.py -q
"""
import json
import os
import subprocess
import sys
import time

import pytest

from app import bootstrap, sandbox

RUNNER = sandbox.RUNNER_PATH
FIXED_KEYS = {"PATH", "HOME", "LANG", "STUDIO_TOOL_SANDBOX",
              "STUDIO_TOOL_MAX_SECONDS", "STUDIO_TOOL_CPU_SECONDS", "STUDIO_TOOL_MEMORY_MB"}


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A temp sandbox dir as STUDIO_TOOLBUILDER_DIR, a planted SECRET_X in the
    app's env, no allowlist, process runner."""
    root = os.path.realpath(str(tmp_path / "sandbox"))
    os.makedirs(root)
    monkeypatch.setenv("STUDIO_TOOLBUILDER_DIR", root)
    monkeypatch.setenv("SECRET_X", "hunter2")
    monkeypatch.delenv("STUDIO_TOOL_ENV_ALLOW", raising=False)
    monkeypatch.delenv("STUDIO_TOOL_RUNNER", raising=False)
    return root


def _server(root, name, body):
    path = os.path.join(root, name)
    with open(path, "w") as f:
        f.write(body)
    return path


def _run(spec, extra_env=None, timeout=20):
    env = dict(spec["env"])
    env.update(extra_env or {})
    return subprocess.run([spec["command"]] + spec["args"], env=env, cwd=spec["cwd"],
                          capture_output=True, text=True, timeout=timeout)


# ── launch_spec: process runner ──────────────────────────────────────────

def test_process_spec_env_is_minimal_and_explicit(box, monkeypatch):
    path = _server(box, "srv_a.py", "pass\n")
    spec = sandbox.launch_spec(path)

    assert spec["command"] == sys.executable
    assert spec["args"] == ["-I", "-u", RUNNER, path]
    assert spec["cwd"] == box
    assert set(spec["env"]) == FIXED_KEYS           # exactly the documented keys
    assert "SECRET_X" not in spec["env"]
    assert spec["env"]["PATH"] == os.environ["PATH"]
    assert spec["env"]["HOME"] == box and spec["env"]["STUDIO_TOOL_SANDBOX"] == box
    assert spec["env"]["STUDIO_TOOL_MAX_SECONDS"] == "900"
    assert spec["env"]["STUDIO_TOOL_CPU_SECONDS"] == "120"
    assert spec["env"]["STUDIO_TOOL_MEMORY_MB"] == "512"
    assert sandbox.is_sandboxed_entry({"transport": "stdio", **spec})

    # The explicit grant: naming it in STUDIO_TOOL_ENV_ALLOW copies it.
    monkeypatch.setenv("STUDIO_TOOL_ENV_ALLOW", "SECRET_X, NOT_SET_ANYWHERE")
    spec = sandbox.launch_spec(path)
    assert spec["env"]["SECRET_X"] == "hunter2"
    assert set(spec["env"]) == FIXED_KEYS | {"SECRET_X"}   # absent names are not invented


def test_process_spec_fixed_keys_win_over_allowlist(box, monkeypatch):
    path = _server(box, "srv_b.py", "pass\n")
    monkeypatch.setenv("HOME", "/somewhere/else")
    monkeypatch.setenv("STUDIO_TOOL_SANDBOX", "/tmp")
    monkeypatch.setenv("STUDIO_TOOL_ENV_ALLOW", "HOME,STUDIO_TOOL_SANDBOX")
    spec = sandbox.launch_spec(path)
    assert spec["env"]["HOME"] == box
    assert spec["env"]["STUDIO_TOOL_SANDBOX"] == box


def test_limits_come_from_env_and_garbage_falls_back(box, monkeypatch):
    path = _server(box, "srv_c.py", "pass\n")
    monkeypatch.setenv("STUDIO_TOOL_MAX_SECONDS", "30")
    monkeypatch.setenv("STUDIO_TOOL_MEMORY_MB", "lots")
    spec = sandbox.launch_spec(path)
    assert spec["env"]["STUDIO_TOOL_MAX_SECONDS"] == "30"
    assert spec["env"]["STUDIO_TOOL_MEMORY_MB"] == "512"


def test_launch_spec_fails_closed(box, tmp_path, monkeypatch):
    outside = _server(str(tmp_path), "outside.py", "pass\n")
    with pytest.raises(ValueError):
        sandbox.launch_spec(outside)
    with pytest.raises(ValueError):
        sandbox.launch_spec(box)                    # the dir itself is not a server
    # A symlink inside the sandbox pointing outside resolves outside.
    link = os.path.join(box, "srv_link.py")
    os.symlink(outside, link)
    with pytest.raises(ValueError):
        sandbox.launch_spec(link)
    inside = _server(box, "srv_d.py", "pass\n")
    monkeypatch.setenv("STUDIO_TOOL_RUNNER", "dokcer")   # typo → never a silent downgrade
    with pytest.raises(ValueError):
        sandbox.launch_spec(inside)


# ── launch_spec: docker runner ───────────────────────────────────────────

def test_docker_spec_carries_isolation_flags(box, monkeypatch):
    path = _server(box, "srv_e.py", "pass\n")
    monkeypatch.setenv("STUDIO_TOOL_RUNNER", "docker")
    monkeypatch.setenv("STUDIO_TOOL_ENV_ALLOW", "SECRET_X")
    spec = sandbox.launch_spec(path)
    args = spec["args"]

    assert spec["command"] == "docker" and args[0] == "run"
    assert spec["cwd"] is None
    for pair in (["--network", "none"], ["--read-only"], ["--cap-drop", "ALL"],
                 ["--security-opt", "no-new-privileges"], ["--pids-limit", "64"],
                 ["--memory", "512m"], ["--cpus", "1"], ["--tmpfs", "/tmp"]):
        i = args.index(pair[0])
        assert args[i:i + len(pair)] == pair
    # Only THIS file, read-only.
    mount = args[args.index("-v") + 1]
    assert mount == f"{path}:/srv/srv_e.py:ro"
    # The granted secret is passed by NAME (value via docker's env, not argv).
    assert args[args.index("-e") + 1] == "SECRET_X"
    assert "hunter2" not in " ".join(args)
    assert spec["env"]["SECRET_X"] == "hunter2"
    assert args[-5:] == ["studio-toolrunner:latest", "python", "-I", "-u", "/srv/srv_e.py"]
    assert sandbox.is_sandboxed_entry({"transport": "stdio", **spec})

    monkeypatch.setenv("STUDIO_TOOL_NETWORK", "tools-egress")
    monkeypatch.setenv("STUDIO_TOOL_IMAGE", "registry.local/toolrunner:1")
    args = sandbox.launch_spec(path)["args"]
    assert args[args.index("--network") + 1] == "tools-egress"
    assert args[-5] == "registry.local/toolrunner:1"


def test_is_sandboxed_entry_rejects_bare_interpreter(box):
    path = _server(box, "srv_f.py", "pass\n")
    bare = {"transport": "stdio", "command": sys.executable, "args": ["-u", path]}
    assert not sandbox.is_sandboxed_entry(bare)
    assert not sandbox.is_sandboxed_entry({"transport": "streamable_http", "url": "http://x"})
    assert not sandbox.is_sandboxed_entry(None)


# ── the process runner is refused in production ──────────────────────────
# The process runner shares the app's uid, filesystem and network with approved
# generated code. bootstrap.enforce() refuses to BOOT that way; these prove the
# second, load-time gate, which is what actually stands between a row and a
# launch (mcp.registered() builds specs long after boot).

def test_process_spec_is_refused_in_production(box, monkeypatch):
    path = _server(box, "srv_prod.py", "pass\n")
    monkeypatch.delenv("STUDIO_DEMO_MODE", raising=False)      # production
    with pytest.raises(ValueError) as ei:
        sandbox.launch_spec(path)
    msg = str(ei.value)
    assert "STUDIO_TOOL_RUNNER=docker" in msg
    assert bootstrap.ALLOW_PROCESS_ENV in msg

    # The explicit opt-in — and only that — brings it back.
    monkeypatch.setenv(bootstrap.ALLOW_PROCESS_ENV, "1")
    assert sandbox.launch_spec(path)["command"] == sys.executable
    monkeypatch.setenv(bootstrap.ALLOW_PROCESS_ENV, "0")
    with pytest.raises(ValueError):
        sandbox.launch_spec(path)


def test_docker_runner_is_unaffected_by_production(box, monkeypatch):
    path = _server(box, "srv_prod_docker.py", "pass\n")
    monkeypatch.delenv("STUDIO_DEMO_MODE", raising=False)
    monkeypatch.setenv("STUDIO_TOOL_RUNNER", "docker")
    assert sandbox.launch_spec(path)["command"] == "docker"


def test_demo_mode_still_gets_the_process_runner(box):
    """The dev default the module documents: a laptop demo needs no config."""
    path = _server(box, "srv_demo.py", "pass\n")
    assert sandbox.launch_spec(path)["command"] == sys.executable   # conftest: demo mode


def test_mcp_skips_the_row_rather_than_downgrading_it(box, monkeypatch, caplog):
    """The refusal reaches the registry as a SKIP: an owner-scoped row whose
    runner this deployment refuses must not load with weaker isolation."""
    from app import mcp
    path = _server(box, "srv_reg.py", "pass\n")
    entry = {"transport": "stdio", "command": sys.executable, "args": ["-u", path]}
    monkeypatch.delenv("STUDIO_DEMO_MODE", raising=False)          # production
    with caplog.at_level("WARNING", logger="studio.mcp"):
        assert mcp._sandboxed("built_tool", entry) is None
    assert any("built_tool" in r.getMessage() for r in caplog.records)

    monkeypatch.setenv(bootstrap.ALLOW_PROCESS_ENV, "1")
    assert sandbox.is_sandboxed_entry(mcp._sandboxed("built_tool", entry))

    # Switching the tool builder off skips it too, even under a real sandbox:
    # a row registered before the operator turned the feature off must stop
    # launching, not merely stop being created.
    monkeypatch.setenv("STUDIO_TOOLBUILDER", "0")
    assert mcp._sandboxed("built_tool", entry) is None


# ── sandbox_runner, run for real through the interpreter ─────────────────

def test_runner_refuses_path_outside_sandbox(box, tmp_path):
    outside = _server(str(tmp_path), "outside.py", "print('ran')\n")
    env = {"PATH": os.environ["PATH"], "STUDIO_TOOL_SANDBOX": box}
    r = subprocess.run([sys.executable, "-I", "-u", RUNNER, outside],
                       env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2
    assert "refusing to run" in r.stderr and "ran" not in r.stdout
    # No STUDIO_TOOL_SANDBOX at all is also a refusal, even for a file that
    # would be inside the dir the app uses.
    inside = _server(box, "srv_g.py", "print('ran')\n")
    r = subprocess.run([sys.executable, "-I", "-u", RUNNER, inside],
                       env={"PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "ran" not in r.stdout


def test_runner_runs_confined_server_with_clean_env(box):
    path = _server(box, "srv_h.py", (
        "import json, os, sys\n"
        "print(json.dumps({'cwd': os.getcwd(), 'env': sorted(os.environ),\n"
        "                  'name': __name__, 'argv0': sys.argv[0],\n"
        "                  'umask': (lambda m: (os.umask(m), m)[1])(os.umask(0))}))\n"
    ))
    r = _run(sandbox.launch_spec(path))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["cwd"] == box
    assert "SECRET_X" not in out["env"]
    assert "STUDIO_TOOL_SANDBOX" in out["env"]
    assert out["name"] == "__main__" and out["argv0"] == path   # the server's own entry guard fires
    assert out["umask"] == 0o077


def test_runner_confined_server_can_still_start_a_child_process(box):
    """A tool must be able to fork. RLIMIT_NPROC is per-REAL-UID, not
    per-process, so capping it at 32 here counted the whole app's processes and
    made even one child impossible on a normal host (see apply_limits)."""
    path = _server(box, "srv_fork.py", (
        "import os, sys\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os._exit(7)\n"
        "_p, status = os.waitpid(pid, 0)\n"
        "print(os.waitstatus_to_exitcode(status))\n"
    ))
    r = _run(sandbox.launch_spec(path))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "7"


def test_runner_watchdog_kills_overrun(box):
    path = _server(box, "srv_i.py", "import time\ntime.sleep(30)\n")
    t0 = time.time()
    r = _run(sandbox.launch_spec(path), extra_env={"STUDIO_TOOL_MAX_SECONDS": "1"}, timeout=15)
    assert r.returncode == 124
    assert time.time() - t0 < 5
    assert "wall-clock limit" in r.stderr


def _load_runner():
    """sandbox_runner as a module — it is stdlib-only and imports nothing from
    the app, so its pure helpers are testable without spawning."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_apply_limits_sets_process_rlimits_and_never_nproc(monkeypatch):
    """NPROC is scoped to the real UID, so it would count the APP's processes
    (and constrain them) rather than the tool's: it must not be set at all."""
    mod = _load_runner()
    monkeypatch.setenv("STUDIO_TOOL_CPU_SECONDS", "5")
    monkeypatch.setenv("STUDIO_TOOL_MEMORY_MB", "64")
    applied = {}
    monkeypatch.setattr(mod, "_setrlimit",
                        lambda _res, name, value: applied.__setitem__(name, value))
    mod.apply_limits()
    assert applied == {"RLIMIT_CPU": 5, "RLIMIT_AS": 64 * 1024 * 1024,
                       "RLIMIT_FSIZE": mod.FSIZE_BYTES, "RLIMIT_NOFILE": mod.NOFILE}
    assert not hasattr(mod, "NPROC")


def test_runner_confined_path_is_pure():
    """The path rule is unit-testable without spawning."""
    mod = _load_runner()
    assert mod.confined_path("/box/srv.py", "/box") == os.path.realpath("/box/srv.py")
    assert mod.confined_path("/box/../etc/passwd", "/box") is None
    assert mod.confined_path("/box", "/box") is None
    assert mod.confined_path("/box/srv.py", "") is None
