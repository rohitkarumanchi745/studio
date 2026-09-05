"""Chat: conversations, the ask endpoint driving the agent, fresh-data rerun,
email reports, and the per-user activity audit log."""
import json
import logging
import os
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import (agent, db, email_service, gateway, governance, jobs, keys,
               lightning, orchestrator, progress, qcache, queryguard, rbac,
               roster, router as model_router, semantic, sessions, skills)
from .auth import current_user
from .matching import match_tables
from .sources import connector_or_400

log = logging.getLogger("studio.chat")

router = APIRouter(tags=["chat"])

# Background tasks let a user start a question in one chat, move to another, and
# be notified (a blue dot on the conversation) when it finishes. They run as
# durable "chat_turn" jobs (jobs.py): the request records the user's turn and
# enqueues a JSON payload, and whichever worker claims it rebuilds the context
# and runs the turn — so a restart never loses an in-flight question.


def init_tables():
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_tasks (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                prompt TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                error TEXT,
                seen INTEGER NOT NULL DEFAULT 0,
                steps TEXT,
                user_message_id TEXT,
                created_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_chat_tasks_user
                ON chat_tasks(user_id, conversation_id);
            CREATE INDEX IF NOT EXISTS idx_chat_tasks_status
                ON chat_tasks(status, created_at);
            """
        )
        # steps holds the live activity feed (progress.py); user_message_id is
        # the turn this task answers (_already_answered). Databases created
        # before either column existed get them from migrations 3 and 6
        # (app/migrations.py). idx_chat_tasks_status serves the orphan sweep
        # (_fail_orphan_tasks), which runs on every reclaim pass and looks up
        # old rows still marked 'running'; it needs no migration because
        # CREATE INDEX IF NOT EXISTS runs here on every boot, new database or
        # old — only the schema a CREATE TABLE owns has to be migrated.
        c.commit()


class Ask(BaseModel):
    prompt: str
    source: str
    table: str  # a table name, or "*" for whole-source chat
    tables: Optional[List[str]] = None  # multi-select: restrict to these tables
    conversation_id: Optional[str] = None
    model: Optional[str] = None  # user-selected model spec from GET /models
    # "All sources" only: run even if the prompt names a table that exists in
    # several sources (the user chose "both, side by side" on a clarification).
    allow_ambiguous: bool = False


@router.get("/agents")
def agents(user=Depends(current_user)):
    """The named agent crew for this user: a worker per accessible source, plus
    the Aggregator/Orchestrator, and whether a question fans out."""
    return roster.summary(user)


@router.get("/models")
def models(user=Depends(current_user)):
    """The model menu (Claude, GPT, …) the company offers. Configure with
    STUDIO_MODELS; availability reflects which provider keys are set."""
    return agent.available_models(user)


class KeyIn(BaseModel):
    provider: str
    api_key: str


@router.get("/settings/keys")
def get_keys(user=Depends(current_user)):
    """Which providers this user has connected. Never returns a key."""
    return {"keys": keys.list_keys(user["id"]), "providers": list(keys.PROVIDERS),
            "server_keys": [p for p in keys.PROVIDERS
                            if os.getenv(agent._KEY_FOR_PROVIDER[p])]}


@router.post("/settings/keys", status_code=201)
def put_key(body: KeyIn, user=Depends(current_user)):
    provider = body.provider.strip().lower()
    api_key = body.api_key.strip()
    if provider not in keys.PROVIDERS:
        raise HTTPException(400, f"provider must be one of {', '.join(keys.PROVIDERS)}")
    if not api_key:
        raise HTTPException(400, "API key cannot be empty")
    ok, detail = keys.verify(provider, api_key)
    if not ok:
        raise HTTPException(400, detail)
    keys.set_key(user["id"], provider, api_key)
    # Log the event, never the secret.
    db.log_activity(user, "api_key_connected", prompt=provider)
    return {"keys": keys.list_keys(user["id"])}


@router.delete("/settings/keys/{provider}")
def drop_key(provider: str, user=Depends(current_user)):
    keys.delete_key(user["id"], provider.strip().lower())
    db.log_activity(user, "api_key_removed", prompt=provider)
    return {"keys": keys.list_keys(user["id"])}


@router.get("/conversations")
def conversations(user=Depends(current_user)):
    convs = db.list_conversations(user["id"])
    states = _task_states(user["id"])
    for c in convs:
        s = states.get(c["id"], {})
        c["running"] = s.get("running", 0)   # a task is still working here
        c["unseen"] = s.get("unseen", 0)     # finished, not yet opened → blue dot
    return convs


@router.get("/conversations/{cid}/messages")
def messages(cid: str, user=Depends(current_user)):
    access = _own_or_404(cid, user)
    _mark_seen(cid, user["id"])   # opening the chat clears its blue dot
    return _visible_messages(cid, user, access)


@router.delete("/conversations/{cid}")
def remove(cid: str, user=Depends(current_user)):
    # Deleting destroys it for everyone it is shared with — owner only.
    _own_or_404(cid, user, need="owner")
    db.delete_conversation(cid)
    with db.connect() as c:   # drop this chat's background-task rows too (no orphans)
        c.execute("DELETE FROM chat_tasks WHERE conversation_id=?", (cid,))
        c.commit()
    return {"deleted": True}


class Rename(BaseModel):
    title: str


@router.patch("/conversations/{cid}")
def rename(cid: str, body: Rename, user=Depends(current_user)):
    """Rename a conversation. Collaborators with edit rights may rename too."""
    _own_or_404(cid, user, need="edit")
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Title cannot be empty")
    db.rename_conversation(cid, title)
    return {"id": cid, "title": title[:80]}


# ── Folders: personal sidebar organization for a user's own chats ────────

class FolderIn(BaseModel):
    name: str


class MoveIn(BaseModel):
    folder_id: Optional[str] = None   # None → back to the root (unfiled)


def _folder_or_404(fid, user):
    with db.connect() as c:
        r = c.execute("SELECT * FROM conversation_folders WHERE id=?", (fid,)).fetchone()
    if r is None or dict(r)["user_id"] != user["id"]:
        raise HTTPException(404, "Folder not found")   # 404, not 403 — no oracle
    return dict(r)


def _reject_duplicate_folder(name, user, ignore_id=None):
    """Two same-named folders would be indistinguishable in the sidebar and in
    the Move-to menu, so names are unique per user (case-insensitive)."""
    with db.connect() as c:
        rows = c.execute("SELECT id, name FROM conversation_folders WHERE user_id=?",
                         (user["id"],)).fetchall()
    for r in rows:
        d = dict(r)
        if d["id"] != ignore_id and d["name"].strip().lower() == name.lower():
            raise HTTPException(400, f"You already have a folder named '{d['name']}'")


@router.get("/folders")
def folders(user=Depends(current_user)):
    with db.connect() as c:
        rows = c.execute("SELECT id, name, created_at FROM conversation_folders "
                         "WHERE user_id=? ORDER BY name", (user["id"],)).fetchall()
    return {"folders": [dict(r) for r in rows]}


@router.post("/folders", status_code=201)
def create_folder(body: FolderIn, user=Depends(current_user)):
    name = (body.name or "").strip()[:60]
    if not name:
        raise HTTPException(400, "Folder name cannot be empty")
    _reject_duplicate_folder(name, user)
    fid = str(uuid.uuid4())
    with db.connect() as c:
        c.execute("INSERT INTO conversation_folders (id, user_id, name, created_at) "
                  "VALUES (?,?,?,?)", (fid, user["id"], name, time.time()))
        c.commit()
    return {"id": fid, "name": name}


@router.patch("/folders/{fid}")
def rename_folder(fid: str, body: FolderIn, user=Depends(current_user)):
    _folder_or_404(fid, user)
    name = (body.name or "").strip()[:60]
    if not name:
        raise HTTPException(400, "Folder name cannot be empty")
    _reject_duplicate_folder(name, user, ignore_id=fid)
    with db.connect() as c:
        c.execute("UPDATE conversation_folders SET name=? WHERE id=?", (name, fid))
        c.commit()
    return {"id": fid, "name": name}


@router.delete("/folders/{fid}")
def delete_folder(fid: str, user=Depends(current_user)):
    """Delete a folder. Its chats are unfiled back to the root, never deleted."""
    _folder_or_404(fid, user)
    with db.connect() as c:
        c.execute("UPDATE conversations SET folder_id=NULL WHERE folder_id=? AND user_id=?",
                  (fid, user["id"]))
        c.execute("DELETE FROM conversation_folders WHERE id=?", (fid,))
        c.commit()
    return {"deleted": True}


@router.post("/conversations/{cid}/folder")
def move_conversation(cid: str, body: MoveIn, user=Depends(current_user)):
    """File a conversation into one of YOUR folders (or None to unfile).
    Owner only: filing is personal — a collaborator organizing their sidebar
    must not reshuffle the owner's."""
    _own_or_404(cid, user, need="owner")
    if body.folder_id is not None:
        _folder_or_404(body.folder_id, user)   # must be the caller's folder
    with db.connect() as c:
        c.execute("UPDATE conversations SET folder_id=? WHERE id=?", (body.folder_id, cid))
        c.commit()
    return {"id": cid, "folder_id": body.folder_id}


class ShareIn(BaseModel):
    email: str
    permission: str = "edit"


@router.get("/conversations/{cid}/shares")
def shares(cid: str, user=Depends(current_user)):
    access = _own_or_404(cid, user)
    return {"shares": db.list_conversation_shares(cid), "can_share": access == "owner"}


@router.post("/conversations/{cid}/shares", status_code=201)
def add_share(cid: str, body: ShareIn, user=Depends(current_user)):
    """Share with another Studio user by email. Owner only — a collaborator
    must not be able to widen access they were merely granted."""
    _own_or_404(cid, user, need="owner")
    if body.permission not in ("view", "edit"):
        raise HTTPException(400, "permission must be 'view' or 'edit'")
    target = db.get_user_by_email(body.email.strip().lower())
    if not target:
        raise HTTPException(404, "No Studio user with that email")
    if target["id"] == user["id"]:
        raise HTTPException(400, "That conversation is already yours")
    db.share_conversation(cid, target["id"], body.permission)
    db.log_activity(user, "conversation_share", prompt=f"{body.email} ({body.permission})")
    return {"shares": db.list_conversation_shares(cid), "can_share": True,
            "hidden_for_recipient": _hidden_count(cid, target["role"])}


@router.delete("/conversations/{cid}/shares/{share_user_id}")
def remove_share(cid: str, share_user_id: str, user=Depends(current_user)):
    _own_or_404(cid, user, need="owner")
    db.unshare_conversation(cid, share_user_id)
    return {"shares": db.list_conversation_shares(cid), "can_share": True}


def _scope(body, user):
    """Validate a turn and resolve its scope: the model, and for a single
    source the connector, the tables the role may see and their schemas.
    Raises HTTPException on validation/RBAC errors so both the sync path and
    the background enqueue fail fast, before anything is written. Pure — no
    writes — so a worker can call it again to rebuild a job's context."""
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Empty prompt")

    model = None
    if body.model:
        if body.model not in {m["spec"] for m in agent.available_models(user)}:
            raise HTTPException(400, f"Model '{body.model}' is not offered")
        model = body.model

    # source "*": the orchestrator fans out across per-database agents.
    if body.source == "*":
        return {"mode": "*", "model": model, "prompt": prompt,
                "allow_ambiguous": bool(getattr(body, "allow_ambiguous", False))}

    if not rbac.can_access(user["role"], body.source, body.table):
        raise HTTPException(403, "Your role has no access to this table")
    connector = connector_or_400(body.source)
    try:
        all_tables = connector.list_tables()
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")
    allowed = rbac.allowed_tables(user["role"], body.source, all_tables)
    if not allowed:
        raise HTTPException(403, "Your role has no access to this source")

    # Scope: an explicit multi-selection, one table, or the whole source ("*").
    selection = [t for t in (body.tables or []) if t]
    if selection:
        denied = [t for t in selection if t not in allowed]
        if denied:
            raise HTTPException(403, f"Your role has no access to: {', '.join(denied)}")
        allowed = [t for t in allowed if t in selection]
        table_param = "*"
        table_label = ", ".join(allowed)
    else:
        if body.table != "*" and body.table not in allowed:
            raise HTTPException(403, "Your role has no access to this table")
        table_param = body.table
        table_label = body.table

    try:
        if table_param == "*":
            from . import util
            tabs = allowed[:10]
            cols = util.pmap(connector.get_schema, tabs)   # independent → parallel
            if any(c is None for c in cols):
                raise RuntimeError("schema fetch failed")
            schemas = dict(zip(tabs, cols))
        else:
            schemas = {table_param: connector.get_schema(table_param)}
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")

    return {"mode": "normal", "model": model, "prompt": prompt,
            "source": body.source, "connector": connector, "all_tables": all_tables,
            "allowed": allowed, "schemas": schemas, "table_param": table_param,
            "table_label": table_label}


def _record_user_turn(body, user, scope=None):
    """Request-time half of a turn: validate (via _scope), resolve/create the
    conversation and append the user message. Everything here happens
    exactly once, at request time, whichever process later runs the turn.
    Returns (conversation id, user message id) — the message id IDENTIFIES
    this turn, which is how a background task later recognises its own answer
    (_already_answered). `scope` lets the sync path reuse the validation it
    already ran instead of fetching schemas twice."""
    scope = scope or _scope(body, user)
    prompt = scope["prompt"]
    cid, _history = _conversation(body.conversation_id, user, prompt)
    if scope["mode"] == "*":
        mid = db.add_message(cid, "user", {"text": prompt, "source": "*",
                                           "table": "all sources",
                                           "author_role": user["role"],
                                           "allow_ambiguous": scope["allow_ambiguous"]})
    else:
        mid = db.add_message(cid, "user", {"text": prompt, "source": body.source,
                                           "table": scope["table_label"],
                                           "author_role": user["role"]})
    return cid, mid


def _build_ctx(body, user, cid, scope=None):
    """Execution context for _run_turn, rebuilt from ids: the connector,
    schemas and history are re-derived (the connector is a live object and
    never crosses the queue). Pure — a worker calls this after
    _record_user_turn already wrote the user message, so the history drops
    that trailing message: a turn's own prompt is not its context."""
    scope = scope or _scope(body, user)
    _cid, history = _conversation(cid, user, scope["prompt"], recorded=True)
    ctx = dict(scope)
    ctx.update({"cid": cid, "history": history})
    return ctx


def _prepare(body, user):
    """Synchronous turn setup: validate, write the user message, build the
    context — kept as the one-call form for the sync endpoint."""
    scope = _scope(body, user)
    cid, _mid = _record_user_turn(body, user, scope=scope)
    return _build_ctx(body, user, cid, scope=scope)


def _and(names):
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _resolved_tables(cid):
    """Shared tables this conversation already chose to query across ALL sources
    ("both / all, side by side"). Remembered on the assistant result of that
    turn, so a later prompt naming the same table isn't asked again — while a
    different shared table still is."""
    out = set()
    try:
        for m in db.list_messages(cid):
            if m["role"] == "assistant":
                out.update(m["content"].get("resolved_tables") or [])
    except Exception:
        pass
    return out


def _answer(ctx, result):
    """Append the turn's assistant message. A BACKGROUND turn stamps the id of
    the user message it answers (ctx["reply_to"]) so a retry can recognise its
    own answer among several concurrent turns' — see _already_answered. A
    synchronous turn has no task and stamps nothing.

    This is the last safe point before a turn becomes visible, so it is where
    the two defences against a DOUBLE answer sit. Two attempts of the same
    chat_turn job really can be running at once: reclaim_stale() gives the job
    to a second worker when the first stops heartbeating, and the first may
    merely have been slow.

      1. COOPERATIVE — jobs.check_claim() raises ClaimLost if this thread's
         claim is already gone, so we abandon before writing anything. It is
         only a courtesy: nothing can preempt a thread that does not check.
      2. ABSOLUTE — reply_to is a column with a UNIQUE index, so if the other
         attempt got there first the INSERT is refused, add_message returns
         None, and we discard our answer. This is the guarantee; (1) only
         saves the wasted work.
    """
    reply_to = ctx.get("reply_to")
    if not reply_to:
        return db.add_message(ctx["cid"], "assistant", result)
    result["reply_to"] = reply_to
    jobs.check_claim()
    mid = db.add_message(ctx["cid"], "assistant", result, reply_to=reply_to)
    if mid is None:
        raise jobs.ClaimLost(
            f"user turn {reply_to} was answered by another attempt while this one ran")
    return mid


def _clarify_turn(ctx, user, clash, t0):
    """A table the question names exists in several sources: record an
    assistant message that ASKS which one, carrying the choices the UI renders
    as chips (one per source, plus all of them side by side). No agent runs,
    nothing is queried, no rollout is recorded and no session turn is counted —
    this turn is a question, not an answer. _conversation() also keeps the
    exchange out of the model's history, since the re-ask repeats the question."""
    cid, prompt = ctx["cid"], ctx["prompt"]
    progress.emit("asking which source to use")
    by_source = {}
    for c in clash:
        for s in c["sources"]:
            by_source.setdefault(s, []).append(c["table"])
    srcs = sorted(by_source)
    where = "; ".join(f"`{c['table']}` is in {_and(c['sources'])}" for c in clash)
    text = (f"{where}. Which source should I use? Pick one, or ask "
            f"{'both' if len(srcs) == 2 else f'all {len(srcs)}'} side by side.")
    result = {
        "text": text, "sql": None, "columns": [], "rows": [], "chart": None,
        "panels": [], "email": None, "errors": [], "mode": "clarify", "model": None,
        "source": "*", "table": "all sources", "matched_tables": [], "agents_used": [],
        "clarify": {
            "prompt": prompt, "table": clash[0]["table"], "conflicts": clash,
            "options": [{"source": s, "tables": by_source[s]} for s in srcs]
                       + [{"source": "*", "tables": [c["table"] for c in clash]}],
        },
        "author_role": user["role"],
    }
    result["inputs"] = _query_inputs(result)
    _answer(ctx, result)
    db.log_activity(user, "chat", prompt=prompt, source="*", table=clash[0]["table"],
                    mode="clarify", ok=True, duration_ms=int((time.time() - t0) * 1000))
    return result


def _run_turn(ctx, user):
    """Heavy half of a turn: run the agent, append the assistant message, trace,
    and checkpoint. Pure work off the prepared context — safe in a thread."""
    cid, model, prompt = ctx["cid"], ctx["model"], ctx["prompt"]
    t0 = time.time()

    if ctx["mode"] == "*":
        # Same-named table in several sources? Don't guess — ask, unless the
        # user already chose "both, side by side" (allow_ambiguous).
        sources = orchestrator.accessible_sources(user)
        clash = orchestrator.ambiguous_tables(prompt, sources)
        if clash and not ctx.get("allow_ambiguous"):
            # Tables this conversation already chose to query across all sources
            # are settled; only a NEW shared table asks again.
            resolved = _resolved_tables(cid)
            clash = [c for c in clash if c["table"] not in resolved]
            if clash:
                return _clarify_turn(ctx, user, clash, t0)
        result = orchestrator.run_orchestrated(prompt, user, ctx["history"], model,
                                               conversation_id=cid, sources=sources)
        if clash and ctx.get("allow_ambiguous"):
            result["resolved_tables"] = [c["table"] for c in clash]   # sticky for this chat
        result.setdefault("source", "*")
        result["table"] = "all sources"
        result["matched_tables"] = []
        result["inputs"] = _query_inputs(result)
        tid = lightning.record_chat_trace(user, cid, prompt, result, int((time.time() - t0) * 1000),
                                          history=ctx["history"])
        if tid:
            result["trace_id"] = tid
        result["author_role"] = user["role"]
        _answer(ctx, result)
        db.log_activity(
            user, "chat", prompt=prompt, source="*",
            table=",".join(result.get("agents_used") or []) or "all sources",
            sql=result.get("sql"), mode=result.get("mode"),
            row_count=len(result.get("rows") or []),
            ok=not (result.get("text") or "").startswith(("(Agent error", "(Orchestrator error")),
            duration_ms=int((time.time() - t0) * 1000))
        _checkpoint(user, cid, result, model, "*", "all sources")
        return result

    connector = ctx["connector"]
    skill_md = skills.get_skill(connector, user["role"], ctx["allowed"], ctx["schemas"])

    def _run(spec, kag_first=False):
        return agent.run_agent(
            prompt=prompt, connector=connector, table=ctx["table_param"],
            allowed_tables=ctx["allowed"], schemas=ctx["schemas"], history=ctx["history"],
            user=user, model=spec, skill_md=skill_md, kag_first=kag_first)

    # Explicit engine chosen in the model selector: honor it directly, ahead of the
    # automatic tiers. 'bitnet' forces the self-hosted engine; 'kag' forces a
    # documents-first turn grounded in the user's own knowledge collections.
    if model == "bitnet" and model_router.bitnet_ready(user):
        progress.emit("routing to the self-hosted BitNet engine")
        result = _run(model_router.bitnet_spec())
        result.setdefault("served_by", "bitnet")
    elif model == "kag":
        progress.emit("searching your knowledge collections")
        result = _run(None, kag_first=True)
        result.setdefault("served_by", "kag")
    # Tier -1 — semantic layer: if the prompt resolves to admin-defined metrics,
    # answer from the ONE canonical definition. Deterministic, needs no model,
    # and guarantees every phrasing of the same question returns the same number.
    # Anything it can't resolve returns None and falls through to the agent.
    elif (result := semantic.answer(user, ctx["source"], prompt, ctx["table_param"])) is not None:
        progress.emit("answered from the semantic layer (canonical metric)")
    # Tier 0 — semantic cache: an equivalent prompt reuses its plan (SQL+chart),
    # re-executed fresh, with no model at all. FIRST TURNS ONLY: the cache keys on
    # the prompt alone, and a follow-up ("and by region?") means something different
    # in every conversation — matching one against another replays the wrong plan.
    elif not ctx["history"] and (cached := qcache.lookup(
            user, ctx["source"], ctx["table_label"], prompt)) is not None:
        progress.emit("reusing a cached plan for an equivalent question")
        result = cached
    else:
        # Tier 1/2 — route learned, repeated work to the self-hosted BitNet;
        # only novel prompts reach the frontier LLM. BitNet's SQL still passes
        # the guard, and a failed BitNet attempt escalates to the frontier.
        route, pattern = model_router.choose(user, ctx["source"], ctx["table_label"], prompt)
        result = None
        if route == "bitnet":
            try:
                progress.emit("trying the self-hosted BitNet engine first")
                r = _run(model_router.bitnet_spec())
                if r.get("sql") and not r.get("errors"):
                    r["served_by"] = "bitnet"
                    routed = {"model": "bitnet"}
                    if pattern:   # a matched learned pattern (confidence)
                        routed.update({k: pattern[k] for k in ("seen", "avg_reward", "similarity")})
                    else:         # BitNet answered as the promoted primary
                        routed["primary"] = True
                    r["routed"] = routed
                    result = r
            except Exception:
                result = None   # escalate below
        if result is None:
            if route == "bitnet":
                progress.emit("BitNet couldn't answer — escalating to the frontier model")
            result = _run(model)   # frontier LLM (default, or BitNet escalation)
            result.setdefault("served_by", "frontier")
        if not ctx["history"]:   # same reason: a follow-up's plan is context-bound
            qcache.store(user, ctx["source"], ctx["table_label"], prompt, result,
                         reward=lightning.heuristic_reward(result))
    progress.emit("finalizing the answer")
    result["source"] = ctx["source"]
    result["table"] = ctx["table_label"]
    try:
        match_schemas = dict(ctx["schemas"])
        for t in rbac.allowed_tables(user["role"], ctx["source"], ctx["all_tables"])[:20]:
            if t not in match_schemas:
                match_schemas[t] = connector.get_schema(t)
        result["matched_tables"] = match_tables(prompt, match_schemas)
    except Exception:
        result["matched_tables"] = []
    result["inputs"] = _query_inputs(result)
    tid = lightning.record_chat_trace(user, cid, prompt, result, int((time.time() - t0) * 1000),
                                      history=ctx["history"])
    if tid:
        result["trace_id"] = tid
    result["author_role"] = user["role"]
    _answer(ctx, result)
    db.log_activity(
        user, "chat", prompt=prompt, source=ctx["source"], table=ctx["table_label"],
        sql=result.get("sql"), mode=result.get("mode"),
        row_count=len(result.get("rows") or []),
        ok=not (result.get("text") or "").startswith("(Agent error"),
        duration_ms=int((time.time() - t0) * 1000))
    _checkpoint(user, cid, result, model, ctx["source"], ctx["table_label"])
    return result


@router.post("/chat")
def ask(body: Ask, user=Depends(current_user)):
    """Synchronous turn — waits for the answer."""
    ctx = _prepare(body, user)
    result = _run_turn(ctx, user)
    return {"conversation_id": ctx["cid"], "message": result}


@router.post("/chat/background", status_code=202)
def ask_background(body: Ask, user=Depends(current_user)):
    """Start a turn without waiting. Returns immediately with a task id; the
    answer is appended to the conversation when it finishes, and the
    conversation gets an unseen marker (the blue dot) until it's opened. Lets a
    user run tasks in several chats at once. The turn itself is a durable
    chat_turn job: recorded here, run by a worker (jobs.py)."""
    scope = _scope(body, user)                       # validates now: 4xx before any write
    # The user message, exactly once — its id is what the task answers, and
    # what tells two concurrent turns in this conversation apart.
    cid, mid = _record_user_turn(body, user, scope=scope)
    tid = str(uuid.uuid4())
    now = time.time()
    # The task row and its queue job go in ONE transaction, on one connection.
    # They used to be two commits, and a failure between them (the enqueue
    # raising, the process dying) left chat_tasks.status='running' with no job
    # behind it: nothing would ever run that turn and nothing would ever fail
    # it, so the UI spun on it forever. Both tables live in this database, so
    # atomicity needs no outbox — either the user gets a task a worker will
    # pick up, or the POST fails and there is no task at all.
    #
    # The user message above is deliberately NOT in this transaction: it is
    # written by db.add_message on its own connection, and an orphan there is
    # harmless — an unanswered question the user can simply ask again, not a
    # row that pretends work is in flight.
    with db.connect() as c:
        c.execute("INSERT INTO chat_tasks (id, conversation_id, user_id, prompt, status, "
                  "seen, user_message_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (tid, cid, user["id"], scope["prompt"][:500], "running", 0, mid, now))
        # conn=c enlists the job row in the transaction above and leaves the
        # commit to us. The id is derived from the task id, so the job and the
        # task it drives can be joined (see _fail_orphan_tasks) and a repeated
        # enqueue for one task can never queue the turn twice.
        jobs.enqueue("chat_turn", {"body": body.model_dump(), "cid": cid, "tid": tid,
                                   "user_id": user["id"]},
                     user_id=user["id"], max_attempts=2, job_id=_task_job_id(tid), conn=c)
        c.commit()
    return {"conversation_id": cid, "task_id": tid, "status": "running"}


# A chat task and the job that runs it share an id, so either can be found
# from the other with no extra column and no scan of the payload JSON.
_JOB_ID_PREFIX = "chat_turn:"


def _task_job_id(tid):
    return _JOB_ID_PREFIX + tid


def _orphan_task_after_s():
    """How long a task may sit 'running' with no live job before the reclaim
    pass gives up on it.

    It must comfortably exceed the longest legitimate gap between a task
    starting and its job existing in a live state, and the queue's own stale
    window bounds that: a claimed job whose worker died is only re-queued
    after STUDIO_JOB_STALE_S, and it may burn several attempts doing so. Three
    stale windows (15 minutes by default) is generous on purpose — the cost of
    waiting is a spinner, the cost of being early is failing a turn that was
    about to answer."""
    override = os.getenv("STUDIO_CHAT_TASK_ORPHAN_S")
    if override:
        return float(override)
    return max(3 * jobs.stale_after_s(), 900.0)


@jobs.reconciler
def _fail_orphan_tasks():
    """Fail every chat task left 'running' with no job behind it.

    Enqueue is atomic now, so no NEW orphan can be created; this heals the
    ones an older build already left in the database, and any future way a job
    row can vanish from under a task (an operator clearing the queue, a job
    finishing without writing the answer because its claim was reclaimed and
    the winner then died). Registered as a jobs reconciler, so it runs on the
    worker's existing reclaim pass rather than on a timer of its own.

    Returns the number of tasks failed."""
    cutoff = time.time() - _orphan_task_after_s()
    failed = 0
    with db.connect() as c:
        # A task is an orphan when its job is missing, or finished while the
        # task never left 'running'. '||' is the SQL-standard concatenation
        # both SQLite and Postgres speak.
        rows = c.execute(
            "SELECT t.id FROM chat_tasks t "
            f"LEFT JOIN background_jobs j ON j.id = '{_JOB_ID_PREFIX}' || t.id "
            "WHERE t.status='running' AND t.created_at < ? "
            "AND (j.id IS NULL OR j.status IN ('done','failed'))",
            (cutoff,)).fetchall()
        for r in rows:
            c.execute(
                "UPDATE chat_tasks SET status='failed', error=?, finished_at=? "
                "WHERE id=? AND status='running'",
                ("this turn was lost: no background job is running it any more",
                 time.time(), r["id"]))
            failed += 1
        c.commit()
    if failed:
        log.warning("chat: failed %s orphaned background task(s) with no job behind them",
                    failed)
    return failed


def _task_turn(tid):
    """(created_at, user_message_id) for a task, or None. user_message_id is
    NULL only for a task recorded before the column existed (migration 6)."""
    with db.connect() as c:
        r = c.execute("SELECT created_at, user_message_id FROM chat_tasks WHERE id=?",
                      (tid,)).fetchone()
    return (float(r["created_at"]), r["user_message_id"]) if r else None


def _already_answered(cid, tid):
    """Re-entrancy guard for a retried chat_turn: True when THIS task's turn
    already carries its assistant message — an earlier attempt finished the
    turn but died before marking the task done.

    The match is by IDENTITY, not by time: the task row records the id of the
    user message it answers and _answer() stamps that id on the answer
    (`reply_to`). The temporal test this replaced ("any assistant message
    newer than my user turn") silently dropped the second of two background
    turns started in the same conversation — whichever answered first marked
    BOTH tasks done, so one question was never answered at all.

    A task written before the column existed (an in-flight turn across a
    deploy) has no id to match, and falls back to the old temporal test: it is
    wrong only when turns overlap, and it is what keeps those tasks
    retry-safe.
    """
    turn = _task_turn(tid)
    if turn is None:
        return False
    created_at, mid = turn
    if mid:
        return any(m["role"] == "assistant" and (m.get("content") or {}).get("reply_to") == mid
                   for m in db.list_messages(cid))
    with db.connect() as c:
        # The user message was written just before the task row, in the same
        # request; the newest user message at or before that moment is this turn's.
        user_turn = c.execute(
            "SELECT created_at FROM messages WHERE conversation_id=? AND role='user' "
            "AND created_at <= ? ORDER BY created_at DESC LIMIT 1",
            (cid, created_at + 1.0)).fetchone()
        since = float(user_turn["created_at"]) if user_turn else created_at
        answered = c.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND role='assistant' "
            "AND created_at > ? LIMIT 1", (cid, since)).fetchone()
    return answered is not None


def _finish_task(tid, status, error=None):
    try:
        with db.connect() as c:
            c.execute("UPDATE chat_tasks SET status=?, error=?, finished_at=? WHERE id=?",
                      (status, error, time.time(), tid))
            c.commit()
    except Exception:
        pass


@jobs.handler("chat_turn")
def _chat_turn_job(payload, job):
    """Runs on a worker: rebuild the context from ids, run the turn, record
    the task outcome. Re-entrant — a retry (or a reclaim after a crashed
    worker) that finds the answer already written just marks the task done.
    An exception always propagates so the queue records it: with attempts
    left the job is retried and the task stays 'running'; on the last attempt
    the task is marked failed first (exactly as the old thread pool did) and
    the job fails alongside it, so both rows carry the error.

    jobs.ClaimLost is the exception to that: it means reclaim_stale() gave
    this job to another worker while we ran, so the attempt executing NOW owns
    the task row. We touch nothing — not the task, not the queue — and let
    jobs._execute abandon us silently."""
    tid, cid = payload["tid"], payload["cid"]
    user = db.get_user(payload["user_id"])
    if user is None:
        _finish_task(tid, "failed", "user no longer exists")
        return {"status": "failed", "task_id": tid}
    if _already_answered(cid, tid):
        _finish_task(tid, "done")
        return {"status": "done", "task_id": tid, "reentered": True}
    last_attempt = int(job.get("attempts") or 1) >= int(job.get("max_attempts") or 1)
    progress.bind(tid)   # this thread's emits feed the task's live activity
    try:
        body = Ask(**payload["body"])
        ctx = _build_ctx(body, user, cid)
        turn = _task_turn(tid)
        # Stamp the answer with the user message this task answers, so the
        # guard above recognises it on a retry (and never another turn's).
        ctx["reply_to"] = turn[1] if turn else None
        # First safe point: the expensive part has not started yet. _answer()
        # checks again immediately before it writes, which is the one that
        # actually has to hold.
        jobs.check_claim()
        _run_turn(ctx, user)
    except jobs.ClaimLost:
        # Another worker owns this job — and therefore this task — now. Leave
        # chat_tasks exactly as it is: the owner will finish it, and marking
        # it failed here would overwrite an answer that is on its way.
        progress.emit("this attempt lost its claim and was abandoned")
        log.warning("chat: task %s abandoned by a reclaimed attempt; the current "
                    "owner of job %s finishes it", tid, job.get("id"))
        raise
    except Exception as e:
        if last_attempt:
            _finish_task(tid, "failed", str(e)[:500])
        else:
            progress.emit(f"attempt failed ({type(e).__name__}); retrying")
        raise
    finally:
        progress.bind(None)
    _finish_task(tid, "done")
    return {"status": "done", "task_id": tid}


@router.get("/tasks/{tid}")
def task_status(tid: str, user=Depends(current_user)):
    with db.connect() as c:
        r = c.execute("SELECT id, conversation_id, user_id, status, error, steps "
                      "FROM chat_tasks WHERE id=?", (tid,)).fetchone()
    if r is None or dict(r)["user_id"] != user["id"]:
        raise HTTPException(404, "Task not found")
    d = dict(r)
    d.pop("user_id", None)
    # The live activity feed the agent emitted so far (progress.py).
    try:
        d["steps"] = json.loads(d.get("steps") or "[]")
    except (TypeError, ValueError):
        d["steps"] = []
    return d


@router.get("/conversations/{cid}/task")
def latest_task(cid: str, user=Depends(current_user)):
    """The most recent background task for a conversation — so reopening a chat
    whose task is still running resumes the live 'working…' state."""
    _own_or_404(cid, user)
    with db.connect() as c:
        r = c.execute("SELECT id, status, error FROM chat_tasks WHERE conversation_id=? AND user_id=? "
                      "ORDER BY created_at DESC LIMIT 1", (cid, user["id"])).fetchone()
    return dict(r) if r else {"status": "none"}


def _task_states(user_id):
    """Per-conversation task counts for the sidebar: how many are still running,
    and how many finished but haven't been seen (the blue dot)."""
    with db.connect() as c:
        rows = c.execute("SELECT conversation_id, status, seen FROM chat_tasks WHERE user_id=?",
                         (user_id,)).fetchall()
    out = {}
    for r in rows:
        d = out.setdefault(r["conversation_id"], {"running": 0, "unseen": 0})
        if r["status"] == "running":
            d["running"] += 1
        elif not r["seen"]:
            d["unseen"] += 1
    return out


def _mark_seen(cid, user_id):
    with db.connect() as c:
        c.execute("UPDATE chat_tasks SET seen=1 WHERE conversation_id=? AND user_id=? AND status!='running'",
                  (cid, user_id))
        c.commit()


class Rerun(BaseModel):
    source: str
    sql: str


@router.post("/chat/rerun")
def rerun(body: Rerun, user=Depends(current_user)):
    """Re-execute a message's SQL live — always fetches the newest records.

    The gateway owns the whole pipeline (RBAC → guard → limit → run →
    governance) and writes the "rerun" audit row itself, on success and on
    failure, so nothing is logged here. Only the HTTP mapping is ours: a
    rejection is the caller's fault (403), an unconfigured source keeps its
    own status, anything else is the warehouse's (502).
    """
    try:
        result = gateway.execute(user, body.source, body.sql, "rerun")
    except queryguard.QueryRejected as e:
        raise HTTPException(403, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Query failed: {e}")
    return {"columns": result.columns, "rows": result.rows}


class CanvasEdit(BaseModel):
    instruction: str
    columns: List[str]
    rows: List[list]
    chart: Optional[dict] = None
    # When provided, the edit is appended to the conversation as a NEW
    # message (a new version) instead of silently mutating the old one.
    conversation_id: Optional[str] = None
    source: Optional[str] = None
    table: Optional[str] = None
    sql: Optional[str] = None


@router.post("/canvas/edit")
def canvas_edit(body: CanvasEdit, user=Depends(current_user)):
    """Prompt-driven edit of a canvas (chart spec + LOCAL data copy).

    Edits never touch source tables — they transform the in-memory result
    only. Each edit is versioned: it becomes a new assistant message in the
    conversation, so earlier versions stay intact in the history.
    """
    instruction = body.instruction.strip()
    if not instruction:
        raise HTTPException(400, "Empty instruction")

    # A sheet may hold several charts. When the source is known, composition
    # can also issue fresh RBAC-guarded SQL, so views the current result
    # aggregated away (a finer time grain, another window) are reachable.
    result = None
    # WITH the user: composition is a full LLM call, and a BYOK user (their own
    # key in keys.py, no server key) is exactly as able to make it as the
    # server is. Asking about the server key alone sent them to the
    # single-chart editor forever. Every other LLM entry point on this path
    # (run_agent, available_models) already passes the user.
    if agent.llm_available(user=user):
        connector, allowed, schemas = _canvas_source(body, user)
        try:
            result = agent.compose_canvas(
                instruction, body.columns, body.rows[:agent.MAX_ROWS], body.chart,
                connector=connector, allowed_tables=allowed, schemas=schemas, user=user)
            first = result["panels"][0]
            result = {**result, "columns": first["columns"], "rows": first["rows"],
                      "chart": first["chart"]}
        except Exception as e:
            result = None  # fall back to the single-chart editor below
            _compose_err = str(e)[:200]

    if result is None:
        result = agent.edit_canvas(
            instruction, body.columns, body.rows[:agent.MAX_ROWS], body.chart, user=user)
        result.setdefault("panels", [{"sql": body.sql, "columns": result["columns"],
                                      "rows": result["rows"], "chart": result["chart"]}])
    db.log_activity(user, "canvas_edit", prompt=instruction,
                    row_count=len(result.get("rows") or []))

    message = None
    if body.conversation_id:
        _own_or_404(body.conversation_id, user, need="edit")
        panels = result.get("panels") or [{"sql": body.sql, "columns": result["columns"],
                                           "rows": result["rows"], "chart": result["chart"]}]
        message = {
            "text": f"✏️ {instruction} — {result.get('note', 'updated')}",
            "sql": panels[0].get("sql") or body.sql,
            "columns": result["columns"],
            "rows": result["rows"],
            "chart": result["chart"],
            "panels": panels,
            "mode": "canvas_edit",
            "model": None,
            "source": body.source,
            "table": body.table,
            "author_role": user["role"],
        }
        db.add_message(body.conversation_id, "assistant", message)

    return {**result, "message": message}


class EmailReport(BaseModel):
    subject: str
    text: str = ""
    sql: Optional[str] = None
    columns: List[str] = []
    rows: List[list] = []


@router.post("/reports/email")
def email_report(body: EmailReport, user=Depends(current_user)):
    """Email a chat result to the signed-in user."""
    html = email_service.report_html(
        body.subject, body.text or "Report from Studio.",
        body.sql, body.columns, body.rows,
        footer=f"Sent to {user['email']} from Studio.",
    )
    try:
        delivery = email_service.send(user["email"], f"Studio report: {body.subject}", html)
    except Exception as e:
        db.log_activity(user, "email_report", prompt=body.subject, ok=False, error=str(e)[:300])
        raise HTTPException(502, f"Email failed: {e}")
    db.log_activity(user, "email_report", prompt=body.subject, sql=body.sql,
                    row_count=len(body.rows), mode=delivery.get("mode"))
    return delivery


@router.get("/audit")
def audit(all: bool = False, limit: int = 200, user=Depends(current_user)):
    """Activity log. Regular users see their own; admins can pass all=true
    to see every user's prompts, SQL, and outcomes."""
    limit = max(1, min(limit, 1000))
    if all:
        if user["role"] != "admin":
            raise HTTPException(403, "Only admins can view all users' activity")
        return db.list_activity(None, limit)
    return db.list_activity(user["id"], limit)


class Feedback(BaseModel):
    trace_id: str
    score: int  # 1 = helpful, -1 = wrong/unhelpful
    note: Optional[str] = None


@router.post("/feedback")
def feedback(body: Feedback, user=Depends(current_user)):
    """👍/👎 on an answer — the explicit reward signal the agent learns from
    (overwrites the heuristic reward on that run's trace)."""
    ok = db.set_trace_reward(
        body.trace_id, 1.0 if body.score > 0 else 0.0, source="user", note=body.note)
    if not ok:
        raise HTTPException(404, "Unknown trace")
    db.log_activity(user, "feedback", prompt=(body.note or "")[:300],
                    ok=body.score > 0)
    return {"ok": True}


@router.get("/learning")
def learning(user=Depends(current_user)):
    """Admin dashboard for the learning loop: rollout volume, rewards by
    mode/model, feedback tallies, and failure clusters."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only admins can view agent learning stats")
    stats = db.trace_stats()
    stats["agent_lightning"] = lightning.agl_available()
    stats["low_reward_recent"] = [
        {k: t[k] for k in ("prompt", "model", "error", "reward", "reward_source")}
        for t in db.list_traces(limit=10, max_reward=0.4)
    ]
    stats["by_agent"] = _agent_tally(db.list_traces(limit=1000))
    # Where the learning is stored — surfaced so it's discoverable in the UI.
    stats["storage"] = {
        "rollouts_table": "agent_traces",
        "store": "postgres" if db.IS_PG else f"sqlite ({db.DB_PATH})",
        "learned_prompt": "prompts/system_learned.txt",
    }
    return stats


def _agent_tally(traces):
    """Per-agent reward rollup. Only single-agent rollouts count — a trace naming
    one agent is that agent's OWN scored decision (a flow stage, a worker, the
    aggregator, the SQL verifier, a single-source answer). A blended multi-agent
    trace (the whole orchestrated answer) is excluded, so no agent's average is
    smeared by another agent's work."""
    tally = {}
    for t in traces:
        meta = t.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        agents = (meta or {}).get("agents") or []
        if len(agents) != 1:          # per-agent reward only, never blended
            continue
        name = agents[0]
        d = tally.setdefault(name, {"agent": name, "n": 0, "rsum": 0.0, "rn": 0})
        d["n"] += 1
        if t.get("reward") is not None:
            d["rsum"] += t["reward"]
            d["rn"] += 1
    out = [{"agent": d["agent"], "n": d["n"],
            "avg_reward": round(d["rsum"] / d["rn"], 3) if d["rn"] else None}
           for d in tally.values()]
    out.sort(key=lambda x: -x["n"])
    return out


@router.get("/skills")
def skills_catalog(user=Depends(current_user)):
    """Every skill file this user's role can see — the RBAC-scoped briefing each
    per-source agent runs on (source, dialect, tables, schemas). This is exactly
    what the agents read, surfaced so a user can read it too."""
    out = []
    for s in orchestrator.accessible_sources(user):
        conn = s["connector"]
        out.append({
            "source": conn.name,
            "agent": roster.name_for(conn.name),
            "dialect": conn.dialect,
            "tables": s["allowed"],
            "skill": s["skill"],
        })
    return {"skills": out, "role": user["role"]}


def _training_stats():
    with db.connect() as c:
        n = lambda q: c.execute(q).fetchone()["n"]
        stats = {
            "total_rollouts": n("SELECT COUNT(*) n FROM agent_traces"),
            "usable": n("SELECT COUNT(*) n FROM agent_traces WHERE reward IS NOT NULL"),
            "distinct_prompts": n("SELECT COUNT(DISTINCT prompt) n FROM agent_traces WHERE prompt IS NOT NULL"),
            "human_labeled": n("SELECT COUNT(*) n FROM agent_traces WHERE reward_source='user'"),
        }
    return stats


@router.get("/training")
def training(user=Depends(current_user)):
    """Readiness to train our own model from collected prompts. On hosted models
    the lever is prompt optimization (APO); the same rollouts graduate to weight
    RL once a self-hosted open-weight model is available. Admin-only."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only admins can view training readiness")
    threshold = int(os.getenv("STUDIO_TRAIN_THRESHOLD", "500"))
    stats = _training_stats()
    collected = stats["distinct_prompts"]      # prompts from users — the gate
    stats["threshold"] = threshold
    stats["collected"] = collected
    stats["ready"] = collected >= threshold
    stats["progress"] = round(min(1.0, collected / threshold), 3) if threshold else 1.0
    stats["store"] = "postgres" if db.IS_PG else f"sqlite ({db.DB_PATH})"
    # usable = reward-labeled rollouts (the trainable subset); the rest are
    # unlabeled prompts still useful for prompt optimization.
    stats["method"] = "APO (prompt optimization) now · weight RL when self-hosted"
    return stats


class ExportIn(BaseModel):
    path: Optional[str] = None


@router.post("/training/export")
def training_export(body: ExportIn, user=Depends(current_user)):
    """Export the reward-labeled rollouts as JSONL (the shape an RL/APO trainer
    consumes). Admin-only."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only admins can export training data")
    path = (body.path or os.path.join(os.getenv("STUDIO_EXPORT_DIR", "/tmp"),
                                      "studio_rollouts.jsonl"))
    try:
        n = lightning.export_rollouts(path)
    except Exception as e:
        raise HTTPException(500, f"Export failed: {e}")
    db.log_activity(user, "training_export", prompt=f"{n} rollouts")
    return {"exported": n, "path": path}


