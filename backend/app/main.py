"""Studio backend — FastAPI app."""
from pathlib import Path

from dotenv import load_dotenv

# Load backend/.env before any module reads env vars (keys, SMTP, models).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import auth, catalog, chat, db
from .agent import llm_available, llm_spec
from .connectors.demo import seed

app = FastAPI(title="Studio", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(chat.router)


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
def health():
    return {
        "status": "ok",
        "llm": llm_spec(),
        "agent": "ready" if llm_available() else "fallback (no API key)",
        "mcp_servers": list(__import__("app.agent", fromlist=["mcp_servers"]).mcp_servers().keys()),
        "agent_lightning": __import__("app.lightning", fromlist=["agl_available"]).agl_available(),
    }
