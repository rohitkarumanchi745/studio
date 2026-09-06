#!/usr/bin/env python3
"""Online (simultaneous) BitNet trainer — the LEARNING half of the loop.

Studio's API (app/trainer.py) is the PRODUCER + adapter registry:
    Studio agent ──rollouts──▶  THIS worker (GPU)  ──adapters──▶  Studio serving (CPU)
This script is the CONSUMER that app/trainer.py's docstring references. It:

  1. polls  GET  /training/rollouts?since=<cursor>   (reward-labeled rollouts)
  2. formats successful trajectories into tool-calling SFT samples
  3. trains a small LoRA adapter on BitNet's bf16 master weights (a GPU job —
     CPU/MPS completes but is slower by more than an order of magnitude; see
     HARDWARE and scripts/README-training.md §7)
  4. publishes it  POST /training/adapters   so serving hot-swaps to it,
  then loops, so training and serving run at the same time.

HARDWARE — the split, because it is the opposite of the intuitive one
- TRAINING (this script) is a GPU job. It fine-tunes
  microsoft/bitnet-b1.58-2B-4T-bf16, ~4.8 GB of ordinary bf16 master weights;
  the packed 1-bit repo cannot be fine-tuned at all (see BASE_MODEL). A LoRA
  round on an 8 GB NVIDIA card is minutes; the same round on CPU is hours.
- SERVING BitNet is a CPU job. Stock vLLM cannot load BitNet at all
  (vllm#17279, "not planned"); the supported runtime is microsoft/BitNet
  (bitnet.cpp), a llama.cpp fork with ternary CPU kernels. The GPU does not
  help there — 1-bit inference on CPU is the whole design goal. See
  serving/README.md §1.
- So one laptop can do both: train on the GPU, serve on the CPU cores.
  Windows + NVIDIA setup, VRAM guidance and failure modes:
  scripts/README-training.md.

DESIGN NOTES
- Runs OUTSIDE the lean API image (heavy ML deps in requirements-trainer.txt).
  It talks to Studio over HTTP as an admin service account — nothing here is
  imported by the app.
- The PLUMBING (auth, poll, cursor, format, publish) is pure stdlib, so
  `--dry-run` runs with zero ML deps and is fully testable against a live API.
  The TRAINING step imports torch/transformers/peft/trl lazily and prints a
  clear install hint if they're absent — so the loop degrades, never crashes.
- Two objectives (STUDIO_TRAIN_MODE): 'sft' = reward-FILTERED supervised fine-
  tuning (the reward selects which trajectories to imitate — a safe baseline,
  not RL); 'dpo' = Direct Preference Optimization = genuine preference-based RL —
  same-prompt outcomes are turned into (chosen > rejected) pairs by the reward,
  and DPO puts that preference in the objective. GRPO/PPO are the next step.
- SOURCE-CONDITIONED (train == serve): rollouts come from DIFFERENT warehouses
  (Databricks/Snowflake/BigQuery/S3/demo), each a distinct dialect + schema. Every
  training sample is conditioned on ITS source's skill file — the same schema+
  dialect briefing the live agent runs on (fetched once per round from GET
  /api/skills). SYSTEM = the source's skill/schema/dialect context, USER = the
  question, ASSISTANT = the SQL tool call. DPO pairs are mined WITHIN one source
  only: a Databricks `date_trunc(...)` "chosen" must never be preferred over a
  sqlite `strftime(...)` "rejected" — that manufactures a false cross-dialect
  preference and corrupts the demo policy. Source-blind training is the bug.
- CHANGING DATA is handled by design: the label is action.sql — a QUERY string
  that serving RE-EXECUTES live (qcache re-runs the stored SQL fresh). The adapter
  learns "given this schema+dialect and this question, emit this SQL", never the
  rows, so row churn between rounds needs no retraining. CHANGING SCHEMA is handled
  by re-fetching /api/skills each round (a schema change flips the source's skill,
  so the round's context is always current) and DROPPING rollouts whose referenced
  tables are no longer in the source's allowed set (stale — can't be re-executed).

CONFIG (env, all optional except credentials):
  STUDIO_API_URL            base URL of the Studio API      (default http://localhost:8000)
  STUDIO_TRAINER_TOKEN      admin JWT (skips login), OR
  STUDIO_TRAINER_EMAIL/PASSWORD   admin service-account login
  STUDIO_TRAIN_BASE_MODEL   HF id of the TRAINABLE base  (default microsoft/bitnet-b1.58-2B-4T-bf16;
                            the packed 1-bit repo cannot be fine-tuned — see BASE_MODEL)
  STUDIO_TRAIN_OUTPUT_DIR   where adapters + cursor live (default ./adapters)
  STUDIO_TRAIN_MIN_REWARD   keep rollouts with reward >= (default 0.6 — the "learned" band)
  STUDIO_TRAIN_MIN_NEW      min new usable samples before an SFT round (default 32)
  STUDIO_TRAIN_POLL_SECONDS loop sleep between polls      (default 60)
  STUDIO_TRAIN_EPOCHS       epochs per round              (default 1)
  STUDIO_TRAIN_DEVICE       'cuda' | 'mps' | 'cpu'        (default: auto-detect)
  STUDIO_TRAIN_DTYPE        'bf16' | 'fp16' | 'fp32'      (default: per device — see
                            _device_and_dtype; fp16 is a COMPATIBILITY switch for
                            pre-Ampere cards, NOT a memory saving over bf16)
  STUDIO_TRAIN_MAX_LENGTH   tokens per training sample    (default 1024 — the biggest
                            VRAM lever after the weights; truncation drops the END of
                            a sample, which is the tool-call label, so shorten with care)
  STUDIO_TRAIN_MAX_PROMPT_LENGTH  DPO prompt budget       (default MAX_LENGTH // 2)
  STUDIO_TRAIN_BATCH_SIZE   micro-batch size per device   (default 1)
  STUDIO_TRAIN_GRAD_ACCUM   micro-batches per optimizer step (default 8 — one visible
                            progress tick costs this many forward/backward passes)
  STUDIO_TRAIN_GRAD_CHECKPOINT  'auto' (on for cuda) | '1' | '0' — trades wall clock
                            (+45% measured on MPS) for most of the activation memory
  STUDIO_TRAIN_MODE         'sft' (default) | 'dpo' (preference-based RL)
  STUDIO_TRAIN_PAIR_MARGIN  DPO: min reward gap for a chosen/rejected pair (default 0.15)
  STUDIO_TRAIN_MIN_PAIRS    DPO: min preference pairs before a round (default 16)
  STUDIO_TRAIN_DPO_BETA     DPO: KL strength toward the reference (default 0.1)
  STUDIO_TRAIN_ADAPTER_BASE_URI   uri prefix serving loads adapters from (default = output dir)
  STUDIO_SERVE_URL          serving side to prime after publish (the gateway, or vLLM);
                            unset = push disabled (serving still picks it up next call)
  STUDIO_SERVE_KIND         'gateway' (default, POST /admin/load_adapter) | 'vllm'
  STUDIO_SERVE_TOKEN        optional bearer token if the serving side requires auth

USAGE
  python train_online.py --once        # one round then exit
  python train_online.py --dry-run     # pull + format only (no ML deps, no publish)
  python train_online.py               # continuous online loop (SFT)
  STUDIO_TRAIN_MODE=dpo python train_online.py --dry-run   # mine preference pairs, no ML deps
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

API = os.getenv("STUDIO_API_URL", "http://localhost:8000").rstrip("/")
OUT_DIR = os.getenv("STUDIO_TRAIN_OUTPUT_DIR", "./adapters")
# The -bf16 MASTER-WEIGHTS repo, not the packed 1-bit one. `bitnet-b1.58-2B-4T`
# ships the quantized inference artifact, and transformers refuses to fine-tune
# it outright: "The model you are trying to fine-tune is quantized with
# QuantizationMethod.BITNET but that quantization method do not support
# training." Training happens on bf16 masters; the i2_s GGUF the serving box
# runs is the quantization OF those same weights, so the LoRA composes at
# inference. Serving is unaffected by this value.
BASE_MODEL = os.getenv("STUDIO_TRAIN_BASE_MODEL", "microsoft/bitnet-b1.58-2B-4T-bf16")
MIN_REWARD = float(os.getenv("STUDIO_TRAIN_MIN_REWARD", "0.6"))
MIN_NEW = int(os.getenv("STUDIO_TRAIN_MIN_NEW", "32"))
POLL_SECONDS = int(os.getenv("STUDIO_TRAIN_POLL_SECONDS", "60"))
EPOCHS = int(os.getenv("STUDIO_TRAIN_EPOCHS", "1"))
# Objective: 'sft' = reward-FILTERED supervised fine-tuning (train on the good
# trajectories); 'dpo' = Direct Preference Optimization = genuine preference-based
# RL (the reward decides chosen>rejected and shapes the objective, not just the
# data filter). DPO needs same-prompt pairs with a reward gap >= PAIR_MARGIN.
MODE = os.getenv("STUDIO_TRAIN_MODE", "sft").strip().lower()
PAIR_MARGIN = float(os.getenv("STUDIO_TRAIN_PAIR_MARGIN", "0.15"))
MIN_PAIRS = int(os.getenv("STUDIO_TRAIN_MIN_PAIRS", "16"))
DPO_BETA = float(os.getenv("STUDIO_TRAIN_DPO_BETA", "0.1"))
CURSOR_FILE = os.path.join(OUT_DIR, ".train_cursor.json")

# ── Hardware knobs (see HARDWARE in the module docstring) ────────────────
# Every one of these exists because a laptop GPU has a hard VRAM ceiling and the
# operator needs a lever they can turn WITHOUT editing this file. The defaults
# are SIZED for an 8 GB card from the memory arithmetic (4.8 GB of weights +
# activations + a vocab-sized logits tensor, the last two linear in max_length);
# nobody has run this on an NVIDIA card yet, which is why every round prints the
# peak memory it actually used. See scripts/README-training.md §10.
# Tokens per sample. The largest memory term after the weights: activations AND
# the vocab-sized logits tensor both scale with it. Truncation removes the TAIL
# of a sample — and the tail is the assistant tool call, i.e. the label — so
# cutting this below the corpus's median sample length quietly trains on
# label-less prefixes. Bootstrap corpus median is ~770 tokens (see
# scripts/README-training.md), which is why 1024 is the default and 512 is a
# last resort rather than a free win.
MAX_LENGTH = int(os.getenv("STUDIO_TRAIN_MAX_LENGTH", "1024"))
MAX_PROMPT_LENGTH = int(os.getenv("STUDIO_TRAIN_MAX_PROMPT_LENGTH",
                                  str(max(64, MAX_LENGTH // 2))))
BATCH_SIZE = int(os.getenv("STUDIO_TRAIN_BATCH_SIZE", "1"))
GRAD_ACCUM = int(os.getenv("STUDIO_TRAIN_GRAD_ACCUM", "8"))
GRAD_CHECKPOINT = os.getenv("STUDIO_TRAIN_GRAD_CHECKPOINT", "auto").strip().lower()

# Windows consoles and redirected logs default to the locale encoding (cp1252 /
# cp932 / …), and this script prints '…' and '—'. A UnicodeEncodeError from a
# print in the middle of a training round would kill a run that was otherwise
# fine, so make stdout/stderr lossy instead of fatal.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ── API client (stdlib only) ─────────────────────────────────────────────

def _req(method, path, token=None, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise SystemExit(f"[trainer] {method} {path} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"[trainer] cannot reach {url}: {e.reason}")


def login():
    """Admin token from env, else a service-account login."""
    tok = os.getenv("STUDIO_TRAINER_TOKEN", "").strip()
    if tok:
        return tok
    email = os.getenv("STUDIO_TRAINER_EMAIL", "").strip()
    password = os.getenv("STUDIO_TRAINER_PASSWORD", "").strip()
    if not email or not password:
        raise SystemExit("[trainer] set STUDIO_TRAINER_TOKEN, or STUDIO_TRAINER_EMAIL + "
                         "STUDIO_TRAINER_PASSWORD (an admin service account).")
    res = _req("POST", "/api/auth/login", body={"email": email, "password": password})
    tok = res.get("access_token")
    if not tok:
        raise SystemExit("[trainer] login returned no access_token")
    return tok


def pull_rollouts(token, since, limit=2000):
    return _req("GET", f"/api/training/rollouts?since={since}&limit={limit}", token=token)


# ── Per-source schema+dialect context (train == serve) ───────────────────
# Every rollout is conditioned on ITS source's skill file — the same RBAC-scoped
# schema+dialect briefing the live agent runs on (app/agent.py wraps it exactly as
# below). Fetched once per round from GET /api/skills so the context is always the
# CURRENT schema: a schema change flips the source's skill, so drift is handled by
# re-fetching, and rollouts that reference now-gone tables are dropped (§stale).
# Pure stdlib — the --dry-run path needs no ML deps.

# The per-source skill file — the SUBSTANTIVE conditioning the serving agent
# uses (schema + dialect + allowed tables), wrapped as app/agent.py::_system_prompt
# wraps it. NOTE (train≈serve, not ==): the live system prompt ALSO carries the
# persona line, scope, memory notes and Rules block, and — because /api/skills is
# fetched by the admin trainer — the ADMIN (superset) table set, not each
# rollout's own role. So the schema/dialect match; the surrounding wrapper and
# per-role table scoping are a known, guard-covered gap (a follow-up).
def _skill_context(skill_md):
    return f"Your skill file for this database:\n\n{skill_md}"


# Copy of app/queryguard.TABLE_REF (the script runs outside the API image, so it
# cannot import app.*). Captures from/join targets incl. schema-qualified names;
# `.split('.')[-1]` then takes the bare table, matching how allowed_tables key.
TABLE_REF = re.compile(
    r"\b(?:from|join)\b(?:\s|/\*[^*]*(?:\*(?!/)[^*]*)*\*/)*[\"`\[]?"
    r"([a-zA-Z_][\w$-]*(?:[\"`\]]?\.[\"`\[]?[a-zA-Z_][\w$-]*)*)",
    re.IGNORECASE,
)


# CTE names bound by `WITH x AS (...)` / `, y AS (...)` — they are query-local,
# not real tables, so the stale-drop must not treat them as removed tables.
_CTE_RE = re.compile(r"(?:\bwith\b|,)\s+[\"`\[]?([a-zA-Z_]\w*)[\"`\]]?\s+as\s*\(", re.IGNORECASE)


def _referenced_tables(sql):
    """Bare table names a SQL statement reads from, EXCLUDING CTE bindings —
    normalized as app/router.py:52 and app/governance.py:183 do. Dropping CTE
    names avoids false-positive stale drops on valid `WITH ... SELECT FROM cte`."""
    sql = sql or ""
    cte = {m.lower() for m in _CTE_RE.findall(sql)}
    return {r.strip('"').split(".")[-1].lower() for r in TABLE_REF.findall(sql)} - cte


def fetch_skills(token):
    """GET /api/skills once per round -> {source: {context, allowed, dialect}}.
    The trainer runs as the admin service account, so it sees every configured
    source. `context` is the verbatim skill file wrapped as serving wraps it;
    `allowed` is the source's CURRENT allowed-table set (for the stale-drop)."""
    res = _req("GET", "/api/skills", token=token)
    out = {}
    for s in res.get("skills", []):
        src = s.get("source")
        if not src:
            continue
        out[src] = {
            "context": _skill_context(s.get("skill") or ""),
            "allowed": {str(t).lower() for t in (s.get("tables") or [])},
            "dialect": s.get("dialect"),
        }
    return out


