"""Chat: conversations, the ask endpoint driving the agent, fresh-data rerun,
email reports, and the per-user activity audit log."""
import concurrent.futures
import json
import os
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import (agent, db, email_service, keys, lightning, orchestrator, queryguard,
               rbac, roster, sessions, skills)
from .auth import current_user
from .catalog import _connector_or_400, match_tables

router = APIRouter(tags=["chat"])

# Background tasks let a user start a question in one chat, move to another, and
# be notified (a blue dot on the conversation) when it finishes. Bounded pool —
# a run holds a worker for its duration.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def init_tables():
    c = db._conn()
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
            created_at REAL NOT NULL,
            finished_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_chat_tasks_user
            ON chat_tasks(user_id, conversation_id);
        """
    )
    c.commit()
    c.close()


class Ask(BaseModel):
    prompt: str
    source: str
    table: str  # a table name, or "*" for whole-source chat
    tables: Optional[List[str]] = None  # multi-select: restrict to these tables
    conversation_id: Optional[str] = None
    model: Optional[str] = None  # user-selected model spec from GET /models


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
    c = db._conn()   # drop this chat's background-task rows too (no orphans)
    c.execute("DELETE FROM chat_tasks WHERE conversation_id=?", (cid,))
    c.commit()
    c.close()
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


def _prepare(body, user):
    """Synchronous half of a turn: validate, resolve/create the conversation,
    append the user message. Returns a context for _run_turn, or raises
    HTTPException on validation/RBAC errors — so both the sync path and the
    background submit fail fast, before any agent work starts."""
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
        cid, history = _conversation(body.conversation_id, user, prompt)
        db.add_message(cid, "user", {"text": prompt, "source": "*", "table": "all sources",
                                     "author_role": user["role"]})
        return {"mode": "*", "cid": cid, "history": history, "model": model, "prompt": prompt}

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
        table_param = "*"
        table_label = ", ".join(allowed)
    else:
        if body.table != "*" and body.table not in allowed:
            raise HTTPException(403, "Your role has no access to this table")
        table_param = body.table
        table_label = body.table

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
    return {"mode": "normal", "cid": cid, "history": history, "model": model, "prompt": prompt,
            "source": body.source, "connector": connector, "all_tables": all_tables,
            "allowed": allowed, "schemas": schemas, "table_param": table_param,
            "table_label": table_label}


def _run_turn(ctx, user):
    """Heavy half of a turn: run the agent, append the assistant message, trace,
    and checkpoint. Pure work off the prepared context — safe in a thread."""
    cid, model, prompt = ctx["cid"], ctx["model"], ctx["prompt"]
    t0 = time.time()

    if ctx["mode"] == "*":
        result = orchestrator.run_orchestrated(prompt, user, ctx["history"], model)
        result.setdefault("source", "*")
        result["table"] = "all sources"
        result["matched_tables"] = []
        result["inputs"] = _query_inputs(result)
        tid = lightning.record_chat_trace(user, cid, prompt, result, int((time.time() - t0) * 1000))
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
            duration_ms=int((time.time() - t0) * 1000))
        _checkpoint(user, cid, result, model, "*", "all sources")
        return result

    connector = ctx["connector"]
    result = agent.run_agent(
        prompt=prompt, connector=connector, table=ctx["table_param"],
        allowed_tables=ctx["allowed"], schemas=ctx["schemas"], history=ctx["history"],
        user=user, model=model,
        skill_md=skills.get_skill(connector, user["role"], ctx["allowed"], ctx["schemas"]))
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
    tid = lightning.record_chat_trace(user, cid, prompt, result, int((time.time() - t0) * 1000))
    if tid:
        result["trace_id"] = tid
    result["author_role"] = user["role"]
    db.add_message(cid, "assistant", result)
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
    user run tasks in several chats at once."""
    ctx = _prepare(body, user)   # validates + writes the user message now
    tid = str(uuid.uuid4())
    now = time.time()
    c = db._conn()
    c.execute("INSERT INTO chat_tasks (id, conversation_id, user_id, prompt, status, "
              "seen, created_at) VALUES (?,?,?,?,?,?,?)",
              (tid, ctx["cid"], user["id"], ctx["prompt"][:500], "running", 0, now))
    c.commit()
    c.close()
    _EXECUTOR.submit(_bg_run, ctx, dict(user), tid)
    return {"conversation_id": ctx["cid"], "task_id": tid, "status": "running"}


