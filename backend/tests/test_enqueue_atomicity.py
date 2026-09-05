"""Atomic enqueue, and the two layers that make a background turn answerable
exactly once.

Three defects live here, each reproduced by a test that fails without its fix:

  * ENQUEUE WAS NOT ATOMIC. POST /api/chat/background committed the chat_tasks
    row and then the queue job as two transactions. A failure in between left
    `chat_tasks.status='running'` with no job behind it: nothing would ever run
    that turn and nothing would ever fail it, so the UI spun on it forever.
    Both rows now go in ONE transaction (jobs.enqueue takes the caller's
    connection), and a reconciler on the worker's reclaim pass heals the
    orphans an older build already left behind.
  * A LOST CLAIM DID NOT REACH THE HANDLER. The claim token fences the job
    ROW, but the handler kept running after its heartbeat was refused, so two
    reclaimed attempts could both emit an answer. jobs now sets an abort Event
    the handler checks at its safe points.
  * THE COOPERATIVE SIGNAL IS NOT A GUARANTEE. Python cannot preempt a
    thread, so the real defence is a UNIQUE index on messages.reply_to — the
    id of the user turn an answer answers. The losing INSERT is refused,
    add_message returns None, and that attempt discards its answer instead of
    raising or duplicating it.

Run from the backend directory:
    python -m pytest tests/test_enqueue_atomicity.py -q
"""
import importlib
import threading
import time
import warnings

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Full app on a fresh SQLite file, no LLM key (deterministic fallback),
    worker OFF so the test drives the queue itself."""
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "atomicity.db"))
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    monkeypatch.setenv("STUDIO_GRAPH_SYNC_TICKER", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    import app.main as main
    importlib.reload(main)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)   # .env may have restored them on reload
    with TestClient(main.app) as c:
        yield c


def _login(c, email="admin@studio.local", password="admin123"):
    tok = c.post("/api/auth/login",
                 json={"email": email, "password": password}).json()["access_token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _rows(sql, params=()):
    from app import db
    with db.connect() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _assistants(cid):
    from app import db
    return [m for m in db.list_messages(cid) if m["role"] == "assistant"]


def _start(c, prompt="total revenue by region", **extra):
    body = {"prompt": prompt, "source": "demo", "table": "sales", **extra}
    r = c.post("/api/chat/background", json=body)
    assert r.status_code == 202, r.text
    return r.json()


# ── R1: the task row and its job are one transaction ─────────────────────

def test_task_and_job_are_written_together(client):
    """The happy path, pinned: one task, one job, and the job's id derives
    from the task's — which is what lets the reconciler join them."""
    from app import chat
    _login(client)
    body = _start(client)
    tid = body["task_id"]
    tasks = _rows("SELECT id, status FROM chat_tasks")
    jobs_ = _rows("SELECT id, kind, status FROM background_jobs")
    assert [t["status"] for t in tasks] == ["running"]
    assert len(jobs_) == 1
    assert jobs_[0]["id"] == chat._task_job_id(tid) == f"chat_turn:{tid}"
    assert jobs_[0]["kind"] == "chat_turn" and jobs_[0]["status"] == "queued"


def test_a_failing_enqueue_leaves_no_task_row(client, monkeypatch):
    """The reproduced bug. With two commits, an enqueue that raised left a
    task marked 'running' that nothing could ever run or fail — the spinner
    never stopped. In one transaction the task row rolls back with it."""
    from app import chat, db, jobs
    _login(client)

    def _boom(*a, **kw):
        raise RuntimeError("queue is unreachable")

    monkeypatch.setattr(jobs, "enqueue", _boom)
    with pytest.raises(RuntimeError, match="queue is unreachable"):
        client.post("/api/chat/background", json={
            "prompt": "total revenue by region", "source": "demo", "table": "sales"})

    assert _rows("SELECT id FROM chat_tasks") == []
    assert _rows("SELECT id FROM background_jobs") == []
    # The conversation and the user's question survive on purpose: an
    # unanswered question is harmless, a task that claims to be running is not.
    convs = _rows("SELECT id FROM conversations")
    assert len(convs) == 1
    assert [m["role"] for m in db.list_messages(convs[0]["id"])] == ["user"]
    # And the API is usable again immediately afterwards.
    monkeypatch.undo()
    body = _start(client)
    assert _rows("SELECT status FROM chat_tasks")[0]["status"] == "running"
    assert len(_rows("SELECT id FROM background_jobs")) == 1
    assert body["task_id"]


