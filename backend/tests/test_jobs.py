"""Durable job queue, scheduler leases, the chat_turn job and the SPA fallback.

Proves: the enqueue → claim → complete lifecycle; a failing handler is
retried once with backoff and then fails with its error recorded; claims are
exclusive across worker ids and across 8 threads racing for 20 jobs;
reclaim_stale re-queues a running job whose heartbeat stopped and fails one
that is out of attempts; a claim is FENCED by its token, so a worker whose job
was reclaimed cannot complete it, heartbeat it or spend one of its attempts,
and its heartbeat thread stops as soon as a beat is refused; a lease cannot be taken by a second holder until it
expires while its holder can renew; the Worker thread drains the queue and
its scheduler loop ticks only under the lease; a SLOW tick runs off the poll
loop (jobs keep flowing), never overlaps itself, renews its lease while it
runs and is waited for by stop(); POST /api/chat/background enqueues a
chat_turn job that run_one() completes into a done task plus an assistant
message, a re-run of the same payload does NOT answer twice, and two turns
started in the SAME conversation both get answered; canvas composition is
offered to a BYOK user with no server key; `python -m app.worker` closes the
Postgres pool on shutdown; autopilot.tick_once / sync.tick_once run without a
lease; and the SPA fallback serves index.html for router paths, files strictly
inside dist/, and a JSON 404 for unknown /api paths.

Run from the backend directory:
    python -m pytest tests/test_jobs.py -q
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
def env(tmp_path, monkeypatch):
    """A fresh SQLite DB with the queue tables, no worker running."""
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    from app import db as _db
    importlib.reload(_db)
    from app import jobs
    _db.init_db()
    jobs.init_tables()
    registered = set(jobs._HANDLERS)
    yield jobs
    # Handlers registered by a test must not leak into the next one.
    for k in list(jobs._HANDLERS):
        if k not in registered:
            del jobs._HANDLERS[k]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Full app, no LLM key (deterministic fallback), worker OFF so tests
    drive jobs with run_one()."""
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "jobs_app.db"))
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


# ── Lifecycle ────────────────────────────────────────────────────────────

def test_enqueue_claim_complete_lifecycle(env):
    jobs = env
    seen = {}

    @jobs.handler("t_echo")
    def _echo(payload, job):
        seen["payload"], seen["job_id"] = payload, job["id"]
        return {"echo": payload["x"]}

    jid = jobs.enqueue("t_echo", {"x": 42}, user_id="u1")
    j = jobs.get(jid)
    assert j["status"] == "queued" and j["attempts"] == 0 and j["user_id"] == "u1"
    assert jobs.stats()["queued"] == 1

    assert jobs.run_one("w1") is True
    j = jobs.get(jid)
    assert j["status"] == "done" and j["attempts"] == 1
    assert j["result"] == {"echo": 42} and j["error"] is None
    assert j["finished_at"] and j["locked_by"] is None
    assert seen == {"payload": {"x": 42}, "job_id": jid}
    assert jobs.run_one("w1") is False                # nothing left
    assert jobs.stats() == {"queued": 0, "running": 0, "done": 1, "failed": 0}


def test_delayed_job_is_not_claimed_before_run_after(env):
    jobs = env
    jobs.handler("t_noop")(lambda p, j: None)
    jid = jobs.enqueue("t_noop", {}, run_after=time.time() + 3600)
    assert jobs.claim("w1") is None
    jobs.enqueue("t_noop", {}, run_after=time.time() - 1)
    assert jobs.claim("w1")["id"] != jid


def test_failing_handler_retries_once_then_fails_with_error(env, monkeypatch):
    jobs = env
    monkeypatch.setattr(jobs, "_backoff", lambda n: 0.0)   # retry immediately
    calls = []

    @jobs.handler("t_boom")
    def _boom(payload, job):
        calls.append(job["attempts"])
        raise ValueError("kaboom")

    jid = jobs.enqueue("t_boom", {}, max_attempts=2)
    assert jobs.run_one("w1") is True
    j = jobs.get(jid)
    assert j["status"] == "queued" and j["attempts"] == 1     # re-queued once
    assert "kaboom" in j["error"] and j["locked_by"] is None
    assert jobs.run_one("w1") is True
    j = jobs.get(jid)
    assert j["status"] == "failed" and j["attempts"] == 2
    assert j["error"] == "ValueError: kaboom" and j["finished_at"]
    assert calls == [1, 2]
    assert jobs.run_one("w1") is False


