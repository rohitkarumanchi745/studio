"""In-app MCP server registry.

Studio's agent already loads tools from any MCP server named in
STUDIO_MCP_SERVERS. This registry lets an admin register those servers in the
app — a filesystem or git server that exposes your existing scripts, an
internal tools server — so agents can pick up that context without a redeploy.
Registered servers merge with the env-configured ones and flow into the same
agent MCP loading, so a "build Python from our existing scripts" request has
the scripts available as tools.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/settings/mcp", tags=["mcp"])


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            transport TEXT NOT NULL,
            url TEXT,
            command TEXT,
            args TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_name ON mcp_servers(name);
        """
    )
    c.commit()
    c.close()


def registered():
    """Enabled registered servers, in the shape MultiServerMCPClient wants."""
    c = db._conn()
    rows = c.execute("SELECT * FROM mcp_servers WHERE enabled=1").fetchall()
    c.close()
    out = {}
    for r in rows:
        d = dict(r)
        entry = {"transport": d["transport"]}
        if d.get("url"):
            entry["url"] = d["url"]
        if d.get("command"):
            entry["command"] = d["command"]
        if d.get("args"):
            try:
                entry["args"] = json.loads(d["args"])
            except Exception:
                entry["args"] = []
        out[d["name"]] = entry
    return out


def _list(user):
    c = db._conn()
    rows = c.execute("SELECT * FROM mcp_servers ORDER BY created_at").fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        if d.get("args"):
            try:
                d["args"] = json.loads(d["args"])
            except Exception:
                pass
        out.append(d)
    return out


class ServerIn(BaseModel):
    name: str
    transport: str          # "streamable_http" | "sse" | "stdio"
    url: str | None = None
    command: str | None = None
    args: list | None = None
    enabled: bool = True


def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "MCP servers are admin-only")


@router.get("")
def list_servers(user=Depends(current_user)):
    _admin(user)
    return {"servers": _list(user), "env_servers": list(_env_names())}


@router.post("", status_code=201)
def add_server(body: ServerIn, user=Depends(current_user)):
    _admin(user)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name is required")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(400, "stdio transport needs a command")
    if body.transport in ("streamable_http", "sse") and not body.url:
        raise HTTPException(400, "http/sse transport needs a url")
    c = db._conn()
    c.execute("DELETE FROM mcp_servers WHERE name=?", (name,))
    c.execute(
        "INSERT INTO mcp_servers (id, name, transport, url, command, args, enabled, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), name, body.transport, body.url, body.command,
         json.dumps(body.args or []), 1 if body.enabled else 0, time.time()),
    )
    c.commit()
    c.close()
    db.log_activity(user, "mcp_register", prompt=name)
    return {"servers": _list(user)}


@router.delete("/{name}")
def remove_server(name: str, user=Depends(current_user)):
    _admin(user)
    c = db._conn()
    c.execute("DELETE FROM mcp_servers WHERE name=?", (name,))
    c.commit()
    c.close()
    return {"servers": _list(user)}


def _env_names():
    import os
    raw = os.getenv("STUDIO_MCP_SERVERS", "").strip()
    if not raw:
        return set()
    try:
        return set(json.loads(raw).keys())
    except Exception:
        return set()
