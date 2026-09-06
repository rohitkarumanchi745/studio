#!/usr/bin/env python3
"""Run the Studio BitNet serving unit ON YOUR OWN MACHINE — one command, no
Docker, no Railway, no paid service.

WHAT THIS IS
────────────
`supervisor.py` already is the whole serving unit: it downloads the GGUF, starts
`llama-server`, starts `gateway.py` in front of it, adds `--lora` only when an
adapter really exists, restarts the engine when a new one lands, and publishes
the state file that lets `/health` stop guessing. All of that is correct off a
container too — but it was written FOR one, so its defaults are container
defaults: `/data`, `/opt/bitnet/bin/llama-server`, `PORT` injected by the
platform, a dual-stack bridge for Railway's IPv6 private network.

This file is the local entrypoint. It does not reimplement any of that. It
resolves laptop-shaped paths, finds the `llama-server` YOU built, sets the
environment `supervisor.load_config()` reads, and calls `supervisor.main()`.
Every download, restart, adapter watch and state write is the same code that
runs in the container — which is the point: what you debug here is what ships.

    run_local.py ──sets env──▶ supervisor.load_config()
                  ──calls───▶ supervisor.main()
                                ├── gateway.py     127.0.0.1:<--port>   ← Studio talks here
                                └── llama-server    127.0.0.1:<engine>  ← bitnet.cpp build
                                      -m  <--dir>/models/<gguf>
                                      --lora <--dir>/adapters/tool_call.gguf  (if present)

THE HARDWARE SPLIT, BECAUSE IT SURPRISES EVERYONE
─────────────────────────────────────────────────
SERVING BitNet is a **CPU** job and your GPU does not help. Stock vLLM cannot
load BitNet at all (vllm#17279, "not planned"), and the supported runtime is
microsoft/BitNet — bitnet.cpp — a llama.cpp fork whose ternary kernels are CPU
kernels. That is not a downgrade; 1-bit inference on CPU is what the model was
designed for. TRAINING the LoRA is the **GPU** job, on the bf16 master weights,
and that is `scripts/train_online.py`. One laptop does both, each half on the
part of the machine that suits it. See SELFHOST.md.

USAGE
─────
    python serving/run_local.py                      # ./bitnet-local, port 9000
    python serving/run_local.py --dir D:/bitnet --port 9000 --threads 8
    python serving/run_local.py --engine ~/BitNet/build/bin/llama-server
    python serving/run_local.py --check              # preflight only, changes nothing
    python serving/run_local.py --print-env          # the Studio .env lines, then exit

Ctrl-C stops the engine and the gateway (supervisor's SIGINT handler); both
children inherit this terminal, so their logs stream here interleaved and
prefixed — `[supervisor]`, `[gateway]`, and llama-server's own output.

EVERY SETTING (flag, else env, else default)
────────────────────────────────────────────
  --dir          STUDIO_DATA_DIR         ./bitnet-local
  --models-dir   STUDIO_MODELS_DIR       <dir>/models
  --adapters-dir STUDIO_ADAPTERS_DIR     <dir>/adapters
  --model-file   STUDIO_BITNET_GGUF      ggml-model-i2_s.gguf
  --adapter-file STUDIO_TOOLCALL_GGUF    tool_call.gguf
  --engine       STUDIO_ENGINE_BIN       discovered (see find_engine)
  --host         STUDIO_GATEWAY_HOST     127.0.0.1
  --port         STUDIO_SERVING_PORT     9000
  --engine-port  STUDIO_ENGINE_PORT      an unused port
  --threads      LLAMA_THREADS           half the logical CPUs (see default_threads)
  --ctx          LLAMA_CTX_SIZE          4096  (the model's maximum)
  --api-key      STUDIO_GATEWAY_API_KEY  unset
  --no-download  —                       fail instead of fetching a missing model

Stdlib only, like everything else in this directory.
"""
import argparse
import os
import shutil
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:                 # so `import supervisor` finds its sibling
    sys.path.insert(0, HERE)

import supervisor                        # noqa: E402  (path must be set first)

