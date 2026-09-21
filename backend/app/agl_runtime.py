"""Cloud-portable Agent Lightning 1.x server runtime for Studio.

Agent Lightning 1.0.1 intentionally uses a process-local store.  This wrapper
adds the deployment invariants Studio needs without modifying the dependency:

* the recovery model and the proxy's default model come from one setting;
* that model is registered in the server process on every boot;
* HTTP admission exposes no generic Python/Kubernetes runner or model-registry
  mutation capability;
* rollouts/events are restored from, and atomically snapshotted to, one locked
  state file; and
* ``/readyz`` verifies persistence, registration, proxy state, and that the
  OpenAI-compatible upstream advertises the configured model.  A separate
  completion smoke test is required before enabling automatic recovery.

Run this app with exactly one ASGI worker.  ``scripts/run_agent_lightning.py``
enforces that rule and starts the matching local controller without putting an
API key on the command line.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse


log = logging.getLogger("studio.agl_runtime")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}")
_TRUE = frozenset({"1", "true", "yes", "on"})
_SNAPSHOT_VERSION = 1
_DEFAULT_MAX_STATE_BYTES = 256 * 1024 * 1024
_MAX_CREATE_BYTES = 1024 * 1024
_RECOVERY_ID_RE = re.compile(r"studio-recovery-[a-f0-9-]{36}")
_TRACE_ID_RE = re.compile(r"studio-([a-f0-9-]{36})")
_RECOVERY_TOP_KEYS = frozenset({"input", "is_train", "config", "metadata", "rollout_id"})
_RECOVERY_INPUT_KEYS = frozenset({
    "original_request", "action", "error", "history", "authorized_schema",
    "model", "diagnostics_truncated", "recovery_rollout_id",
})
_RECOVERY_METADATA_KEYS = frozenset({
    "batch_idx", "sample_idx_in_batch", "mode", "studio_user_id",
    "request_id", "input_digest",
})
_TRACE_INPUT_KEYS = frozenset({
    "data_id", "prompt", "source", "table", "conversation_id", "history",
})
_TRACE_METADATA_KEYS = frozenset({
    "batch_idx", "sample_idx_in_batch", "studio_trace_id", "studio_user_id",
    "studio_role", "mode", "model", "agents", "created_at", "run_id",
    "repairs_run_id", "execution_status", "action",
})


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUE


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _safe_url(value: str, name: str) -> str:
    """Accept an HTTP base URL but never embedded credentials or decorations."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username \
            or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must be an absolute HTTP(S) URL without credentials, query, or fragment")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise RuntimeError(f"{name} contains invalid characters")
    return value.rstrip("/")


