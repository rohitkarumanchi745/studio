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
  4. publishes to compatible directory-based LoRA serving only after an
     independent regression gate, or stops at an explicit PEFT -> evaluated
     GGUF release gate for strict bitnet.cpp,
  then loops, so experience collection and training can run at the same time.

HARDWARE — the split, because it is the opposite of the intuitive one
- TRAINING (this script) is a GPU job. It fine-tunes
  microsoft/bitnet-b1.58-2B-4T-bf16, ~4.8 GB of ordinary bf16 master weights;
  the packed 1-bit repo cannot be fine-tuned at all (see BASE_MODEL). A LoRA
  Measure the real round on the selected GPU; CPU/MPS is a slow fallback.
- The supplied BitNet serving image is a CPU job. Stock vLLM cannot load BitNet
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
- The cursor and eligible sub-threshold rows are one atomic private checkpoint,
  bounded by STUDIO_TRAIN_MAX_PENDING_ROLLOUTS / MAX_PENDING_BYTES. A failed or
  deferred round is replayable after restart; only an exact published-release
  acknowledgement clears a strict GGUF batch.
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
  STUDIO_TRAIN_MAX_PENDING_ROLLOUTS  checkpoint row cap (default 10000)
  STUDIO_TRAIN_MAX_PENDING_BYTES     checkpoint byte cap (default 64 MiB)
  STUDIO_TRAIN_MAX_REPLAY_ROLLOUTS   cumulative replay row cap (default 50000)
  STUDIO_TRAIN_MAX_REPLAY_BYTES      cumulative replay byte cap (default 256 MiB)
  STUDIO_TRAIN_INCLUDE_HISTORY       opt in to global training on conversation history
  STUDIO_TRAIN_ALLOW_SQL_LITERALS    opt in to free-form string literals (trusted tenant only)
  STUDIO_TRAIN_MIN_REWARD   keep rollouts with reward >= (default 0.6 — the "learned" band)
  STUDIO_TRAIN_MIN_NEW      min new usable samples before an SFT round (default 32)
  STUDIO_TRAIN_POLL_SECONDS loop sleep between polls      (default 60)
  STUDIO_TRAIN_EPOCHS       epochs per round              (default 1)
  STUDIO_TRAIN_DEVICE       'cuda' | 'mps' | 'cpu'        (default: auto-detect)
  STUDIO_TRAIN_DTYPE        'bf16' | 'fp16' | 'fp32'      (default: per device — see
                            _device_and_dtype; fp16 is a COMPATIBILITY switch for
                            pre-Ampere cards, NOT a memory saving over bf16)
  STUDIO_TRAIN_MAX_LENGTH   tokens per training sample    (default 1024 — the biggest
                            VRAM lever after the weights; SFT reserves the full completion
                            and truncates prompt context from the middle)
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
  STUDIO_TRAIN_EVALUATOR_COMMAND  required for direct PEFT publication: JSON argv array for
                            an independent evaluator (never executed through a shell)
  STUDIO_TRAIN_EVAL_SUITE_SHA256  required SHA-256 of the fixed regression suite
  STUDIO_TRAIN_EVAL_MIN_CASES     minimum paired baseline/candidate cases (default 50)
  STUDIO_TRAIN_EVAL_MAX_PASS_RATE_DROP maximum candidate pass-rate regression (default 0)
  STUDIO_TRAIN_EVAL_MAX_UNSAFE_RATE maximum candidate unsafe-action rate (default 0)
  STUDIO_TRAIN_EVAL_MAX_UNSAFE_RATE_INCREASE maximum safety regression (default 0)
  STUDIO_TRAIN_EVAL_TIMEOUT_SECONDS evaluator deadline (default 1800)
  STUDIO_SERVE_URL          serving side to prime after publish (the gateway, or vLLM);
                            unset = push disabled (serving still picks it up next call)
  STUDIO_SERVE_KIND         'gateway' (default, POST /admin/load_adapter) | 'vllm'
  STUDIO_SERVE_TOKEN        optional bearer token if the serving side requires auth

USAGE
  python train_online.py --once        # one round then exit
  python train_online.py --once --defer-publish  # retain PEFT + batch for strict GGUF release
  python train_online.py --ack-published-release --release-uri URI \
      --release-version N --release-sha256 HEX
  python train_online.py --dry-run     # pull + format only (no ML deps, no publish)
  python train_online.py               # continuous online loop (SFT)
  STUDIO_TRAIN_MODE=dpo python train_online.py --dry-run   # mine preference pairs, no ML deps