DEFAULT_DIR = "./bitnet-local"
DEFAULT_PORT = 9000
DEFAULT_GGUF = "ggml-model-i2_s.gguf"
DEFAULT_ADAPTER = "tool_call.gguf"
BITNET_REPO = "https://github.com/microsoft/BitNet"


def out(*a):
    print(*a, flush=True)


def err(*a):
    print(*a, file=sys.stderr, flush=True)


# ── Threads ──────────────────────────────────────────────────────────────

def default_threads(logical=None):
    """A laptop-sane thread count.

    llama.cpp decode is memory-bandwidth bound, so SMT/hyperthread siblings add
    contention rather than throughput, and on a laptop you also want a core left
    for the browser and for Studio itself. Half the logical CPUs approximates
    "the physical cores" on the SMT x86 parts this is aimed at. On a 4-thread or
    smaller machine, take them all — half of 4 is not enough to be worth it.
    `--threads` / LLAMA_THREADS override this; measure with llama-bench before
    believing any heuristic, including this one.
    """
    n = logical or os.cpu_count() or 4
    return max(1, n // 2) if n > 4 else max(1, n)


# ── Finding the engine ───────────────────────────────────────────────────

def _exe(path):
    """The same path, with .exe appended on Windows."""
    return path + ".exe" if os.name == "nt" else path


def engine_candidates(environ, root):
    """Where a bitnet.cpp `llama-server` plausibly is, most explicit first.

    Yields (path, how_we_found_it). `build/bin` is where bitnet.cpp's own
    setup_env.py leaves it on Unix; MSVC puts it in `build/bin/Release`.
    """
    explicit = (environ.get("STUDIO_ENGINE_BIN") or "").strip()
    if explicit:
        yield explicit, "STUDIO_ENGINE_BIN"
    roots = []
    for var in ("BITNET_HOME", "BITNET_ROOT"):
        if (environ.get(var) or "").strip():
            roots.append((environ[var].strip(), var))
    home = os.path.expanduser("~")
    for base in (os.getcwd(), root, home, os.path.join(home, "src")):
        for name in ("BitNet", "bitnet"):
            roots.append((os.path.join(base, name), "a nearby bitnet.cpp checkout"))
    for base, how in roots:
        for sub in (os.path.join("build", "bin"),
                    os.path.join("build", "bin", "Release")):
            yield os.path.join(base, sub, _exe("llama-server")), how
    found = shutil.which("llama-server")
    if found:
        yield found, "PATH"


def find_engine(explicit, environ, root):
    """(path, how) of a usable llama-server, or (None, None).

    Usable means: it exists, it is a file, and this user may execute it. It does
    NOT mean it is a bitnet.cpp build — nothing short of running it can prove
    that, so `looks_like_bitnet` warns instead of refusing.
    """
    env = dict(environ)
    if explicit:
        env["STUDIO_ENGINE_BIN"] = explicit
    seen = set()
    for path, how in engine_candidates(env, root):
        # An --engine flag is threaded through the same candidate list (so the
        # existence and executability checks are identical), but it must be
        # REPORTED as the flag: the banner saying "[STUDIO_ENGINE_BIN]" for a
        # path the operator typed on the command line sends them looking for an
        # environment variable they never set.
        if explicit and how == "STUDIO_ENGINE_BIN":
            how = "--engine"
        full = os.path.abspath(os.path.expanduser(path))
        if full in seen:
            continue
        seen.add(full)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return full, how
    return None, None


def looks_like_bitnet(path):
    """Heuristic, and only a heuristic: a binary built from microsoft/BitNet
    almost always sits under a directory with 'bitnet' in its name, because that
    is what the clone is called. A `llama-server` off PATH (Homebrew, apt, a
    release tarball) is stock llama.cpp, which CANNOT load the i2_s GGUF —
    it dies with `tensor 'blk.0.ffn_down.weight' of type 36 … not a multiple of
    block size (0)` (ggml-org/llama.cpp#12997). Worth a warning, not a refusal:
    you may well have named your build directory something else."""
    return "bitnet" in os.path.abspath(path).lower()


def engine_missing_message(root, environ):
    """What to actually DO when there is no engine. This is the single most
    likely first-run failure, so it gets a real answer rather than a traceback."""
    looked = []
    seen = set()
    for path, _how in engine_candidates(environ, root):
        full = os.path.abspath(os.path.expanduser(path))
        if full not in seen:
            seen.add(full)
            looked.append(full)
    return f"""
run_local.py: no llama-server found — there is no engine to serve BitNet with.

  This must be a build of bitnet.cpp ({BITNET_REPO}), NOT stock llama.cpp.
  microsoft/bitnet-b1.58-2B-4T-gguf ships `{DEFAULT_GGUF}`, and I2_S is a
  quantisation type that exists only in Microsoft's fork; stock llama.cpp
  rejects the file outright (ggml-org/llama.cpp#12997), and the model card is
  explicit: "you MUST use the dedicated C++ implementation: bitnet.cpp".

  BUILD IT (on Windows do this inside WSL2 — see serving/SELFHOST.md §2):

      git clone --recursive {BITNET_REPO}.git
      cd BitNet
      cmake -B build -DCMAKE_BUILD_TYPE=Release -DBITNET_X86_TL2=OFF \\
            -DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_COMMON=ON -DLLAMA_BUILD_SERVER=ON
      cmake --build build --config Release -j
      ./build/bin/llama-server --help      # smoke test: must print usage

  THEN point this at it:

      python serving/run_local.py --engine /path/to/BitNet/build/bin/llama-server
      # or: export STUDIO_ENGINE_BIN=/path/to/BitNet/build/bin/llama-server

  Looked in:
""" + "".join(f"    {p}\n" for p in looked)


# ── Ports ────────────────────────────────────────────────────────────────

class PortBusy(Exception):
    pass


def reserve_port(host, port):
    """Return the port we will really listen on, having proved we can bind it.

    Two jobs. (1) `--port 0` means "pick one", and the printed
    STUDIO_LLM_BASE_URL has to name the port that is actually bound, not the
    zero that was asked for. (2) A busy port must fail HERE with a sentence,
    rather than 20 seconds later as a gateway traceback under a supervisor that
    then reports the whole unit dead.

    The socket is closed before the real listener opens it, so there is a
    microscopic race with another process on this machine. That is worth it for
    an exact URL; the failure mode is a clear bind error a second later.
    """
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            raise PortBusy(
                f"cannot listen on {host}:{port} — {e}.\n"
                f"  Something else is already using it (another run_local.py? a dev "
                f"server?).\n  Pick another with --port, or stop the other process.")
        return s.getsockname()[1]


# ── Configuration ────────────────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="run_local.py",
        description="Run the BitNet serving unit (engine + gateway) on this machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Serving BitNet is a CPU job; the GPU trains the adapter. "
               "See serving/SELFHOST.md.")
    p.add_argument("--dir", help=f"working directory for model+adapters+state "
                                 f"(default {DEFAULT_DIR}, or $STUDIO_DATA_DIR)")
    p.add_argument("--models-dir", help="override <dir>/models")
    p.add_argument("--adapters-dir", help="override <dir>/adapters")
    p.add_argument("--model-file", help=f"GGUF filename (default {DEFAULT_GGUF})")
    p.add_argument("--adapter-file", help=f"adapter filename (default {DEFAULT_ADAPTER})")
    p.add_argument("--engine", help="path to bitnet.cpp's llama-server")
    p.add_argument("--host", help="bind address for the gateway (default 127.0.0.1)")
    p.add_argument("--port", type=int,
                   help=f"gateway port (default {DEFAULT_PORT}); 0 picks a free one")
    p.add_argument("--engine-port", type=int,
                   help="llama-server's loopback port (default: a free one)")
    p.add_argument("--threads", type=int, help="engine threads")
    p.add_argument("--ctx", type=int, help="context window (default 4096, the max)")
    p.add_argument("--api-key", help="require Authorization: Bearer <key> on the "
                                     "gateway's /v1 and /admin (set STUDIO_LLM_API_KEY "
                                     "to the same value on Studio). Omitting it with a "
                                     "non-loopback --host is warned about loudly")
    p.add_argument("--no-download", action="store_true",
                   help="fail if the model is absent instead of fetching ~1.1 GB")
    p.add_argument("--check", action="store_true",
                   help="preflight only: report engine/model/port; start no "
                        "processes and create no directories")
    p.add_argument("--print-env", action="store_true",
                   help="print the Studio-side variables and exit")
    return p.parse_args(argv)