def test_backoff_delays_the_retry(env):
    jobs = env
    jobs.handler("t_boom2")(lambda p, j: (_ for _ in ()).throw(RuntimeError("x")))
    jid = jobs.enqueue("t_boom2", {}, max_attempts=3)
    jobs.run_one("w1")
    j = jobs.get(jid)
    assert j["status"] == "queued" and j["run_after"] > time.time() + 1
    assert jobs.claim("w1") is None                            # not due yet


def test_unregistered_kind_fails_without_retry(env):
    jobs = env
    jid = jobs.enqueue("t_nobody", {})
    assert jobs.run_one("w1") is True
    j = jobs.get(jid)
    assert j["status"] == "failed" and "no handler" in j["error"]


# ── Exclusive claims ─────────────────────────────────────────────────────

def test_two_workers_never_claim_the_same_job(env):
    jobs = env
    jobs.handler("t_x")(lambda p, j: None)
    a = jobs.enqueue("t_x", {"n": 1})
    b = jobs.enqueue("t_x", {"n": 2})
    j1 = jobs.claim("w1")
    j2 = jobs.claim("w2")
    assert {j1["id"], j2["id"]} == {a, b}
    assert j1["locked_by"].startswith("w1#") and j2["locked_by"].startswith("w2#")
    assert j1["status"] == j2["status"] == "running"
    assert jobs.claim("w3") is None


