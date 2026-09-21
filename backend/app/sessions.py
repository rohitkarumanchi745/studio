"""Agent session serialization — save, resume, fork an agent's state.

A *conversation* stores the visible transcript. A *session* is the serialized
AGENT: everything needed to resume a run identically — model spec, source/table
scope, the full message transcript (not chat's lossy last-8 window), any pinned
per-user context, and a stable hash of the cacheable prefix.

Why the prefix hash matters on hosted APIs: you cannot dump and reload the raw
attention KV cache from Anthropic/OpenAI — it lives on their servers. The
reachable equivalent is *prompt caching* (agent._apply_prompt_cache marks the
stable system/skill prefix with cache_control). Re-sending an identical prefix
rebuilds that KV cache server-side and bills cache reads at ~10%. A session
persists exactly that prefix + its hash, so resuming a run reuses the provider
cache instead of re-processing the whole context. Cache/token counts from each
turn's usage metadata accumulate here so the reuse is visible and auditable.
"""
import hashlib
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/sessions", tags=["sessions"])


def init_tables():
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                conversation_id TEXT,
                title TEXT NOT NULL,
                model_spec TEXT,
                source TEXT,
                table_scope TEXT,
                messages TEXT NOT NULL,
                conversation_message_ids TEXT,
                is_fork INTEGER NOT NULL DEFAULT 0,
                pinned_context TEXT,
                prefix_hash TEXT,
                cache_prefix_len INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                tokens_in INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                turns INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON agent_sessions(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sessions_conv
                ON agent_sessions(conversation_id);
            """
        )
        c.commit()


# ── Serialization helpers ────────────────────────────────────────────────

def normalize_messages(items):
    """Normalize a transcript to [{role, text}] — role is user|assistant.

    Accepts chat history dicts ({role, text}) or raw stored messages
    ({role, content:{text}}). Empty-text entries are dropped so the prefix a
    session serializes is exactly what would be replayed."""
    out = []
    for m in items or []:
        role = m.get("role") or "user"
        role = "assistant" if role in ("assistant", "ai") else "user"
        text = m.get("text")
        if text is None:
            text = (m.get("content") or {}).get("text", "")
        text = (text or "").strip()
        if text:
            out.append({"role": role, "text": text})
    return out


def prefix_hash(model_spec, messages, pinned_context=None):
    """Stable id for the cacheable prefix. Identical (model, pinned, prefix)
    across sessions → the provider prompt cache is reused on replay."""
    payload = json.dumps(
        {"m": model_spec or "", "p": pinned_context or [], "msgs": messages},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _visible_conversation_entries(user, conversation_id):
    """Current governed conversation entries as ``[(id, normalized), ...]``.

    None means the user no longer has access. The lazy import avoids the
    chat->sessions checkpoint cycle.
    """
    access = db.conversation_access(conversation_id, user["id"])
    if access is None:
        return None
    from . import chat
    out = []
    for message in chat._visible_messages(conversation_id, user, access):
        normalized = normalize_messages([message])
        if normalized:
            out.append((message["id"], normalized[0]))
    return out


def _bind_conversation_messages(user, conversation_id, messages, requested_ids=None):
    """Prove a session transcript is an exact subset of governed chat rows.

    New snapshots keep the row ids, which makes a fork a stable branch while
    still letting every later read re-run chat's policy gate. With no ids (the
    public save API and pre-migration rows), an exact ordered subsequence match
    binds the supplied transcript without trusting client text.
    """
    entries = _visible_conversation_entries(user, conversation_id)
    if entries is None:
        return None
    messages = normalize_messages(messages)
    if requested_ids is not None:
        if (not isinstance(requested_ids, list)
                or len(requested_ids) != len(messages)
                or not all(isinstance(mid, str) and mid for mid in requested_ids)):
            return None
        by_id = {mid: message for mid, message in entries}
        if any(by_id.get(mid) != message
               for mid, message in zip(requested_ids, messages)):
            return None
        return list(requested_ids)

    # Greedy ordered-subsequence matching preserves explicit subset snapshots
    # and handles repeated text deterministically.
    bound, cursor = [], 0
    for wanted in messages:
        while cursor < len(entries) and entries[cursor][1] != wanted:
            cursor += 1
        if cursor >= len(entries):
            return None
        bound.append(entries[cursor][0])
        cursor += 1
    return bound


def _accumulate_usage(row_usage, usage):
    """Fold one turn's usage metadata into the running totals."""
    u = usage or {}
    return {
        "tokens_in": row_usage.get("tokens_in", 0) + int(u.get("input_tokens", 0) or 0),
        "tokens_out": row_usage.get("tokens_out", 0) + int(u.get("output_tokens", 0) or 0),
        "cache_read_tokens": row_usage.get("cache_read_tokens", 0) + int(u.get("cache_read_tokens", 0) or 0),
        "cache_write_tokens": row_usage.get("cache_write_tokens", 0) + int(u.get("cache_write_tokens", 0) or 0),
    }


