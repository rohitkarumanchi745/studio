"""Agent Lightning: Studio's learning loop, and the client that ships it out.

Every agent run becomes a *rollout* (prompt -> actions -> outcome) with a
*reward*, and those rewarded traces drive optimization:

- Every /chat run is recorded as a trace with a heuristic reward (did SQL run,
  did a chart render, were there errors along the way).
- User 👍/👎 feedback overwrites the heuristic — explicit reward beats guessed.
- Recent failures are injected into the system prompt ("known pitfalls"), so
  the agent learns from bugs immediately, with no training run at all.
- scripts/train_apo.py distills low-reward traces into prompts/system_learned.txt
  (Agent Lightning's APO idea: optimize the prompt, not the weights) — the
  right lever for API models like Claude/GPT, whose weights we can't touch.

The second half of this module makes Studio a real Agent Lightning CLIENT
(the `agentlightning` package, github.com/microsoft/agent-lightning), so the
same rollouts land in Agent Lightning's own store and its verl/GRPO trainer
consumes Studio's real traffic instead of a private imitation of it:

- Set STUDIO_AGL_URL (and STUDIO_AGL_TOKEN if the server has a key) and every
  recorded trace is ALSO delivered to that server as a RolloutCreate + events
  + a RewardData reward event, using the package's own schemas.
- Delivery never runs on the chat path. record_chat_trace() enqueues one
  "agl_emit" job on the durable queue (jobs.py) — one INSERT — and the worker
  does the HTTP. A chat turn cannot wait on, or fail because of, an external
  server; a server that is down just fails the job, which the queue retries
  with backoff.
- The rollout id is derived from the trace id, so a retried job re-uses the
  same rollout instead of creating a second one, and a reward that arrives
  later (👍/👎 replacing the heuristic) SUPERSEDES the one already there
  rather than appending a second reward — see _deliver() for how the store's
  attempt semantics make that an update.
- With STUDIO_AGL_URL unset, none of this exists: nothing is enqueued, the
  package is not even imported, and the loop behaves exactly as it does
  without it. That has always been the promise here and it still holds.
"""
import json
import logging
import os
import time
import uuid

from . import db, jobs

log = logging.getLogger("studio.lightning")

# The job kind that delivers one trace to the Agent Lightning server.
AGL_KIND = "agl_emit"


# ── Configuration ────────────────────────────────────────────────────────

def agl_url():
    """Base URL of the Agent Lightning server (its API lives under /api).
    Empty string when unset, which is what turns delivery off entirely."""
    return (os.getenv("STUDIO_AGL_URL") or "").strip().rstrip("/")


def agl_token():
    """Bearer key the server was started with (AGL_KEY), or None when it runs
    without authentication."""
    return (os.getenv("STUDIO_AGL_TOKEN") or "").strip() or None


def emit_enabled():
    """True when rollouts should be delivered to an Agent Lightning server."""
    return bool(agl_url())


def _timeout_s():
    try:
        return max(1.0, float(os.getenv("STUDIO_AGL_TIMEOUT_S") or 10))
    except ValueError:
        return 10.0


def _max_attempts():
    """How many times the queue may try to deliver one trace. Higher than the
    queue default: the failure mode here is an external server being briefly
    unavailable, and the backoff (5s, 10s, 20s...) is designed for exactly
    that."""
    try:
        return max(1, int(os.getenv("STUDIO_AGL_MAX_ATTEMPTS") or 5))
    except ValueError:
        return 5


def _is_train():
    """Whether delivered rollouts are training data (default) or evaluation."""
    return (os.getenv("STUDIO_AGL_TRAIN") or "1").strip().lower() not in ("0", "false", "no")


def agl_package():
    """The installed agentlightning version, or None when it is not importable.

    Imported lazily: the package is a real requirement (requirements.txt), but
    an image built without it must still boot and answer — the loop works
    without Agent Lightning, and this module keeps that true."""
    try:
        import agentlightning
        return getattr(agentlightning, "__version__", "installed")
    except Exception:
        return None


