"""Live agent activity tests — the steps feed a background chat turn emits.

Proves: emits bound to a task land in its chat_tasks row and come back from
GET /tasks/{tid} as parsed steps (oldest → newest); emit_for works from worker
threads that never bound the contextvar (the orchestrator fan-out case); an
unbound emit and an unknown task id are silent no-ops; the feed is capped so a
chatty turn can't bloat the row; and a stranger still gets 404, steps or not.

Run from the backend directory:
    python -m pytest tests/test_progress.py -q
"""
import os
import tempfile
import threading
import time
import uuid

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-progress-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException

from app import chat, db, progress

USER = {"id": "u-ana", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}
STRANGER = {"id": "u-x", "email": "x@studio.test", "role": "analyst", "name": "X"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    yield


def _task(user=USER):
    tid = str(uuid.uuid4())
    c = db._conn()
    c.execute("INSERT INTO chat_tasks (id, conversation_id, user_id, prompt, status, "
              "seen, created_at) VALUES (?,?,?,?,?,?,?)",
              (tid, "conv-1", user["id"], "q", "running", 0, time.time()))
    c.commit()
    c.close()
    return tid


def test_bound_emits_come_back_from_task_status_in_order():
    tid = _task()
    progress.bind(tid)
    try:
        progress.emit("reading the question")
        progress.emit("running SQL on demo")
    finally:
        progress.bind(None)
    d = chat.task_status(tid, user=USER)
    assert [s["label"] for s in d["steps"]] == [
        "reading the question", "running SQL on demo"]
    assert all(s["t"] > 0 for s in d["steps"])
    assert d["status"] == "running"


def test_emit_for_works_from_a_thread_that_never_bound():
    """The orchestrator fan-out case: pool threads emit via the captured id."""
    tid = _task()
    t = threading.Thread(target=progress.emit_for, args=(tid, "snowflake agent: finished"))
    t.start()
    t.join()
    d = chat.task_status(tid, user=USER)
    assert [s["label"] for s in d["steps"]] == ["snowflake agent: finished"]


def test_unbound_and_unknown_task_emits_are_silent_noops():
    progress.bind(None)
    progress.emit("goes nowhere")                     # unbound → no-op
    progress.emit_for("no-such-task", "also nowhere")  # unknown id → no-op
    tid = _task()
    assert chat.task_status(tid, user=USER)["steps"] == []


def test_feed_is_capped():
    tid = _task()
    for i in range(progress.MAX_STEPS + 15):
        progress.emit_for(tid, f"step {i}")
    steps = chat.task_status(tid, user=USER)["steps"]
    assert len(steps) == progress.MAX_STEPS
    assert steps[-1]["label"] == f"step {progress.MAX_STEPS + 14}"  # newest kept


def test_stranger_still_404s_on_the_task():
    tid = _task()
    progress.emit_for(tid, "private step")
    with pytest.raises(HTTPException) as e:
        chat.task_status(tid, user=STRANGER)
    assert e.value.status_code == 404
