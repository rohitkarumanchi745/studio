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

from . import agent, db, email_service, gateway, grains, jobs, lightning, queries, queryguard, rbac, suggest, util
from .auth import current_user
from .connectors import all_sources, get_connector
from .matching import _tokens, match_tables
from .queryguard import TABLE_REF

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

MAX_STEPS = 6


def init_tables():
    with db.connect() as c:
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
    from . import sql_recovery
    sql_recovery.init_tables()


# ── Intent routing: prompt → the source whose tables best match ─────────

def _accessible(user):
    """Configured sources the user's role may query, with schemas — the pool
    the router picks from. RBAC is applied here, so the router can never route
    to a source the user has no access to."""
    role = user["role"]

    def build(meta):
        if not meta["configured"] or meta["name"] not in rbac.allowed_sources(role):
            return None
        conn = get_connector(meta["name"])
        try:
            allowed = rbac.allowed_tables(role, meta["name"], conn.list_tables())
        except Exception:
            return None
        if not allowed:
            return None
        tabs = allowed[:20]
        cols = util.pmap(conn.get_schema, tabs, default=[])
        schemas = {t: (c or []) for t, c in zip(tabs, cols)}
        return {"connector": conn, "allowed": allowed, "schemas": schemas}

    # Independent per source → probe concurrently.
    return [e for e in util.pmap(build, all_sources(), workers=6) if e]


def route(user, prompt):
    """Pick the accessible source whose tables best match the prompt, plus the
    ranked matching tables. Returns (source_entry, matched) or (None, [])."""
    best, best_score, best_matched = None, -1.0, []
    for s in _accessible(user):
        matched = match_tables(prompt, s["schemas"])
        score = sum(m["score"] for m in matched[:3])
        if score > best_score:
            best, best_score, best_matched = s, score, matched
    return best, best_matched


def _step_tables(step):
    """Tables a step reads, from its SQL (falls back to the declared table)."""
    refs = {r.strip('"').split(".")[-1].lower()
            for r in TABLE_REF.findall(step.get("sql") or "")}
    if not refs and step.get("table"):
        refs = {step["table"].lower()}
    return sorted(refs)


def lineage(steps, failed_index=None):
    """Provenance graph for a set of steps: which SOURCE feeds which TABLE feeds
    which STEP. Rendered as a diagram under a pipeline so a multi-source request
    shows exactly where each table comes from; the failing step is marked."""
    sources, tables, snodes, edges = {}, {}, [], []
    for i, st in enumerate(steps):
        src = st.get("source") or "?"
        sources.setdefault(src, {"id": f"s:{src}", "label": src})
        step_tables = []
        for t in _step_tables(st):
            tid = f"t:{src}.{t}"
            if tid not in tables:
                tables[tid] = {"id": tid, "label": t, "source": src}
                edges.append({"from": f"s:{src}", "to": tid})
            step_tables.append(tid)
        sid = f"step:{i}"
        snodes.append({"id": sid, "label": st.get("name") or f"Step {i + 1}",
                       "index": i, "tables": step_tables,
                       "failed": i == failed_index})
        for tid in step_tables:
            edges.append({"from": tid, "to": sid})
    return {
        "sources": list(sources.values()),
        "tables": list(tables.values()),
        "steps": snodes,
        "edges": edges,
        "multi_source": len(sources) > 1,
    }


def _prompt_match(names, terms):
    """The first column in `names` whose own tokens overlap the prompt's, or
    None. Order is the caller's ranking (suggest._classify already ranks
    measures best-first), so this is "the best column the user asked for"."""
    for n in names:
        if _tokens(str(n).replace("_", " ")) & terms:
            return n
    return None


def _relevant(table, columns, terms):
    """Does this table hold a column the prompt actually named? The
    deterministic drafter uses this to stop stepping over tables that only
    matched on an unrelated token — "monthly revenue by region" must not draft
    a step on customers just because it ranked third."""
    _, measures, dims = suggest._classify(columns)
    return bool(_prompt_match(measures, terms) or _prompt_match(dims, terms))


