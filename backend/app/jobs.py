"""Durable background job queue + single-instance scheduler leases.

Before this module, background work lived in the web process: chat turns on
a ThreadPoolExecutor, the autopilot and M365 tickers on daemon threads. A
restart lost every in-flight turn, and N web replicas ran N tickers. This
module moves that work onto the app database (SQLite or Postgres — the same
db facade every other module uses, no broker, no new dependency):

  * background_jobs is the queue. A web request ENQUEUES a JSON payload and
    returns; a Worker CLAIMS it with one atomic UPDATE and runs the handler
    registered for its kind. The row carries the whole outcome (status,
    attempts, result, error), so a job survives a process restart: a claimed
    job whose worker died stops heartbeating, and reclaim_stale() puts it
    back on the queue (or fails it when it is out of attempts).
  * scheduler_leases makes the periodic tickers single-instance. Every
    worker tries to acquire the "autopilot" / "m365_sync" lease before
    ticking; only the holder ticks, and a dead holder's lease expires on
    its own, so another replica takes over without coordination.

Invariants:
  - Payloads and results are JSON (jsonable dicts). Nothing live — no
    connectors, no user objects — crosses the queue; handlers re-derive
    everything from ids so a job can run in ANY process, including one
    started after the enqueuer exited.
  - claim() is the only way a job goes 'queued' -> 'running', and it does so
    in ONE conditional UPDATE keyed by a per-claim token, so two workers
    (threads, processes or replicas) can never both win the same row. On
    Postgres the subselect adds FOR UPDATE SKIP LOCKED so contending workers
    skip past each other instead of serialising; SQLite serialises writers,
    which gives the same guarantee.
  - That per-claim token FENCES every later write. heartbeat(), complete()
    and fail() all carry it and match `AND locked_by = <token>`; reclaim_stale()
    clears locked_by, so the moment a job is taken away from a worker whose
    heartbeat went stale, that worker's token stops matching and it can no
    longer complete, fail or keep alive a job the new owner is running. A
    worker that discovers its token no longer matches ABANDONS the job
    silently — no completion, no failure, no retry — because the new owner's
    run is the one that counts. Without the fence a slow-but-alive worker
    could mark done a job another worker was still executing, which is how a
    chat turn got answered twice and a scheduler tick ran twice.
  - Handlers must be re-entrant: a retry (or a reclaim after a crash) runs
    the same payload again, so a handler checks for its own prior effects
    before repeating a side effect.
  - The fence protects the queue ROW; it cannot protect a handler's EFFECTS,
    because Python cannot preempt a running thread. So a lost claim is also
    published COOPERATIVELY: _execute() hands the handler an abort Event (in
    job["abort"], and via claim_lost()/check_claim() for code too deep to
    thread it through) and sets it the moment a heartbeat is refused. A
    handler that checks it at its safe points raises ClaimLost and is
    abandoned silently. A handler that IGNORES it keeps running to the end —
    nothing can stop it — so any effect that must happen at most once needs a
    real database constraint behind it as well (chat's answers have a UNIQUE
    index on messages.reply_to; see chat._answer).
  - A reclaim pass also runs the registered reconciler() callbacks. reclaim
    repairs the queue; a reconciler repairs the rows that POINT AT the queue
    (a chat_tasks row left 'running' with no job behind it), which reclaim
    itself cannot see.
  - STUDIO_WORKER_MODE decides WHERE jobs run: "thread" (default — the web
    process runs one Worker in-process, the pre-queue behaviour), "external"
    (the web process only enqueues; `python -m app.worker` runs them) or
    "off" (tests: nothing runs unless the test calls run_one()).
"""
import concurrent.futures
import json
import logging
import os
import socket
import threading
import time
import uuid

from . import db

log = logging.getLogger("studio.jobs")

STATUSES = ("queued", "running", "done", "failed")
_HANDLERS = {}
# fn() callbacks run after every reclaim pass — see reconciler(). Keyed by
# qualified name so re-importing a module replaces its callback instead of
# registering a second copy.
_RECONCILERS = {}