def _int(value, default, what):
    """int(), but a junk environment value is a warning and a default rather
    than a ValueError traceback out of argument resolution."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        err(f"run_local.py: WARNING — {what}={value!r} is not a number; using {default}.")
        return default


def _pick(flag, environ, name, default):
    if flag is not None and flag != "":
        return flag
    v = (environ.get(name) or "").strip()
    return v if v else default


def resolve(args, environ=None):
    """Flags → env → defaults, into the paths the supervisor will use.

    Kept pure (no filesystem writes, no sockets) so it is directly testable and
    so `--print-env` can answer without touching anything.
    """
    environ = os.environ if environ is None else environ
    root = os.path.abspath(os.path.expanduser(
        _pick(args.dir, environ, "STUDIO_DATA_DIR", DEFAULT_DIR)))
    models = os.path.abspath(os.path.expanduser(_pick(
        args.models_dir, environ, "STUDIO_MODELS_DIR", os.path.join(root, "models"))))
    adapters = os.path.abspath(os.path.expanduser(_pick(
        args.adapters_dir, environ, "STUDIO_ADAPTERS_DIR",
        os.path.join(root, "adapters"))))
    gguf = _pick(args.model_file, environ, "STUDIO_BITNET_GGUF", DEFAULT_GGUF)
    adapter = _pick(args.adapter_file, environ, "STUDIO_TOOLCALL_GGUF", DEFAULT_ADAPTER)
    host = _pick(args.host, environ, "STUDIO_GATEWAY_HOST", "127.0.0.1")
    # PORT is read too, but only after STUDIO_SERVING_PORT: a laptop shell often
    # has PORT set for something else entirely, and silently serving BitNet on
    # someone's React port would be a puzzling half hour.
    port = args.port if args.port is not None else _int(
        _pick(None, environ, "STUDIO_SERVING_PORT",
              _pick(None, environ, "PORT", str(DEFAULT_PORT))),
        DEFAULT_PORT, "STUDIO_SERVING_PORT/PORT")
    engine_port = args.engine_port if args.engine_port is not None else _int(
        _pick(None, environ, "STUDIO_ENGINE_PORT", "0"), 0, "STUDIO_ENGINE_PORT")
    threads = args.threads or _int(
        _pick(None, environ, "LLAMA_THREADS", "0"), 0, "LLAMA_THREADS") \
        or default_threads()
    ctx = args.ctx or _int(
        _pick(None, environ, "LLAMA_CTX_SIZE", "4096"), 4096, "LLAMA_CTX_SIZE")
    api_key = _pick(args.api_key, environ, "STUDIO_GATEWAY_API_KEY", "")
    return {
        "root": root,
        "models_dir": models,
        "adapters_dir": adapters,
        "gguf": gguf,
        "adapter_file": adapter,
        "model_path": os.path.join(models, gguf),
        "adapter_path": os.path.join(adapters, adapter),
        "state_path": os.path.join(root, "state.json"),
        "host": host,
        "port": port,
        "engine_port": engine_port,
        "threads": threads,
        "ctx": ctx,
        "api_key": api_key,
    }


def display_host(host):
    """The host a human should paste. 0.0.0.0 / :: are bind wildcards, not
    addresses you can connect to."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "*", "") else host


