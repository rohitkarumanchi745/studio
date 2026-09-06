#!/usr/bin/env python3
"""Studio BitNet serving supervisor — the whole serving unit in ONE container.

WHY THIS EXISTS
───────────────
`docker-compose.yml` runs the serving unit as TWO containers (engine + gateway)
on a shared bind-mount. Railway runs ONE container per service and a volume
belongs to exactly one service, so the compose shape does not survive the move
(see RAILWAY.md §1 for the full argument). This process is PID 1 of the single
Railway container and owns both halves:

    supervisor.py (PID 1)
      ├── bridge      [::]:$PORT  ──▶ 127.0.0.1:$STUDIO_GATEWAY_PORT   (dual-stack)
      ├── gateway.py  127.0.0.1:$STUDIO_GATEWAY_PORT ──▶ engine        (unchanged)
      └── llama-server (bitnet.cpp build) 127.0.0.1:$STUDIO_ENGINE_PORT
                       -m  <volume>/models/<gguf>
                       --lora <volume>/adapters/<gguf>   ← ONLY IF IT EXISTS

FOUR THINGS IT SOLVES
─────────────────────
1. BOOT ORDER. The gateway and the public listener come up FIRST, in seconds, so
   Railway's healthcheck passes while the 1.1 GB model is still downloading. A
   call that arrives early gets the gateway's own 502 fail-safe, and Studio
   escalates to the frontier — which is exactly what it does today.

2. NO MODEL IN GIT. The GGUF is fetched once onto the Railway volume and reused
   on every later boot. A gated/rate-limited HuggingFace pull fails with a
   message that names HUGGING_FACE_HUB_TOKEN instead of a stack trace.

3. NO ADAPTER ON DAY ONE. `--lora FNAME` is a STARTUP flag and llama-server
   refuses to start if the file is missing — the compose CPU command hard-codes
   it, so a fresh deployment would crash-loop forever. Here the flag is added
   only when the file is actually on disk, so the unit boots and serves the BASE
   model with no adapter, which is the state every new deployment starts in.

4. ADAPTER ARRIVAL. llama-server cannot hot-load an adapter FILE (only re-scale
   one mounted at startup — see README §3). So this polls the adapter path and,
   when a stable new file appears or changes, restarts ONLY llama-server with the
   flag added. The gateway and the public port never go down; the engine is
   unavailable for the few seconds of reload, during which the gateway falls back
   and Studio escalates. That is the difference between a serving box that starts
   and one that crash-loops.

CONFIG (env) — every value has a working default
────────────────────────────────────────────────
  PORT                        public/private listen port          (Railway sets it; 9000)
  STUDIO_DATA_DIR             Railway volume mount                (/data)
  STUDIO_MODELS_DIR           $STUDIO_DATA_DIR/models
  STUDIO_ADAPTERS_DIR         $STUDIO_DATA_DIR/adapters
  STUDIO_BITNET_GGUF          model filename                      (ggml-model-i2_s.gguf)
  STUDIO_BITNET_GGUF_REPO     HF repo            (microsoft/bitnet-b1.58-2B-4T-gguf)
  STUDIO_BITNET_GGUF_URL      full override URL                   (derived from repo+file)
  STUDIO_BITNET_GGUF_BYTES    expected size, 0 disables the check (1187801280)
  HUGGING_FACE_HUB_TOKEN      (or HF_TOKEN) for gated/rate-limited pulls
  STUDIO_TOOLCALL_GGUF        adapter filename                    (tool_call.gguf)
  STUDIO_ADAPTER_URL          optional: fetch the adapter at boot if absent
  STUDIO_MODEL_FETCH_RETRIES  download attempts before giving up (3)
  STUDIO_ENGINE_PORT          llama-server, loopback only         (8080)
  STUDIO_GATEWAY_PORT         gateway, loopback only              (9001)
  STUDIO_SUPERVISOR_BRIDGE    1 = dual-stack bridge on $PORT      (1)
  STUDIO_ADAPTER_POLL_SECONDS adapter watch interval              (15)
  LLAMA_THREADS               engine threads   (default: this container's CPU quota)
  LLAMA_CTX_SIZE              context window                      (4096 = the model's max)
  LLAMA_PARALLEL              server slots; N>1 SPLITS the context (1)
  LLAMA_EXTRA_ARGS            extra llama-server argv, shlex-split ("")
  STUDIO_BASE_MODEL_NAME      model id clients send == --alias     (bitnet)
  STUDIO_GATEWAY_API_KEY      bearer auth on the gateway           (unset)

Stdlib only, like gateway.py — nothing to install, nothing to drift.
"""
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def log(*a):
    print("[supervisor]", *a, file=sys.stderr, flush=True)