def agl_available():
    """What /health and the learning dashboard report about Agent Lightning.

    Not a version string that means nothing: the package version, whether
    delivery is CONFIGURED, and whether the server was actually REACHABLE the
    last time a job talked to it (persisted by the worker, so the web process
    reports the truth about a server it never calls itself).

    Returns None — falsy, exactly as before — only when the package is missing
    AND delivery is unconfigured, i.e. when Agent Lightning is not in play at
    all; every caller that treats this as a boolean keeps working."""
    version = agl_package()
    configured = emit_enabled()
    if not version and not configured:
        return None
    out = {"version": version, "installed": bool(version), "configured": configured,
           "url": agl_url() or None, "reachable": None,
           "last_contact_at": None, "last_error": None}
    if configured:
        st = _server_status()
        if st:
            out["reachable"] = bool(st["ok"])
            out["last_contact_at"] = st["checked_at"]
            out["last_error"] = st["error"]
    return out


def heuristic_reward(result):
    """Score a run 0..1 from observable outcomes. User feedback replaces this.

    Fallback-mode runs return None — deterministic previews aren't agent
    behavior, and training on them would teach nothing.
    """
    if result.get("mode") != "agent":
        return None
    text = result.get("text") or ""
    if text.startswith("(Agent error"):
        return 0.0
    r = 0.35  # answered at all
    if result.get("sql"):
        r += 0.25  # grounded in a real query
    if result.get("rows"):
        r += 0.15  # the query produced data
    chart = result.get("chart") or {}
    if chart.get("type") and chart.get("type") != "table":
        r += 0.15  # visualized the answer
    if len(result.get("panels") or []) > 1:
        r += 0.10  # multi-view answer
    r -= 0.10 * min(len(result.get("errors") or []), 2)  # stumbles along the way
    return round(max(0.0, min(1.0, r)), 2)


def agent_reward(role, sub):
    """Score ONE agent's own decision, by what that agent's role is responsible
    for — so each named agent's policy is rewarded independently instead of
    sharing one blended score for the whole answer.

    - worker (a per-source data agent): grounded in real SQL, the query ran,
      it returned rows, and it visualized — minus stumbles.
    - aggregator: it actually synthesized something substantive across the
      workers' answers (its job is the reduce, not the SQL).
    """
    text = (sub.get("text") or "")
    if text.startswith(("(agent error", "(Agent error", "(Orchestrator error")):
        return 0.0

    if role == "aggregator":
        if not text.strip():
            return 0.0
        r = 0.4
        r += 0.3 if len(text.split()) >= 15 else 0.0     # a real synthesis, not a stub
        r += 0.3 if len(sub.get("panels") or []) > 1 else 0.0  # combined multiple sources' views
        return round(min(1.0, r), 2)

    # worker (default)
    r = 0.3
    if sub.get("sql"):
        r += 0.3
    if sub.get("rows"):
        r += 0.2
    chart = sub.get("chart") or {}
    if chart.get("type") and chart.get("type") != "table":
        r += 0.2
    r -= 0.10 * min(len(sub.get("errors") or []), 2)
    return round(max(0.0, min(1.0, r)), 2)


def record_agent_rollout(user, conversation_id, prompt, agent_name, role, sub,
                         duration_ms=None):
    """Persist one agent's decision as its own rollout, scored by its role. Uses
    the raw user prompt (not an agent-prefixed one) so per-agent rollouts don't
    inflate the distinct-prompt count that gates training readiness."""
    try:
        tid = db.add_trace(
            user, conversation_id=conversation_id, prompt=(prompt or "")[:1000],
            model=sub.get("model"), mode=f"agent:{role}",
            source=sub.get("_source") or sub.get("source"), sql=sub.get("sql"),
            ok=not (sub.get("text") or "").startswith(("(agent error", "(Agent error")),
            error=(sub.get("errors") or [None])[0],
            row_count=len(sub.get("rows") or []),
            chart_type=(sub.get("chart") or {}).get("type"),
            panel_count=len(sub.get("panels") or []), duration_ms=duration_ms,
            reward=agent_reward(role, sub), reward_source="per_agent",
            meta={"agent": agent_name, "agents": [agent_name], "role": role},
        )
    except Exception:
        return None
    _enqueue_emit(tid)
    return tid


