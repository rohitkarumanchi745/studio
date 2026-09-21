"""Online (simultaneous) BitNet training loop — Studio's half.

Agent Lightning decouples the AGENT (many concurrent rollout producers) from the
TRAINER (consumes rollouts, updates the policy). They run at the same time:

    Studio agent ──rollouts──▶  trainer (GPU worker)  ──adapters──▶  Studio serving
      (produces)                (SFT / DPO / GRPO)                   (hot-swaps, CPU)
         ▲                                                               │
         └──────────────────── keeps serving with the newest ───────────┘

The hardware split is the opposite of the intuitive one, and it is worth stating
here because this docstring used to get it wrong. TRAINING wants a GPU: the
packed 1-bit repo cannot be fine-tuned at all (transformers refuses), so the
LoRA is trained on microsoft/bitnet-b1.58-2B-4T-bf16 — 4.8 GB of ordinary dense
master weights whose linears re-quantize to ternary on every forward. CPU/MPS
finishes but is slower by more than an order of magnitude (measured: ~170 s per
micro-batch at max_length=128 on an Apple M1). SERVING is the CPU half: stock
vLLM cannot load BitNet (vllm#17279, "not planned") and the supported runtime is
bitnet.cpp, whose ternary kernels are CPU kernels. See serving/README.md §1 and
scripts/README-training.md.

This module is the producer + adapter server: it streams reward-labeled rollouts
to the trainer, holds the registry of published LoRA adapters, and tells the
serving layer which to load — a GLOBAL tool-calling adapter plus an optional PER-USER style adapter,
composed per request. The trainer loop (scripts/train_online.py) polls the
stream, trains, and publishes new adapter versions here; serving picks them up on
the next call, so training and serving are genuinely simultaneous. The trainer's
heavy ML deps live in scripts/requirements-trainer.txt, out of the lean API image.
"""
import hashlib
import json
import os
import time
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, qcache
from .auth import current_user

router = APIRouter(prefix="/training", tags=["training"])

KINDS = ("tool_call", "user_style")   # global tool-calling policy · per-user style


def require_tool_adapter_sha256():
    return (os.getenv("STUDIO_REQUIRE_TOOL_ADAPTER_SHA256") or "").strip().lower() \
        in {"1", "true", "yes", "on"}