def _canvas_source(body, user):
    """Bind the canvas to its source so composed SQL stays inside this
    user's RBAC allowlist. Returns (connector, allowed_tables, schemas);
    all None/empty when the source is unknown — then no SQL is composed."""
    if not body.source or body.source == "*":
        return None, [], {}
    try:
        connector = connector_or_400(body.source)
        all_tables = connector.list_tables()
    except Exception:
        return None, [], {}
    allowed = rbac.allowed_tables(user["role"], body.source, all_tables)
    if not allowed:
        return None, [], {}
    schemas = {}
    for t in allowed[:10]:
        try:
            schemas[t] = connector.get_schema(t)
        except Exception:
            pass
    return connector, allowed, schemas


def _conversation(cid, user, prompt, recorded=False):
    """Resolve/create the conversation and return (cid, recent history).
    recorded=True means this turn's user message is already the newest row
    (a background job rebuilding its context) and must not count as history."""
    if cid:
        access = _own_or_404(cid, user, need="edit")
        msgs = _visible_messages(cid, user, access)
        if recorded and msgs and msgs[-1]["role"] == "user":
            msgs = msgs[:-1]
        # A clarification ("which source?") and the question that triggered it
        # are not conversation for the model: the re-ask repeats the question,
        # so replaying the pair would show the model its own unanswered question
        # twice — and make a clarified FIRST question look like a follow-up
        # (skipping the semantic cache). Drop the pair.
        keep = []
        for i, m in enumerate(msgs):
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            if m["role"] == "assistant" and m["content"].get("mode") == "clarify":
                continue
            if (m["role"] == "user" and nxt and nxt["role"] == "assistant"
                    and nxt["content"].get("mode") == "clarify"):
                continue
            keep.append(m)
        history = [
            {"role": m["role"], "text": m["content"].get("text", "")}
            for m in keep if m["content"].get("text")
        ][-int(os.getenv("STUDIO_HISTORY_TURNS", "8")):]
        return cid, history
    return db.create_conversation(user["id"], prompt), []


