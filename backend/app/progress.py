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


def emit(label):
    """Append one step to the bound task's live feed. No-op when unbound
    (synchronous /chat has no task row and stays silent)."""
    emit_for(_task.get(), label)


def emit_for(tid, label):
    """Append one step to a specific task's feed. Never raises."""
    if not tid or not label:
        return
    try:
        with _lock:
            c = db._conn()
            r = c.execute("SELECT steps FROM chat_tasks WHERE id=?", (tid,)).fetchone()
            if r is None:
                c.close()
                return
            try:
                steps = json.loads(dict(r)["steps"] or "[]")
            except (TypeError, ValueError):
                steps = []
            steps.append({"t": round(time.time(), 2), "label": str(label)[:200]})
            c.execute("UPDATE chat_tasks SET steps=? WHERE id=?",
                      (json.dumps(steps[-MAX_STEPS:]), tid))
            c.commit()
            c.close()
    except Exception:
        pass