def test_eight_threads_claiming_twenty_jobs_see_no_duplicates(env):
    jobs = env
    jobs.handler("t_race")(lambda p, j: None)
    ids = {jobs.enqueue("t_race", {"i": i}) for i in range(20)}
    claimed, lock = [], threading.Lock()
    go = threading.Event()

    def _claimer(wid):
        go.wait()
        while True:
            j = jobs.claim(wid)
            if j is None:
                return
            with lock:
                claimed.append(j["id"])

    threads = [threading.Thread(target=_claimer, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join(timeout=30)
    assert len(claimed) == 20
    assert set(claimed) == ids                                 # each exactly once
    assert jobs.stats()["running"] == 20


# ── Stale reclaim ────────────────────────────────────────────────────────

def _set(jobs, jid, **cols):
    from app import db
    c = db._conn()
    sets = ", ".join(f"{k}=?" for k in cols)
    c.execute(f"UPDATE background_jobs SET {sets} WHERE id=?", list(cols.values()) + [jid])
    c.commit()
    c.close()


def test_reclaim_stale_requeues_or_fails_by_attempts(env):
    jobs = env
    jobs.handler("t_stale")(lambda p, j: None)
    fresh = jobs.enqueue("t_stale", {"k": "fresh"})
    stale = jobs.enqueue("t_stale", {"k": "stale"})
    spent = jobs.enqueue("t_stale", {"k": "spent"}, max_attempts=1)
    for _ in range(3):
        assert jobs.claim("dead-worker") is not None
    old = time.time() - 10_000
    _set(jobs, stale, heartbeat_at=old)
    _set(jobs, spent, heartbeat_at=old)

    assert jobs.reclaim_stale(300) == (1, 1)
    assert jobs.get(fresh)["status"] == "running"             # heartbeat is recent
    s = jobs.get(stale)
    assert s["status"] == "queued" and s["locked_by"] is None and "stale" in s["error"]
    f = jobs.get(spent)
    assert f["status"] == "failed" and f["finished_at"] and "stale" in f["error"]

    # The re-queued job is claimable again, by a different worker.
    j = jobs.claim("alive-worker")
    assert j["id"] == stale and j["attempts"] == 2


def test_heartbeat_keeps_a_running_job_out_of_reclaim(env):
    jobs = env
    jobs.handler("t_hb")(lambda p, j: None)
    jid = jobs.enqueue("t_hb", {})
    token = jobs.claim("w1")["locked_by"]
    _set(jobs, jid, heartbeat_at=time.time() - 10_000)
    assert jobs.heartbeat(jid, token) is True
    assert jobs.reclaim_stale(300) == (0, 0)
    assert jobs.get(jid)["status"] == "running"


# ── Fenced leases: a reclaimed job is no longer the old worker's ─────────

def test_stale_worker_cannot_complete_a_job_that_was_reclaimed(env):
    """The P1 double-execution bug: A is slow, its heartbeat goes stale, B
    reclaims and re-runs the job — and A then finishes and calls complete().
    Without the token fence A's completion landed on B's row and the job was
    'done' while B was still executing it (a chat turn answered twice)."""
    jobs = env
    jobs.handler("t_fence")(lambda p, j: None)
    jid = jobs.enqueue("t_fence", {"n": 1}, max_attempts=3)
    a = jobs.claim("A")
    assert a["id"] == jid
    token_a = a["locked_by"]

    _set(jobs, jid, heartbeat_at=time.time() - 10_000)
    assert jobs.reclaim_stale(300) == (1, 0)
    b = jobs.claim("B")
    assert b["id"] == jid and b["locked_by"] != token_a
    token_b = b["locked_by"]

    # A comes back and tries to finish the job it no longer owns.
    assert jobs.complete(jid, token_a, {"from": "A"}) is False
    j = jobs.get(jid)
    assert j["status"] == "running" and j["locked_by"] == token_b
    assert j["result"] is None

    # B's completion is the one that counts.
    assert jobs.complete(jid, token_b, {"from": "B"}) is True
    j = jobs.get(jid)
    assert j["status"] == "done" and j["result"] == {"from": "B"}
    assert j["locked_by"] is None

    # And A still cannot overwrite the finished row afterwards.
    assert jobs.complete(jid, token_a, {"from": "A"}) is False
    assert jobs.get(jid)["result"] == {"from": "B"}


def test_stale_heartbeat_is_refused_after_reclaim(env):
    jobs = env
    jobs.handler("t_fence_hb")(lambda p, j: None)
    jid = jobs.enqueue("t_fence_hb", {}, max_attempts=3)
    token_a = jobs.claim("A")["locked_by"]
    _set(jobs, jid, heartbeat_at=time.time() - 10_000)
    jobs.reclaim_stale(300)
    # Re-queued but not yet re-claimed: locked_by is NULL, so A matches nothing.
    assert jobs.heartbeat(jid, token_a) is False
    token_b = jobs.claim("B")["locked_by"]
    assert jobs.heartbeat(jid, token_a) is False       # ... nor once B holds it
    assert jobs.heartbeat(jid, token_b) is True
    beat = jobs.get(jid)["heartbeat_at"]
    assert beat and jobs.get(jid)["locked_by"] == token_b


def test_lost_claim_stops_the_heartbeat_thread(env, monkeypatch):
    """A handler that outlives its claim must stop beating the moment a
    heartbeat is refused — otherwise the old worker keeps the row that the
    NEW owner is running looking freshly alive."""
    jobs = env
    monkeypatch.setattr(jobs, "_HEARTBEAT_S", 0.02)
    started, release = threading.Event(), threading.Event()

    @jobs.handler("t_fence_thread")
    def _slow(payload, job):
        started.set()
        release.wait(5)
        return {"ok": True}

    jid = jobs.enqueue("t_fence_thread", {}, max_attempts=3)
    runner = threading.Thread(target=jobs.run_one, args=("A",), daemon=True)
    runner.start()
    assert started.wait(5)
    beat_names = lambda: [t.name for t in threading.enumerate()
                          if t.name.startswith("job-heartbeat-")]
    assert beat_names()                                    # beating while it runs

    # Reclaim it out from under the running handler, then let the handler end.
    _set(jobs, jid, heartbeat_at=time.time() - 10_000)
    assert jobs.reclaim_stale(300) == (1, 0)
    token_b = jobs.claim("B")["locked_by"]
    deadline = time.time() + 5
    while beat_names() and time.time() < deadline:
        time.sleep(0.02)
    assert not beat_names(), "heartbeat thread kept beating after losing the claim"
    stamp = jobs.get(jid)["heartbeat_at"]

    release.set()
    runner.join(timeout=5)
    # A's result is discarded: the job is still B's, still running, untouched.
    j = jobs.get(jid)
    assert j["status"] == "running" and j["locked_by"] == token_b
    assert j["result"] is None and j["heartbeat_at"] == stamp


def test_stale_fail_neither_consumes_an_attempt_nor_requeues(env):
    jobs = env
    jobs.handler("t_fence_fail")(lambda p, j: None)
    jid = jobs.enqueue("t_fence_fail", {}, max_attempts=5)
    token_a = jobs.claim("A")["locked_by"]
    _set(jobs, jid, heartbeat_at=time.time() - 10_000)
    jobs.reclaim_stale(300)
    token_b = jobs.claim("B")["locked_by"]
    before = jobs.get(jid)

    assert jobs.fail(jid, token_a, "A blew up", retry=True) is None
    assert jobs.fail(jid, token_a, "A blew up", retry=False) is None
    after = jobs.get(jid)
    assert after["status"] == "running" and after["locked_by"] == token_b
    assert after["attempts"] == before["attempts"] == 2
    assert after["run_after"] == before["run_after"]
    assert "A blew up" not in (after["error"] or "")

    # B's failure is honoured and is the only one that spends an attempt.
    assert jobs.fail(jid, token_b, "B blew up", retry=True) == "queued"
    assert jobs.get(jid)["error"] == "B blew up"


def test_fail_on_an_unknown_job_returns_none(env):
    jobs = env
    assert jobs.fail("no-such-job", "tok", "boom") is None


# ── Leases ───────────────────────────────────────────────────────────────

def test_lease_is_exclusive_until_expiry_and_renewable(env):
    jobs = env
    assert jobs.acquire_lease("autopilot", "A", ttl_s=60) is True
    assert jobs.acquire_lease("autopilot", "B", ttl_s=60) is False   # held by A
    assert jobs.acquire_lease("autopilot", "A", ttl_s=60) is True    # A renews
    assert jobs.acquire_lease("autopilot", "B", ttl_s=60) is False
    # Expire A's lease (as a dead holder would) → B takes over.
    from app import db
    c = db._conn()
    c.execute("UPDATE scheduler_leases SET expires_at=? WHERE name=?", (time.time() - 1, "autopilot"))
    c.commit()
    c.close()
    assert jobs.acquire_lease("autopilot", "B", ttl_s=60) is True
    assert jobs.acquire_lease("autopilot", "A", ttl_s=60) is False
    # Independent names do not interfere; release frees it for anyone.
    assert jobs.acquire_lease("m365_sync", "A", ttl_s=60) is True
    jobs.release_lease("autopilot", "B")
    assert jobs.acquire_lease("autopilot", "A", ttl_s=60) is True


def test_release_by_non_holder_is_a_noop(env):
    jobs = env
    assert jobs.acquire_lease("x", "A", ttl_s=60)
    jobs.release_lease("x", "B")
    assert jobs.acquire_lease("x", "B", ttl_s=60) is False


# ── The Worker thread ────────────────────────────────────────────────────

def test_worker_thread_drains_the_queue_and_stops_cleanly(env):
    jobs = env
    done = []
    lock = threading.Lock()

    @jobs.handler("t_work")
    def _work(payload, job):
        with lock:
            done.append(payload["i"])
        return {"i": payload["i"]}

    ids = [jobs.enqueue("t_work", {"i": i}) for i in range(6)]
    w = jobs.Worker(worker_id="wt", concurrency=3, poll_s=0.05, schedulers=[]).start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline and jobs.stats()["done"] < 6:
            time.sleep(0.05)
    finally:
        w.stop()
    assert sorted(done) == list(range(6))
    assert all(jobs.get(i)["status"] == "done" for i in ids)
    assert w.running is False
    assert w.worker_id == "wt"


def test_scheduler_runs_only_under_its_lease(env):
    jobs = env
    ticks = []
    scheds = lambda: [{"name": "autopilot", "fn": lambda: ticks.append("tick"),
                       "enabled": lambda: True, "interval_s": 60}]
    a = jobs.Worker(worker_id="A", schedulers=scheds())
    b = jobs.Worker(worker_id="B", schedulers=scheds())
    a.run_schedulers()
    assert ticks == ["tick"]                    # A holds the lease
    b.run_schedulers()
    assert ticks == ["tick"]                    # B is locked out
    a._next_sched["autopilot"] = 0              # A's next interval is due
    a.run_schedulers()
    assert ticks == ["tick", "tick"]            # A renews and ticks again
    # A disabled ticker (kill-switch) never takes the lease at all.
    off = [{"name": "m365_sync", "fn": lambda: ticks.append("m365"),
            "enabled": lambda: False, "interval_s": 60}]
    jobs.Worker(worker_id="C", schedulers=off).run_schedulers()
    assert "m365" not in ticks
    assert jobs.acquire_lease("m365_sync", "D", ttl_s=1) is True


def test_slow_tick_runs_off_the_poll_loop_and_never_overlaps(env, monkeypatch):
    """A tick is arbitrarily slow (an autopilot pass, an M365 sync). Inline in
    the poll loop it starved job dispatch, outlived its lease and was
    abandoned by stop(); it now runs on the scheduler executor."""
    jobs = env
    monkeypatch.setattr(jobs, "_SCHED_RENEW_S", 0.05)      # renew fast enough to observe
    started, finished, release = [], [], threading.Event()

    def _slow_tick():
        started.append(time.time())
        release.wait(10)
        finished.append(time.time())

    sched = [{"name": "autopilot", "fn": _slow_tick,
              "enabled": lambda: True, "interval_s": 0.05}]
    ran = []

    @jobs.handler("t_during_tick")
    def _work(payload, job):
        ran.append(payload["i"])
        return {"i": payload["i"]}

    w = jobs.Worker(worker_id="sw", concurrency=2, poll_s=0.02, schedulers=sched).start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not started:
            time.sleep(0.01)
        assert started, "the scheduler never ticked"

        # Jobs are dispatched WHILE the tick is stuck.
        ids = [jobs.enqueue("t_during_tick", {"i": i}) for i in range(3)]
        deadline = time.time() + 10
        while time.time() < deadline and jobs.stats()["done"] < 3:
            time.sleep(0.02)
        assert sorted(ran) == [0, 1, 2]
        assert all(jobs.get(i)["status"] == "done" for i in ids)

        # Many intervals have passed: still exactly one tick, and its lease is
        # being renewed under it rather than expiring mid-run.
        def _expiry():
            from app import db
            c = db._conn()
            r = c.execute("SELECT holder, expires_at FROM scheduler_leases WHERE name=?",
                          ("autopilot",)).fetchone()
            c.close()
            return r["holder"], float(r["expires_at"])

        holder, first = _expiry()
        assert holder == "sw"
        time.sleep(0.3)
        assert started == started[:1], "a second tick started while the first was running"
        assert _expiry()[1] > first, "the lease was not renewed under the running tick"

        release.set()
        w.stop()                                   # waits for the in-flight tick
        assert len(finished) == 1
    finally:
        release.set()
        w.stop()


def test_stop_waits_for_an_in_flight_tick_before_releasing_the_lease(env):
    """stop() used to abandon a running tick and drop its lease, so another
    replica could start the same pass alongside it."""
    jobs = env
    done = []

    def _tick():
        time.sleep(0.4)
        done.append("finished")

    w = jobs.Worker(worker_id="sw2", concurrency=1, poll_s=0.02,
                    schedulers=[{"name": "m365_sync", "fn": _tick,
                                 "enabled": lambda: True, "interval_s": 0.05}]).start()
    deadline = time.time() + 5
    while time.time() < deadline and not w._sched_inflight:
        time.sleep(0.01)
    w.stop()
    assert done == ["finished"]                    # stop() returned only after it
    assert jobs.acquire_lease("m365_sync", "other", ttl_s=5) is True   # lease released


def test_worker_mode_env(monkeypatch):
    from app import jobs
    monkeypatch.delenv("STUDIO_WORKER_MODE", raising=False)
    assert jobs.worker_mode() == "thread"
    monkeypatch.setenv("STUDIO_WORKER_MODE", "external")
    assert jobs.worker_mode() == "external"
    monkeypatch.setenv("STUDIO_WORKER_MODE", "nonsense")
    assert jobs.worker_mode() == "thread"


# ── Tickers exist and run without a lease ────────────────────────────────

def test_tick_once_runs_without_a_lease(client):
    from app import autopilot, main
    from app.extraction import sync
    assert callable(autopilot.tick_once) and callable(sync.tick_once)
    autopilot.tick_once()          # nothing due: a no-op, no exception
    sync.tick_once()               # unconfigured: a no-op
    assert autopilot.start_ticker() is None and sync.start_ticker() is None
    assert not any(t.name in ("autopilot-ticker", "m365-sync-ticker")
                   for t in threading.enumerate())
    assert main._worker is None    # STUDIO_WORKER_MODE=off started nothing


def test_thread_mode_starts_an_in_process_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "thread_mode.db"))
    monkeypatch.setenv("STUDIO_WORKER_MODE", "thread")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    monkeypatch.setenv("STUDIO_JOB_POLL_S", "0.05")
    from app import db as _db, jobs
    importlib.reload(_db)
    import app.main as main
    importlib.reload(main)
    jobs.handler("t_inproc")(lambda p, j: {"ok": True})
    try:
        with TestClient(main.app):
            assert main._worker is not None and main._worker.running
            jid = jobs.enqueue("t_inproc", {})
            deadline = time.time() + 10
            while time.time() < deadline and jobs.get(jid)["status"] != "done":
                time.sleep(0.05)
            assert jobs.get(jid)["status"] == "done"
        assert main._worker is None            # shutdown stopped it
    finally:
        jobs._HANDLERS.pop("t_inproc", None)


