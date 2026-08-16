"""Online (simultaneous) BitNet training loop — Studio's half.

Agent Lightning decouples the AGENT (many concurrent rollout producers) from the
TRAINER (consumes rollouts, updates the policy). They run at the same time:

    Studio agent ──rollouts──▶  trainer (CPU worker)  ──adapters──▶  Studio serving
      (produces)                (SFT / DPO / GRPO)                   (hot-swaps)
         ▲                                                               │
         └──────────────────── keeps serving with the newest ───────────┘

The trainer is a CPU worker — BitNet's 1-bit base is CPU-efficient and its LoRA
adapters are small, so no GPU is required (one only speeds it up). This module is
the producer + adapter server: it streams reward-labeled rollouts to the trainer,
holds the registry of published LoRA adapters, and tells the serving layer which
to load — a GLOBAL tool-calling adapter plus an optional PER-USER style adapter,
composed per request. The trainer loop (scripts/train_online.py) polls the
stream, trains, and publishes new adapter versions here; serving picks them up on
the next call, so training and serving are genuinely simultaneous. The trainer's
heavy ML deps live in scripts/requirements-trainer.txt, out of the lean API image.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/training", tags=["training"])

KINDS = ("tool_call", "user_style")   # global tool-calling policy · per-user style


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS training_adapters (
            id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,          -- 'global' or a user_id
            kind TEXT NOT NULL,           -- 'tool_call' | 'user_style'
            version INTEGER NOT NULL,
            uri TEXT NOT NULL,            -- where serving loads it (path/URL/adapter name)
            base_model TEXT,
            metrics TEXT,                 -- JSON: loss, reward, steps, n_rollouts
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_adapters_scope
            ON training_adapters(scope, kind, status);
        """
    )
    c.commit()
    c.close()


def _admin(user):
    # The trainer authenticates as an admin (a service account); rollouts and
    # adapter control are never exposed to ordinary users.
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Training control is admin-only")


# ── Rollout stream: producer → trainer ──────────────────────────────────

def stream(since=0.0, limit=500):
    """Reward-labeled rollouts after `since` (a created_at cursor), shaped as
    training samples. The trainer pulls incrementally and keeps the last cursor,
    so it trains on new experience as it arrives — concurrently with serving."""
    c = db._conn()
    rows = c.execute(
        "SELECT id, created_at, user_id, role, prompt, sql, chart_type, mode, reward, "
        "reward_source, meta FROM agent_traces WHERE created_at > ? AND reward IS NOT NULL "
        "ORDER BY created_at LIMIT ?", (since, limit)).fetchall()
    c.close()
    out = []
    for r in rows:
        meta = {}
        if r["meta"]:
            try:
                meta = json.loads(r["meta"])
            except ValueError:
                meta = {}
        out.append({
            "id": r["id"], "created_at": r["created_at"],
            "user_id": r["user_id"], "role": r["role"],
            "prompt": r["prompt"],
            # the action the decision-maker took (tool call), for tool-call training
            "action": {"sql": r["sql"], "chart_type": r["chart_type"]},
            "reward": r["reward"], "reward_source": r["reward_source"],
            "mode": r["mode"], "agents": meta.get("agents") or [],
        })
    cursor = out[-1]["created_at"] if out else since
    return {"rollouts": out, "cursor": cursor, "count": len(out)}


# ── Adapter registry: trainer → serving ─────────────────────────────────

def publish(scope, kind, uri, base_model=None, metrics=None):
    """Register a freshly trained adapter and make it the active one for its
    (scope, kind). Prior versions are marked superseded — serving always loads
    the newest without a restart."""
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    c = db._conn()
    prev = c.execute(
        "SELECT MAX(version) v FROM training_adapters WHERE scope=? AND kind=?",
        (scope, kind)).fetchone()
    version = (prev["v"] or 0) + 1
    c.execute("UPDATE training_adapters SET status='superseded' WHERE scope=? AND kind=? AND status='active'",
              (scope, kind))
    aid = str(uuid.uuid4())
    c.execute("INSERT INTO training_adapters (id, scope, kind, version, uri, base_model, "
              "metrics, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              (aid, scope, kind, version, uri, base_model,
               json.dumps(metrics or {}), "active", time.time()))
    c.commit()
    c.close()
    return {"id": aid, "scope": scope, "kind": kind, "version": version, "uri": uri}


def _active(scope, kind):
    c = db._conn()
    r = c.execute("SELECT * FROM training_adapters WHERE scope=? AND kind=? AND status='active' "
                  "ORDER BY version DESC LIMIT 1", (scope, kind)).fetchone()
    c.close()
    return dict(r) if r else None


def active_adapters(user_id):
    """What serving loads for this user: the global tool-calling adapter plus
    this user's style adapter (if trained). Composed, not merged, so improving
    one never regresses the other."""
    out = {}
    tc = _active("global", "tool_call")
    if tc:
        out["tool_call"] = {"uri": tc["uri"], "version": tc["version"]}
    if user_id:
        us = _active(user_id, "user_style")
        if us:
            out["user_style"] = {"uri": us["uri"], "version": us["version"]}
    return out


def status():
    c = db._conn()
    tc = _active("global", "tool_call")
    n_user = c.execute("SELECT COUNT(*) n FROM training_adapters WHERE kind='user_style' AND status='active'").fetchone()["n"]
    last_at = c.execute("SELECT MAX(created_at) t FROM training_adapters").fetchone()["t"] or 0
    fresh = c.execute("SELECT COUNT(*) n FROM agent_traces WHERE reward IS NOT NULL AND created_at > ?",
                      (last_at,)).fetchone()["n"]
    c.close()
    return {
        "tool_call_adapter": {"version": tc["version"], "uri": tc["uri"],
                              "metrics": json.loads(tc["metrics"] or "{}")} if tc else None,
        "user_adapters": n_user,
        "last_publish_at": last_at or None,
        "rollouts_since_last_train": fresh,
        "loop": "live" if (last_at and (fresh == 0)) else ("training-behind" if last_at else "idle"),
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
    base_model: str | None = None
    metrics: dict | None = None


@router.post("/adapters", status_code=201)
def publish_adapter(body: AdapterIn, user=Depends(current_user)):
    """Trainer publishes a freshly trained adapter; serving hot-swaps to it."""
    _admin(user)
    if not body.uri.strip():
        raise HTTPException(400, "uri is required")
    res = publish(body.scope.strip(), body.kind, body.uri.strip(),
                  body.base_model, body.metrics)
    db.log_activity(user, "adapter_publish", prompt=f"{body.scope}/{body.kind} v{res['version']}")
    return res


@router.get("/adapters")
def list_adapters(user=Depends(current_user)):
    _admin(user)
    c = db._conn()
    rows = c.execute("SELECT id, scope, kind, version, uri, base_model, status, created_at "
                     "FROM training_adapters ORDER BY created_at DESC LIMIT 200").fetchall()
    c.close()
    return {"adapters": [dict(r) for r in rows]}


@router.get("/adapters/for-user/{uid}")
def adapters_for_user(uid: str, user=Depends(current_user)):
    _admin(user)
    return {"user_id": uid, "adapters": active_adapters(uid)}


@router.get("/online")
def online(user=Depends(current_user)):
    """Online-training status for the dashboard: current adapter, user adapters,
    and how far the trainer is behind the live rollout stream."""
    _admin(user)
    return status()
