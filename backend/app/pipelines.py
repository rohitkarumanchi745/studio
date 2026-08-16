"""Prompt-built, access-verified data pipelines.

An agent turns a plain-English request into a pipeline: it routes the prompt
to the connected source whose tables best match the intent (a supply-chain
request lands on the warehouse's inventory / production / downtime tables; an
S3 or Snowflake source slots into the same router once configured), drafts an
ordered set of steps, and VERIFIES each one — RBAC check, query guard, real
execution — so a pipeline can only ever touch data the requesting user's role
may see.

Triggering a pipeline runs its steps in order, each re-verified against the
runner's role. If a step fails, the run stops, the user who triggered it is
emailed the failing step and error, and the failure is recorded. Every run is
traced through Agent Lightning (lightning.py) for observability and reward
signal — that is what "implement agent lightning" means here: the RL/trace
layer records pipeline runs, it does not orchestrate them.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agent, db, email_service, lightning, queries, rbac, suggest
from .auth import current_user
from .connectors import all_sources, get_connector

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

MAX_STEPS = 6


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS pipelines (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            source TEXT NOT NULL,
            steps TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'private',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id TEXT PRIMARY KEY,
            pipeline_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            steps_result TEXT,
            failed_step INTEGER,
            error TEXT,
            trace_id TEXT,
            emailed INTEGER NOT NULL DEFAULT 0,
            started_at REAL NOT NULL,
            finished_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pl
            ON pipeline_runs(pipeline_id, started_at DESC);
        """
    )
    c.commit()
    c.close()


# ── Intent routing: prompt → the source whose tables best match ─────────

def _accessible(user):
    """Configured sources the user's role may query, with schemas — the pool
    the router picks from. RBAC is applied here, so the router can never route
    to a source the user has no access to."""
    role = user["role"]
    out = []
    for meta in all_sources():
        if not meta["configured"] or meta["name"] not in rbac.allowed_sources(role):
            continue
        conn = get_connector(meta["name"])
        try:
            allowed = rbac.allowed_tables(role, meta["name"], conn.list_tables())
        except Exception:
            continue
        if not allowed:
            continue
        schemas = {}
        for t in allowed[:20]:
            try:
                schemas[t] = conn.get_schema(t)
            except Exception:
                schemas[t] = []
        out.append({"connector": conn, "allowed": allowed, "schemas": schemas})
    return out


def route(user, prompt):
    """Pick the accessible source whose tables best match the prompt, plus the
    ranked matching tables. Returns (source_entry, matched) or (None, [])."""
    from .catalog import match_tables

    best, best_score, best_matched = None, -1.0, []
    for s in _accessible(user):
        matched = match_tables(prompt, s["schemas"])
        score = sum(m["score"] for m in matched[:3])
        if score > best_score:
            best, best_score, best_matched = s, score, matched
    return best, best_matched


def _draft_sql(connector, table, columns):
    """A sensible verified step for one table: aggregate a measure over a
    dimension/date when the shape allows, else a bounded preview."""
    dates, measures, dims = suggest._classify(columns)
    if measures and (dates or dims):
        m = measures[0]
        g = dates[0] if dates else dims[0]
        return f"SELECT {g}, SUM({m}) AS total_{m} FROM {table} GROUP BY {g} ORDER BY 1 LIMIT 500"
    return f"SELECT * FROM {table} LIMIT 200"


def _llm_steps(user, source, skill_schemas, prompt, spec):
    """Ask the model for ordered SQL steps. Best-effort; caller falls back."""
    import re
    from langchain.chat_models import init_chat_model  # noqa: F401 (via make_llm)

    llm = agent.make_llm(spec, user)
    sys = ("You design a short data pipeline as ordered SQL steps for a business "
           "request. Return ONLY a JSON array of objects "
           '[{"name": str, "table": str, "sql": str}] — 1 to 5 steps, each a '
           "single read-only SELECT over the tables given. No prose.")
    schema_txt = "\n".join(
        f"- {t}({', '.join(c['name'] for c in cols)})" for t, cols in skill_schemas.items())
    payload = f"Source: {source}\nTables:\n{schema_txt}\n\nRequest: {prompt}"
    reply = llm.invoke([("system", sys), ("user", payload)])
    text = reply.content if isinstance(reply.content, str) else "".join(
        b.get("text", "") for b in reply.content if isinstance(b, dict))
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(text)
    return [d for d in data if isinstance(d, dict) and d.get("sql")][:MAX_STEPS]


