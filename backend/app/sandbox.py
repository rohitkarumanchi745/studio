"""How an approved, model-generated MCP server is LAUNCHED.

toolbuilder.py decides WHETHER generated code may run (a human admin's
approval) and stores only `sys.executable -u <confined path>`. This module
decides HOW it runs, at load time, from the environment — so the isolation
level is an operator setting, never something baked into a DB row at
registration. mcp.registered() swaps every owner-scoped stdio row's command
and args for launch_spec(<stored path>) before the agent's MultiServerMCPClient
sees it. agent.py is untouched: the spec flows through the returned entries.

Two runners, chosen by STUDIO_TOOL_RUNNER:

  process (default)  the app's own interpreter runs sandbox_runner.py under
                     `-I -u`, which confines the path, applies rlimits, a
                     wall-clock watchdog, cwd/umask — with a MINIMAL EXPLICIT
                     environment. The child inherits nothing from the app's
                     env except PATH; a credential reaches a tool only by name
                     via STUDIO_TOOL_ENV_ALLOW. It still shares the host's uid,
                     filesystem and network (so it *could* open backend/.env
                     if the uid can) — this runner stops accidents and runaway
                     resource use, not a determined tool. That is an acceptable
                     DEV default and an unacceptable PRODUCTION one, so
                     PRODUCTION REFUSES IT: launch_spec raises and
                     bootstrap.enforce() refuses to boot unless the operator
                     sets STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1 ("I accept that
                     approved generated code runs with the app's privileges").
  docker             `docker run` of the toolrunner image (scripts/
                     Dockerfile.toolrunner): no network by default, read-only
                     root, tmpfs /tmp, memory/cpu/pids caps, all capabilities
                     dropped, no-new-privileges, non-root, and ONLY the server
                     file bind-mounted read-only. This is the full isolation.

Invariants:
  - launch_spec never copies an environment variable that is not either a
    fixed runner key or named in STUDIO_TOOL_ENV_ALLOW. Fixed keys win over
    allowlisted ones, so the confinement variables cannot be overridden.
  - launch_spec raises ValueError for a path outside the sandbox dir or an
    unknown runner name — fail closed; the caller skips the row.
  - In production (STUDIO_DEMO_MODE unset) launch_spec never builds a process
    spec without STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1. It raises ValueError like
    every other refusal, so mcp.registered() SKIPS the row with its existing
    warning instead of silently launching it with weaker isolation. This is
    defence in depth: specs are built at load time, long after the boot gate.
  - This module imports nothing from the app at module level, and the only one
    it imports at all is bootstrap (lazily, via _mode(), for the deployment's
    mode and configured runner) — never the reverse.
"""
import os
import shlex
import sys

# The single confined directory approved servers are written into. Owned here
# (not by toolbuilder) so mcp.py and the runner-spec code can confine paths
# without importing toolbuilder — which imports both of them.
_DEFAULT_SANDBOX = os.path.join(os.path.dirname(__file__), "..", "toolbuilder_sandbox")
RUNNER_PATH = os.path.realpath(os.path.join(os.path.dirname(__file__), "sandbox_runner.py"))

DEFAULTS = {
    "STUDIO_TOOL_MAX_SECONDS": 900,
    "STUDIO_TOOL_CPU_SECONDS": 120,
    "STUDIO_TOOL_MEMORY_MB": 512,
}
# Everything the docker CLI itself may need to reach a daemon; never the tool.
_DOCKER_CLIENT_VARS = ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT",
                       "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


def _mode():
    """app.bootstrap — which mode this deployment is in and which tool runner
    it configured. Imported lazily and one-way: bootstrap is a leaf module that
    must never import back (tests/test_layering.py), and keeping the import
    inside the call also keeps this file importable on its own."""
    from . import bootstrap
    return bootstrap


def sandbox_dir():
    """Realpath of the sandbox dir (STUDIO_TOOLBUILDER_DIR). Read per call so a
    test can point it at a temp dir; the app sets it once at startup."""
    return os.path.realpath(os.getenv("STUDIO_TOOLBUILDER_DIR") or _DEFAULT_SANDBOX)


def confine(server_path):
    """Realpath of `server_path`, or ValueError if it is not strictly inside
    the sandbox dir. Symlinks are resolved first so they cannot escape."""
    root = sandbox_dir()
    path = os.path.realpath(server_path or "")
    if not path or path == root or os.path.commonpath([path, root]) != root:
        raise ValueError(f"server path {server_path!r} is outside the tool sandbox")
    return path


def _limits():
    """The STUDIO_TOOL_* limit vars as validated ints (garbage → default)."""
    out = {}
    for name, default in DEFAULTS.items():
        try:
            out[name] = str(max(1, int(os.getenv(name, "") or default)))
        except ValueError:
            out[name] = str(default)
    return out