def _condition(r, skills, stale):
    """Resolve a rollout's source context, or a reason to drop it. Returns
    (source, context) to keep, or (None, reason) to drop. `stale` accumulates
    per-reason drop counts. Enforces the non-negotiable: a rollout with no
    current source context is never used (it would teach a vanished policy)."""
    src = r.get("source")
    if not src:
        stale["no_source"] += 1          # legacy pre-column trace / source-blind
        return None, "no_source"
    ctx = skills.get(src)
    if ctx is None:
        stale["source_gone"] += 1        # deconfigured / renamed / RBAC-revoked
        return None, "source_gone"
    refs = _referenced_tables((r.get("action") or {}).get("sql"))
    if refs and not refs.issubset(ctx["allowed"]):
        stale["stale_tables"] += 1       # references a table no longer allowed
        return None, "stale_tables"
    return src, ctx["context"]


def publish_adapter(token, scope, kind, uri, base_model, metrics):
    return _req("POST", "/api/training/adapters", token=token, body={
        "scope": scope, "kind": kind, "uri": uri,
        "base_model": base_model, "metrics": metrics})


def get_status(token):
    return _req("GET", "/api/training/online", token=token)


# ── Serving push: tell the serving side to load the new adapter ──────────
# After publishing to the registry, optionally poke the serving box so the NEXT
# call serves the fresh adapter without waiting for a natural cache miss. Gated on
# STUDIO_SERVE_URL and fully FAIL-SAFE: a serving box that's down (or any error)
# is logged and ignored — it must NEVER break training. Pure stdlib.
#
#   STUDIO_SERVE_URL   base of the serving side (the gateway, or vLLM directly).
#                      unset → push disabled (registry publish alone is enough;
#                      serving picks the adapter up on its next call).
#   STUDIO_SERVE_KIND  'gateway' (default) → POST {URL}/admin/load_adapter
#                      'vllm'              → POST {URL}/v1/load_lora_adapter
#   STUDIO_SERVE_TOKEN optional bearer token if the serving side requires auth.
SERVE_URL = os.getenv("STUDIO_SERVE_URL", "").strip().rstrip("/")
SERVE_KIND = os.getenv("STUDIO_SERVE_KIND", "gateway").strip().lower()
SERVE_TOKEN = os.getenv("STUDIO_SERVE_TOKEN", "").strip()