def _draft_sql(connector, table, columns, prompt=None):
    """A sensible verified step for one table, STEERED BY THE PROMPT.

    When the request names a time grain ("monthly revenue by region"), the
    step buckets the date column to that grain in the connector's own dialect
    (grains.bucket_expr), aggregates the measure the prompt named, and groups
    by the dimension it named. Ignoring the grain is how this drafter used to
    answer a monthly question with a daily table.

    With no grain in the prompt the shape is unchanged from before — first
    measure over the first date/dimension — so existing behaviour is kept for
    every request that never asked for a bucket.
    """
    dates, measures, dims = suggest._classify(columns)
    if not (measures and (dates or dims)):
        return f"SELECT * FROM {table} LIMIT 200"
    terms = _tokens(prompt or "")
    grain = grains.detect(prompt or "")
    bucket = grains.bucket_expr(connector.dialect, dates[0], grain) if (grain and dates) else None
    if not bucket:
        # No grain asked for (or no date column to bucket): today's shape.
        m = measures[0]
        g = dates[0] if dates else dims[0]
        return f"SELECT {g}, SUM({m}) AS total_{m} FROM {table} GROUP BY {g} ORDER BY 1 LIMIT 500"
    m = _prompt_match(measures, terms) or measures[0]
    dim = _prompt_match(dims, terms)
    select = [f"{bucket} AS {grain}"] + ([dim] if dim else [])
    group = [bucket] + ([dim] if dim else [])
    return (f"SELECT {', '.join(select)}, SUM({m}) AS total_{m} FROM {table} "
            f"GROUP BY {', '.join(group)} ORDER BY 1 LIMIT 500")


def _intent_warnings(prompt, sql):
    """Ways a drafted step visibly does NOT answer the request. Reported on the
    step (and badged in the UI) — never a reason to drop it: a step that
    verified is real data, it just may not be the breakdown that was asked
    for, and only the user can judge that."""
    grain = grains.detect(prompt or "")
    if grain and not grains.has_bucket(sql, grain):
        return [f"does not bucket by {grain}"]
    return []


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