def build(user, prompt):
    """Draft an access-verified pipeline from a prompt. Never saves. Every
    step is verified (RBAC + guard + execution); steps that don't verify are
    dropped, so a saved draft only contains runnable steps."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Describe what the pipeline should do")
    s, matched = route(user, prompt)
    if not s:
        raise HTTPException(403, "You have no access to a source that fits this request")
    source = s["connector"].name

    drafts = []
    spec = agent.llm_spec()
    if agent.llm_available(spec, user):
        try:
            drafts = _llm_steps(user, source, s["schemas"], prompt, spec)
        except Exception:
            drafts = []
    if not drafts:
        # Deterministic: one step per top matched table.
        for m in matched[:3]:
            t = m["table"]
            drafts.append({"name": f"Extract {t}", "table": t,
                           "sql": _draft_sql(s["connector"], t, s["schemas"].get(t, []))})
    if not drafts:
        raise HTTPException(422, "Could not draft any steps for this request")

    steps = []
    for d in drafts[:MAX_STEPS]:
        v = queries.verify_sql(user, source, d.get("table"), d.get("sql", ""))
        steps.append({
            "name": d.get("name") or f"Step {len(steps) + 1}",
            "source": source,
            "table": d.get("table"),
            "sql": v.get("sql") or d.get("sql", ""),
            "verified": v["ok"],
            "row_count": v.get("row_count"),
            "columns": v.get("columns", []),
            "error": None if v["ok"] else v.get("error"),
        })
    return {
        "prompt": prompt,
        "source": source,
        "matched_tables": [m["table"] for m in matched[:5]],
        "steps": [st for st in steps if st["verified"]] or steps,
        "dropped": [st for st in steps if not st["verified"]],
    }


# ── Execution: trigger a pipeline, email on failure, trace the run ──────

def run_pipeline(pipeline, user):
    """Execute steps in order; stop + email + trace on the first failure."""
    steps = pipeline["steps"]
    rid = str(uuid.uuid4())
    t0 = time.time()
    results, failed_step, error, status = [], None, None, "success"

    for i, step in enumerate(steps):
        v = queries.verify_sql(user, step["source"], step.get("table"), step["sql"])
        results.append({"name": step.get("name"), "source": step["source"],
                        "ok": v["ok"], "row_count": v.get("row_count"),
                        "columns": v.get("columns", []), "error": v.get("error"),
                        "sql": v.get("sql") or step["sql"]})
        if not v["ok"]:
            failed_step, error, status = i, v.get("error"), "failed"
            break

    dur = int((time.time() - t0) * 1000)
    trace_id = _trace(user, pipeline, status, results, error, dur)
    emailed = 0
    if status == "failed":
        emailed = 1 if _email_failure(user, pipeline, failed_step, error, results) else 0

    c = db._conn()
    c.execute(
        "INSERT INTO pipeline_runs (id, pipeline_id, user_id, status, steps_result, "
        "failed_step, error, trace_id, emailed, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, pipeline["id"], user["id"], status, json.dumps(results, default=str),
         failed_step, error, trace_id, emailed, t0, time.time()),
    )
    c.commit()
    c.close()
    db.log_activity(user, "pipeline_run", prompt=pipeline["name"],
                    source=pipeline["source"], ok=(status == "success"),
                    error=error, duration_ms=dur)
    return {"id": rid, "status": status, "failed_step": failed_step, "error": error,
            "steps_result": results, "trace_id": trace_id, "emailed": bool(emailed),
            "took_ms": dur}


def _trace(user, pipeline, status, results, error, dur):
    """Record the run through Agent Lightning's trace store (agent_traces)."""
    try:
        ok = status == "success"
        return db.add_trace(
            user, prompt=f"pipeline: {pipeline['name']}"[:1000],
            mode="pipeline", source=pipeline["source"],
            sql=results[-1]["sql"] if results else None,
            ok=ok, error=(error or "")[:500] if error else None,
            row_count=sum(r.get("row_count") or 0 for r in results),
            panel_count=len(results), duration_ms=dur,
            reward=1.0 if ok else 0.0, reward_source="pipeline",
            meta={"steps": len(results), "agl": lightning.agl_available()},
        )
    except Exception:
        return None


def _email_failure(user, pipeline, step_idx, error, results):
    step = results[step_idx] if step_idx is not None and step_idx < len(results) else {}
    html = (
        f"<p>Your pipeline <b>{pipeline['name']}</b> failed while you triggered it.</p>"
        f"<p><b>Step {(step_idx or 0) + 1}: {step.get('name', '?')}</b> "
        f"(source: {pipeline['source']})</p>"
        f"<pre style='background:#f6f6f6;padding:8px;border-radius:6px'>{step.get('sql', '')}</pre>"
        f"<p style='color:#b00'>{error}</p>"
        f"<p>Fix the step in Studio and re-run.</p>"
    )
    try:
        email_service.send(user["email"], f"Pipeline failed: {pipeline['name']}", html)
        return True
    except Exception:
        return False


