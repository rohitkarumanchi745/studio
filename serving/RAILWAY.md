# Deploying the BitNet serving unit on Railway

**Nothing here has been deployed.** These are the artefacts plus the brief; the
deploy is your call, and §9 is the number it costs.

| | |
|---|---|
| **Shape** | **ONE** Railway service. `llama-server` + `gateway.py` in one container under `supervisor.py`. §1 |
| **Engine** | **bitnet.cpp built from source**, not the stock `llama.cpp` image the compose file uses. Stock llama.cpp **cannot load** this GGUF. §2 |
| **Model file** | **Pulled on first boot onto the Railway volume**, not baked into the image. §3 |
| **Adapter** | **Optional at boot.** Serves the base model with no adapter; mounts one when it appears. §4 |
| **Files** | `Dockerfile.railway`, `railway.json`, `supervisor.py`, existing `gateway.py` |
| **Cost** | **~$23 / month** light use, ~$39 heavy — and a **~$19 floor at zero traffic** (RAM is billed while the model is resident). §9 |
| **Speed** | Est. **5–15 tok/s** decode on 2–4 vCPU, **5–30 s to first token** on a cold prompt. Fine for background work; marginal for interactive chat. §9 |

---

## What is verified, and what is not

Everything below the gateway has been exercised by tests on a developer machine.
**Nothing has ever run in a container.** Be precise about which is which before
you trust a green tick.

| Link | State | Where |
|---|---|---|
| Studio attaches `studio_adapters` in the shape the gateway parses | **verified** | `backend/tests/test_bitnet_path.py` |
| `bitnet_ready()` two-gate logic, routing, escalation on failure | **verified** | `backend/tests/test_bitnet_path.py` |
| Routing refuses a pattern the requester cannot read (incl. qualified names) | **verified** | `backend/tests/test_router_access.py` |
| Gateway `/health` reports each stage honestly; 200 only when servable | **verified** (real gateway process, stub engine) | `backend/tests/test_serving_readiness.py` |
| Gateway refuses to claim an adapter it cannot prove is mounted | **verified** (same) | `backend/tests/test_serving_readiness.py` |
| Gateway strips `studio_adapters` and proxies the OpenAI call | **verified** (stub engine) | `backend/tests/test_serving_readiness.py` |
| Bootstrap corpus is executable and RBAC-respecting | **verified** | `backend/tests/test_bootstrap_rollouts.py` |
| Trainer polls → formats → publishes | **verified** (dry-run + a real round on a stand-in base) | rehearsal |
| **bitnet.cpp compiles in the image** | **UNVERIFIED** | needs the box |
| **The i2_s GGUF loads and generates** | **UNVERIFIED** | needs the box |
| **A PEFT adapter converts to GGUF for this architecture** | **UNVERIFIED** | needs the box |
| **`--lora` applies at all on an i2_s base** | **UNVERIFIED** | needs the box |
| **Every RAM / tokens-per-second / cost figure here** | **ESTIMATE, not measurement** | §9 |

### Smoke procedure — check the unverified links, in order

Each step is a command and the observation that means it passed. Stop at the
first failure; later steps depend on earlier ones.