# The abort Event for the job (or scheduler tick) running on THIS thread.
# Thread-local because a handler's inner layers — chat._answer is four calls
# deep — must be able to ask "do I still own this work?" without every
# function in between growing a parameter for it.
_LOCAL = threading.local()


class ClaimLost(Exception):
    """A handler noticed its claim (or its scheduler lease) was taken away
    mid-run and abandoned the work before writing anything.

    _execute() treats it as a SILENT abandonment: no completion, no failure,
    no retry, one log line. The worker that reclaimed the job is the one whose
    run counts, and a fail() from us would spend an attempt that is no longer
    ours and could re-queue a payload that is already executing elsewhere."""

# Cadence knobs. Heartbeats are much more frequent than the stale window so a
# slow-but-alive handler is never mistaken for a dead worker.
_HEARTBEAT_S = 10.0
_RECLAIM_EVERY_S = 30.0
# An in-flight scheduler tick renews its lease this often (bounded below by a
# third of the lease TTL, so a renewal always lands well before expiry), and
# stop() waits at most this long for one to finish before giving up on it.
# Giving up does NOT release the lease — see Worker.stop().
_SCHED_RENEW_S = 10.0
_SCHED_STOP_WAIT_S = 5.0


def worker_mode():
    """'thread' | 'external' | 'off' — see the module docstring."""
    mode = (os.getenv("STUDIO_WORKER_MODE") or "thread").strip().lower()
    return mode if mode in ("thread", "external", "off") else "thread"


def stale_after_s():
    return float(os.getenv("STUDIO_JOB_STALE_S") or 300)


# ── Schema ───────────────────────────────────────────────────────────────