def _int_setting(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((env.get(name) or str(default)).strip())
    except (TypeError, ValueError):
        raise RuntimeError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _canonical_uuid(value: Any, *, version: int) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == version and str(parsed) == value


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return _MAX_CREATE_BYTES + 1


def _studio_trace_create_allowed(raw: dict[str, Any]) -> bool:
    """Accept only Studio's inert trace replay shape.

    Trace rollouts carry observations and rewards to the store.  They have no
    runner configuration, so the local controller must never interpret their
    input or metadata as executable configuration.
    """
    if set(raw) != _RECOVERY_TOP_KEYS or raw.get("config") is not None \
            or not isinstance(raw.get("is_train"), bool):
        return False
    rollout_id = raw.get("rollout_id")
    match = _TRACE_ID_RE.fullmatch(rollout_id) if isinstance(rollout_id, str) else None
    if not match or not _canonical_uuid(match.group(1), version=4):
        return False
    task = raw.get("input")
    metadata = raw.get("metadata")
    if not isinstance(task, dict) or set(task) != _TRACE_INPUT_KEYS \
            or task.get("data_id") != match.group(1):
        return False
    if not isinstance(metadata, dict) or set(metadata) != _TRACE_METADATA_KEYS \
            or metadata.get("studio_trace_id") != match.group(1) \
            or metadata.get("batch_idx") is not None \
            or metadata.get("sample_idx_in_batch") is not None:
        return False
    for key, limit in (("prompt", 1000), ("source", 1000), ("table", 1000),
                       ("conversation_id", 1000)):
        value = task.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            return False
    history = task.get("history")
    if not isinstance(history, list) or len(history) > 8:
        return False
    if any(not isinstance(item, dict) or set(item) != {"role", "text"}
           or item.get("role") not in {"user", "assistant"}
           or not isinstance(item.get("text"), str) or len(item["text"]) > 600
           for item in history):
        return False
    agents = metadata.get("agents")
    if not isinstance(agents, list) or len(agents) > 32 \
            or any(not isinstance(agent, str) or len(agent) > 200 for agent in agents):
        return False
    return _json_size(raw) <= _MAX_CREATE_BYTES


def _studio_recovery_create_allowed(raw: dict[str, Any], config: "RuntimeConfig") -> bool:
    """Verify the one local-Python contract Studio permits on this server."""
    from . import recovery_planner

    if set(raw) != _RECOVERY_TOP_KEYS or raw.get("is_train") is not True:
        return False
    rollout_id = raw.get("rollout_id")
    if not isinstance(rollout_id, str) or not _RECOVERY_ID_RE.fullmatch(rollout_id):
        return False
    try:
        if uuid.UUID(rollout_id.removeprefix("studio-recovery-")).version != 5:
            return False
    except (ValueError, AttributeError):
        return False

    runner = raw.get("config")
    if not isinstance(runner, dict) or set(runner) != {"timeout_seconds", "local", "k8s"} \
            or runner.get("k8s") is not None \
            or not isinstance(runner.get("timeout_seconds"), int) \
            or isinstance(runner.get("timeout_seconds"), bool) \
            or not 30 <= runner["timeout_seconds"] <= 900:
        return False
    local = runner.get("local")
    if not isinstance(local, dict) or set(local) != {"agent_class", "env_map"} \
            or local.get("agent_class") != recovery_planner.AGENT_CLASS \
            or local.get("env_map") != {"STUDIO_RECOVERY_TASK_JSON": "input"}:
        return False

    task = raw.get("input")
    metadata = raw.get("metadata")
    if not isinstance(task, dict) or set(task) != _RECOVERY_INPUT_KEYS \
            or task.get("recovery_rollout_id") != rollout_id \
            or task.get("model") != config.model \
            or not isinstance(task.get("diagnostics_truncated"), bool):
        return False
    if not isinstance(task.get("original_request"), str) or not task["original_request"].strip() \
            or task["original_request"] != recovery_planner._text(
                task["original_request"], recovery_planner.MAX_PROMPT):
        return False
    if not isinstance(task.get("error"), str) or not task["error"].strip() \
            or task["error"] != recovery_planner._text(task["error"], recovery_planner.MAX_ERROR):
        return False
    if not isinstance(task.get("authorized_schema"), (dict, list)):
        return False
    try:
        schema_json = json.dumps(task["authorized_schema"], allow_nan=False)
        action = recovery_planner._action_context(task.get("action"))
        history, _ = recovery_planner._history_context(task.get("history"))
    except (TypeError, ValueError):
        return False
    if len(schema_json) > 16000 or action != task.get("action") or history != task.get("history") \
            or _json_size(task) > recovery_planner.MAX_INPUT_BYTES:
        return False

    if not isinstance(metadata, dict) or set(metadata) != _RECOVERY_METADATA_KEYS \
            or metadata.get("batch_idx") is not None \
            or metadata.get("sample_idx_in_batch") is not None \
            or metadata.get("mode") != "pipeline_recovery" \
            or not isinstance(metadata.get("studio_user_id"), str) \
            or not metadata["studio_user_id"] \
            or not isinstance(metadata.get("request_id"), str):
        return False
    unsigned = dict(task)
    unsigned.pop("recovery_rollout_id")
    digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True).encode()).hexdigest()
    if metadata.get("input_digest") != digest:
        return False
    seed = json.dumps([metadata["studio_user_id"], metadata["request_id"] or digest])
    expected_id = "studio-recovery-" + str(uuid.uuid5(uuid.NAMESPACE_URL, seed))
    return rollout_id == expected_id