def _checkpoint(user, cid, result, model, source, table_scope):
    """Serialize the conversation as a resumable agent session after each turn,
    folding in this turn's token/cache usage. Best-effort — a snapshot failure
    must never fail the chat response.

    The transcript is the ACTING user's visible view (_visible_messages), not
    the raw rows: the session lands in that user's own agent_sessions row and
    is readable back through /sessions, /resume and /fork, so an edit-recipient
    of a shared chat would otherwise carry every redacted message's text out
    in their own session. What their role may not see is snapshotted as the
    redaction placeholder, exactly as the chat shows it to them."""
    try:
        access = db.conversation_access(cid, user["id"]) or "view"
        transcript = [{"role": m["role"], "text": (m.get("content") or {}).get("text", "")}
                      for m in _visible_messages(cid, user, access)]
        title = (db.get_conversation_title(cid) if hasattr(db, "get_conversation_title")
                 else None) or (transcript[0]["text"][:80] if transcript else "Session")
        sessions.snapshot(
            user, conversation_id=cid, title=title,
            model_spec=result.get("model") or model, source=source,
            table_scope=table_scope, messages=transcript, usage=result.get("usage"),
            count_turn=True)
    except Exception:
        pass


def _query_inputs(result):
    """What the answer was actually computed from.

    The requested table label is what the user picked; the executed SQL is what
    the agent really read. Those differ whenever the agent follows a question
    into a neighbouring table, so report the SQL's own references.
    """
    sqls = [result.get("sql")] + [p.get("sql") for p in (result.get("panels") or [])]
    tables, seen = [], set()
    for sql in [s for s in sqls if s]:
        for ref in queryguard.TABLE_REF.findall(sql):
            name = ref.strip('"').split(".")[-1].lower()
            if name and name not in seen:
                seen.add(name)
                tables.append(name)
    return {
        "tables": tables,
        "columns": [c for c in (result.get("columns") or []) if c][:12],
        "row_count": len(result.get("rows") or []),
    }