```bash
# 1. BUILD — the single most likely thing to fail (a from-source C++ compile).
#    PASS: the Railway build log ends with a pushed image, no compiler error.

# 2. MODEL — first boot downloads ~1.1 GB onto the volume.
curl -s https://<serving>/health
#    PASS: {"ok":false,"stage":"downloading_model"} then, minutes later,
#          {"ok":true,"stage":"ready","mounted_adapter":null}
#    A 200 while the volume is still empty would be the readiness bug returning.

# 3. ENGINE — a base-model completion, no adapter involved.
curl -s -X POST https://<serving>/v1/chat/completions \
  -H "Authorization: Bearer $STUDIO_GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"bitnet","messages":[{"role":"user","content":"say ready"}],"max_tokens":8}'
#    PASS: a choices[0].message.content comes back. This is the first proof a
#          1-bit model has generated a token on this hardware.

# 4. ADAPTER CONVERSION — on the trainer box, using bitnet.cpp's VENDORED script
#    (upstream llama.cpp does not know this architecture).
python 3rdparty/llama.cpp/convert_lora_to_gguf.py <peft-dir> --outfile tool_call.gguf
#    PASS: a .gguf is written without an "unknown architecture" error.

# 5. ADAPTER MOUNTS — put the file and its provenance on the volume together.
#    The .uri sidecar is what lets the gateway PROVE the mounted file is the one
#    Studio asked for; without it the adapter is anonymous and never claimed.
railway ssh -- sh -c 'cat > /data/adapters/tool_call.gguf' < tool_call.gguf
railway ssh -- sh -c 'echo "<the uri you published>" > /data/adapters/tool_call.gguf.uri'
#    PASS: within ~15s the supervisor restarts the engine, then
curl -s https://<serving>/health
#    shows {"stage":"ready","mounted_adapter":{"uri":"<the uri you published>"}}

# 6. GATEWAY CLAIMS IT — send the block Studio sends, with that same uri.
#    PASS: the log line says "verified"; /health shows no adapter_mismatch.
#    If it says REFUSING, the uri in the sidecar does not match what you
#    published — that is the check doing its job, not a bug.

# 7. STUDIO SEES IT — only now set STUDIO_LLM_BASE_URL on the app.
curl -s https://<studio>/health
#    PASS: the auth/llm block reports the endpoint, and a learned prompt comes
#          back with served_by=bitnet.
```

## 0. What gets deployed

```
                    Railway service "bitnet-serving"          ── ONE container ──
  Studio ──────▶ [::]:$PORT  bridge  ─▶ 127.0.0.1:9001  gateway.py ─▶ 127.0.0.1:8080
  STUDIO_LLM_       (supervisor.py, PID 1)                 (unchanged)    llama-server
  BASE_URL                     │                                          (bitnet.cpp)
                               │                                                │
                               └── watches ────────────────────────────────┐    │
                                                                           │    │
                    Railway volume  /data ─── models/ggml-model-i2_s.gguf ──│────┘
                    (5 GB)                └── adapters/tool_call.gguf ──────┘
```

`gateway.py` is used **exactly as it is today** — same env contract, same
`studio_adapters` handling, same fail-safe. Nothing under `backend/` changes.

---

## 1. Why ONE service, not two

Two services (engine + gateway over `*.railway.internal`) is the natural
translation of `docker-compose.yml`. It does not work here, for three reasons —
the first is decisive:

1. **A Railway volume belongs to one service.** The docs are explicit: *"Each
   service can only have a single volume"*, and volumes are attached per service.
   The compose design has the engine and the trainer/adapter drop share
   `/adapters` through one bind-mount. Split across two services, the adapter file
   simply cannot be in both places.
2. **The adapter needs a process restart, and only a co-located parent can do
   it.** `llama-server` cannot hot-load an adapter *file* — only re-scale one
   mounted at startup (README §3, §6). So "a new adapter arrived" means "restart
   the engine". A sibling Railway service cannot restart another service without
   a Railway API token and a deploy call; the supervisor in the same container
   does it in one `terminate()`/`Popen()`.
3. **One public port, no engine exposure.** `llama-server` and `gateway.py` both
   bind `127.0.0.1`; only the bridge listens outward. A two-service split has to
   expose the raw engine on the private network — an unauthenticated
   `/v1/chat/completions` and an unauthenticated `POST /lora-adapters` — to
   anything else in the project.

The cost of one service is that the gateway restarts with the engine on a
redeploy, and a gateway crash takes the engine with it. Both are handled
(§4) and neither is worth the three problems above.

---

## 2. The engine: bitnet.cpp, not stock llama.cpp

**This corrects an assumption in `docker-compose.yml` and README §1.** The compose
`cpu` profile runs `ghcr.io/ggml-org/llama.cpp:server` against
`ggml-model-i2_s.gguf`. That combination does not work:

