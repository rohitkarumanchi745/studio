"""Chat: conversations, the ask endpoint driving the agent, fresh-data rerun,
email reports, and the per-user activity audit log."""
import os
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import (agent, db, email_service, keys, lightning, orchestrator, queryguard,
               rbac, skills)
from .auth import current_user
from .catalog import _connector_or_400, match_tables

router = APIRouter(tags=["chat"])


class Ask(BaseModel):
    prompt: str
    source: str
    table: str  # a table name, or "*" for whole-source chat
    tables: Optional[List[str]] = None  # multi-select: restrict to these tables
    conversation_id: Optional[str] = None
    model: Optional[str] = None  # user-selected model spec from GET /models


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
    return db.list_conversations(user["id"])


@router.get("/conversations/{cid}/messages")
def messages(cid: str, user=Depends(current_user)):
    access = _own_or_404(cid, user)
    return _visible_messages(cid, user, access)


@router.delete("/conversations/{cid}")
def remove(cid: str, user=Depends(current_user)):
    # Deleting destroys it for everyone it is shared with — owner only.
    _own_or_404(cid, user, need="owner")
    db.delete_conversation(cid)
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


@router.post("/chat")
def ask(body: Ask, user=Depends(current_user)):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Empty prompt")

    model = None
    if body.model:
        if body.model not in {m["spec"] for m in agent.available_models(user)}:
            raise HTTPException(400, f"Model '{body.model}' is not offered")
        model = body.model

    # source "*": the orchestrator fans the question out across per-database
    # agents (one per source this role may access, each with its skill file).
    if body.source == "*":
        cid, history = _conversation(body.conversation_id, user, prompt)
        db.add_message(cid, "user", {"text": prompt, "source": "*", "table": "all sources",
                                     "author_role": user["role"]})
        t0 = time.time()
        result = orchestrator.run_orchestrated(prompt, user, history, model)
        result.setdefault("source", "*")
        result["table"] = "all sources"
        result["matched_tables"] = []
        result["inputs"] = _query_inputs(result)
        tid = lightning.record_chat_trace(
            user, cid, prompt, result, int((time.time() - t0) * 1000))
        if tid:
            result["trace_id"] = tid
        result["author_role"] = user["role"]
        db.add_message(cid, "assistant", result)
        db.log_activity(
            user, "chat", prompt=prompt, source="*",
            table=",".join(result.get("agents_used") or []) or "all sources",
            sql=result.get("sql"), mode=result.get("mode"),
            row_count=len(result.get("rows") or []),
            ok=not (result.get("text") or "").startswith(("(Agent error", "(Orchestrator error")),
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {"conversation_id": cid, "message": result}

    if not rbac.can_access(user["role"], body.source, body.table):
        raise HTTPException(403, "Your role has no access to this table")

    connector = _connector_or_400(body.source)
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
        table_param = "*"  # agent scopes to `allowed`, which is the selection
        table_label = ", ".join(allowed)
    else:
        if body.table != "*" and body.table not in allowed:
            raise HTTPException(403, "Your role has no access to this table")
        table_param = body.table
        table_label = body.table

    # Schema context for every table in scope (capped).
    try:
        if table_param == "*":
            schemas = {t: connector.get_schema(t) for t in allowed[:10]}
        else:
            schemas = {table_param: connector.get_schema(table_param)}
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")

    cid, history = _conversation(body.conversation_id, user, prompt)
    db.add_message(cid, "user", {"text": prompt, "source": body.source, "table": table_label,
                                 "author_role": user["role"]})

    t0 = time.time()
    result = agent.run_agent(
        prompt=prompt,
        connector=connector,
        table=table_param,
        allowed_tables=allowed,
        schemas=schemas,
        history=history,
        user=user,
        model=model,
        # This source's agent skill file: the RBAC-scoped database briefing,
        # auto-rebuilt whenever the schema or this role's access changes.
        skill_md=skills.get_skill(connector, user["role"], allowed, schemas),
    )
    result["source"] = body.source
    result["table"] = table_label

    # Which tables does this business request touch? Match across everything
    # the user may see (not just the current selection) so discovery works.
    try:
        match_schemas = dict(schemas)
        for t in rbac.allowed_tables(user["role"], body.source, all_tables)[:20]:
            if t not in match_schemas:
                match_schemas[t] = connector.get_schema(t)
        result["matched_tables"] = match_tables(prompt, match_schemas)
    except Exception:
        result["matched_tables"] = []
    result["inputs"] = _query_inputs(result)
    # Agent Lightning-style rollout: record the run with a heuristic reward;
    # the trace id rides inside the message so 👍/👎 can re-score it later.
    tid = lightning.record_chat_trace(
        user, cid, prompt, result, int((time.time() - t0) * 1000))
    if tid:
        result["trace_id"] = tid
    result["author_role"] = user["role"]
    db.add_message(cid, "assistant", result)

    db.log_activity(
        user, "chat", prompt=prompt, source=body.source, table=table_label,
        sql=result.get("sql"), mode=result.get("mode"),
        row_count=len(result.get("rows") or []),
        ok=not (result.get("text") or "").startswith("(Agent error"),
        duration_ms=int((time.time() - t0) * 1000),
    )

    return {"conversation_id": cid, "message": result}


class Rerun(BaseModel):
    source: str
    sql: str


@router.post("/chat/rerun")
def rerun(body: Rerun, user=Depends(current_user)):
    """Re-execute a message's SQL live — always fetches the newest records."""
    connector = _connector_or_400(body.source)
    try:
        all_tables = connector.list_tables()
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")
    allowed = rbac.allowed_tables(user["role"], body.source, all_tables)
    t0 = time.time()
    try:
        cleaned = queryguard.validate(body.sql, allowed)
        cleaned = queryguard.enforce_limit(cleaned, agent.MAX_ROWS)
        columns, rows = connector.run_query(cleaned)
    except queryguard.QueryRejected as e:
        db.log_activity(user, "rerun", source=body.source, sql=body.sql, ok=False, error=str(e))
        raise HTTPException(403, str(e))
    except Exception as e:
        db.log_activity(user, "rerun", source=body.source, sql=body.sql, ok=False, error=str(e)[:300])
        raise HTTPException(502, f"Query failed: {e}")
    db.log_activity(user, "rerun", source=body.source, sql=cleaned,
                    row_count=len(rows), duration_ms=int((time.time() - t0) * 1000))
    return {"columns": columns, "rows": rows}


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
    if agent.llm_available():
        connector, allowed, schemas = _canvas_source(body, user)
        try:
            result = agent.compose_canvas(
                instruction, body.columns, body.rows[:agent.MAX_ROWS], body.chart,
                connector=connector, allowed_tables=allowed, schemas=schemas)
            first = result["panels"][0]
            result = {**result, "columns": first["columns"], "rows": first["rows"],
                      "chart": first["chart"]}
        except Exception as e:
            result = None  # fall back to the single-chart editor below
            _compose_err = str(e)[:200]

    if result is None:
        result = agent.edit_canvas(
            instruction, body.columns, body.rows[:agent.MAX_ROWS], body.chart)
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
    return stats


def _canvas_source(body, user):
    """Bind the canvas to its source so composed SQL stays inside this
    user's RBAC allowlist. Returns (connector, allowed_tables, schemas);
    all None/empty when the source is unknown — then no SQL is composed."""
    if not body.source or body.source == "*":
        return None, [], {}
    try:
        connector = _connector_or_400(body.source)
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


def _conversation(cid, user, prompt):
    """Resolve/create the conversation and return (cid, recent history)."""
    if cid:
        access = _own_or_404(cid, user, need="edit")
        history = [
            {"role": m["role"], "text": m["content"].get("text", "")}
            for m in _visible_messages(cid, user, access)
            if m["content"].get("text")
        ][-8:]
        return cid, history
    return db.create_conversation(user["id"], prompt), []


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
    """True when the role may read EVERY table in a source ("*" policy)."""
    return rbac.POLICIES.get(role, {}).get(source) == "*"


def _msg_allowed(role, content):
    """May this role see a stored message's results? Messages carry result
    ROWS, so this is the RBAC boundary for anyone who is not the owner.

    Whole-source and orchestrated answers are only released to a role with
    unrestricted access, since the author may have reached any table.
    """
    content = content or {}
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
        return bool(sources) and all(_unrestricted(role, s) for s in sources)
    if label in ("*", "all tables", "all sources"):
        return _unrestricted(role, source)
    tables = [t.strip() for t in label.split(",") if t.strip()] or ["*"]
    return all(rbac.can_access(role, source, t) for t in tables)


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
        if not _msg_allowed(user["role"], content):
            m = {**m, "content": {
                "text": _REDACTED, "redacted": True,
                "source": content.get("source"), "table": content.get("table"),
                "columns": [], "rows": [], "panels": [], "chart": None, "sql": None,
            }}
        out.append(m)
    return out


def _hidden_count(cid, role):
    return sum(1 for m in db.list_messages(cid)
               if not _msg_allowed(role, m.get("content") or {}))


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