def record_chat_trace(user, conversation_id, prompt, result, duration_ms, history=None):
    """Persist one rollout. Returns the trace id (also stored in the message,
    so the UI's 👍/👎 can target it later).

    history: the conversation turns the model actually saw this turn (role/text).
    Stored on the rollout so the trainer can reproduce the SAME multi-turn
    conditioning at training time — BitNet serves with history, so it must train
    with history (train == serve), and a follow-up like "and by region?" is only
    learnable WITH the turns that give it meaning."""
    errors = result.get("errors") or []
    chart = result.get("chart") or {}
    try:
        tid = db.add_trace(
            user,
            conversation_id=conversation_id,
            prompt=prompt[:1000],
            model=result.get("model"),
            mode=result.get("mode"),
            source=result.get("source"),
            table=result.get("table"),
            sql=result.get("sql"),
            ok=not (result.get("text") or "").startswith("(Agent error"),
            error=errors[0][:500] if errors else None,
            row_count=len(result.get("rows") or []),
            chart_type=chart.get("type"),
            panel_count=len(result.get("panels") or []),
            duration_ms=duration_ms,
            reward=heuristic_reward(result),
            reward_source="heuristic",
            # Attribute the rollout to the named agents behind it, so the
            # learning store shows which agents were called (single worker, or
            # the fan-out crew + Aggregator).
            meta={"errors": errors[:5],
                  "agents": [a.get("name") for a in (result.get("agents") or [])],
                  # trimmed to keep the trace row lean; same order the model saw
                  "history": [{"role": h["role"], "text": (h.get("text") or "")[:600]}
                              for h in (history or [])][-8:]},
        )
    except Exception:
        return None  # learning must never break answering
    # One INSERT on the durable queue when an Agent Lightning server is
    # configured, nothing at all when it is not. The HTTP happens in the
    # worker; the turn never waits on it and never fails because of it.
    _enqueue_emit(tid)
    return tid


def record_pipeline_outcome(user, *, run_id, prompt, source, action, status,
                            error=None, conversation_id=None, repairs_run_id=None,
                            duration_ms=None):
    """Record one immutable, observed execution outcome and its complete action.

    A failed attempt and its corrected successor have different run ids and
    remain separate examples. Replaying an outcome never replaces feedback,
    changes its stream cursor, or turns a failed execution into a success.
    Pending approvals, submissions and other unproved outcomes are unscored.
    Structured pipeline actions stay separate from the single-query ``sql``
    column: the SQL-only BitNet trainer must not treat a platform payload or
    an entire step bundle as a ``run_sql`` call.
    """
    status = str(status or "").strip().lower()
    if status not in {"success", "succeeded", "succeeded_sql_only", "failed"}:
        return None
    if not run_id or not isinstance(action, dict):
        return None
    try:
        # JSON serialization also snapshots mutable caller-owned recipes.
        action = json.loads(json.dumps(action))
        kind = action.get("type")
        if kind == "sql_pipeline":
            steps = action.get("steps")
            if not isinstance(steps, list) or not steps or any(
                    not isinstance(s, dict) or not isinstance(s.get("sql"), str)
                    or not s["sql"].strip() for s in steps):
                return None
            action = {"type": kind, "steps": [
                {k: step.get(k) for k in ("name", "source", "table", "sql")}
                for step in steps]}
        elif kind == "platform_run":
            if not action.get("target") or not isinstance(action.get("payload"), dict):
                return None
            action = {"type": kind, "target": action["target"], "payload": action["payload"]}
        elif kind == "airflow_dag":
            plan = action.get("plan")
            if not isinstance(plan, dict) or not plan.get("tasks") or not plan.get("source"):
                return None
            # Full immutable recipe, with no chat payload/logs/credentials.
            action = {"type": kind, "plan": {k: plan[k] for k in
                      ("version", "name", "dag_id", "source", "schedule", "tasks", "parameters", "prompt") if k in plan}}
        else:
            return None
        success = status != "failed"
        tid = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(
            ["studio-pipeline-outcome", str(user["id"]), str(run_id)])))
        meta = {"action": action, "run_id": str(run_id), "status": status,
                "agents": ["Pipeline executor"], "errors": [str(error)[:500]] if error else []}
        if repairs_run_id and str(repairs_run_id) != str(run_id):
            meta["repairs_run_id"] = str(repairs_run_id)
        with db.connect() as c:
            cur = c.execute(
                "INSERT INTO agent_traces (id,user_id,email,role,conversation_id,prompt,"
                "mode,source,sql,ok,error,panel_count,duration_ms,reward,reward_source,meta,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                (tid, user["id"], user["email"], user["role"], conversation_id,
                 str(prompt or "")[:1000], "pipeline", source, None,
                 int(success), str(error)[:500] if error else None,
                 len(action.get("steps") or []), duration_ms, 1.0 if success else 0.0,
                 "pipeline_outcome", json.dumps(meta), time.time()))
            if cur.rowcount == 1:
                # The trace and its delivery job commit together. If the
                # queue is unavailable, the caller can retry this run id.
                _enqueue_emit(tid, conn=c)
            c.commit()
        return tid
    except Exception:
        log.warning("pipeline outcome could not be recorded for run %s", run_id, exc_info=True)
        return None