def _serve_post(path, body):
    """Best-effort POST to the serving side. Returns True on 2xx, else False —
    never raises, never SystemExits (unlike _req), so training is never blocked."""
    url = SERVE_URL + path
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if SERVE_TOKEN:
        headers["Authorization"] = "Bearer " + SERVE_TOKEN
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[trainer] serving push to {url} failed (ignored): {e}")
        return False


def push_to_serving(uri, kind, base_model, name=None):
    """Ask the serving side to load the adapter at `uri` NOW. No-op (returns
    None) when STUDIO_SERVE_URL is unset. Fail-safe."""
    if not SERVE_URL:
        return None
    if SERVE_KIND == "vllm":
        # Talk straight to vLLM's runtime-LoRA endpoint. lora_path == the uri the
        # server sees (shared adapter volume); lora_name derived from the uri.
        lora_name = name or _uri_basename(uri)   # already like tool_call-<ts>
        ok = _serve_post("/v1/load_lora_adapter",
                         {"lora_name": lora_name, "lora_path": uri})
    else:
        # Default: the gateway's admin hook — it maps uri → backend LoRA name and
        # loads (vLLM) or enables (llama). Idempotent.
        ok = _serve_post("/admin/load_adapter",
                         {"uri": uri, "kind": kind, "base_model": base_model})
    print(f"[trainer] serving push ({SERVE_KIND}) for {kind} {uri}: "
          f"{'ok' if ok else 'failed (adapter still served on next call)'}")
    return ok


# ── Cursor persistence (train only on new experience) ────────────────────

def load_cursor():
    try:
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return float(json.load(f).get("cursor", 0.0))
    except (OSError, ValueError):
        return 0.0


def save_cursor(cursor):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CURSOR_FILE, "w", encoding="utf-8") as f:
        json.dump({"cursor": cursor, "at": time.time()}, f)


# ── Rollouts -> tool-calling SFT samples ─────────────────────────────────

def _completion_for(action):
    """The tool-call target for a rollout's action (run_sql [+ render_chart]) as
    compact JSON — the label a tool-calling policy learns. None if no SQL."""
    sql = (action or {}).get("sql")
    sql = (sql or "").strip()
    if not sql:
        return None
    target = {"tool": "run_sql", "sql": sql}
    if action.get("chart_type"):
        target = [target, {"tool": "render_chart", "chart_type": action["chart_type"]}]
    return json.dumps(target, separators=(",", ":"))


def _norm_prompt(p):
    """Group key so the SAME question asked twice pairs up, regardless of casing
    or whitespace — DPO wants same-prompt / different-outcome pairs."""
    return re.sub(r"\s+", " ", (p or "").strip().lower())


def _ctx_key(history):
    """Stable digest of the conversation turns a rollout was conditioned on.
    Folded into the DPO group key so pairs never mix contexts: "and by region?"
    after a revenue question and after a downtime question are DIFFERENT prompts
    to the policy, and pairing them would teach nonsense preferences."""
    import hashlib
    if not history:
        return ""
    joined = "\x1e".join(f"{h.get('role','')}:{_norm_prompt(h.get('text',''))}"
                          for h in history)
    return hashlib.sha1(joined.encode()).hexdigest()[:12]


def to_samples(rollouts, skills=None):
    """SFT: keep successful, well-scored trajectories that took a real action,
    formatted as (source-context -> prompt -> tool-call) examples. The reward
    FILTERS the data; the source's skill file CONDITIONS each sample so the
    adapter learns dialect-correct, schema-grounded SQL per warehouse.

    Every kept sample carries `system` (the source's schema+dialect context) and
    `source`. Rollouts with no current source context, or that reference a table
    no longer in the source's allowed set (schema drift), are DROPPED — source-
    blind cross-dialect imitation is the bug this avoids. `skills` is the
    {source: {context, allowed, ...}} map from fetch_skills(); a defaultdict of
    stale-drop counters is returned alongside the samples for reporting."""
    from collections import defaultdict
    skills = skills or {}
    stale = defaultdict(int)
    samples = []
    for r in rollouts:
        reward = r.get("reward")
        prompt = (r.get("prompt") or "").strip()
        if reward is None or reward < MIN_REWARD or not prompt:
            continue
        if (r.get("mode") or "").startswith(("fallback", "error")):
            continue  # deterministic fallback / errors aren't policy to imitate
        completion = _completion_for(r.get("action"))
        if completion is None:
            continue
        src, ctx = _condition(r, skills, stale)
        if src is None:
            continue  # no current source context / stale schema -> not trainable
        samples.append({"system": ctx, "prompt": prompt, "completion": completion,
                        "reward": float(reward), "source": src,
                        # the turns the model actually saw — trained in the same
                        # positions serving puts them (train == serve, multi-turn)
                        "history": r.get("history") or []})
    return samples, stale


