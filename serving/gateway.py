#!/usr/bin/env python3
"""Studio adapter-aware OpenAI gateway — the trainer↔serve glue.

WHY THIS EXISTS
───────────────
Studio's app (backend/app/agent.py make_llm) talks to a self-hosted BitNet as an
OpenAI-compatible endpoint. On every /v1/chat/completions it sends:

    model = "bitnet"                       # STUDIO_BITNET_LLM = "openai:bitnet"
    extra_body["studio_adapters"] = {      # from trainer.active_adapters(user_id)
        "tool_call":  {"uri": "<adapter uri>", "version": N,
                       "sha256": "<artifact digest>"},
        "user_style": {"uri": "<adapter uri>", "version": N},   # optional, per-user
    }

But the REAL serving engines pick a LoRA differently:
  • vLLM       — by the OpenAI `model` field (model == the loaded LoRA's name), and
                 it can load/unload adapters at runtime over HTTP.
  • llama.cpp  — by a *global* adapter scale set via POST /lora-adapters; the
                 OpenAI `model` field stays the base model.

Neither understands a custom `studio_adapters` body field. This gateway bridges
that gap WITHOUT touching Studio's app code:

    Studio app ──(model=bitnet + studio_adapters)──▶ THIS GATEWAY ──▶ vLLM / llama-server
                                                          │
                                     reads studio_adapters, ensures the named LoRA
                                     is loaded in the backend (idempotent, uri→name
                                     cache), rewrites the request, proxies it (incl.
                                     stream=true), streams the response back verbatim.

STUDIO_LLM_BASE_URL points at THIS gateway (e.g. http://gateway:9000/v1).

CONTRACT THIS GATEWAY HONORS (do not change — mirrors backend/app/{agent,trainer}.py)
──────────────────────────────────────────────────────────────────────────────────
  Request  in : POST /v1/chat/completions
                { "model": "bitnet", "messages": [...], "stream": bool,
                  "studio_adapters": { "tool_call": {"uri","version","sha256"},
                                       "user_style"?: {"uri","version"} }, ... }
                (OpenAI clients nest extra_body at the TOP level of the JSON body,
                 so `studio_adapters` arrives as a top-level key.)
  Behavior    : 1. Resolve the effective adapter from studio_adapters by priority
                   (STUDIO_GATEWAY_ADAPTER_PRIORITY, default "user_style,tool_call").
                   vLLM applies ONE adapter per request via the model field, so the
                   most specific present adapter wins; a per-user style adapter is
                   trained on top of the tool-call data, so selecting it keeps tool
                   calling AND adds style (composed in the registry, one at serve).
                2. Map that adapter's uri → a stable backend LoRA name (idempotent).
                3. Ensure it is loaded in the backend:
                     vLLM  : POST /v1/load_lora_adapter {lora_name, lora_path=uri}
                     llama : POST /lora-adapters [{id, scale:1.0}]  (adapter must be
                             mounted at startup via --lora; see README CPU caveat)
                4. Rewrite the outgoing request:
                     vLLM  : model → <lora_name>
                     llama : model unchanged (global scale already applied)
                5. Proxy to STUDIO_BACKEND_URL, streaming SSE back byte-for-byte.
  Fail-safe   : if a LoRA cannot be loaded, fall back to the BASE model
                (STUDIO_BASE_MODEL_NAME, default "bitnet") — never 500 on adapter
                trouble. Studio re-guards BitNet's SQL and escalates to the frontier
                on any failure, so a base-model answer is safe, just un-personalized.
                In STUDIO_GATEWAY_REQUIRE_TOOL_ADAPTER=1 mode this fallback is
                disabled: readiness and requests fail closed unless the exact
                URI/version/SHA-256 is mounted and GET /lora-adapters confirms
                id 0 at the mounted path with scale 1.
                This strict byte attestation is intentionally llama/supervisor
                only; vLLM's runtime API does not prove artifact bytes and is
                refused when strict mode is requested.

  Also exposes:
    GET  /health                      → {"ok": true, ...}
    GET  /v1/models                   → proxied backend model list (OpenAI shape)
    POST /admin/load_adapter          → trainer push hook (see scripts/train_online.py):
                                        { "uri": "...", "name"?: "...", "kind"?: "tool_call" }
                                        loads the adapter NOW so the next call serves it.

CONFIG (env)
────────────
  STUDIO_BACKEND_URL          real engine base, incl. /v1 if it has one
                              (default http://vllm:8000/v1)   [vLLM] or
                              (         http://llama:8080/v1)  [llama.cpp]
  STUDIO_BACKEND_KIND         "vllm" | "llama"                (default "vllm")
  STUDIO_BASE_MODEL_NAME      model id the client sends / base fallback (default "bitnet")
  STUDIO_GATEWAY_BASE_MODEL_SHA256 exact base GGUF digest required from supervisor
  STUDIO_GATEWAY_HOST         bind host                       (default 0.0.0.0)
  STUDIO_GATEWAY_PORT         bind port                       (default 9000)
  STUDIO_GATEWAY_ADAPTER_PRIORITY  kinds, most-specific first (default "user_style,tool_call")
  STUDIO_GATEWAY_TIMEOUT      upstream timeout seconds        (default 600)
  STUDIO_GATEWAY_API_KEY      if set, require Authorization: Bearer <key> on /v1/* + /admin/*
  STUDIO_GATEWAY_REQUIRE_TOOL_ADAPTER  1 = strict fail-closed adapter attestation
  STUDIO_GATEWAY_TOOL_ADAPTER_{URI,VERSION,SHA256} exact required identity
  STUDIO_GATEWAY_MAX_REQUEST_BYTES / MAX_RESPONSE_BYTES bounded proxy buffers
  STUDIO_GATEWAY_MAX_STREAM_SECONDS  hard duration bound for streamed responses

Dependency-light on purpose: pure Python stdlib (http.server + urllib). No FastAPI,
no httpx — so the gateway image is tiny and nothing here can drift the app image.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Config ────────────────────────────────────────────────────────────────
BACKEND_URL = os.getenv("STUDIO_BACKEND_URL", "http://vllm:8000/v1").rstrip("/")
BACKEND_KIND = os.getenv("STUDIO_BACKEND_KIND", "vllm").strip().lower()
BASE_MODEL_NAME = os.getenv("STUDIO_BASE_MODEL_NAME", "bitnet").strip()
REQUIRED_BASE_SHA256 = os.getenv("STUDIO_GATEWAY_BASE_MODEL_SHA256", "").strip().lower()
HOST = os.getenv("STUDIO_GATEWAY_HOST", "0.0.0.0")
PORT = int(os.getenv("STUDIO_GATEWAY_PORT", "9000"))
PRIORITY = [k.strip() for k in os.getenv(
    "STUDIO_GATEWAY_ADAPTER_PRIORITY", "user_style,tool_call").split(",") if k.strip()]
TIMEOUT = int(os.getenv("STUDIO_GATEWAY_TIMEOUT", "600"))
API_KEY = os.getenv("STUDIO_GATEWAY_API_KEY", "").strip()
MAX_REQUEST_BYTES = max(1024, min(16 * 1024 * 1024, int(os.getenv(
    "STUDIO_GATEWAY_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)))))
MAX_RESPONSE_BYTES = max(1024, min(64 * 1024 * 1024, int(os.getenv(
    "STUDIO_GATEWAY_MAX_RESPONSE_BYTES", str(8 * 1024 * 1024)))))
MAX_STREAM_SECONDS = max(1, min(3600, int(os.getenv(
    "STUDIO_GATEWAY_MAX_STREAM_SECONDS", str(min(TIMEOUT, 900))))))
REQUIRE_TOOL_ADAPTER = os.getenv(
    "STUDIO_GATEWAY_REQUIRE_TOOL_ADAPTER", "").strip().lower() in {
        "1", "true", "yes", "on"}

# The engine root WITHOUT the OpenAI /v1 suffix — vLLM's runtime-LoRA endpoints
# (/v1/load_lora_adapter) live under /v1, but llama.cpp's /lora-adapters is at the
# server root, so derive both bases from BACKEND_URL.
_ROOT = re.sub(r"/v1/?$", "", BACKEND_URL)


def _log(*a):
    print("[gateway]", *a, file=sys.stderr, flush=True)


# ── uri → backend LoRA name (idempotent load cache) ───────────────────────
# vLLM keys adapters by a name we choose; we derive a stable, filesystem/HTTP-safe
# name from the adapter uri so the SAME uri always maps to the SAME name (and is
# loaded at most once). Keyed by uri → name; a name in _LOADED is known-present.
_NAME_LOCK = threading.Lock()
_LOADED = set()          # backend LoRA names we have successfully loaded this run
# Last refused adapter request, surfaced on /health so a mismatch is visible
# rather than silent: {"requested": uri, "mounted": uri or None}.
_MISMATCH = {}
# The engine generation _LOADED was populated against. A restarted engine has
# enabled NOTHING — llama mounts at scale 0 (--lora-init-without-apply) and vLLM
# loses its runtime LoRAs entirely — so a cache that outlives the engine makes
# the gateway skip the very call that turns the adapter on, and report an
# adapter that is not applied. See _sync_engine_epoch.
_ENGINE_EPOCH = None
_APPLIED = {}             # backend LoRA name -> exact identity confirmed active

# The supervisor writes this; see serving/supervisor.py write_state(). The
# gateway starts BEFORE the model is downloaded and before the engine exists,
# so a /health that just says {"ok": true} is a readiness FALSE-POSITIVE: the
# platform reads a pass as permission to route traffic, and every request then
# hits a box with no engine. Reading the supervisor's state is how /health
# stops guessing.
STATE_PATH = os.getenv("STUDIO_STATE_PATH",
                       os.path.join(os.getenv("STUDIO_DATA_DIR", "/data"), "state.json"))
# Stages in which the unit can actually answer a completion.
_READY_STAGES = {"ready"}


def _supervisor_state():
    """The supervisor's last published state, or a conservative stand-in.

    A missing or unreadable file means the supervisor has not got that far (or
    the gateway is running standalone, e.g. under docker-compose where the
    engine is a separate always-on container). Standalone is the only case
    where "no state file" is not a problem, so it is distinguished by name
    rather than silently treated as ready."""
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        if isinstance(st, dict) and st.get("stage"):
            return st
    except (OSError, ValueError):
        pass
    return {"stage": "unsupervised", "detail":
            f"no supervisor state at {STATE_PATH}; readiness falls back to probing the engine",
            "adapter": None}


def _sync_engine_epoch(state=None):
    """Drop the enabled-adapter cache when the engine has been replaced.

    The gateway outlives the engine: the supervisor restarts llama-server when
    an adapter file lands or the process dies, while this process keeps running.
    Every adapter the gateway had enabled is off again after that restart, so
    the cache must not survive it — otherwise `if name in _LOADED: return True`
    skips the POST /lora-adapters that applies the adapter, and the box serves
    the base model with both sides reporting success."""
    global _ENGINE_EPOCH
    st = state if state is not None else _supervisor_state()
    epoch = st.get("engine_epoch")
    if epoch is None:                       # unsupervised: no restarts to track
        return st
    with _NAME_LOCK:
        if _ENGINE_EPOCH != epoch:
            if _ENGINE_EPOCH is not None and _LOADED:
                _log(f"engine restarted (epoch {_ENGINE_EPOCH} → {epoch}) — dropping "
                     f"{len(_LOADED)} cached adapter(s); they will be re-enabled on "
                     f"the next request that asks for them.")
            _ENGINE_EPOCH = epoch
            _LOADED.clear()
            _APPLIED.clear()
            _MISMATCH.clear()
    return st


def _engine_answers(timeout=3):
    """Does the backend actually respond? The last link /health can check
    without generating a token."""
    try:
        _http_json("GET", f"{BACKEND_URL}/models", None, timeout=timeout)
        return True
    except Exception:
        return False


def _identity(value):
    """Normalize a URI/version/SHA-256 identity, or return None."""
    if not isinstance(value, dict):
        return None
    uri = value.get("uri")
    version = value.get("version")
    sha256 = str(value.get("sha256") or "").lower()
    if not isinstance(uri, str) or not uri.strip() or not isinstance(version, int) \
            or not 1 <= version <= 2**31 - 1 or len(sha256) != 64 \
            or any(c not in "0123456789abcdef" for c in sha256):
        return None
    return {"uri": uri.strip(), "version": version, "sha256": sha256}


def _required_identity():
    if not REQUIRE_TOOL_ADAPTER:
        return None
    try:
        version = int(os.getenv("STUDIO_GATEWAY_TOOL_ADAPTER_VERSION", ""))
    except ValueError:
        return None
    return _identity({
        "uri": os.getenv("STUDIO_GATEWAY_TOOL_ADAPTER_URI", "").strip(),
        "version": version,
        "sha256": os.getenv("STUDIO_GATEWAY_TOOL_ADAPTER_SHA256", "").strip(),
    })


def _mismatch(requested, mounted):
    """Record an exact mismatch while retaining the legacy URI-only shape."""
    with _NAME_LOCK:
        if REQUIRE_TOOL_ADAPTER:
            _MISMATCH["requested"] = requested
            _MISMATCH["mounted"] = mounted
        else:
            _MISMATCH["requested"] = (requested or {}).get("uri")
            _MISMATCH["mounted"] = (mounted or {}).get("uri")


def _readiness():
    """(http_status, payload) for GET /health. 200 ONLY when a request would be
    served; 503 with a machine-readable stage otherwise, so an operator can see
    which link is missing instead of a silent black hole."""
    st = _sync_engine_epoch()
    stage = st.get("stage")
    adapter = st.get("adapter")
    body = {"stage": stage, "backend": BACKEND_URL, "kind": BACKEND_KIND,
            "base_model": BASE_MODEL_NAME, "priority": PRIORITY,
            "base_model_identity": st.get("model"),
            "mounted_adapter": adapter, "loaded_adapters": sorted(_LOADED),
            "applied_adapter": None}
    if _MISMATCH:
        body["adapter_mismatch"] = dict(_MISMATCH)
    if st.get("detail"):
        body["detail"] = st["detail"]
    if stage in _READY_STAGES or stage == "unsupervised":
        if REQUIRED_BASE_SHA256:
            model_identity = st.get("model") or {}
            if len(REQUIRED_BASE_SHA256) != 64 \
                    or any(c not in "0123456789abcdef" for c in REQUIRED_BASE_SHA256):
                body.update(ok=False, stage="base_model_config_invalid",
                            detail="the required base-model SHA-256 is invalid")
                return 503, body
            if model_identity.get("sha256") != REQUIRED_BASE_SHA256:
                body.update(ok=False, stage="base_model_identity_mismatch",
                            detail="the running base GGUF does not match the pinned digest")
                return 503, body
        # The supervisor says the engine is up — confirm it actually answers
        # before claiming readiness. A running process that cannot serve is
        # exactly the state this endpoint exists to expose.
        if not _engine_answers():
            body["ok"] = False
            body["stage"] = "engine_not_answering"
            body.setdefault("detail", "the engine process is up but its API did not respond")
            return 503, body
        if REQUIRE_TOOL_ADAPTER:
            if BACKEND_KIND != "llama":
                body.update(ok=False, stage="adapter_attestation_unsupported",
                            detail="strict SHA-256 attestation is supported only by the supervised llama path")
                return 503, body
            required = _required_identity()
            mounted = _identity(adapter)
            if required is None:
                body.update(ok=False, stage="adapter_config_invalid",
                            detail="strict adapter mode requires URI, version, and SHA-256")
                return 503, body
            if mounted != required:
                _mismatch(required, mounted)
                body["adapter_mismatch"] = dict(_MISMATCH)
                body.update(ok=False, stage="adapter_identity_mismatch",
                            detail="the mounted adapter does not match the pinned identity")
                return 503, body
            name = _lora_name_for(required["uri"], "tool_call")
            if not _ensure_loaded(required["uri"], name, required):
                body["adapter_mismatch"] = dict(_MISMATCH) if _MISMATCH else None
                body.update(ok=False, stage="adapter_not_applied",
                            detail="the engine did not confirm the pinned adapter at scale 1")
                return 503, body
            body["loaded_adapters"] = sorted(_LOADED)
            body["applied_adapter"] = _APPLIED.get(name)
        body["ok"] = True
        return 200, body
    body["ok"] = False
    return 503, body
_URI_TO_NAME = {}        # uri → lora name


def _lora_name_for(uri, kind):
    """Stable, unique backend LoRA name for an adapter uri. Deterministic so
    reloads are idempotent; includes the kind + a short hash of the uri so two
    different uris never collide on the same name."""
    with _NAME_LOCK:
        if uri in _URI_TO_NAME:
            return _URI_TO_NAME[uri]
        base = os.path.basename(uri.rstrip("/")) or kind
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", base)[:48]
        import hashlib
        h = hashlib.sha1(uri.encode()).hexdigest()[:8]
        name = f"{kind}.{safe}.{h}"
        _URI_TO_NAME[uri] = name
        return name


def _http_json(method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        encoded = r.read(MAX_RESPONSE_BYTES + 1)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ValueError("upstream JSON response is too large")
        raw = encoded.decode() or "{}"
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, {"raw": raw}


# ── Backend adapter loading (engine-specific, idempotent, fail-safe) ──────

def _ensure_loaded_vllm(uri, name, identity=None):
    """Ensure a vLLM LoRA is loaded. POST /v1/load_lora_adapter with
    {lora_name, lora_path}. Idempotent: if it's already known loaded, no-op;
    a 'already loaded'/409 from vLLM is treated as success. Returns True on
    success, False on failure (caller falls back to the base model).
    Requires the server started with --enable-lora AND
    VLLM_ALLOW_RUNTIME_LORA_UPDATING=True (see README)."""
    if REQUIRE_TOOL_ADAPTER:
        # vLLM's runtime load API acknowledges a path/name but does not attest
        # the loaded artifact bytes. Never turn that acknowledgement into a
        # SHA-256 claim we cannot prove.
        _log("strict adapter attestation is unavailable for vLLM; refusing load")
        return False
    _sync_engine_epoch()      # a restarted vLLM has dropped its runtime LoRAs
    if name in _LOADED:
        return True
    try:
        status, _ = _http_json(
            "POST", f"{BACKEND_URL}/load_lora_adapter",
            {"lora_name": name, "lora_path": uri}, timeout=120)
        _LOADED.add(name)
        if identity:
            _APPLIED[name] = identity
        _log(f"vLLM loaded LoRA {name} ← {uri} (HTTP {status})")
        return True
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        # vLLM answers 400/409 when the adapter name already exists — that means
        # it IS available, so treat "already" as loaded, everything else as fail.
        if e.code in (400, 409) and "already" in detail.lower():
            _LOADED.add(name)
            if identity:
                _APPLIED[name] = identity
            _log(f"vLLM LoRA {name} already present ← {uri}")
            return True
        _log(f"vLLM load FAILED {name} ← {uri}: HTTP {e.code} {detail}")
        return False
    except Exception as e:
        _log(f"vLLM load FAILED {name} ← {uri}: {e}")
        return False


def _llama_adapter_is_applied(mounted):
    """Ask llama-server what is active; a POST status alone is not attestation."""
    try:
        _status, rows = _http_json("GET", f"{_ROOT}/lora-adapters", None, timeout=30)
    except Exception as exc:
        _log(f"llama.cpp adapter confirmation FAILED: {exc}")
        return False
    if not isinstance(rows, list):
        return False
    expected_path = mounted.get("path")
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != 0:
            continue
        try:
            scale = float(row.get("scale"))
        except (TypeError, ValueError):
            return False
        return row.get("path") == expected_path and scale == 1.0
    return False


def _ensure_loaded_llama(uri, name, identity=None):
    """llama.cpp CPU path: enable the mounted adapter — but ONLY if the mounted
    file is provably the one being asked for.

    llama-server cannot hot-load an adapter FILE; adapters are fixed at startup
    via --lora and only their SCALE is adjustable (POST /lora-adapters). The
    obvious implementation — scale id 0 and report success — is a LIE whenever
    the mounted file is not the requested one: it returns True for a uri that
    has never existed on this box. Studio reads that as "the adapter is
    serving", so the router sends learned prompts to a model that is actually
    the BASE, or a stale adapter, and nothing anywhere reports the mismatch.

    So the identity of the mounted file is checked against the request. Strict
    deployments compare path-independent URI + release version + SHA-256 from
    the supervisor's identity sidecar, then query GET /lora-adapters after the
    scale POST. An adapter without that identity is ANONYMOUS and can never
    satisfy strict readiness: "a file is mounted" is not evidence it is right
    or active.
    A mismatch is not an error — the request proceeds on the base model, which
    is the honest degradation — but it is refused, logged, and visible on
    /health as mounted_adapter versus the uri that was asked for."""
    mounted = (_sync_engine_epoch().get("adapter") or {})
    mounted_uri = mounted.get("uri")
    requested = identity or {"uri": uri}
    required = _required_identity()
    strict_match = not REQUIRE_TOOL_ADAPTER or (
        required is not None and _identity(requested) == required
        and _identity(mounted) == required)
    if mounted_uri != uri or not strict_match:
        _mismatch(requested, mounted)
        why = ("no adapter is mounted" if not mounted else
               "the mounted adapter has no recorded uri (anonymous)" if not mounted_uri
               else f"the mounted adapter is {mounted_uri}")
        _log(f"llama.cpp REFUSING to claim {name} (uri={uri}): {why}. Serving the "
             f"BASE model. The supervisor restarts the engine when a new adapter "
             f"file lands; until then Studio's routing is ahead of this box.")
        return False
    if name in _LOADED:
        if not REQUIRE_TOOL_ADAPTER or _llama_adapter_is_applied(mounted):
            return True
        _LOADED.discard(name)
        _APPLIED.pop(name, None)
    try:
        # Set the (single, mounted, VERIFIED) global adapter's scale to 1.0.
        _http_json("POST", f"{_ROOT}/lora-adapters", [{"id": 0, "scale": 1.0}], timeout=30)
        if REQUIRE_TOOL_ADAPTER and not _llama_adapter_is_applied(mounted):
            _log(f"llama.cpp did not confirm id0/path/scale for {name}")
            return False
        _LOADED.add(name)
        _APPLIED[name] = _identity(mounted) if REQUIRE_TOOL_ADAPTER else requested
        with _NAME_LOCK:
            _MISMATCH.clear()
        _log(f"llama.cpp enabled mounted LoRA id0 for {name} (uri={uri}, verified)")
        return True
    except Exception as e:
        _log(f"llama.cpp enable FAILED for {name}: {e}")
        return False


def _ensure_loaded(uri, name, identity=None):
    if BACKEND_KIND == "llama":
        return _ensure_loaded_llama(uri, name, identity)
    return _ensure_loaded_vllm(uri, name, identity)


def _resolve_adapter(studio_adapters):
    """Pick the effective adapter from studio_adapters by PRIORITY (most specific
    first). Returns (uri, kind) or (None, None). On the llama CPU path, user_style
    is document-and-IGNORED (single global adapter only), never an error."""
    if not isinstance(studio_adapters, dict):
        return None, None
    order = PRIORITY
    if BACKEND_KIND == "llama":
        # CPU path serves only the global tool_call adapter; drop user_style.
        order = [k for k in PRIORITY if k == "tool_call"] or ["tool_call"]
        if studio_adapters.get("user_style"):
            _log("llama.cpp CPU path: user_style adapter ignored (needs the GPU/vLLM "
                 "multi-LoRA path); serving tool_call only.")
    for kind in order:
        entry = studio_adapters.get(kind)
        if isinstance(entry, dict) and entry.get("uri"):
            return entry["uri"].strip(), kind
    return None, None


def _resolve_adapter_identity(studio_adapters):
    """Return the selected request entry without losing version or digest."""
    uri, kind = _resolve_adapter(studio_adapters)
    if not uri:
        return None, None
    entry = studio_adapters.get(kind)
    if REQUIRE_TOOL_ADAPTER:
        return _identity(entry), kind
    # Preserve the complete entry when supplied, while retaining legacy URI-only
    # behavior outside strict mode.
    return dict(entry), kind


# ── The proxy request handler ─────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet default access log; we log meaningfully above
        pass

    def _auth_ok(self):
        if not API_KEY:
            return True
        got = self.headers.get("Authorization", "")
        return got == f"Bearer {API_KEY}"

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        # BaseHTTPRequestHandler does not decode chunked request bodies. If we
        # treated one as an empty body, unread chunk bytes could be parsed as a
        # second request on this HTTP/1.1 connection. The private gateway needs
        # neither chunked uploads nor ambiguous duplicate lengths.
        if self.headers.get_all("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding is not supported")
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1:
            raise ValueError("multiple Content-Length headers are not supported")
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        return self.rfile.read(length) if length else b""

    # ---- routing ----
    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            status, body = _readiness()
            return self._send_json(status, body)
        if self.path.startswith("/v1/models"):
            if not self._auth_ok():
                return self._send_json(401, {"error": "unauthorized"})
            return self._proxy_passthrough("GET", "/v1/models", b"")
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send_json(401, {"error": "unauthorized"})
        if REQUIRE_TOOL_ADAPTER and BACKEND_KIND != "llama":
            return self._send_json(503, {
                "error": "strict adapter attestation is unavailable for this backend",
                "stage": "adapter_attestation_unsupported",
            })
        if self.path.startswith("/v1/chat/completions"):
            return self._handle_chat()
        if self.path.rstrip("/") == "/admin/load_adapter":
            return self._handle_admin_load()
        # Any other /v1/* verb we simply pass through (embeddings, completions…).
        if self.path.startswith("/v1/"):
            try:
                body = self._read_body()
            except ValueError as exc:
                return self._send_json(413 if "too large" in str(exc) else 400,
                                       {"error": str(exc)})
            return self._proxy_passthrough("POST", self.path, body)
        return self._send_json(404, {"error": "not found"})

    # ---- /v1/chat/completions: the adapter-aware path ----
    def _handle_chat(self):
        try:
            raw = self._read_body()
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            status = 413 if "too large" in str(exc) else 400
            return self._send_json(status, {"error": str(exc) if status == 413 else "invalid JSON body"})

        # studio_adapters arrives top-level (OpenAI extra_body merges into the body).
        studio_adapters = payload.pop("studio_adapters", None)
        adapter, kind = _resolve_adapter_identity(studio_adapters)
        uri = adapter.get("uri") if adapter else None

        if REQUIRE_TOOL_ADAPTER and adapter != _required_identity():
            _mismatch(adapter, _identity((_sync_engine_epoch().get("adapter") or {})))
            return self._send_json(503, {
                "error": "the exact required tool adapter was not requested",
                "stage": "adapter_identity_mismatch",
            })

        effective_model = BASE_MODEL_NAME
        if uri:
            name = _lora_name_for(uri, kind)
            if _ensure_loaded(uri, name, adapter):
                # vLLM selects the LoRA by the model field; llama keeps base model
                # (the global scale was already applied in _ensure_loaded_llama).
                if BACKEND_KIND != "llama":
                    effective_model = name
            else:
                if REQUIRE_TOOL_ADAPTER:
                    return self._send_json(503, {
                        "error": "the required tool adapter is not confirmed active",
                        "stage": "adapter_not_applied",
                    })
                _log(f"adapter {uri} unavailable → base model {BASE_MODEL_NAME} (fail-safe)")
        payload["model"] = effective_model
        if REQUIRE_TOOL_ADAPTER and BACKEND_KIND == "llama":
            # llama-server's per-request `lora` field overrides the global
            # scale. Never let a caller disable or replace the attested adapter
            # after readiness confirmed it. Removing the override makes this
            # completion use the globally confirmed id-0 scale and remains
            # compatible with BitNet forks predating per-request LoRA fields.
            payload.pop("lora", None)

        stream = bool(payload.get("stream"))
        out = json.dumps(payload).encode()
        self._proxy("POST", "/v1/chat/completions", out, stream=stream)

    # ---- trainer push hook ----
    def _handle_admin_load(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except ValueError as exc:
            status = 413 if "too large" in str(exc) else 400
            return self._send_json(status, {"error": str(exc) if status == 413 else "invalid JSON body"})
        uri = (body.get("uri") or "").strip()
        if not uri:
            return self._send_json(400, {"error": "uri is required"})
        adapter = _identity(body) if REQUIRE_TOOL_ADAPTER else dict(body)
        if REQUIRE_TOOL_ADAPTER and adapter != _required_identity():
            return self._send_json(409, {
                "error": "adapter identity does not match the pinned deployment",
            })
        kind = (body.get("kind") or "tool_call").strip()
        name = (body.get("name") or "").strip() or _lora_name_for(uri, kind)
        with _NAME_LOCK:
            _URI_TO_NAME[uri] = name
        # Force a fresh load even if a name was seen before (new weights, same slot).
        _LOADED.discard(name)
        ok = _ensure_loaded(uri, name, adapter)
        return self._send_json(200 if ok else 502,
                               {"loaded": ok, "name": name, "uri": uri, "kind": kind,
                                "version": body.get("version"), "sha256": body.get("sha256")})

    # ---- proxy helpers ----
    def _proxy_passthrough(self, method, path, body):
        self._proxy(method, path, body, stream=False)

    def _proxy(self, method, path, body, stream):
        """Forward to the backend and relay the response. Streams SSE chunks
        through untouched when stream=True."""
        # path already begins with /v1/... and BACKEND_URL ends with /v1, so join
        # against the engine ROOT to avoid a doubled /v1.
        url = _ROOT + path
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=body or None, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(
                req, timeout=min(TIMEOUT, MAX_STREAM_SECONDS) if stream else TIMEOUT)
        except urllib.error.HTTPError as e:
            detail = e.read(min(MAX_RESPONSE_BYTES, 64 * 1024))
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(detail)))
            self.end_headers()
            self.wfile.write(detail)
            return
        except Exception as e:
            return self._send_json(502, {"error": f"backend unreachable: {e}"})

        ctype = resp.headers.get("Content-Type", "application/json")
        if stream or "text/event-stream" in ctype:
            # Relay chunked/SSE without buffering: no Content-Length, flush each read.
            # Close after the stream so completion doesn't depend only on the SSE
            # [DONE] sentinel (BaseHTTPRequestHandler honors close_connection, not a
            # manually written Connection header) and the worker thread frees up.
            self.close_connection = True
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype or "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            started = time.monotonic()
            relayed = 0
            try:
                while True:
                    if time.monotonic() - started > MAX_STREAM_SECONDS:
                        _log("stream relay reached its duration limit")
                        break
                    chunk = resp.read(min(4096, MAX_RESPONSE_BYTES - relayed + 1))
                    if not chunk:
                        break
                    relayed += len(chunk)
                    if relayed > MAX_RESPONSE_BYTES:
                        _log("stream relay reached its byte limit")
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception as e:
                _log(f"stream relay ended: {e}")
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
            return
        data = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            try:
                resp.close()
            except Exception:
                pass
            return self._send_json(502, {"error": "backend response exceeded the configured limit"})
        self.send_response(resp.status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    _log(f"starting on {HOST}:{PORT} → backend {BACKEND_URL} (kind={BACKEND_KIND}, "
         f"base={BASE_MODEL_NAME}, priority={PRIORITY})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
