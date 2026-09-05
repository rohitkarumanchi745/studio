"""Confined launcher for an approved, model-generated MCP server.

The stdio MCP client spawns this file — never the server directly — as

    python -I -u <abs path to sandbox_runner.py> <abs path to server.py>

STDLIB ONLY, NO APP IMPORTS: it must run under `-I` (isolated: no PYTHONPATH,
no user site, no script-dir on sys.path) in a minimal environment, and inside
the docker toolrunner image where the app does not exist. Nothing here may
touch stdout — stdout IS the MCP channel; every diagnostic goes to stderr.

What it enforces before handing control to the generated code:

  1. path confinement — the server file must live under STUDIO_TOOL_SANDBOX
     (realpath-compared, so symlinks cannot escape). This is the only FATAL
     check: a refused path exits 2 without running anything.
  2. resource limits (resource.setrlimit) — CPU seconds, address space, file
     size, open files. Each is best-effort: a platform that lacks or refuses a
     limit (macOS ignores RLIMIT_AS) logs and continues, so a tool still runs
     there — the docker runner is the strong isolation. Process count is
     deliberately NOT limited here; see apply_limits().
  3. cwd = the sandbox dir, umask 0o077 — anything the tool writes is private.
  4. a daemon watchdog that os._exit(124)s after STUDIO_TOOL_MAX_SECONDS of
     wall clock, so a hung or looping tool can never outlive its turn budget.
  5. runpy.run_path(server, run_name="__main__") — the same `if __name__ ==
     "__main__": mcp.run(transport="stdio")` entry the generated file expects.

What it CANNOT do (a process is not a container): cut off the network, make
the filesystem read-only, or hide the host's files from the tool's uid. Set
STUDIO_TOOL_RUNNER=docker for that.
"""
import os
import runpy
import sys
import threading

DEFAULT_CPU_SECONDS = 120
DEFAULT_MEMORY_MB = 512
DEFAULT_MAX_SECONDS = 900
FSIZE_BYTES = 16 * 1024 * 1024
NOFILE = 64
EXIT_REFUSED = 2
EXIT_TIMEOUT = 124


def _log(msg):
    """stderr only — stdout belongs to the MCP transport."""
    sys.stderr.write(f"[sandbox_runner] {msg}\n")
    sys.stderr.flush()


def _env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        _log(f"{name} is not an integer; using {default}")
        return default


def confined_path(server_path, sandbox):
    """The realpath of `server_path` if it sits under `sandbox`, else None.
    A pure check so the same rule is unit-testable without spawning."""
    if not sandbox or not server_path:
        return None
    root = os.path.realpath(sandbox)
    path = os.path.realpath(server_path)
    try:
        inside = os.path.commonpath([path, root]) == root and path != root
    except ValueError:        # different drives (Windows) → not inside
        inside = False
    return path if inside else None


def _setrlimit(resource, name, value):
    """Best-effort setrlimit. Never raises: a missing constant, an EINVAL
    (macOS RLIMIT_AS), or a hard limit below `value` is logged and skipped."""
    which = getattr(resource, name, None)
    if which is None:
        _log(f"{name}: not available on this platform, skipped")
        return
    try:
        _soft, hard = resource.getrlimit(which)
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        resource.setrlimit(which, (value, value))
    except (ValueError, OSError) as e:
        _log(f"{name}: could not set ({e}), skipped")


def apply_limits():
    """Resource limits from the STUDIO_TOOL_* env (defaults above). Every
    failure is non-fatal by design — see the module docstring.

    NO RLIMIT_NPROC. Unlike every limit here, NPROC is scoped to the REAL UID,
    not to this process: it counts ALL of that uid's processes, host-wide. In
    process mode the tool runs as the app's own uid, so a small cap is either
    useless or actively harmful — on a normal host the app (uvicorn workers,
    thread pools, the job worker) is already over it, so the tool cannot fork
    at all, and a cap the app is under would equally deny FORK to the app's own
    processes. The real per-tool process cap is the docker runner's
    --pids-limit (sandbox.py), which is namespaced to the container.
    """
    try:
        import resource
    except ImportError:
        _log("resource module unavailable; no rlimits applied")
        return
    _setrlimit(resource, "RLIMIT_CPU", _env_int("STUDIO_TOOL_CPU_SECONDS", DEFAULT_CPU_SECONDS))
    _setrlimit(resource, "RLIMIT_AS",
               _env_int("STUDIO_TOOL_MEMORY_MB", DEFAULT_MEMORY_MB) * 1024 * 1024)
    _setrlimit(resource, "RLIMIT_FSIZE", FSIZE_BYTES)
    _setrlimit(resource, "RLIMIT_NOFILE", NOFILE)


def start_watchdog(max_seconds):
    """Hard wall-clock cap. A daemon thread + os._exit so it fires even while
    the main thread is blocked inside the tool's event loop or a C call."""
    def _fire():
        _log(f"wall-clock limit of {max_seconds}s reached; exiting {EXIT_TIMEOUT}")
        os._exit(EXIT_TIMEOUT)

    t = threading.Timer(max_seconds, _fire)
    t.daemon = True
    t.start()
    return t


def main(argv):
    if len(argv) != 2:
        _log("usage: sandbox_runner.py <server.py>")
        return EXIT_REFUSED
    sandbox = os.environ.get("STUDIO_TOOL_SANDBOX", "")
    path = confined_path(argv[1], sandbox)
    if path is None:
        _log(f"refusing to run: {argv[1]!r} is not under STUDIO_TOOL_SANDBOX={sandbox!r}")
        return EXIT_REFUSED
    if not os.path.isfile(path):
        _log(f"refusing to run: {path} is not a file")
        return EXIT_REFUSED

    apply_limits()
    try:
        os.chdir(os.path.realpath(sandbox))
    except OSError as e:
        _log(f"chdir to sandbox failed ({e}); continuing")
    os.umask(0o077)
    start_watchdog(max(1, _env_int("STUDIO_TOOL_MAX_SECONDS", DEFAULT_MAX_SECONDS)))

    # The server sees itself as a normally-invoked script: argv[0] is its own
    # path and __name__ == "__main__" so its `mcp.run(...)` guard fires.
    sys.argv = [path]
    runpy.run_path(path, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