# ── Snapshot / resume / fork ─────────────────────────────────────────────

def snapshot(user, *, messages, session_id=None, conversation_id=None, title=None,
             model_spec=None, source=None, table_scope=None, pinned_context=None,
             usage=None, status="active", count_turn=False,
             conversation_message_ids=None, is_fork=False):
    """Upsert a session snapshot. Called after each agent turn (auto-checkpoint,
    count_turn=True) or explicitly by the user. Accumulates cache/token usage
    across turns; `turns` counts checkpointed turns even in fallback mode where
    no tokens are billed."""
    msgs = normalize_messages(messages)
    bound_ids = None
    if conversation_id:
        bound_ids = _bind_conversation_messages(
            user, conversation_id, msgs, conversation_message_ids)
        if bound_ids is None:
            raise ValueError(
                "Conversation-backed session messages must match currently visible chat rows")
    elif conversation_message_ids is not None:
        raise ValueError("Conversation message ids require a conversation_id")
    ph = prefix_hash(model_spec, msgs, pinned_context)
    now = time.time()
    with db.connect() as c:
        existing = None
        if session_id:
            existing = c.execute("SELECT * FROM agent_sessions WHERE id=? AND user_id=?",
                                 (session_id, user["id"])).fetchone()
        # An explicit id means "create/update THIS session".  Fork supplies a
        # fresh id while deliberately retaining the parent conversation link;
        # falling back to the conversation lookup in that case would overwrite
        # the original session instead of creating a governed branch.
        if existing is None and conversation_id and not session_id:
            existing = c.execute(
                "SELECT * FROM agent_sessions WHERE conversation_id=? AND user_id=? "
                "AND is_fork=0 ORDER BY created_at ASC LIMIT 1",
                (conversation_id, user["id"])).fetchone()

        if existing is not None:
            row = dict(existing)
            totals = _accumulate_usage(row, usage)
            c.execute(
                "UPDATE agent_sessions SET messages=?, model_spec=?, source=?, table_scope=?, "
                "conversation_message_ids=?, pinned_context=?, prefix_hash=?, "
                "cache_prefix_len=?, status=?, "
                "tokens_in=?, tokens_out=?, cache_read_tokens=?, cache_write_tokens=?, "
                "turns=?, updated_at=?, title=? WHERE id=?",
                (json.dumps(msgs), model_spec or row.get("model_spec"),
                 source or row.get("source"), table_scope or row.get("table_scope"),
                 json.dumps(bound_ids) if bound_ids is not None else None,
                 json.dumps(pinned_context or json.loads(row.get("pinned_context") or "[]")),
                 ph, len(msgs), status,
                 totals["tokens_in"], totals["tokens_out"],
                 totals["cache_read_tokens"], totals["cache_write_tokens"],
                 row.get("turns", 0) + (1 if (usage or count_turn) else 0), now,
                 title or row.get("title"), row["id"]),
            )
            sid = row["id"]
        else:
            sid = session_id or str(uuid.uuid4())
            totals = _accumulate_usage({}, usage)
            c.execute(
                "INSERT INTO agent_sessions (id, user_id, conversation_id, title, model_spec, "
                "source, table_scope, messages, conversation_message_ids, is_fork, pinned_context, "
                "prefix_hash, cache_prefix_len, "
                "status, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens, turns, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, user["id"], conversation_id, (title or "Session")[:120], model_spec,
                 source, table_scope, json.dumps(msgs),
                 json.dumps(bound_ids) if bound_ids is not None else None,
                 1 if is_fork else 0,
                 json.dumps(pinned_context or []),
                 ph, len(msgs), status, totals["tokens_in"], totals["tokens_out"],
                 totals["cache_read_tokens"], totals["cache_write_tokens"],
                 1 if (usage or count_turn) else 0, now, now),
            )
        c.commit()
    return sid