def init_tables():
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS background_jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                run_after REAL NOT NULL,
                locked_by TEXT,
                locked_at REAL,
                heartbeat_at REAL,
                result TEXT,
                error TEXT,
                user_id TEXT,
                created_at REAL NOT NULL,
                finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_background_jobs_due
                ON background_jobs(status, run_after);
            CREATE TABLE IF NOT EXISTS scheduler_leases (
                name TEXT PRIMARY KEY,
                holder TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        c.commit()


# ── Handlers ─────────────────────────────────────────────────────────────

def handler(kind):
    """Register fn(payload: dict, job: dict) -> jsonable result for a kind.
    Registration happens at import, so a process that runs jobs must import
    the modules that own them (worker.py does; main.py does implicitly)."""
    def _register(fn):
        _HANDLERS[kind] = fn
        return fn
    return _register


def handlers():
    return dict(_HANDLERS)


def reconciler(fn):
    """Register fn() to run after every reclaim pass.

    reclaim_stale() repairs the QUEUE — a running job whose worker died goes
    back on it. A reconciler repairs the rows that POINT AT the queue, which
    the queue cannot see: chat_tasks.status='running' with no job behind it is
    a task nothing will ever run and nothing will ever fail, so the UI spins on
    it forever. Enqueue is atomic now (chat.ask_background writes the task row
    and its job in one transaction), so no NEW orphan can be created — this is
    the net that heals the ones an older build already left behind, and any
    other way a job can vanish from under a task.

    Callbacks run OUTSIDE reclaim_stale's transaction and every exception is
    swallowed: a broken reconciler must never stop the queue from healing."""
    _RECONCILERS[f"{fn.__module__}.{fn.__qualname__}"] = fn
    return fn


def run_reconcilers():
    """Run every registered reconciler, isolating failures. Returns the number
    that raised."""
    failed = 0
    for key, fn in list(_RECONCILERS.items()):
        try:
            fn()
        except Exception:
            failed += 1
            log.exception("jobs: reconciler %s failed", key)
    return failed


# ── Cooperative abort ────────────────────────────────────────────────────

def abort_event():
    """The Event that is SET when the work running on THIS thread has lost its
    claim (a reclaimed job) or its scheduler lease. None outside a job/tick —
    a synchronous request, a test calling a handler directly — which is why
    every caller must treat None as 'still ours'."""
    return getattr(_LOCAL, "abort", None)


def claim_lost():
    """True once this thread's claim/lease is gone. Cheap: poll it freely."""
    ev = abort_event()
    return ev is not None and ev.is_set()


def check_claim():
    """Raise ClaimLost when this thread no longer owns its work.

    Call it at SAFE POINTS — immediately before any write that must not happen
    twice — so the abandonment costs nothing but the wasted compute. It is a
    cooperative check, not a guarantee: a handler that never calls it runs to
    completion regardless (Python cannot preempt a thread), so a
    must-happen-once effect still needs a database constraint."""
    if claim_lost():
        raise ClaimLost("the claim on this job was reclaimed while it was running")


# ── Queue operations ─────────────────────────────────────────────────────

def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("payload", "result"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except (TypeError, ValueError):
            d[k] = None
    return d


def enqueue(kind, payload, *, user_id=None, run_after=None, max_attempts=2,
            job_id=None, conn=None):
    """Append a job and return its id. run_after (epoch seconds) delays it;
    job_id lets a caller make the enqueue idempotent with its own key.

    `conn` ENLISTS the insert in a transaction the caller already has open:
    the row is written on that connection and NOT committed here, so it lands
    (or rolls back) atomically with the caller's own rows. That is what makes
    a queue job and the application row that tracks it all-or-nothing — see
    chat.ask_background, where a separate commit per row could leave a task
    marked 'running' with no job to run it. Omitting `conn` keeps the old
    behaviour exactly: our own connection, our own commit."""
    if kind not in _HANDLERS:
        # Not fatal — the handler may live in the worker process — but worth
        # a log line, since a typo here would sit on the queue forever.
        log.info("jobs: enqueue kind=%s has no handler in this process", kind)
    jid = job_id or str(uuid.uuid4())
    now = time.time()
    sql = ("INSERT INTO background_jobs (id, kind, payload, status, attempts, max_attempts, "
           "run_after, user_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)")
    params = (jid, kind, json.dumps(payload or {}), "queued", 0, int(max_attempts),
              float(run_after if run_after is not None else now), user_id, now)
    if conn is not None:
        # The caller owns the transaction: no commit, no rollback, no close.
        conn.execute(sql, params)
        return jid
    with db.connect() as c:
        c.execute(sql, params)
        c.commit()
    return jid


def get(job_id):
    with db.connect() as c:
        r = c.execute("SELECT * FROM background_jobs WHERE id=?", (job_id,)).fetchone()
    return _row(r)


def claim(worker_id, kinds=None):
    """Atomically move the oldest due queued job to 'running' for this
    worker, or return None. One UPDATE keyed by a fresh token: whichever
    worker's UPDATE lands first flips the row, and the loser's UPDATE matches
    nothing because the `AND status='queued'` guard no longer holds.

    The returned row's `locked_by` IS the claim token — fresh per claim, never
    reused. Pass it to heartbeat()/complete()/fail() for this job; it is the
    only proof that we still own the row."""
    token = f"{worker_id}#{uuid.uuid4().hex}"
    now = time.time()
    params = [token, now, now, now]
    kind_sql = ""
    if kinds:
        kinds = list(kinds)
        kind_sql = " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
        params.extend(kinds)
    lock = " FOR UPDATE SKIP LOCKED" if db.IS_PG else ""
    with db.connect() as c:
        c.execute(
            "UPDATE background_jobs SET status='running', locked_by=?, locked_at=?, "
            "heartbeat_at=?, attempts=attempts+1 WHERE id = ("
            "SELECT id FROM background_jobs WHERE status='queued' AND run_after<=?"
            + kind_sql + " ORDER BY run_after, created_at LIMIT 1" + lock + ") "
            "AND status='queued'",
            params)
        c.commit()
        r = c.execute("SELECT * FROM background_jobs WHERE locked_by=?", (token,)).fetchone()
    return _row(r)


def _matched(cur):
    """Did a guarded UPDATE hit a row? Both drivers report rowcount for an
    UPDATE; an unknown (-1/None) is read as 'matched', so a driver that does
    not report it can never make a worker abandon a job it really owns."""
    n = getattr(cur, "rowcount", None)
    return True if n is None or n < 0 else n > 0


def heartbeat(job_id, token):
    """Renew the liveness stamp for a job we hold. Returns False when the
    claim is GONE — reclaimed (locked_by cleared or replaced) or already
    finished — and the caller must then stop working on the job: someone else
    owns it now, and every further write of ours would corrupt their run."""
    with db.connect() as c:
        cur = c.execute(
            "UPDATE background_jobs SET heartbeat_at=? "
            "WHERE id=? AND status='running' AND locked_by=?",
            (time.time(), job_id, token))
        c.commit()
    return _matched(cur)


def complete(job_id, token, result=None):
    """Record success, fenced on the claim token. Returns False — writing
    nothing at all — when the job was reclaimed while we ran, so a stale
    worker can never finish a job another worker is executing."""
    with db.connect() as c:
        cur = c.execute(
            "UPDATE background_jobs SET status='done', result=?, error=NULL, finished_at=?, "
            "locked_by=NULL WHERE id=? AND locked_by=?",
            (json.dumps(result) if result is not None else None, time.time(), job_id, token))
        c.commit()
    return _matched(cur)


def _backoff(attempts):
    """Seconds before a retry: 5s, 10s, 20s ... capped at five minutes."""
    return min(300.0, 5.0 * (2 ** max(0, attempts - 1)))


def fail(job_id, token, error, retry=True):
    """Record a failure, fenced on the claim token. retry=True re-queues with
    backoff while attempts remain; otherwise (or when out of attempts) the job
    is 'failed'. The error text is kept either way so a retried job shows its
    history.

    Returns the new status, or None when the row is gone or our token no
    longer matches. A stale worker MUST NOT land here: re-queuing a job that
    another worker is already running would run its payload a third time, and
    the attempt it burned is not ours to spend."""
    error = (str(error) if error is not None else "")[:2000]
    now = time.time()
    with db.connect() as c:
        # The token is on the SELECT too, so a stale worker reads nothing and
        # leaves attempts/status exactly as the new owner set them.
        r = c.execute("SELECT attempts, max_attempts FROM background_jobs "
                      "WHERE id=? AND locked_by=?", (job_id, token)).fetchone()
        if r is None:
            return None
        attempts, max_attempts = int(r["attempts"]), int(r["max_attempts"])
        if retry and attempts < max_attempts:
            status = "queued"
            cur = c.execute(
                "UPDATE background_jobs SET status='queued', error=?, run_after=?, "
                "locked_by=NULL, locked_at=NULL, heartbeat_at=NULL "
                "WHERE id=? AND locked_by=?",
                (error, now + _backoff(attempts), job_id, token))
        else:
            status = "failed"
            cur = c.execute(
                "UPDATE background_jobs SET status='failed', error=?, finished_at=?, "
                "locked_by=NULL WHERE id=? AND locked_by=?",
                (error, now, job_id, token))
        c.commit()
    return status if _matched(cur) else None


def reclaim_stale(stale_after=None):
    """Running jobs whose heartbeat stopped — their worker died or was
    restarted — go back to the queue, or to 'failed' when out of attempts.
    This is what makes a restart safe. Returns (requeued, failed) counts.

    It is also where reconciler() callbacks run: the queue is only half the
    picture, and a chat_tasks row left 'running' with no job behind it has
    nothing here to reclaim. Reconcilers run after (and outside) our
    transaction, and their failures are logged, never raised.

    Both UPDATEs CLEAR locked_by, and that is load-bearing, not tidiness: it
    invalidates the old owner's claim token. A worker that was merely slow
    (not dead) and comes back to complete/fail its job finds `locked_by = ?`
    matching nothing, so it cannot overwrite the outcome of the re-run. Any
    future change here must keep locked_by moving to a value the previous
    holder cannot present — a fresh token or NULL, never the old one."""
    stale_after = stale_after_s() if stale_after is None else float(stale_after)
    cutoff = time.time() - stale_after
    with db.connect() as c:
        cur = c.execute(
            "UPDATE background_jobs SET status='queued', locked_by=NULL, locked_at=NULL, "
            "heartbeat_at=NULL, error=? WHERE status='running' "
            "AND COALESCE(heartbeat_at, locked_at, 0) < ? AND attempts < max_attempts",
            ("stale: worker stopped heartbeating", cutoff))
        requeued = cur.rowcount if cur.rowcount is not None else 0
        cur = c.execute(
            "UPDATE background_jobs SET status='failed', locked_by=NULL, finished_at=?, "
            "error=? WHERE status='running' AND COALESCE(heartbeat_at, locked_at, 0) < ? "
            "AND attempts >= max_attempts",
            (time.time(), "stale: worker stopped heartbeating and no attempts remain", cutoff))
        failed = cur.rowcount if cur.rowcount is not None else 0
        c.commit()
    if requeued or failed:
        log.warning("jobs: reclaimed stale jobs: requeued=%s failed=%s", requeued, failed)
    run_reconcilers()
    return requeued, failed


def stats():
    with db.connect() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM background_jobs GROUP BY status").fetchall()
    out = {s: 0 for s in STATUSES}
    for r in rows:
        out[r["status"]] = int(r["n"])
    return out


def run_one(worker_id, kinds=None):
    """Claim one due job and run it to completion in the calling thread.
    Returns False when nothing was due. See _execute() for the run itself."""
    job = claim(worker_id, kinds)
    if job is None:
        return False
    _execute(job)
    return True


# ── Scheduler leases ─────────────────────────────────────────────────────

def acquire_lease(name, holder, ttl_s):
    """Take (or renew) the named lease for ttl_s seconds. Atomic on both
    dialects: the upsert only overwrites an EXPIRED lease or one this same
    holder already owns, and the read-back confirms who won."""
    now = time.time()
    expires = now + float(ttl_s)
    with db.connect() as c:
        c.execute(
            "INSERT INTO scheduler_leases (name, holder, expires_at) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, "
            "expires_at=excluded.expires_at "
            "WHERE scheduler_leases.expires_at < ? OR scheduler_leases.holder = excluded.holder",
            (name, holder, expires, now))
        c.commit()
        r = c.execute("SELECT holder, expires_at FROM scheduler_leases WHERE name=?",
                      (name,)).fetchone()
    return bool(r and r["holder"] == holder and float(r["expires_at"]) >= expires - 1e-6)


def release_lease(name, holder):
    with db.connect() as c:
        c.execute("DELETE FROM scheduler_leases WHERE name=? AND holder=?", (name, holder))
        c.commit()


# ── Worker ───────────────────────────────────────────────────────────────

def default_worker_id(prefix="worker"):
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


# Floor for a scheduler lease. The bound that matters: an in-flight tick
# renews every _SCHED_RENEW_S (10s), so the lease only expires after
# _SCHED_LEASE_FLOOR_S / _SCHED_RENEW_S = 12 consecutive renewals fail — by
# which point the holder really is gone. It also has to comfortably exceed
# _SCHED_STOP_WAIT_S (5s), because a stop() that gives up on a stuck tick
# leaves the lease to expire rather than releasing it.
_SCHED_LEASE_FLOOR_S = 120.0


def _lease_ttl(s):
    """A scheduler's lease TTL: four full intervals, never under two minutes.

    Two things need covering. A tick SLOWER than its cadence keeps the lease
    by renewing under itself, so the TTL is not what protects it. What the TTL
    bounds is failover: how long a DEAD holder's lease blocks every other
    replica, and how long a lease abandoned by a stuck tick lingers. Two
    minutes is the floor because it is ~12 renewal periods — far more than any
    transient blip — and well past stop()'s five-second grace."""
    return max(4 * s["interval_s"], _SCHED_LEASE_FLOOR_S)


def _default_schedulers():
    """The periodic tickers, each with its own lease and cadence. Imported
    lazily: chat/autopilot import this module for enqueue/handler, so a
    module-level import here would be a cycle. `enabled` is read on every
    tick so the STUDIO_*_TICKER kill-switches keep their meaning: a disabled
    ticker means the scheduler skips that lease entirely."""
    from . import autopilot
    from .extraction import sync as m365_sync
    return [
        {"name": "autopilot", "fn": autopilot.tick_once,
         "enabled": autopilot.ticker_enabled, "interval_s": autopilot._TICK_SECONDS},
        {"name": "m365_sync", "fn": m365_sync.tick_once,
         "enabled": m365_sync.ticker_enabled, "interval_s": m365_sync._TICK_SECONDS},
    ]


class Worker:
    """Claims and runs jobs on a bounded thread pool, reclaims stale jobs
    every ~30s, and runs each lease-guarded scheduler on its own cadence.
    start()/stop() are idempotent; stop() waits for in-flight handlers.

    The poll loop thread only DISPATCHES: a scheduler tick runs on its own
    small executor, never inline. A tick is arbitrarily slow (an autopilot
    pass queries every watched source; an M365 sync walks a mailbox), and
    inline it stalled job dispatch for its whole duration, outlived the lease
    it took, and was abandoned mid-run by stop()."""

    def __init__(self, worker_id=None, concurrency=None, poll_s=None, kinds=None,
                 schedulers=None):
        self.worker_id = worker_id or default_worker_id()
        self.concurrency = int(concurrency or os.getenv("STUDIO_JOB_WORKERS") or 4)
        self.poll_s = float(poll_s or os.getenv("STUDIO_JOB_POLL_S") or 1.0)
        self.kinds = list(kinds) if kinds else None
        self.schedulers = schedulers   # None → _default_schedulers() at start()
        self._stop = threading.Event()
        self._thread = None
        self._pool = None
        self._inflight = 0
        self._lock = threading.Lock()
        self._next_sched = {}
        self._next_reclaim = 0.0
        self._sched_pool = None       # ticks only — never shares the job slots
        self._sched_inflight = {}     # name -> Future of the tick still running

    # -- lifecycle --

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        if self.schedulers is None:
            self.schedulers = _default_schedulers()
        # First scheduler pass after one full interval, as the old daemon
        # tickers did — startup never fires a tick.
        now = time.time()
        self._next_sched = {s["name"]: now + s["interval_s"] for s in self.schedulers}
        self._next_reclaim = now
        self._stop.clear()
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.concurrency, thread_name_prefix="job")
        # One slot per scheduler: a slow autopilot tick must not delay the
        # M365 tick either, and a second tick of the SAME scheduler is
        # refused by run_schedulers rather than queued behind the first.
        self._sched_inflight = {}
        self._sched_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(self.schedulers)), thread_name_prefix="sched")
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name=f"job-worker-{self.worker_id}")
        self._thread.start()
        log.info("jobs: worker %s started (concurrency=%s, poll=%ss)",
                 self.worker_id, self.concurrency, self.poll_s)
        return self

    def stop(self, wait=True):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.poll_s * 2))
        # Give a tick that is still running a bounded moment to finish BEFORE
        # the leases go: releasing the lease under a live tick is what would
        # let another replica start a second copy of the same pass.
        pending = {n: f for n, f in self._sched_inflight.items() if not f.done()}
        if pending:
            concurrent.futures.wait(list(pending.values()), timeout=_SCHED_STOP_WAIT_S)
        # Whatever is STILL running after the grace period keeps its lease.
        # The old code released it anyway, and that is exactly the overlap the
        # lease exists to prevent: our tick is still executing (we cannot kill
        # it — daemon threads are not preemptible), so handing the name to
        # another replica starts a second copy of the same pass alongside it.
        # Leaving it alone costs at most _lease_ttl seconds of no ticking,
        # which is what the TTL is for; releasing it costs correctness.
        stuck = {n for n, f in pending.items() if not f.done()}
        if self._sched_pool:
            self._sched_pool.shutdown(wait=False)
            self._sched_pool = None
        self._sched_inflight = {}
        if self._pool:
            self._pool.shutdown(wait=wait)
            self._pool = None
        for s in self.schedulers or []:
            if s["name"] in stuck:
                log.warning(
                    "jobs: worker %s is stopping while the %s tick is still running — "
                    "leaving its lease to expire (in up to %.0fs) rather than handing "
                    "the scheduler to another worker mid-pass",
                    self.worker_id, s["name"], _lease_ttl(s))
                continue
            try:
                release_lease(s["name"], self.worker_id)
            except Exception:
                pass
        log.info("jobs: worker %s stopped", self.worker_id)

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # -- the loop --

    def _loop(self):
        while not self._stop.is_set():
            try:
                busy = self._tick()
            except Exception:
                log.exception("jobs: worker loop error")
                busy = False
            if not busy:
                self._stop.wait(self.poll_s)

    def _tick(self):
        """One pass: reclaim, schedulers, then fill free pool slots with due
        jobs. Returns True when a job was dispatched (poll again at once)."""
        now = time.time()
        if now >= self._next_reclaim:
            self._next_reclaim = now + _RECLAIM_EVERY_S
            try:
                reclaim_stale()
            except Exception:
                log.exception("jobs: reclaim_stale failed")
        self.run_schedulers(now)
        dispatched = False
        while not self._stop.is_set():
            with self._lock:
                if self._inflight >= self.concurrency:
                    break
                self._inflight += 1
            job = None
            try:
                job = claim(self.worker_id, self.kinds)
            except Exception:
                log.exception("jobs: claim failed")
            if job is None:
                with self._lock:
                    self._inflight -= 1
                break
            self._pool.submit(self._run_claimed, job)
            dispatched = True
        return dispatched

    def _run_claimed(self, job):
        try:
            _execute(job)
        finally:
            with self._lock:
                self._inflight -= 1

    def run_schedulers(self, now=None):
        """Every scheduler whose cadence is due: take its lease (renewing
        our own), start one tick, and skip silently when another replica
        holds it. The lease outlives two intervals so a slow tick never lets
        a second holder in mid-run, yet a dead holder is replaced quickly.

        The gating (enabled → lease) stays on the caller's thread — it is two
        fast queries — and only the tick itself is handed to the scheduler
        executor. A scheduler whose previous tick has not finished is skipped
        entirely: it keeps its lease, so nobody else ticks it either, and the
        ticks never pile up behind a slow one.
        """
        now = time.time() if now is None else now
        for s in self.schedulers or []:
            if now < self._next_sched.get(s["name"], 0):
                continue
            self._next_sched[s["name"]] = now + s["interval_s"]
            fut = self._sched_inflight.get(s["name"])
            if fut is not None and not fut.done():
                log.info("jobs: scheduler %s is still running; skipping this tick",
                         s["name"])
                continue
            try:
                if not s["enabled"]():
                    continue
                if not acquire_lease(s["name"], self.worker_id, _lease_ttl(s)):
                    continue
            except Exception:
                log.exception("jobs: scheduler %s could not be gated", s["name"])
                continue
            if self._sched_pool is None:
                # Not started (a direct call, or a test driving the cadence):
                # run it here, so tick_once() semantics are identical.
                self._run_scheduled_tick(s)
            else:
                self._sched_inflight[s["name"]] = self._sched_pool.submit(
                    self._run_scheduled_tick, s)

    def _run_scheduled_tick(self, s):
        """One tick, with the lease RENEWED for as long as it runs. Without
        the renewal a tick slower than its TTL lost the lease under itself and
        a second replica started the same pass alongside it.

        A renewal that does not come back holding the lease is treated as LOSS
        of the lease, not as noise to log and move past: we stop renewing (a
        later renewal would steal the name back from whoever holds it now) and
        set the tick's abort Event, so a tick that checks jobs.check_claim()
        at its safe points stops instead of running a second pass alongside
        the new holder. A transient database blip therefore costs one tick —
        the safe direction, and the tick simply runs again next interval."""
        ttl = _lease_ttl(s)
        stop = threading.Event()
        lost = threading.Event()

        def _renew():
            while not stop.wait(min(_SCHED_RENEW_S, max(1.0, ttl / 3.0))):
                try:
                    held = acquire_lease(s["name"], self.worker_id, ttl)
                except Exception:
                    log.exception("jobs: scheduler %s lease renewal failed", s["name"])
                    held = False
                if not held:
                    lost.set()
                    log.warning(
                        "jobs: scheduler %s lost its lease mid-tick — the tick has been "
                        "signalled to abandon and will not be renewed again", s["name"])
                    return

        threading.Thread(target=_renew, daemon=True,
                         name=f"sched-lease-{s['name']}").start()
        prev = getattr(_LOCAL, "abort", None)
        _LOCAL.abort = lost
        try:
            s["fn"]()
        except ClaimLost:
            log.warning("jobs: scheduler %s abandoned its tick after losing the lease",
                        s["name"])
        except Exception:
            log.exception("jobs: scheduler %s failed", s["name"])
        finally:
            stop.set()
            _LOCAL.abort = prev