def _bg_run(ctx, user, tid):
    """Runs in a worker thread: execute the turn, then record task outcome."""
    status, error = "done", None
    try:
        _run_turn(ctx, user)
    except Exception as e:
        status, error = "failed", str(e)[:500]
    try:
        c = db._conn()
        c.execute("UPDATE chat_tasks SET status=?, error=?, finished_at=? WHERE id=?",
                  (status, error, time.time(), tid))
        c.commit()
        c.close()
    except Exception:
        pass


@router.get("/tasks/{tid}")
def task_status(tid: str, user=Depends(current_user)):
    c = db._conn()
    r = c.execute("SELECT id, conversation_id, user_id, status, error FROM chat_tasks WHERE id=?",
                  (tid,)).fetchone()
    c.close()
    if r is None or dict(r)["user_id"] != user["id"]:
        raise HTTPException(404, "Task not found")
    d = dict(r)
    d.pop("user_id", None)
    return d


@router.get("/conversations/{cid}/task")
def latest_task(cid: str, user=Depends(current_user)):
    """The most recent background task for a conversation — so reopening a chat
    whose task is still running resumes the live 'working…' state."""
    _own_or_404(cid, user)
    c = db._conn()
    r = c.execute("SELECT id, status, error FROM chat_tasks WHERE conversation_id=? AND user_id=? "
                  "ORDER BY created_at DESC LIMIT 1", (cid, user["id"])).fetchone()
    c.close()
    return dict(r) if r else {"status": "none"}


def _task_states(user_id):
    """Per-conversation task counts for the sidebar: how many are still running,
    and how many finished but haven't been seen (the blue dot)."""
    c = db._conn()
    rows = c.execute("SELECT conversation_id, status, seen FROM chat_tasks WHERE user_id=?",
                     (user_id,)).fetchall()
    c.close()
    out = {}
    for r in rows:
        d = out.setdefault(r["conversation_id"], {"running": 0, "unseen": 0})
        if r["status"] == "running":
            d["running"] += 1
        elif not r["seen"]:
            d["unseen"] += 1
    return out


def _mark_seen(cid, user_id):
    c = db._conn()
    c.execute("UPDATE chat_tasks SET seen=1 WHERE conversation_id=? AND user_id=? AND status!='running'",
              (cid, user_id))
    c.commit()
    c.close()


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
        from . import governance
        columns, rows = governance.filter_result(body.source, cleaned, columns, rows)
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
    stats["by_agent"] = _agent_tally(db.list_traces(limit=1000))
    # Where the learning is stored — surfaced so it's discoverable in the UI.
    stats["storage"] = {
        "rollouts_table": "agent_traces",
        "store": "postgres" if db.IS_PG else f"sqlite ({db.DB_PATH})",
        "learned_prompt": "prompts/system_learned.txt",
    }
    return stats


def _agent_tally(traces):
    """Rollup of rollouts per named agent (from each trace's meta.agents), so
    the learning dashboard shows which agents are called and how they score."""
    tally = {}
    for t in traces:
        meta = t.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        for name in (meta or {}).get("agents") or []:
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
    c = db._conn()
    n = lambda q: c.execute(q).fetchone()["n"]
    stats = {
        "total_rollouts": n("SELECT COUNT(*) n FROM agent_traces"),
        "usable": n("SELECT COUNT(*) n FROM agent_traces WHERE reward IS NOT NULL"),
        "distinct_prompts": n("SELECT COUNT(DISTINCT prompt) n FROM agent_traces WHERE prompt IS NOT NULL"),
        "human_labeled": n("SELECT COUNT(*) n FROM agent_traces WHERE reward_source='user'"),
    }
    c.close()
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


def _checkpoint(user, cid, result, model, source, table_scope):
    """Serialize the conversation as a resumable agent session after each turn,
    folding in this turn's token/cache usage. Best-effort — a snapshot failure
    must never fail the chat response."""
    try:
        transcript = [{"role": m["role"], "text": (m.get("content") or {}).get("text", "")}
                      for m in db.list_messages(cid)]
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