"""
import argparse
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote

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
REPLAY_FILE = os.path.join(OUT_DIR, ".training_replay.json")
# The cursor and not-yet-trained rows are one atomic checkpoint. Advancing a
# cursor without retaining a sub-threshold batch permanently loses that batch:
# on a quiet deployment, sixteen samples in one poll plus sixteen in the next
# would otherwise never satisfy MIN_NEW=32. Bound both dimensions so a DPO
# workload with no pairable prompts cannot grow the trainer volume forever.
MAX_PENDING_ROLLOUTS = int(os.getenv("STUDIO_TRAIN_MAX_PENDING_ROLLOUTS", "10000"))
MAX_PENDING_BYTES = int(os.getenv("STUDIO_TRAIN_MAX_PENDING_BYTES", str(64 * 1024 * 1024)))
_STATE_VERSION = 2
MAX_REPLAY_ROLLOUTS = int(os.getenv("STUDIO_TRAIN_MAX_REPLAY_ROLLOUTS", "50000"))
MAX_REPLAY_BYTES = int(os.getenv("STUDIO_TRAIN_MAX_REPLAY_BYTES", str(256 * 1024 * 1024)))
_REPLAY_VERSION = 1
INCLUDE_HISTORY = os.getenv("STUDIO_TRAIN_INCLUDE_HISTORY", "").strip().lower() \
    in {"1", "true", "yes", "on"}
ALLOW_SQL_LITERALS = os.getenv("STUDIO_TRAIN_ALLOW_SQL_LITERALS", "").strip().lower() \
    in {"1", "true", "yes", "on"}

# ── Hardware knobs (see HARDWARE in the module docstring) ────────────────
# Every one of these exists because a laptop GPU has a hard VRAM ceiling and the
# operator needs a lever they can turn WITHOUT editing this file. The defaults
# are SIZED for an 8 GB card from the memory arithmetic (4.8 GB of weights +
# activations + a vocab-sized logits tensor, the last two linear in max_length);
# nobody has run this on an NVIDIA card yet, which is why every round prints the
# peak memory it actually used. See scripts/README-training.md §10.
# Tokens per sample. The largest memory term after the weights: activations AND
# the vocab-sized logits tensor both scale with it. SFT reserves the entire
# assistant completion first and removes prompt context from the middle; a
# completion that cannot fit is rejected. Shorter contexts can still reduce
# quality, which is why 1024 is the default and 512 is a last resort rather
# than a free win.
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
# rollout's own role. So both schema/dialect and role-visible tables match;
# the final query guard remains authoritative at execution time.
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


def fetch_skills(token, roles=None):
    """Fetch the current skill for each rollout role, never the admin superset."""
    out = {}
    requested = sorted({str(role).strip() for role in (roles or []) if str(role).strip()})
    # No roles preserves the old one-request helper contract for diagnostics.
    requested = requested or [None]
    for role in requested:
        path = "/api/skills" if role is None else f"/api/skills?role={quote(role, safe='')}"
        res = _req("GET", path, token=token)
        effective_role = res.get("role") or role
        for s in res.get("skills", []):
            src = s.get("source")
            if not src:
                continue
            key = src if role is None else (src, effective_role)
            out[key] = {
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
    role = (r.get("role") or "").strip()
    if not role:
        stale["no_role"] += 1
        return None, "no_role"
    ctx = skills.get((src, role)) or skills.get(src)
    if ctx is None:
        stale["source_gone"] += 1        # deconfigured / renamed / RBAC-revoked
        return None, "source_gone"
    refs = _referenced_tables((r.get("action") or {}).get("sql"))
    if refs and not refs.issubset(ctx["allowed"]):
        stale["stale_tables"] += 1       # references a table no longer allowed
        return None, "stale_tables"
    if _has_private_sql_literal((r.get("action") or {}).get("sql")):
        stale["private_literal"] += 1
        return None, "private_literal"
    return src, ctx["context"]


_SQL_STRING = re.compile(r"'(?:''|[^'])*'")
_SAFE_SQL_WORDS = {
    "day", "week", "month", "quarter", "year", "hour", "minute", "second",
    "true", "false", "utc",
}
_SAFE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ t]\d{2}:\d{2}(?::\d{2})?(?:z|[+-]\d{2}:?\d{2})?)?",
                        re.IGNORECASE)
_SAFE_TIME_FORMAT = re.compile(r"(?:%[a-zA-Z]|[-/: .])+")


def _has_private_sql_literal(sql):
    """Conservatively exclude free-form SQL strings from a global adapter.

    Dates, date-part names and strftime masks are structural SQL. Customer
    names, emails, regions, ticket text and other free-form values require an
    explicit trusted-single-tenant opt-in.
    """
    if ALLOW_SQL_LITERALS:
        return False
    for match in _SQL_STRING.finditer(sql or ""):
        value = match.group(0)[1:-1].replace("''", "'").strip()
        if (not value or value.lower() in _SAFE_SQL_WORDS or _SAFE_DATE.fullmatch(value)
                or _SAFE_TIME_FORMAT.fullmatch(value)):
            continue
        return True
    return False


def publish_adapter(token, scope, kind, uri, base_model, metrics, sha256=None):
    body = {
        "scope": scope, "kind": kind, "uri": uri,
        "base_model": base_model, "metrics": metrics}
    if sha256:
        body["sha256"] = sha256
    return _req("POST", "/api/training/adapters", token=token, body=body)


def get_status(token):
    return _req("GET", "/api/training/online", token=token)


def acknowledge_published_release(token, uri, version, sha256):
    """Consume a deferred batch only after Studio records its exact GGUF release.

    Conversion and evaluation remain operator-controlled.  This acknowledgement
    closes the durable cursor transaction after that release: a typo or stale
    registry row leaves every pending rollout untouched.
    """
    uri = str(uri or "").strip()
    sha256 = str(sha256 or "").strip().lower()
    try:
        version = int(version)
    except (TypeError, ValueError):
        raise SystemExit("[trainer] --release-version must be a positive integer; pending data was retained") from None
    if not uri or not 1 <= version <= 2**31 - 1 \
            or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise SystemExit(
            "[trainer] release acknowledgement requires an exact URI, positive version, "
            "and 64-character SHA-256; pending data was retained")
    active = (get_status(token).get("tool_call_adapter") or {})
    expected = {"uri": uri, "version": version, "sha256": sha256}
    actual = {key: active.get(key) for key in expected} if isinstance(active, dict) else {}
    if actual != expected:
        raise SystemExit(
            "[trainer] Studio's active tool_call adapter does not match the acknowledged "
            "URI/version/SHA-256; pending data was retained")
    cursor, pending = load_training_state()
    if pending:
        save_training_state(cursor, [])
    return {"acknowledged": True, "released": expected, "cursor": cursor,
            "cleared_pending": len(pending)}


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


def push_to_serving(uri, kind, base_model, name=None, version=None, sha256=None):
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
        body = {"uri": uri, "kind": kind, "base_model": base_model}
        if version is not None:
            body["version"] = version
        if sha256:
            body["sha256"] = sha256
        ok = _serve_post("/admin/load_adapter", body)
    print(f"[trainer] serving push ({SERVE_KIND}) for {kind} {uri}: "
          f"{'ok' if ok else 'failed (adapter still served on next call)'}")
    return ok


# ── Cursor + pending-rollout persistence ─────────────────────────────────

def _state_error(message):
    return SystemExit(f"[trainer] invalid training checkpoint {CURSOR_FILE}: {message}. "
                      "Refusing to advance the rollout cursor; repair or archive the file explicitly.")


def _checked_state(cursor, pending):
    """Validate and serialize one bounded checkpoint.

    The pending rows contain prompts and model actions, so the on-disk file is
    mode 0600. Overflow is fail-closed: silently evicting the oldest rows would
    make the training result depend on polling cadence and could discard the
    first half of a later DPO pair.
    """
    try:
        cursor = float(cursor)
    except (TypeError, ValueError):
        raise _state_error("cursor is not numeric")
    if not math.isfinite(cursor) or cursor < 0:
        raise _state_error("cursor must be a finite non-negative number")
    if not isinstance(pending, list):
        raise _state_error("pending must be a list")
    if MAX_PENDING_ROLLOUTS < 1 or MAX_PENDING_BYTES < 1024:
        raise _state_error("pending limits must be positive (bytes must be at least 1024)")
    if len(pending) > MAX_PENDING_ROLLOUTS:
        raise SystemExit(
            f"[trainer] pending rollout buffer would contain {len(pending)} rows, above "
            f"STUDIO_TRAIN_MAX_PENDING_ROLLOUTS={MAX_PENDING_ROLLOUTS}. Cursor did not "
            "advance. Raise the bounded limit, switch/train the objective, or archive "
            "and deliberately reset the checkpoint; no rollout was silently evicted.")
    seen = set()
    for row in pending:
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or not rid or rid in seen:
            raise _state_error("each pending rollout needs one unique non-empty id")
        seen.add(rid)
    payload = {"version": _STATE_VERSION, "cursor": cursor, "pending": pending,
               "at": time.time()}
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise _state_error("checkpoint is not finite JSON")
    if len(encoded) > MAX_PENDING_BYTES:
        raise SystemExit(
            f"[trainer] pending rollout checkpoint would use {len(encoded)} bytes, above "
            f"STUDIO_TRAIN_MAX_PENDING_BYTES={MAX_PENDING_BYTES}. Cursor did not advance. "
            "Raise the bounded limit, switch/train the objective, or archive and "
            "deliberately reset the checkpoint; no rollout was silently evicted.")
    return cursor, pending, encoded


def load_training_state():
    """Load a v2 cursor+pending checkpoint, accepting the legacy cursor-only file."""
    try:
        size = os.path.getsize(CURSOR_FILE)
    except FileNotFoundError:
        return 0.0, []
    except OSError as exc:
        raise _state_error(str(exc))
    if size > MAX_PENDING_BYTES:
        raise _state_error(f"file is {size} bytes (limit {MAX_PENDING_BYTES})")
    try:
        with open(CURSOR_FILE, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, ValueError) as exc:
        raise _state_error(str(exc))
    if not isinstance(value, dict):
        raise _state_error("root is not an object")
    version = value.get("version", 1)
    if version not in (1, _STATE_VERSION):
        raise _state_error(f"unsupported version {version!r}")
    pending = value.get("pending", []) if version == _STATE_VERSION else []
    cursor, pending, _ = _checked_state(value.get("cursor", 0.0), pending)
    return cursor, pending


def save_training_state(cursor, pending):
    """Atomically replace the checkpoint; a crash leaves old or new, never half."""
    cursor, pending, encoded = _checked_state(cursor, pending)
    os.makedirs(OUT_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".train-cursor-", suffix=".tmp", dir=OUT_DIR)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CURSOR_FILE)
        try:
            os.chmod(CURSOR_FILE, 0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
    return cursor


def load_cursor():
    """Compatibility accessor used by status/tests; pending data is retained."""
    return load_training_state()[0]


def save_cursor(cursor):
    """Compatibility writer for a completed round (there is no pending data)."""
    return save_training_state(cursor, [])


def _checked_replay(rows):
    """Validate and serialize the durable, cumulative training corpus."""
    if not isinstance(rows, list):
        raise SystemExit(f"[trainer] invalid replay corpus {REPLAY_FILE}: root is not a list")
    if MAX_REPLAY_ROLLOUTS < 1 or MAX_REPLAY_BYTES < 1024:
        raise SystemExit("[trainer] replay limits must be positive (bytes at least 1024)")
    if len(rows) > MAX_REPLAY_ROLLOUTS:
        raise SystemExit(
            f"[trainer] replay corpus would contain {len(rows)} rows, above "
            f"STUDIO_TRAIN_MAX_REPLAY_ROLLOUTS={MAX_REPLAY_ROLLOUTS}. Nothing was "
            "evicted or published; curate/archive the corpus or raise the explicit bound.")
    seen = set()
    for row in rows:
        rid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(rid, str) or not rid or rid in seen:
            raise SystemExit(
                f"[trainer] invalid replay corpus {REPLAY_FILE}: every row needs one "
                "unique non-empty id")
        seen.add(rid)
    payload = {"version": _REPLAY_VERSION, "rollouts": rows, "at": time.time()}
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"[trainer] invalid replay corpus {REPLAY_FILE}: {exc}") from None
    if len(encoded) > MAX_REPLAY_BYTES:
        raise SystemExit(
            f"[trainer] replay corpus would use {len(encoded)} bytes, above "
            f"STUDIO_TRAIN_MAX_REPLAY_BYTES={MAX_REPLAY_BYTES}. Nothing was evicted "
            "or published; curate/archive the corpus or raise the explicit bound.")
    return encoded


def load_replay_corpus():
    """Load the private cumulative corpus; a corrupt file fails closed."""
    try:
        size = os.path.getsize(REPLAY_FILE)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SystemExit(f"[trainer] cannot stat replay corpus {REPLAY_FILE}: {exc}")
    if size > MAX_REPLAY_BYTES:
        raise SystemExit(f"[trainer] replay corpus {REPLAY_FILE} exceeds its byte limit")
    try:
        with open(REPLAY_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[trainer] invalid replay corpus {REPLAY_FILE}: {exc}")
    if not isinstance(payload, dict) or payload.get("version") != _REPLAY_VERSION:
        raise SystemExit(f"[trainer] invalid replay corpus {REPLAY_FILE}: unsupported format")
    rows = payload.get("rollouts")
    _checked_replay(rows)
    return rows


def save_replay_corpus(rows):
    """Atomically persist the cumulative corpus with private permissions."""
    encoded = _checked_replay(rows)
    os.makedirs(OUT_DIR, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".training-replay-", suffix=".tmp", dir=OUT_DIR)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, REPLAY_FILE)
        try:
            os.chmod(REPLAY_FILE, 0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
    return rows


def _buffer_candidate(row):
    """Rows this SQL tool-call trainer could consume now or in DPO later.

    Structured pipeline/recovery actions intentionally remain outside this
    adapter. Buffering them would eventually overflow a worker that can never
    format them, and mixing their JSON contract into run_sql labels is unsafe.
    """
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise SystemExit("[trainer] rollout stream returned a row without a stable id; cursor did not advance")
    prompt = row.get("prompt")
    reward = row.get("reward")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(reward, (int, float)) \
            or isinstance(reward, bool) or not math.isfinite(float(reward)):
        return False
    if (row.get("mode") or "").startswith(("fallback", "error")):
        return False
    return _completion_for(row.get("action")) is not None


def merge_pending(pending, incoming):
    """Deduplicate stable rollout identities while retaining arrival order."""
    combined = []
    positions = {}
    for row in [*(pending or []), *(incoming or [])]:
        if not _buffer_candidate(row):
            continue
        rid = row["id"]
        if rid in positions:
            # A later API representation may contain superseding user feedback.
            combined[positions[rid]] = row
        else:
            positions[rid] = len(combined)
            combined.append(row)
    # Validate bounds before the caller persists a cursor beyond these rows.
    _checked_state(0.0, combined)
    return combined


def merge_replay(existing, updates):
    """Replace reward revisions by trace id while retaining every prior row."""
    combined = []
    positions = {}
    for row in [*(existing or []), *(updates or [])]:
        if not _buffer_candidate(row):
            continue
        rid = row["id"]
        if rid in positions:
            combined[positions[rid]] = row
        else:
            positions[rid] = len(combined)
            combined.append(row)
    _checked_replay(combined)
    return combined


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
        if r.get("mode") == "agent:aggregator":
            # The global adapter emits SQL/chart tool calls. The Aggregator is
            # a synthesis policy whose output is prose, not a worker action.
            stale["non_tool_policy"] += 1
            continue
        if (r.get("mode") or "").startswith(("fallback", "error")):
            continue  # deterministic fallback / errors aren't policy to imitate
        completion = _completion_for(r.get("action"))
        if completion is None:
            continue
        if r.get("history") and not INCLUDE_HISTORY:
            stale["private_history"] += 1
            continue
        src, ctx = _condition(r, skills, stale)
        if src is None:
            continue  # no current source context / stale schema -> not trainable
        samples.append({"id": r["id"], "system": ctx, "prompt": prompt,
                        "completion": completion,
                        "reward": float(reward), "source": src,
                        # the turns the model actually saw — trained in the same
                        # positions serving puts them (train == serve, multi-turn)
                        "history": r.get("history") or []})
    return samples, stale


def tool_policy_rollouts(rollouts):
    """Remove synthesis-policy rows before pending/replay persistence.

    Older Studio versions mislabeled the Aggregator with a worker's last SQL.
    Filtering only while formatting would make those poisoned rows live in the
    durable pending/replay files forever. Drop them at ingestion as well; the
    defensive checks in SFT/DPO formatting remain for direct callers.
    """
    return [row for row in (rollouts or [])
            if not (isinstance(row, dict) and row.get("mode") == "agent:aggregator")]


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
        if r.get("mode") == "agent:aggregator":
            stale["non_tool_policy"] += 1
            continue
        if (r.get("mode") or "").startswith(("fallback", "error")):
            continue
        completion = _completion_for(r.get("action"))
        if completion is None:
            continue
        if r.get("history") and not INCLUDE_HISTORY:
            stale["private_history"] += 1
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
    """Atomically write prompt-bearing diagnostics with private permissions."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}-", suffix=".tmp", dir=directory)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        # Encoding is explicit: a skill file with a non-ASCII column comment
        # would raise UnicodeEncodeError under Windows' cp1252 default.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            for sample in samples:
                handle.write(json.dumps(sample) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
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
                     "(STUDIO_TRAIN_DEVICE=cpu — an overnight job, but it produces a "
                     "real candidate for the promotion gate) "
                     "or point STUDIO_TRAIN_BASE_MODEL at a smaller base.")
    elif total_gb < 7.5:
        lines.append("[trainer] 6 GB class card: tight but usually workable. Keep "
                     "STUDIO_TRAIN_GRAD_CHECKPOINT=1 and STUDIO_TRAIN_BATCH_SIZE=1, and "
                     "if it still OOMs STUDIO_TRAIN_MAX_LENGTH=512 (which preserves the "
                     "tool-call label but removes more prompt context, so close other GPU "
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
        "the vocab-sized logits scale with it. SFT keeps the complete tool-call label "
        "and removes prompt context from the middle; less context can reduce quality. "
        "Prefer the levers above.)",
        f"  4. STUDIO_TRAIN_BATCH_SIZE=1   (now {BATCH_SIZE}; raise STUDIO_TRAIN_GRAD_ACCUM "
        "to keep the same effective batch)",
        "  5. Close other GPU users (browser, game, another python) — `nvidia-smi` shows "
        "who holds the VRAM. On Windows the desktop compositor itself takes ~0.5-1 GB.",
        "  6. STUDIO_TRAIN_DEVICE=cpu   (last resort: very slow on this base — think "
        "overnight, not minutes — but it completes a real candidate for evaluation)",
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


def _completion_features(tokenizer, sample, max_length=MAX_LENGTH):
    """Tokenize one SFT sample while masking every non-assistant target token.

    The completion budget is reserved first. If context is too long, retain its
    beginning (system/source identity) and end (current user request) rather
    than right-truncating away the SQL action the adapter is meant to learn.
    """
    messages = []
    if sample.get("system"):
        messages.append({"role": "system", "content": sample["system"]})
    for item in sample.get("history") or []:
        messages.append({"role": "user" if item.get("role") == "user" else "assistant",
                         "content": item.get("text") or ""})
    messages.append({"role": "user", "content": sample["prompt"]})
    try:
        prompt_text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True)
    except Exception:
        prompt_text = "".join(
            f"<|{item['role']}|>\n{item['content']}\n" for item in messages
        ) + "<|assistant|>\n"
    eos = getattr(tokenizer, "eos_token", None) or ""

    def ids(text, *, special=False):
        value = tokenizer(text, add_special_tokens=special, truncation=False)
        return list(value["input_ids"])

    prompt_ids = ids(prompt_text, special=True)
    completion_ids = ids(sample["completion"] + eos)
    if not completion_ids:
        raise ValueError("assistant completion tokenized to an empty sequence")
    if len(completion_ids) >= max_length:
        raise ValueError(
            f"assistant completion uses {len(completion_ids)} tokens, leaving no context "
            f"inside STUDIO_TRAIN_MAX_LENGTH={max_length}")
    keep = max_length - len(completion_ids)
    if len(prompt_ids) > keep:
        # Preserve both the skill/source prefix and the live user suffix. Any
        # removed middle is old history/schema detail, never the target label.
        head = min(len(prompt_ids), max(1, keep // 3))
        tail = keep - head
        prompt_ids = prompt_ids[:head] + (prompt_ids[-tail:] if tail else [])
    input_ids = prompt_ids + completion_ids
    labels = [-100] * len(prompt_ids) + completion_ids
    if not any(label != -100 for label in labels):
        raise ValueError("assistant completion was entirely masked")
    return {"input_ids": input_ids, "attention_mask": [1] * len(input_ids),
            "labels": labels}


def _completion_collator(tokenizer):
    """Pad completion-masked causal-LM features without overwriting labels."""
    def collate(features):
        import torch
        width = max(len(item["input_ids"]) for item in features)
        pad_id = tokenizer.pad_token_id
        if pad_id is None:
            pad_id = tokenizer.eos_token_id
        if pad_id is None:
            raise ValueError("tokenizer has neither pad_token_id nor eos_token_id")
        left = getattr(tokenizer, "padding_side", "right") == "left"
        batch = {"input_ids": [], "attention_mask": [], "labels": []}
        for item in features:
            n = width - len(item["input_ids"])
            if left:
                batch["input_ids"].append([pad_id] * n + item["input_ids"])
                batch["attention_mask"].append([0] * n + item["attention_mask"])
                batch["labels"].append([-100] * n + item["labels"])
            else:
                batch["input_ids"].append(item["input_ids"] + [pad_id] * n)
                batch["attention_mask"].append(item["attention_mask"] + [0] * n)
                batch["labels"].append(item["labels"] + [-100] * n)
        return {key: torch.tensor(value, dtype=torch.long) for key, value in batch.items()}
    return collate


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
        from peft import LoraConfig, get_peft_model
        from transformers import AutoTokenizer, Trainer, TrainingArguments
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

    try:
        features = [_completion_features(tok, sample) for sample in samples]
    except ValueError as exc:
        raise SystemExit(f"[trainer] refusing label-less/truncated SFT sample: {exc}") from None
    ds = Dataset.from_list(features)
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
    model = get_peft_model(model, lora)
    adapter_dir = os.path.join(out_dir, f"tool_call-{int(time.time())}")
    cfg = TrainingArguments(output_dir=adapter_dir, num_train_epochs=epochs,
                            learning_rate=2e-4, logging_steps=_log_every(steps),
                            save_strategy="no", report_to=[], remove_unused_columns=False,
                            **_training_kwargs(device))
    trainer = Trainer(model=model, args=cfg, train_dataset=ds,
                      data_collator=_completion_collator(tok))
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
        "loss_scope": "assistant_completion_only",
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
        for h in (p.get("history") or []):   # empty unless trusted-deployment opt-in
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


_EVALUATION_PROTOCOL = "studio.bitnet.promotion-eval.v1"
_EVALUATION_REPORT_MAX_BYTES = 1024 * 1024
_HEX_64 = re.compile(r"[0-9a-f]{64}")


def _promotion_error(message):
    return SystemExit(
        f"[trainer] promotion evaluation refused: {message}. Candidate was not "
        "published and the rollout checkpoint remains pending")


def _eval_float(name, default):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        raise _promotion_error(f"{name} must be a finite number from 0 through 1") from None
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise _promotion_error(f"{name} must be a finite number from 0 through 1")
    return value


def _promotion_evaluator_config():
    """Parse the independent evaluation contract before spending GPU time.

    The command is JSON argv, not a shell fragment.  The trainer appends the
    reserved ``--request`` and ``--report`` arguments.  A pinned suite digest
    makes a green report for an easier/replaced benchmark unusable.
    """
    raw = os.getenv("STUDIO_TRAIN_EVALUATOR_COMMAND", "").strip()
    if not raw:
        raise _promotion_error(
            "STUDIO_TRAIN_EVALUATOR_COMMAND is required for automatic PEFT promotion "
            "(use --defer-publish for a manual release)")
    try:
        command = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _promotion_error(
            f"STUDIO_TRAIN_EVALUATOR_COMMAND must be a JSON argv array ({exc})") from None
    if not isinstance(command, list) or not command or len(command) > 128 \
            or any(not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 4096
                   for arg in command):
        raise _promotion_error(
            "STUDIO_TRAIN_EVALUATOR_COMMAND must contain 1-128 non-empty string arguments")
    if "--request" in command or "--report" in command:
        raise _promotion_error(
            "evaluator argv may not contain reserved --request/--report arguments")

    suite_sha256 = os.getenv("STUDIO_TRAIN_EVAL_SUITE_SHA256", "").strip().lower()
    if not _HEX_64.fullmatch(suite_sha256):
        raise _promotion_error(
            "STUDIO_TRAIN_EVAL_SUITE_SHA256 must pin the fixed suite with 64 lowercase/uppercase hex characters")
    try:
        min_cases = int(os.getenv("STUDIO_TRAIN_EVAL_MIN_CASES", "50").strip())
        timeout = int(os.getenv("STUDIO_TRAIN_EVAL_TIMEOUT_SECONDS", "1800").strip())
    except ValueError:
        raise _promotion_error(
            "STUDIO_TRAIN_EVAL_MIN_CASES and STUDIO_TRAIN_EVAL_TIMEOUT_SECONDS must be integers") from None
    if not 1 <= min_cases <= 10_000_000:
        raise _promotion_error("STUDIO_TRAIN_EVAL_MIN_CASES must be between 1 and 10000000")
    if not 1 <= timeout <= 86_400:
        raise _promotion_error("STUDIO_TRAIN_EVAL_TIMEOUT_SECONDS must be between 1 and 86400")
    return {
        "command": command,
        "suite_sha256": suite_sha256,
        "min_cases": min_cases,
        "max_pass_rate_drop": _eval_float("STUDIO_TRAIN_EVAL_MAX_PASS_RATE_DROP", 0),
        "max_unsafe_rate": _eval_float("STUDIO_TRAIN_EVAL_MAX_UNSAFE_RATE", 0),
        "max_unsafe_rate_increase": _eval_float(
            "STUDIO_TRAIN_EVAL_MAX_UNSAFE_RATE_INCREASE", 0),
        "timeout_seconds": timeout,
    }


def _adapter_tree_sha256(adapter_dir):
    """Digest a directory's paths and bytes, rejecting links and special files."""
    root = os.path.abspath(adapter_dir)
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise _promotion_error(f"cannot inspect candidate adapter {root}: {exc}") from None
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise _promotion_error("candidate adapter must be a real directory, not a link")

    digest = hashlib.sha256(b"studio-peft-directory-v1\0")
    files_seen = 0
    def walk_error(exc):
        raise _promotion_error(f"cannot traverse candidate adapter: {exc}")

    for current, dirs, files in os.walk(
            root, topdown=True, followlinks=False, onerror=walk_error):
        dirs.sort()
        files.sort()
        for name in dirs:
            path = os.path.join(current, name)
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise _promotion_error(f"candidate adapter contains directory symlink {name!r}")
        for name in files:
            path = os.path.join(current, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            try:
                before = os.lstat(path)
            except OSError as exc:
                raise _promotion_error(f"cannot inspect candidate file {rel!r}: {exc}") from None
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise _promotion_error(f"candidate adapter contains non-regular file {rel!r}")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                raise _promotion_error(f"cannot open candidate file {rel!r}: {exc}") from None
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != \
                        (before.st_dev, before.st_ino):
                    raise _promotion_error(f"candidate file changed while hashing: {rel!r}")
                rel_bytes = rel.encode("utf-8")
                digest.update(len(rel_bytes).to_bytes(8, "big"))
                digest.update(rel_bytes)
                digest.update(opened.st_size.to_bytes(8, "big"))
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            files_seen += 1
    if not files_seen:
        raise _promotion_error("candidate adapter directory is empty")
    return digest.hexdigest()


def _evaluation_counts(report, name):
    section = report.get(name)
    if not isinstance(section, dict):
        raise _promotion_error(f"evaluator report {name!r} must be an object")
    values = []
    for field in ("cases", "passed", "unsafe"):
        value = section.get(field)
        if type(value) is not int or value < 0:
            raise _promotion_error(f"evaluator report {name}.{field} must be a non-negative integer")
        values.append(value)
    cases, passed, unsafe = values
    if passed > cases or unsafe > cases:
        raise _promotion_error(f"evaluator report {name} counts exceed its case count")
    return {"cases": cases, "passed": passed, "unsafe": unsafe}


def _read_evaluation_report(path):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _promotion_error(f"evaluator did not create its report: {exc}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _promotion_error("evaluator report must be a regular file, not a link")
    if info.st_size <= 0 or info.st_size > _EVALUATION_REPORT_MAX_BYTES:
        raise _promotion_error(
            f"evaluator report must be 1-{_EVALUATION_REPORT_MAX_BYTES} bytes")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != \
                    (info.st_dev, info.st_ino) or opened.st_size != info.st_size:
                raise _promotion_error("evaluator report changed while it was opened")
            raw = handle.read(_EVALUATION_REPORT_MAX_BYTES + 1)
        report = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise _promotion_error(f"evaluator report is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(report, dict):
        raise _promotion_error("evaluator report root must be an object")
    return report


def _evaluation_log_tail(path):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 4000))
            return handle.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def evaluate_candidate(adapter_dir, mode, config=None):
    """Run and verify an independent paired baseline/candidate regression eval.

    Evaluator argv receives ``--request FILE --report FILE``. It must evaluate
    the exact fixed suite once against the unadapted base and once against the
    candidate, then write the v1 JSON report described in README-training.md.
    The trainer owns threshold decisions and rehashes the candidate afterwards.
    """
    config = config or _promotion_evaluator_config()
    adapter_dir = os.path.abspath(adapter_dir)
    artifact_sha256 = _adapter_tree_sha256(adapter_dir)
    request_id = secrets.token_hex(16)
    request = {
        "protocol": _EVALUATION_PROTOCOL,
        "request_id": request_id,
        "adapter_dir": adapter_dir,
        "artifact_sha256": artifact_sha256,
        "base_model": BASE_MODEL,
        "mode": mode,
        "suite_sha256": config["suite_sha256"],
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=".promotion-eval-", dir=OUT_DIR)
    try:
        try:
            os.chmod(scratch, 0o700)
        except OSError:
            pass
        request_path = os.path.join(scratch, "request.json")
        report_path = os.path.join(scratch, "report.json")
        log_path = os.path.join(scratch, "evaluator.log")
        with open(request_path, "x", encoding="utf-8") as handle:
            try:
                os.chmod(request_path, 0o600)
            except OSError:
                pass
            json.dump(request, handle, sort_keys=True, separators=(",", ":"))
        argv = [*config["command"], "--request", request_path, "--report", report_path]
        try:
            with open(log_path, "wb") as log:
                completed = subprocess.run(
                    argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    shell=False, timeout=config["timeout_seconds"], check=False)
        except subprocess.TimeoutExpired:
            raise _promotion_error(
                f"evaluator exceeded {config['timeout_seconds']} seconds") from None
        except OSError as exc:
            raise _promotion_error(f"could not start evaluator: {exc}") from None
        if completed.returncode != 0:
            tail = _evaluation_log_tail(log_path)
            detail = f"; output tail: {tail}" if tail else ""
            raise _promotion_error(f"evaluator exited {completed.returncode}{detail}")
        report = _read_evaluation_report(report_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    expected_identity = {
        "protocol": _EVALUATION_PROTOCOL,
        "request_id": request_id,
        "artifact_sha256": artifact_sha256,
        "base_model": BASE_MODEL,
        "suite_sha256": config["suite_sha256"],
    }
    actual_identity = {key: report.get(key) for key in expected_identity}
    if actual_identity != expected_identity:
        raise _promotion_error(
            "evaluator report identity does not match its request (protocol, request, "
            "base, suite, or candidate digest)")
    baseline = _evaluation_counts(report, "baseline")
    candidate = _evaluation_counts(report, "candidate")
    if baseline["cases"] != candidate["cases"]:
        raise _promotion_error("baseline and candidate must run the same number of paired cases")
    if candidate["cases"] < config["min_cases"]:
        raise _promotion_error(
            f"report has {candidate['cases']} cases, below STUDIO_TRAIN_EVAL_MIN_CASES="
            f"{config['min_cases']}")
    baseline_pass_rate = baseline["passed"] / baseline["cases"]
    candidate_pass_rate = candidate["passed"] / candidate["cases"]
    baseline_unsafe_rate = baseline["unsafe"] / baseline["cases"]
    candidate_unsafe_rate = candidate["unsafe"] / candidate["cases"]
    if candidate_pass_rate + config["max_pass_rate_drop"] < baseline_pass_rate:
        raise _promotion_error(
            f"candidate pass rate {candidate_pass_rate:.6f} regressed from baseline "
            f"{baseline_pass_rate:.6f} beyond allowed drop {config['max_pass_rate_drop']:.6f}")
    if candidate_unsafe_rate > config["max_unsafe_rate"]:
        raise _promotion_error(
            f"candidate unsafe rate {candidate_unsafe_rate:.6f} exceeds absolute limit "
            f"{config['max_unsafe_rate']:.6f}")
    if candidate_unsafe_rate > baseline_unsafe_rate + config["max_unsafe_rate_increase"]:
        raise _promotion_error(
            f"candidate unsafe rate {candidate_unsafe_rate:.6f} regressed from baseline "
            f"{baseline_unsafe_rate:.6f} beyond allowed increase "
            f"{config['max_unsafe_rate_increase']:.6f}")
    if _adapter_tree_sha256(adapter_dir) != artifact_sha256:
        raise _promotion_error("candidate adapter changed during evaluation")
    return {
        "protocol": _EVALUATION_PROTOCOL,
        "suite_sha256": config["suite_sha256"],
        "artifact_sha256": artifact_sha256,
        "baseline": {**baseline, "pass_rate": baseline_pass_rate,
                     "unsafe_rate": baseline_unsafe_rate},
        "candidate": {**candidate, "pass_rate": candidate_pass_rate,
                      "unsafe_rate": candidate_unsafe_rate},
        "thresholds": {
            "min_cases": config["min_cases"],
            "max_pass_rate_drop": config["max_pass_rate_drop"],
            "max_unsafe_rate": config["max_unsafe_rate"],
            "max_unsafe_rate_increase": config["max_unsafe_rate_increase"],
        },
    }


def run_once(token, dry_run=False, defer_publish=False):
    release_policy = get_status(token)
    requires_gguf_release = bool(release_policy.get("requires_tool_adapter_sha256"))
    if requires_gguf_release and not (dry_run or defer_publish):
        raise SystemExit(
            "Studio requires a SHA-256-attested served adapter, but train_online.py "
            "produces a PEFT directory, not the final GGUF bytes. Refusing before "
            "training so the registry cannot be replaced with the wrong artifact. "
            "Run with --defer-publish to train and retain the PEFT output, then "
            "convert/evaluate/upload the GGUF and publish its URI + version + "
            "SHA-256 through POST /api/training/adapters. The cursor does not move.")
    evaluator_config = None
    if not (requires_gguf_release or dry_run or defer_publish):
        evaluator_config = _promotion_evaluator_config()
    since, pending = load_training_state()
    pulled = pull_rollouts(token, since)
    incoming, cursor = pulled["rollouts"], pulled["cursor"]
    try:
        cursor = float(cursor)
    except (TypeError, ValueError):
        raise SystemExit("[trainer] rollout stream returned an invalid cursor; checkpoint did not change")
    if not math.isfinite(cursor) or cursor < since:
        raise SystemExit("[trainer] rollout stream cursor moved backwards or is not finite; checkpoint did not change")
    if not isinstance(incoming, list):
        raise SystemExit("[trainer] rollout stream returned no rollout list; checkpoint did not change")
    incoming = tool_policy_rollouts(incoming)
    pending = tool_policy_rollouts(pending)
    batch = merge_pending(pending, incoming)
    replay_before = tool_policy_rollouts(load_replay_corpus())
    rollouts = merge_replay(replay_before, batch)
    # Cursor + raw candidates commit together before a real round. A crash,
    # OOM, failed publish, or sub-threshold return can therefore replay every
    # retained row after restart. Dry-run remains observational with respect to
    # trainer state (it may still write the documented last_samples JSONL).
    if not dry_run:
        save_training_state(cursor, batch)
        # Persist before expensive training. If the process dies after this
        # write, the pending batch is still present; the next round deduplicates
        # it into this corpus by trace id. No successful prior release is ever
        # replaced by training solely on the newest arrivals.
        save_replay_corpus(rollouts)
    # Re-fetch the CURRENT per-source schema+dialect context once per round. This
    # is the drift handler: a schema change flips the source's skill, so every
    # sample this round is conditioned on the up-to-date schema (§changing schema).
    roles = {r.get("role") for r in rollouts if r.get("role")}
    skills = fetch_skills(token, roles)
    print(f"[trainer] source context: {len(skills)} source(s) from /api/skills "
          f"{ {s: skills[s]['dialect'] for s in skills} }")

    # Prepare the objective's training data: SFT filters, DPO mines pairs — both
    # conditioned per source, both dropping rollouts with no current source context.
    if MODE == "dpo":
        pairs, stale = mine_preference_pairs(rollouts, skills)
        per_source = _per_source_pairs(pairs)
        print(f"[trainer] DPO: {len(incoming)} new + {len(pending)} pending -> "
              f"{len(batch)} unreleased; {len(rollouts)} cumulative replay rollouts -> "
              f"{len(pairs)} preference pairs (within-source, reward gap >= {PAIR_MARGIN})")
        print(f"[trainer] DPO per-source pairs: {per_source}")
        print(f"[trainer] dropped rollouts: {dict(stale)} "
              f"(no_source=legacy/blind, source_gone=deconfigured, stale_tables=schema drift)")
        if not batch or len(pairs) < MIN_PAIRS:
            print(f"[trainer] {len(pairs)} < MIN_PAIRS={MIN_PAIRS} — not enough preference "
                  f"signal or no unreleased revisions (needs same-source, same-prompt, "
                  f"reward-differing outcomes); accumulating.")
            return {"trained": False, "mode": "dpo", "pairs": len(pairs),
                    "pending": len(batch), "replay": len(rollouts), "per_source": per_source,
                    "dropped": dict(stale), "cursor": cursor}
        data_path = write_samples_jsonl(pairs, os.path.join(OUT_DIR, "last_pairs.jsonl"))
        if dry_run:
            print(f"[trainer] --dry-run: wrote {len(pairs)} preference pairs to {data_path}; "
                  f"skipping training + publish.")
            return {"trained": False, "mode": "dpo", "dry_run": True, "pairs": len(pairs),
                    "pending": len(batch), "replay": len(rollouts),
                    "per_source": per_source, "dropped": dict(stale),
                    "pairs_path": data_path, "cursor": cursor}
        print(f"[trainer] DPO-training LoRA on {len(pairs)} preference pairs (base={BASE_MODEL}) …")
        adapter_dir, metrics = train_dpo(pairs, BASE_MODEL, OUT_DIR, EPOCHS)
        metrics["per_source"] = per_source
        metrics["dropped"] = dict(stale)
    else:
        samples, stale = to_samples(rollouts, skills)
        new_samples, _new_stale = to_samples(batch, skills)
        per_source = _per_source_samples(samples)
        print(f"[trainer] SFT: {len(incoming)} new + {len(pending)} pending -> "
              f"{len(new_samples)} unreleased usable; {len(samples)} cumulative replay "
              f"samples (reward >= {MIN_REWARD}) across {len(per_source)} source(s)")
        print(f"[trainer] SFT per-source samples: {per_source}")
        print(f"[trainer] dropped rollouts: {dict(stale)} "
              f"(no_source=legacy/blind, source_gone=deconfigured, stale_tables=schema drift)")
        if len(new_samples) < MIN_NEW:
            print(f"[trainer] {len(new_samples)} < MIN_NEW={MIN_NEW} — not enough new "
                  f"experience; retained {len(batch)} rollout revision(s) in the durable "
                  f"pending buffer and {len(rollouts)} in cumulative replay.")
            return {"trained": False, "mode": "sft", "samples": len(samples),
                    "new_samples": len(new_samples), "pending": len(batch),
                    "replay": len(rollouts), "per_source": per_source,
                    "dropped": dict(stale), "cursor": cursor}
        data_path = write_samples_jsonl(samples, os.path.join(OUT_DIR, "last_samples.jsonl"))
        if dry_run:
            print(f"[trainer] --dry-run: wrote {len(samples)} samples to {data_path}; "
                  f"skipping training + publish.")
            return {"trained": False, "mode": "sft", "dry_run": True, "samples": len(samples),
                    "pending": len(batch), "replay": len(rollouts),
                    "per_source": per_source, "dropped": dict(stale),
                    "samples_path": data_path, "cursor": cursor}
        print(f"[trainer] SFT-training LoRA on {len(samples)} samples (base={BASE_MODEL}) …")
        adapter_dir, metrics = train_lora(samples, BASE_MODEL, OUT_DIR, EPOCHS)
        metrics["per_source"] = per_source
        metrics["dropped"] = dict(stale)

    metrics["new_rollout_revisions"] = len(batch)
    metrics["replay_rollouts"] = len(rollouts)

    if defer_publish:
        # llama-server cannot serve this PEFT directory. Deliberately stop at the
        # release boundary instead of hashing adapter_model.safetensors and
        # pretending that digest identifies the later converted GGUF.
        print("[trainer] PEFT training complete; publication deferred. Convert and "
              "evaluate this directory, upload the final GGUF, then publish the "
              "GGUF URI + SHA-256 through Studio's adapter API. Buffered rollout "
              "inputs remain pending and will not be lost across restart.")
        return {"trained": True, "published": False, "mode": MODE,
                "peft_adapter": adapter_dir, "artifact_format": "peft",
                "release_requires": ["gguf", "evaluation", "stable_uri", "sha256"],
                "metrics": metrics, "cursor": cursor, "pending": len(batch),
                "replay": len(rollouts)}

    evaluation = evaluate_candidate(adapter_dir, MODE, evaluator_config)
    metrics["promotion_evaluation"] = evaluation
    print("[trainer] promotion evaluation passed: "
          f"candidate pass={evaluation['candidate']['pass_rate']:.4f} "
          f"baseline={evaluation['baseline']['pass_rate']:.4f} "
          f"unsafe={evaluation['candidate']['unsafe_rate']:.4f} "
          f"cases={evaluation['candidate']['cases']} "
          f"suite={evaluation['suite_sha256']}")

    # Shared publish + serving-push tail (same registry contract for either objective).
    # The uri is what SERVING will open, so join it in the base's own separator
    # style (never os.path.join — see _uri_join) and flag the cross-OS trap when
    # the default (this machine's absolute path) is a Windows path.
    base_uri = os.getenv("STUDIO_TRAIN_ADAPTER_BASE_URI", os.path.abspath(OUT_DIR))
    uri = _uri_join(base_uri, _uri_basename(adapter_dir))
    hint = _cross_os_uri_hint(uri)
    if hint and not os.getenv("STUDIO_TRAIN_ADAPTER_BASE_URI", "").strip():
        print(hint)
    if _adapter_tree_sha256(adapter_dir) != evaluation["artifact_sha256"]:
        raise _promotion_error("candidate adapter changed after evaluation")
    pub = publish_adapter(token, "global", "tool_call", uri, BASE_MODEL, metrics)
    print(f"[trainer] published global/tool_call v{pub['version']} <- {uri}  metrics={metrics}")
    # Optional, fail-safe: prime the serving side so the next call serves it now.
    push_to_serving(uri, "tool_call", BASE_MODEL)
    # Only a successful registry publish consumes the batch. If training or
    # publish raised, the pre-training checkpoint still contains every input.
    save_training_state(cursor, [])
    return {"trained": True, "mode": MODE, "adapter": uri, "version": pub["version"],
            "metrics": metrics, "cursor": cursor}


def run_loop(token, dry_run=False, defer_publish=False):
    print(f"[trainer] online loop: {API}  poll={POLL_SECONDS}s  base={BASE_MODEL}")
    while True:
        try:
            run_once(token, dry_run=dry_run, defer_publish=defer_publish)
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
    ap.add_argument("--defer-publish", action="store_true",
                    help="train PEFT weights but do not publish; required before the manual GGUF release gate")
    ap.add_argument("--ack-published-release", action="store_true",
                    help="after GGUF conversion/evaluation/publication, verify the active identity and clear its retained batch")
    ap.add_argument("--release-uri")
    ap.add_argument("--release-version", type=int)
    ap.add_argument("--release-sha256")
    args = ap.parse_args()
    if args.defer_publish and not (args.once or args.dry_run):
        ap.error("--defer-publish requires --once; a loop would retrain the retained batch")

    token = login()
    if args.ack_published_release:
        result = acknowledge_published_release(
            token, args.release_uri, args.release_version, args.release_sha256)
        print(json.dumps(result, indent=2, default=str))
        return
    if args.status:
        print(json.dumps(get_status(token), indent=2))
        return
    if args.once or args.dry_run:
        result = run_once(token, dry_run=args.dry_run, defer_publish=args.defer_publish)
        print(json.dumps(result, indent=2, default=str))
        return
    run_loop(token, dry_run=args.dry_run, defer_publish=args.defer_publish)


if __name__ == "__main__":
    sys.exit(main())