# ── chat_turn end to end ─────────────────────────────────────────────────

def _assistant_count(cid):
    from app import db
    return sum(1 for m in db.list_messages(cid) if m["role"] == "assistant")


def _task(tid):
    from app import db
    c = db._conn()
    r = c.execute("SELECT status, error FROM chat_tasks WHERE id=?", (tid,)).fetchone()
    c.close()
    return dict(r)


def test_chat_background_enqueues_and_run_one_answers_once(client):
    from app import db, jobs
    _login(client)
    r = client.post("/api/chat/background", json={
        "prompt": "total revenue by region", "source": "demo", "table": "sales"})
    assert r.status_code == 202, r.text
    body = r.json()
    cid, tid = body["conversation_id"], body["task_id"]
    assert body["status"] == "running"
    assert _task(tid)["status"] == "running"
    # The user turn was recorded at request time; nothing has answered yet.
    msgs = db.list_messages(cid)
    assert [m["role"] for m in msgs] == ["user"]
    assert jobs.stats()["queued"] == 1
    c = db._conn()
    job = dict(c.execute("SELECT * FROM background_jobs WHERE status='queued'").fetchone())
    c.close()
    assert job["kind"] == "chat_turn"
    payload = jobs.get(job["id"])["payload"]
    assert payload["cid"] == cid and payload["tid"] == tid
    assert payload["body"]["prompt"] == "total revenue by region"

    # A worker runs it: task done, exactly one assistant message.
    assert jobs.run_one("w1") is True
    assert jobs.get(job["id"])["status"] == "done"
    assert _task(tid)["status"] == "done"
    assert _assistant_count(cid) == 1
    st = client.get(f"/api/tasks/{tid}").json()
    assert st["status"] == "done"
    assert st["steps"]                                       # live activity landed

    # A re-queued copy of the same payload (a retry / reclaim after a crash)
    # must NOT answer twice.
    jid2 = jobs.enqueue("chat_turn", payload, user_id=payload["user_id"])
    assert jobs.run_one("w2") is True
    j2 = jobs.get(jid2)
    assert j2["status"] == "done" and j2["result"].get("reentered") is True
    assert _assistant_count(cid) == 1
    assert _task(tid)["status"] == "done"


