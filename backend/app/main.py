"""Studio backend — FastAPI app."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before any module reads env vars (keys, SMTP, models).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import (auth, autopilot, catalog, chat, dashboards, db, flow, freshness,
               governance, jobs, kag, keys, mcp, migrations, pipelines, pybuild,
               qcache, queries, redteam, repos, semantic, sessions, supervisor,
               toolbuilder, trainer)
from .agent import llm_available, llm_spec
from .connectors import objectstore
from .connectors.demo import seed
from .extraction import routes as m365, sync as m365_sync

app = FastAPI(title="Studio", version="0.1.0")

_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
if os.getenv("FRONTEND_URL"):
    _origins.append(os.environ["FRONTEND_URL"].rstrip("/"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every route lives under /api — exactly once. The Vite dev proxy forwards
# /api/* unchanged and the built frontend calls /api/* directly, so there is a
# single URL per endpoint in dev, prod and /docs. Anything in front of the app
# (reverse proxies, auth gateways) only needs to protect the /api prefix.
_ROUTERS = (auth.router, catalog.router, chat.router, dashboards.router,
            queries.router, pipelines.router, governance.router,
            supervisor.router, mcp.router, pybuild.router, toolbuilder.router,
            kag.router, repos.router, repos._settings, sessions.router,
            flow.router, trainer.router, freshness.router, objectstore.router,
            semantic.router, autopilot.router, redteam.router, m365.router)
for _router in _ROUTERS:
    app.include_router(_router, prefix="/api")


def init_state():
    """Bring the state store up: base tables, boot invariants, every module's
    init_tables(), demo seed, then migrations. Shared by the web process
    (startup()) and the job worker (`python -m app.worker`) so both see the
    same complete schema; it starts NO threads, tickers or workers — that is
    the caller's decision, per STUDIO_WORKER_MODE. Idempotent: every step is
    CREATE IF NOT EXISTS / upsert / versioned, so N processes may run it."""
    db.init_db()
    # Boot-time invariants (secrets, demo-mode guard, ...). Imported lazily so a
    # circular import can never break app import; a RuntimeError from enforce()
    # is deliberately NOT caught — refusing to boot is the point.
    from . import bootstrap
    bootstrap.enforce()
    chat.init_tables()
    # Expire stored result rows past STUDIO_MESSAGE_ROWS_RETENTION_DAYS (0 = never); a scheduler will own this later.
    chat.purge_message_rows()
    dashboards.init_tables()
    keys.init_tables()
    queries.init_tables()
    pipelines.init_tables()
    governance.init_tables()
    governance.load()
    semantic.init_tables()
    semantic.load()
    supervisor.init_tables()
    mcp.init_tables()
    toolbuilder.init_tables()
    kag.init_tables()
    repos.init_tables()
    sessions.init_tables()
    flow.init_tables()
    qcache.init_tables()
    trainer.init_tables()
    redteam.init_tables()
    objectstore.init_tables()
    autopilot.init_tables()
    # Microsoft 365 / Graph extraction tables (graph_accounts / _subscriptions /
    # _items). CREATE-only DDL, safe on SQLite and Postgres; inert when dormant.
    m365_sync.init_tables()
    # The durable job queue + scheduler leases (background chat turns, tickers).
    jobs.init_tables()
    seed()
    # Schema migrations run AFTER the last init_tables(): the CREATEs above
    # are the complete baseline for a fresh database, and migrations only
    # bring an older one up to it. STUDIO_AUTO_MIGRATE=0 makes this a check
    # that raises (refusing to boot) when anything is pending — see
    # migrations.run_startup().
    migrations.run_startup()


# The in-process job worker, when STUDIO_WORKER_MODE=thread (the default): it
# runs background chat turns and the lease-guarded autopilot / M365 tickers
# exactly as the old thread pool and daemon tickers did, but from the durable
# queue, so a restart resumes rather than drops them. "external" leaves the
# queue to `python -m app.worker`; "off" runs nothing (tests).
_worker = None


@app.on_event("startup")
def startup():
    global _worker
    init_state()
    if jobs.worker_mode() == "thread":
        _worker = jobs.Worker(worker_id=jobs.default_worker_id("web")).start()


@app.on_event("shutdown")
def shutdown():
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None
    # Close pooled warehouse connections cleanly.
    from .connectors import _REGISTRY
    for conn in _REGISTRY.values():
        if hasattr(conn, "close"):
            try:
                conn.close()
            except Exception:
                pass
    db.close_pool()


# /health stays unprefixed for the Railway healthcheck; /api/health is the
# canonical path the frontend and proxies use.
@app.get("/health", include_in_schema=False)
@app.get("/api/health")
def health():
    from . import dashboards as _dash
    return {
        "status": "ok",
        "store": "postgres" if db.IS_PG else f"sqlite ({db.DB_PATH})",
        # None on SQLite / before the first Postgres borrow; otherwise the
        # psycopg_pool gauges (pool_size, pool_available, requests_waiting…).
        "db_pool": db.pool_stats(),
        "tile_cache": "redis" if _dash._redis() is not None else "in-process",
        "llm": llm_spec(),
        "agent": "ready" if llm_available() else "fallback (no API key)",
        "mcp_servers": list(__import__("app.agent", fromlist=["mcp_servers"]).mcp_servers().keys()),
        "agent_lightning": __import__("app.lightning", fromlist=["agl_available"]).agl_available(),
    }


# Serve the built frontend (single-service deploys, e.g. Railway). The
# frontend is a React Router app (/c/<id>, /dashboards/<id>, /jobs, ...), so
# every non-API, non-file path must return index.html and let the router
# take over. Declared LAST — after the router include loop and /health — so
# /api, /docs and /openapi.json always win. Absent in dev, where Vite serves
# the frontend.
_static = Path(os.getenv("STUDIO_STATIC_DIR", Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"))
if _static.is_dir():
    _static_root = _static.resolve()
    if (_static / "assets").is_dir():
        # Hashed bundles as a real static mount, so they keep proper caching.
        app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        # A missing API route is a 404, never the SPA shell: the frontend and
        # proxies must see JSON for /api/*.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not Found")
        if path:
            # A real file (favicon, manifest, ...) strictly inside dist/ —
            # resolve() collapses any traversal before the containment check.
            try:
                target = (_static / path).resolve()
            except (OSError, ValueError):
                target = None
            if target is not None and target.is_file() and target.is_relative_to(_static_root):
                return FileResponse(target)
        return FileResponse(_static / "index.html")