def mine_preference_pairs(rollouts, skills=None):
    """DPO: reward-labeled rollouts -> preference pairs, mined WITHIN one source.
    For each (source, prompt) seen with MORE THAN ONE distinct completion, pair its
    highest-reward completion (chosen) with each lower-reward one (rejected) when
    the reward gap clears PAIR_MARGIN.

    This is the genuine RL step: the heuristic reward now decides a PREFERENCE
    (chosen > rejected) that DPO puts in the objective — no human labels, and the
    reward is no longer just a data filter. The group key is (source, norm_prompt),
    so pairs NEVER cross warehouses: the same question asked of two dialects has two
    different correct answers, and pairing them would teach BitNet that one dialect's
    SQL is "better than" the other's — corrupting the losing source's policy. Each
    pair carries its source's schema+dialect `system` context. Returns (pairs, stale)
    where `stale` counts rollouts dropped for having no current source context."""
    from collections import defaultdict
    skills = skills or {}
    stale = defaultdict(int)
    # (source, norm-prompt) -> {completion: (best_reward, raw_prompt, context)}
    groups = defaultdict(dict)
    for r in rollouts:
        prompt = (r.get("prompt") or "").strip()
        reward = r.get("reward")
        if not prompt or reward is None:
            continue
        if (r.get("mode") or "").startswith(("fallback", "error")):
            continue
        completion = _completion_for(r.get("action"))
        if completion is None:
            continue
        src, ctx = _condition(r, skills, stale)
        if src is None:
            continue  # no current source context / stale schema -> unpaired
        hist = r.get("history") or []
        g = groups[(src, _ctx_key(hist), _norm_prompt(prompt))]
        if completion not in g or float(reward) > g[completion][0]:
            g[completion] = (float(reward), prompt, ctx, hist)  # best reward per distinct completion
    pairs = []
    for (src, _ck, _norm), by_comp in groups.items():
        if len(by_comp) < 2:
            continue
        ranked = sorted(((rw, pr, ctx, hist, comp) for comp, (rw, pr, ctx, hist) in by_comp.items()),
                        key=lambda x: x[0], reverse=True)
        best_rw, best_prompt, best_ctx, best_hist, best_comp = ranked[0]
        for rej_rw, _, _, _, rej_comp in ranked[1:]:
            if best_rw - rej_rw >= PAIR_MARGIN:
                pairs.append({"system": best_ctx, "prompt": best_prompt, "chosen": best_comp,
                              "rejected": rej_comp, "margin": round(best_rw - rej_rw, 3),
                              "source": src, "history": best_hist})
    return pairs, stale