def recent_pipeline_examples(user, source=None, limit=3):
    """The caller's recent pipeline outcomes, for prompt context only.

    SQL recipes are rechecked against current permissions and namespaces
    without execution. Platform payloads stay owner-scoped; their consumer
    must apply the usual platform validation/approval before any submission.
    A user's thumbs-up does not turn an observed failed run into a proven
    success: ``status`` comes from the immutable execution metadata.
    """
    from . import queryguard, rbac
    from .connectors import get_connector

    limit = max(1, min(int(limit), 10))
    with db.connect() as c:
        rows = c.execute(
            "SELECT id,prompt,source,error,reward,meta FROM agent_traces "
            "WHERE user_id=? AND mode='pipeline' ORDER BY created_at DESC LIMIT ?",
            (user["id"], max(30, limit * 10))).fetchall()
    examples = []
    for row in rows:
        try:
            meta = json.loads(row["meta"] or "{}")
            action = meta.get("action") or {}
            if meta.get("status") not in {"success", "succeeded", "succeeded_sql_only", "failed"}:
                continue
            if source and source != "*" and row["source"] != source:
                continue
            if action.get("type") == "sql_pipeline":
                steps = action.get("steps") or []
                if not steps:
                    continue
                for step in steps:
                    src = step.get("source") or row["source"]
                    if source and source != "*" and src != source:
                        raise ValueError("outside selected source")
                    connector = get_connector(src)
                    dialect = getattr(connector, "dialect", None)
                    tokens, _ = queryguard._tokens(step["sql"])
                    allowed = [queryguard._canon(parts[-1], dialect)
                               for parts, _ in queryguard._table_refs(tokens)
                               if rbac.can_access(user["role"], src, parts[-1].text)]
                    queryguard.validate(step["sql"], allowed,
                                        qualifiers=connector.qualifiers(), dialect=dialect)
            elif action.get("type") != "platform_run":
                continue
            examples.append({"trace_id": row["id"], "run_id": meta.get("run_id"),
                             "prompt": row["prompt"], "source": row["source"],
                             "action": action, "status": meta["status"], "error": row["error"],
                             "reward": row["reward"], "repairs_run_id": meta.get("repairs_run_id")})
        except (KeyError, TypeError, ValueError, queryguard.QueryRejected):
            continue
        if len(examples) >= limit:
            break
    return examples


def export_rollouts(path, limit=5000):
    """Write traces as JSONL in the shape RL/APO training jobs consume."""
    n = 0
    with open(path, "w") as f:
        for t in db.list_traces(limit=limit):
            if t.get("reward") is None:
                continue
            meta = json.loads(t.get("meta") or "{}")
            f.write(json.dumps({
                "prompt": t["prompt"],
                "response": (meta.get("action")
                             or {"sql": t["sql"], "chart_type": t["chart_type"]}),
                "reward": t["reward"],
                "metadata": {
                    "model": t["model"], "source": t["source"], "table": t["tbl"],
                    "error": t["error"], "reward_source": t["reward_source"],
                    "duration_ms": t["duration_ms"],
                    "run_id": meta.get("run_id"), "repairs_run_id": meta.get("repairs_run_id"),
                    "execution_status": meta.get("status"),
                },
            }, default=str) + "\n")
            n += 1
    return n


# ── Delivery to a real Agent Lightning server ────────────────────────────
#
# Everything below is dormant unless STUDIO_AGL_URL is set. The shape of the
# wire traffic is not invented here: it is the package's own schemas
# (RolloutCreate / EventCreate / RewardData) against the routes its server
# actually serves (agentlightning/server/routes/{rollouts,events}.py, mounted
# under /api).

