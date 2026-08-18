# Studio BitNet Serving Unit

A real, self-hosted, **OpenAI-compatible** serving box for BitNet, plus the thin
**adapter-aware gateway** that lets Studio's existing client work against it
**unchanged**. This runs on a proper box (GPU or CPU) — not in the dev sandbox.

```
Studio app ──(model=bitnet + studio_adapters)──▶ gateway ──▶ vLLM / llama-server
   agent.py make_llm()                          gateway.py       (real LoRA engine)
        ▲                                            │
        └──────────── trainer writes LoRA ───────────┘
                      (scripts/train_online.py)  shared /adapters volume
```

Studio already speaks OpenAI and attaches its LoRA choice in a custom
`studio_adapters` body field (see `backend/app/agent.py::make_llm`). Real engines
don't understand that field — they pick a LoRA by the OpenAI `model` field (vLLM)
or a global scale call (llama.cpp). **The gateway bridges that gap.** You point
`STUDIO_LLM_BASE_URL` at the gateway; nothing in the app image changes.

---

## 1. Pick a profile

| Profile | Engine | LoRA story | Use when |
|--------|--------|-----------|----------|
| **cpu** *(supported BitNet path)* | [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` serving the BitNet GGUF | Adapter mounted at **startup**; runtime toggles its **scale**. Serves the **global tool_call adapter only.** | CPU-only box. **BitNet's native runtime — the recommended way to serve BitNet.** |
| **gpu** *(needs BitNet-enabled vLLM)* | [vLLM](https://docs.vllm.ai) OpenAI server | Runtime multi-LoRA + per-user style adapters. **But stock vLLM has NO BitNet support** ([vllm #17279](https://github.com/vllm-project/vllm/issues/17279), *not planned*) — the BitNet base won't load. Use only with a BitNet-enabled vLLM build, or set `STUDIO_VLLM_MODEL` to a vLLM-supported base for GPU multi-LoRA. | NVIDIA GPU **+** a BitNet-capable vLLM (or a non-BitNet base). |

```bash
cd serving
docker compose --profile gpu up -d        # GPU box
# or
docker compose --profile cpu up -d        # CPU box
docker compose --profile gpu config       # validate compose without starting
```

---

## 2. The BitNet base model — where to get it

- **Base (training + vLLM):** [`microsoft/bitnet-b1.58-2B-4T`](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T)
  — the same HF id the trainer uses (`STUDIO_TRAIN_BASE_MODEL`). NOTE: **stock
  vLLM cannot load this** (no `BitnetForCausalLM`); it's for TRAINING + a
  BitNet-enabled vLLM only. vLLM pulls it on
  first start into the `hf-cache` volume. A gated pull needs `HUGGING_FACE_HUB_TOKEN`.
- **GGUF (CPU / llama.cpp):** [`microsoft/bitnet-b1.58-2B-4T-gguf`](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T-gguf)
  — download the `i2_s` GGUF into `${STUDIO_MODELS_DIR:-./models}` and set
  `STUDIO_BITNET_GGUF` to its filename. BitNet's official CPU runtime is
  [BitNet.cpp](https://github.com/microsoft/BitNet) (a llama.cpp fork); the stock
  `ghcr.io/ggml-org/llama.cpp:server` image serves the same GGUF over the OpenAI
  API and is what this compose uses.

---

## 3. How the trainer → server adapter volume connects

Both the trainer and the server see the **same directory**:

| Side | Setting | Value |
|------|---------|-------|
| Trainer writes adapters to | `STUDIO_TRAIN_OUTPUT_DIR` | `./adapters` (host) |
| Trainer publishes uri prefix | `STUDIO_TRAIN_ADAPTER_BASE_URI` | `/adapters` (the path **inside** the server container) |
| Compose bind-mounts | `${STUDIO_ADAPTERS_DIR:-./adapters}` → `/adapters` | in `vllm` / `llama` |

So a published adapter's `uri` (e.g. `/adapters/tool_call-1723890000`) is **exactly
the path the server reads**. Trainer writes → server loads, same bytes, no copy.

- **vLLM (gpu):** the gateway calls `POST /v1/load_lora_adapter` with
  `lora_path=<uri>` — vLLM reads the PEFT adapter directory directly. Works with
  the adapter the trainer saved as-is.
- **llama.cpp (cpu):** llama-server loads adapters as **GGUF at startup** and cannot
  hot-load a new adapter FILE at runtime. Two consequences:
  1. Convert the PEFT adapter to GGUF once with
     [`convert_lora_to_gguf.py`](https://github.com/ggml-org/llama.cpp/blob/master/convert_lora_to_gguf.py),
     place it at `./adapters/${STUDIO_TOOLCALL_GGUF:-tool_call.gguf}`.
  2. To serve a **freshly trained** adapter, restart the `llama` service pointing at
     the new file (`docker compose --profile cpu up -d llama`). The trainer's
     push only re-scales an already-mounted adapter on this path.

---

## 4. EXACT Studio env to set

Point Studio's app at the **gateway** (host port `9000`, path `/v1`):

```bash
# Studio backend (Railway service "studio" / your box) — enables BitNet routing:
STUDIO_LLM_BASE_URL=http://<serving-host>:9000/v1   # ← the GATEWAY, not vLLM/llama
STUDIO_BITNET_LLM=openai:bitnet                      # OpenAI client sends model="bitnet"
```

That is all Studio needs. `backend/app/router.py::bitnet_ready` then returns true
once a global `tool_call` adapter is published, and matching prompts route to
BitNet (SQL still re-guarded; any failure escalates to the frontier).

> If you set `STUDIO_GATEWAY_API_KEY` on the gateway, also give Studio a matching
> key for the OpenAI base_url (the LangChain OpenAI client's `api_key`), since the
> gateway will then require `Authorization: Bearer <key>`.

Gateway env (defaults live in `docker-compose.yml`):

| Env | Default | Meaning |
|-----|---------|---------|
| `STUDIO_BACKEND_URL` | `http://vllm:8000/v1` | real engine OpenAI base |
| `STUDIO_BACKEND_KIND` | `vllm` | `vllm` or `llama` |
| `STUDIO_BASE_MODEL_NAME` | `bitnet` | model id clients send / base fallback |
| `STUDIO_GATEWAY_ADAPTER_PRIORITY` | `user_style,tool_call` | most-specific-first adapter selection |
| `STUDIO_GATEWAY_API_KEY` | _(unset)_ | require bearer auth on `/v1/*` + `/admin/*` |

---

## 5. Trainer push (optional, fail-safe)

`scripts/train_online.py` can tell the serving side to load a just-trained adapter
so the **next** call serves it without waiting for a natural cache miss. Gated on
`STUDIO_SERVE_URL`; a serving box that's down **never breaks training**.

```bash
# On the trainer box, in addition to the training env:
STUDIO_SERVE_URL=http://<serving-host>:9000    # the gateway's admin base
# (optional) STUDIO_SERVE_KIND=gateway|vllm     # default: gateway
# (optional) STUDIO_SERVE_TOKEN=<key>           # if the gateway/vLLM needs auth
```

- `gateway` (default): `POST {STUDIO_SERVE_URL}/admin/load_adapter {uri, kind}` — the
  gateway maps the uri → a backend LoRA name and loads it (vLLM) or enables it
  (llama). Idempotent.
- `vllm`: `POST {STUDIO_SERVE_URL}/v1/load_lora_adapter {lora_name, lora_path}` —
  talk straight to vLLM if you skip the gateway for pushes.

If the push fails, the trainer logs it and moves on — serving still picks the new
adapter up on its next call once `active_adapters` returns the new version.

---

## 6. Verified versions & API shapes (checked against official docs)

Pin these; the runtime-LoRA API and CLI flags are version-sensitive.

**vLLM** — image `vllm/vllm-openai:v0.11.0`
([Docker Hub](https://hub.docker.com/r/vllm/vllm-openai/tags),
[LoRA docs](https://docs.vllm.ai/en/latest/features/lora.html))
- Enable: `--enable-lora`; per-request selection: OpenAI **`model` = the LoRA name**.
- Runtime load: `POST /v1/load_lora_adapter` `{"lora_name","lora_path"}`;
  unload: `POST /v1/unload_lora_adapter` `{"lora_name"}`.
- Gated by env **`VLLM_ALLOW_RUNTIME_LORA_UPDATING=True`**.
- `--max-lora-rank` must be ≥ the trainer's LoRA `r` (16 here).

**llama.cpp** — image `ghcr.io/ggml-org/llama.cpp:server`
([container](https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp),
[server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md))
— pin a build tag (e.g. `:server--b6000`) in prod; the HTTP API/flags change between builds.
- OpenAI endpoint: `POST /v1/chat/completions`; model id set via `--alias`.
- LoRA flags: `--lora FNAME`, `--lora-scaled FNAME:SCALE`, `--lora-init-without-apply`.
- Runtime **scale** only: `GET /lora-adapters`, `POST /lora-adapters`
  `[{"id":0,"scale":1.0}]`. **No runtime file load** — new adapter file ⇒ restart.

**BitNet** — [`microsoft/bitnet-b1.58-2B-4T`](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T)
(base) / [`-gguf`](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T-gguf) (CPU) /
[BitNet.cpp](https://github.com/microsoft/BitNet) (official CPU runtime).

---

## 7. Honest limitations

- **Per-user style is GPU-only.** vLLM applies one adapter per request selected by
  the `model` field. The per-user style adapter is trained *on top of* the
  tool-call data, so selecting it (priority `user_style,tool_call`) keeps tool
  calling **and** adds style — one adapter, no in-request merge needed. On the
  **CPU/llama path** only the single global `tool_call` adapter is served; a
  `user_style` block is **logged and ignored**, never an error.
- **CPU hot-swap is limited.** llama-server can't load a new adapter file at
  runtime; a freshly trained CPU adapter needs a `llama` service restart, and PEFT
  adapters must be converted to GGUF first (§3).
- The gateway is **fail-safe by design**: any adapter-load trouble falls back to the
  base model. Studio re-guards BitNet's SQL and escalates to the frontier on any
  failure, so a fallback answer is safe — just un-personalized.