def env(name, default=""):
    return os.getenv(name, default).strip()


def env_int(name, default):
    try:
        return int(env(name, "") or default)
    except ValueError:
        return int(default)


# ── Paths ────────────────────────────────────────────────────────────────
DATA_DIR = env("STUDIO_DATA_DIR", "/data")
MODELS_DIR = env("STUDIO_MODELS_DIR", os.path.join(DATA_DIR, "models"))
ADAPTERS_DIR = env("STUDIO_ADAPTERS_DIR", os.path.join(DATA_DIR, "adapters"))
GGUF_NAME = env("STUDIO_BITNET_GGUF", "ggml-model-i2_s.gguf")
GGUF_REPO = env("STUDIO_BITNET_GGUF_REPO", "microsoft/bitnet-b1.58-2B-4T-gguf")
GGUF_URL = env("STUDIO_BITNET_GGUF_URL") or \
    f"https://huggingface.co/{GGUF_REPO}/resolve/main/{GGUF_NAME}"
# Verified with an HTTP HEAD against the repo: ggml-model-i2_s.gguf is exactly
# 1,187,801,280 bytes. A short file means a truncated transfer, not a model.
GGUF_BYTES = env_int("STUDIO_BITNET_GGUF_BYTES", 1187801280)
ADAPTER_NAME = env("STUDIO_TOOLCALL_GGUF", "tool_call.gguf")
ADAPTER_URL = env("STUDIO_ADAPTER_URL")
MODEL_PATH = os.path.join(MODELS_DIR, GGUF_NAME)
ADAPTER_PATH = os.path.join(ADAPTERS_DIR, ADAPTER_NAME)
# The supervisor is the only process that knows what it actually did — whether
# the model landed, whether the engine came up, and WHICH adapter file it
# launched with. The gateway serves /health and answers Studio, but it starts
# BEFORE any of that is true. This file is how the one that knows tells the one
# that answers; without it /health can only guess, and guessing "ok" while a
# 1.1 GB download is in flight is how a platform routes traffic into a void.
STATE_PATH = os.path.join(DATA_DIR, "state.json")
# Written beside the adapter when its provenance is known: the published uri the
# file came from. Without it the mounted adapter is anonymous, and an anonymous
# adapter can never be PROVEN to be the one Studio asked for.
ADAPTER_URI_PATH = ADAPTER_PATH + ".uri"

# ── Ports ────────────────────────────────────────────────────────────────
PUBLIC_PORT = env_int("PORT", 9000)          # Railway injects PORT
GATEWAY_PORT = env_int("STUDIO_GATEWAY_PORT", 9001)
ENGINE_PORT = env_int("STUDIO_ENGINE_PORT", 8080)
BRIDGE = env("STUDIO_SUPERVISOR_BRIDGE", "1").lower() in ("1", "true", "yes")

# ── Engine tuning ────────────────────────────────────────────────────────
CTX_SIZE = env_int("LLAMA_CTX_SIZE", 4096)   # = the model's max_position_embeddings
PARALLEL = env_int("LLAMA_PARALLEL", 1)
BASE_MODEL_NAME = env("STUDIO_BASE_MODEL_NAME", "bitnet")
ENGINE_BIN = env("STUDIO_ENGINE_BIN", "/opt/bitnet/bin/llama-server")
POLL_SECONDS = env_int("STUDIO_ADAPTER_POLL_SECONDS", 15)
FETCH_RETRIES = max(1, env_int("STUDIO_MODEL_FETCH_RETRIES", 3))

_STOP = threading.Event()


# ── CPU quota ────────────────────────────────────────────────────────────

def cpu_quota():
    """Threads this container may actually use.

    os.cpu_count() reports the HOST's cores, not the container's cgroup limit —
    on a shared cloud host that is often 32-64 while the service is entitled to
    2. Handing llama-server 64 threads on a 2-vCPU slice makes it slower, not
    faster (the threads fight over the same slice and burn the CPU budget you
    are billed for). So read the cgroup quota first and fall back to the count."""
    n = os.cpu_count() or 1
    try:                                       # cgroup v2
        with open("/sys/fs/cgroup/cpu.max", encoding="utf-8") as f:
            quota, period = (f.read().split() + ["100000"])[:2]
        if quota != "max":
            n = min(n, max(1, round(int(quota) / int(period))))
            return n
    except (OSError, ValueError):
        pass
    try:                                       # cgroup v1
        with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", encoding="utf-8") as f:
            quota = int(f.read().strip())
        with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", encoding="utf-8") as f:
            period = int(f.read().strip())
        if quota > 0 and period > 0:
            n = min(n, max(1, round(quota / period)))
    except (OSError, ValueError):
        pass
    return max(1, n)