def allowed_env_names():
    """Names in STUDIO_TOOL_ENV_ALLOW (comma-separated). This list IS the
    credential grant: nothing else from the app's environment reaches a tool."""
    raw = os.getenv("STUDIO_TOOL_ENV_ALLOW", "")
    return [n.strip() for n in raw.split(",") if n.strip()]


def _granted_env():
    return {n: os.environ[n] for n in allowed_env_names() if n in os.environ}


def _process_spec(path, root):
    env = _granted_env()
    env.update({                      # fixed keys last: they cannot be overridden
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": root,
        "LANG": "C.UTF-8",
        "STUDIO_TOOL_SANDBOX": root,
        **_limits(),
    })
    return {
        "command": sys.executable,
        "args": ["-I", "-u", RUNNER_PATH, path],
        "env": env,
        "cwd": root,
    }


def _docker_spec(path, root):
    mem = _limits()["STUDIO_TOOL_MEMORY_MB"]
    granted = _granted_env()
    args = [
        "run", "--rm", "-i",
        "--network", os.getenv("STUDIO_TOOL_NETWORK", "none") or "none",
        "--read-only", "--tmpfs", "/tmp",
        "--memory", f"{mem}m",
        "--cpus", os.getenv("STUDIO_TOOL_CPUS", "1") or "1",
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # Only THIS server file, read-only — not the whole sandbox, so one
        # user's tool cannot read another user's generated source.
        "-v", f"{path}:/srv/{os.path.basename(path)}:ro",
    ]
    # `-e NAME` (no value) makes docker copy the value from ITS environment, so
    # a granted secret never appears in the host's process listing as argv.
    for name in granted:
        args += ["-e", name]
    args += [os.getenv("STUDIO_TOOL_IMAGE", "studio-toolrunner:latest") or "studio-toolrunner:latest",
             "python", "-I", "-u", f"/srv/{os.path.basename(path)}"]
    env = {**granted,
           "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", root)}
    for name in _DOCKER_CLIENT_VARS:
        if name in os.environ:
            env[name] = os.environ[name]
    return {"command": "docker", "args": args, "env": env, "cwd": None}


def runner_mode():
    """The runner name STUDIO_TOOL_RUNNER selects ('process' by default)."""
    return _mode().tool_runner()


def process_runner_refused():
    """True when a process-runner launch must be refused: production mode
    without STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1. bootstrap.enforce() (boot) and
    launch_spec (load time) read the SAME predicate, so they cannot drift."""
    return _mode().process_tool_runner_refused()


def launch_spec(server_path):
    """{command, args, env, cwd} to launch the approved server at `server_path`
    under the runner named by STUDIO_TOOL_RUNNER. Raises ValueError for a path
    outside the sandbox, an unknown runner (fail closed — a typo must never
    silently downgrade to a weaker runner), or the process runner in production
    without STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1."""
    path = confine(server_path)
    mode = runner_mode()
    if mode == "process":
        if process_runner_refused():
            raise ValueError(
                "the 'process' tool runner shares the app's filesystem, network "
                "and credentials with approved generated code, so production "
                "refuses it: set STUDIO_TOOL_RUNNER=docker, or "
                f"{_mode().ALLOW_PROCESS_ENV}=1 to accept that approved "
                "generated code runs with the app's privileges")
        return _process_spec(path, sandbox_dir())
    if mode == "docker":
        return _docker_spec(path, sandbox_dir())
    raise ValueError(f"unknown STUDIO_TOOL_RUNNER {mode!r} (expected 'process' or 'docker')")


def is_sandboxed_entry(entry):
    """Pure predicate: does this registry entry launch through one of the two
    sandboxed runners (and not the bare interpreter)? Used by tests."""
    if not isinstance(entry, dict) or entry.get("transport") != "stdio":
        return False
    args = entry.get("args") or []
    if entry.get("command") == "docker":
        need = ("--network", "--read-only", "--cap-drop", "--security-opt", "--pids-limit")
        return all(flag in args for flag in need) and any(a.endswith(":ro") for a in args)
    env = entry.get("env")
    return (len(args) == 4 and args[0:2] == ["-I", "-u"]
            and os.path.basename(args[2]) == "sandbox_runner.py"
            and isinstance(env, dict) and env.get("STUDIO_TOOL_SANDBOX") == entry.get("cwd")
            and bool(entry.get("cwd")))


def describe(entry):
    """One-line, shell-quoted rendering for logs/admin UI (never executed)."""
    return " ".join(shlex.quote(a) for a in [entry.get("command", "")] + list(entry.get("args") or []))
