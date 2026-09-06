# Training the BitNet adapter on your own laptop

`scripts/train_online.py` is the **learning half** of Studio's BitNet loop. It
polls Studio for reward-labeled rollouts, turns the good ones into tool-calling
training samples, fine-tunes a small **LoRA adapter**, and publishes it back so
serving hot-swaps to it. Then it does it again. Nothing here is imported by the
API — it talks to Studio over HTTP, so it can run on a different machine, on
your desk, with no cloud bill.

This guide is for running it on a **Windows + NVIDIA laptop**. It assumes you
have not read the script.

---

## 1. The hardware split (it is the opposite of the intuitive one)

| Job | Runs on | Why |
|---|---|---|
| **Training** the LoRA — this script | **The GPU** | It fine-tunes `microsoft/bitnet-b1.58-2B-4T-bf16`: ~4.8 GB of ordinary bf16 weights, plus activations and optimizer state. That is a normal small-model fine-tune and it wants CUDA. On CPU it works but is punishing (§7 has the measurements) — treat it as an overnight fallback, not a plan. |
| **Serving** BitNet — `serving/` | **The CPU** | Stock vLLM cannot load BitNet at all ([vllm#17279](https://github.com/vllm-project/vllm/issues/17279), *"not planned"*). The supported runtime is [microsoft/BitNet](https://github.com/microsoft/BitNet) (`bitnet.cpp`), a llama.cpp fork with ternary CPU kernels. **The GPU does not help there** — 1-bit inference on CPU is BitNet's entire design goal, not a downgrade. See `serving/README.md` §1. |

So one laptop does both, each job on the part of the machine that suits it: the
GPU trains while the CPU cores serve. They are separate processes and they can
run at the same time.

## 2. Two model repos, and only one of them can be trained

| Repo | Size | Use |
|---|---|---|
| `microsoft/bitnet-b1.58-2B-4T-bf16` | **4.83 GB** (measured: one `safetensors` blob of 4,825,679,400 bytes) | **Training.** Master weights, ordinary bf16 tensors. This is the trainer's default `STUDIO_TRAIN_BASE_MODEL`. |
| `microsoft/bitnet-b1.58-2B-4T` | ~1.2 GB | **Not trainable.** The packed 1-bit inference artifact. transformers refuses it outright: *"The model you are trying to fine-tune is quantized with QuantizationMethod.BITNET but that quantization method do not support training."* |
| `microsoft/bitnet-b1.58-2B-4T-gguf` | ~1.1 GB | **Serving only** (the `i2_s` GGUF `bitnet.cpp` loads). |

The 1-bit artifact is the *quantization of* the bf16 masters, so a LoRA trained
on the masters composes with the quantized model at inference. That is why the
split is legitimate and not a bait-and-switch.

One detail worth knowing, because it explains the speed section below: the
`-bf16` repo's own `config.json` still declares

```json
"quantization_config": {"quant_method": "bitnet", "linear_class": "autobitlinear",
                        "quantization_mode": "online"}
```

— the weights are stored bf16 and **re-quantized to ternary on the fly inside
every linear**, on every forward pass. That is what makes it trainable (there is
a real bf16 master to put a gradient on) and it is also why transformers prints
*"You don't have a GPU available to load the model, the inference will be slow
because of weight unpacking"* when you run it without CUDA. Take that warning
literally: this path is unusually punishing on CPU/MPS.

If you point `STUDIO_TRAIN_BASE_MODEL` at the packed repo you get the
transformers error above, immediately, before any GPU work. Change it back.

---

## 3. Install (Windows + NVIDIA)

Python **3.10–3.12**. From the repo root, in PowerShell:

```powershell
py -3.12 -m venv .venv-trainer
.\.venv-trainer\Scripts\Activate.ps1
python -m pip install -U pip

# 1. torch WITH CUDA — do this FIRST and on its own.
#    Get the exact line for your driver from https://pytorch.org/get-started/locally/
#    (Stable / Windows / Pip / Python / CUDA). Known-good example:
pip install torch --index-url https://download.pytorch.org/whl/cu124

# 2. everything else
pip install -r scripts\requirements-trainer.txt
```

**Check that step 1 actually worked** — this is the single most common way to
end up training on the CPU by accident:

```powershell
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"
# want: 2.x.y+cu124  12.4  True
# torch.version.cuda == None  ->  you have the CPU-only wheel. pip uninstall torch, redo step 1.
```

The trainer tells you the same thing at the top of every round: a line reading
`[trainer] device=cpu` on a machine with an NVIDIA card means the CPU wheel.

You do **not** need the CUDA toolkit, Visual Studio, or WSL. The torch wheel
ships its own CUDA runtime; you need only a recent NVIDIA **driver**.

> `scripts/Dockerfile.trainer` builds a **CPU-only** trainer image (it installs
> the `whl/cpu` torch index deliberately, for a headless box with no GPU). Do
> not use it on the laptop — it would leave your NVIDIA card idle and the round
> would take hours. Use the venv above.

> The trainer is a plain Python process. Leave it running in its own terminal;
> `--once` if you would rather drive each round by hand.

## 4. Point it at Studio and do a dry run first

```powershell
$env:STUDIO_API_URL      = "http://localhost:8000"     # your Studio API
$env:STUDIO_TRAINER_EMAIL    = "admin@your-org"        # an admin service account
$env:STUDIO_TRAINER_PASSWORD = "..."                   # (or STUDIO_TRAINER_TOKEN)
$env:STUDIO_TRAIN_OUTPUT_DIR = "C:\studio\adapters"

python scripts\train_online.py --dry-run
```

`--dry-run` uses **no ML dependencies at all** — pure stdlib. It pulls rollouts,
formats samples, writes `last_samples.jsonl`, and stops. If that prints a sample
count, the plumbing (auth, poll, source conditioning, cursor) is correct and any
later failure is a machine-learning problem, not a wiring problem.

Then one real round:

```powershell
python scripts\train_online.py --once
```

and finally the loop (`python scripts\train_online.py`, no flags) which polls
every `STUDIO_TRAIN_POLL_SECONDS` (default 60) forever.

### No rollouts to train on?

A brand-new deployment has no traffic, so there is nothing to learn from, so
`--dry-run` reports `0 usable samples` and `--once` says *"not enough new
experience"*. That is the documented circularity — break it with:

```powershell
python scripts\bootstrap_rollouts.py --source demo --limit 200
```

which manufactures a first corpus from real schema + a deterministic SQL drafter
and verifies every row through the real gateway. Read that script's header for
what the corpus can and cannot teach (measured: 486 candidates → 324 kept, but
only **six distinct SQL shapes**).

## 5. Where the adapter goes — and the URI gotcha

The trainer writes `C:\studio\adapters\tool_call-<timestamp>\` and then
**publishes a URI** to Studio's adapter registry. What that URI has to be
depends on which serving path you are on, and the two are different:

- **The CPU / `bitnet.cpp` path** (`serving/run_local.py`, and what this repo's
  self-hosting guide describes) — the URI is an **identity, not a path anything
  opens**. `llama-server` mounts `<dir>/adapters/tool_call.gguf` at startup, and
  the gateway only ever *compares* the requested URI against the `.uri` sidecar
  sitting beside that file. So the URI never has to resolve on the serving box —
  but it must match the sidecar **byte for byte**, or the gateway refuses to
  claim the adapter and serves the base model. See `serving/SELFHOST.md` §7.2.
- **The GPU / vLLM path** — the URI *is* opened: `gateway.py` posts it to vLLM
  as `{"lora_name", "lora_path"}` and vLLM loads that directory itself. There it
  has to be a path valid **on the serving process's filesystem**.

Either way, publishing a `C:\...` string when the server is somewhere Unix-shaped
is at best confusing and at worst broken, so set the base explicitly:

- **Trainer and server both on Windows, same folder** → nothing to do. The
  default published URI is the trainer's own absolute output path.
- **Server in WSL, Linux, or a container** → it sees that folder under a
  different name, and the Windows path is meaningless to it. Set the base to the
  path (or, on the CPU path, the identity) *the server* will be told about:

```powershell
# C:\studio\adapters  is  /mnt/c/studio/adapters  inside WSL
$env:STUDIO_TRAIN_ADAPTER_BASE_URI = "/mnt/c/studio/adapters"
```

The trainer prints this warning itself when it notices it is publishing a
Windows path and you have not set the variable. The join is separator-aware:
a `/mnt/...` base always produces `/mnt/c/studio/adapters/tool_call-1723890000`,
never a mixed `/mnt/c/studio/adapters\tool_call-...`.

Optionally set `STUDIO_SERVE_URL` to the serving gateway so a fresh adapter is
loaded immediately rather than at the next natural cache miss. If the serving
box is down, the push is logged and ignored — it never breaks training.

---

## 6. VRAM, by card

Defaults are sized for an **8 GB** card: `max_length=1024`, batch 1, grad-accum
8, gradient checkpointing **on** for CUDA. The trainer prints a memory verdict
*before* it loads 4.8 GB of weights, and the peak actually used afterwards.

| VRAM | What to do |
|---|---|
| **12 GB+** | Defaults work. Go faster: `STUDIO_TRAIN_BATCH_SIZE=2` and `STUDIO_TRAIN_GRAD_ACCUM=4` (same effective batch, fewer, bigger passes). |
| **8 GB** (RTX 4060 / 3070 laptop) | Defaults. Keep gradient checkpointing on — it is what makes it fit. Close anything else using the GPU. |
| **6 GB** (RTX 3050 / 2060 laptop) | Tight but usually workable: keep batch 1 + checkpointing (it costs time — ~45% measured on MPS — but it is the memory you need), close other GPU users first (`nvidia-smi` shows who holds VRAM; the Windows desktop itself takes ~0.5–1 GB). If it still OOMs, `STUDIO_TRAIN_MAX_LENGTH=768`, then `512`. |
| **4 GB or less** | The 4.8 GB of weights do not fit. `STUDIO_TRAIN_DEVICE=cpu` (overnight, but it completes and publishes a real adapter), or a smaller `STUDIO_TRAIN_BASE_MODEL`. |

Two things worth being precise about, because the internet is sloppy about both:

- **`fp16` is not a memory saving over `bf16`.** Both are 2 bytes per weight.
  `STUDIO_TRAIN_DTYPE=fp16` is a *compatibility* switch for pre-Ampere cards
  (GTX 16xx, RTX 20xx) that only emulate bf16 — the trainer already picks it
  automatically via `torch.cuda.is_bf16_supported()`. The dtype that *is* a
  memory decision is **fp32**: 9.6 GB instead of 4.8 GB. fp32 is the default on
  plain CPU only.
- **Shortening `max_length` truncates the END of a sample, which is the label.**
  Samples run ~770 tokens at the median, and the assistant tool call is the last
  thing in them. Cutting to 512 trains on some prefixes with no answer attached.
  Try checkpointing, batch size, and closing other GPU users before this one.

Every knob: `STUDIO_TRAIN_DTYPE`, `STUDIO_TRAIN_DEVICE`, `STUDIO_TRAIN_MAX_LENGTH`,
`STUDIO_TRAIN_MAX_PROMPT_LENGTH` (DPO), `STUDIO_TRAIN_BATCH_SIZE`,
`STUDIO_TRAIN_GRAD_ACCUM`, `STUDIO_TRAIN_GRAD_CHECKPOINT`.

## 7. How long a round takes, and what governs it

One round is:

```
optimizer steps  =  ceil(samples / batch_size) x epochs / grad_accum
wall clock       =  micro-batches x (time for one forward+backward at max_length)
```

**Worked example, with the numbers we actually have.** 200 samples, batch 1,
grad-accum 8, 1 epoch → **200 micro-batches → 25 optimizer steps**. The progress
bar counts *optimizer steps*, so it moves 25 times, once per 8 forward/backward
passes. That is why it appears frozen for stretches: it is accumulating, not
hanging. The trainer prints this arithmetic before training starts.

### The timings we actually have (all on an Apple M1 / 16 GB — not your card)

Two complete rounds were run on this repo, MPS backend, bf16, batch 1, 4 samples,
`max_length=128`:

| Run | `train_runtime` | Per micro-batch | Final loss |
|---|---|---|---|
| gradient checkpointing **off** | 681 s | **~170 s** | 4.887 |
| gradient checkpointing **on** | 990 s | **~247 s** (+45%) | 4.892 |

Two things to take from that, and one warning:

1. **Gradient checkpointing costs about 45% wall clock here** (CUDA is usually
   quoted at 20–40%) and changes the result essentially not at all — the two
   losses land within 0.005 of each other (4.887 vs 4.892), i.e. it preserves
   the gradients it recomputes. It buys memory, nothing else, which
   is why the trainer turns it on for CUDA and leaves it off on CPU/MPS.
2. **Those per-micro-batch numbers are terrible, and they are the honest ones.**
   An older note in the source recorded *~4.5 s for a 512-token micro-batch* on
   the same M1. That figure does not reproduce inside a real SFTTrainer round:
   what was measured here is ~40× slower at a quarter of the sequence length.
   The most likely explanation is the online `autobitlinear` re-quantization
   described in §2 — the thing transformers warns about when there is no GPU.
3. **Do not extrapolate either number to your NVIDIA card.** They bound nothing
   about CUDA; they are evidence that the *CPU/MPS* path is a fallback, not a
   plan. If your first CUDA round is minutes rather than hours, that is the
   expected outcome and it will be the first real datapoint anyone has.

The trainer prints elapsed time and **peak CUDA memory** every round, so your
first run replaces this whole section with facts about your own machine.

The **first** run also pays a one-time 4.83 GB download (§9).

## 8. What a good run looks like

Illustrative: the lines are the ones the trainer really prints (in this order —
the MPS half of this was captured from a real round), with CUDA-side values
filled in for a card nobody here has.

```
[trainer] source context: 2 source(s) from /api/skills {'demo': 'sqlite', 'wh': 'databricks'}
[trainer] SFT: pulled 412 rollouts since 0.000 -> 200 usable samples (reward >= 0.6) across 2 source(s)
[trainer] SFT per-source samples: {'demo': {'n': 160, 'avg_reward': 0.84}, 'wh': {'n': 40, 'avg_reward': 0.79}}
[trainer] dropped rollouts: {'no_source': 12, 'stale_tables': 3}
[trainer] SFT-training LoRA on 200 samples (base=microsoft/bitnet-b1.58-2B-4T-bf16) …
[trainer] base model microsoft/bitnet-b1.58-2B-4T-bf16: already in the HF cache (...) — no download expected.
[trainer] device=cuda dtype=bfloat16
[trainer] CUDA device: NVIDIA GeForce RTX 4060 Laptop GPU (compute 8.9)
[trainer] CUDA memory: 8.0 GB total; weights alone need ~4.8 GB in bfloat16 ...
[trainer] 8 GB class card: the defaults are sized for exactly this ...
[trainer] plan: 200 samples x 1 epoch(s) = 200 micro-batches of 1; grad_accum=8 -> 25 optimizer steps
[trainer] the progress bar counts OPTIMIZER steps, so it advances once per 8 forward/backward passes ...
{'loss': 1.71, 'grad_norm': 0.9, 'learning_rate': 0.00019, 'epoch': 0.04}
...
[trainer] peak CUDA memory this round: 6.41 GB (max_length=1024, batch=1, grad_checkpoint=True)
[trainer] adapter written to C:\studio\adapters\tool_call-1723890000
[trainer] published global/tool_call v3 <- /mnt/c/studio/adapters/tool_call-1723890000  metrics={...}
```

Read it for: **device=cuda** (not cpu), a **loss that decreases** across the
logged steps, **peak memory below total**, a **per-source** breakdown that isn't
one source starved, and a **published version number** that increments each
round. `dropped` counts are normal — they are rollouts whose source or tables no
longer exist, which must not be trained on.

## 9. Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `[trainer] device=cpu` on an NVIDIA laptop | CPU-only torch wheel | §3 step 1, then re-check `torch.version.cuda` |
| `OUT OF MEMORY while training` | Card too small for the current settings | The message lists every lever in the order worth trying. §6 |
| A silent pause of many minutes on the **first** run | The 4.83 GB weights download | The trainer announces it first: size, destination, free space. Re-runs are cached; `HF_HUB_OFFLINE=1` guarantees no network. Move the cache to a bigger drive with `HF_HOME=D:\hf-cache`. Ctrl-C is safe — it resumes. |
| The progress bar sits still for a minute | grad-accum 8: one tick = 8 passes | Not a hang. §7. `STUDIO_TRAIN_GRAD_ACCUM=4` makes it tick twice as often (and halves the effective batch). |
| `not enough new experience` / 0 samples | No rollouts yet | §4, `bootstrap_rollouts.py` |
| DPO mines **0 pairs** from a bootstrap corpus | Every bootstrap rollout carries the same reward, and a preference pair needs two outcomes for one prompt with a reward *gap* | Expected. Bootstrap gets you an SFT adapter; DPO waits for real traffic. |
| `quantization method do not support training` | `STUDIO_TRAIN_BASE_MODEL` points at the packed 1-bit repo | Use the `-bf16` repo. §2 |
| Serving never picks the adapter up | The published URI is not the path the server opens | §5. The serving gateway deliberately refuses to claim an adapter it cannot prove is mounted, so this fails loudly rather than silently serving the base model. |
| `missing: torch` / `peft` | ML stack not installed | `pip install -r scripts\requirements-trainer.txt`. `--dry-run` and `--status` never need it. |
| HF symlink warning on Windows | Cache dedupe wants symlinks | Harmless. Silence with `HF_HUB_DISABLE_SYMLINKS_WARNING=1`, or enable Developer Mode. |
| `UnicodeEncodeError` in a redirected log | Console/locale encoding | Already handled (stdout is reconfigured to UTF-8 with replacement); `PYTHONUTF8=1` if anything else in the pipeline complains. |

---

## 10. What is measured and what is not

Stated plainly, because the difference matters when you are budgeting a laptop:

**Measured**
- 4,825,679,400 bytes for the bf16 master-weights blob (from a real HF cache).
- ~770-token median training sample from the bootstrap corpus.
- 200 samples at batch 1 / grad-accum 8 = 25 optimizer steps (arithmetic, and
  pinned by `backend/tests/test_train_online.py`).
- Two full SFT rounds on an **Apple M1 / 16 GB, MPS, bf16** (4 samples,
  `max_length=128`, batch 1): **681 s** without gradient checkpointing and
  **990 s** with it (+45%), losses 4.887 vs 4.892 — so checkpointing costs time,
  preserves the result, and saves memory. An older source note claiming ~4.5 s
  per 512-token micro-batch on that machine **did not reproduce**; see §7.
- The `-bf16` repo declares an online `autobitlinear` bitnet
  `quantization_config` (read from the real `config.json`), and transformers
  warns about slow weight unpacking without a GPU.
- The LoRA that comes out is a real adapter: `adapter_model.safetensors` +
  `adapter_config.json`, saved and re-loadable.
- The bootstrap corpus shape: 486 candidates → 324 verified rollouts, 6 distinct
  SQL shapes.

**Not measured — reasoned from the above**
- Everything about **CUDA and Windows**: the VRAM table, per-step timing on any
  NVIDIA card, the CUDA install commands, and the claim that gradient
  checkpointing is what makes 8 GB fit. This work was done on an Apple M1 with
  no CUDA, no Windows and no Docker daemon.
  The memory arithmetic (4.8 GB weights + activations + a vocab-sized logits
  tensor, all linear in `max_length`) is sound and the guidance follows from it,
  but no one has run this on an NVIDIA card yet. The trainer prints **peak CUDA
  memory** after every round precisely so your first run replaces these
  estimates with facts. If the numbers differ, trust yours.