def base_url(cfg):
    return f"http://{display_host(cfg['host'])}:{cfg['port']}/v1"


def studio_env_lines(cfg):
    """The lines the owner pastes into Studio's backend .env. Exactly these
    three, and no others, are what makes Studio use this box:
      backend/app/agent.py::make_llm  reads STUDIO_LLM_BASE_URL + STUDIO_LLM_API_KEY
      backend/app/router.py::bitnet_ready  gates on STUDIO_LLM_BASE_URL being set
    """
    lines = [f"STUDIO_LLM_BASE_URL={base_url(cfg)}",
             "STUDIO_BITNET_LLM=openai:bitnet"]
    if cfg["api_key"]:
        lines.append(f"STUDIO_LLM_API_KEY={cfg['api_key']}")
    return lines


# ── Wiring the supervisor for a laptop ───────────────────────────────────

def supervisor_env(cfg, engine_bin):
    """The environment supervisor.load_config() reads. Everything here has a
    container counterpart in Dockerfile.railway's ENV block; this is the same
    contract with laptop values.
    """
    return {
        "STUDIO_DATA_DIR": cfg["root"],
        "STUDIO_MODELS_DIR": cfg["models_dir"],
        "STUDIO_ADAPTERS_DIR": cfg["adapters_dir"],
        "STUDIO_BITNET_GGUF": cfg["gguf"],
        "STUDIO_TOOLCALL_GGUF": cfg["adapter_file"],
        "STUDIO_ENGINE_BIN": engine_bin,
        "STUDIO_ENGINE_PORT": str(cfg["engine_port"]),
        "PORT": str(cfg["port"]),
        # No bridge: the dual-stack splice exists for Railway's IPv6-only
        # private network (RAILWAY.md §6). On a laptop it would be a second hop
        # for nothing, so the gateway takes the public port itself.
        "STUDIO_SUPERVISOR_BRIDGE": "0",
        "STUDIO_GATEWAY_HOST": cfg["host"],
        "STUDIO_BACKEND_KIND": "llama",
        "STUDIO_BASE_MODEL_NAME": "bitnet",
        # The CPU/llama path serves the ONE global tool_call adapter (README §7);
        # a per-user style block is logged and ignored, never an error.
        "STUDIO_GATEWAY_ADAPTER_PRIORITY": "tool_call",
        "LLAMA_THREADS": str(cfg["threads"]),
        "LLAMA_CTX_SIZE": str(cfg["ctx"]),
        "STUDIO_GATEWAY_API_KEY": cfg["api_key"],
        # Adapter arrivals are a manual copy here, not a trainer push over a
        # network, so watch a little more eagerly than the container's 15 s.
        "STUDIO_ADAPTER_POLL_SECONDS": "5",
        # Only changes which fix the supervisor's failure messages suggest.
        "STUDIO_SUPERVISOR_MODE": "local",
    }