THREADS = env_int("LLAMA_THREADS", 0) or cpu_quota()


# ── Downloads (model + optional adapter) ─────────────────────────────────

def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _hf_token():
    return env("HUGGING_FACE_HUB_TOKEN") or env("HF_TOKEN")


def _explain_http(code, url, what):
    """Turn an HTTP status into an instruction, not a stack trace."""
    tokened = "set" if _hf_token() else "NOT set"
    if code in (401, 403):
        return (
            f"{what}: HuggingFace refused the download of {url} (HTTP {code}).\n"
            f"  HUGGING_FACE_HUB_TOKEN is currently {tokened}.\n"
            f"  The repo is gated, private, or the token is invalid/expired.\n"
            f"  FIX: on this Railway service, Variables → add HUGGING_FACE_HUB_TOKEN\n"
            f"       = a HuggingFace access token with READ access to {GGUF_REPO}\n"
            f"       (huggingface.co/settings/tokens), accept the model's licence on\n"
            f"       the model page if it asks, then redeploy.")
    if code == 429:
        return (
            f"{what}: HuggingFace rate-limited the download of {url} (HTTP 429).\n"
            f"  HUGGING_FACE_HUB_TOKEN is currently {tokened}.\n"
            f"  FIX: set HUGGING_FACE_HUB_TOKEN on this Railway service — authenticated\n"
            f"       pulls get a far higher limit — or host the file yourself and set\n"
            f"       STUDIO_BITNET_GGUF_URL to that URL.")
    if code == 404:
        return (f"{what}: {url} does not exist (HTTP 404). Check "
                f"STUDIO_BITNET_GGUF_REPO / STUDIO_BITNET_GGUF, or set "
                f"STUDIO_BITNET_GGUF_URL to the exact file URL.")
    return f"{what}: {url} returned HTTP {code}."