def test_enqueue_on_a_caller_connection_commits_and_rolls_back_with_it(tmp_path, monkeypatch):
    """jobs.enqueue(conn=...) must enlist in the caller's transaction: no
    commit of its own on the way in, and no surviving row when the caller
    rolls back. Without that there is no way to make two tables atomic."""
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "enq.db"))
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    from app import db as _db
    importlib.reload(_db)
    from app import jobs
    _db.init_db()
    jobs.init_tables()

    # Rolled back by the caller -> the job never existed.
    with _db.connect() as c:
        jid = jobs.enqueue("t_enlist", {"n": 1}, conn=c)
        c.rollback()
    assert jobs.get(jid) is None

    # Committed by the caller -> exactly one queued job.
    with _db.connect() as c:
        jid = jobs.enqueue("t_enlist", {"n": 2}, conn=c)
        c.commit()
    row = jobs.get(jid)
    assert row["status"] == "queued" and row["payload"] == {"n": 2}

    # And the default (no conn) still commits on its own connection.
    jid2 = jobs.enqueue("t_enlist", {"n": 3})
    assert jobs.get(jid2)["status"] == "queued"


# ── R1: the reconciliation safety net ────────────────────────────────────

def _orphan(tid, cid, uid, age_s):
    from app import db
    with db.connect() as c:
        c.execute("INSERT INTO chat_tasks (id, conversation_id, user_id, prompt, status, "
                  "seen, created_at) VALUES (?,?,?,?,?,?,?)",
                  (tid, cid, uid, "an old question", "running", 0, time.time() - age_s))
        c.commit()


def test_reclaim_fails_a_task_left_running_with_no_job(client):
    """An already-stuck row heals: the worker's reclaim pass fails a task that
    is 'running' with no job behind it, so the UI stops spinning."""
    from app import chat, jobs
    _login(client)
    body = _start(client)
    cid = body["conversation_id"]
    _orphan("orphan-1", cid, "u1", age_s=chat._orphan_task_after_s() + 60)

    # The live task from _start() is NOT touched — its job is still queued.
    assert jobs.reclaim_stale() == (0, 0)
    by_id = {t["id"]: t for t in _rows("SELECT id, status, error FROM chat_tasks")}
    assert by_id["orphan-1"]["status"] == "failed"
    assert "no background job" in by_id["orphan-1"]["error"]
    assert by_id[body["task_id"]]["status"] == "running"


def test_reconciler_spares_a_young_task_and_one_whose_job_is_alive(client):
    """Two ways to be legitimately 'running': the job is still on the queue,
    or the task is simply too young to judge. Neither may be failed."""
    from app import chat
    _login(client)
    cid = _start(client)["conversation_id"]
    _orphan("young", cid, "u1", age_s=1)                                  # no job, but new
    _orphan("old-live", cid, "u1", age_s=chat._orphan_task_after_s() + 60)
    from app import db, jobs
    with db.connect() as c:                       # give "old-live" a live job
        jobs.enqueue("chat_turn", {"tid": "old-live"}, job_id=chat._task_job_id("old-live"),
                     conn=c)
        c.commit()

    assert chat._fail_orphan_tasks() == 0
    statuses = {t["id"]: t["status"] for t in _rows("SELECT id, status FROM chat_tasks")}
    assert statuses["young"] == "running"
    assert statuses["old-live"] == "running"


def test_a_finished_job_whose_task_never_left_running_is_reconciled(client):
    """The other orphan shape: the job row exists but is done/failed while the
    task is still 'running' — the attempt that owned it died between the two
    writes. Nothing will ever run that turn again."""
    from app import chat, db
    _login(client)
    cid = _start(client)["conversation_id"]
    _orphan("done-job", cid, "u1", age_s=chat._orphan_task_after_s() + 60)
    with db.connect() as c:
        c.execute("INSERT INTO background_jobs (id, kind, payload, status, attempts, "
                  "max_attempts, run_after, created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (chat._task_job_id("done-job"), "chat_turn", "{}", "done", 1, 2,
                   time.time(), time.time()))
        c.commit()
    assert chat._fail_orphan_tasks() == 1
    assert _rows("SELECT status FROM chat_tasks WHERE id='done-job'")[0]["status"] == "failed"


# ── R2: one answer per turn, guaranteed by the database ──────────────────

def test_the_unique_index_refuses_a_second_answer_to_the_same_turn(client):
    """db.add_message returns None — cleanly, not an IntegrityError and not a
    500 — when the turn it answers is already answered."""
    from app import db
    _login(client)
    cid = _start(client)["conversation_id"]
    uid = [m["id"] for m in db.list_messages(cid) if m["role"] == "user"][0]

    first = db.add_message(cid, "assistant", {"text": "mine"}, reply_to=uid)
    assert first
    assert db.add_message(cid, "assistant", {"text": "also mine"}, reply_to=uid) is None
    assert [m["content"]["text"] for m in _assistants(cid)] == ["mine"]
    # Unrelated messages are unconstrained: only reply_to is unique.
    assert db.add_message(cid, "assistant", {"text": "sync answer"})
    assert db.add_message(cid, "user", {"text": "another question"})
    assert len(db.list_messages(cid)) == 4