_ROLE_RANK = {"admin": 3, "analyst": 2, "viewer": 1}


def _unrestricted(role, source):
    """True when the role may read EVERY table in a source ("*" policy).

    Resolved through rbac's governance-aware lookup, never rbac.POLICIES
    directly: a governance YAML that tightens a role (analyst.demo: [sales])
    must also tighten what that role can be shown from a stored chat.
    """
    return rbac._role_policy(role, source) == "*"


def _sql_tables_allowed(role, source, content):
    """Every base table a stored message's SQL (and each panel's SQL) reads
    must be one the role may query. The client-supplied table label is what
    the user picked; the SQL is what was actually read, and the two differ
    whenever the agent followed a question into a neighbouring table. Panels
    from an orchestrated turn may name their own source; the message's source
    is the fallback. SQL with no attributable table (unparseable, or a file
    path) fails closed to the whole-source rule."""
    checks = [(source, content.get("sql"))]
    for p in content.get("panels") or []:
        if isinstance(p, dict):
            checks.append((p.get("source") or source, p.get("sql")))
    for src, sql in checks:
        if not sql or not src or src == "*":
            continue
        tables = queryguard.base_tables(sql)
        if not tables:
            if not _unrestricted(role, src):
                return False
            continue
        if not all(rbac.can_access(role, src, t) for t in tables):
            return False
    return True


