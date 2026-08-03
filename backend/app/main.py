"""Studio backend — FastAPI app."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before any module reads env vars (keys, SMTP, models).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import auth, catalog, chat, db
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

# In production the built frontend calls the API at /api/* (the Vite dev
# server proxies and strips that prefix, so dev keeps the unprefixed routes).
for _router in (auth.router, catalog.router, chat.router):
    app.include_router(_router, prefix="/api", include_in_schema=False)


@app.on_event("startup")
def startup():
    db.init_db()
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
    return {
        "status": "ok",
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