- `I2_S` is a quantisation type from **Microsoft's llama.cpp fork**, not upstream.
  Upstream's loader rejects the file:
  `tensor 'blk.0.ffn_down.weight' of type 36 (TYPE_IQ4_NL_4_4 REMOVED, use IQ4_NL
  with runtime repacking) has 6912 elements per row, not a multiple of block size (0)`
  — [ggml-org/llama.cpp#12997](https://github.com/ggml-org/llama.cpp/issues/12997),
  open and stale.
- The model card is unambiguous: *"For achieving the efficiency benefits
  demonstrated in the technical paper, you MUST use the dedicated C++
  implementation: bitnet.cpp"*, and links only there —
  [microsoft/bitnet-b1.58-2B-4T-gguf](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T-gguf).
- Upstream `llama-arch.cpp` carries one `LLM_ARCH_BITNET` entry (the older
  `bitnet_b1_58-3B`), not the `BitNetForCausalLM` architecture of 2B-4T.

So `Dockerfile.railway` **builds [microsoft/BitNet](https://github.com/microsoft/BitNet)
from source**, pinned to commit `0b341e58`, with the exact cmake flags its own
`setup_env.py` uses for `-q i2_s` on x86_64:

```
-DBITNET_X86_TL2=OFF -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18
-DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_EXAMPLES=ON -DLLAMA_BUILD_COMMON=ON -DLLAMA_BUILD_SERVER=ON
```

**The gateway contract survives this substitution.** bitnet.cpp vendors llama.cpp
at a 2025-era commit that has `tools/server`, whose documented surface includes
everything `gateway.py` and `supervisor.py` use — verified flag-by-flag against
that pinned commit's `tools/server/README.md`:

| Needed by | Flag / endpoint | Present |
|---|---|---|
| supervisor | `-m`, `--alias`, `--host`, `--port`, `--ctx-size`, `--threads`, `--threads-batch`, `--parallel`, `--cont-batching`, `--no-webui` | yes |
| supervisor (adapter) | `--lora FNAME`, `--lora-init-without-apply` | yes |
| gateway `_ensure_loaded_llama` | `POST /lora-adapters` (scale only) | yes |
| gateway proxy | `POST /v1/chat/completions` | yes |
| readiness | `GET /health` → `{"status":"ok"}`, or 503 `"Loading model"` while loading | yes |

Also built into the image and worth using once: `/opt/bitnet/bin/llama-bench`
(§9, "measure it yourself").

The build is a from-source C++ compile: **expect 10–20 minutes on a first
Railway build**, then it is layer-cached until `BITNET_REF` changes.

---

## 3. The model file: volume, not image

`ggml-model-i2_s.gguf` is **1,187,801,280 bytes** (verified by HTTP HEAD against
the repo). It cannot live in git, so it either bakes into the image or lands on
the volume at boot.

| | Bake at build | **Pull on first boot (chosen)** |
|---|---|---|
| Image size | +1.1 GB, pushed and pulled on **every** deploy | ~400 MB, engine only |
| Build time | +2–5 min every build | unchanged |
| First boot | seconds | **1–3 min download, once per volume** |
| Later boots | seconds | seconds (file is already there) |
| Redeploy of a one-line gateway fix | re-ships 1.1 GB | re-ships nothing new |
| Volume needed? | **still yes** — for adapters | yes |
| Gated repo | fails the **build**, blocking deploys | fails at boot, model swappable by env |

The deciding argument: **the volume is not optional anyway.** Adapters have to
persist somewhere, and Railway gives the service exactly one volume. Once you are
paying for it, putting a 1.1 GB read-only blob on it costs $0.18/month and buys
back a fast, small image you can redeploy freely.

Boot-time behaviour, in `supervisor.py::ensure_model`:

- Present on the volume → used immediately, no network.
- Absent → one `curl -L` from
  `https://huggingface.co/$STUDIO_BITNET_GGUF_REPO/resolve/main/$STUDIO_BITNET_GGUF`,
  written to `<name>.part` and **atomically renamed** only after it verifies the
  size and the `GGUF` magic bytes. A truncated or error-page download can never
  masquerade as a model.
- **Gated / rate-limited** → no stack trace. HTTP 401/403 and 429 each print a
  message naming **`HUGGING_FACE_HUB_TOKEN`**, saying whether it is currently set,
  and giving the exact fix. 401/403/404 fail fast (retrying a permission problem
  is pointless); network errors retry with backoff.
- Total failure → the supervisor exits non-zero so Railway shows a **failed
  deploy** rather than a permanently empty green box.

To bake it instead, add to the runtime stage of `Dockerfile.railway`:

```dockerfile
RUN mkdir -p /opt/models && curl -fL --retry 3 \
      -o /opt/models/ggml-model-i2_s.gguf \
      https://huggingface.co/microsoft/bitnet-b1.58-2B-4T-gguf/resolve/main/ggml-model-i2_s.gguf
```
and set `STUDIO_MODELS_DIR=/opt/models`. (A gated repo then needs the token as a
build arg, and it is in the image layer forever — one reason not to.)

---

## 4. The adapter: day one must not crash-loop

The compose `cpu` command hard-codes `--lora /adapters/tool_call.gguf`. `--lora`
is a **startup** flag and `llama-server` will not start if the file is missing.
A new deployment has no adapter — `scripts/train_online.py` builds it from
reward-labelled rollouts that do not exist yet. **As written, the compose unit
crash-loops on its first boot, forever.**

`supervisor.py` fixes this by making the flag conditional:

- **No adapter file** → `--lora` is omitted entirely. The engine starts and serves
  the **base model**. `gateway.py` still tries `POST /lora-adapters`, llama-server
  refuses (nothing is mounted), the gateway catches it and falls back to the base
  model — its existing, documented fail-safe. Nothing errors.
- **Adapter file present at boot** → `--lora <path> --lora-init-without-apply`,
  exactly as compose does, so the gateway's `[{"id":0,"scale":1.0}]` is what
  switches it on.
- **Adapter appears later** → a 15 s poll notices it, waits for the file to stop
  changing (a half-written GGUF is not an adapter — mounting one *is* the
  crash-loop we are avoiding), then **restarts only `llama-server`** with the
  flag. The public port and the gateway never go down; the engine is missing for
  the few seconds of reload, and calls in that window fall back and escalate.

This is the only way to do it on this engine. There is no runtime file-load API —
`POST /lora-adapters` sets *scales* on already-mounted adapters and nothing else.

Day-one state is worth being clear about: with no adapter published,
`router.bitnet_ready()` is `False`, so **Studio sends this box no traffic at all**.
It sits there serving the base model to nobody until the first `tool_call` adapter
is published. That is correct, not a bug — but it means the serving box is billed
before it is useful. See §9.

---

## 5. Deploy steps

Do these in order. Steps 1–4 create the serving service; step 7 is the switch
that actually turns BitNet routing on in Studio.

1. **New service** in the same Railway project/environment as Studio, from this
   same GitHub repo.
2. **Settings → Root Directory = `serving`.** Non-optional. Railway reads
   `<root>/railway.json` and builds with `<root>` as the Docker context — leave
   it at `/` and Railway will find the repo-root `railway.json` and build the
   *Studio app* again. With `serving` set, `serving/railway.json` selects
   `Dockerfile.railway` and `COPY gateway.py supervisor.py` resolves.
3. **Settings → Volume → add a volume, mount path `/data`, 5 GB.** (Hobby caps
   volumes at 5 GB; Pro at 50 GB. 1.2 GB model + adapters fits 5 GB fine.)
   Keep replicas at **1** — one volume cannot back two active deployments.
4. **Variables** — set the serving-side block in §6. At minimum `PORT=9000`.
5. **Deploy.** Expect a 10–20 min first build (§2), then the healthcheck to pass
   within seconds of the container starting, then the model download to finish in
   the logs a minute or two later. Look for:
   `[supervisor] model: OK — 1187801280 bytes → /data/models/ggml-model-i2_s.gguf`
   then `[supervisor] no adapter … serving the BASE model`.
6. **Verify** with §7 before touching Studio.
7. **Studio service → Variables**: add the Studio-side block in §6. This restarts
   Studio and is the moment routing changes. Note `bitnet_ready()` also requires a
   published `tool_call` adapter, so this alone changes nothing yet — it is safe
   to set early.

---

## 6. Environment variables — both sides

### Serving service (`bitnet-serving`)

| Variable | Set it to | Why |
|---|---|---|
| `PORT` | `9000` | **Set it explicitly.** It is the port the supervisor's public listener binds AND the port other services must name in the private URL. Leaving it implicit makes the internal URL guesswork. |
| `STUDIO_DATA_DIR` | `/data` | Must equal the volume mount path. |
| `STUDIO_GATEWAY_API_KEY` | a long random string | Turns on `Authorization: Bearer` for `/v1/*` and `/admin/*`. **Strongly recommended** — otherwise anyone who can reach the service gets free inference and a `POST /admin/load_adapter`. `/health` stays unauthenticated, so the Railway healthcheck still works. |
| `LLAMA_THREADS` | *(unset)* | Defaults to the container's **cgroup CPU quota**, not the host's core count — `os.cpu_count()` on a shared host reports 32–64 and would thrash a 2-vCPU slice. Override only to tune. |
| `LLAMA_CTX_SIZE` | *(unset → 4096)* | 4096 is the model's `max_position_embeddings`; you cannot go higher. Lower it to 2048 to save ~150 MB (§9). |
| `HUGGING_FACE_HUB_TOKEN` | a read token | Only needed if the pull is refused or rate-limited. The failure message tells you. |
| `STUDIO_ADAPTER_URL` | a URL | Optional. Where to fetch `tool_call.gguf` at boot (§8). |
| `STUDIO_BITNET_GGUF_URL` | a URL | Optional. Full override if you mirror the GGUF yourself. |
| `LLAMA_PARALLEL` | *(unset → 1)* | Slots. **N > 1 splits the 4096 context N ways**, it does not add capacity. |
| `RAILWAY_RUN_UID` | — | Do not set. The container runs as root to write the root-owned volume. |

### Studio service

| Variable | Set it to | Why |
|---|---|---|
| `STUDIO_LLM_BASE_URL` | `http://bitnet-serving.railway.internal:9000/v1` | Gate 1 of `router.bitnet_ready()`. **Point it at the GATEWAY (`/v1`), never at llama-server.** |
| `STUDIO_BITNET_LLM` | `openai:bitnet` | The spec the router asks for; `bitnet` must equal the serving side's `STUDIO_BASE_MODEL_NAME` (default `bitnet`, which is `--alias bitnet` on the engine). Default already matches — set it only to be explicit. |
| `STUDIO_LLM_API_KEY` | the same string as `STUDIO_GATEWAY_API_KEY` | Required **if** you set a gateway key. `agent.py::make_llm` sends this as the OpenAI `api_key` for the self-hosted spec only; with no gateway key it falls back to a `studio-local` placeholder (the OpenAI client insists on some credential). |

**Private hostname form**: `<service-name>.railway.internal`, and you must name
the port — `http://bitnet-serving.railway.internal:9000/v1`. Use the service's
name in Railway, lowercased, exactly. This keeps inference traffic off the public
internet and off your egress bill.

**One IPv6 caveat, already handled.** Railway environments created **after
2025-10-16** resolve `*.railway.internal` to both IPv4 and IPv6; **legacy
environments are IPv6-only**. `gateway.py` uses `http.server`, which is
`AF_INET`-only, so on a legacy environment nothing could reach it privately.
`supervisor.py` therefore fronts it with a **dual-stack `[::]` byte-splice
bridge** (raw TCP, no HTTP parsing — SSE streaming and keep-alive pass through
untouched; verified locally over both `::1` and `127.0.0.1`). Set
`STUDIO_SUPERVISOR_BRIDGE=0` to drop the bridge and let `gateway.py` bind
`$PORT` directly — only safe on a dual-stack environment or a public domain.

**If you use the public domain instead** (`https://<service>.up.railway.app/v1`):
it works, but set `STUDIO_GATEWAY_API_KEY` first — that URL is open to the world.

---

## 7. Verify it is live

**a. The serving box itself** (Railway → the service → its public domain, or from
another service on the private network):

```bash
# 1. Gateway up? Unauthenticated on purpose — this is the healthcheck path.
curl -s https://<serving-domain>/health
# {"ok":true,"backend":"http://127.0.0.1:8080/v1","kind":"llama",
#  "base_model":"bitnet","loaded_adapters":[],"priority":["tool_call"]}
#  loaded_adapters: [] is CORRECT before an adapter is published.

# 2. Engine up? Proxied to llama-server — 502 here means the model is still
#    loading or failed to load; check the deploy logs.
curl -s -H "Authorization: Bearer $STUDIO_GATEWAY_API_KEY" \
     https://<serving-domain>/v1/models

# 3. The real thing — a chat completion, exactly as Studio sends it.
curl -s https://<serving-domain>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $STUDIO_GATEWAY_API_KEY" \
  -d '{"model":"bitnet","max_tokens":40,
       "messages":[{"role":"user","content":"Reply with the single word: ready"}]}'
# → {"choices":[{"message":{"role":"assistant","content":"ready"...

# 4. Same call WITH the adapter block Studio attaches. Before any adapter is
#    published this must STILL answer — via the base-model fail-safe.
curl -s https://<serving-domain>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $STUDIO_GATEWAY_API_KEY" \
  -d '{"model":"bitnet","max_tokens":20,"messages":[{"role":"user","content":"hi"}],
       "studio_adapters":{"tool_call":{"uri":"/data/adapters/nope","version":1}}}'
```

**b. Studio sees it.** `GET /health` on the Studio service should stay green, and
`GET /api/training/online` (admin token) is the real state of the loop:

```bash
curl -s -H "Authorization: Bearer $ADMIN_JWT" https://<studio>/api/training/online
# {"tool_call_adapter": null, ...}  ← null means bitnet_ready() is still False.
```

`tool_call_adapter: null` with `STUDIO_LLM_BASE_URL` set is the expected state
until §8 completes. Studio keeps using the frontier for everything — that is the
router working, not a failure.

**c. End to end**, after an adapter is published: ask a question Studio has
already learned; the response's `served_by` should be `"bitnet"` and `routed`
should carry the matched pattern's `seen` / `avg_reward`.

---

## 8. Getting a trained adapter onto the box

This is the part with the most friction, and it is worth knowing before you
deploy. The volume cannot be shared with a trainer service (§1), so the adapter
has to arrive as bytes over HTTP.

1. Run `scripts/train_online.py` anywhere with the ML deps and an admin token
   (`STUDIO_API_URL=https://<studio> STUDIO_TRAINER_TOKEN=… python train_online.py --once`).
   It publishes to `POST /api/training/adapters`, which flips
   `router.bitnet_ready()` to `True`.
2. **Convert the PEFT adapter to GGUF.** Use **bitnet.cpp's vendored**
   `3rdparty/llama.cpp/convert_lora_to_gguf.py`, not upstream llama.cpp's —
   upstream does not know this architecture (§2), so upstream's converter is
   expected to fail on it. *(Expected, not verified — see §10.)*
3. Get the GGUF onto `/data/adapters/tool_call.gguf`, by either:
   - **`STUDIO_ADAPTER_URL`** — put the file somewhere fetchable (a private HF
     repo, S3, a GitHub release), set the variable, redeploy. The supervisor
     downloads it at boot with the same validation as the model.
   - **A shell into the running container** (`railway ssh`, on plans that offer
     it) and `curl` it straight into `/data/adapters/`. No redeploy needed — the
     watcher picks it up within 15 s and restarts the engine itself.
4. Optional: set `STUDIO_SERVE_URL=https://<serving-domain>` on the trainer so it
   calls `POST /admin/load_adapter` after publishing. On this CPU path that only
   re-scales an already-mounted adapter — **the file landing on the volume is
   what actually mounts it.** Harmless and idempotent either way.

Note the ordering trap: publishing the adapter to Studio (step 1) flips
`bitnet_ready()` immediately, but the serving box only mounts it at step 3. In
between, Studio routes learned prompts to a **base-model** BitNet, which will
mostly fail to emit usable SQL and escalate to the frontier. Costs a retry, never
a wrong answer — but do steps 1–3 close together.

---

## 9. What it costs, honestly

### Memory — ~1.8 GB steady, at ctx 4096

| Component | | |
|---|---:|---|
| Weights (`i2_s`, mmapped) | **1,133 MiB** | the file itself; RSS approaches this once warm |
| KV cache @ 4096 ctx, f16 | **300 MiB** | 30 layers × 5 KV heads × 128 head-dim × 2 (K+V) × 2 B = **75 KiB/token** × 4096 |
| Compute / batch buffers | ~250 MiB | scales with batch size |
| `gateway.py` + supervisor (Python) | ~40 MiB | |
| **Total** | **≈ 1.7–1.8 GiB** | peak a little higher during load |

**Provision 2 GB minimum, 3 GB comfortable.** Halving the context to 2048 saves
150 MiB and is a reasonable trade if prompts are short — but Studio's agent sends
a whole skill file, so don't.

*(The "0.4 GB" on the model card is non-embedding weights only. This model has a
128k vocab × 2560 hidden embedding table, which is why the real file is 1.19 GB.)*

### vCPU — 2 is a floor, 4 is the sweet spot

CPU inference is memory-bandwidth and thread bound. 4 vCPU is where a 2B ternary
model stops being painful; beyond ~8 the returns flatten. The supervisor reads
the cgroup quota so `--threads` matches what you actually bought.

### Volume — 5 GB

1.13 GB model + adapters (a LoRA GGUF at r=16 is tens of MB) + headroom.

### The Railway bill

Railway's published rates: **RAM $10/GB/month**, **vCPU $20/vCPU/month**,
**volume $0.15/GB/month**, billed on measured usage. Hobby is $5/month including
$5 of usage; per-service limits (48 GB / 48 vCPU on Hobby) are nowhere near
binding here.

| Line | Assumption | Monthly |
|---|---|---:|
| RAM | ~1.8 GB resident 24/7 | **~$18** |
| vCPU | 4 vCPU, busy ~5% of the time | **~$4** |
| vCPU | 4 vCPU, busy ~25% of the time | *(~$20)* |
| Volume | 5 GB | **$0.75** |
| **Total, light use** | | **~$23** |
| **Total, heavy use** | | **~$39** |
| **Floor, zero traffic** | model still resident | **~$19** |

**So: this does not fit "hobby-tier" in the sense of costing nothing.** The plan
limits are fine — the *usage* is roughly 4–6× the $5 Hobby credit. The dominant
line is RAM you pay for whether or not anyone asks a question, because a language
model that is not resident is not a serving box.

**Break-even.** Plug in your own frontier rate; at roughly $3/M input and $15/M
output, a Studio turn with a skill-file system prompt (~5k in, ~300 out) costs
about **$0.02**, so ~$23/month is **~1,150 displaced turns/month — about 38 a
day** — and *more* than that in practice, because Studio already prompt-caches
the frontier prefix at ~10% on repeat turns. Below a few thousand learned turns a
month this is a latency, privacy and independence decision, **not** a cost saving.
Be honest with yourself about which one you are buying.

### Speed — usable for background work, marginal for chat

- **Decode**: Microsoft reports **29 ms/token (~34 tok/s)** for this model on CPU,
  on *unspecified* hardware — read that as a ceiling from a large desktop/server
  CPU, not a 4-vCPU cloud slice. **Estimate 5–15 tok/s on Railway.** A 300-token
  answer is therefore **20–60 seconds**.
- **Time to first token** is the bigger problem: prefill of a 2–5k-token system
  prompt on CPU is roughly **5–30 s cold**. `llama-server` reuses the KV cache
  when consecutive requests share a prefix, and Studio deliberately puts stable
  content first (`agent.py::_apply_prompt_cache`), so *repeated* prompts against
  the same source are much faster. Add `LLAMA_EXTRA_ARGS=--cache-reuse 256` to
  extend that to partially-shared prefixes.
- **Verdict**: good for the work the router actually sends it — learned, repeated,
  background queries where a 30 s answer is fine. Not a replacement for the
  frontier in an interactive chat box. The router's design already reflects this;
  just don't expect Sonnet latency.

**Measure it yourself before believing any of the above.** `llama-bench` is in the
image. Point the service's start command at it once:

```
/opt/bitnet/bin/llama-bench -m /data/models/ggml-model-i2_s.gguf -p 512 -n 128 -t 4
```
`pp512` is prefill tok/s, `tg128` is decode tok/s. That one number decides whether
this is worth $23/month to you, and it takes five minutes to get.

---

## 10. What is NOT verified

No container was built or run to produce this. There is no docker daemon on the
machine these files were written on, and **nothing was deployed to Railway**.

**Verified locally / against primary sources**
- `supervisor.py` compiles and passes 35 local assertions: conditional `--lora`,
  adapter-arrival detection and half-written-file rejection, cgroup thread
  derivation, gateway env wiring, and the bridge over **real sockets** on both
  `::1` and `127.0.0.1` — including a large POST body, an SSE stream verified to
  arrive incrementally rather than buffered, and 40 concurrent connections.
- Download failure handling: 403 / 429 / 404 / non-GGUF payload / wrong size,
  each producing the intended message and leaving no `.part` file behind.
- `railway.json` parses; every field it uses appears in Railway's config-as-code
  documentation.
- Every `llama-server` flag and endpoint used was checked against
  `tools/server/README.md` **at bitnet.cpp's pinned llama.cpp commit**.
- The GGUF URL, its exact byte size, and the BitNet commit pin were checked over
  the network (HTTP HEAD / GitHub API).
- The cmake flags mirror bitnet.cpp's own `setup_env.py` for `-q i2_s`/x86_64.

**Unverified — needs one real build/deploy**
1. **The bitnet.cpp build itself.** Ubuntu 24.04 + clang-18 + those cmake flags
   producing a working `llama-server` is inferred from `setup_env.py`, not
   compiled. Most likely failure point of the whole thing, and the `--help`
   smoke-test in the builder stage will catch it *at build time*, not in prod.
2. **Whether `--lora` works against an I2_S base.** llama.cpp applies LoRA as
   runtime graph ops, which is quant-agnostic in principle, but bitnet.cpp's
   custom I2_S kernels may not exercise that path. **If this fails, the CPU
   adapter story fails and BitNet serves base-only** — a real risk to the whole
   premise, and cheap to test the moment you have a converted adapter.
3. **`convert_lora_to_gguf.py` on a BitNet PEFT adapter** (§8 step 2), from either
   fork.
4. **The `GGML_NATIVE=OFF` + AVX2 baseline** actually running on Railway's CPUs
   (a wrong guess here is a SIGILL on start, not a subtle bug).
5. **Actual tokens/sec, actual RSS, actual monthly bill.** §9 is arithmetic from
   published rates and model dimensions, not a measurement.
6. Railway specifics not exercised: build time for a from-source C++ compile
   against any build timeout, `railway ssh` availability on this account, and
   whether the healthcheck tolerates the model download the way §5 predicts.
