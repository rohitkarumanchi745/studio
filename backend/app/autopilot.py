"""Autopilot agents — autonomous, proactive agents that fire on a TRIGGER
(not a chat), reason over data toward a GOAL, and take GOVERNED actions.

Studio already has the reactive substrate (a chat turn runs the agent under a
user's RBAC). Autopilot adds the *proactive* half: a persisted agent definition
that a background ticker runs HEADLESS as its OWNER when a trigger fires. The
four triggers are schedule (interval), threshold (a metric crosses a rule),
event (new data landed), and manual (run-now). Autonomy is ACT-WITH-APPROVAL:
the READ phase (query/chart/freshness) runs autonomously through the exact same
gate a human turn uses, and any WRITE/pipeline/platform action is PROPOSED to
`supervisor.submit`, which returns needs_human — a supervised job an admin
approves in Jobs before it ever executes.

Security invariants (non-negotiable):
- A run executes as its OWNER and can NEVER exceed the owner's RBAC. The owner
  identity + scope are re-resolved from the DB on EVERY run, so a demotion
  between create and run shrinks access. All SQL flows through
  `queries.verify_sql` / `agent.run_agent`'s guarded `run_sql` tool — the
  identical gate as a human turn. The ticker never elevates.
- No autonomous writes. `run_agent` has no DML path; the only way an autopilot
  run touches a write/pipeline is `supervisor.submit -> needs_human`. This
  module never calls a connector's run_script / submit_spark_job itself.
- A disabled/deleted agent never fires. trigger_config is whitelisted and never
  string-interpolated into SQL/DDL/identifiers.
- Dormant-safe: with no LLM key the read path still returns rows via the
  deterministic fallback / semantic layer; the ticker never crashes startup.

Scale note: the claim is a race-safe conditional UPDATE (SQLite serializes
writes; Postgres row-locks). The ticking itself is owned by the job worker
(jobs.Worker.run_schedulers): one lease-guarded tick_once() per interval, so
N web replicas or workers never run N tickers.
"""
import json
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from . import (agent, db, email_service, freshness, lightning, queries, rbac,
               roster, semantic, skills)
from .auth import current_user
from .sources import connector_or_400

router = APIRouter(prefix="/autopilot", tags=["autopilot"])

# This run's named agent, so its rollouts are attributed like every other agent.
AUTOPILOT_AGENT = {"name": "Autopilot", "role": "autopilot",
                   "produces": "triggered, governed runs"}

TRIGGER_TYPES = ("schedule", "threshold", "event", "manual")
_OPS = {
    "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
}
_ACTION_KINDS = ("sql_script", "spark_job", "platform_run")

# Bounds so trigger_config is injection-free (ints only, never interpolated).
_MIN_INTERVAL = 60
_MAX_INTERVAL = 2_592_000          # 30 days
_DEFAULT_POLL = 300                # threshold/event poll cadence
_CLAIM_STALE = 300                 # a claim older than this is considered dead
_RESULT_ROW_CAP = 200              # cap rows stored on a run so the table stays lean
_KEEP_RUNS = 200                   # runs kept per agent, newest first
_TICK_SECONDS = int(os.getenv("STUDIO_AUTOPILOT_TICK_SECONDS", "60"))