def _msg_allowed(role, content, msg_role=None):
    """May this role see a stored message's results? Messages carry result
    ROWS, so this is the RBAC boundary for anyone who is not the owner.
    `msg_role` is the stored row's role ("user" / "assistant"), when known.

    Whole-source and orchestrated answers are only released to a role with
    unrestricted access, since the author may have reached any table. A
    table-labelled answer must additionally pass on every table its SQL
    references, because the label alone is not trusted.
    """
    content = content or {}
    # A message carrying no data — a user's question, a clarification, an
    # agent error — has nothing the whole-source / table rules protect: they
    # exist for result ROWS. Without this, every prompt in a shared
    # orchestrated chat was a "Hidden" placeholder for a restricted reader and
    # an edit-recipient's follow-up ran with the questions stripped from its
    # history. A message that names SQL or carries rows/panels (a purged
    # message keeps its SQL) still takes the full gate below: its text may
    # summarise rows the role cannot query.
    if msg_role == "user" or content.get("mode") == "clarify":
        if not (content.get("rows") or content.get("panels") or content.get("sql")):
            return True
    # The source/table labels on a stored message are client-supplied (canvas
    # edits post their own), so they cannot be trusted on their own. The
    # author's role is stamped server-side: never release a message to a role
    # less privileged than the one that produced it.
    author = content.get("author_role")
    if author and _ROLE_RANK.get(role, 0) < _ROLE_RANK.get(author, 0):
        return False
    source = content.get("source")
    if not source:
        # No provenance and no source: only safe for a message carrying no data.
        return not (content.get("rows") or content.get("panels")) if not author else True
    label = str(content.get("table") or "*")
    if source == "*":
        sources = rbac.allowed_sources(role)
        return (bool(sources) and all(_unrestricted(role, s) for s in sources)
                and _sql_tables_allowed(role, source, content))
    if label in ("*", "all tables", "all sources"):
        return _unrestricted(role, source)
    tables = [t.strip() for t in label.split(",") if t.strip()] or ["*"]
    return (all(rbac.can_access(role, source, t) for t in tables)
            and _sql_tables_allowed(role, source, content))