def write_samples_jsonl(samples, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # encoding is explicit: a skill file with a non-ASCII column comment would
    # raise UnicodeEncodeError under Windows' cp1252 default.
    with open(path, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")
    return path


# ── Adapter URIs: the path the SERVER will read, not the path we wrote ───
# The trainer writes with the LOCAL os.sep; the uri it publishes is consumed by
# the SERVING box, which may be a different OS (the supported shape is a Windows
# trainer + a WSL/Linux or container server sharing one directory). os.path.join
# would splice a Windows backslash into a POSIX server path — '/adapters' +
# 'tool_call-1' becomes '/adapters\tool_call-1', which the server cannot open —
# so the separator follows the BASE's own style, never the trainer's platform.


def _uri_sep(base):
    """The separator `base` is already written in. URLs and POSIX paths use '/';
    only a Windows-style base (drive letter or UNC, written with backslashes)
    gets '\\'."""
    if "://" in base:
        return "/"
    if re.match(r"^[A-Za-z]:[\\/]", base) or base.startswith("\\\\"):
        return "\\" if "\\" in base else "/"
    return "/"


def _uri_join(base, name):
    """Join an adapter directory name onto the published base uri, preserving the
    base's separator style so the uri stays valid on the SERVER's filesystem."""
    base = (base or "").rstrip("/\\")
    if not base:
        return name
    return base + _uri_sep(base) + name


def _uri_basename(uri):
    """Last path segment of a uri written with EITHER separator (the vLLM push
    derives a lora_name from it, and os.path.basename only splits on os.sep — on
    Linux it would return the whole 'C:\\adapters\\tool_call-1' string)."""
    return re.split(r"[\\/]", (uri or "").rstrip("/\\"))[-1]


def _cross_os_uri_hint(uri):
    """A Windows-path uri that a Linux/WSL server cannot open under that name.

    The default base uri is this machine's absolute output dir, which is right
    when trainer and server share a filesystem and WRONG the moment the server
    lives in WSL or a container: 'C:\\studio\\adapters' is '/mnt/c/studio/adapters'
    over there. Returns an advisory string, or None when the uri is already
    POSIX/URL-shaped."""
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", uri or "")
    if not m:
        return None
    drive, rest = m.group(1).lower(), m.group(2).replace("\\", "/")
    return ("[trainer] note: this uri is a WINDOWS path. If the serving box is "
            "WSL, Linux or a container it reads that directory under a different "
            f"name (WSL: /mnt/{drive}/{rest}). The published uri must be the path "
            "the SERVER opens — set STUDIO_TRAIN_ADAPTER_BASE_URI to it "
            f"(e.g. STUDIO_TRAIN_ADAPTER_BASE_URI=/mnt/{drive}/{rest.rsplit('/', 1)[0] if '/' in rest else rest}). "
            "Same-machine Windows serving needs no change.")


# ── LoRA training (real; heavy deps imported lazily) ─────────────────────

# ── The model download: 4.8 GB, once, and silent until it isn't ──────────
# from_pretrained() blocks with no output until the HTTP transfer actually
# begins, so a DNS stall, a proxy or a full disk look exactly like a hang. Say
# what is about to be fetched and where BEFORE handing control to transformers.

# Approximate download sizes so the operator can tell a stall from a big file.
_MODEL_SIZE_GB = {
    "microsoft/bitnet-b1.58-2B-4T-bf16": 4.8,   # bf16 master weights (trainable)
    "microsoft/bitnet-b1.58-2B-4T": 1.2,        # packed 1-bit — NOT trainable
}


def _hf_cache_root():
    """Where huggingface_hub will put (or has put) the weights. Mirrors the
    hub's own precedence so the printed path is the real one."""
    for key in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        val = os.getenv(key, "").strip()
        if val:
            return val
    home = os.getenv("HF_HOME", "").strip()
    if home:
        return os.path.join(home, "hub")
    return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")


def _hf_cached(model_id, root=None):
    """True if the repo already has a cache directory (no download expected)."""
    root = root or _hf_cache_root()
    return os.path.isdir(os.path.join(root, "models--" + model_id.replace("/", "--")))


def announce_model_fetch(model_id):
    """Print what is about to be downloaded, where, and how to make the next run
    offline — before the call that blocks. Never raises; this is diagnostics."""
    try:
        if os.path.isdir(model_id):
            print(f"[trainer] base model: local directory {model_id} (no download)")
            return
        root = _hf_cache_root()
        if _hf_cached(model_id, root):
            print(f"[trainer] base model {model_id}: already in the HF cache "
                  f"({root}) — no download expected. HF_HUB_OFFLINE=1 makes that a "
                  f"guarantee (fails fast instead of hitting the network).")
            return
        size = _MODEL_SIZE_GB.get(model_id)
        size_txt = f"~{size:.1f} GB" if size else "the full repo"
        print(f"[trainer] base model {model_id} is NOT cached: downloading {size_txt} "
              f"from huggingface.co into {root}")
        print("[trainer] this is a FIRST-RUN cost (minutes on a home connection). "
              "There is no progress bar until the transfer starts, so a long silence "
              "here is the handshake, not training. Ctrl-C is safe — the download resumes.")
        try:
            # The cache dir does not exist yet on a first run, and disk_usage needs
            # a real path — walk up to the nearest ancestor that exists.
            probe = os.path.abspath(root)
            while probe and not os.path.isdir(probe):
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            free = shutil.disk_usage(probe).free / 1e9
            need = (size or 5.0) * 1.2
            print(f"[trainer] free space on the cache volume: {free:.1f} GB "
                  f"(need ~{need:.1f} GB)")
            if free < need:
                print("[trainer] WARNING: that is not enough room — the download will "
                      "fail partway. Move the cache with HF_HOME=D:\\hf-cache (or any "
                      "drive with space) and re-run.")
        except OSError:
            pass
        print("[trainer] after this run, HF_HUB_OFFLINE=1 skips the hub entirely.")
    except Exception as e:                      # never let diagnostics break a round
        print(f"[trainer] (could not inspect the HF cache: {e})")


# ── VRAM: fit the round, or say exactly what to turn down ────────────────

def _vram_advice(total_gb, dtype_name, max_length=None, grad_ckpt=True):
    """Pre-flight verdict for a card of `total_gb`, as a list of lines.

    Weights alone are ~4.8 GB in bf16/fp16 and ~9.6 GB in fp32; on top of that
    sit the activations and a vocab-sized logits tensor, both linear in
    max_length. Pure function of the numbers so it is testable without a GPU."""
    max_length = MAX_LENGTH if max_length is None else max_length
    fp32 = dtype_name in ("float32", "fp32")
    weights = 9.6 if fp32 else 4.8
    headroom = total_gb - weights
    lines = [f"[trainer] CUDA memory: {total_gb:.1f} GB total; weights alone need "
             f"~{weights:.1f} GB in {dtype_name} (activations and a vocab-sized "
             f"logits tensor sit on top, both linear in max_length={max_length})"]
    if fp32:
        lines.append("[trainer] fp32 DOUBLES the weights to 9.6 GB. Set "
                     "STUDIO_TRAIN_DTYPE=bf16 (or fp16 on a pre-Ampere card): on a "
                     "laptop GPU that is the difference between fitting and not.")
    if headroom < 1.2:
        lines.append(f"[trainer] that leaves {headroom:.1f} GB for everything else — "
                     "too small for this base. Expect an OOM. Either train on CPU "
                     "(STUDIO_TRAIN_DEVICE=cpu — an overnight job, but it finishes and "
                     "publishes a real adapter) "
                     "or point STUDIO_TRAIN_BASE_MODEL at a smaller base.")
    elif total_gb < 7.5:
        lines.append("[trainer] 6 GB class card: tight but usually workable. Keep "
                     "STUDIO_TRAIN_GRAD_CHECKPOINT=1 and STUDIO_TRAIN_BATCH_SIZE=1, and "
                     "if it still OOMs STUDIO_TRAIN_MAX_LENGTH=512 (which truncates the "
                     "tail of long samples — the tool-call label — so close other GPU "
                     "users first: a browser or a game can hold 1-2 GB).")
    elif total_gb < 10:
        lines.append("[trainer] 8 GB class card: the defaults are sized for exactly this "
                     f"(batch {BATCH_SIZE} x grad-accum {GRAD_ACCUM}, max_length "
                     f"{max_length}, gradient checkpointing "
                     f"{'on' if grad_ckpt else 'OFF — turn it on if this OOMs'}).")
    else:
        lines.append("[trainer] comfortable: raise STUDIO_TRAIN_BATCH_SIZE (and lower "
                     "STUDIO_TRAIN_GRAD_ACCUM to keep the effective batch) for a faster "
                     "round.")
    return lines


def _hardware_preflight(device, dtype_name):
    """Print the memory verdict BEFORE loading 4.8 GB, so a too-small card is a
    sentence rather than a stack trace. Returns total GB when known."""
    if device != "cuda":
        if device == "cpu":
            print("[trainer] CPU training: correct, and SLOW on this particular base "
                  "— the bf16 repo re-quantizes every linear on each forward "
                  "(quantization_mode: online). The closest non-CUDA datapoint we "
                  "have is ~170s per 128-token micro-batch on an M1's MPS backend "
                  "(measured; plain CPU was not timed). A few-hundred-sample round at "
                  "max_length=1024 is an overnight job, not a coffee break. "
                  "Set STUDIO_TRAIN_DEVICE=cuda if this box has an "
                  "NVIDIA card (a torch built without CUDA is the usual reason it did "
                  "not auto-detect: python -c \"import torch;print(torch.version.cuda)\").")
        return None
    try:
        import torch
        props = torch.cuda.get_device_properties(0)
        total = props.total_memory / (1024 ** 3)
        print(f"[trainer] CUDA device: {props.name} (compute {props.major}.{props.minor})")
        for line in _vram_advice(total, dtype_name):
            print(line)
        return total
    except Exception as e:
        print(f"[trainer] (could not read CUDA properties: {e})")
        return None


def _prep_alloc_env():
    """Fragmentation is what usually OOMs an 8 GB card that arithmetically fits;
    expandable_segments is the allocator setting that fixes it. Must be set
    before the first CUDA allocation, and only if the operator has no opinion."""
    if not os.getenv("PYTORCH_CUDA_ALLOC_CONF"):
        os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"


def _is_oom(exc):
    """True for a CUDA/MPS/host out-of-memory failure, by type name and message
    so it works across torch versions (torch.cuda.OutOfMemoryError in 2.x,
    torch.OutOfMemoryError from 2.5) and without importing torch."""
    if "OutOfMemoryError" in type(exc).__name__:
        return True
    msg = str(exc).lower()
    return ("out of memory" in msg or "insufficient memory" in msg
            or "can't allocate memory" in msg)


def _oom_hint(stage, device, dtype_name):
    """The message a 6 GB card should get instead of a CUDA traceback: every
    lever, in the order worth trying, with the current value of each."""
    lines = [
        f"[trainer] OUT OF MEMORY while {stage} (device={device}, dtype={dtype_name}).",
        f"[trainer] current settings: max_length={MAX_LENGTH} batch={BATCH_SIZE} "
        f"grad_accum={GRAD_ACCUM} grad_checkpoint={GRAD_CHECKPOINT}",
        "[trainer] the 2.4B base is ~4.8 GB of weights in bf16/fp16 before a single "
        "activation, so the levers are, in order:",
    ]
    if dtype_name in ("float32", "fp32"):
        lines.append("  1. STUDIO_TRAIN_DTYPE=bf16   <- YOU ARE IN fp32: this halves the "
                     "weights 9.6 -> 4.8 GB. Use fp16 instead on a pre-Ampere card "
                     "(GTX 16xx / RTX 20xx).")
    else:
        lines.append("  1. STUDIO_TRAIN_DTYPE=fp16   (only if bf16 is unsupported — "
                     f"you are already at {dtype_name}, and fp16 is the SAME 2 bytes "
                     "per weight, a compatibility switch, not a memory saving)")
    lines += [
        "  2. STUDIO_TRAIN_GRAD_CHECKPOINT=1   (recompute activations instead of "
        "storing them: the biggest memory saving after the weights, and it costs "
        "time — +45% measured on M1/MPS, commonly quoted at 20-40% on CUDA)",
        f"  3. STUDIO_TRAIN_MAX_LENGTH=768 then 512   (now {MAX_LENGTH}; activations AND "
        "the vocab-sized logits scale with it. Truncation cuts the END of a sample, "
        "which is the tool-call label — bootstrap samples run ~770 tokens median, so "
        "512 does lose labels. Prefer the levers above.)",
        f"  4. STUDIO_TRAIN_BATCH_SIZE=1   (now {BATCH_SIZE}; raise STUDIO_TRAIN_GRAD_ACCUM "
        "to keep the same effective batch)",
        "  5. Close other GPU users (browser, game, another python) — `nvidia-smi` shows "
        "who holds the VRAM. On Windows the desktop compositor itself takes ~0.5-1 GB.",
        "  6. STUDIO_TRAIN_DEVICE=cpu   (last resort: very slow on this base — think "
        "overnight, not minutes — but it completes and publishes a real adapter)",
    ]
    lines.append("[trainer] nothing was published; the cursor did not move, so the same "
                 "rollouts are still there for the next attempt. The online loop retries "
                 "at the next poll — freeing VRAM (close the browser/game) is enough to "
                 "make that attempt succeed; if it dies identically every round, one of "
                 "the settings above has to change.")
    return "\n".join(lines)


def _guard_oom(stage, device, dtype_name, fn, *args, **kwargs):
    """Run `fn`, turning an out-of-memory failure into an actionable SystemExit
    instead of a CUDA traceback. Any other exception propagates untouched."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if not _is_oom(e):
            raise
        raise SystemExit(_oom_hint(stage, device, dtype_name)) from e


# ── The progress bar counts OPTIMIZER steps, not samples ─────────────────

def _step_plan(n_items, epochs, batch_size=None, grad_accum=None):
    """(micro_batches, optimizer_steps) for a round. With grad-accum 8 a single
    visible tick costs 8 forward/backward passes, which is what makes the bar
    look frozen."""
    batch_size = BATCH_SIZE if batch_size is None else batch_size
    grad_accum = GRAD_ACCUM if grad_accum is None else grad_accum
    micro = math.ceil(max(0, n_items) / max(1, batch_size)) * max(1, epochs)
    return micro, max(1, math.ceil(micro / max(1, grad_accum)))


def _log_every(steps):
    """A round of 25 steps logging every 10 prints twice. Log every step for
    short rounds so the operator sees a loss moving."""
    return 1 if steps <= 50 else 10


def _print_step_plan(n_items, unit, epochs):
    """Say the arithmetic out loud, and say that a frozen-looking bar is normal."""
    micro, steps = _step_plan(n_items, epochs)
    print(f"[trainer] plan: {n_items} {unit} x {epochs} epoch(s) = {micro} micro-batches "
          f"of {BATCH_SIZE}; grad_accum={GRAD_ACCUM} -> {steps} optimizer steps")
    print(f"[trainer] the progress bar counts OPTIMIZER steps, so it advances once per "
          f"{GRAD_ACCUM} forward/backward passes — expect it to sit still for "
          f"{GRAD_ACCUM} x (one micro-batch) between ticks. On CPU that is minutes per "
          f"tick and is NOT a hang; on an 8 GB GPU it is seconds.")
    return micro, steps


def _grad_checkpoint_on(device):
    """Default ON for CUDA (it is what buys the activation memory an 8 GB card
    does not have), off elsewhere — on CPU/MPS it only trades speed for memory
    nobody is short of, and it measured +45% wall clock on M1/MPS. An explicit
    STUDIO_TRAIN_GRAD_CHECKPOINT wins either way."""
    if GRAD_CHECKPOINT in ("1", "true", "yes", "on"):
        return True
    if GRAD_CHECKPOINT in ("0", "false", "no", "off"):
        return False
    return device == "cuda"


def _training_kwargs(device):
    """Trainer args shared by SFT and DPO: the memory/throughput settings that
    are env-configurable, plus gradient checkpointing when it is on."""
    kw = {"per_device_train_batch_size": BATCH_SIZE,
          "gradient_accumulation_steps": GRAD_ACCUM}
    if _grad_checkpoint_on(device):
        # use_reentrant=False is required for checkpointing to compose with LoRA
        # (the reentrant path loses the grad graph through frozen base layers).
        kw["gradient_checkpointing"] = True
        kw["gradient_checkpointing_kwargs"] = {"use_reentrant": False}
    return kw


def _load_base_model(base_model, dtype, device, grad_ckpt):
    """from_pretrained + placement, with the two settings gradient checkpointing
    needs on a LoRA run (no KV cache, and inputs that carry grad)."""
    from transformers import AutoModelForCausalLM
    model = AutoModelForCausalLM.from_pretrained(base_model, torch_dtype=dtype)
    if grad_ckpt:
        if getattr(model, "config", None) is not None:
            model.config.use_cache = False
        if hasattr(model, "enable_input_require_grads"):
            model.enable_input_require_grads()
    if device != "cpu":
        model = model.to(device)
    return model


def _report_peak(device):
    """The one number an operator actually wants after a round: how close the
    card came to the ceiling."""
    if device != "cuda":
        return None
    try:
        import torch
        peak = torch.cuda.max_memory_allocated() / (1024 ** 3)
        print(f"[trainer] peak CUDA memory this round: {peak:.2f} GB "
              f"(max_length={MAX_LENGTH}, batch={BATCH_SIZE}, "
              f"grad_checkpoint={_grad_checkpoint_on(device)})")
        return peak
    except Exception:
        return None


def _device_and_dtype():
    """Pick the accelerator and weight dtype the way the hardware wants.

    The trainer used to hardcode torch_dtype=torch.float32 and let the Trainer
    guess the device. On a 2.4B model that is 9.6 GB of weights, which on a
    16 GB laptop means swapping before the first optimizer step completes — one
    measured run sat 26 minutes without finishing step 1, and led to the wrong
    conclusion that fine-tuning needs a dedicated box.

    CAUTION on that earlier note: a bare 512-token micro-batch was timed at ~4.5s
    in bf16 on Apple's MPS backend (M1/16 GB), but a FULL SFTTrainer round on the
    same machine is far slower — measured 2026-09, 4 samples at max_length=128,
    batch 1: 681s of train_runtime (~170s per micro-batch), and 990s (~247s, +45%)
    with gradient checkpointing on. The likely reason is that this repo's config
    carries quantization_config {quant_method: bitnet, quantization_mode: online,
    linear_class: autobitlinear}, so every linear re-quantizes its weights each
    forward and transformers warns "You don't have a GPU available to load the
    model, the inference will be slow because of weight unpacking". Read that
    warning as the point of this whole section: the ternary path wants the GPU
    for TRAINING, even though it wants the CPU for serving.

    bf16 everywhere that supports it — MPS on Apple silicon, and CUDA from
    Ampere on — fp16 on older CUDA cards that only emulate bf16, and fp32 only
    on plain CPU, where bf16 is usually slower rather than faster.
    STUDIO_TRAIN_DEVICE / STUDIO_TRAIN_DTYPE override both for a box that knows
    better than this heuristic.
    """
    import torch
    dev = os.getenv("STUDIO_TRAIN_DEVICE", "").strip().lower()
    if not dev:
        if torch.cuda.is_available():
            dev = "cuda"
        elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            dev = "mps"
        else:
            dev = "cpu"
    want = os.getenv("STUDIO_TRAIN_DTYPE", "").strip().lower()
    if want in ("bf16", "bfloat16"):
        dtype = torch.bfloat16
    elif want in ("fp16", "float16"):
        dtype = torch.float16
    elif want in ("fp32", "float32"):
        dtype = torch.float32
    elif dev == "cpu":
        dtype = torch.float32            # bf16 on CPU is usually slower, not faster
    elif dev == "cuda":
        # bf16 needs Ampere or newer. On a GTX 16xx / RTX 20xx (compute < 8.0)
        # torch reports bf16 as "available" but emulates it, which is slower than
        # fp16 and can silently underflow — so ask whether it is actually
        # supported and fall back to fp16, which every CUDA GPU since Pascal
        # does in hardware.
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        dtype = torch.bfloat16           # mps: bf16 is the fast path
    return dev, dtype


def train_lora(samples, base_model, out_dir, epochs):
    """Reward-filtered SFT of a small LoRA adapter on BitNet's bf16 masters.

    This is the GPU half of the system (serving BitNet is the CPU half — see
    HARDWARE above): ~4.8 GB of weights plus activations, which fits an 8 GB
    card with the defaults here. CPU works and takes hours instead of minutes.
    Returns (adapter_dir, metrics). Raises a clear, actionable error if the ML
    stack isn't installed, and turns an OOM into instructions (_oom_hint)."""
    _prep_alloc_env()
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import SFTConfig, SFTTrainer
    except ImportError as e:
        raise SystemExit(
            "[trainer] training needs the ML stack — install it:\n"
            "    pip install -r scripts/requirements-trainer.txt\n"
            "  (a CPU-only torch works, but this base is punishing without a GPU;\n"
            "   install the CUDA build\n"
            "   FIRST if this box has an NVIDIA card — scripts/README-training.md §3)\n"
            f"  (missing: {e.name}). Use --dry-run to exercise the loop without it.")

    announce_model_fetch(base_model)
    tok = AutoTokenizer.from_pretrained(base_model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    # One chat-formatted text per sample: the source's schema+dialect context as
    # the SYSTEM message, the question, then the tool-call target — the exact
    # conditioning serving applies, so the adapter trains on what it's served under.
    def _fmt(s):
        msgs = []
        if s.get("system"):
            msgs.append({"role": "system", "content": s["system"]})
        for h in (s.get("history") or []):   # conversation turns, exactly as served
            msgs.append({"role": "user" if h.get("role") == "user" else "assistant",
                         "content": h.get("text") or ""})
        msgs += [{"role": "user", "content": s["prompt"]},
                 {"role": "assistant", "content": s["completion"]}]
        try:
            return tok.apply_chat_template(msgs, tokenize=False)
        except Exception:
            sys_prefix = f"<|system|>\n{s['system']}\n" if s.get("system") else ""
            hist = "".join(
                f"<|{'user' if h.get('role') == 'user' else 'assistant'}|>\n{h.get('text') or ''}\n"
                for h in (s.get("history") or []))
            return f"{sys_prefix}{hist}<|user|>\n{s['prompt']}\n<|assistant|>\n{s['completion']}{tok.eos_token}"

    ds = Dataset.from_dict({"text": [_fmt(s) for s in samples]})
    device, dtype = _device_and_dtype()
    dtype_name = str(dtype).split(".")[-1]
    print(f"[trainer] device={device} dtype={dtype_name}")
    _hardware_preflight(device, dtype_name)
    _, steps = _print_step_plan(len(samples), "samples", epochs)
    grad_ckpt = _grad_checkpoint_on(device)
    model = _guard_oom("loading the base model", device, dtype_name,
                       _load_base_model, base_model, dtype, device, grad_ckpt)
    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
                      task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    adapter_dir = os.path.join(out_dir, f"tool_call-{int(time.time())}")
    cfg = SFTConfig(output_dir=adapter_dir, num_train_epochs=epochs,
                    learning_rate=2e-4, logging_steps=_log_every(steps),
                    save_strategy="no", report_to=[], max_length=MAX_LENGTH,
                    **_training_kwargs(device))
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds, peft_config=lora)
    result = _guard_oom("training", device, dtype_name, trainer.train)
    _report_peak(device)
    trainer.save_model(adapter_dir)      # saves the LoRA adapter only
    tok.save_pretrained(adapter_dir)
    print(f"[trainer] adapter written to {adapter_dir}")
    metrics = {
        "loss": float(getattr(result, "training_loss", 0.0) or 0.0),
        "steps": int(getattr(result, "global_step", 0) or 0),
        "n_rollouts": len(samples),
        "avg_reward": round(sum(s["reward"] for s in samples) / max(1, len(samples)), 4),
        "epochs": epochs,
        "device": device, "dtype": dtype_name, "max_length": MAX_LENGTH,
    }
    return adapter_dir, metrics


def train_dpo(pairs, base_model, out_dir, epochs):
    """Direct Preference Optimization of a LoRA adapter — genuine preference-based
    RL. Optimizes the policy so the chosen (higher-reward) completion is preferred
    over the rejected one, relative to a frozen reference (the base with the LoRA
    disabled — no second model copy). CPU-feasible for a 1-bit base + small LoRA.
    Raises a clear, actionable error if the ML stack isn't installed. DPO holds
    a chosen AND a rejected sequence per example, so it wants MORE memory than
    SFT at the same max_length — an OOM here is turned into instructions."""
    _prep_alloc_env()
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import DPOConfig, DPOTrainer
    except ImportError as e:
        raise SystemExit(
            "[trainer] DPO needs the ML stack — install it:\n"
            "    pip install -r scripts/requirements-trainer.txt\n"
            "  (CUDA build first on an NVIDIA box — scripts/README-training.md §3)\n"
            f"  (missing: {e.name}). Use --dry-run to mine pairs without it.")

    announce_model_fetch(base_model)
    tok = AutoTokenizer.from_pretrained(base_model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def _prompt(p):
        # Same source-conditioning as SFT: the schema+dialect SYSTEM context leads,
        # so DPO's chosen>rejected preference is learned WITHIN that source's regime.
        msgs = []
        if p.get("system"):
            msgs.append({"role": "system", "content": p["system"]})
        for h in (p.get("history") or []):   # conversation turns, exactly as served
            msgs.append({"role": "user" if h.get("role") == "user" else "assistant",
                         "content": h.get("text") or ""})
        msgs.append({"role": "user", "content": p["prompt"]})
        try:
            return tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        except Exception:
            sys_prefix = f"<|system|>\n{p['system']}\n" if p.get("system") else ""
            hist = "".join(
                f"<|{'user' if h.get('role') == 'user' else 'assistant'}|>\n{h.get('text') or ''}\n"
                for h in (p.get("history") or []))
            return f"{sys_prefix}{hist}<|user|>\n{p['prompt']}\n<|assistant|>\n"

    ds = Dataset.from_dict({
        "prompt": [_prompt(p) for p in pairs],
        "chosen": [p["chosen"] for p in pairs],
        "rejected": [p["rejected"] for p in pairs]})
    device, dtype = _device_and_dtype()
    dtype_name = str(dtype).split(".")[-1]
    print(f"[trainer] device={device} dtype={dtype_name}")
    _hardware_preflight(device, dtype_name)
    _, steps = _print_step_plan(len(pairs), "pairs", epochs)
    grad_ckpt = _grad_checkpoint_on(device)
    model = _guard_oom("loading the base model", device, dtype_name,
                       _load_base_model, base_model, dtype, device, grad_ckpt)
    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
                      task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    adapter_dir = os.path.join(out_dir, f"tool_call-dpo-{int(time.time())}")
    cfg = DPOConfig(output_dir=adapter_dir, num_train_epochs=epochs,
                    learning_rate=5e-5, beta=DPO_BETA, logging_steps=_log_every(steps),
                    save_strategy="no", report_to=[], max_length=MAX_LENGTH,
                    max_prompt_length=MAX_PROMPT_LENGTH, **_training_kwargs(device))
    # ref_model=None + peft_config: the reference is the base with adapters
    # disabled, so DPO needs no second full-model copy.
    trainer = DPOTrainer(model=model, ref_model=None, args=cfg, train_dataset=ds,
                         processing_class=tok, peft_config=lora)
    result = _guard_oom("training", device, dtype_name, trainer.train)
    _report_peak(device)
    trainer.save_model(adapter_dir)
    tok.save_pretrained(adapter_dir)
    print(f"[trainer] adapter written to {adapter_dir}")
    metrics = {
        "method": "dpo", "beta": DPO_BETA,
        "loss": float(getattr(result, "training_loss", 0.0) or 0.0),
        "steps": int(getattr(result, "global_step", 0) or 0),
        "n_pairs": len(pairs),
        "avg_margin": round(sum(p["margin"] for p in pairs) / max(1, len(pairs)), 4),
        "epochs": epochs,
        "device": device, "dtype": dtype_name, "max_length": MAX_LENGTH,
    }
    return adapter_dir, metrics


# ── One training round + the online loop ─────────────────────────────────

def _per_source_samples(samples):
    """{source: {n, avg_reward}} for SFT samples — coverage per warehouse."""
    from collections import defaultdict
    agg = defaultdict(lambda: [0, 0.0])
    for s in samples:
        a = agg[s.get("source")]
        a[0] += 1
        a[1] += float(s.get("reward", 0.0))
    return {src: {"n": n, "avg_reward": round(tot / max(1, n), 4)}
            for src, (n, tot) in sorted(agg.items(), key=lambda kv: -kv[1][0])}


def _per_source_pairs(pairs):
    """{source: n_pairs} for DPO pairs — all within-source by construction."""
    from collections import defaultdict
    agg = defaultdict(int)
    for p in pairs:
        agg[p.get("source")] += 1
    return dict(sorted(agg.items(), key=lambda kv: -kv[1]))


def run_once(token, dry_run=False):
    since = load_cursor()
    pulled = pull_rollouts(token, since)
    rollouts, cursor = pulled["rollouts"], pulled["cursor"]
    # Re-fetch the CURRENT per-source schema+dialect context once per round. This
    # is the drift handler: a schema change flips the source's skill, so every
    # sample this round is conditioned on the up-to-date schema (§changing schema).
    skills = fetch_skills(token)
    print(f"[trainer] source context: {len(skills)} source(s) from /api/skills "
          f"{ {s: skills[s]['dialect'] for s in skills} }")

    # Prepare the objective's training data: SFT filters, DPO mines pairs — both
    # conditioned per source, both dropping rollouts with no current source context.
    if MODE == "dpo":
        pairs, stale = mine_preference_pairs(rollouts, skills)
        per_source = _per_source_pairs(pairs)
        print(f"[trainer] DPO: pulled {len(rollouts)} rollouts since {since:.3f} -> "
              f"{len(pairs)} preference pairs (within-source, reward gap >= {PAIR_MARGIN})")
        print(f"[trainer] DPO per-source pairs: {per_source}")
        print(f"[trainer] dropped rollouts: {dict(stale)} "
              f"(no_source=legacy/blind, source_gone=deconfigured, stale_tables=schema drift)")
        if len(pairs) < MIN_PAIRS:
            print(f"[trainer] {len(pairs)} < MIN_PAIRS={MIN_PAIRS} — not enough preference "
                  f"signal yet (needs same-source, same-prompt, reward-differing outcomes); accumulating.")
            save_cursor(cursor)
            return {"trained": False, "mode": "dpo", "pairs": len(pairs),
                    "per_source": per_source, "dropped": dict(stale), "cursor": cursor}
        data_path = write_samples_jsonl(pairs, os.path.join(OUT_DIR, "last_pairs.jsonl"))
        if dry_run:
            print(f"[trainer] --dry-run: wrote {len(pairs)} preference pairs to {data_path}; "
                  f"skipping training + publish.")
            save_cursor(cursor)
            return {"trained": False, "mode": "dpo", "dry_run": True, "pairs": len(pairs),
                    "per_source": per_source, "dropped": dict(stale),
                    "pairs_path": data_path, "cursor": cursor}
        print(f"[trainer] DPO-training LoRA on {len(pairs)} preference pairs (base={BASE_MODEL}) …")
        adapter_dir, metrics = train_dpo(pairs, BASE_MODEL, OUT_DIR, EPOCHS)
        metrics["per_source"] = per_source
        metrics["dropped"] = dict(stale)
    else:
        samples, stale = to_samples(rollouts, skills)
        per_source = _per_source_samples(samples)
        print(f"[trainer] SFT: pulled {len(rollouts)} rollouts since {since:.3f} -> "
              f"{len(samples)} usable samples (reward >= {MIN_REWARD}) across {len(per_source)} source(s)")
        print(f"[trainer] SFT per-source samples: {per_source}")
        print(f"[trainer] dropped rollouts: {dict(stale)} "
              f"(no_source=legacy/blind, source_gone=deconfigured, stale_tables=schema drift)")
        if len(samples) < MIN_NEW:
            print(f"[trainer] {len(samples)} < MIN_NEW={MIN_NEW} — not enough new experience; "
                  f"advancing cursor, will accumulate.")
            save_cursor(cursor)
            return {"trained": False, "mode": "sft", "samples": len(samples),
                    "per_source": per_source, "dropped": dict(stale), "cursor": cursor}
        data_path = write_samples_jsonl(samples, os.path.join(OUT_DIR, "last_samples.jsonl"))
        if dry_run:
            print(f"[trainer] --dry-run: wrote {len(samples)} samples to {data_path}; "
                  f"skipping training + publish.")
            save_cursor(cursor)
            return {"trained": False, "mode": "sft", "dry_run": True, "samples": len(samples),
                    "per_source": per_source, "dropped": dict(stale),
                    "samples_path": data_path, "cursor": cursor}
        print(f"[trainer] SFT-training LoRA on {len(samples)} samples (base={BASE_MODEL}) …")
        adapter_dir, metrics = train_lora(samples, BASE_MODEL, OUT_DIR, EPOCHS)
        metrics["per_source"] = per_source
        metrics["dropped"] = dict(stale)

    # Shared publish + serving-push tail (same registry contract for either objective).
    # The uri is what SERVING will open, so join it in the base's own separator
    # style (never os.path.join — see _uri_join) and flag the cross-OS trap when
    # the default (this machine's absolute path) is a Windows path.
    base_uri = os.getenv("STUDIO_TRAIN_ADAPTER_BASE_URI", os.path.abspath(OUT_DIR))
    uri = _uri_join(base_uri, _uri_basename(adapter_dir))
    hint = _cross_os_uri_hint(uri)
    if hint and not os.getenv("STUDIO_TRAIN_ADAPTER_BASE_URI", "").strip():
        print(hint)
    pub = publish_adapter(token, "global", "tool_call", uri, BASE_MODEL, metrics)
    print(f"[trainer] published global/tool_call v{pub['version']} <- {uri}  metrics={metrics}")
    # Optional, fail-safe: prime the serving side so the next call serves it now.
    push_to_serving(uri, "tool_call", BASE_MODEL)
    save_cursor(cursor)
    return {"trained": True, "mode": MODE, "adapter": uri, "version": pub["version"],
            "metrics": metrics, "cursor": cursor}


def run_loop(token, dry_run=False):
    print(f"[trainer] online loop: {API}  poll={POLL_SECONDS}s  base={BASE_MODEL}")
    while True:
        try:
            run_once(token, dry_run=dry_run)
        except SystemExit as e:
            print(str(e))            # transient API/auth error — keep looping
        time.sleep(POLL_SECONDS)


def main():
    ap = argparse.ArgumentParser(
        description="Studio online BitNet trainer. Trains on the GPU if there is "
                    "one (serving BitNet is the CPU half — see HARDWARE in the "
                    "module docstring, and scripts/README-training.md).")
    ap.add_argument("--once", action="store_true", help="one training round then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="pull + format only (no ML deps, no publish)")
    ap.add_argument("--status", action="store_true", help="print training status and exit")
    args = ap.parse_args()

    token = login()
    if args.status:
        print(json.dumps(get_status(token), indent=2))
        return
    if args.once or args.dry_run:
        result = run_once(token, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, default=str))
        return
    run_loop(token, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
