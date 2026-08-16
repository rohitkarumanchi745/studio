"""Studio backend — FastAPI app."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before any module reads env vars (keys, SMTP, models).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import (auth, catalog, chat, dashboards, db, governance, keys, mcp, pipelines,
               pybuild, queries, repos, sessions, supervisor)
from .agent import llm_available, llm_spec
from .connectors.demo import seed

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

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(chat.router)
app.include_router(dashboards.router)
app.include_router(queries.router)
app.include_router(pipelines.router)
app.include_router(governance.router)
app.include_router(supervisor.router)
app.include_router(mcp.router)
app.include_router(pybuild.router)
app.include_router(repos.router)
app.include_router(repos._settings)
app.include_router(sessions.router)

# In production the built frontend calls the API at /api/* (the Vite dev
# server proxies and strips that prefix, so dev keeps the unprefixed routes).
for _router in (auth.router, catalog.router, chat.router, dashboards.router,
                queries.router, pipelines.router, governance.router,
                supervisor.router, mcp.router, pybuild.router,
                repos.router, repos._settings, sessions.router):
    app.include_router(_router, prefix="/api", include_in_schema=False)


@app.on_event("startup")
def startup():
    db.init_db()
    dashboards.init_tables()
    keys.init_tables()
    queries.init_tables()
    pipelines.init_tables()
    governance.init_tables()
    governance.load()
    supervisor.init_tables()
    mcp.init_tables()
    repos.init_tables()
    sessions.init_tables()
    seed()


@app.on_event("shutdown")
def shutdown():
    # Close pooled warehouse connections cleanly.
    from .connectors import _REGISTRY
    for conn in _REGISTRY.values():
        if hasattr(conn, "close"):
            try:
                conn.close()
            except Exception:
                pass


@app.get("/health")
@app.get("/api/health", include_in_schema=False)
def health():
    from . import dashboards as _dash
    return {
        "status": "ok",
        "store": "postgres" if db.IS_PG else f"sqlite ({db.DB_PATH})",
        "tile_cache": "redis" if _dash._redis() is not None else "in-process",
        "llm": llm_spec(),
        "agent": "ready" if llm_available() else "fallback (no API key)",
        "mcp_servers": list(__import__("app.agent", fromlist=["mcp_servers"]).mcp_servers().keys()),
        "agent_lightning": __import__("app.lightning", fromlist=["agl_available"]).agl_available(),
    }


# Serve the built frontend (single-service deploys, e.g. Railway). Mounted
# last so API routes win; absent in dev, where Vite serves the frontend.
_static = Path(os.getenv("STUDIO_STATIC_DIR", Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"))
if _static.is_dir():
    app.mount("/", StaticFiles(directory=_static, html=True), name="frontend")