def test_chat_background_validation_fails_before_any_write(client):
    from app import jobs
    _login(client, "viewer@studio.local", "viewer123")
    r = client.post("/api/chat/background", json={
        "prompt": "peek", "source": "demo", "table": "customers"})   # PII: viewer denied
    assert r.status_code == 403
    assert jobs.stats()["queued"] == 0
    assert client.post("/api/chat/background", json={
        "prompt": "   ", "source": "demo", "table": "sales"}).status_code == 400


def test_chat_turn_retries_then_marks_task_failed(client, monkeypatch):
    from app import chat, jobs
    monkeypatch.setattr(jobs, "_backoff", lambda n: 0.0)
    _login(client)
    body = client.post("/api/chat/background", json={
        "prompt": "count sales rows", "source": "demo", "table": "sales"}).json()
    cid, tid = body["conversation_id"], body["task_id"]

    def _boom(ctx, user):
        raise RuntimeError("warehouse down")

    monkeypatch.setattr(chat, "_run_turn", _boom)
    assert jobs.run_one("w1") is True
    # First attempt: re-queued, task still running (a retry may still answer).
    from app import db
    conn = db._conn()
    job = dict(conn.execute("SELECT * FROM background_jobs").fetchone())
    conn.close()
    assert job["status"] == "queued" and job["attempts"] == 1
    assert _task(tid)["status"] == "running"
    # Last attempt: the task fails with the error, as the thread pool did.
    assert jobs.run_one("w1") is True
    j = jobs.get(job["id"])
    assert j["status"] == "failed" and "warehouse down" in j["error"]
    t = _task(tid)
    assert t["status"] == "failed" and "warehouse down" in t["error"]
    assert _assistant_count(cid) == 0


