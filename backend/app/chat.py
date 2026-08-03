"""Chat: conversations, the ask endpoint driving the agent, fresh-data rerun,
email reports, and the per-user activity audit log."""
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agent, db, email_service, lightning, queryguard, rbac
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
    return agent.available_models()


@router.get("/conversations")
def conversations(user=Depends(current_user)):
    return db.list_conversations(user["id"])


@router.get("/conversations/{cid}/messages")
def messages(cid: str, user=Depends(current_user)):
    _own_or_404(cid, user)
    return db.list_messages(cid)


@router.delete("/conversations/{cid}")
def remove(cid: str, user=Depends(current_user)):
    _own_or_404(cid, user)
    db.delete_conversation(cid)
    return {"deleted": True}


@router.post("/chat")
def ask(body: Ask, user=Depends(current_user)):
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Empty prompt")
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

    cid = body.conversation_id
    if cid:
        _own_or_404(cid, user)
        history = [
            {"role": m["role"], "text": m["content"].get("text", "")}
            for m in db.list_messages(cid)
            if m["content"].get("text")
        ][-8:]
    else:
        cid = db.create_conversation(user["id"], prompt)
        history = []

    db.add_message(cid, "user", {"text": prompt, "source": body.source, "table": table_label})

    model = None
    if body.model:
        if body.model not in {m["spec"] for m in agent.available_models()}:
            raise HTTPException(400, f"Model '{body.model}' is not offered")
        model = body.model

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
    # Agent Lightning-style rollout: record the run with a heuristic reward;
    # the trace id rides inside the message so 👍/👎 can re-score it later.
    tid = lightning.record_chat_trace(
        user, cid, prompt, result, int((time.time() - t0) * 1000))
    if tid:
        result["trace_id"] = tid
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
    result = agent.edit_canvas(
        instruction, body.columns, body.rows[:agent.MAX_ROWS], body.chart
    )
    db.log_activity(user, "canvas_edit", prompt=instruction,
                    row_count=len(result.get("rows") or []))

    message = None
    if body.conversation_id:
        _own_or_404(body.conversation_id, user)
        message = {
            "text": f"✏️ {instruction} — {result.get('note', 'updated')}",
            "sql": body.sql,
            "columns": result["columns"],
            "rows": result["rows"],
            "chart": result["chart"],
            "panels": [{"sql": body.sql, "columns": result["columns"],
                        "rows": result["rows"], "chart": result["chart"]}],
            "mode": "canvas_edit",
            "model": None,
            "source": body.source,
            "table": body.table,
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


def _own_or_404(cid, user):
    owner = db.conversation_owner(cid)
    if owner is None:
        raise HTTPException(404, "Conversation not found")
    if owner != user["id"]:
        raise HTTPException(403, "Not your conversation")