def test_two_concurrent_attempts_of_one_chat_turn_answer_exactly_once(client):
    """The reproduced race, at full strength: two threads run the SAME
    chat_turn payload at the same instant, past the check-then-write guard
    (both see 'not answered yet'). Exactly one assistant message may exist —
    the loser discards its answer and abandons with ClaimLost."""
    from app import chat, jobs
    _login(client)
    body = _start(client, prompt="count sales rows")
    cid, tid = body["conversation_id"], body["task_id"]
    payload = jobs.get(chat._task_job_id(tid))["payload"]

    barrier = threading.Barrier(2)
    outcomes, lock = [], threading.Lock()

    def _attempt(n):
        job = {"id": f"attempt-{n}", "attempts": 1, "max_attempts": 2}
        barrier.wait(10)
        try:
            chat._chat_turn_job(payload, job)
            out = "answered"
        except jobs.ClaimLost:
            out = "abandoned"
        except Exception as e:                     # never a raw IntegrityError
            out = f"{type(e).__name__}: {e}"
        with lock:
            outcomes.append(out)

    threads = [threading.Thread(target=_attempt, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert len(_assistants(cid)) == 1, f"duplicate answer; outcomes={outcomes}"
    assert sorted(outcomes) in (["abandoned", "answered"], ["answered", "answered"]), outcomes
    # ["answered", "answered"] only happens when one attempt saw the other's
    # answer at the re-entrancy guard and short-circuited; either way the
    # conversation holds exactly one answer and the task is done.
    assert _rows("SELECT status FROM chat_tasks WHERE id=?", (tid,))[0]["status"] == "done"
    assert _assistants(cid)[0]["content"]["reply_to"]


def test_a_revoked_claim_stops_the_answer_being_written(client, monkeypatch):
    """A handler whose claim is reclaimed mid-run must not write an answer.
    Before the abort Event it ran to completion and appended one anyway,
    alongside whatever the new owner produced."""
    from app import chat, jobs
    monkeypatch.setattr(jobs, "_HEARTBEAT_S", 0.05)
    _login(client)
    body = _start(client, prompt="count sales rows")
    cid, tid = body["conversation_id"], body["task_id"]
    started, reclaimed = threading.Event(), threading.Event()
    real_answer = chat._answer

    def _slow_turn(ctx, user):
        started.set()
        reclaimed.wait(10)
        time.sleep(0.3)                    # let a heartbeat discover the loss
        return real_answer(ctx, {"text": "late answer", "source": "demo",
                                 "table": "sales", "rows": [], "columns": []})

    real_run_turn = chat._run_turn
    monkeypatch.setattr(chat, "_run_turn", _slow_turn)
    t = threading.Thread(target=jobs.run_one, args=("w1",), daemon=True)
    t.start()
    assert started.wait(10)
    jobs.reclaim_stale(stale_after=-1)     # another worker takes the job
    reclaimed.set()
    t.join(30)

    assert _assistants(cid) == [], "an attempt that lost its claim still answered"
    # Abandoned, not failed: the task belongs to whoever owns the job now, and
    # the job is back on the queue for them rather than burnt.
    assert _rows("SELECT status, error FROM chat_tasks WHERE id=?", (tid,))[0]["status"] == "running"
    job = jobs.get(chat._task_job_id(tid))
    assert job["status"] == "queued" and job["locked_by"] is None
    # The reclaimed attempt then answers normally.
    monkeypatch.setattr(chat, "_run_turn", real_run_turn)
    assert jobs.run_one("w2") is True
    assert len(_assistants(cid)) == 1
    assert _rows("SELECT status FROM chat_tasks WHERE id=?", (tid,))[0]["status"] == "done"


def test_a_duplicate_answer_abandons_instead_of_raising(client, monkeypatch):
    """The unique-violation path is a normal outcome, not an error: the job is
    abandoned silently (no completion, no failure, no retry) and no traceback
    of a database exception reaches the queue."""
    from app import chat, db, jobs
    _login(client)
    body = _start(client, prompt="count sales rows")
    cid, tid = body["conversation_id"], body["task_id"]
    uid = _rows("SELECT user_message_id FROM chat_tasks WHERE id=?", (tid,))[0]["user_message_id"]

    # Someone else answered this turn while the job sat on the queue, and the
    # task row was never updated — so the re-entrancy guard is bypassed below.
    assert db.add_message(cid, "assistant", {"text": "already answered"}, reply_to=uid)
    monkeypatch.setattr(chat, "_already_answered", lambda *a, **kw: False)
    assert jobs.run_one("w1") is True

    assert len(_assistants(cid)) == 1
    job = jobs.get(chat._task_job_id(tid))
    # Abandoned: still 'running' under the (now meaningless) claim, neither
    # completed nor failed nor re-queued — and no database exception escaped.
    assert job["status"] == "running" and job["error"] is None