def test_two_background_turns_in_one_conversation_are_both_answered(client):
    """The re-entrancy guard must be EXACT, not temporal. When it asked "is
    there an assistant message newer than my user turn?", the first turn to
    answer marked the second task done as well and its question was never
    answered."""
    from app import db, jobs
    _login(client)
    first = client.post("/api/chat/background", json={
        "prompt": "total revenue by region", "source": "demo", "table": "sales"}).json()
    cid = first["conversation_id"]
    second = client.post("/api/chat/background", json={
        "prompt": "revenue by month", "source": "demo", "table": "sales",
        "conversation_id": cid}).json()
    assert second["conversation_id"] == cid
    assert first["task_id"] != second["task_id"]

    # Each task records the user message it answers, and they differ.
    c = db._conn()
    mids = {r["id"]: r["user_message_id"] for r in c.execute(
        "SELECT id, user_message_id FROM chat_tasks WHERE conversation_id=?", (cid,)).fetchall()}
    c.close()
    assert all(mids.values()) and len(set(mids.values())) == 2
    user_ids = [m["id"] for m in db.list_messages(cid) if m["role"] == "user"]
    assert sorted(mids.values()) == sorted(user_ids)

    assert jobs.run_one("w1") is True                 # answers the first turn
    assert jobs.run_one("w1") is True                 # ... and still answers the second
    assert _assistant_count(cid) == 2
    assert _task(first["task_id"])["status"] == "done"
    assert _task(second["task_id"])["status"] == "done"
    answers = [m["content"] for m in db.list_messages(cid) if m["role"] == "assistant"]
    assert sorted(a["reply_to"] for a in answers) == sorted(user_ids)

    # Retry safety is intact: re-running either payload answers nothing again.
    payloads = [jobs.get(j)["payload"] for j in
                [r["id"] for r in _all_jobs()]]
    for payload in payloads:
        jid = jobs.enqueue("chat_turn", payload, user_id=payload["user_id"])
        assert jobs.run_one("w2") is True
        assert jobs.get(jid)["result"].get("reentered") is True
    assert _assistant_count(cid) == 2


