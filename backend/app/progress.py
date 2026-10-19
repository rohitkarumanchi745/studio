"""Live agent activity for a background chat turn.

A background turn already runs in a worker thread while the UI polls
GET /tasks/{tid}. This module gives that poll something to show: the turn
binds its task id here (a contextvar, so nothing threads it through every
call), and the pipeline / agents / orchestrator emit short labeled steps as
they act ("snowflake agent: running SQL…"). Steps land in the chat_tasks row
the UI is already polling, so live visibility needs no new transport.

Fan-out threads don't inherit the contextvar, so the orchestrator captures
current() before dispatch and rebinds (or emits via emit_for) inside each
worker. Emitting is fail-safe and bounded: progress display must never break
or bloat a turn.
"""
import contextvars
import json
import threading
import time

from . import db

_task = contextvars.ContextVar("studio_progress_task", default=None)
_lock = threading.Lock()   # all of one task's emits come from one process
MAX_STEPS = 60


def bind(tid):
    """Attach this thread's subsequent emit() calls to a task (None to detach)."""
    _task.set(tid)


def current():
    """The bound task id, for handing into threads that emit on our behalf."""
    return _task.get()


#: What a step is doing — the activity feed draws an icon for each. A step on
#: a source ("query", "schema", "agent") also carries the source, its type and
#: its engine, so the feed shows that database's own icon.
KINDS = ("query", "schema", "chart", "visual", "semantic", "verified", "cache", "route",
         "plan", "agents", "agent", "aggregate", "docs", "tools", "model", "pipeline",
         "error", "done")


def describe(source):
    """{source, source_type, engine} for a connector or a source name — the
    connection's type ("lancedb", "sql", …) or the built-in source's own name
    ("snowflake", "demo"), and the engine it speaks. {} when unknown."""
    try:
        if isinstance(source, str):
            from .connectors import get_connector
            conn = get_connector(source)
        else:
            conn = source
        return {"source": conn.name,
                "source_type": getattr(conn, "ctype", None) or conn.name,
                "engine": str(getattr(conn, "dialect", "") or "")}
    except Exception:
        return {"source": source} if isinstance(source, str) else {}


def emit(label, agent=None, kind=None, source=None):
    """Append one step to the bound task's live feed. No-op when unbound
    (synchronous /chat has no task row and stays silent)."""
    emit_for(_task.get(), label, agent=agent, kind=kind, source=source)


def emit_for(tid, label, agent=None, kind=None, source=None):
    """Append one step to a specific task's feed. Never raises. `agent` names
    who acted ("Snowflake agent", "Aggregator", a planner), so the UI can show
    how many agents a task is using while it runs; `kind` says what the step
    is doing (KINDS) and `source` (a connector or a name) where."""
    if not tid or not label:
        return
    try:
        with _lock, db.connect() as c:
            r = c.execute("SELECT steps FROM chat_tasks WHERE id=?", (tid,)).fetchone()
            if r is None:
                return
            try:
                steps = json.loads(dict(r)["steps"] or "[]")
            except (TypeError, ValueError):
                steps = []
            step = {"t": round(time.time(), 2), "label": str(label)[:200]}
            if agent:
                step["agent"] = str(agent)[:80]
            if kind in KINDS:
                step["kind"] = kind
            if source is not None:
                step.update({k: str(v)[:80] for k, v in describe(source).items() if v})
            steps.append(step)
            c.execute("UPDATE chat_tasks SET steps=? WHERE id=?",
                      (json.dumps(steps[-MAX_STEPS:]), tid))
            c.commit()
    except Exception:
        pass