def _execute(job):
    """run_one()'s body for an already-claimed job (the Worker claims in its
    loop thread so it can bound concurrency, then runs here on the pool).

    Every write carries the claim token claim() put in job["locked_by"]. A
    write that matches nothing means reclaim_stale() handed this job to
    another worker while we ran: we ABANDON it — one log line, no completion,
    no failure, no retry — and leave the row to its new owner.

    The fence keeps a stale worker off the ROW, but the handler is still
    executing and its side effects are not fenced by anything. So the moment
    a heartbeat is refused we also set an abort Event — published as
    job["abort"] and through claim_lost()/check_claim() for this thread — and
    a handler that checks it raises ClaimLost and is abandoned before writing.
    A handler that does not check it cannot be stopped (Python has no thread
    preemption); its at-most-once effects need a database constraint."""
    token = job.get("locked_by")
    fn = _HANDLERS.get(job["kind"])
    if fn is None:
        fail(job["id"], token, f"no handler registered for kind '{job['kind']}'", retry=False)
        log.error("jobs: %s kind=%s has no handler", job["id"], job["kind"])
        return
    stop = threading.Event()
    abort = threading.Event()

    def _beat():
        while not stop.wait(_HEARTBEAT_S):
            try:
                alive = heartbeat(job["id"], token)
            except Exception:
                continue          # a transient DB blip is not a lost claim
            if not alive:
                # The first notice that our claim is gone. Stop beating at
                # once: another worker owns the row now, and a heartbeat from
                # us would keep ITS run looking alive under our stale stamp.
                # Tell the handler too — the row is already lost, and every
                # side effect it has left to write would be a duplicate of the
                # new owner's.
                stop.set()
                abort.set()
                log.warning("jobs: %s claim lost (reclaimed by another worker); "
                            "heartbeat stopped and the handler signalled to abandon",
                            job["id"])
                return

    threading.Thread(target=_beat, daemon=True,
                     name=f"job-heartbeat-{job['id'][:8]}").start()
    t0 = time.time()
    log.info("jobs: start %s kind=%s attempt=%s/%s", job["id"], job["kind"],
             job["attempts"], job["max_attempts"])
    job = dict(job)
    job["abort"] = abort     # the handler's copy of the cooperative signal
    prev_abort = getattr(_LOCAL, "abort", None)
    _LOCAL.abort = abort
    try:
        result = fn(job["payload"] or {}, job)
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = {"repr": repr(result)[:2000]}
        if complete(job["id"], token, result):
            log.info("jobs: done %s kind=%s in %.1fs", job["id"], job["kind"],
                     time.time() - t0)
        else:
            log.warning("jobs: %s kind=%s finished in %.1fs but its claim was gone — "
                        "abandoned to the worker that reclaimed it, result discarded",
                        job["id"], job["kind"], time.time() - t0)
    except ClaimLost as e:
        # The handler stopped itself. Deliberately NOT fail(): the attempt it
        # would burn belongs to the worker that owns the row now, and a
        # re-queue would run a payload that is already executing.
        log.warning("jobs: %s kind=%s abandoned after %.1fs — the handler noticed its "
                    "claim was gone and wrote nothing: %s",
                    job["id"], job["kind"], time.time() - t0, e)
    except Exception as e:
        status = fail(job["id"], token, f"{type(e).__name__}: {e}", retry=True)
        if status is None:
            log.warning("jobs: %s kind=%s raised in %.1fs but its claim was gone — "
                        "abandoned, not retried: %s",
                        job["id"], job["kind"], time.time() - t0, e)
        else:
            log.warning("jobs: %s %s kind=%s after %.1fs: %s", status, job["id"], job["kind"],
                        time.time() - t0, e)
    finally:
        stop.set()
        _LOCAL.abort = prev_abort