_TABLES_READY = False


def _ensure_tables():
    """Create the two bookkeeping tables, once per process.

    Deliberately NOT part of main.init_state(): an unconfigured deployment
    must not grow tables for a feature it does not use, and the chat path
    never touches them (it only enqueues), so lazy creation in the worker is
    both sufficient and invisible."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS agl_deliveries (
                trace_id TEXT PRIMARY KEY,
                rollout_id TEXT NOT NULL,
                attempt_id TEXT,
                reward REAL,
                reward_source TEXT,
                pending INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agl_status (
                id TEXT PRIMARY KEY,
                ok INTEGER NOT NULL,
                checked_at REAL NOT NULL,
                error TEXT
            );
            """
        )
        c.commit()
    _TABLES_READY = True


def _enqueue_emit(trace_id, *, conn=None):
    """Queue one delivery for a trace. Returns the job id, or None when
    delivery is off (the unconfigured default) or the queue rejected it.

    This is the ONLY thing the chat path does for Agent Lightning: one INSERT
    into background_jobs. It never raises — a learning-loop side channel must
    not be able to break an answer."""
    if not trace_id or not emit_enabled():
        return None
    try:
        return jobs.enqueue(AGL_KIND, {"trace_id": trace_id},
                            max_attempts=_max_attempts(), conn=conn)
    except Exception:
        if conn is not None:
            raise  # caller owns the transaction; do not commit a trace without its job
        log.warning("agl: could not enqueue delivery for trace %s", trace_id, exc_info=True)
        return None


def rollout_id_for(trace_id):
    """The server-side rollout id for a Studio trace — stable, so a retried
    job (or a later reward) addresses the SAME rollout instead of creating a
    second one. RolloutCreate takes a caller-supplied rollout_id precisely so
    creation is idempotent."""
    return f"studio-{trace_id}"


def _schemas():
    """The package's schema module, or a clear error when it is missing.

    Configured-but-not-installed is a real misconfiguration: the job fails
    (and the queue retries it), which is visible in /health and in the job
    row, instead of Studio silently inventing its own JSON shape."""
    try:
        from agentlightning import schemas
        return schemas
    except Exception as e:      # pragma: no cover - depends on the image
        raise RuntimeError(
            "STUDIO_AGL_URL is set but the agentlightning package is not "
            f"importable ({e}); install it (requirements.txt) or unset the URL")


def _client():
    """The package's own sync client, with retries turned OFF.

    agentlightning's client retries in-process with sleeps up to 30s per
    attempt. That is the wrong place for retries here: jobs.py already gives
    at-least-once delivery with fenced claims and exponential backoff across
    process restarts, and a worker thread must not block for minutes. So one
    attempt per job run, and the queue owns the retry."""
    from agentlightning.client import AgentLightningSyncClient
    return AgentLightningSyncClient(
        key=agl_token(), base_url=agl_url(), timeout=_timeout_s(), max_retries=0)


# ── Studio trace -> Agent Lightning schemas ──────────────────────────────

def _trace(trace_id):
    with db.connect() as c:
        row = c.execute("SELECT * FROM agent_traces WHERE id=?", (trace_id,)).fetchone()
    if row is None:
        return None
    t = dict(row)
    try:
        t["meta"] = json.loads(t.get("meta") or "{}")
    except (TypeError, ValueError):
        t["meta"] = {}
    return t


def rollout_input(t):
    """What the agent was asked to do — the model's actual conditioning.

    data_id is the Studio trace id: the server's /rollouts/terminal projection
    exposes input["data_id"], which is how the verl trainer joins a finished
    rollout back to the row it came from."""
    return {
        "data_id": t["id"],
        "prompt": t.get("prompt"),
        "source": t.get("source"),
        "table": t.get("tbl"),
        "conversation_id": t.get("conversation_id"),
        # The turns the model actually saw, so training conditions the way
        # serving does (see record_chat_trace).
        "history": (t.get("meta") or {}).get("history") or [],
    }


