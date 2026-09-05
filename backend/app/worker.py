"""The job worker process: `python -m app.worker`.

Runs the durable queue (jobs.py) OUTSIDE the web process: background chat
turns and the lease-guarded autopilot / M365 tickers. Deploy it as a second
service from the same image with this as the start command and set
STUDIO_WORKER_MODE=external on the web service, which then only enqueues.
Several workers may run side by side — claims are atomic and the schedulers
are single-instance by lease — and a worker may be restarted at any time: a
job it was running stops heartbeating and another worker reclaims it.

Shutdown is the mirror image: SIGTERM/SIGINT stops the Worker (which waits
for in-flight handlers and any running scheduler tick), then closes the
Postgres pool, so the process exits promptly instead of waiting on the pool's
own threads.

Startup mirrors the web process exactly (same .env, same init_state(), same
migrations policy) so the two never disagree about the schema, and it imports
main so every @jobs.handler the app registers at import time is registered
here too. It starts NO web server and NO in-process Worker of main's own —
init_state() deliberately stops before that.
"""
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

# Same env file the web process loads, before any module reads env vars.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger("studio.worker")


def main(argv=None):
    logging.basicConfig(
        level=os.getenv("STUDIO_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Importing main registers every handler (chat's chat_turn, ...) and gives
    # us init_state(); it never starts anything by itself.
    from . import db, jobs
    from . import main as web
    web.init_state()
    if not jobs.handlers():
        log.error("worker: no job handlers registered — nothing would ever run")
        return 2
    worker = jobs.Worker(worker_id=jobs.default_worker_id("worker"))
    stop = threading.Event()

    def _signal(signum, _frame):
        log.info("worker: received %s, stopping", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _signal)
    worker.start()
    log.info("worker: %s running handlers=%s mode=%s",
             worker.worker_id, sorted(jobs.handlers()), jobs.worker_mode())
    try:
        while not stop.is_set():
            # Short waits keep the main thread responsive to signals on every
            # platform (a bare Event.wait() can block SIGINT on some).
            stop.wait(1.0)
            if not worker.running:
                log.error("worker: loop thread died; restarting it")
                worker.start()
    finally:
        worker.stop()
        # Let in-flight handlers' last DB writes land before the process exits.
        time.sleep(0.1)
        # Return the Postgres pool's sessions now. The pool keeps non-daemon
        # maintenance threads, so without this the process lingered ~20s after
        # SIGTERM waiting for them (and left server-side sessions to time out).
        # The web process does the same in main.py's shutdown.
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(main())