def fetch(url, dest, expect_bytes=0, what="download", retries=3):
    """Download to <dest>.part and rename only on success — a half-written file
    must never look like a model. Returns True, or logs WHY and returns False."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    part = dest + ".part"
    headers = []
    tok = _hf_token()
    if tok and "huggingface.co" in url:
        headers = ["-H", f"Authorization: Bearer {tok}"]
    for attempt in range(1, retries + 1):
        # -L: HF 302s to a pre-signed CDN URL. curl drops the Authorization
        # header on a cross-host redirect, which is correct — the CDN URL is
        # already signed. Attempt 2+ resumes the partial file (-C -).
        cmd = ["curl", "-sS", "-L", "--fail-with-body",
               "--connect-timeout", "20", "--max-time", "3600",
               "--retry", "2", "--retry-delay", "3",
               "-w", "%{http_code}", "-o", part] + headers
        if attempt > 1 and os.path.exists(part):
            cmd += ["-C", "-"]
        cmd.append(url)
        log(f"{what}: attempt {attempt}/{retries} → {url}")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            log(f"{what}: FATAL — `curl` is not installed in this image.")
            return False
        code = (p.stdout or "").strip()[-3:]
        if p.returncode == 0 and code in ("200", "206"):
            # The transfer SUCCEEDED at the HTTP level — so anything wrong with
            # the bytes is wrong content, not a truncated stream. Discard the
            # part file in that case: a later attempt resuming with `-C -` would
            # otherwise append the real model onto an error page.
            size = os.path.getsize(part) if os.path.exists(part) else 0
            with open(part, "rb") as f:
                magic = f.read(4)
            if magic != b"GGUF":
                log(f"{what}: the server returned {size} bytes that are not a GGUF "
                    f"file (magic={magic!r}) — probably an error page or a git-LFS "
                    f"pointer, not the model. Check {url}.")
                _rm(part)
                return False
            if expect_bytes and size != expect_bytes:
                log(f"{what}: WRONG SIZE — got {size} bytes, expected {expect_bytes}. "
                    f"Discarding and retrying. (If the upstream file legitimately "
                    f"changed, set STUDIO_BITNET_GGUF_BYTES to the new size, or 0 "
                    f"to skip this check.)")
                _rm(part)
                continue
            os.replace(part, dest)
            log(f"{what}: OK — {size} bytes → {dest}")
            return True
        if code.isdigit() and int(code) >= 400:
            log(_explain_http(int(code), url, what))
            if int(code) in (401, 403, 404):
                return False          # retrying will not fix a permission problem
        else:
            log(f"{what}: transfer failed (curl exit {p.returncode}): "
                f"{(p.stderr or '').strip()[:300]}")
        time.sleep(min(30, 5 * attempt))
    return False


def ensure_model():
    """The model must exist before the engine can start. Present on the volume →
    instant. Absent → one 1.1 GB pull, then never again for this volume."""
    if os.path.exists(MODEL_PATH) and os.path.getsize(MODEL_PATH) > 1024:
        log(f"model present: {MODEL_PATH} ({os.path.getsize(MODEL_PATH)} bytes)")
        return True
    free = shutil.disk_usage(os.path.dirname(MODEL_PATH) or "/").free
    need = (GGUF_BYTES or 1_200_000_000) + 200_000_000
    if free < need:
        log(f"WARNING: only {free // 2**20} MiB free at {MODELS_DIR}; the model needs "
            f"~{need // 2**20} MiB. Attach a Railway volume (≥5 GB) mounted at "
            f"{DATA_DIR}, or the download will fail.")
    log(f"model missing — downloading once onto the volume ({DATA_DIR}). "
        f"This takes a few minutes on first boot only.")
    return fetch(GGUF_URL, MODEL_PATH, GGUF_BYTES, "model", FETCH_RETRIES)


def ensure_adapter():
    """OPTIONAL. A Railway volume cannot be shared with the trainer's service, so
    the trained tool_call adapter (converted to GGUF) arrives by URL if you set
    one. No URL and no file is the NORMAL day-one state: base model, no adapter."""
    if os.path.exists(ADAPTER_PATH):
        return True
    if not ADAPTER_URL:
        return False
    return fetch(ADAPTER_URL, ADAPTER_PATH, 0, "adapter", 2)


# ── Shared state: what the gateway is allowed to claim ───────────────────

def mounted_adapter():
    """The adapter the engine was LAUNCHED with, as an identity the gateway can
    compare against a requested uri — or None when the engine is serving the
    base model. `uri` is None when the file has no .uri sidecar: present but
    anonymous, which must NOT be treated as a match for anything."""
    sig = adapter_sig()
    if sig is None:
        return None
    uri = None
    try:
        with open(ADAPTER_URI_PATH) as f:
            uri = f.read().strip() or None
    except OSError:
        pass
    return {"path": ADAPTER_PATH, "uri": uri, "size": sig[0], "mtime_ns": sig[1]}


def write_state(stage, adapter=None, detail=None):
    """Publish the unit's real state for the gateway to serve on /health.

    stage is the honest answer to "can this box serve a request right now?":
      starting | downloading_model | model_failed | starting_engine |
      ready | engine_down | stopping
    Only `ready` means yes. Written atomically — the gateway reads it on every
    health check and a torn read would be a lie of a different kind.
    """
    payload = {"stage": stage, "detail": detail, "adapter": adapter,
               "model_path": MODEL_PATH, "engine_port": ENGINE_PORT,
               "updated_at": time.time()}
    tmp = STATE_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(STATE_PATH) or "/", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, STATE_PATH)
    except OSError as e:                 # never let bookkeeping kill the unit
        log(f"could not write {STATE_PATH}: {e}")


# ── Adapter presence, as a value that can change ─────────────────────────

def adapter_sig():
    """(size, mtime_ns) of the adapter file, or None when there isn't one.
    Comparing this across polls is how a NEW adapter is noticed."""
    try:
        st = os.stat(ADAPTER_PATH)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def stable_adapter_sig(settle=2.0):
    """Same as adapter_sig, but None unless the file stopped changing. A file
    still being written (or downloaded) is not an adapter yet — starting the
    engine on a half-written GGUF is a crash, and llama-server exits on a bad
    --lora file, which would look exactly like the crash-loop we are avoiding."""
    first = adapter_sig()
    if first is None:
        return None
    time.sleep(settle)
    return first if adapter_sig() == first else None


# ── Engine argv: the conditional --lora that makes day one work ──────────

def engine_argv(adapter_present):
    """llama-server's command line. `--lora` is a STARTUP flag and the server
    exits if the file is missing, so it is present ONLY when the file is."""
    argv = [
        ENGINE_BIN,
        "-m", MODEL_PATH,
        "--alias", BASE_MODEL_NAME,      # == STUDIO_BASE_MODEL_NAME the gateway sends
        "--host", "127.0.0.1",           # loopback: only the gateway may reach it
        "--port", str(ENGINE_PORT),
        "--ctx-size", str(CTX_SIZE),
        "--threads", str(THREADS),
        "--threads-batch", str(THREADS),
        "--parallel", str(PARALLEL),
        "--cont-batching",
        "--no-webui",                    # API only; no static UI on a public port
    ]
    if adapter_present:
        # --lora-init-without-apply mounts it at scale 0; gateway.py's
        # POST /lora-adapters [{"id":0,"scale":1.0}] is what turns it on, exactly
        # as in docker-compose.yml's cpu profile.
        argv += ["--lora", ADAPTER_PATH, "--lora-init-without-apply"]
    argv += shlex.split(env("LLAMA_EXTRA_ARGS"))
    return argv


def gateway_env():
    """gateway.py's config. It talks to the engine over loopback and listens on
    loopback; the bridge owns the public port. Nothing in gateway.py changes."""
    e = dict(os.environ)
    e.update({
        "STUDIO_BACKEND_URL": f"http://127.0.0.1:{ENGINE_PORT}/v1",
        "STUDIO_BACKEND_KIND": "llama",
        "STUDIO_BASE_MODEL_NAME": BASE_MODEL_NAME,
        # CPU/llama path serves the single global tool_call adapter (README §7).
        "STUDIO_GATEWAY_ADAPTER_PRIORITY": env(
            "STUDIO_GATEWAY_ADAPTER_PRIORITY", "tool_call"),
    })
    if BRIDGE:
        e["STUDIO_GATEWAY_HOST"] = "127.0.0.1"
        e["STUDIO_GATEWAY_PORT"] = str(GATEWAY_PORT)
    else:
        # No bridge: the gateway itself takes the public port. http.server is
        # AF_INET only, so this is IPv4-only — fine on a public domain and on
        # Railway environments created after 2025-10-16 (dual-stack private
        # network), NOT on a legacy IPv6-only private network. See RAILWAY.md §6.
        e["STUDIO_GATEWAY_HOST"] = env("STUDIO_GATEWAY_HOST", "0.0.0.0")
        e["STUDIO_GATEWAY_PORT"] = str(PUBLIC_PORT)
    return e


# ── Dual-stack bridge: [::]:$PORT → 127.0.0.1:$GATEWAY_PORT ──────────────
# Railway's private network resolves <service>.railway.internal to IPv6 (and, in
# environments created after 2025-10-16, also IPv4). gateway.py uses
# http.server, which is AF_INET-only, so on a legacy environment nothing could
# reach it privately. This is a raw byte splice — no HTTP parsing — so SSE
# streaming, chunked encoding and keep-alive pass through untouched.

def _splice(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle_conn(client, target_port):
    upstream = None
    try:
        upstream = socket.create_connection(("127.0.0.1", target_port), timeout=30)
        upstream.settimeout(None)
        client.settimeout(None)
        t = threading.Thread(target=_splice, args=(client, upstream), daemon=True)
        t.start()
        _splice(upstream, client)
        t.join(timeout=5)
    except OSError as e:
        log(f"bridge: upstream connect failed: {e}")
    finally:
        for s in (client, upstream):
            try:
                if s:
                    s.close()
            except OSError:
                pass


def make_bridge_socket(port):
    """Prefer a dual-stack AF_INET6 listener (accepts IPv6 AND IPv4-mapped);
    fall back to AF_INET where IPv6 is unavailable."""
    try:
        srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            log("bridge: IPV6_V6ONLY=0 rejected; IPv6-only listener")
        srv.bind(("::", port))
    except OSError as e:
        log(f"bridge: IPv6 bind failed ({e}); falling back to IPv4")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
    srv.listen(128)
    return srv


def serve_bridge(listen_port, target_port, ready=None):
    srv = make_bridge_socket(listen_port)
    log(f"bridge listening on [::]:{listen_port} → 127.0.0.1:{target_port}")
    if ready is not None:
        ready.set()
    try:
        while not _STOP.is_set():
            try:
                conn, _addr = srv.accept()
            except OSError:
                if _STOP.is_set():
                    break
                continue
            threading.Thread(target=_handle_conn, args=(conn, target_port),
                             daemon=True).start()
    finally:
        srv.close()


# ── Process management ───────────────────────────────────────────────────

def start_gateway():
    argv = [sys.executable, "-u", os.path.join(HERE, "gateway.py")]
    log(f"starting gateway: {' '.join(argv)}")
    return subprocess.Popen(argv, env=gateway_env())


def start_engine(adapter_present):
    argv = engine_argv(adapter_present)
    log(f"starting engine ({'WITH' if adapter_present else 'WITHOUT'} adapter): "
        f"{' '.join(argv)}")
    return subprocess.Popen(argv)


def stop(proc, name, timeout=20):
    if proc is None or proc.poll() is not None:
        return
    log(f"stopping {name}")
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"{name} did not exit; killing")
        proc.kill()


def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    os.makedirs(ADAPTERS_DIR, exist_ok=True)
    log(f"data={DATA_DIR} model={MODEL_PATH} adapter={ADAPTER_PATH} "
        f"threads={THREADS} ctx={CTX_SIZE} public_port={PUBLIC_PORT}")

    gateway = engine = None

    def shutdown(signum, _frame):
        log(f"signal {signum} — shutting down")
        _STOP.set()
        stop(engine, "engine")
        stop(gateway, "gateway")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # 1. Public listener + gateway FIRST, so Railway's healthcheck on /health
    #    passes in seconds even while a 1.1 GB model is still downloading.
    write_state("starting")
    gateway = start_gateway()
    if BRIDGE:
        ready = threading.Event()
        threading.Thread(target=serve_bridge,
                         args=(PUBLIC_PORT, GATEWAY_PORT, ready),
                         daemon=True).start()
        ready.wait(timeout=10)

    # 2. Model onto the volume (once per volume). /health reports 503
    #    "downloading_model" throughout, so nothing routes here yet.
    write_state("downloading_model")
    if not ensure_model():
        write_state("model_failed", detail="the model file could not be fetched")
        log("FATAL: no model file — the engine cannot start. See the message "
            "above for the fix. Exiting so Railway surfaces a failed deploy "
            "rather than a permanently empty serving box.")
        stop(gateway, "gateway")
        return 1

    # 3. Engine, with --lora ONLY if an adapter is really there.
    write_state("starting_engine")
    ensure_adapter()
    live_sig = stable_adapter_sig()
    if live_sig is None:
        log(f"no adapter at {ADAPTER_PATH} — serving the BASE model. This is the "
            f"normal state for a new deployment: router.bitnet_ready() is still "
            f"False until scripts/train_online.py publishes a tool_call adapter, "
            f"so Studio sends nothing here yet.")
    engine = start_engine(live_sig is not None)
    # The engine is up but not yet answering; the gateway probes it before it
    # reports ready, so this stage is honest about the gap.
    write_state("ready", adapter=mounted_adapter())

    # 4. Watch: adapter arrivals restart the engine; a dead child is handled.
    fails = 0
    while not _STOP.is_set():
        time.sleep(POLL_SECONDS)
        if gateway.poll() is not None:
            log(f"FATAL: gateway exited ({gateway.returncode}); "
                f"exiting so Railway restarts the service.")
            stop(engine, "engine")
            return 1
        if engine.poll() is not None:
            fails += 1
            write_state("engine_down", adapter=mounted_adapter(),
                        detail=f"engine exited ({engine.returncode})")
            log(f"engine exited ({engine.returncode}) — restart {fails}/5")
            if fails >= 5:
                log("FATAL: engine will not stay up. Most likely causes: the GGUF "
                    "is not loadable by this build, the container ran out of "
                    "memory (needs ~2 GB at ctx 4096), or LLAMA_EXTRA_ARGS is "
                    "invalid. Exiting so Railway surfaces the failure.")
                stop(gateway, "gateway")
                return 1
            time.sleep(min(60, 5 * fails))
            live_sig = stable_adapter_sig()
            engine = start_engine(live_sig is not None)
            write_state("ready", adapter=mounted_adapter())
            continue
        fails = 0
        sig = stable_adapter_sig()
        if sig != live_sig:
            log(f"adapter changed ({live_sig} → {sig}) — restarting the engine to "
                f"mount it (llama-server cannot hot-load an adapter FILE).")
            write_state("starting_engine", detail="mounting a new adapter")
            stop(engine, "engine")
            live_sig = sig
            engine = start_engine(sig is not None)
            # Only now is the new adapter really the one being served — the
            # gateway will not claim it before this line runs.
            write_state("ready", adapter=mounted_adapter())
    return 0


if __name__ == "__main__":
    sys.exit(main())