def test_task_written_before_the_column_keeps_the_temporal_guard(client):
    """A task recorded by an older build has no user_message_id; it must still
    be retry-safe (the fallback), not answer twice."""
    from app import chat, db, jobs
    _login(client)
    body = client.post("/api/chat/background", json={
        "prompt": "total revenue by region", "source": "demo", "table": "sales"}).json()
    cid, tid = body["conversation_id"], body["task_id"]
    c = db._conn()
    c.execute("UPDATE chat_tasks SET user_message_id=NULL WHERE id=?", (tid,))
    c.commit()
    c.close()
    assert chat._already_answered(cid, tid) is False
    assert jobs.run_one("w1") is True
    assert _assistant_count(cid) == 1
    assert chat._already_answered(cid, tid) is True      # temporal fallback still holds


def _all_jobs():
    from app import db
    c = db._conn()
    rows = [dict(r) for r in c.execute("SELECT id FROM background_jobs").fetchall()]
    c.close()
    return rows


# ── Canvas composition and the user's own key ────────────────────────────

def test_canvas_compose_is_offered_to_a_byok_user(client, monkeypatch):
    """canvas_edit asked whether the SERVER has a key, so a user with only
    their OWN key never reached compose_canvas — they silently got the
    single-chart editor instead."""
    from app import agent, chat
    _login(client)
    seen = {}

    def _available(spec=None, user=None):
        # The server has no key; this user brought one (keys.py).
        return bool(user and user.get("id"))

    def _compose(instruction, columns, rows, chart, **kw):
        seen["user"] = (kw.get("user") or {}).get("email")
        return {"note": "composed", "panels": [
            {"sql": None, "columns": columns, "rows": rows, "chart": chart}]}

    monkeypatch.setattr(agent, "llm_available", _available)
    monkeypatch.setattr(agent, "compose_canvas", _compose)
    monkeypatch.setattr(chat, "_canvas_source", lambda body, user: (None, [], {}))
    monkeypatch.setattr(agent, "edit_canvas",
                        lambda *a, **k: pytest.fail("fell back to the single-chart editor"))

    r = client.post("/api/canvas/edit", json={
        "instruction": "make it a bar chart", "columns": ["a"], "rows": [[1]]})
    assert r.status_code == 200, r.text
    assert r.json()["note"] == "composed"
    assert seen["user"] == "admin@studio.local"

    # And with no key anywhere, the fallback still applies.
    monkeypatch.setattr(agent, "llm_available", lambda spec=None, user=None: False)
    monkeypatch.setattr(agent, "edit_canvas", lambda *a, **k: {
        "note": "edited", "columns": ["a"], "rows": [[1]], "chart": None})
    r = client.post("/api/canvas/edit", json={
        "instruction": "make it a bar chart", "columns": ["a"], "rows": [[1]]})
    assert r.status_code == 200 and r.json()["note"] == "edited"