def _governed(content):
    """Re-apply the compliance rules to a stored message's rows at READ time.

    Rows were filtered when the query ran, but a governance document applied
    later (a new deny_columns / mask_columns rule) must reach data that is
    already sitting in chat history — for every reader, the owner included.
    Returns a NEW content dict; the stored row is never mutated. Orchestrated
    messages (source "*") have no single source to key rules on, so only their
    panels are filtered, each by its own source when it names one.
    """
    source = content.get("source")

    def _apply(src, sql, columns, rows):
        if not src or src == "*" or not columns or not rows:
            return columns, rows
        try:
            return governance.filter_result(src, sql or "", columns, rows)
        except queryguard.QueryRejected:
            # A denied column reached inside a CTE / derived table: the gate
            # refuses that shape outright now, and rows stored under an older
            # (or no) rule are hidden the same way rather than guessed at.
            return [], []

    out = dict(content)
    out["columns"], out["rows"] = _apply(
        source, content.get("sql"), content.get("columns"), content.get("rows"))
    panels = content.get("panels")
    if isinstance(panels, list):
        new_panels = []
        for p in panels:
            if isinstance(p, dict):
                p = dict(p)
                p["columns"], p["rows"] = _apply(
                    p.get("source") or source, p.get("sql"), p.get("columns"), p.get("rows"))
            new_panels.append(p)
        out["panels"] = new_panels
    return out


