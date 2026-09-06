# Self-hosting BitNet on your own machine

No Docker. No Railway. No paid service. One NVIDIA Windows laptop doing both
halves of the loop — **serving on the CPU, training on the GPU** — with
`serving/run_local.py` as the one command that brings serving up.

This is the alternative to [RAILWAY.md](RAILWAY.md). Same `gateway.py`, same
`supervisor.py`, same contract with Studio; the only thing that changes is where
it runs and what it costs (nothing).

| | |
|---|---|
| **Serving** | `serving/run_local.py` → `supervisor.py` → `llama-server` (bitnet.cpp) + `gateway.py`. **CPU.** |
| **Training** | `scripts/train_online.py` → PEFT LoRA on `microsoft/bitnet-b1.58-2B-4T-bf16`. **GPU (CUDA).** |
| **Where they meet** | a `tool_call.gguf` file + its `.uri` sidecar in `<dir>/adapters/`, and one publish to Studio's registry. |
| **What Studio needs** | three env vars (§4). Nothing in the app image changes. |
| **Cost** | electricity. |
| **Verified on this machine?** | The runner, the supervisor seam and the gateway path: **yes** (`backend/tests/test_selfhost_runner.py`, `test_serving_readiness.py`). The bitnet.cpp build, the real model, CUDA and Windows: **no** — see §9 before you trust a step. |

---

## 1. Why your GPU does not serve BitNet — and does train the adapter

Read this once and the rest of the document stops being surprising.

### Serving BitNet is a CPU job