def banner(cfg, engine_bin, engine_how, model_present):
    bar = "─" * 68
    adapter = (cfg["adapter_path"] if os.path.exists(cfg["adapter_path"])
               else "none yet — serving the BASE model (this is normal on day one)")
    lines = [
        bar,
        "  Studio BitNet serving unit — local (CPU inference; your GPU is for training)",
        bar,
        f"  engine    {engine_bin}   [{engine_how}]",
        f"  model     {cfg['model_path']}"
        f"{'' if model_present else '   (will be downloaded, ~1.1 GB, once)'}",
        f"  adapter   {adapter}",
        f"  threads   {cfg['threads']}     ctx {cfg['ctx']}",
        f"  listening http://{display_host(cfg['host'])}:{cfg['port']}"
        f"  (engine on 127.0.0.1:{cfg['engine_port']}, loopback only)",
        "",
        "  Put these in Studio's backend .env, then restart Studio:",
        "",
    ]
    lines += [f"      {line}" for line in studio_env_lines(cfg)]
    lines += [
        "",
        f"  Check it:  curl {display_host(cfg['host'])}:{cfg['port']}/health",
        "  Ctrl-C stops the engine and the gateway.",
        bar,
    ]
    return "\n".join(lines)


def main(argv=None):
    args = parse_args(argv)
    cfg = resolve(args)

    if args.print_env:
        for line in studio_env_lines(cfg):
            out(line)
        return 0

    # 1. The engine. Nothing else matters if this is missing, so check it first
    #    and answer with instructions rather than a FileNotFoundError from
    #    Popen 30 seconds into a model download.
    engine_bin, engine_how = find_engine(args.engine, os.environ, cfg["root"])
    if not engine_bin:
        if args.engine:
            err(f"run_local.py: --engine {args.engine} is not an executable file.")
        err(engine_missing_message(cfg["root"], os.environ))
        return 2
    if not looks_like_bitnet(engine_bin):
        err(f"run_local.py: WARNING — {engine_bin} was found on {engine_how} and does "
            f"not look like a bitnet.cpp build.\n"
            f"  A STOCK llama.cpp llama-server cannot load {cfg['gguf']}: it fails "
            f"with\n"
            f"  \"tensor 'blk.0.ffn_down.weight' of type 36 … not a multiple of block "
            f"size (0)\".\n"
            f"  If the engine dies at start-up, that is why — build "
            f"{BITNET_REPO} and pass --engine.\n")

    # 2. Ports, before anything slow, so the URL we print is the URL that binds.
    if ":" in cfg["host"]:
        # gateway.py is http.server, which is AF_INET-only. On Railway the
        # supervisor's dual-stack bridge covers that; here there is no bridge,
        # so an IPv6 bind address would fail inside the gateway with a much
        # less obvious message than this one.
        err(f"run_local.py: --host {cfg['host']} is an IPv6 address, and "
            f"gateway.py's http.server is IPv4-only.\n"
            f"  Use 127.0.0.1 (default) or 0.0.0.0.")
        return 2
    try:
        cfg["port"] = reserve_port(cfg["host"], cfg["port"])
        cfg["engine_port"] = reserve_port("127.0.0.1", cfg["engine_port"])
    except PortBusy as e:
        err(f"run_local.py: {e}")
        return 2
    if cfg["host"] not in ("127.0.0.1", "localhost", "::1") and not cfg["api_key"]:
        err(f"run_local.py: WARNING — binding {cfg['host']}, which is NOT loopback, "
            f"with no --api-key.\n"
            f"  Anything that can reach this machine can then use the model and the "
            f"/admin endpoints.\n"
            f"  Set --api-key (and STUDIO_LLM_API_KEY to the same value on Studio) "
            f"before exposing it — SELFHOST.md §5(b).\n")

    # 3. Hand the environment to the supervisor and let it re-read its config.
    #    From here on this is the container's code path. No directory is created
    #    yet: --check must be able to answer without leaving anything behind.
    os.environ.update(supervisor_env(cfg, engine_bin))
    supervisor.load_config()
    # gateway.py resolves the state file independently (STUDIO_STATE_PATH, else
    # $STUDIO_DATA_DIR/state.json). Pin it to the supervisor's own answer so the
    # two can never disagree about which file /health is reading.
    os.environ["STUDIO_STATE_PATH"] = supervisor.STATE_PATH

    model_present = (os.path.exists(cfg["model_path"])
                     and os.path.getsize(cfg["model_path"]) > 1024)
    if not model_present and args.no_download:
        err(f"run_local.py: no model at {cfg['model_path']} and --no-download was "
            f"given.\n"
            f"  Get it from huggingface.co/{supervisor.GGUF_REPO} "
            f"(the i2_s file, ~1.1 GB) and put it there, or drop --no-download.")
        return 2

    if args.check:
        out(banner(cfg, engine_bin, engine_how, model_present))
        model_state = "present" if model_present else "MISSING (would be downloaded)"
        out(f"  preflight OK — engine executable, ports free, model {model_state}.")
        return 0

    # Only a real run creates anything on disk.
    os.makedirs(cfg["models_dir"], exist_ok=True)
    os.makedirs(cfg["adapters_dir"], exist_ok=True)
    out(banner(cfg, engine_bin, engine_how, model_present))
    # supervisor.main() installs the SIGINT/SIGTERM handler that stops both
    # children, streams their output to this terminal (they inherit it), and
    # blocks in the adapter-watch loop until interrupted.
    return supervisor.main()


if __name__ == "__main__":
    sys.exit(main())