# ── Worker process shutdown ──────────────────────────────────────────────

def test_worker_main_closes_the_pg_pool_on_shutdown(tmp_path, monkeypatch):
    """SIGTERM-to-exit took ~20s because the Postgres pool's threads were left
    running; the worker must close it exactly as main.py's shutdown does."""
    import os
    import signal
    from app import db as _db, jobs, worker as worker_mod
    import app.main as main

    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "worker_main.db"))
    monkeypatch.setattr(main, "init_state", lambda: None)
    closed = []
    monkeypatch.setattr(_db, "close_pool", lambda: closed.append("closed"))
    events = []

    class FakeWorker:
        worker_id = "fake"
        running = True

        def start(self):
            events.append("start")
            return self

        def stop(self, wait=True):
            events.append("stop")

    monkeypatch.setattr(jobs, "Worker", lambda **kw: FakeWorker())
    handlers = {"chat_turn": lambda p, j: None}
    monkeypatch.setattr(jobs, "handlers", lambda: handlers)

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    fire = threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM))
    fire.start()
    try:
        assert worker_mod.main([]) == 0
    finally:
        fire.cancel()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    assert events == ["start", "stop"]               # the worker was stopped first
    assert closed == ["closed"]                      # ... then the pool was closed


def test_sync_chat_is_unchanged(client):
    _login(client)
    r = client.post("/api/chat", json={
        "prompt": "total revenue by region", "source": "demo", "table": "sales"})
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]
    from app import db, jobs
    assert [m["role"] for m in db.list_messages(cid)] == ["user", "assistant"]
    assert jobs.stats()["queued"] == 0                       # nothing enqueued
    # A follow-up in the same conversation sees the first exchange as history
    # but not its own prompt (the recorded turn is dropped from context).
    r2 = client.post("/api/chat", json={
        "prompt": "and by month", "source": "demo", "table": "sales",
        "conversation_id": cid})
    assert r2.status_code == 200
    assert [m["role"] for m in db.list_messages(cid)] == ["user", "assistant"] * 2


# ── SPA fallback ─────────────────────────────────────────────────────────

@pytest.fixture()
def spa_client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root>SPA</div>")
    (dist / "assets" / "x.js").write_text("console.log('bundle')")
    (dist / "favicon.ico").write_bytes(b"ICO")
    (tmp_path / "secret.txt").write_text("outside dist")
    monkeypatch.setenv("STUDIO_STATIC_DIR", str(dist))
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    import app.main as main
    importlib.reload(main)
    yield TestClient(main.app)
    # Put main back on the real dist path for the rest of the suite.
    monkeypatch.undo()
    importlib.reload(main)


def test_spa_fallback_serves_index_files_and_api_404(spa_client):
    c = spa_client
    for path in ("/jobs", "/c/abc-123", "/dashboards/9", "/"):
        r = c.get(path)
        assert r.status_code == 200 and "SPA" in r.text, path
        assert r.headers["content-type"].startswith("text/html")
    r = c.get("/api/nope")
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    assert c.get("/api").status_code == 404
    r = c.get("/assets/x.js")
    assert r.status_code == 200 and "bundle" in r.text
    r = c.get("/favicon.ico")
    assert r.status_code == 200 and r.content == b"ICO"
    # Traversal never escapes dist/: the SPA shell comes back, not the secret.
    r = c.get("/%2e%2e/secret.txt")
    assert r.status_code == 200 and "outside dist" not in r.text
    r = c.get("/assets/../../secret.txt")
    assert "outside dist" not in r.text
    # Unprefixed health, docs and the schema keep working ahead of the catch-all.
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/docs").status_code == 200
    assert c.get("/openapi.json").status_code == 200
