#!/usr/bin/env python3
"""Studio adapter-aware OpenAI gateway — the trainer↔serve glue.

WHY THIS EXISTS
───────────────
Studio's app (backend/app/agent.py make_llm) talks to a self-hosted BitNet as an
OpenAI-compatible endpoint. On every /v1/chat/completions it sends:

    model = "bitnet"                       # STUDIO_BITNET_LLM = "openai:bitnet"
    extra_body["studio_adapters"] = {      # from trainer.active_adapters(user_id)
        "tool_call":  {"uri": "<adapter uri>", "version": N},
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
                  "studio_adapters": { "tool_call": {"uri","version"},
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
  STUDIO_GATEWAY_HOST         bind host                       (default 0.0.0.0)
  STUDIO_GATEWAY_PORT         bind port                       (default 9000)
  STUDIO_GATEWAY_ADAPTER_PRIORITY  kinds, most-specific first (default "user_style,tool_call")
  STUDIO_GATEWAY_TIMEOUT      upstream timeout seconds        (default 600)
  STUDIO_GATEWAY_API_KEY      if set, require Authorization: Bearer <key> on /v1/* + /admin/*

Dependency-light on purpose: pure Python stdlib (http.server + urllib). No FastAPI,
no httpx — so the gateway image is tiny and nothing here can drift the app image.
"""
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ── Config ────────────────────────────────────────────────────────────────
BACKEND_URL = os.getenv("STUDIO_BACKEND_URL", "http://vllm:8000/v1").rstrip("/")
BACKEND_KIND = os.getenv("STUDIO_BACKEND_KIND", "vllm").strip().lower()
BASE_MODEL_NAME = os.getenv("STUDIO_BASE_MODEL_NAME", "bitnet").strip()
HOST = os.getenv("STUDIO_GATEWAY_HOST", "0.0.0.0")
PORT = int(os.getenv("STUDIO_GATEWAY_PORT", "9000"))
PRIORITY = [k.strip() for k in os.getenv(
    "STUDIO_GATEWAY_ADAPTER_PRIORITY", "user_style,tool_call").split(",") if k.strip()]
TIMEOUT = int(os.getenv("STUDIO_GATEWAY_TIMEOUT", "600"))
API_KEY = os.getenv("STUDIO_GATEWAY_API_KEY", "").strip()

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
        raw = r.read().decode() or "{}"
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, {"raw": raw}


# ── Backend adapter loading (engine-specific, idempotent, fail-safe) ──────

def _ensure_loaded_vllm(uri, name):
    """Ensure a vLLM LoRA is loaded. POST /v1/load_lora_adapter with
    {lora_name, lora_path}. Idempotent: if it's already known loaded, no-op;
    a 'already loaded'/409 from vLLM is treated as success. Returns True on
    success, False on failure (caller falls back to the base model).
    Requires the server started with --enable-lora AND
    VLLM_ALLOW_RUNTIME_LORA_UPDATING=True (see README)."""
    if name in _LOADED:
        return True
    try:
        status, _ = _http_json(
            "POST", f"{BACKEND_URL}/load_lora_adapter",
            {"lora_name": name, "lora_path": uri}, timeout=120)
        _LOADED.add(name)
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
            _log(f"vLLM LoRA {name} already present ← {uri}")
            return True
        _log(f"vLLM load FAILED {name} ← {uri}: HTTP {e.code} {detail}")
        return False
    except Exception as e:
        _log(f"vLLM load FAILED {name} ← {uri}: {e}")
        return False


def _ensure_loaded_llama(uri, name):
    """llama.cpp CPU path. llama-server does NOT dynamically load an adapter FILE
    at runtime — adapters are fixed at startup via --lora/--lora-scaled; only their
    SCALE is adjustable at runtime via POST /lora-adapters. So here we enable the
    mounted tool_call adapter (id 0) at scale 1.0. If the freshly trained file
    isn't the one mounted, the server must be restarted (see README CPU caveat).
    We optimistically enable adapter id 0; failure → base model (fail-safe)."""
    if name in _LOADED:
        return True
    try:
        # Set the (single, mounted) global adapter's scale to 1.0.
        _http_json("POST", f"{_ROOT}/lora-adapters", [{"id": 0, "scale": 1.0}], timeout=30)
        _LOADED.add(name)
        _log(f"llama.cpp enabled mounted LoRA id0 for {name} (uri={uri})")
        return True
    except Exception as e:
        _log(f"llama.cpp enable FAILED for {name}: {e}")
        return False


def _ensure_loaded(uri, name):
    if BACKEND_KIND == "llama":
        return _ensure_loaded_llama(uri, name)
    return _ensure_loaded_vllm(uri, name)


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
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---- routing ----
    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._send_json(200, {
                "ok": True, "backend": BACKEND_URL, "kind": BACKEND_KIND,
                "base_model": BASE_MODEL_NAME, "loaded_adapters": sorted(_LOADED),
                "priority": PRIORITY})
        if self.path.startswith("/v1/models"):
            if not self._auth_ok():
                return self._send_json(401, {"error": "unauthorized"})
            return self._proxy_passthrough("GET", "/v1/models", b"")
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            return self._send_json(401, {"error": "unauthorized"})
        if self.path.startswith("/v1/chat/completions"):
            return self._handle_chat()
        if self.path.rstrip("/") == "/admin/load_adapter":
            return self._handle_admin_load()
        # Any other /v1/* verb we simply pass through (embeddings, completions…).
        if self.path.startswith("/v1/"):
            return self._proxy_passthrough("POST", self.path, self._read_body())
        return self._send_json(404, {"error": "not found"})

    # ---- /v1/chat/completions: the adapter-aware path ----
    def _handle_chat(self):
        raw = self._read_body()
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._send_json(400, {"error": "invalid JSON body"})

        # studio_adapters arrives top-level (OpenAI extra_body merges into the body).
        studio_adapters = payload.pop("studio_adapters", None)
        uri, kind = _resolve_adapter(studio_adapters)

        effective_model = BASE_MODEL_NAME
        if uri:
            name = _lora_name_for(uri, kind)
            if _ensure_loaded(uri, name):
                # vLLM selects the LoRA by the model field; llama keeps base model
                # (the global scale was already applied in _ensure_loaded_llama).
                if BACKEND_KIND != "llama":
                    effective_model = name
            else:
                _log(f"adapter {uri} unavailable → base model {BASE_MODEL_NAME} (fail-safe)")
        payload["model"] = effective_model

        stream = bool(payload.get("stream"))
        out = json.dumps(payload).encode()
        self._proxy("POST", "/v1/chat/completions", out, stream=stream)

    # ---- trainer push hook ----
    def _handle_admin_load(self):
        try:
            body = json.loads(self._read_body() or b"{}")
        except ValueError:
            return self._send_json(400, {"error": "invalid JSON body"})
        uri = (body.get("uri") or "").strip()
        if not uri:
            return self._send_json(400, {"error": "uri is required"})
        kind = (body.get("kind") or "tool_call").strip()
        name = (body.get("name") or "").strip() or _lora_name_for(uri, kind)
        with _NAME_LOCK:
            _URI_TO_NAME[uri] = name
        # Force a fresh load even if a name was seen before (new weights, same slot).
        _LOADED.discard(name)
        ok = _ensure_loaded(uri, name)
        return self._send_json(200 if ok else 502,
                               {"loaded": ok, "name": name, "uri": uri, "kind": kind})

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
            resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            detail = e.read()
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
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception as e:
                _log(f"stream relay ended: {e}")
            return
        data = resp.read()
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
