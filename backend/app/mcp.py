"""In-app MCP server registry.

Studio's agent already loads tools from any MCP server named in
STUDIO_MCP_SERVERS. This registry lets an admin register those servers in the
app — a filesystem or git server that exposes your existing scripts, an
internal tools server — so agents can pick up that context without a redeploy.
Registered servers merge with the env-configured ones and flow into the same
agent MCP loading, so a "build Python from our existing scripts" request has
the scripts available as tools.

Two kinds of row share the table, told apart by owner_id:
  NULL      an admin-registered GLOBAL server — command/args are trusted as
            stored (an admin chose them) and handed to the client verbatim.
  set       a toolbuilder-built, model-generated server owned by one user.
            Its stored command/args are NOT what runs: registered() keeps only
            the confined path from args and substitutes sandbox.launch_spec(),
            so the isolation level is decided at load time by the operator's
            STUDIO_TOOL_RUNNER, never frozen into the row at approval time.

Owner-scoped rows fail CLOSED at every step: a path that cannot be confined, an
unknown runner, a runner this deployment refuses (the process runner in
production — sandbox.py), or the tool builder being switched off entirely
(bootstrap.tool_builder_enabled()) all SKIP the row with a warning. Nothing here
ever downgrades a row to a weaker launch than the operator configured.
"""
import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import bootstrap, db, sandbox
from .auth import current_user

log = logging.getLogger("studio.mcp")

router = APIRouter(prefix="/settings/mcp", tags=["mcp"])


def init_tables():
    with db.connect() as c:
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
                owner_id TEXT,
                created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_name ON mcp_servers(name);
            """
        )
        # owner_id scopes a server to its owner: NULL = an admin-registered GLOBAL
        # server (loads into every agent); set = a toolbuilder-built tool (loads
        # ONLY for its owner). Databases created before the column existed get it
        # from migration 4 (app/migrations.py).
        c.commit()


def registered(user=None):
    """Enabled registered servers, in the shape MultiServerMCPClient wants,
    SCOPED to the caller: admin-registered globals (owner_id IS NULL) plus this
    user's own built tools (owner_id = user id). With no user, only globals —
    the safe default, so a built tool never leaks into another user's agent."""
    uid = (user or {}).get("id")
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM mcp_servers WHERE enabled=1 "
            "AND (owner_id IS NULL OR owner_id = ?)", (uid,)).fetchall()
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
        if d.get("owner_id") is not None:
            entry = _sandboxed(d["name"], entry)
            if entry is None:
                continue          # logged; a row we cannot confine never loads
        out[d["name"]] = entry
    return out


def _sandboxed(name, entry):
    """The launch spec for an owner-scoped (model-generated) row, or None.
    The stored args' last element is the server path (toolbuilder stores
    ["-u", path]); it is re-confined to the sandbox dir HERE, at load time, so
    a row edited in the DB, a moved sandbox, a bad runner name, or a runner
    this deployment refuses (the process runner in production) all fail closed
    with a warning instead of launching something outside the box."""
    if entry.get("transport") != "stdio":
        log.warning("mcp: skipping owner-scoped server %s: not stdio", name)
        return None
    # An operator can switch the whole feature off after rows already exist;
    # a registered row must then stop launching, not merely stop being created.
    if not bootstrap.tool_builder_enabled():
        log.warning("mcp: skipping owner-scoped server %s: the tool builder is "
                    "disabled (STUDIO_TOOLBUILDER)", name)
        return None
    args = entry.get("args") or []
    if not args:
        log.warning("mcp: skipping owner-scoped server %s: no server path", name)
        return None
    try:
        spec = sandbox.launch_spec(args[-1])
    except ValueError as e:
        log.warning("mcp: skipping owner-scoped server %s: %s", name, e)
        return None
    return {"transport": "stdio", **spec}


# Non-HTTP registration for an approved, supervised build (toolbuilder.py). The
# authority is an admin-approved supervised job established by the caller, not
# the current request, so there is no HTTP/admin gate here — but the name is
# validated to a safe identifier (it becomes a unique-indexed row) and the
# command/args are passed by the caller as a fixed interpreter + argv list (no
# shell). See toolbuilder.py for the confinement of the path in args.
_SAFE_SERVER = re.compile(r"[a-z][a-z0-9_]{0,63}")


def register_stdio(name, command, args, owner_id=None):
    """Register (idempotently) an approved stdio MCP server. Raises ValueError
    on an unsafe name. `command` must be a real interpreter path and `args` a
    list — both are stored verbatim and later handed to the stdio transport as
    argv, never through a shell."""
    if not (isinstance(name, str) and _SAFE_SERVER.fullmatch(name)):
        raise ValueError("unsafe server name")
    # owner_id set = a model-generated, admin-approved build. If the operator
    # turned the tool builder off, approval no longer produces a runnable row.
    if owner_id is not None and not bootstrap.tool_builder_enabled():
        raise ValueError("the tool builder is disabled (STUDIO_TOOLBUILDER)")
    if not command or not isinstance(args, list):
        raise ValueError("stdio server needs a command and an args list")
    with db.connect() as c:
        c.execute("DELETE FROM mcp_servers WHERE name=?", (name,))
        c.execute(
            "INSERT INTO mcp_servers (id, name, transport, url, command, args, enabled, owner_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), name, "stdio", None, command,
             json.dumps(args), 1, owner_id, time.time()),
        )
        c.commit()


def unregister(name):
    """Remove a registered server by name (used when a built tool is deleted)."""
    with db.connect() as c:
        c.execute("DELETE FROM mcp_servers WHERE name=?", (name,))
        c.commit()


def _list(user):
    with db.connect() as c:
        rows = c.execute("SELECT * FROM mcp_servers ORDER BY created_at").fetchall()
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
    with db.connect() as c:
        c.execute("DELETE FROM mcp_servers WHERE name=?", (name,))
        c.execute(
            "INSERT INTO mcp_servers (id, name, transport, url, command, args, enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), name, body.transport, body.url, body.command,
             json.dumps(body.args or []), 1 if body.enabled else 0, time.time()),
        )
        c.commit()
    db.log_activity(user, "mcp_register", prompt=name)
    return {"servers": _list(user)}


@router.delete("/{name}")
def remove_server(name: str, user=Depends(current_user)):
    _admin(user)
    with db.connect() as c:
        c.execute("DELETE FROM mcp_servers WHERE name=?", (name,))
        c.commit()
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