def rollout_metadata(t):
    """Batch/context fields. RolloutMetadata allows extras, so Studio's own
    identifiers ride along without being smuggled into `input`."""
    meta = t.get("meta") or {}
    return {
        "studio_trace_id": t["id"],
        "studio_user_id": t.get("user_id"),
        "studio_role": t.get("role"),
        "mode": t.get("mode"),
        "model": t.get("model"),
        "agents": [a for a in (meta.get("agents") or []) if a],
        "created_at": t.get("created_at"),
        "run_id": meta.get("run_id"), "repairs_run_id": meta.get("repairs_run_id"),
        "execution_status": meta.get("status"),
        # The terminal projection includes metadata. Keep the observed
        # action here as a label, never in the model's conditioning input.
        "action": meta.get("action"),
    }


def _reward_data(t, schemas):
    """The trace's current reward as RewardData, or None when it has none
    (fallback-mode runs are deliberately unscored — see heuristic_reward)."""
    if t.get("reward") is None:
        return None
    source = t.get("reward_source") or "heuristic"
    reason = {"user": "user_feedback", "heuristic": "studio_heuristic",
              "per_agent": "studio_per_agent_role"}.get(source, source)
    meta = t.get("meta") or {}
    if source == "user":
        message = meta.get("feedback_note") or ("👍 helpful" if t["reward"] >= 0.5 else "👎 not helpful")
    else:
        message = (f"sql={'y' if t.get('sql') else 'n'} rows={t.get('row_count') or 0} "
                   f"chart={t.get('chart_type') or 'none'} errors={0 if t.get('ok') else 1}")
    return schemas.RewardData(value=float(t["reward"]), source=source,
                             reason=reason, message=message[:500])


def trajectory_events(t, schemas):
    """The interesting steps of the run, as EventCreate.

    One event per type, so re-running a delivery can tell what is already
    there and post only what is missing (the store has no event dedupe of its
    own — events are append-only and identified by position)."""
    meta = t.get("meta") or {}
    out = [schemas.EventCreate(event_type="studio.run", data={
        "mode": t.get("mode"), "model": t.get("model"), "source": t.get("source"),
        "table": t.get("tbl"), "ok": bool(t.get("ok")),
        "duration_ms": t.get("duration_ms"),
        "conversation_id": t.get("conversation_id"),
        "agents": [a for a in (meta.get("agents") or []) if a],
    })]
    if meta.get("action"):
        out.append(schemas.EventCreate(event_type="studio.action", data={
            "action": meta["action"], "run_id": meta.get("run_id"),
            "repairs_run_id": meta.get("repairs_run_id"), "status": meta.get("status")}))
    if t.get("sql"):
        out.append(schemas.EventCreate(event_type="studio.query", data={
            "sql": t["sql"], "row_count": t.get("row_count"),
            "source": t.get("source"), "table": t.get("tbl")}))
    if t.get("chart_type"):
        out.append(schemas.EventCreate(event_type="studio.chart", data={
            "type": t["chart_type"], "panel_count": t.get("panel_count")}))
    errors = [e for e in (meta.get("errors") or []) if e] or (
        [t["error"]] if t.get("error") else [])
    if errors:
        out.append(schemas.EventCreate(event_type="studio.errors",
                                       data={"errors": [str(e)[:500] for e in errors[:5]]}))
    return out


# ── The job: deliver one trace ───────────────────────────────────────────

def _next_attempt(attempt_id):
    try:
        return str(int(attempt_id) + 1)
    except (TypeError, ValueError):
        return f"{attempt_id}+1"


def _reward_delivered(events, reward):
    """Is the store's current reward already the one we want? Compares the
    LAST reward event of the current attempt — the same one the verl bridge
    reads (agl_rollout_manager takes reward_events[-1])."""
    rewards = [e for e in events if e.get("event_type") == "reward"]
    if not rewards:
        return False
    data = rewards[-1].get("data") or {}
    try:
        same_value = abs(float(data.get("value")) - reward.value) < 1e-9
    except (TypeError, ValueError):
        return False
    return same_value and (data.get("source") or None) == (reward.source or None)


def _raise_for_status(r):
    r.raise_for_status()
    return r