# ── Persistence + API ───────────────────────────────────────────────────

def _row(r):
    d = dict(r)
    for k in ("steps", "steps_result"):
        if k in d and d.get(k):
            d[k] = json.loads(d[k])
    return d


def _own_or_404(pid, user, *, edit=False):
    c = db._conn()
    row = c.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone()
    c.close()
    if row is None:
        raise HTTPException(404, "Pipeline not found")
    d = _row(row)
    owner = d["user_id"] == user["id"]
    if not owner and d["visibility"] != "org":
        raise HTTPException(404, "Pipeline not found")  # 404, not 403 — no oracle
    if edit and not owner:
        raise HTTPException(403, "Only the owner can change this pipeline")
    return d


class BuildIn(BaseModel):
    prompt: str


@router.post("/build")
def build_endpoint(body: BuildIn, user=Depends(current_user)):
    """Draft a pipeline from a prompt (agent routes + verifies). Not saved."""
    return build(user, body.prompt)


class SaveIn(BaseModel):
    name: str | None = None
    prompt: str
    source: str
    steps: list
    visibility: str = "private"


@router.post("", status_code=201)
def create(body: SaveIn, user=Depends(current_user)):
    """Save a pipeline — but only if every step verifies (re-run server-side)."""
    if not body.steps:
        raise HTTPException(400, "A pipeline needs at least one step")
    verified = []
    for st in body.steps[:MAX_STEPS]:
        v = queries.verify_sql(user, st.get("source") or body.source,
                               st.get("table"), st.get("sql", ""))
        if not v["ok"]:
            raise HTTPException(400, f"Step '{st.get('name', '?')}' does not verify: {v['error']}")
        verified.append({"name": st.get("name") or f"Step {len(verified) + 1}",
                         "source": st.get("source") or body.source, "table": st.get("table"),
                         "sql": v["sql"], "verified": True,
                         "row_count": v["row_count"], "columns": v["columns"]})
    name = (body.name or body.prompt)[:120]
    visibility = body.visibility if body.visibility in ("private", "org") else "private"
    now = time.time()
    pid = str(uuid.uuid4())
    c = db._conn()
    c.execute(
        "INSERT INTO pipelines (id, user_id, name, prompt, source, steps, visibility, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, user["id"], name, body.prompt, body.source, json.dumps(verified),
         visibility, now, now),
    )
    c.commit()
    c.close()
    db.log_activity(user, "pipeline_save", prompt=name, source=body.source)
    return get(pid, user)


@router.get("")
def listing(user=Depends(current_user)):
    c = db._conn()
    rows = c.execute(
        "SELECT * FROM pipelines WHERE user_id=? OR visibility='org' ORDER BY updated_at DESC",
        (user["id"],)).fetchall()
    c.close()
    out = []
    for r in rows:
        d = _row(r)
        d["mine"] = d["user_id"] == user["id"]
        d["step_count"] = len(d.get("steps") or [])
        out.append(d)
    return {"pipelines": out, "agent_lightning": lightning.agl_available()}


@router.get("/{pid}")
def get(pid: str, user=Depends(current_user)):
    d = _own_or_404(pid, user)
    d["mine"] = d["user_id"] == user["id"]
    return d


@router.delete("/{pid}")
def remove(pid: str, user=Depends(current_user)):
    _own_or_404(pid, user, edit=True)
    c = db._conn()
    c.execute("DELETE FROM pipeline_runs WHERE pipeline_id=?", (pid,))
    c.execute("DELETE FROM pipelines WHERE id=?", (pid,))
    c.commit()
    c.close()
    return {"deleted": True}


@router.post("/{pid}/run")
def trigger(pid: str, user=Depends(current_user)):
    """Trigger a pipeline. Steps re-verify against the runner's role, so an
    org-shared pipeline still can't touch tables this user can't see."""
    pipeline = _own_or_404(pid, user)
    return run_pipeline(pipeline, user)


@router.get("/{pid}/runs")
def runs(pid: str, user=Depends(current_user)):
    _own_or_404(pid, user)
    c = db._conn()
    rows = c.execute(
        "SELECT * FROM pipeline_runs WHERE pipeline_id=? ORDER BY started_at DESC LIMIT 50",
        (pid,)).fetchall()
    c.close()
    return {"runs": [_row(r) for r in rows]}