Stock **vLLM cannot load BitNet at all.** There is no `BitnetForCausalLM` in
vLLM, and the tracking issue —
[vllm-project/vllm#17279](https://github.com/vllm-project/vllm/issues/17279) —
is closed as **not planned**. So the obvious "put the model on the 4090 with
vLLM" path does not exist; it is not a matter of a flag or a nightly build.

The runtime that *does* work is Microsoft's own:
[**microsoft/BitNet**](https://github.com/microsoft/BitNet) — "bitnet.cpp" — a
fork of llama.cpp whose ternary (1.58-bit) kernels are **CPU** kernels. The
model card for
[`microsoft/bitnet-b1.58-2B-4T-gguf`](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T-gguf)
says it outright: *"you MUST use the dedicated C++ implementation:
bitnet.cpp"*. Stock llama.cpp will not do either — the `I2_S` quantisation type
exists only in Microsoft's fork, and upstream rejects the file with
`tensor 'blk.0.ffn_down.weight' of type 36 (TYPE_IQ4_NL_4_4 REMOVED …) has 6912
elements per row, not a multiple of block size (0)`
([llama.cpp#12997](https://github.com/ggml-org/llama.cpp/issues/12997), open).

**This is not a downgrade.** Cheap CPU inference is the entire point of a 1-bit
model: the weights are ternary, so the hot loop is additions instead of
floating-point multiplies, and the 2.4B-parameter model is 1.1 GB on disk and
~1.8 GB resident. BitNet on a CPU is the design working, not a fallback. Your
GPU sits idle during serving, and that is correct.

### Training the LoRA is a GPU job

The packed 1-bit repo **cannot be fine-tuned at all** — transformers refuses:
*"The model you are trying to fine-tune is quantized with
QuantizationMethod.BITNET but that quantization method do not support
training."* Training happens on the **master weights**,
[`microsoft/bitnet-b1.58-2B-4T-bf16`](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T-bf16)
— 4.8 GB in bf16, an ordinary dense model. A round should take minutes on an
NVIDIA card and hours on a CPU; the CPU/MPS half of that IS measured (~170 s per
micro-batch at `max_length=128` on an Apple M1 — `scripts/README-training.md`
§7), the CUDA half is an estimate nobody here could run (§9).

`scripts/train_online.py::_device_and_dtype()` already picks `cuda` + `bf16` automatically (and `fp16` on pre-Ampere cards,
which only emulate bf16).

The i2_s GGUF the CPU serves is the **quantisation of those same weights**, so a
LoRA trained on the bf16 masters composes at inference. That is what makes one
laptop able to do both halves.

```
        ┌──────────────── your laptop ────────────────┐
        │                                             │
        │  GPU  train_online.py  ── PEFT adapter ──┐  │
        │       (bf16 masters, minutes/round)      │  │
        │                                          ▼  │
        │                        convert_lora_to_gguf.py
        │                                          │  │
        │  CPU  run_local.py                       ▼  │
        │       supervisor.py ── llama-server ◀ tool_call.gguf
        │            └────────── gateway.py ──────────┼──▶ Studio
        └─────────────────────────────────────────────┘
```

---

## 2. Building bitnet.cpp on Windows

You have to build it. There is no official Windows binary of bitnet.cpp's
`llama-server`, and anything called `llama-server` that you did *not* build from
microsoft/BitNet is stock llama.cpp and cannot load the model (§1).

### Use WSL2. Here is why.

**Recommended: Ubuntu under WSL2.** Not because Windows is unsupported, but
because everything around the build is Unix-shaped:

- The build is CMake + **clang**. bitnet.cpp's own `setup_env.py` passes
  `-DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++`; on Linux that is
  `apt install clang`, on Windows it is a Visual Studio component whose exact
  name and version matter.
- The conversion scripts (`convert_lora_to_gguf.py`, §7) are Python with
  Unix-shaped path handling and a requirements file that installs cleanly on
  Linux; `torch` for them is a CPU wheel either way.
- Every command in this document, and every path in `supervisor.py`, is POSIX.
  You will be pasting from here.
- WSL2 shares the machine's RAM and CPU with no virtualisation penalty worth
  measuring for this workload, and `localhost` is forwarded from Windows into
  WSL2 automatically, so Studio on Windows can reach a server in WSL2 at
  `127.0.0.1` with no extra configuration.

```bash
# In Windows PowerShell (once):
wsl --install -d Ubuntu          # then reboot if it asks

# Inside Ubuntu:
sudo apt update
sudo apt install -y git cmake clang libcurl4-openssl-dev python3 python3-pip build-essential

git clone --recursive https://github.com/microsoft/BitNet.git ~/BitNet
cd ~/BitNet
# Optional but recommended: pin the commit this repo's Dockerfile.railway pins,
# so you are building the engine that was checked flag-by-flag against
# gateway.py's needs (RAILWAY.md §2). microsoft/BitNet publishes no tags.
git checkout 0b341e582afbf9e1011f24744b554c96a3477eb5
git submodule update --init --recursive

cmake -B build -DCMAKE_BUILD_TYPE=Release \
      -DBITNET_X86_TL2=OFF \
      -DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++ \
      -DLLAMA_BUILD_TOOLS=ON -DLLAMA_BUILD_EXAMPLES=ON \
      -DLLAMA_BUILD_COMMON=ON -DLLAMA_BUILD_SERVER=ON
cmake --build build --config Release -j "$(nproc)"

./build/bin/llama-server --help | head -3     # ← the smoke test. Usage = built.
```

Those `-D` flags are exactly what bitnet.cpp's `setup_env.py` passes for
`-q i2_s` on x86_64. Expect **10–20 minutes** of compiling.

`GGML_NATIVE` is left at its default (`ON`), i.e. `-march=native`. On your own
laptop that is what you want — you build and run on the same CPU, and it is
worth roughly 10–20%. (`Dockerfile.railway` turns it *off* and pins an AVX2
baseline precisely because Railway builds and runs on different machines.)

### Native Windows: real, but the slower road

microsoft/BitNet's README documents a native Windows build: Visual Studio 2022
with the *Desktop development with C++* workload, *C++ CMake tools for Windows*,
*C++ Clang Compiler for Windows*, and *MSBuild support for LLVM (clang-cl)*,
driven from a **Developer Command Prompt for VS 2022** so the toolchain is on
PATH. It produces `build\bin\Release\llama-server.exe`, and `run_local.py`
already looks in `build\bin\Release\` as well as `build\bin\`.

It is a genuine option, not vapour — but it is the road with more ways to go
wrong (the clang component missing, MSBuild picking MSVC instead of clang-cl,
`libcurl` not found), and none of the conversion tooling in §7 gets easier.
**If you are choosing right now, choose WSL2.** If you already have VS 2022 set
up and prefer it, build there and pass `--engine` with the `.exe` path — nothing
else in this document changes.

> Neither build was performed on the machine these files were written on (an
> Apple M1 with no Docker daemon). §9.

---

## 3. The model file

One file, 1.1 GB, downloaded once.

- Repo: [`microsoft/bitnet-b1.58-2B-4T-gguf`](https://huggingface.co/microsoft/bitnet-b1.58-2B-4T-gguf)
- File: `ggml-model-i2_s.gguf`, **1,187,801,280 bytes** (verified by HTTP HEAD)

**You do not have to download it yourself.** `run_local.py` fetches it on first
start into `<dir>/models/`, checks the `GGUF` magic and the exact byte count,
writes to `.part` and renames only on success, and never fetches it again. If
you would rather pull it by hand (or already have it), drop it at
`<dir>/models/ggml-model-i2_s.gguf` and the runner will say `model present` and
skip straight to starting the engine.

If HuggingFace refuses the download you get a sentence naming
`HUGGING_FACE_HUB_TOKEN` and what to do with it, not a stack trace.

### What CPU inference will actually feel like

Be realistic before you wire it into anything.

- **Decode:** Microsoft reports **29 ms/token (~34 tok/s)** for this model on
  CPU, on *unspecified* hardware — read that as a large desktop or server part.
  On a laptop with 6–8 usable threads, **estimate 8–20 tok/s**. A 300-token
  answer is therefore roughly **15–40 seconds**.
- **Time to first token** is the part people notice: prefilling a 2–5k-token
  system prompt on CPU is **3–15 seconds cold**. `llama-server` reuses the KV
  cache across requests that share a prefix, and Studio deliberately puts stable
  content first (`agent.py::_apply_prompt_cache`), so the second question about
  the same source is much faster than the first. `LLAMA_EXTRA_ARGS=--cache-reuse
  256` extends that to partially-shared prefixes.
- **On battery it will be slower**, and the fan will run. Windows power mode
  "Best performance" while plugged in is worth more than any flag here.

**This is the right shape for what Studio actually sends it.** `router.choose()`
only routes a prompt to BitNet once `qcache` has *learned* it — a repeated
question with a good score, on a source the requester can read. That is
background, repetitive work where 20 seconds is fine. It is **not** a frontier
replacement in an interactive chat box, and the router already knows that:
anything unlearned, and any failure, goes to the frontier model.

**Measure it rather than believing the above.** The build gives you
`llama-bench` for free:

```bash
~/BitNet/build/bin/llama-bench -m ~/bitnet-local/models/ggml-model-i2_s.gguf -p 512 -n 128 -t 8
# pp512 = prefill tok/s, tg128 = decode tok/s
```

---

## 4. Running it

```bash
cd ~/studio                # wherever this repo is
python3 serving/run_local.py --engine ~/BitNet/build/bin/llama-server
```

That is the whole thing. It:

1. finds the engine (`--engine`, else `$STUDIO_ENGINE_BIN`, else `$BITNET_HOME`,
   else a `BitNet/` checkout beside you or in `$HOME`, else `PATH`) and, if
   there is none, prints the build instructions from §2 and exits **2**;
2. creates `./bitnet-local/{models,adapters}` and downloads the GGUF if absent;
3. starts `llama-server` on a loopback port and `gateway.py` on the public one;
4. prints the line you paste into Studio;
5. streams both processes' logs to your terminal until Ctrl-C, which stops both.

```
────────────────────────────────────────────────────────────────────
  Studio BitNet serving unit — local (CPU inference; your GPU is for training)
────────────────────────────────────────────────────────────────────
  engine    /home/you/BitNet/build/bin/llama-server   [--engine]
  model     /home/you/studio/bitnet-local/models/ggml-model-i2_s.gguf
  adapter   none yet — serving the BASE model (this is normal on day one)
  threads   8     ctx 4096
  listening http://127.0.0.1:9000  (engine on 127.0.0.1:41235, loopback only)

  Put these in Studio's backend .env, then restart Studio:

      STUDIO_LLM_BASE_URL=http://127.0.0.1:9000/v1
      STUDIO_BITNET_LLM=openai:bitnet

  Check it:  curl 127.0.0.1:9000/health
  Ctrl-C stops the engine and the gateway.
────────────────────────────────────────────────────────────────────
```

### Flags worth knowing

| Flag | Default | Why you would change it |
|---|---|---|
| `--dir` | `./bitnet-local` | Put the 1.1 GB somewhere with room: `--dir /mnt/d/bitnet`. Inside WSL2's own filesystem is much faster than `/mnt/c`. |
| `--engine` | discovered | Your build, if it is not in an obvious place. |
| `--port` | 9000 | Something else has 9000. `0` picks a free port and prints it. |
| `--threads` | half your logical CPUs | llama.cpp decode is memory-bandwidth bound, so hyperthread siblings mostly add contention. Benchmark before overriding. |
| `--ctx` | 4096 | 4096 **is the model's maximum** (`max_position_embeddings`). Lower it to save RAM; you cannot raise it. |
| `--api-key` | unset | Mandatory the moment this is reachable from anywhere but this machine (§5b). |
| `--host` | `127.0.0.1` | Only for §5b, and then only with `--api-key`. |
| `--check` | — | Preflight: is the engine there, is the model there, are the ports free? Starts no processes and creates no directories. |
| `--print-env` | — | Just the Studio lines, for a script. |
| `--no-download` | — | Fail rather than pull 1.1 GB (useful on a metered connection). |

Every flag has an environment equivalent — see `python3 serving/run_local.py -h`.

### The Studio side

Three variables, in Studio's **backend** environment:

```bash
STUDIO_LLM_BASE_URL=http://127.0.0.1:9000/v1   # ← the GATEWAY, not llama-server
STUDIO_BITNET_LLM=openai:bitnet                 # the OpenAI client sends model="bitnet"
STUDIO_LLM_API_KEY=<same string as --api-key>   # ONLY if you set --api-key
```

- `STUDIO_LLM_BASE_URL` is gate one of `router.bitnet_ready()`. Gate two is a
  published `tool_call` adapter — until §6/§7 are done, `bitnet_ready()` is
  `False` and Studio sends this box nothing at all. That is intended: an
  un-adapted base model would mostly fail to emit usable SQL.
- `STUDIO_LLM_API_KEY` is applied by `agent.py::make_llm` **for the self-hosted
  spec only**, never to a user's BYOK provider key. With no key set it sends the
  placeholder `studio-local`, because the OpenAI client insists on some
  credential.
- It must end in `/v1`, and it must be reachable **from the Studio backend
  process**. If Studio itself runs in a container, `127.0.0.1` is that
  container's own loopback — use `http://host.docker.internal:9000/v1` instead.

---

## 5. Connecting it: two shapes

### (a) Studio local too — start here

Everything on one machine. Studio's backend talks to `127.0.0.1:9000`, nothing
listens on an external interface, no tunnel, no key, nothing exposed to the
internet at any point.

```
  [ laptop ]  Studio backend ──▶ 127.0.0.1:9000 gateway ──▶ 127.0.0.1:… llama-server
```

**Recommended for getting it working, and probably for keeping it.** Reasons:

- The loop you are debugging has four moving parts already (engine, gateway,
  trainer, adapter conversion). Adding a tunnel means a fifth thing that can be
  the reason nothing works, and it is the one with the least informative errors.
- Latency is loopback. On the Railway shape, every BitNet turn crosses the
  public internet twice.
- No security surface at all: nothing to authenticate because nothing is
  reachable.
- The trainer, the converter and the serving box share a filesystem, so §7 is a
  `cp` rather than an upload.

If Studio's backend runs under WSL2 alongside the serving unit, or on Windows
while serving runs in WSL2, `127.0.0.1:9000` works either way — WSL2 forwards
localhost between the two.

### (b) Studio on Railway, model on the laptop

Possible, and occasionally what you want (you already run Studio for other
people and only the model is coming home). Know what it costs:

- **A tunnel is required.** Your laptop has no public address. Use
  [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
  (`cloudflared tunnel --url http://127.0.0.1:9000`, free, no account for a
  quick tunnel) or `ngrok http 9000`. Both print an `https://…` URL; that URL +
  `/v1` is `STUDIO_LLM_BASE_URL` on the Railway service.
- **The gateway key stops being optional.** The moment that URL exists, anyone
  who has it can use your GPU-adjacent laptop as a free model endpoint *and*
  reach `/admin/load_adapter`. Start with `--api-key "$(openssl rand -hex 24)"`
  and set `STUDIO_LLM_API_KEY` to the same string on Studio. `/health` stays
  unauthenticated on purpose (a healthcheck cannot carry a secret), and it
  reveals only stage/adapter metadata.
- **The laptop must stay awake.** A sleeping laptop is an endpoint that fails
  every request. On Windows: Settings → System → Power → *Screen and sleep* →
  **Never** on AC; or `powercfg /change standby-timeout-ac 0`. Closing the lid
  still sleeps it unless you change the lid action too. If serving runs in WSL2,
  also know that WSL2 shuts down its VM after a period with no activity — a live
  `llama-server` counts as activity, but a suspended laptop does not.
- **A quick cloudflared tunnel URL changes every time you restart it**, so
  `STUDIO_LLM_BASE_URL` on Railway has to be updated each time. A named tunnel
  (free, needs a Cloudflare account and a domain) gets you a stable hostname.
- **Latency stacks on top of §3.** Two internet hops plus 15–40 s of CPU decode.

Do (a) first even if (b) is where you are going. Prove the loop end to end on
loopback, then move the URL.

---

## 6. The GPU half: training the adapter on Windows/CUDA

This runs wherever your CUDA-capable Python is. Native Windows is the simplest
place for it (the NVIDIA driver is already there, and `pip install torch` with
the CUDA index just works); WSL2 also works if your driver supports CUDA-on-WSL.
Pick one and stay in it — the trainer and the serving unit only ever meet
through files and HTTP, so they do not need to share a Python environment.

### Install

```powershell
py -3.11 -m venv .venv-train
.\.venv-train\Scripts\Activate.ps1

pip install torch --index-url https://download.pytorch.org/whl/cu124
pip install -r scripts\requirements-trainer.txt

python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
# ← must print a cuda version and True. If it prints None/False you installed
#   the CPU wheel; uninstall torch and redo the line above with the cu124 index.
```

(`requirements-trainer.txt` lists `torch>=2.2` unpinned because CPU wheels are
fine for the *plumbing*; the `--index-url` line above is what makes it a CUDA
build. Install torch first, then the requirements file, so pip does not resolve
a CPU wheel over the top of it.)

### VRAM

The bf16 master weights are **4.8 GB**, before optimizer state and activations.
`train_online.py` prints a verdict for your exact card before it loads anything
(`_hardware_preflight` / `_vram_advice`), but in short:

| Card | Verdict |
|---|---|
| **≥ 10 GB** | Comfortable. You can raise `STUDIO_TRAIN_BATCH_SIZE`. |
| **8 GB** | The defaults are sized for this: batch 1 × grad-accum 8, `max_length` 1024, gradient checkpointing on. |
| **6 GB** | Tight but usually workable: `STUDIO_TRAIN_GRAD_CHECKPOINT=1`, and close anything else holding VRAM (a browser or a game holds 1–2 GB). If it still OOMs, `STUDIO_TRAIN_MAX_LENGTH=512` — but that truncates the **end** of long samples, which is the tool-call label, so it is a last resort rather than a free win. |
| **< 6 GB** | 4.8 GB of weights leaves nothing for activations. Train on CPU (`STUDIO_TRAIN_DEVICE=cpu` — hours, but it finishes) or point `STUDIO_TRAIN_BASE_MODEL` at a smaller base. |

**On a pre-Ampere card** (GTX 16xx, RTX 20xx — compute capability < 8.0) set
`STUDIO_TRAIN_DTYPE=fp16`. Torch reports bf16 as "available" on those cards but
*emulates* it: slower than fp16 and liable to silently underflow.
`_device_and_dtype()` already detects this and switches for you; the variable is
there for when you know better. Note it is a **compatibility** switch, not a
memory saving — fp16 and bf16 are both 2 bytes.

### First, the bootstrap corpus

There is a circularity to break: routing to BitNet needs an adapter, the adapter
needs rollouts, and rollouts come from traffic that never happens because
nothing routes to BitNet. `scripts/bootstrap_rollouts.py` breaks it using
machinery Studio already has — real schemas, the real SQL drafter, every pair
executed through the real gateway as a real user before it is kept.

```bash
python scripts/bootstrap_rollouts.py --dry-run          # see what it would make
python scripts/bootstrap_rollouts.py --source demo      # write them
```

Measured on the seeded demo warehouse: 486 candidates → 324 verified rollouts
kept, but only **six distinct SQL shapes**. Read that honestly — it is enough to
make an adapter exist and to prove the loop end to end. It is not enough to make
one good. Real traffic is what improves it.

### Then one training round

```powershell
$env:STUDIO_API_URL      = "http://localhost:8000"
$env:STUDIO_TRAINER_TOKEN = "<an admin JWT>"      # or STUDIO_TRAINER_EMAIL/PASSWORD
$env:STUDIO_TRAIN_OUTPUT_DIR = "C:\studio\adapters"

python scripts\train_online.py --dry-run          # plumbing only, no ML deps used
python scripts\train_online.py --once             # the real round
```

`--dry-run` first: it polls, formats and reports **without importing torch**, so
if your credentials or the API URL are wrong you find out in two seconds instead
of after a 4.8 GB download. An SFT round needs `STUDIO_TRAIN_MIN_NEW` (32) new
usable samples; the bootstrap corpus clears that comfortably.

### Watch the HuggingFace download

This is the step that wasted two hours on the other machine, so it is worth a
paragraph. `from_pretrained()` blocks **with no output at all** until the HTTP
transfer actually starts — a DNS stall, a corporate proxy, a full disk and a
perfectly healthy 4.8 GB download look identical for the first minute.

`train_online.py::announce_model_fetch` now prints what is about to be fetched,
where it will land, and how much free space that volume has, *before* handing
control to transformers. Use it:

- If it says **"already in the HF cache — no download expected"**, a long pause
  after that is training, not a fetch. Set `HF_HUB_OFFLINE=1` to make it a
  guarantee (it then fails fast rather than reaching for the network).
- If it says **"NOT cached: downloading ~4.8 GB"**, watch the cache directory
  grow: `du -sh ~/.cache/huggingface/hub` (Linux/WSL) or
  `Get-ChildItem -Recurse $env:USERPROFILE\.cache\huggingface\hub | Measure-Object -Sum Length`
  (PowerShell). **If that number does not move for 60 seconds, it is stuck.**
  Ctrl-C is safe — the download resumes on the next run.
- Pre-fetching it separately gives you a progress bar and separates "the
  download is broken" from "training is broken":
  ```bash
  pip install huggingface_hub[cli]
  huggingface-cli download microsoft/bitnet-b1.58-2B-4T-bf16
  ```
- Short of disk? `HF_HOME=D:\hf-cache` moves the whole cache.

A successful round ends with a line like:

```
[trainer] published global/tool_call v1 <- C:\studio\adapters\tool_call-1723890000  metrics={...}
```

**That uri is the identity you will need in §7. Copy it exactly.** Publishing it
also flips `router.bitnet_ready()` to `True` — which means Studio will start
routing learned prompts here **before** the serving box has the adapter. In that
window BitNet answers on the base model, mostly fails, and Studio escalates to
the frontier: a retry, never a wrong answer. Do §7 promptly.

---

## 7. Closing the loop: the adapter onto the serving box

`llama-server` loads LoRA adapters as **GGUF at startup**, and cannot hot-load a
new adapter *file* at runtime (it can only re-scale one already mounted). So
there are three steps, and the supervisor handles the third.

### 7.1 Convert the PEFT adapter to GGUF

Use **bitnet.cpp's vendored** converter, not upstream llama.cpp's — upstream
does not know this architecture.

```bash
cd ~/BitNet
python3 -m pip install -r 3rdparty/llama.cpp/requirements.txt   # or the
        # requirements/requirements-convert_lora_to_gguf.txt file if your
        # checkout has one; it needs torch (CPU wheel is fine), transformers,
        # numpy and gguf

python3 3rdparty/llama.cpp/convert_lora_to_gguf.py \
        --base /path/to/bitnet-b1.58-2B-4T-bf16 \
        --outfile ~/bitnet-local/adapters/tool_call.gguf \
        /mnt/c/studio/adapters/tool_call-1723890000
```

- `--base` points at the **bf16 base**, so the converter reads its config
  locally instead of fetching from HuggingFace. The HF cache snapshot works:
  `~/.cache/huggingface/hub/models--microsoft--bitnet-b1.58-2B-4T-bf16/snapshots/<hash>`.
- Run `convert_lora_to_gguf.py --help` first; the flags have changed across
  llama.cpp versions and your checkout is the authority.
- If you trained on Windows and are converting in WSL2, `C:\studio\adapters` is
  `/mnt/c/studio/adapters` over there. That is a path, not the uri — see below.

> **This is the riskiest step in the whole document.** Whether this converter
> handles a BitNet PEFT adapter, and whether `--lora` applies at all on top of
> an `i2_s` base, are both **unverified** (§9). If either fails, serving works
> and stays on the base model — which the gateway reports honestly rather than
> pretending otherwise.

### 7.2 Put it in place, with its provenance

```bash
cp tool_call.gguf ~/bitnet-local/adapters/tool_call.gguf
printf '%s' 'C:\studio\adapters\tool_call-1723890000' \
    > ~/bitnet-local/adapters/tool_call.gguf.uri
```

The `.uri` sidecar is not optional bookkeeping. `gateway.py` **refuses to claim
an adapter whose provenance it cannot prove**: when Studio asks for
`tool_call` at uri *X*, the gateway compares *X* against the sidecar of the file
the engine actually mounted. No sidecar means the mounted file is *anonymous*,
and an anonymous adapter never matches anything — the request is served on the
base model and `/health` reports the mismatch. That check exists because the
alternative (re-scaling whatever happens to be mounted and reporting success)
had Studio believing a trained adapter was live while the box ran the base
model.

So the sidecar must contain the **published uri, byte for byte** — the string
after `<-` in the trainer's publish line. On this CPU path the uri is an
**identity, not a path anything opens**: `llama-server` opens
`adapters/tool_call.gguf`, and the uri is only ever compared. The trainer's
"this uri is a WINDOWS path" advisory is therefore harmless here — as long as
the sidecar matches what was published. (Setting
`STUDIO_TRAIN_ADAPTER_BASE_URI` to a tidier value before training makes both
sides prettier.)

### 7.3 Watch the supervisor mount it

Nothing else to do. `run_local.py` sets the adapter poll to **5 seconds**, so
within a few seconds of the file settling you will see:

```
[supervisor] adapter changed (None → (1234567, ...)) — restarting the engine to
             mount it (llama-server cannot hot-load an adapter FILE).
[supervisor] starting engine (WITH adapter, epoch 2): … --lora …/tool_call.gguf
             --lora-init-without-apply
```

The gateway and your port never go down. The engine is unavailable for the few
seconds of reload, during which the gateway's fail-safe returns and Studio
escalates. The epoch bump tells the gateway its "already enabled" cache is stale
so it re-sends the scale call — without that, a restarted engine serves the base
model while both sides report success.

The adapter is mounted at scale 0 (`--lora-init-without-apply`); the gateway's
`POST /lora-adapters [{"id":0,"scale":1.0}]` on the first matching request is
what turns it on.

---

## 8. Verifying it, step by step

Each step is a command and what the answer means. Stop at the first failure —
later steps depend on earlier ones.

```bash
# 1. THE ENGINE EXISTS. Before anything else.
~/BitNet/build/bin/llama-server --help | head -3
#   PASS: usage text. FAIL: "No such file" → §2, you have not built it.

# 2. PREFLIGHT. No downloads, no processes.
python3 serving/run_local.py --check --engine ~/BitNet/build/bin/llama-server
#   PASS: "preflight OK — engine executable, ports free, model …".
#   FAIL(2): read the message; it names the missing thing and the fix.

# 3. IT COMES UP. In one terminal:
python3 serving/run_local.py --engine ~/BitNet/build/bin/llama-server
#   First run downloads 1.1 GB; the gateway answers throughout.

# 4. READINESS, from another terminal. This is the honest one.
curl -s localhost:9000/health
#   {"ok":false,"stage":"downloading_model"}  → still fetching. Wait.
#   {"ok":true,"stage":"ready","mounted_adapter":null}  → serving the BASE model.
#   {"ok":false,"stage":"engine_not_answering"} → the process is up but its API
#       is not. Almost always a stock-llama.cpp binary failing on the i2_s file:
#       look in the runner's log for "type 36 … block size (0)".
#   A 200 while the model is still downloading would be a readiness bug; it is
#   pinned by backend/tests/test_serving_readiness.py.

# 5. THE ENGINE REALLY GENERATES. The first proof a 1-bit model has produced a
#    token on your hardware.
curl -s localhost:9000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"bitnet","max_tokens":16,
  "messages":[{"role":"user","content":"Say the single word: ready"}]}'
#   PASS: choices[0].message.content comes back. Time it — that is your §3
#   reality check.

# 6. THE ADAPTER BLOCK, exactly as Studio sends it. Before §7 this must STILL
#    answer, via the base-model fail-safe.
curl -s localhost:9000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model":"bitnet","max_tokens":16,
  "messages":[{"role":"user","content":"hi"}],
  "studio_adapters":{"tool_call":{"uri":"/adapters/does-not-exist","version":1}}}'
#   PASS: an answer, and the runner's log says the gateway REFUSED to claim an
#   adapter it cannot prove. That refusal is the check working.

# 7. AFTER §7: the adapter is mounted AND claimed.
curl -s localhost:9000/health | python3 -m json.tool
#   PASS: "mounted_adapter": {"uri": "<exactly what the trainer published>"}
#   and no "adapter_mismatch". A mismatch here means your .uri sidecar does not
#   match the published uri — fix the sidecar, not the gateway.

# 8. STUDIO SEES IT. Only now set STUDIO_LLM_BASE_URL and restart Studio.
curl -s localhost:8000/health
curl -s localhost:8000/api/training/adapters -H "Authorization: Bearer $TOKEN"
#   PASS: a row with "kind": "tool_call", "status": "active" is listed →
#         router.bitnet_ready()'s second gate is satisfied.
#   An empty list → nothing was published; §6. (403 → that token is not an
#         admin; this endpoint is admin-only.)

# 9. THE REAL THING. Ask Studio a question it has LEARNED (asked before, well
#    scored, same source), and look at the response metadata.
#   PASS: served_by = bitnet.
#   Still "frontier"? That is the router doing its job: the prompt is not in
#   qcache's learned set, or the requester cannot read a table the learned
#   pattern names. Both are correct refusals, not failures.
```

---

## 9. What is verified, and what is not

The machine this was written on is an **Apple M1 with no Docker daemon**. It
cannot compile for x86, cannot run CUDA, and is not Windows. Be precise about
which line you are trusting.

**Verified here, by tests that actually run**

| Thing | Where |
|---|---|
| `run_local.py` resolves flags → env → defaults, and never into `/data` | `backend/tests/test_selfhost_runner.py` |
| A missing engine gives instructions naming bitnet.cpp and exits 2, before creating anything or fetching anything | same |
| An already-present model is **not** re-downloaded (proved by pointing the fetch URL at a dead port and still reaching `ready`) | same |
| The printed `STUDIO_LLM_BASE_URL` is the port that actually binds and answers | same |
| runner → supervisor → gateway → `/health: ready`, end to end against a **stub** engine | same |
| Ctrl-C and SIGTERM stop both children | same |
| `supervisor.load_config()` reads back exactly what `run_local.py` resolved, and the container defaults (`/data`, `/opt/bitnet/bin/llama-server`, bridge on) are unchanged | same |
| `/health` is 503 until the unit can really serve; the gateway refuses adapters it cannot prove, and drops its cache on an engine restart | `backend/tests/test_serving_readiness.py` |
| Studio's `studio_adapters` shape, `bitnet_ready()`'s two gates, escalation on failure | `backend/tests/test_bitnet_path.py` |
| The bootstrap corpus executes and respects RBAC | `backend/tests/test_bootstrap_rollouts.py` |

**Not verified — needs your laptop**

1. **The bitnet.cpp build on Windows or WSL2.** The commands in §2 mirror
   `setup_env.py` and `Dockerfile.railway`; neither has been compiled here. The
   `--help` smoke test is how you find out in one line.
2. **The native-Windows VS 2022 build** (§2). Described from microsoft/BitNet's
   documentation, not performed.
3. **That the i2_s GGUF loads and generates** on your CPU.
4. **`convert_lora_to_gguf.py` on a BitNet PEFT adapter** (§7.1).
5. **That `--lora` applies at all on an i2_s base.** llama.cpp applies LoRA as
   runtime graph ops, which is quant-agnostic in principle, but bitnet.cpp's
   custom I2_S kernels may not exercise that path. **If this fails, the CPU
   adapter story fails and BitNet serves base-only** — the biggest single risk
   in this design, and cheap to test the moment you have a converted adapter.
6. **Every tokens-per-second number in §3.** Arithmetic and a vendor claim on
   unspecified hardware, not a measurement. `llama-bench` settles it in five
   minutes.
7. **CUDA training on your card** — the VRAM table is the trainer's own
   arithmetic over model dimensions, not a run on an 8 GB laptop GPU.

---

## 10. When it does not work

| Symptom | What it is |
|---|---|
| `no llama-server found` | You have not built bitnet.cpp. §2. The message lists every path it checked. |
| Engine exits immediately; log has `type 36 … not a multiple of block size (0)` | That is **stock llama.cpp**, not bitnet.cpp. The runner warns when the binary it found does not look like a bitnet.cpp build; this is that warning coming true. |
| `/health` stuck on `downloading_model` | It is downloading 1.1 GB. Watch `<dir>/models/*.part` grow. If it is not growing, check the log for the HuggingFace message. |
| `/health` says `engine_not_answering` | The process is up, its API is not: the model is still loading (large, slow disk) or it failed to load. The runner's log has the engine's own stderr. |
| `engine will not stay up` after 5 restarts | Out of memory (needs ~2 GB at ctx 4096), a bad GGUF, or bad `LLAMA_EXTRA_ARGS`. |
| Adapter copied in, `/health` still `mounted_adapter: null` | The supervisor only mounts a file that has stopped changing. Wait 10 s. If it stays null, the file is still being written or is not readable. |
| `adapter_mismatch` on `/health` | The `.uri` sidecar does not equal the uri the trainer published. §7.2 — and this is the check doing its job. |
| Studio never routes to BitNet | `bitnet_ready()` needs **both** `STUDIO_LLM_BASE_URL` set **and** a published `tool_call` adapter. And then the prompt must be one `qcache` has learned. |
| Port already in use | Something else has 9000. `--port 0` picks a free one and prints it. |
| Windows: `UnicodeEncodeError` from the trainer | Already handled — `train_online.py` reconfigures stdout to UTF-8 with replacement. If you see one from another script, `set PYTHONUTF8=1`. |

---

## See also

- **[README.md](README.md)** — the profiles, the model sources, the
  trainer↔server adapter volume, and §7's honest limitations.
- **[RAILWAY.md](RAILWAY.md)** — the same unit as a paid one-service deployment,
  with the cost breakdown. Everything in §2 (the engine argument), §4 (the
  adapter-on-day-one problem) and §8 (verification) applies to both.
- `serving/run_local.py -h` — every flag and its environment equivalent.
- `scripts/train_online.py` — the trainer's full env list is in its docstring.