def init_tables():
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS training_adapters (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,          -- 'global' or a user_id
                kind TEXT NOT NULL,           -- 'tool_call' | 'user_style'
                version INTEGER NOT NULL,
                uri TEXT NOT NULL,            -- where serving loads it (path/URL/adapter name)
                sha256 TEXT,                  -- immutable artifact digest when available
                base_model TEXT,
                metrics TEXT,                 -- JSON: loss, reward, steps, n_rollouts
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_adapters_scope
                ON training_adapters(scope, kind, status);
            """
        )
        duplicate = c.execute(
            "SELECT 1 FROM training_adapters WHERE status='active' "
            "GROUP BY scope,kind HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        # An old database may contain a pre-fix race. Migration 11 reconciles
        # it before adding this constraint; a fresh/current database gets the
        # complete invariant immediately from the baseline.
        if not duplicate:
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_adapters_one_active "
                "ON training_adapters(scope,kind) WHERE status='active'")
        c.commit()


def _registry_lock_key(scope, kind):
    return int.from_bytes(
        hashlib.sha256(f"studio.training_adapters:{scope}:{kind}".encode()).digest()[:8],
        "big", signed=True)


def _lock_registry(c, scope, kind):
    """Serialize one scope/kind across threads, processes, and replicas."""
    if db.IS_PG:
        # Transaction-scoped: commit/rollback releases it even if publication
        # fails, and different scope/kind pairs can publish independently.
        c.execute("SELECT pg_advisory_xact_lock(?)", (_registry_lock_key(scope, kind),))
    else:
        c.execute("PRAGMA busy_timeout = 30000")
        c.execute("BEGIN IMMEDIATE")


def bootstrap_from_env():
    """Register one operator-provided trained adapter on a fresh deployment.

    This closes the otherwise manual registry gate for immutable cloud images.
    It is intentionally create-only: an environment change never supersedes a
    live adapter behind the operator's back. Normal upgrades still publish via
    the authenticated training API.
    """
    uri = (os.getenv("STUDIO_BOOTSTRAP_TOOL_ADAPTER_URI") or "").strip()
    if not uri:
        return None
    if len(uri) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in uri):
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_URI is invalid")
    parsed = urlsplit(uri)
    if parsed.scheme in {"http", "https"} and (not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_URI must be a stable URL without credentials")
    try:
        version = int((os.getenv("STUDIO_BOOTSTRAP_TOOL_ADAPTER_VERSION") or "1").strip())
    except ValueError:
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_VERSION must be an integer") from None
    if not 1 <= version <= 2**31 - 1:
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_VERSION is out of range")
    sha256 = (os.getenv("STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256") or "").strip().lower()
    if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256 must be 64 hexadecimal characters")
    sha256 = sha256 or None
    if require_tool_adapter_sha256() and not sha256:
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256 is required in strict adapter mode")
    aid = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"studio-bootstrap-tool-adapter:{version}:{sha256}:{uri}"))
    with db.connect() as c:
        _lock_registry(c, "global", "tool_call")
        active = c.execute(
            "SELECT * FROM training_adapters WHERE scope=? AND kind=? "
            "AND status='active' ORDER BY version DESC LIMIT 1",
            ("global", "tool_call"),
        ).fetchone()
        if active:
            active = dict(active)
            if active["uri"] != uri or int(active["version"]) != version \
                    or active.get("sha256") != sha256:
                raise RuntimeError(
                    "the configured bootstrap adapter does not match the active registry entry")
            aid = active["id"]
        else:
            c.execute("INSERT INTO training_adapters (id, scope, kind, version, uri, sha256, base_model, "
                      "metrics, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (aid, "global", "tool_call", version, uri, sha256,
                       (os.getenv("STUDIO_BITNET_BASE_MODEL") or "bitnet").strip(),
                       json.dumps({"source": "deployment_bootstrap"}), "active", time.time()))
        c.commit()
    return {"id": aid, "scope": "global", "kind": "tool_call",
            "version": version, "uri": uri,
            **({"sha256": sha256} if sha256 else {})}


def _admin(user):
    # The trainer authenticates as an admin (a service account); rollouts and
    # adapter control are never exposed to ordinary users.
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Training control is admin-only")


# ── Rollout stream: producer → trainer ──────────────────────────────────

def stream(since=0.0, limit=500):
    """Reward-labeled rollout revisions after ``since``.

    ``training_revision`` advances when explicit feedback replaces a heuristic
    reward, so an already-seen trace is emitted again and the trainer's
    ID-deduped pending/replay stores replace the stale label. Unlike a timestamp
    cursor, this monotonic database counter cannot skip tied rows at a page edge.
    """
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, created_at, COALESCE(updated_at, created_at) training_updated_at, "
            "training_revision, "
            "user_id, role, prompt, sql, chart_type, mode, reward, reward_source, source, "
            "tbl, meta FROM agent_traces WHERE training_revision > ? "
            "AND reward IS NOT NULL ORDER BY training_revision LIMIT ?",
            (since, limit)).fetchall()
    out = []
    for r in rows:
        meta = {}
        if r["meta"]:
            try:
                meta = json.loads(r["meta"])
            except ValueError:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        action = meta.get("action")
        if not isinstance(action, dict):
            action = {"sql": r["sql"], "chart_type": r["chart_type"]}
        out.append({
            "id": r["id"], "created_at": r["created_at"],
            "updated_at": r["training_updated_at"],
            "revision": r["training_revision"],
            "user_id": r["user_id"], "role": r["role"],
            "prompt": r["prompt"],
            # the action the decision-maker took (tool call), for tool-call training
            "action": action,
            "reward": r["reward"], "reward_source": r["reward_source"],
            # Which warehouse this rollout came from (dialect + schema regime).
            # The trainer conditions each sample on this source's skill file so a
            # Databricks sample never teaches the sqlite/demo policy, and vice
            # versa. Additive: prior keys are unchanged.
            "source": r["source"], "tbl": r["tbl"],
            "mode": r["mode"], "agents": meta.get("agents") or [],
            "history": meta.get("history") or [],
            "meta": meta, "run_id": meta.get("run_id"),
            "repairs_run_id": meta.get("repairs_run_id"),
            "execution_status": meta.get("status"),
        })
    cursor = out[-1]["revision"] if out else since
    return {"rollouts": out, "cursor": cursor, "count": len(out)}


# ── Adapter registry: trainer → serving ─────────────────────────────────

def publish(scope, kind, uri, base_model=None, metrics=None, sha256=None):
    """Register a freshly trained adapter and make it the active one for its
    (scope, kind). Prior versions are marked superseded — serving always loads
    the newest without a restart."""
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    sha256 = (sha256 or "").strip().lower() or None
    if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
        raise HTTPException(400, "sha256 must be 64 hexadecimal characters")
    if require_tool_adapter_sha256() and scope == "global" and kind == "tool_call" and not sha256:
        raise HTTPException(400, "sha256 is required for global tool_call adapters")
    with db.connect() as c:
        _lock_registry(c, scope, kind)
        prev = c.execute(
            "SELECT MAX(version) v FROM training_adapters WHERE scope=? AND kind=?",
            (scope, kind)).fetchone()
        version = (prev["v"] or 0) + 1
        c.execute("UPDATE training_adapters SET status='superseded' WHERE scope=? AND kind=? AND status='active'",
                  (scope, kind))
        aid = str(uuid.uuid4())
        c.execute("INSERT INTO training_adapters (id, scope, kind, version, uri, sha256, base_model, "
                  "metrics, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (aid, scope, kind, version, uri, sha256, base_model,
                   json.dumps(metrics or {}), "active", time.time()))
        c.commit()
    result = {"id": aid, "scope": scope, "kind": kind, "version": version, "uri": uri}
    if sha256:
        result["sha256"] = sha256
    return result


def _active(scope, kind):
    with db.connect() as c:
        r = c.execute("SELECT * FROM training_adapters WHERE scope=? AND kind=? AND status='active' "
                      "ORDER BY version DESC LIMIT 1", (scope, kind)).fetchone()
    return dict(r) if r else None


def active_adapters(user_id):
    """What serving loads for this user: the global tool-calling adapter plus
    this user's style adapter (if trained). Composed, not merged, so improving
    one never regresses the other."""
    out = {}
    tc = _active("global", "tool_call")
    if tc:
        out["tool_call"] = {"uri": tc["uri"], "version": tc["version"]}
        if tc.get("sha256"):
            out["tool_call"]["sha256"] = tc["sha256"]
    if user_id:
        us = _active(user_id, "user_style")
        if us:
            out["user_style"] = {"uri": us["uri"], "version": us["version"]}
            if us.get("sha256"):
                out["user_style"]["sha256"] = us["sha256"]
    return out


def status():
    with db.connect() as c:
        tc = _active("global", "tool_call")
        n_user = c.execute("SELECT COUNT(*) n FROM training_adapters WHERE kind='user_style' AND status='active'").fetchone()["n"]
        last_at = c.execute("SELECT MAX(created_at) t FROM training_adapters").fetchone()["t"] or 0
        fresh = c.execute(
            "SELECT COUNT(*) n FROM agent_traces WHERE reward IS NOT NULL "
            "AND COALESCE(updated_at, created_at) > ?", (last_at,)).fetchone()["n"]
    return {
        "tool_call_adapter": {"version": tc["version"], "uri": tc["uri"],
                              "sha256": tc.get("sha256"),
                              "metrics": json.loads(tc["metrics"] or "{}")} if tc else None,
        "user_adapters": n_user,
        "last_publish_at": last_at or None,
        "rollouts_since_last_train": fresh,
        "loop": "live" if (last_at and (fresh == 0)) else ("training-behind" if last_at else "idle"),
        "requires_tool_adapter_sha256": require_tool_adapter_sha256(),
        # BitNet's scope: how many use cases it now covers, and how many are
        # still being learned (handled by the frontier until they cross over).
        "scope": qcache.scope_stats(),
    }


# ── API (admin / trainer service account) ───────────────────────────────

@router.get("/rollouts")
def rollouts(since: float = 0.0, limit: int = 500, user=Depends(current_user)):
    """Trainer pulls new reward-labeled rollouts since a cursor."""
    _admin(user)
    return stream(since, max(1, min(limit, 2000)))


class AdapterIn(BaseModel):
    scope: str = "global"          # 'global' (tool_call) or a user_id (user_style)
    kind: str                      # 'tool_call' | 'user_style'
    uri: str
    sha256: str | None = None
    base_model: str | None = None
    metrics: dict | None = None


@router.post("/adapters", status_code=201)
def publish_adapter(body: AdapterIn, user=Depends(current_user)):
    """Trainer publishes a freshly trained adapter; serving hot-swaps to it."""
    _admin(user)
    if not body.uri.strip():
        raise HTTPException(400, "uri is required")
    res = publish(body.scope.strip(), body.kind, body.uri.strip(),
                  body.base_model, body.metrics, body.sha256)
    db.log_activity(user, "adapter_publish", prompt=f"{body.scope}/{body.kind} v{res['version']}")
    return res


@router.get("/adapters")
def list_adapters(user=Depends(current_user)):
    _admin(user)
    with db.connect() as c:
        rows = c.execute("SELECT id, scope, kind, version, uri, sha256, base_model, status, created_at "
                         "FROM training_adapters ORDER BY created_at DESC LIMIT 200").fetchall()
    return {"adapters": [dict(r) for r in rows]}


@router.get("/adapters/for-user/{uid}")
def adapters_for_user(uid: str, user=Depends(current_user)):
    _admin(user)
    return {"user_id": uid, "adapters": active_adapters(uid)}


@router.get("/online")
def online(user=Depends(current_user)):
    """Online-training status for the dashboard: current adapter, user adapters,
    how far the trainer is behind the stream, and BitNet's growing scope."""
    _admin(user)
    return status()
