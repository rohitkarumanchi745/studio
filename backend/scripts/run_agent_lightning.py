#!/usr/bin/env python3
"""Safe, provider-neutral entrypoint for Studio's Agent Lightning services.

Examples (all configuration is read from environment, so keys never enter the
process command line):

    python scripts/run_agent_lightning.py server
    python scripts/run_agent_lightning.py controller
    python scripts/run_agent_lightning.py check
    python scripts/run_agent_lightning.py controller-check
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


# Direct script execution otherwise puts only backend/scripts on sys.path.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_HEARTBEAT_VERSION = 1
_DEFAULT_HEARTBEAT_PATH = "/tmp/studio-agent-lightning-controller.json"


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((os.getenv(name) or str(default)).strip())
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _server_url() -> str:
    value = (os.getenv("STUDIO_AGL_URL") or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username \
            or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("STUDIO_AGL_URL must be an absolute HTTP(S) URL without credentials")
    return value


def _token() -> str:
    server_key = (os.getenv("AGL_KEY") or "").strip()
    studio_key = (os.getenv("STUDIO_AGL_TOKEN") or "").strip()
    if server_key and studio_key and server_key != studio_key:
        raise RuntimeError("AGL_KEY and STUDIO_AGL_TOKEN must match when both are set")
    token = studio_key or server_key
    if not token and (os.getenv("STUDIO_AGL_ALLOW_INSECURE") or "").lower() not in {"1", "true", "yes", "on"}:
        raise RuntimeError("STUDIO_AGL_TOKEN is required")
    return token


def _controller_rollouts(body):
    """Hide data-only trace rows from the execution reconciler.

    The server's admission layer guarantees that every remaining local runner
    is Studio's exact recovery contract.  Keep this projection deliberately
    small: terminal trace consumers use other endpoints and are unaffected.
    """
    if not isinstance(body, list):
        raise RuntimeError("Agent Lightning returned an invalid rollout list")
    return [row for row in body if isinstance(row, dict)
            and isinstance(row.get("config"), dict)
            and isinstance(row["config"].get("local"), dict)]


def _heartbeat_path() -> Path:
    path = Path((os.getenv("STUDIO_AGL_CONTROLLER_HEARTBEAT_PATH")
                 or _DEFAULT_HEARTBEAT_PATH).strip())
    if not path.is_absolute() or path == Path("/"):
        raise RuntimeError("STUDIO_AGL_CONTROLLER_HEARTBEAT_PATH must be an absolute file path")
    return path


def _write_controller_heartbeat() -> None:
    """Publish proof that LocalReconciler completed a real queue poll.

    A background timer would keep ticking even when the reconciler itself was
    wedged.  This function is instead called only after its `/api/rollouts`
    request succeeds and the response is validated.
    """
    path = _heartbeat_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise RuntimeError("controller heartbeat may not be a symbolic link")
    except FileNotFoundError:
        pass
    payload = json.dumps({"version": _HEARTBEAT_VERSION, "pid": os.getpid(),
                          "at": time.time()}, separators=(",", ":")).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _controller_heartbeat_status() -> dict[str, object]:
    """Validate the poll heartbeat written by the running controller."""
    path = _heartbeat_path()
    max_age = _integer("STUDIO_AGL_CONTROLLER_HEARTBEAT_MAX_AGE_S", 30, 5, 600)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1024:
            raise ValueError
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict) or set(body) != {"version", "pid", "at"} \
                or body.get("version") != _HEARTBEAT_VERSION:
            raise ValueError
        pid = body["pid"]
        at = float(body["at"])
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1 or not math.isfinite(at):
            raise ValueError
        age = time.time() - at
        if age < -30 or age > max_age:
            return {"status": "not_ready", "stage": "controller_heartbeat_stale"}
        # The probe runs inside the same PID namespace as the controller.  A
        # stale file from an earlier container must not make a new one ready.
        os.kill(pid, 0)
    except FileNotFoundError:
        return {"status": "not_ready", "stage": "controller_not_polled"}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "not_ready", "stage": "controller_heartbeat_invalid"}
    return {"status": "ready", "controller_poll_age_s": round(max(0.0, age), 3)}


class _RecoveryOnlyApi:
    """Agent Lightning client view containing executable recovery rows only."""

    def __init__(self, delegate):
        self._delegate = delegate

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    async def get(self, path, *args, **kwargs):
        if path != "/api/rollouts":
            return await self._delegate.get(path, *args, **kwargs)
        import httpx

        # LocalReconciler 1.0.1 asks the stock list endpoint for the first 50
        # queued/running rows.  Stock Agent Lightning applies that limit before
        # this process can project away data-only traces, so 50 inert rows can
        # hide recovery forever.  The paired Studio endpoint performs the
        # recovery predicate inside the server, before limiting; an old or
        # mismatched server returns an error and cannot refresh readiness.
        response = await self._delegate.get(
            "/api/studio/recovery-rollouts", *args, **kwargs)
        if response.status_code >= 400:
            return response
        projected = _controller_rollouts(response.json())
        headers = {name: value for name, value in response.headers.items()
                   if name.lower() not in {"content-length", "content-type"}}
        _write_controller_heartbeat()
        return httpx.Response(response.status_code, json=projected,
                              headers=headers, request=response.request)


def run_server() -> None:
    # Validate before binding a port.  create_app validates again inside the
    # actual server process, keeping factory invocation independently safe.
    from app.agl_runtime import RuntimeConfig
    RuntimeConfig.from_env()
    import uvicorn

    port = _integer("PORT", 8080, 1, 65535)
    print(json.dumps({"service": "agent-lightning-server", "status": "starting", "port": port}),
          flush=True)
    uvicorn.run("app.agl_runtime:create_app", factory=True, host="0.0.0.0", port=port,
                workers=1, access_log=False, timeout_keep_alive=120)


async def _preflight() -> None:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(_server_url() + "/readyz")
            if response.status_code != 200 or response.json().get("status") != "ready":
                raise RuntimeError
    except Exception:
        raise RuntimeError("Agent Lightning server is not ready") from None


async def _run_controller() -> None:
    from agentlightning.client import AgentLightningAsyncClient
    from agentlightning.controller.local_reconciler import LocalReconciler
    from omegaconf import OmegaConf

    url = _server_url()
    key = _token()
    await _preflight()
    config = OmegaConf.create({
        "runner_type": "local",
        "agl_server": {"url": url, "agent_url": None, "key": key},
        "local_runner": {
            "maximum_size": _integer("STUDIO_AGL_CONTROLLER_MAXIMUM_SIZE", 10, 1, 100),
            "poll_interval": _integer("STUDIO_AGL_CONTROLLER_POLL_INTERVAL_S", 2, 1, 60),
        },
    })
    # Do not print config: it contains the API key.
    print(json.dumps({"service": "agent-lightning-controller", "status": "starting"}), flush=True)
    async with AgentLightningAsyncClient(base_url=url, key=key or None) as api:
        await LocalReconciler(api=_RecoveryOnlyApi(api), config=config).run()


def run_controller() -> None:
    port = _integer("STUDIO_AGL_CONTROLLER_HEALTH_PORT", 8082, 1, 65535)

    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            if self.path.rstrip("/") != "/readyz":
                self.send_error(404)
                return
            body = _controller_heartbeat_status()
            encoded = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(200 if body["status"] == "ready" else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever,
                              name="lightning-controller-health", daemon=True)
    thread.start()
    try:
        asyncio.run(_run_controller())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _check() -> dict[str, object]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.get(_server_url() + "/readyz")
            body = response.json()
            if response.status_code == 200 and body.get("status") == "ready":
                return {"status": "ready"}
            return {"status": "not_ready", "stage": body.get("stage", "unknown")}
    except Exception:
        return {"status": "not_ready", "stage": "unreachable"}


def run_check() -> None:
    result = asyncio.run(_check())
    print(json.dumps(result), flush=True)
    if result["status"] != "ready":
        raise SystemExit(1)


def run_controller_check() -> None:
    result = asyncio.run(_check())
    if result["status"] == "ready":
        result = _controller_heartbeat_status()
    print(json.dumps(result), flush=True)
    if result["status"] != "ready":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Studio's Agent Lightning services")
    parser.add_argument("command", choices=("server", "controller", "check", "controller-check"))
    args = parser.parse_args()
    if args.command == "server":
        run_server()
    elif args.command == "controller":
        run_controller()
    elif args.command == "check":
        run_check()
    else:
        run_controller_check()


if __name__ == "__main__":
    main()
