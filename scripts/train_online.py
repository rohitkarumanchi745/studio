#!/usr/bin/env python3
"""Online (simultaneous) BitNet trainer — the CPU worker half of the loop.

Studio's API (app/trainer.py) is the PRODUCER + adapter registry:
    Studio agent ──rollouts──▶  THIS worker (CPU)  ──adapters──▶  Studio serving
This script is the CONSUMER that app/trainer.py's docstring references. It:

  1. polls  GET  /training/rollouts?since=<cursor>   (reward-labeled rollouts)
  2. formats successful trajectories into tool-calling SFT samples
  3. trains a small LoRA adapter on a (1-bit) base model — CPU is enough
  4. publishes it  POST /training/adapters   so serving hot-swaps to it,
  then loops, so training and serving run at the same time.

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
  STUDIO_TRAIN_BASE_MODEL   HF id of the base            (default microsoft/bitnet-b1.58-2B-4T)
  STUDIO_TRAIN_OUTPUT_DIR   where adapters + cursor live (default ./adapters)
  STUDIO_TRAIN_MIN_REWARD   keep rollouts with reward >= (default 0.6 — the "learned" band)
  STUDIO_TRAIN_MIN_NEW      min new usable samples before an SFT round (default 32)
  STUDIO_TRAIN_POLL_SECONDS loop sleep between polls      (default 60)
  STUDIO_TRAIN_EPOCHS       epochs per round              (default 1)
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
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = os.getenv("STUDIO_API_URL", "http://localhost:8000").rstrip("/")
OUT_DIR = os.getenv("STUDIO_TRAIN_OUTPUT_DIR", "./adapters")
BASE_MODEL = os.getenv("STUDIO_TRAIN_BASE_MODEL", "microsoft/bitnet-b1.58-2B-4T")
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
        lora_name = name or os.path.basename(uri.rstrip("/"))  # already like tool_call-<ts>
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
        with open(CURSOR_FILE) as f:
            return float(json.load(f).get("cursor", 0.0))
    except (OSError, ValueError):
        return 0.0


def save_cursor(cursor):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(CURSOR_FILE, "w") as f:
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
    with open(path, "w") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")
    return path


# ── LoRA training (real; heavy deps imported lazily) ─────────────────────

def train_lora(samples, base_model, out_dir, epochs):
    """Reward-filtered SFT of a small LoRA adapter on the base model. CPU is
    sufficient for a 1-bit base + a small adapter. Returns (adapter_dir, metrics).
    Raises a clear, actionable error if the ML stack isn't installed."""
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import SFTConfig, SFTTrainer
    except ImportError as e:
        raise SystemExit(
            "[trainer] training needs the ML stack — install it (CPU is fine):\n"
            "    pip install -r scripts/requirements-trainer.txt\n"
            f"  (missing: {e.name}). Use --dry-run to exercise the loop without it.")

    tok = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
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
    model = AutoModelForCausalLM.from_pretrained(
        base_model, trust_remote_code=True, torch_dtype=torch.float32)
    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
                      task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    adapter_dir = os.path.join(out_dir, f"tool_call-{int(time.time())}")
    cfg = SFTConfig(output_dir=adapter_dir, num_train_epochs=epochs,
                    per_device_train_batch_size=1, gradient_accumulation_steps=8,
                    learning_rate=2e-4, logging_steps=10, save_strategy="no",
                    report_to=[], max_length=1024)
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds, peft_config=lora)
    result = trainer.train()
    trainer.save_model(adapter_dir)      # saves the LoRA adapter only
    tok.save_pretrained(adapter_dir)
    metrics = {
        "loss": float(getattr(result, "training_loss", 0.0) or 0.0),
        "steps": int(getattr(result, "global_step", 0) or 0),
        "n_rollouts": len(samples),
        "avg_reward": round(sum(s["reward"] for s in samples) / max(1, len(samples)), 4),
        "epochs": epochs,
    }
    return adapter_dir, metrics


def train_dpo(pairs, base_model, out_dir, epochs):
    """Direct Preference Optimization of a LoRA adapter — genuine preference-based
    RL. Optimizes the policy so the chosen (higher-reward) completion is preferred
    over the rejected one, relative to a frozen reference (the base with the LoRA
    disabled — no second model copy). CPU-feasible for a 1-bit base + small LoRA.
    Raises a clear, actionable error if the ML stack isn't installed."""
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import DPOConfig, DPOTrainer
    except ImportError as e:
        raise SystemExit(
            "[trainer] DPO needs the ML stack — install it (CPU is fine):\n"
            "    pip install -r scripts/requirements-trainer.txt\n"
            f"  (missing: {e.name}). Use --dry-run to mine pairs without it.")

    tok = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
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
    model = AutoModelForCausalLM.from_pretrained(
        base_model, trust_remote_code=True, torch_dtype=torch.float32)
    lora = LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
                      task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj"])
    adapter_dir = os.path.join(out_dir, f"tool_call-dpo-{int(time.time())}")
    cfg = DPOConfig(output_dir=adapter_dir, num_train_epochs=epochs,
                    per_device_train_batch_size=1, gradient_accumulation_steps=8,
                    learning_rate=5e-5, beta=DPO_BETA, logging_steps=10,
                    save_strategy="no", report_to=[], max_length=1024, max_prompt_length=512)
    # ref_model=None + peft_config: the reference is the base with adapters
    # disabled, so DPO needs no second full-model copy.
    trainer = DPOTrainer(model=model, ref_model=None, args=cfg, train_dataset=ds,
                         processing_class=tok, peft_config=lora)
    result = trainer.train()
    trainer.save_model(adapter_dir)
    tok.save_pretrained(adapter_dir)
    metrics = {
        "method": "dpo", "beta": DPO_BETA,
        "loss": float(getattr(result, "training_loss", 0.0) or 0.0),
        "steps": int(getattr(result, "global_step", 0) or 0),
        "n_pairs": len(pairs),
        "avg_margin": round(sum(p["margin"] for p in pairs) / max(1, len(pairs)), 4),
        "epochs": epochs,
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
    base_uri = os.getenv("STUDIO_TRAIN_ADAPTER_BASE_URI", os.path.abspath(OUT_DIR))
    uri = os.path.join(base_uri, os.path.basename(adapter_dir))
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
    ap = argparse.ArgumentParser(description="Studio online BitNet trainer (CPU worker).")
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