def build(user, prompt, *, source=None, tables=None, model=None, sql_only=False, planner_context=None):
    """Draft an access-verified pipeline from a prompt. Never saves.

    Every drafted step is verified (RBAC + guard + real execution). Only the
    ones that PASSED come back in "steps"; every failure comes back in
    "dropped" with its error. If none passed, "steps" is empty and the
    response is still 200 — an empty pipeline the UI can explain beats a list
    of steps labelled "verified" that were never anything of the kind.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Describe what the pipeline should do")
    selection = list(dict.fromkeys(t for t in (tables or []) if t and t != "*"))
    if source and source != "*":
        try:
            connector, allowed = gateway.scope(user, source)
        except queryguard.QueryRejected as e:
            raise HTTPException(403, str(e)) from e
        if any(t not in allowed for t in selection):
            raise HTTPException(403, "Your role has no access to the selected tables")
        tabs = selection or allowed[:20]
        cols = util.pmap(connector.get_schema, tabs, default=[])
        s = {"connector": connector, "allowed": allowed,
             "schemas": {t: c or [] for t, c in zip(tabs, cols)}}
        matched = match_tables(prompt, s["schemas"])
    elif selection:
        # All-source routing still respects an explicit table selection.
        pool = []
        for entry in _accessible(user):
            schemas = {t: c for t, c in entry["schemas"].items() if t in selection}
            if schemas:
                ranked = match_tables(prompt, schemas)
                pool.append((sum(m["score"] for m in ranked[:3]),
                             {**entry, "schemas": schemas}, ranked))
        _, s, matched = max(pool, key=lambda p: p[0]) if pool else (0, None, [])
    else:
        s, matched = route(user, prompt)
    if not s:
        raise HTTPException(403, "You have no access to a source that fits this request")
    source = s["connector"].name

    drafts = []
    spec = model or agent.llm_spec()
    if agent.llm_available(spec, user):
        try:
            # Experience guides the model only. It must not change routing,
            # deterministic templates, intent checks, or the saved requirement.
            model_prompt = prompt + ("\n\n" + planner_context if planner_context else "")
            drafts = _llm_steps(user, source, s["schemas"], model_prompt, spec)
        except Exception:
            drafts = []
    generation = "model" if drafts else "deterministic"
    if not drafts:
        # Deterministic: one step per top matched table that actually holds a
        # column the prompt named. `matched` ranks by token overlap, which can
        # rank a table on a coincidental hit; without this filter a request
        # drafts steps over tables it never mentioned. Never filter down to
        # nothing, though — a name-only match (e.g. "inventory levels") is a
        # real signal, so fall back to the ranking when nothing survives.
        terms = _tokens(prompt)
        top = [m for m in matched[:3]
               if _relevant(m["table"], s["schemas"].get(m["table"], []), terms)] or matched[:3]
        for m in top:
            t = m["table"]
            drafts.append({"name": f"Extract {t}", "table": t,
                           "sql": _draft_sql(s["connector"], t, s["schemas"].get(t, []), prompt)})
    if not drafts:
        raise HTTPException(422, "Could not draft any steps for this request")

    steps = []
    for d in drafts[:MAX_STEPS]:
        jobs.check_claim()
        # A model cannot relabel SQL from another source as this selection,
        # nor escape a narrower table selection just because RBAC permits it.
        try:
            if d.get("source") and d["source"] != source:
                raise queryguard.QueryRejected("Step names a source outside the selected source")
            if selection or sql_only:
                queryguard.validate(d.get("sql", ""), selection or s["allowed"],
                                    qualifiers=s["connector"].qualifiers(),
                                    dialect=s["connector"].dialect)
            v = queries.verify_sql(user, source, d.get("table"), d.get("sql", ""))
        except queryguard.QueryRejected as e:
            v = {"ok": False, "error": str(e)}
        sql = v.get("sql") or d.get("sql", "")
        step = {
            "name": d.get("name") or f"Step {len(steps) + 1}",
            "source": d.get("source") or source,
            "table": d.get("table"),
            "sql": sql,
            "verified": v["ok"],
            "row_count": v.get("row_count"),
            "columns": v.get("columns", []),
            "error": None if v["ok"] else v.get("error"),
            "intent_warnings": _intent_warnings(prompt, sql),
        }
        steps.append(step)
    # INVARIANT: "steps" contains ONLY steps that verified. A step that failed
    # RBAC / the guard / execution is never handed back as part of the
    # pipeline — it goes to "dropped" WITH its error so the UI can say why.
    # When nothing verified this is an empty list and a 200: the caller gets a
    # truthful "nothing could be built and here is why", not a pipeline of
    # steps that would fail the moment it ran.
    kept = [st for st in steps if st["verified"]]
    return {
        "prompt": prompt,
        "source": source,
        "matched_tables": [m["table"] for m in matched[:5]],
        "steps": kept,
        "dropped": [st for st in steps if not st["verified"]],
        "repo": _pick_repo(prompt),
        "lineage": lineage(kept),
        "generation": generation,
        "warnings": (["Built using schema-based templates. Review the SQL: filters and complex "
                      "requirements may need refinement."] if generation == "deterministic" else []),
    }


def _pick_repo(prompt):
    """The registered GitHub repo whose scripts best fit this prompt (if any).
    Best-effort — pipelines still build with no repos registered."""
    try:
        from . import repos
        best, _ = repos.pick(prompt)
        if not best or best.get("score", 0) <= 0:
            return None
        return {"name": best["name"], "url": best["url"],
                "description": best.get("description"), "score": best["score"]}
    except Exception:
        return None


# ── Execution: trigger a pipeline, email on failure, trace the run ──────

def _run_response(row, steps):
    d = _row(row)
    end = d.get("finished_at") or time.time()
    return {"id": d["id"], "status": d["status"], "failed_step": d.get("failed_step"),
            "error": d.get("error"), "steps_result": d.get("steps_result") or [],
            "trace_id": d.get("trace_id"), "emailed": bool(d.get("emailed")),
            "lineage": lineage(steps, failed_index=d.get("failed_step")),
            "took_ms": int((end - d["started_at"]) * 1000)}


def run_pipeline(pipeline, user, *, run_id=None, notify_failure=True):
    """Execute steps in order; stop + trace on the first failure.

    A caller-supplied run id deduplicates persisted runs. Read-only queries
    can repeat after an interrupted attempt; the first completed attempt wins
    one durable result. A retry of that result performs access checks, never
    re-executes its queries. Chat disables failure email explicitly.
    """
    steps = pipeline["steps"]
    if not steps:
        raise HTTPException(400, "A pipeline needs at least one step")
    jobs.check_claim()
    rid = run_id or str(uuid.uuid4())
    t0 = time.time()
    with db.connect() as c:
        c.execute(
            "INSERT INTO pipeline_runs (id, pipeline_id, user_id, status, started_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (rid, pipeline["id"], user["id"], "running", t0))
        row = c.execute("SELECT * FROM pipeline_runs WHERE id=?", (rid,)).fetchone()
        c.commit()
    if row["pipeline_id"] != pipeline["id"] or row["user_id"] != user["id"]:
        raise HTTPException(409, "This run request already belongs to another pipeline")
    if (pipeline.get("agent_recovery") or {}).get("enabled") is True:
        from . import sql_recovery
        sql_recovery.watch(pipeline, user, rid)
    if row["status"] != "running":
        # Old verified metadata is not evidence of today's permissions.
        for step in steps:
            jobs.check_claim()
            try:
                gateway.check(user, step["source"], step["sql"],
                              table_label=step.get("table") or "*")
            except queryguard.QueryRejected as e:
                raise HTTPException(403, str(e)) from e
        if not row["trace_id"]:
            # A worker may finish the execution and stop before recording
            # learning. Retrying repairs that gap using the same outcome id.
            stored = _row(row)
            trace_id = _trace(user, pipeline, row["status"], stored.get("steps_result") or [],
                              row["error"], int(((row["finished_at"] or t0) - row["started_at"]) * 1000),
                              run_id=rid)
            if trace_id:
                with db.connect() as c:
                    c.execute("UPDATE pipeline_runs SET trace_id=? WHERE id=? AND trace_id IS NULL", (trace_id, rid))
                    c.commit()
                    row = c.execute("SELECT * FROM pipeline_runs WHERE id=?", (rid,)).fetchone()
        _observe_agent_recovery(pipeline, rid)
        return _run_response(row, steps)
    results, failed_step, error, status = [], None, None, "success"

    for i, step in enumerate(steps):
        jobs.check_claim()
        v = queries.verify_sql(user, step["source"], step.get("table"), step["sql"])
        results.append({"name": step.get("name"), "source": step["source"],
                        "ok": v["ok"], "row_count": v.get("row_count"),
                        "columns": v.get("columns", []), "error": v.get("error"),
                        "sql": v.get("sql") or step["sql"]})
        if not v["ok"]:
            failed_step, error, status = i, v.get("error"), "failed"
            break

    jobs.check_claim()
    finished = time.time()
    with db.connect() as c:
        cur = c.execute(
            "UPDATE pipeline_runs SET status=?, steps_result=?, failed_step=?, error=?, "
            "finished_at=? WHERE id=? AND user_id=? AND status='running'",
            (status, json.dumps(results, default=str), failed_step, error,
             finished, rid, user["id"]),
        )
        c.commit()
        completed_here = cur.rowcount == 1
    if completed_here:
        jobs.check_claim()
        dur = int((finished - t0) * 1000)
        trace_id = _trace(user, pipeline, status, results, error, dur, run_id=rid)
        emailed = 0
        if status == "failed" and notify_failure:
            jobs.check_claim()
            emailed = 1 if _email_failure(user, pipeline, failed_step, error, results) else 0
        jobs.check_claim()
        with db.connect() as c:
            c.execute("UPDATE pipeline_runs SET trace_id=?, emailed=? WHERE id=?",
                      (trace_id, emailed, rid))
            c.commit()
        db.log_activity(user, "pipeline_run", prompt=pipeline["name"],
                        source=pipeline["source"], ok=(status == "success"),
                        error=error, duration_ms=dur)
    with db.connect() as c:
        row = c.execute("SELECT * FROM pipeline_runs WHERE id=?", (rid,)).fetchone()
    _observe_agent_recovery(pipeline, rid)
    return _run_response(row, steps)


def _observe_agent_recovery(pipeline, run_id):
    if (pipeline.get("agent_recovery") or {}).get("enabled") is not True:
        return
    from . import sql_recovery
    try:
        sql_recovery.observe(run_id)
    except jobs.ClaimLost:
        raise
    except Exception:
        # Execution is already durable. The reconciler repairs a missed
        # enqueue; don't misreport a completed read as an execution error.
        sql_recovery.log.exception("Could not schedule SQL agent recovery for %s", run_id)


def _trace(user, pipeline, status, results, error, dur, *, run_id):
    """Keep the whole attempted recipe, not only its last query, as experience."""
    return lightning.record_pipeline_outcome(
        user, run_id=run_id, prompt=pipeline.get("prompt") or pipeline["name"],
        source=pipeline["source"],
        action={"type": "sql_pipeline", "steps": pipeline["steps"]},
        status=status, error=error, duration_ms=dur,
        conversation_id=pipeline.get("conversation_id"),
        repairs_run_id=pipeline.get("repairs_run_id"))


def _email_failure(user, pipeline, step_idx, error, results):
    step = results[step_idx] if step_idx is not None and step_idx < len(results) else {}
    defn = pipeline["steps"][step_idx] if step_idx is not None and step_idx < len(pipeline["steps"]) else {}
    src = step.get("source") or pipeline["source"]
    tbls = ", ".join(_step_tables(defn)) or defn.get("table") or "—"
    html = (
        f"<p>Your pipeline <b>{pipeline['name']}</b> failed while you triggered it.</p>"
        f"<p><b>Step {(step_idx or 0) + 1}: {step.get('name', '?')}</b></p>"
        f"<p>Source <b>{src}</b> → table(s) <b>{tbls}</b></p>"
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
    with db.connect() as c:
        row = c.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone()
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
    return save_pipeline(body, user)


def _recipe(steps, source):
    return [(s.get("source") or source, s.get("table"),
             (s.get("sql") or "").strip().rstrip(";")) for s in steps]


def save_pipeline(body: SaveIn, user, *, pipeline_id=None):
    """Save a pipeline — but only if EVERY step verifies here, server-side.

    A client's `verified` flag is never trusted: each step is re-run through
    RBAC + guard + execution under THIS user, and the first failure rejects
    the whole save. That is what makes "saved" mean "runnable", and it is the
    reason build() may return an empty step list rather than a hopeful one.
    """
    if not body.steps:
        raise HTTPException(400, "A pipeline needs at least one step")
    if len(body.steps) > MAX_STEPS:
        # Truncating silently would save fewer steps than the user submitted.
        raise HTTPException(400, f"A pipeline can have at most {MAX_STEPS} steps")
    if any(not isinstance(st, dict) for st in body.steps):
        raise HTTPException(400, "Each step must be an object with a sql field")
    jobs.check_claim()
    pid = pipeline_id or str(uuid.uuid4())
    if pipeline_id:
        with db.connect() as c:
            existing = c.execute("SELECT * FROM pipelines WHERE id=?", (pid,)).fetchone()
        if existing is not None:
            saved = _row(existing)
            if (saved["user_id"] != user["id"] or saved["prompt"] != body.prompt
                    or saved["source"] != body.source
                    or _recipe(saved["steps"], saved["source"]) != _recipe(body.steps, body.source)):
                raise HTTPException(409, "This request already saved a different pipeline")
            for st in saved["steps"]:
                jobs.check_claim()
                try:
                    gateway.check(user, st["source"], st["sql"], table_label=st.get("table") or "*")
                except queryguard.QueryRejected as e:
                    raise HTTPException(403, str(e)) from e
            return get(pid, user)
    verified = []
    for st in body.steps:
        jobs.check_claim()
        v = queries.verify_sql(user, st.get("source") or body.source,
                               st.get("table"), st.get("sql", ""))
        if not v["ok"]:
            raise HTTPException(400, f"Step '{st.get('name', '?')}' does not verify: {v['error']}")
        verified.append({"name": st.get("name") or f"Step {len(verified) + 1}",
                         "source": st.get("source") or body.source, "table": st.get("table"),
                         "sql": v["sql"], "verified": True,
                         "row_count": v["row_count"], "columns": v["columns"],
                         "intent_warnings": _intent_warnings(body.prompt, v["sql"])})
    name = (body.name or body.prompt)[:120]
    visibility = body.visibility if body.visibility in ("private", "org") else "private"
    now = time.time()
    jobs.check_claim()
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO pipelines (id, user_id, name, prompt, source, steps, visibility, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (pid, user["id"], name, body.prompt, body.source, json.dumps(verified),
             visibility, now, now),
        )
        c.commit()
        inserted = cur.rowcount == 1
    if not inserted:
        return save_pipeline(body, user, pipeline_id=pid)
    db.log_activity(user, "pipeline_save", prompt=name, source=body.source)
    return get(pid, user)


@router.get("")
def listing(user=Depends(current_user)):
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM pipelines WHERE user_id=? OR visibility='org' ORDER BY updated_at DESC",
            (user["id"],)).fetchall()
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
    d["lineage"] = lineage(d.get("steps") or [])
    return d


@router.delete("/{pid}")
def remove(pid: str, user=Depends(current_user)):
    _own_or_404(pid, user, edit=True)
    with db.connect() as c:
        c.execute("DELETE FROM pipeline_runs WHERE pipeline_id=?", (pid,))
        c.execute("DELETE FROM pipelines WHERE id=?", (pid,))
        c.commit()
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
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM pipeline_runs WHERE pipeline_id=? ORDER BY started_at DESC LIMIT 50",
            (pid,)).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d["lineage"] = lineage(d.get("steps_result") or [], failed_index=d.get("failed_step"))
        out.append(d)
    return {"runs": out}


@router.get("/{pid}/runs/{rid}/recovery")
def recovery_status(pid: str, rid: str, user=Depends(current_user)):
    """Only the execution owner can inspect their autonomous repair chain."""
    _own_or_404(pid, user)
    with db.connect() as c:
        row = c.execute("SELECT id FROM pipeline_runs WHERE id=? AND pipeline_id=? AND user_id=?",
                        (rid, pid, user["id"])).fetchone()
    if row is None:
        raise HTTPException(404, "Pipeline run not found")
    from . import sql_recovery
    return {"recovery": sql_recovery.status(rid, user)}