_REDACTED = "🔒 Hidden — this message used data your role cannot access."


def _visible_messages(cid, user, access):
    """Redact, at READ time, anything the viewer's role may not query.

    Checking only at share time is not enough: the owner can add a PII query
    to an already-shared conversation, and the recipient would inherit it.
    """
    # Redaction keys off the READER's role, never ownership: a viewer who
    # owns a chat and invites an analyst would otherwise inherit whatever
    # the analyst queried into it.
    out = []
    for m in db.list_messages(cid):
        content = m.get("content") or {}
        if not _msg_allowed(user["role"], content, m.get("role")):
            m = {**m, "content": {
                "text": _REDACTED, "redacted": True,
                "source": content.get("source"), "table": content.get("table"),
                "columns": [], "rows": [], "panels": [], "chart": None, "sql": None,
            }}
        elif content.get("rows") or content.get("panels"):
            # Released, and carrying data: today's compliance rules apply to
            # rows stored under yesterday's.
            m = {**m, "content": _governed(content)}
        out.append(m)
    return out


# ── Retention: stored result rows expire, the conversation does not ──────

_PURGED_MARK = '"rows_purged": true'   # json.dumps spelling of the flag we set


def _retention_days():
    try:
        return int(os.getenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS", "0") or 0)
    except ValueError:
        return 0


def purge_message_rows(max_age_days=None, batch=500):
    """Strip result rows from assistant messages older than `max_age_days`
    (default: STUDIO_MESSAGE_ROWS_RETENTION_DAYS; 0 keeps rows forever).

    Text, SQL, chart and columns stay, so the history still renders; the
    message gains rows_purged=True and the UI shows a "rows expired" marker.
    The SQL is the durable artefact — a reader with access can rerun it for
    fresh rows. Keyset-paginated over the id so a large table is never read
    at once, dialect-neutral SQL (SQLite and the Postgres facade), and it
    never raises: a retention hiccup must not stop the app from booting.
    Returns the number of messages purged.
    """
    days = _retention_days() if max_age_days is None else int(max_age_days)
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    purged, last_id = 0, ""
    try:
        with db.connect() as c:
            while True:
                rows = c.execute(
                    "SELECT id, content FROM messages WHERE role='assistant' AND created_at < ? "
                    "AND id > ? AND content NOT LIKE ? ORDER BY id LIMIT ?",
                    (cutoff, last_id, "%" + _PURGED_MARK + "%", batch)).fetchall()
                if not rows:
                    break
                for r in rows:
                    last_id = r["id"]
                    try:
                        content = json.loads(r["content"])
                    except Exception:
                        continue
                    if not isinstance(content, dict):
                        continue
                    panels = content.get("panels")
                    has_rows = bool(content.get("rows")) or any(
                        isinstance(p, dict) and p.get("rows") for p in (panels or []))
                    if not has_rows:
                        continue
                    content["rows"] = []
                    if isinstance(panels, list):
                        content["panels"] = [
                            {**p, "rows": []} if isinstance(p, dict) else p for p in panels]
                    content["rows_purged"] = True
                    c.execute("UPDATE messages SET content=? WHERE id=?",
                              (json.dumps(content), r["id"]))
                    purged += 1
                c.commit()
                if len(rows) < batch:
                    break
    except Exception:
        pass
    return purged


def _hidden_count(cid, role):
    return sum(1 for m in db.list_messages(cid)
               if not _msg_allowed(role, m.get("content") or {}, m.get("role")))


def _own_or_404(cid, user, need="view"):
    """The one gate for conversation access. need: view | edit | owner.

    404 when the conversation is invisible to this user — an unshared
    conversation must not be distinguishable from a nonexistent one, or the
    id space becomes an existence oracle.
    """
    access = db.conversation_access(cid, user["id"])
    if access is None:
        raise HTTPException(404, "Conversation not found")
    if need == "owner" and access != "owner":
        raise HTTPException(403, "Only the owner can do that")
    if need == "edit" and access not in ("owner", "edit"):
        raise HTTPException(403, "You have view-only access to this conversation")
    return access