def _rollout_create_allowed(body: Any, config: "RuntimeConfig") -> bool:
    """Allow exactly one inert Studio trace or one fixed recovery agent run."""
    if not isinstance(body, list) or len(body) != 1 or not isinstance(body[0], dict):
        return False
    raw = body[0]
    if raw.get("config") is None:
        return _studio_trace_create_allowed(raw)
    return _studio_recovery_create_allowed(raw, config)


@dataclass(frozen=True)
class RuntimeConfig:
    model: str
    model_endpoint: str
    models_url: str
    model_version: int
    key: str
    state_path: Path | None
    max_state_bytes: int
    ephemeral: bool
    upstream_timeout_s: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RuntimeConfig":
        env = os.environ if environ is None else environ
        model = _required(env, "STUDIO_AGL_RECOVERY_MODEL")
        if not _MODEL_RE.fullmatch(model):
            raise RuntimeError("STUDIO_AGL_RECOVERY_MODEL is not a valid model identifier")

        endpoint = _safe_url(_required(env, "STUDIO_AGL_MODEL_ENDPOINT"),
                             "STUDIO_AGL_MODEL_ENDPOINT")
        if endpoint.endswith(("/chat/completions", "/completions", "/models")):
            raise RuntimeError("STUDIO_AGL_MODEL_ENDPOINT must be the OpenAI API base URL")
        # Agent Lightning appends chat/completions to this endpoint.  Its model
        # registry has no credential field, so this must be a private/internal
        # endpoint that does not require an Authorization header.
        models_url = endpoint + "/models"

        server_key = (env.get("AGL_KEY") or "").strip()
        studio_key = (env.get("STUDIO_AGL_TOKEN") or "").strip()
        if server_key and studio_key and server_key != studio_key:
            raise RuntimeError("AGL_KEY and STUDIO_AGL_TOKEN must match when both are set")
        key = server_key or studio_key
        ephemeral = _enabled(env.get("STUDIO_AGL_ALLOW_EPHEMERAL"))
        if not key and not _enabled(env.get("STUDIO_AGL_ALLOW_INSECURE")):
            raise RuntimeError("AGL_KEY is required unless insecure development mode is explicitly enabled")

        raw_path = (env.get("STUDIO_AGL_STATE_PATH") or "").strip()
        if raw_path:
            state_path = Path(raw_path)
            if not state_path.is_absolute():
                raise RuntimeError("STUDIO_AGL_STATE_PATH must be absolute")
        elif ephemeral:
            state_path = None
        else:
            raise RuntimeError("STUDIO_AGL_STATE_PATH is required unless ephemeral development mode is explicitly enabled")

        return cls(
            model=model,
            model_endpoint=endpoint,
            models_url=models_url,
            model_version=_int_setting(env, "STUDIO_AGL_MODEL_VERSION", 0, 0, 2**31 - 1),
            key=key,
            state_path=state_path,
            max_state_bytes=_int_setting(env, "STUDIO_AGL_MAX_STATE_BYTES", _DEFAULT_MAX_STATE_BYTES,
                                         1024, 2 * 1024 * 1024 * 1024),
            ephemeral=ephemeral,
            upstream_timeout_s=_int_setting(env, "STUDIO_AGL_UPSTREAM_HEALTH_TIMEOUT_S", 5, 1, 60),
        )

    def server_mapping(self) -> dict[str, Any]:
        # Recovery decisions do not require token log probabilities.  Keeping
        # this false also permits a standards-only OpenAI-compatible bridge.
        return {
            "key": self.key,
            "default_proxy": {
                "model_name": self.model,
                "include_log_probs": False,
                "train": {"temperature": 0.0},
                "val": {"temperature": 0.0},
            },
        }