def _row(r):
    d = dict(r)
    for k in ("messages", "pinned_context"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                d[k] = [] if k == "pinned_context" else []
    if d.get("conversation_message_ids") is not None:
        try:
            parsed = json.loads(d["conversation_message_ids"])
            d["conversation_message_ids"] = parsed if isinstance(parsed, list) else None
        except (TypeError, ValueError):
            d["conversation_message_ids"] = None
    d["cache_hit_ratio"] = round(
        d["cache_read_tokens"] / d["tokens_in"], 3) if d.get("tokens_in") else 0.0
    return d


def _own_or_404(sid, user):
    with db.connect() as c:
        row = c.execute("SELECT * FROM agent_sessions WHERE id=?", (sid,)).fetchone()
    if row is None or dict(row)["user_id"] != user["id"]:
        raise HTTPException(404, "Session not found")  # 404, not 403 — no oracle
    return _row(row)


def _current_conversation_view(row, user):
    """Re-govern the exact conversation rows captured by this snapshot.

    Session rows are durable prompt-cache state, so returning their serialized
    transcript verbatim would bypass chat's read-time RBAC/governance checks:
    a policy could redact a stored answer in the conversation while GET,
    resume, or fork kept replaying its old summary from ``agent_sessions``.
    Rebuild from chat's one visibility gate on every read, but select only the
    stored row ids: later turns in the original conversation must never change
    an old snapshot or fork. A session that no longer has access, or whose
    binding cannot be proven, fails closed to an empty transcript.

    The import is deliberately lazy because chat imports sessions for
    auto-checkpointing.
    """
    if not row.get("conversation_id"):
        return row
    out = dict(row)
    entries = _visible_conversation_entries(user, row["conversation_id"])
    ids = row.get("conversation_message_ids")
    if entries is None:
        messages, ids = [], []
    else:
        # Upgrade-compatible read for rows created before migration 12: bind
        # the serialized snapshot only when it is still an exact visible
        # subsequence. Never replace it with the conversation's full transcript.
        if ids is None:
            ids = _bind_conversation_messages(
                user, row["conversation_id"], row.get("messages") or [])
        by_id = {mid: message for mid, message in entries}
        if ids is None or any(mid not in by_id for mid in ids):
            messages, ids = [], []
        else:
            messages = [by_id[mid] for mid in ids]
    out["messages"] = messages
    out["conversation_message_ids"] = ids
    out["prefix_hash"] = prefix_hash(
        out.get("model_spec"), messages, out.get("pinned_context"))
    out["cache_prefix_len"] = len(messages)
    return out


def _readable(sid, user):
    return _current_conversation_view(_own_or_404(sid, user), user)


# ── API ──────────────────────────────────────────────────────────────────

@router.get("")
def listing(user=Depends(current_user)):
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, title, conversation_id, model_spec, source, table_scope, status, "
            "cache_prefix_len, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens, "
            "turns, created_at, updated_at FROM agent_sessions WHERE user_id=? "
            "ORDER BY updated_at DESC LIMIT 100", (user["id"],)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["cache_hit_ratio"] = round(
            d["cache_read_tokens"] / d["tokens_in"], 3) if d.get("tokens_in") else 0.0
        out.append(d)
    return {"sessions": out}


@router.get("/{sid}")
def get(sid: str, user=Depends(current_user)):
    """Full serialized snapshot — the state to rehydrate an agent from."""
    return _readable(sid, user)


class SnapshotIn(BaseModel):
    conversation_id: str | None = None
    title: str | None = None
    model_spec: str | None = None
    source: str | None = None
    table_scope: str | None = None
    messages: list = []
    pinned_context: list | None = None


@router.post("", status_code=201)
def save(body: SnapshotIn, user=Depends(current_user)):
    """Explicitly serialize a session from the client (e.g. a manual 'save
    context' from the chat view)."""
    try:
        sid = snapshot(user, messages=body.messages, conversation_id=body.conversation_id,
                       title=body.title, model_spec=body.model_spec, source=body.source,
                       table_scope=body.table_scope, pinned_context=body.pinned_context)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.log_activity(user, "session_save", prompt=(body.title or "session")[:120])
    return get(sid, user)


@router.post("/{sid}/resume")
def resume(sid: str, user=Depends(current_user)):
    """Rehydrate a session: return the state to continue from and mark active.
    The prefix_hash tells the caller which cached prefix a replay will reuse."""
    d = _readable(sid, user)
    with db.connect() as c:
        c.execute("UPDATE agent_sessions SET status='active', updated_at=? WHERE id=?",
                  (time.time(), sid))
        c.commit()
    db.log_activity(user, "session_resume", prompt=d["title"])
    return {
        "id": sid,
        "conversation_id": d.get("conversation_id"),
        "model_spec": d.get("model_spec"),
        "source": d.get("source"),
        "table_scope": d.get("table_scope"),
        "messages": d.get("messages") or [],
        "pinned_context": d.get("pinned_context") or [],
        "prefix_hash": d.get("prefix_hash"),
        "cache_prefix_len": d.get("cache_prefix_len"),
        "cache_read_tokens": d.get("cache_read_tokens"),
    }


@router.post("/{sid}/fork")
def fork(sid: str, user=Depends(current_user)):
    """Branch a new session from this snapshot (shared cacheable prefix, so the
    branch's first turn still hits the provider cache). Usage counters reset."""
    d = _readable(sid, user)
    try:
        new_id = snapshot(user, session_id=str(uuid.uuid4()),
                          conversation_id=d.get("conversation_id"),
                          conversation_message_ids=d.get("conversation_message_ids"),
                          is_fork=True,
                          messages=d.get("messages") or [],
                          title=f"{d['title']} (fork)", model_spec=d.get("model_spec"),
                          source=d.get("source"), table_scope=d.get("table_scope"),
                          pinned_context=d.get("pinned_context"))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return get(new_id, user)


@router.delete("/{sid}")
def remove(sid: str, user=Depends(current_user)):
    _own_or_404(sid, user)
    with db.connect() as c:
        c.execute("DELETE FROM agent_sessions WHERE id=?", (sid,))
        c.commit()
    return {"deleted": True}