def init_tables():
    """Mirror the shape of queries.init_tables / supervisor.init_tables: TEXT
    uuid PKs, REAL epoch timestamps, INTEGER bool flags, JSON stored as TEXT
    (db._pg_sql maps ? -> %s and REAL -> DOUBLE PRECISION for Postgres)."""
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS autopilot_agents (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                name TEXT NOT NULL,
                goal TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_config TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL,
                tables TEXT NOT NULL DEFAULT '[]',
                model TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                next_run_at REAL,
                claimed_at REAL,
                last_seen_freshness TEXT,
                last_threshold_state INTEGER,
                last_checked_at REAL,
                last_check TEXT,
                conversation_id TEXT,
                visual INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_autopilot_owner
                ON autopilot_agents(owner_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_autopilot_due
                ON autopilot_agents(enabled, next_run_at);

            CREATE TABLE IF NOT EXISTS autopilot_runs (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                trigger TEXT,
                status TEXT NOT NULL,
                summary TEXT,
                result TEXT,
                trace_id TEXT,
                proposed_job_id TEXT,
                started_at REAL NOT NULL,
                finished_at REAL,
                seen_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_autopilot_runs_agent
                ON autopilot_runs(agent_id, started_at DESC);
            """
        )
        c.commit()


# ── Owner identity + scope (RBAC re-resolved at RUN time) ────────────────

def _owner_identity(owner_id):
    """Re-read the owner from the DB at RUN time so the role is always current —
    a demotion between create and run must shrink access, never a cached role."""
    u = db.get_user(owner_id)
    if not u:
        raise RuntimeError("owner no longer exists")
    return {"id": u["id"], "email": u["email"], "role": u["role"], "name": u["name"]}


def _build_scope(owner, source, tables):
    """Resolve the (connector, allowed_tables, schemas, table_param) for this
    owner + source + requested tables, re-checking RBAC. Raises PermissionError
    when the owner cannot reach the source or a requested table — used both at
    create time (born no wider than the owner) and at run time (a later demotion
    still shrinks it). Identical filtering to chat._prepare."""
    if not rbac.can_access(owner["role"], source, "*"):
        raise PermissionError("owner has no access to this source")
    connector = connector_or_400(source)           # raises HTTPException(400) if unknown
    allowed = rbac.allowed_tables(owner["role"], source, connector.list_tables())
    if not allowed:
        raise PermissionError("owner has no access to this source")
    picks = [t for t in (tables or []) if t]
    if picks:
        denied = [t for t in picks if t not in allowed]
        if denied:
            raise PermissionError(f"owner cannot access: {denied}")
        allowed = [t for t in allowed if t in picks]
    table_param = "*" if (not picks or len(allowed) > 1) else allowed[0]
    schemas = {}
    for t in allowed[:10]:
        try:
            schemas[t] = connector.get_schema(t)
        except Exception:
            pass
    return connector, allowed, schemas, table_param


def _within(sql, tables):
    """True when `sql` reads only `tables` (lowercase names) — every table it
    reads, comma joins and CTEs included (queryguard.base_tables). An agent
    with no declared tables (`tables` empty) may read any the owner can."""
    if not tables:
        return True
    from .queryguard import base_tables
    reads = base_tables(sql or "")
    return bool(reads) and reads <= tables


def _run_headless(owner, source, tables, goal, model=None, visual=False):
    """Answer the goal as the owner, with no conversation — in the order chat
    answers a question, so a scheduled report and a chat answer agree:
    a CERTIFIED metric's definition, then an exact VERIFIED query, then the
    agent. A tier's answer counts only when its SQL stays inside the agent's
    declared tables. Dormant-safe: with no LLM key the agent tier returns rows
    via its deterministic fallback."""
    out = _answer_goal(owner, source, tables, goal, model, visual)
    if visual:
        # A D3 & artifacts agent: every chart drawn with D3, as in that window.
        agent.d3_only(out, goal)
    return out


def _answer_goal(owner, source, tables, goal, model, visual):
    connector, allowed, schemas, table_param = _build_scope(owner, source, tables)
    declared = {t.lower() for t in allowed} if [t for t in (tables or []) if t] else set()
    single = None if table_param == "*" else table_param
    try:
        out = semantic.certified_answer(owner, [source], goal, single)
    except Exception:
        out = None
    if out is not None and _within(out.get("sql"), declared):
        out.setdefault("served_by", "semantic")
        return out
    try:
        from . import verified_sql
        match = verified_sql.match(owner, source, single or "*", goal)
        out = verified_sql.answer(owner, source, match) \
            if match and match["kind"] == "exact" else None
    except Exception:
        out = None
    if out is not None and _within(out.get("sql"), declared):
        out.setdefault("served_by", "verified_sql")
        return out
    skill_md = skills.get_skill(connector, owner["role"], allowed, schemas)
    return agent.run_agent(
        prompt=goal, connector=connector, table=table_param, allowed_tables=allowed,
        schemas=schemas, history=[], user=owner, model=model, skill_md=skill_md,
        visual_mode=bool(visual))


# ── Trigger evaluation ──────────────────────────────────────────────────

def _scalar(columns, rows, column=None):
    """Pull a single numeric value out of a metric result. Prefers a named
    column, else the first numeric value in the first row."""
    if not rows:
        return None
    row = list(rows[0])
    if column and columns and column in columns:
        v = row[columns.index(column)]
    else:
        v = next((x for x in row if isinstance(x, (int, float))), row[0] if row else None)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class TriggerError(Exception):
    """The trigger could not be checked — said in words, so the agent shows
    why instead of looking like a condition that simply was not met."""


def _threshold_metric(owner, agent_def):
    """Evaluate the threshold metric AS THE OWNER. Prefer the deterministic
    semantic layer (no LLM key needed); fall back to a guarded SQL through the
    exact human gate (queries.verify_sql = RBAC + query guard + governance +
    execution). Returns a float, or raises TriggerError saying why not."""
    cfg = agent_def["trigger_config"]
    source = agent_def["source"]
    declared = {str(t).lower() for t in (agent_def.get("tables") or []) if t}
    why = []
    if cfg.get("metric_prompt"):
        try:
            res = semantic.answer(owner, source, cfg["metric_prompt"], "*")
        except Exception as e:
            res, why = None, [f"the metric could not be read ({str(e)[:120]})"]
        if res and res.get("rows") and _within(res.get("sql"), declared):
            v = _scalar(res.get("columns"), res["rows"], cfg.get("column"))
            if v is not None:
                return v
        if not why:
            why.append(f"no defined metric matches “{cfg['metric_prompt']}” on {source}")
    if cfg.get("sql"):
        # Confine the threshold SQL to the agent's DECLARED tables — an agent
        # scoped to [sales] must not probe [customers] even though the owner
        # could; every table the SQL reads counts, comma joins included.
        # (verify_sql still enforces the owner's full RBAC on top.)
        if not _within(cfg["sql"], declared):
            raise TriggerError("the threshold SQL reads tables outside this agent's scope ("
                               + ", ".join(sorted(declared)) + ")")
        res = queries.verify_sql(owner, source, "*", cfg["sql"], full_rows=True)
        if not res.get("ok"):
            raise TriggerError(f"the threshold SQL failed: {str(res.get('error') or '')[:200]}")
        v = _scalar(res.get("columns"), res.get("rows") or [], cfg.get("column"))
        if v is None:
            raise TriggerError("the threshold SQL returned no number")
        return v
    raise TriggerError("; ".join(why) or "the threshold has no metric to check")


def _threshold_crossed(owner, agent_def):
    cfg = agent_def["trigger_config"]
    val = _threshold_metric(owner, agent_def)
    op = _OPS.get(cfg.get("op"))
    if op is None:
        raise TriggerError(f"unknown comparison {cfg.get('op')!r}")
    return bool(op(val, float(cfg["value"]))), val


def _event_advanced(owner, agent_def):
    """New-data check via freshness: the table's newest timestamp, read as the
    owner through the gateway (freshness.for_table). Returns the latest value
    as text; raises TriggerError when it cannot be read."""
    cfg = agent_def["trigger_config"]
    try:
        connector, allowed, _schemas, _tp = _build_scope(
            owner, agent_def["source"], agent_def.get("tables"))
    except PermissionError as e:
        raise TriggerError(f"the owner can no longer read this scope: {e}")
    table = cfg.get("table")
    if not table or table not in allowed:
        raise TriggerError(f"the owner can no longer read {table}")
    r = freshness.for_table(owner, agent_def["source"], table, connector=connector)
    if r.get("error"):
        raise TriggerError(f"could not read the newest row of {table}: {r['error']}")
    if r.get("latest") is None:
        raise TriggerError(r.get("note") and f"{table} has {r['note']}"
                           or f"{table} has no rows yet")
    return str(r["latest"])


def _should_fire(owner, agent_def, trigger):
    """Decide whether a due agent fires now. Returns (fire, field_updates,
    check): `check` is what this poll found — {"status", "value"?, "detail"?}
    — kept on the agent so a poll that does not fire is visible without a run.
    Manual and schedule always fire; a threshold fires on a false->true CROSS;
    new data fires when the newest row moved past the one seen before (the
    first poll only records where to start). Raises TriggerError."""
    tt = agent_def["trigger_type"]
    if trigger == "manual" or tt in ("manual", "schedule"):
        return True, {}, {"status": "fired"}
    if tt == "threshold":
        crossed, val = _threshold_crossed(owner, agent_def)
        prev = bool(agent_def.get("last_threshold_state"))
        fire = crossed and not prev
        status = "fired" if fire else "met" if crossed else "not met"
        return fire, {"last_threshold_state": 1 if crossed else 0}, \
            {"status": status, "value": val}
    if tt == "event":
        latest = _event_advanced(owner, agent_def)
        seen = agent_def.get("last_seen_freshness")
        if not seen:
            return False, {"last_seen_freshness": latest}, \
                {"status": "watching", "value": latest}
        fire = latest != seen
        return fire, {"last_seen_freshness": latest}, \
            {"status": "fired" if fire else "no new data", "value": latest}
    return False, {}, {"status": "error", "detail": f"unknown trigger {tt!r}"}


# ── The 'act with approval' seam ─────────────────────────────────────────

def _maybe_gate_action(agent_def, owner):
    """After the READ phase, if the agent definition carries an action block,
    PROPOSE it through supervisor.submit — which runs the SAME classify/RBAC gate
    a human hits and returns needs_human for any write/pipeline (an
    awaiting_approval supervised job an admin approves in Jobs). This module
    NEVER executes the write itself. Returns (job id, None), (None, None) for a
    pure read/alert agent, or (None, why) when the proposal failed — the run
    still lands, saying so."""
    action = (agent_def.get("trigger_config") or {}).get("action") or {}
    kind = action.get("kind")
    if kind not in _ACTION_KINDS:
        return None, None
    from . import supervisor  # CALL only — never edit supervisor.py
    try:
        job = supervisor.submit(kind, action.get("target") or agent_def["source"],
                                action.get("script") or "", owner)
    except Exception as e:
        detail = getattr(e, "detail", None) or str(e) or type(e).__name__
        return None, str(detail)[:300]
    if isinstance(job, dict) and job.get("id"):
        return job["id"], None
    return None, "the supervisor returned no job"


# ── The run loop ─────────────────────────────────────────────────────────

def _summarize(result):
    text = (result.get("text") or "").strip()
    return text[:500] if text else "(no output)"


def _capped_result(result, proposed_job_id=None, action_error=None):
    """The JSON stored on a run row: text/sql/chart + capped rows + panels,
    plus a pointer to any gated job so the UI links to Jobs."""
    out = {
        "text": result.get("text"),
        "sql": result.get("sql"),
        "columns": result.get("columns") or [],
        "rows": (result.get("rows") or [])[:_RESULT_ROW_CAP],
        "chart": result.get("chart"),
        "panels": [{"sql": p.get("sql"), "columns": p.get("columns"),
                    "rows": (p.get("rows") or [])[:_RESULT_ROW_CAP], "chart": p.get("chart"),
                    **({"visual": p["visual"]} if p.get("visual") else {}),
                    **({"engine": p["engine"]} if p.get("engine") else {})}
                   for p in (result.get("panels") or [])],
        "visual_mode": bool(result.get("visual_mode")),
        "mode": result.get("mode"),
        "served_by": result.get("served_by"),
        "row_count": len(result.get("rows") or []),
    }
    if proposed_job_id:
        out["proposed_job_id"] = proposed_job_id
    if action_error:
        out["action_error"] = action_error
    return out


def _email_owner(owner, agent_def, result, proposed_job_id):
    """Email the owner the run result / alert. Best-effort — a mail failure
    never fails the run."""
    try:
        subject = f"Autopilot: {agent_def['name']}"
        body = (result.get("text") or "Autopilot run completed.")
        if proposed_job_id:
            body += ("\n\nA governed action was proposed and is awaiting a human "
                     "administrator's approval in Studio -> Jobs.")
        html = email_service.report_html(
            subject, body, result.get("sql"), result.get("columns") or [],
            result.get("rows") or [],
            footer=f"Autopilot agent '{agent_def['name']}' ran as {owner['email']}.")
        email_service.send(owner["email"], f"Studio {subject}", html)
    except Exception:
        pass


def _run_agent_now(agent_def, trigger):
    """One poll of an (already loaded) agent. Resolves the owner fresh, checks
    the trigger, and — when it fires — answers the goal as the owner, gates
    any action through the supervisor, and records a run (an alert until the
    owner sees it), a lightning trace and an email.

    A poll that does not fire writes no run: what it found is kept on the
    agent (last_checked_at / last_check). A check that STARTS failing is one
    failed run — news once — and then shows on the agent until it recovers.
    Returns the run row, or {"status": "checked", "check": …} when nothing ran.
    Never raises to its caller (the ticker / the manual endpoint)."""
    run_id = str(uuid.uuid4())
    started = time.time()
    manual = trigger == "manual"

    try:
        owner = _owner_identity(agent_def["owner_id"])
    except Exception as e:
        return _record_run(run_id, agent_def, trigger, "failed", started,
                           summary=f"owner unavailable: {e}", seen=manual)

    try:
        fire, field_updates, check = _should_fire(owner, agent_def, trigger)
    except TriggerError as e:
        fire, field_updates, check = False, {}, {"status": "error", "detail": str(e)}
    except Exception as e:
        fire, field_updates, check = False, {}, {
            "status": "error", "detail": f"trigger evaluation error: {str(e)[:300]}"}
    if not manual:
        field_updates = {**field_updates, "last_checked_at": time.time(),
                         "last_check": json.dumps(check, default=str)}
    if field_updates:
        _update_fields(agent_def["id"], field_updates)

    if not fire:
        before = agent_def.get("last_check") or {}
        if check["status"] == "error" and before.get("status") != "error":
            return _record_run(run_id, agent_def, trigger, "failed", started,
                               summary=f"the trigger could not be checked: {check['detail']}")
        return {"id": None, "agent_id": agent_def["id"], "status": "checked", "check": check}

    t0 = time.time()
    try:
        result = _run_headless(owner, agent_def["source"], agent_def.get("tables"),
                               agent_def["goal"], agent_def.get("model"),
                               visual=bool(agent_def.get("visual")))
    except PermissionError as e:
        # The owner's RBAC no longer covers this scope (a demotion). Fail the run
        # — an autopilot agent can never run wider than its current owner.
        return _record_run(run_id, agent_def, trigger, "failed", started,
                           summary=f"owner RBAC denies this scope: {e}", seen=manual)
    except Exception as e:
        return _record_run(run_id, agent_def, trigger, "failed", started,
                           summary=f"agent error: {str(e)[:400]}", seen=manual)

    # Act-with-approval: propose any write/pipeline action; never execute it here.
    proposed_job_id, action_error = _maybe_gate_action(agent_def, owner)

    # Trace the rollout like every other run (best-effort; swallows its own errors).
    result_for_trace = {**result, "source": agent_def["source"], "table": "*"}
    trace_id = lightning.record_chat_trace(
        owner, None, agent_def["goal"], result_for_trace, int((time.time() - t0) * 1000))

    _email_owner(owner, agent_def, result, proposed_job_id)
    try:
        db.log_activity(owner, "autopilot_run", prompt=agent_def["goal"],
                        source=agent_def["source"], sql=result.get("sql"),
                        mode=result.get("mode"), row_count=len(result.get("rows") or []),
                        ok=True, duration_ms=int((time.time() - t0) * 1000))
    except Exception:
        pass

    summary = _summarize(result)
    if check.get("status") == "fired" and "value" in check and agent_def["trigger_type"] == "threshold":
        value = check["value"]
        shown = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
        summary = f"Threshold crossed (value {shown}). {summary}"[:500]
    if action_error:
        summary = f"{summary[:300]} — the governed action could not be proposed: {action_error}"[:500]
    return _record_run(run_id, agent_def, trigger, "done", started, summary=summary,
                       result=_capped_result(result, proposed_job_id, action_error),
                       trace_id=trace_id, proposed_job_id=proposed_job_id, seen=manual,
                       full=result)


def _record_run(run_id, agent_def, trigger, status, started, summary=None,
                result=None, trace_id=None, proposed_job_id=None, seen=False, full=None):
    """Write a run — unread (an alert) unless the owner started it — keep the
    agent's newest _KEEP_RUNS, and post it to the agent's chat (`full`: the
    whole answer, rows uncapped)."""
    now = time.time()
    row = {
        "id": run_id, "agent_id": agent_def["id"], "owner_id": agent_def["owner_id"],
        "trigger": trigger, "status": status, "summary": summary,
        "result": json.dumps(result, default=str) if result is not None else None,
        "trace_id": trace_id, "proposed_job_id": proposed_job_id,
        "started_at": started, "finished_at": now, "seen_at": now if seen else None,
    }
    cols = ("id,agent_id,owner_id,trigger,status,summary,result,trace_id,"
            "proposed_job_id,started_at,finished_at,seen_at")
    with db.connect() as c:
        c.execute(f"INSERT INTO autopilot_runs ({cols}) VALUES ({','.join('?' * 12)})",
                  tuple(row[k] for k in cols.split(",")))
        c.execute("DELETE FROM autopilot_runs WHERE agent_id=? AND id NOT IN ("
                  "SELECT id FROM autopilot_runs WHERE agent_id=? "
                  "ORDER BY started_at DESC LIMIT ?)",
                  (agent_def["id"], agent_def["id"], _KEEP_RUNS))
        c.commit()
    _deliver(agent_def, row, full, seen)
    return row


_POSTED_KEYS = ("text", "sql", "columns", "rows", "chart", "panels", "mode", "model",
                "served_by", "visual_mode", "errors", "inputs")


def _deliver(agent_def, run, result, seen):
    """Post a run to the conversation the agent was made in: the answer as
    the chat shows any answer (charts to the canvas, or drawn with D3 in a
    D3 & artifacts chat), or why it could not run. Best-effort — a chat that
    is gone or no longer the owner's to edit is skipped; the run stands."""
    cid = agent_def.get("conversation_id")
    if not cid:
        return
    meta = {"agent_id": agent_def["id"], "agent_name": agent_def["name"], "run_id": run["id"],
            "trigger": run["trigger"], "status": run["status"], "summary": run["summary"]}
    scope = ", ".join(agent_def.get("tables") or []) or "*"
    if run["status"] == "done" and result is not None:
        content = {k: result[k] for k in _POSTED_KEYS if k in result}
    else:
        content = {"text": f"Autopilot “{agent_def['name']}” couldn't run: {run['summary']}",
                   "mode": "autopilot_failed", "errors": [run["summary"]], "sql": None,
                   "columns": [], "rows": [], "chart": None, "panels": []}
    content.update({"source": agent_def["source"], "table": scope, "autopilot": meta})
    try:
        from . import chat
        chat.deliver_autopilot(cid, {"id": agent_def["owner_id"]}, content, seen=seen)
    except Exception:
        import logging
        logging.getLogger("studio.autopilot").warning(
            "autopilot: could not post run %s to its chat", run["id"], exc_info=True)


# ── Persistence helpers ─────────────────────────────────────────────────

def _agent_row(r):
    """Parse an autopilot_agents row into a dict with JSON columns decoded."""
    d = dict(r)
    d["trigger_config"] = json.loads(d["trigger_config"]) if d.get("trigger_config") else {}
    d["tables"] = json.loads(d["tables"]) if d.get("tables") else []
    d["enabled"] = bool(d.get("enabled"))
    try:
        d["last_check"] = json.loads(d["last_check"]) if d.get("last_check") else None
    except (TypeError, ValueError):
        d["last_check"] = None
    return d


def _run_row(r):
    d = dict(r)
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except Exception:
            d["result"] = None
    d.pop("owner_id", None)
    return d


def _get_agent(agent_id):
    with db.connect() as c:
        r = c.execute("SELECT * FROM autopilot_agents WHERE id=?", (agent_id,)).fetchone()
    return _agent_row(r) if r else None


def _update_fields(agent_id, fields):
    if not fields:
        return
    fields = {**fields, "updated_at": time.time()}
    sets = ", ".join(f"{k}=?" for k in fields)
    with db.connect() as c:
        c.execute(f"UPDATE autopilot_agents SET {sets} WHERE id=?",
                  list(fields.values()) + [agent_id])
        c.commit()


def _last_run(agent_id):
    with db.connect() as c:
        r = c.execute("SELECT status, started_at, summary FROM autopilot_runs "
                      "WHERE agent_id=? ORDER BY started_at DESC LIMIT 1", (agent_id,)).fetchone()
    return dict(r) if r else None


def _public_agent(d):
    """The AgentRow the frontend gets — no owner_id leak, last_run folded in."""
    return {
        "id": d["id"], "name": d["name"], "goal": d["goal"],
        "trigger_type": d["trigger_type"], "trigger_config": d["trigger_config"],
        "source": d["source"], "tables": d["tables"], "model": d.get("model"),
        "enabled": d["enabled"], "next_run_at": d.get("next_run_at"),
        "last_run": _last_run(d["id"]), "created_at": d.get("created_at"),
        "last_checked_at": d.get("last_checked_at"), "last_check": d.get("last_check"),
        "conversation_id": d.get("conversation_id"), "visual": bool(d.get("visual")),
        "conversation_title": _conversation_title(d.get("conversation_id")),
    }


def _conversation_title(cid):
    if not cid:
        return None
    with db.connect() as c:
        r = c.execute("SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()
    return r["title"] if r else None


# ── Validation (create/update) ───────────────────────────────────────────

def _int_interval(cfg, default=None):
    raw = cfg.get("interval_seconds", default)
    if raw is None:
        raise HTTPException(400, "interval_seconds is required")
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise HTTPException(400, "interval_seconds must be an integer")
    if not (_MIN_INTERVAL <= raw <= _MAX_INTERVAL):
        raise HTTPException(400, f"interval_seconds must be {_MIN_INTERVAL}..{_MAX_INTERVAL}")
    return raw


def _validate_action(action):
    """Validate the optional risky-action block. It is NEVER interpreted here —
    it is passed whole to supervisor.submit at run time, which re-validates."""
    if not action:
        return
    if not isinstance(action, dict):
        raise HTTPException(400, "action must be an object")
    if action.get("kind") not in _ACTION_KINDS:
        raise HTTPException(400, f"action.kind must be one of {_ACTION_KINDS}")
    if not isinstance(action.get("script", ""), str):
        raise HTTPException(400, "action.script must be a string")
    if action.get("target") is not None and not isinstance(action["target"], str):
        raise HTTPException(400, "action.target must be a string")


def _validate_trigger(trigger_type, cfg, allowed_tables):
    """Whitelist trigger_config per type. Never string-formats a value into SQL
    or an identifier — the sql path is passed whole to the query guard; table /
    column are validated against the real allowlist/enum. Returns a normalized
    config."""
    if trigger_type not in TRIGGER_TYPES:
        raise HTTPException(400, f"trigger_type must be one of {TRIGGER_TYPES}")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "trigger_config must be an object")
    out = {}
    if trigger_type == "schedule":
        out["interval_seconds"] = _int_interval(cfg)
    elif trigger_type == "threshold":
        if not (cfg.get("metric_prompt") or cfg.get("sql")):
            raise HTTPException(400, "threshold needs metric_prompt or sql")
        if cfg.get("metric_prompt") is not None and not isinstance(cfg["metric_prompt"], str):
            raise HTTPException(400, "metric_prompt must be a string")
        if cfg.get("sql") is not None and not isinstance(cfg["sql"], str):
            raise HTTPException(400, "sql must be a string")
        if cfg.get("op") not in _OPS:
            raise HTTPException(400, f"op must be one of {tuple(_OPS)}")
        try:
            out["value"] = float(cfg["value"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "value must be numeric")
        out["op"] = cfg["op"]
        if cfg.get("metric_prompt"):
            out["metric_prompt"] = cfg["metric_prompt"]
        if cfg.get("sql"):
            out["sql"] = cfg["sql"]
        if cfg.get("column") is not None:
            if not isinstance(cfg["column"], str):
                raise HTTPException(400, "column must be a string")
            out["column"] = cfg["column"]
        out["interval_seconds"] = _int_interval(cfg, _DEFAULT_POLL)
    elif trigger_type == "event":
        table = cfg.get("table")
        if not isinstance(table, str) or table not in allowed_tables:
            raise HTTPException(400, "event trigger 'table' must be a table the owner can access")
        out["table"] = table
        out["interval_seconds"] = _int_interval(cfg, _DEFAULT_POLL)
    # manual: no config
    action = cfg.get("action")
    if action:
        _validate_action(action)
        out["action"] = {"kind": action["kind"],
                         "target": action.get("target"),
                         "script": action.get("script", "")}
    return out


def _schedule_next(trigger_type, cfg):
    """next_run_at for an enabled agent. Manual agents are never polled (NULL)."""
    if trigger_type == "manual":
        return None
    interval = cfg.get("interval_seconds") or _DEFAULT_POLL
    return time.time() + interval


# ── API ──────────────────────────────────────────────────────────────────

class AgentIn(BaseModel):
    name: str
    goal: str
    trigger_type: str
    trigger_config: dict = {}
    source: str
    tables: Optional[List[str]] = None
    model: Optional[str] = None
    # Made from a chat: its runs are posted there. `visual`: made in the D3 &
    # artifacts window — its answers are drawn with D3 (custom visuals too).
    conversation_id: Optional[str] = None
    visual: bool = False


class AgentPatch(BaseModel):
    name: Optional[str] = None
    goal: Optional[str] = None
    trigger_config: Optional[dict] = None
    tables: Optional[List[str]] = None
    model: Optional[str] = None


def _validate_scope_or_403(user, source, tables):
    """The agent is born no wider than its owner: verify the owner can reach the
    source and every requested table right now. Raises 403 otherwise."""
    try:
        _connector, allowed, _schemas, _tp = _build_scope(user, source, tables)
    except PermissionError as e:
        raise HTTPException(403, f"Your role has no access to that scope: {e}")
    return allowed


def _validate_model_or_400(user, model):
    if model and model not in {m["spec"] for m in agent.available_models(user)}:
        raise HTTPException(400, f"Model '{model}' is not offered")
    return model


@router.get("")
def list_agents(user=Depends(current_user)):
    """Own agents; an admin sees all (they run the platform)."""
    with db.connect() as c:
        if user["role"] == "admin":
            rows = c.execute("SELECT * FROM autopilot_agents ORDER BY updated_at DESC "
                             "LIMIT 500").fetchall()
        else:
            rows = c.execute("SELECT * FROM autopilot_agents WHERE owner_id=? "
                             "ORDER BY updated_at DESC LIMIT 500", (user["id"],)).fetchall()
    return {"agents": [_public_agent(_agent_row(r)) for r in rows], "can_manage": True}


@router.post("", status_code=201)
def create_agent(body: AgentIn, user=Depends(current_user)):
    name, goal = body.name.strip(), body.goal.strip()
    if not name or not goal:
        raise HTTPException(400, "name and goal are required")
    if body.trigger_type not in TRIGGER_TYPES:
        raise HTTPException(400, f"trigger_type must be one of {TRIGGER_TYPES}")
    allowed = _validate_scope_or_403(user, body.source, body.tables)
    model = _validate_model_or_400(user, body.model)
    cfg = _validate_trigger(body.trigger_type, body.trigger_config or {}, allowed)
    if body.conversation_id and db.conversation_access(body.conversation_id, user["id"]) \
            not in ("owner", "edit"):
        # Posting into a chat needs the right to write in it.
        raise HTTPException(404, "Conversation not found")

    now = time.time()
    aid = str(uuid.uuid4())
    row = {
        "id": aid, "owner_id": user["id"], "name": name, "goal": goal,
        "trigger_type": body.trigger_type, "trigger_config": json.dumps(cfg),
        "source": body.source, "tables": json.dumps([t for t in (body.tables or []) if t]),
        "model": model, "enabled": 1,
        "next_run_at": _schedule_next(body.trigger_type, cfg),
        "claimed_at": None, "last_seen_freshness": None, "last_threshold_state": None,
        "conversation_id": body.conversation_id or None, "visual": 1 if body.visual else 0,
        "created_at": now, "updated_at": now,
    }
    cols = ("id,owner_id,name,goal,trigger_type,trigger_config,source,tables,model,"
            "enabled,next_run_at,claimed_at,last_seen_freshness,last_threshold_state,"
            "conversation_id,visual,created_at,updated_at")
    with db.connect() as c:
        c.execute(f"INSERT INTO autopilot_agents ({cols}) VALUES ({','.join('?' * 18)})",
                  tuple(row[k] for k in cols.split(",")))
        c.commit()
    db.log_activity(user, "autopilot_create", prompt=name, source=body.source,
                    mode=body.trigger_type)
    return _public_agent(_get_agent(aid))


class SeenIn(BaseModel):
    run_ids: Optional[List[str]] = None   # none: every unread alert


@router.get("/alerts")
def list_alerts(user=Depends(current_user)):
    """The signed-in user's alerts: runs of THEIR agents (an admin's too — an
    admin sees every agent, but is alerted only about their own), newest first,
    with how many are unread."""
    with db.connect() as c:
        unread = c.execute(
            "SELECT COUNT(*) AS n FROM autopilot_runs r JOIN autopilot_agents a "
            "ON a.id = r.agent_id WHERE r.owner_id=? AND r.seen_at IS NULL",
            (user["id"],)).fetchone()["n"]
        rows = c.execute(
            "SELECT r.id, r.agent_id, r.trigger, r.status, r.summary, r.proposed_job_id, "
            "r.started_at, r.seen_at, a.name AS agent_name FROM autopilot_runs r "
            "JOIN autopilot_agents a ON a.id = r.agent_id WHERE r.owner_id=? "
            "ORDER BY r.started_at DESC LIMIT 20", (user["id"],)).fetchall()
    return {"unread": int(unread or 0), "alerts": [dict(r) for r in rows]}


@router.post("/alerts/seen")
def mark_alerts_seen(body: SeenIn, user=Depends(current_user)):
    """Mark the user's own alerts read — the ones named, or all of them."""
    sql = "UPDATE autopilot_runs SET seen_at=? WHERE owner_id=? AND seen_at IS NULL"
    params = [time.time(), user["id"]]
    if body.run_ids is not None:
        ids = [i for i in body.run_ids if i][:500]
        if not ids:
            return {"marked": 0}
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        params += ids
    with db.connect() as c:
        cur = c.execute(sql, params)
        c.commit()
    return {"marked": getattr(cur, "rowcount", 0) or 0}


@router.get("/runs/{run_id}")
def get_run(run_id: str, user=Depends(current_user)):
    """One run in full, for its owner (or an admin)."""
    with db.connect() as c:
        r = c.execute("SELECT r.*, a.name AS agent_name FROM autopilot_runs r "
                      "JOIN autopilot_agents a ON a.id = r.agent_id WHERE r.id=?",
                      (run_id,)).fetchone()
    if not r or (r["owner_id"] != user["id"] and user["role"] != "admin"):
        raise HTTPException(404, "Run not found")
    return _run_row(r)


def _own_or_404(agent_id, user):
    """404 (not 403) for another user's agent id — no existence oracle. An admin
    may manage any agent (platform operator)."""
    d = _get_agent(agent_id)
    if d is None:
        raise HTTPException(404, "Agent not found")
    if d["owner_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(404, "Agent not found")
    return d


@router.get("/{agent_id}")
def get_agent(agent_id: str, user=Depends(current_user)):
    return _public_agent(_own_or_404(agent_id, user))


@router.patch("/{agent_id}")
def update_agent(agent_id: str, body: AgentPatch, user=Depends(current_user)):
    d = _own_or_404(agent_id, user)
    fields = {}
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(400, "name cannot be empty")
        fields["name"] = body.name.strip()
    if body.goal is not None:
        if not body.goal.strip():
            raise HTTPException(400, "goal cannot be empty")
        fields["goal"] = body.goal.strip()
    if body.model is not None:
        fields["model"] = _validate_model_or_400(user, body.model) or None
    # Re-validate scope against the OWNER's current RBAC on any table change.
    owner = _owner_identity(d["owner_id"])
    tables = body.tables if body.tables is not None else d["tables"]
    allowed = _validate_scope_or_403(owner, d["source"], tables)
    if body.tables is not None:
        fields["tables"] = json.dumps([t for t in body.tables if t])
    if body.trigger_config is not None:
        cfg = _validate_trigger(d["trigger_type"], body.trigger_config, allowed)
        fields["trigger_config"] = json.dumps(cfg)
        if d["enabled"]:
            fields["next_run_at"] = _schedule_next(d["trigger_type"], cfg)
    if body.trigger_config is not None or body.tables is not None:
        # A changed condition or scope starts its watch over: the old crossing,
        # the newest row seen and the last check describe a different trigger.
        fields.update({"last_threshold_state": None, "last_seen_freshness": None,
                       "last_check": None, "last_checked_at": None})
    if fields:
        _update_fields(agent_id, fields)
    return _public_agent(_get_agent(agent_id))


@router.post("/{agent_id}/enable")
def enable_agent(agent_id: str, user=Depends(current_user)):
    d = _own_or_404(agent_id, user)
    _update_fields(agent_id, {"enabled": 1,
                              "next_run_at": _schedule_next(d["trigger_type"], d["trigger_config"]),
                              "claimed_at": None})
    return _public_agent(_get_agent(agent_id))


@router.post("/{agent_id}/disable")
def disable_agent(agent_id: str, user=Depends(current_user)):
    _own_or_404(agent_id, user)
    _update_fields(agent_id, {"enabled": 0, "next_run_at": None, "claimed_at": None})
    return _public_agent(_get_agent(agent_id))


@router.delete("/{agent_id}")
def delete_agent(agent_id: str, user=Depends(current_user)):
    _own_or_404(agent_id, user)
    with db.connect() as c:
        c.execute("DELETE FROM autopilot_agents WHERE id=?", (agent_id,))
        c.execute("DELETE FROM autopilot_runs WHERE agent_id=?", (agent_id,))
        c.commit()
    db.log_activity(user, "autopilot_delete", prompt=agent_id)
    return {"deleted": True}


@router.post("/{agent_id}/run")
def run_now(agent_id: str, user=Depends(current_user)):
    """Manual trigger — runs immediately as the OWNER (re-checking RBAC). A
    disabled agent is fully inert: enable it to run. The scope re-check happens
    inside the run regardless."""
    d = _own_or_404(agent_id, user)
    if not d.get("enabled"):
        raise HTTPException(409, "Agent is disabled — enable it to run.")
    run = _run_agent_now(d, "manual")
    return _run_row_from_dict(run)


@router.get("/{agent_id}/runs")
def list_runs(agent_id: str, user=Depends(current_user)):
    _own_or_404(agent_id, user)
    with db.connect() as c:
        rows = c.execute("SELECT * FROM autopilot_runs WHERE agent_id=? "
                         "ORDER BY started_at DESC LIMIT 100", (agent_id,)).fetchall()
    return {"runs": [_run_row(r) for r in rows]}


def _run_row_from_dict(run):
    """Shape an in-memory run dict (from _run_agent_now) like a stored RunRow."""
    d = dict(run)
    if isinstance(d.get("result"), str):
        try:
            d["result"] = json.loads(d["result"])
        except Exception:
            d["result"] = None
    d.pop("owner_id", None)
    return d


# ── The scheduler (race-safe claim + background ticker) ──────────────────

def _claim_due(now):
    """Atomically claim ALL due, enabled, unclaimed(or stale-claimed) agents by
    stamping claimed_at, then read them back. The conditional UPDATE is the race
    guard: SQLite serializes writes and Postgres row-locks the matched rows, so
    two instances never both win the same agent. Returns the claimed agent dicts.

    Single-instance is fine for v1; because the claim is race-safe this lifts
    onto a durable task queue (Celery / Cloud Tasks) at scale with no data-model
    change."""
    stale = now - _CLAIM_STALE
    with db.connect() as c:
        c.execute(
            "UPDATE autopilot_agents SET claimed_at=? "
            "WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=? "
            "AND (claimed_at IS NULL OR claimed_at<?)",
            (now, now, stale))
        c.commit()
        rows = c.execute("SELECT * FROM autopilot_agents WHERE claimed_at=?", (now,)).fetchall()
    return [_agent_row(r) for r in rows]


def tick_once():
    """One scheduler pass: claim due agents, run each, reschedule. Each agent's
    run is isolated in try/except so one failure never kills the ticker.
    Called by the job worker while it holds the "autopilot" lease (jobs.py);
    tests call it directly — it needs no lease itself."""
    now = time.time()
    for agent_def in _claim_due(now):
        try:
            _run_agent_now(agent_def, agent_def["trigger_type"])
        except Exception:
            pass  # a run never crashes the ticker
        finally:
            # Reschedule the next poll and release the claim regardless of outcome.
            try:
                latest = _get_agent(agent_def["id"])
                if latest and latest["enabled"]:
                    _update_fields(agent_def["id"], {
                        "next_run_at": _schedule_next(latest["trigger_type"],
                                                      latest["trigger_config"]),
                        "claimed_at": None})
                else:
                    _update_fields(agent_def["id"], {"claimed_at": None})
            except Exception:
                pass


_tick = tick_once   # older tests/callers drive ticks by this name


def ticker_enabled():
    """STUDIO_AUTOPILOT_TICKER kill-switch (default on). Read on every tick so
    a disabled ticker means the worker's scheduler skips the autopilot lease."""
    return os.getenv("STUDIO_AUTOPILOT_TICKER", "1").lower() not in ("0", "false", "no")


def start_ticker():
    """Compatibility shim. The daemon ticker thread is gone: the job worker
    (jobs.Worker, started by main.py per STUDIO_WORKER_MODE, or
    `python -m app.worker`) runs tick_once() under the "autopilot" lease on
    the old cadence. Nothing to start here; the env guard keeps its meaning
    through ticker_enabled()."""
    return None


def stop_ticker():
    return None