class SnapshotStore:
    """Durability adapter for Agent Lightning 1.0.1's module-level store.

    The lock is deliberately exclusive and held for the process lifetime.  A
    shared volume therefore rejects a second server replica rather than letting
    two independent in-memory histories overwrite each other.
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.path = config.state_path
        self.max_bytes = config.max_state_bytes
        self.ephemeral = config.ephemeral
        self.error = False
        self._lock_fd: int | None = None
        self._snapshot_lock = asyncio.Lock()

    @property
    def durable(self) -> bool:
        return self.path is not None

    def acquire(self) -> None:
        if self.path is None:
            return
        import fcntl

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._reject_symlink(self.path)
        lock_path = Path(str(self.path) + ".lock")
        self._reject_symlink(lock_path)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._lock_fd = os.open(lock_path, flags, 0o600)
        os.fchmod(self._lock_fd, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise RuntimeError("Agent Lightning state is already owned by another server replica") from None

    def release(self) -> None:
        if self._lock_fd is None:
            return
        import fcntl

        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise RuntimeError("Agent Lightning state paths may not be symbolic links")
        except FileNotFoundError:
            pass

    def restore(self) -> None:
        from agentlightning.server import store

        if self.path is None:
            store._rollouts.clear()
            store._events.clear()
            store._terminal_order.clear()
            store._models.clear()
            return
        if not self.path.exists():
            store._rollouts.clear()
            store._events.clear()
            store._terminal_order.clear()
            store._models.clear()
            return
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError("Agent Lightning state must be a private regular file")
        if info.st_size > self.max_bytes:
            raise RuntimeError("Agent Lightning state exceeds the configured size limit")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rollouts, events, terminal = self._validate(raw, self.config)
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("Agent Lightning state is corrupt or incompatible") from None
        store._rollouts.clear()
        store._rollouts.update(rollouts)
        store._events.clear()
        store._events.update(events)
        store._terminal_order.clear()
        store._terminal_order.extend(terminal)
        # Endpoints are configuration, not historical data.  They are always
        # re-registered from current environment during this boot.
        store._models.clear()

    @staticmethod
    def _validate(raw: Any, config: RuntimeConfig):
        from agentlightning.schemas import Event, Rollout, TERMINAL_STATES

        if not isinstance(raw, dict) or set(raw) != {"version", "rollouts", "events", "terminal_order"} \
                or raw.get("version") != _SNAPSHOT_VERSION or not isinstance(raw["rollouts"], dict) \
                or not isinstance(raw["events"], dict) or not isinstance(raw["terminal_order"], list):
            raise RuntimeError("Agent Lightning state is corrupt or incompatible")
        rollouts = {}
        for rid, value in raw["rollouts"].items():
            if not isinstance(rid, str) or not rid or len(rid) > 512:
                raise RuntimeError("Agent Lightning state contains an invalid rollout")
            rollout = Rollout.model_validate(value)
            if rollout.rollout_id != rid:
                raise RuntimeError("Agent Lightning state contains a mismatched rollout")
            # A snapshot can predate this admission layer.  Never restore an
            # executable local/Kubernetes runner merely because it was already
            # on disk: the controller would otherwise run it immediately on
            # upgrade. Historical data-only rollouts remain safe and readable.
            runner = rollout.config
            if runner.k8s is not None:
                raise RuntimeError("Agent Lightning state contains a forbidden executable rollout")
            if runner.local is not None:
                normalized = rollout.model_dump(mode="json")
                candidate = {key: normalized[key] for key in _RECOVERY_TOP_KEYS}
                if not _studio_recovery_create_allowed(candidate, config):
                    raise RuntimeError("Agent Lightning state contains a forbidden executable rollout")
            rollouts[rid] = rollout
        if set(raw["events"]) != set(rollouts):
            raise RuntimeError("Agent Lightning state contains inconsistent event indexes")
        events = {}
        for rid, attempts in raw["events"].items():
            if not isinstance(attempts, dict):
                raise RuntimeError("Agent Lightning state contains invalid events")
            events[rid] = {}
            for attempt, values in attempts.items():
                if not isinstance(attempt, str) or not isinstance(values, list):
                    raise RuntimeError("Agent Lightning state contains invalid events")
                parsed = [Event.model_validate(value) for value in values]
                if any(event.rollout_id != rid or event.attempt_id != attempt for event in parsed):
                    raise RuntimeError("Agent Lightning state contains mismatched events")
                events[rid][attempt] = parsed
        terminal = raw["terminal_order"]
        if len(terminal) != len(set(terminal)) or any(rid not in rollouts for rid in terminal):
            raise RuntimeError("Agent Lightning state contains an invalid terminal index")
        expected_terminal = {rid for rid, rollout in rollouts.items() if rollout.status.state in TERMINAL_STATES}
        if set(terminal) != expected_terminal:
            raise RuntimeError("Agent Lightning state contains an incomplete terminal index")
        return rollouts, events, list(terminal)

    def _document(self) -> dict[str, Any]:
        from agentlightning.server import store

        return {
            "version": _SNAPSHOT_VERSION,
            "rollouts": {rid: value.model_dump(mode="json") for rid, value in store._rollouts.items()},
            "events": {rid: {attempt: [event.model_dump(mode="json") for event in values]
                              for attempt, values in attempts.items()}
                       for rid, attempts in store._events.items()},
            "terminal_order": list(store._terminal_order),
        }

    async def snapshot(self) -> None:
        if self.path is None:
            return
        async with self._snapshot_lock:
            try:
                payload = json.dumps(self._document(), separators=(",", ":"), ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")
                if len(payload) > self.max_bytes:
                    raise RuntimeError("state size limit exceeded")
                self._atomic_write(payload)
                self.error = False
            except Exception:
                self.error = True
                # Do not attach the exception: filesystem paths and rollout
                # payloads do not belong in centralized application logs.
                log.error("Agent Lightning state snapshot failed")
                raise RuntimeError("Agent Lightning state persistence failed") from None

    def _atomic_write(self, payload: bytes) -> None:
        assert self.path is not None
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                         dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600, follow_symlinks=False)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _model_registered(config: RuntimeConfig) -> bool:
    from agentlightning.server import store

    registered = store._models.get(config.model, {}).get(config.model_endpoint)
    return bool(registered and registered.model == config.model
                and registered.endpoint == config.model_endpoint
                and registered.version == config.model_version)


def _upstream_has_model(body: Any, model: str) -> bool:
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return False
    return any(isinstance(item, dict) and item.get("id") == model for item in body["data"])


def create_app():
    """Uvicorn factory for Studio's durable single-worker Lightning server."""
    from agentlightning.schemas import Model
    from agentlightning.server.app import create_app as create_agentlightning_app
    from agentlightning.server.routes.models import register_models

    config = RuntimeConfig.from_env()
    snapshots = SnapshotStore(config)
    application = create_agentlightning_app(config.server_mapping())
    original_lifespan = application.router.lifespan_context
    # Agent Lightning 1.0.1 mutates module-level dictionaries. Serialize every
    # mutation with its snapshot so a concurrent request cannot change a dict
    # while it is being encoded or let a later write reach memory before an
    # earlier response has become durable.
    mutation_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        snapshots.acquire()
        try:
            snapshots.restore()
            async with original_lifespan(app):
                registered = await register_models([Model(model=config.model,
                    endpoint=config.model_endpoint, version=config.model_version)])
                if len(registered) != 1 or not _model_registered(config):
                    raise RuntimeError("Agent Lightning recovery model registration failed")
                await snapshots.snapshot()
                app.state.studio_agl_config = config
                app.state.studio_agl_snapshots = snapshots
                yield
                await snapshots.snapshot()
        finally:
            snapshots.release()

    application.router.lifespan_context = lifespan

    @application.middleware("http")
    async def persist_mutations(request, call_next):
        if request.method not in _MUTATING_METHODS:
            return await call_next(request)
        async with mutation_lock:
            if snapshots.error:
                return JSONResponse(status_code=503,
                                    content={"status": "not_ready", "stage": "state_persistence_failed"})
            # The model registry is boot-owned configuration. Startup invokes
            # register_models() directly, so no HTTP caller needs authority to
            # add an SSRF-capable endpoint or erase the readiness invariant.
            if request.url.path.rstrip("/") == "/api/models":
                return JSONResponse(status_code=403,
                                    content={"detail": "Model registry is managed at startup"})
            # Agent Lightning's stock local controller imports the caller's
            # ``config.local.agent_class`` and copies caller-selected input
            # paths into subprocess environment variables.  Its bearer token
            # must therefore not be a general-purpose Python execution token.
            # This portable server accepts only Studio's exact, deterministic
            # recovery contract; inert trace replays are separately admitted
            # with config=null.  Validate the raw JSON so unknown Pydantic
            # fields cannot be silently ignored and become meaningful after a
            # dependency upgrade.
            if request.method == "POST" and request.url.path == "/api/rollouts":
                raw_body = await request.body()
                if len(raw_body) > _MAX_CREATE_BYTES:
                    return JSONResponse(status_code=413,
                                        content={"detail": "Rollout request is too large"})
                try:
                    body = json.loads(raw_body)
                except (TypeError, ValueError):
                    return JSONResponse(status_code=400,
                                        content={"detail": "Invalid rollout request"})
                if not _rollout_create_allowed(body, config):
                    return JSONResponse(status_code=403,
                                        content={"detail": "Rollout contract is not permitted"})
            response = await call_next(request)
            try:
                await snapshots.snapshot()
            except RuntimeError:
                return JSONResponse(status_code=503,
                                    content={"status": "not_ready", "stage": "state_persistence_failed"})
            return response

    async def verify_studio_key(request: Request) -> None:
        """Protect Studio-owned API routes with the same server credential."""
        if not config.key:
            return
        authorization = request.headers.get("authorization", "")
        supplied = [request.headers.get("x-api-key", "")]
        if authorization.startswith("Bearer "):
            supplied.append(authorization[7:])
        expected = config.key.encode("utf-8")
        if not any(value and hmac.compare_digest(value.encode("utf-8"), expected)
                   for value in supplied):
            raise HTTPException(status_code=401, detail="Invalid or missing API key")

    @application.get(
        "/api/studio/recovery-rollouts", include_in_schema=False,
        dependencies=[Depends(verify_studio_key)],
    )
    async def recovery_rollouts(
            state_in: list[str] = Query(...),
            limit: int = Query(50, ge=1, le=500)):
        """Filter executable Studio recovery rows before applying ``limit``.

        Agent Lightning 1.0.1's stock list route truncates first.  Keeping this
        predicate beside admission makes inert trace volume unable to starve
        the local controller, without granting the controller a generic Python
        runner or returning an unbounded store snapshot.
        """
        if snapshots.error:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "stage": "state_persistence_failed"})
        states = set(state_in)
        if not states or not states <= {"queuing", "running"}:
            raise HTTPException(status_code=422,
                                detail="Only active recovery states may be listed")
        from agentlightning.server import store

        matches = []
        for rollout in store._rollouts.values():
            state = getattr(rollout.status.state, "value", rollout.status.state)
            if state not in states:
                continue
            normalized = rollout.model_dump(mode="json")
            candidate = {key: normalized[key] for key in _RECOVERY_TOP_KEYS}
            if _studio_recovery_create_allowed(candidate, config):
                matches.append(normalized)
                if len(matches) >= limit:
                    break
        return matches

    @application.get("/readyz", include_in_schema=False)
    async def readyz():
        if snapshots.error:
            return JSONResponse(status_code=503,
                                content={"status": "not_ready", "stage": "state_persistence_failed"})
        if not _model_registered(config):
            return JSONResponse(status_code=503,
                                content={"status": "not_ready", "stage": "model_not_registered"})
        pause = getattr(application.state, "proxy_pause_state", None)
        if pause is None or pause.paused:
            return JSONResponse(status_code=503,
                                content={"status": "not_ready", "stage": "proxy_paused"})
        try:
            response = await application.state.http_client.get(
                config.models_url, timeout=config.upstream_timeout_s)
            response.raise_for_status()
            if not _upstream_has_model(response.json(), config.model):
                raise ValueError("configured model absent")
        except Exception:
            return JSONResponse(status_code=503,
                                content={"status": "not_ready", "stage": "model_upstream_unavailable"})
        return {"status": "ready", "durable": snapshots.durable,
                "model_registered": True, "model_upstream_advertised": True,
                "completion_verified": False}

    return application