def _finalize(client, rollout_id, state, ok):
    """Walk the rollout to a terminal state, so it appears in the server's
    /rollouts/terminal log — which is how the verl trainer discovers finished
    work. Studio's run is already over by the time we deliver, so this is a
    replay of a lifecycle the store insists on: queuing -> running -> done."""
    target = "succeeded" if ok else "failed"
    if state in ("succeeded", "failed"):
        return state
    if state == "queuing":
        _raise_for_status(client.patch(f"/api/rollouts/{rollout_id}",
                                       json={"status": {"state": "running"}}))
    _raise_for_status(client.patch(f"/api/rollouts/{rollout_id}",
                                   json={"status": {"state": target}}))
    return target


def _deliver(client, t, schemas):
    """One trace -> one rollout on the server. Idempotent, and reward-updating.

    Idempotency: the rollout id is derived from the trace id and the server
    returns the existing rollout unchanged for an id it already has, so a
    retried job never creates a second rollout. Events cannot be de-duplicated
    by the store (they are append-only), so we read the attempt's events first
    and post only the types that are missing.

    Reward updates (👍/👎 replacing the heuristic) are an UPDATE, not a second
    reward, because the store scopes event reads to ONE attempt: GET
    /rollouts/{id}/events returns the events of status.last_attempt_id only.
    So a superseding reward opens the next attempt, PATCHes last_attempt_id to
    it, and re-posts the trajectory there. Every reader — the events route and
    the verl bridge — then sees exactly one reward: the new one. The old
    attempt stays on disk as history, which is what an RL store should keep."""
    rollout_id = rollout_id_for(t["id"])
    create = schemas.RolloutCreate(
        rollout_id=rollout_id, input=rollout_input(t), is_train=_is_train(),
        metadata=rollout_metadata(t))
    created = _raise_for_status(
        client.post_with_retry("/api/rollouts", json=[create.model_dump(mode="json")])).json()
    status = (created[0].get("status") or {}) if created else {}
    attempt = status.get("last_attempt_id") or schemas.DEFAULT_ATTEMPT_ID
    reward = _reward_data(t, schemas)

    events = _raise_for_status(client.get(f"/api/rollouts/{rollout_id}/events")).json()
    superseded = False
    if reward is not None and not _reward_delivered(events, reward) and \
            any(e.get("event_type") == "reward" for e in events):
        # A different reward is already recorded for this rollout: open the
        # next attempt and make it the one readers see.
        attempt = _next_attempt(attempt)
        _raise_for_status(client.patch(f"/api/rollouts/{rollout_id}",
                                       json={"status": {"last_attempt_id": attempt}}))
        events, superseded = [], True

    have = {e.get("event_type") for e in events}
    for ev in trajectory_events(t, schemas):
        if ev.event_type in have:
            continue
        _raise_for_status(client.post_with_retry(
            f"/api/rollouts/{rollout_id}/attempt/{attempt}/events",
            json=ev.model_dump(mode="json")))
    if reward is not None and "reward" not in have:
        _raise_for_status(client.post_with_retry(
            f"/api/rollouts/{rollout_id}/attempt/{attempt}/events",
            json=schemas.EventCreate(event_type="reward",
                                     data=reward.model_dump(mode="json")).model_dump(mode="json")))
    state = _finalize(client, rollout_id, status.get("state") or "queuing",
                      bool(t.get("ok")))
    return {"rollout_id": rollout_id, "attempt_id": attempt, "state": state,
            "superseded": superseded,
            "reward": None if reward is None else reward.value,
            "reward_source": None if reward is None else reward.source}


def emit_trace(trace_id):
    """Deliver one Studio trace to the Agent Lightning server. The body of the
    'agl_emit' job — safe to run again on the same trace at any time."""
    if not emit_enabled():
        return {"skipped": "unconfigured"}
    t = _trace(trace_id)
    if t is None:
        # The trace was deleted (or never landed): nothing to deliver, and
        # nothing a retry would fix.
        return {"skipped": "unknown_trace", "trace_id": trace_id}
    schemas = _schemas()
    _ensure_tables()
    try:
        with _client() as client:
            out = _deliver(client, t, schemas)
    except Exception as e:
        _record_contact(False, e)
        raise                    # the queue retries with backoff
    _record_contact(True, None)
    _record_delivery(t, out)
    return dict(out, trace_id=trace_id)


@jobs.handler(AGL_KIND)
def _agl_emit_job(payload, job=None):
    return emit_trace((payload or {}).get("trace_id"))


# ── Bookkeeping: what we shipped, and whether the server answered ────────

def _record_delivery(t, out):
    now = time.time()
    with db.connect() as c:
        c.execute(
            "INSERT INTO agl_deliveries (trace_id, rollout_id, attempt_id, reward, "
            "reward_source, pending, updated_at) VALUES (?,?,?,?,?,0,?) "
            "ON CONFLICT(trace_id) DO UPDATE SET rollout_id=excluded.rollout_id, "
            "attempt_id=excluded.attempt_id, reward=excluded.reward, "
            "reward_source=excluded.reward_source, pending=0, updated_at=excluded.updated_at",
            (t["id"], out["rollout_id"], out["attempt_id"], t.get("reward"),
             t.get("reward_source"), now))
        c.commit()


def _record_contact(ok, error):
    """Persist the last outcome of talking to the server, so /health can tell
    the truth in a process that never calls it (the web process enqueues; the
    worker does the HTTP)."""
    try:
        _ensure_tables()
        with db.connect() as c:
            c.execute(
                "INSERT INTO agl_status (id, ok, checked_at, error) VALUES ('server',?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET ok=excluded.ok, "
                "checked_at=excluded.checked_at, error=excluded.error",
                (1 if ok else 0, time.time(), None if ok else str(error)[:500]))
            c.commit()
    except Exception:
        log.debug("agl: could not record server contact", exc_info=True)


def _server_status():
    try:
        with db.connect() as c:
            row = c.execute("SELECT ok, checked_at, error FROM agl_status "
                            "WHERE id='server'").fetchone()
        return None if row is None else dict(row)
    except Exception:
        return None      # not configured long enough to have a table yet


def delivery(trace_id):
    """What was last delivered for a trace (tests and support queries)."""
    try:
        with db.connect() as c:
            row = c.execute("SELECT * FROM agl_deliveries WHERE trace_id=?",
                            (trace_id,)).fetchone()
        return None if row is None else dict(row)
    except Exception:
        return None


# ── Reward updates: the sweep that catches 👍/👎 ─────────────────────────

def _pending_stale_s():
    try:
        return max(30.0, float(os.getenv("STUDIO_AGL_PENDING_STALE_S") or 600))
    except ValueError:
        return 600.0


@jobs.reconciler
def sweep_reward_updates(limit=200):
    """Re-deliver traces whose reward CHANGED after we shipped them.

    A 👍/👎 lands in chat.feedback -> db.set_trace_reward, which overwrites the
    heuristic reward on the trace row. Rather than reach into that path, this
    runs where the queue already heals itself (jobs.reconciler, after every
    reclaim pass) and compares what we last delivered against what the trace
    now says. Durable by construction: feedback given while the Agent
    Lightning server was down, or while the worker was stopped, is picked up
    on a later pass instead of being lost.

    `pending` keeps one queued job per trace; a job that dies without clearing
    it is retried after STUDIO_AGL_PENDING_STALE_S. Returns the number of
    deliveries enqueued."""
    if not emit_enabled():
        return 0
    _ensure_tables()
    stale_before = time.time() - _pending_stale_s()
    with db.connect() as c:
        rows = c.execute(
            "SELECT d.trace_id FROM agl_deliveries d JOIN agent_traces t ON t.id = d.trace_id "
            "WHERE t.reward IS NOT NULL AND (d.pending = 0 OR d.updated_at < ?) AND "
            "(d.reward IS NULL OR d.reward <> t.reward OR "
            " COALESCE(d.reward_source,'') <> COALESCE(t.reward_source,'')) "
            "ORDER BY t.created_at DESC LIMIT ?", (stale_before, int(limit))).fetchall()
    n = 0
    for r in rows:
        trace_id = r["trace_id"]
        with db.connect() as c:
            cur = c.execute(
                "UPDATE agl_deliveries SET pending=1, updated_at=? WHERE trace_id=? "
                "AND (pending=0 OR updated_at < ?)", (time.time(), trace_id, stale_before))
            c.commit()
        # Whoever's UPDATE landed owns the enqueue; a second worker's matches
        # nothing and does not queue a duplicate job for the same trace.
        if jobs._matched(cur) and _enqueue_emit(trace_id):
            n += 1
    return n
