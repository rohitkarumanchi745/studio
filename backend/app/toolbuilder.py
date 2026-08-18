"""Self-extending agents — build a tool or an MCP server from a user's context.

Studio agents load tools from registered MCP servers (mcp.py). This module lets
a user DESCRIBE a need and have Studio AUTHOR either a single tool or a whole
self-contained stdio MCP server, grounded in exactly the data sources/schemas
their role can reach (orchestrator.accessible_sources) plus the MCP servers
already available. The generated code is a DELIVERABLE — the same invariant
pybuild.py states: Studio never executes arbitrary Python it wrote.

The safety spine (highest-risk feature in the app):

    build ──▶ code in a DB row (status=draft)          no file, no exec
      │
    submit ─▶ supervisor.submit("mcp_build", <accessible source>, code)
      │       code-as-not-read ⇒ needs_human ⇒ awaiting_approval
      │                                          (no mcp_servers row yet)
    a human ADMIN approves the supervised job (supervisor._need_approver)
      │
    poll ──▶ job.human_by is set ⇒ _register(): write the server to a confined
             sandbox dir and register it as an ISOLATED stdio subprocess.

`code → runnable` has exactly ONE edge, and that edge is `human_by is set` — an
admin's approval of the supervised job. The app's only contact with the
generated code is open(path,"w").write(code); it is never import/exec/eval'd
in-process. It runs only when the MCP client spawns it as a separate OS process
over stdio, and only for the approved, enabled=1 row.
"""
import os
import re
import sys
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agent, db, mcp, supervisor
from .auth import current_user
from .orchestrator import accessible_sources

router = APIRouter(prefix="/toolbuilder", tags=["toolbuilder"])

# A single confined directory for approved servers. srv_{uuid}.py — no user
# input in the path — and every write is realpath-checked to stay under it.
SANDBOX = os.path.realpath(os.getenv(
    "STUDIO_TOOLBUILDER_DIR",
    os.path.join(os.path.dirname(__file__), "..", "toolbuilder_sandbox")))

_STATUS = ("draft", "awaiting_approval", "registered", "rejected")


# ── System prompts (two modes) ──────────────────────────────────────────

_SYS_MCP = """You are a senior platform engineer. Author ONE self-contained Python file: a Model Context Protocol server over stdio, using the official MCP Python SDK.

Rules:
- Return ONLY Python — no prose, no markdown fences.
- Use: from mcp.server.fastmcp import FastMCP ; mcp = FastMCP("<name>").
- Expose one or more @mcp.tool() functions that satisfy the user's need, shaped to the data sources / schemas in the provided context. Type-annotate every tool arg and return; add a one-line docstring per tool (the agent reads it).
- Read all config (endpoints, credentials, DSNs) from os.environ or tool arguments — NEVER hardcode secrets, NEVER invent credentials.
- End with: if __name__ == "__main__": mcp.run(transport="stdio")
- Must import cleanly and expose valid tools even if a data backend is unreachable."""

_SYS_TOOL = """You are a senior platform engineer. Author ONE self-contained Python tool function that satisfies the user's need, shaped to the data sources / schemas in the provided context.

Rules:
- Return ONLY Python — no prose, no markdown fences.
- Define one clearly named function; type-annotate every argument and the return; add a one-line docstring (the agent reads it to decide when to call it).
- Read all config (endpoints, credentials, DSNs) from os.environ or function arguments — NEVER hardcode secrets, NEVER invent credentials.
- No server bootstrap — just the function and any imports it needs. It will be wrapped in an MCP server at registration time."""


# ── Grounding: the user's RBAC-scoped context ───────────────────────────

def _context(user):
    """A grounding string built ONLY from what this user's role can reach —
    accessible_sources is already RBAC-filtered, so the generation can never be
    told about a source/table the owner lacks."""
    lines = ["Data sources this user can access (ground tools in these; do not exceed them):"]
    for s in accessible_sources(user):
        conn = s["connector"]
        lines.append(f"- {conn.name} (dialect {conn.dialect}); tables: "
                     + ", ".join(s["allowed"][:20]))
        for t, cols in list(s["schemas"].items())[:6]:
            lines.append("    " + t + "(" + ", ".join(
                f"{col['name']} {col['type']}" for col in cols) + ")")
    existing = list(mcp.registered(user).keys())
    if existing:
        lines.append("Existing MCP tool servers already available: " + ", ".join(existing))
    return "\n".join(lines)


# ── Generation (reuse pybuild's shape; never executes anything) ──────────

def _generate(prompt, context, user, spec, kind):
    """Draft code with the LLM, using registered MCP tools as context when
    present. Mirrors pybuild._generate: build graph if MCP servers exist, else
    a single llm.invoke; strip fences. Never touches the filesystem."""
    sys_prompt = _SYS_MCP if kind == "mcp" else _SYS_TOOL
    grounding = _context(user)
    extra = f"\n\nExtra reference:\n{context[:6000]}" if context else ""
    ask = f"Build a {'stdio MCP server' if kind == 'mcp' else 'tool function'} for: {prompt}\n\n{grounding}{extra}"

    mcp_cfg = agent.mcp_servers(user)
    if mcp_cfg:
        import asyncio

        async def _arun():
            tools = list(await agent._load_mcp_tools(mcp_cfg))
            llm = agent.make_llm(spec, user)
            graph = agent._build_graph(llm, tools, sys_prompt)
            return await graph.ainvoke({"messages": [("user", ask)]},
                                       config={"recursion_limit": 12})

        text = agent._final_text(asyncio.run(_arun()))
    else:
        llm = agent.make_llm(spec, user)
        reply = llm.invoke([("system", sys_prompt), ("user", ask)])
        text = reply.content if isinstance(reply.content, str) else "".join(
            b.get("text", "") for b in reply.content if isinstance(b, dict))

    return re.sub(r"^```(python)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()


def _scaffold(prompt, name):
    """A VALID FastMCP stdio server (never a half-server) for the no-key /
    generation-error path — so the feature is dormant-safe and demoable."""
    safe = _slug(name or prompt) or "tool"
    p = prompt.replace('"""', "'''")
    return (
        f'"""{p}  — scaffold; connect an LLM key for real generation."""\n'
        "import os\n"
        "from mcp.server.fastmcp import FastMCP\n\n"
        f'mcp = FastMCP("{safe}")\n\n\n'
        "@mcp.tool()\n"
        "def placeholder(query: str) -> str:\n"
        f'    """TODO: implement — {p}"""\n'
        '    return "not implemented"\n\n\n'
        'if __name__ == "__main__":\n'
        '    mcp.run(transport="stdio")\n'
    )


def _wrap_tool(code):
    """Wrap a single-function `tool` artifact in a minimal FastMCP server so the
    REGISTERED artifact is always a runnable stdio server, even though the
    previewed deliverable can be just a function."""
    return (
        '"""Studio tool wrapped as an MCP server."""\n'
        "from mcp.server.fastmcp import FastMCP\n\n"
        f"{code}\n\n"
        'mcp = FastMCP("studio_tool")\n\n'
        "# Register every top-level function this module defines as a tool.\n"
        "import inspect as _inspect, sys as _sys\n"
        "for _n, _f in list(vars(_sys.modules[__name__]).items()):\n"
        "    if _inspect.isfunction(_f) and not _n.startswith('_'):\n"
        "        mcp.tool()(_f)\n\n"
        'if __name__ == "__main__":\n'
        '    mcp.run(transport="stdio")\n'
    )


# ── Naming / injection-safe validation ──────────────────────────────────

def _slug(raw):
    """Lowercase identifier from free text: [a-z0-9_], must start with a letter.
    Strips every shell metacharacter, so nothing user-supplied can survive into
    a server name. Empty (nothing valid) → "" (caller falls back)."""
    s = re.sub(r"[^a-z0-9_]+", "_", (raw or "").lower()).strip("_")
    s = re.sub(r"^[^a-z]+", "", s)
    return s[:40]


def _server_name(artifact, requested=None):
    """The registered server name: tb_<owner8>_<slug>_<artifact8>. The artifact
    id makes the name UNIQUE per artifact, so two same-owner artifacts that slug
    identically never collide (and register_stdio's delete-by-name can never
    repoint one artifact's server row at another's file). A safe identifier only
    — validated again by mcp.register_stdio (defense in depth)."""
    base = _slug(requested) or _slug(artifact["prompt"]) or "tool"
    owner8 = re.sub(r"[^a-z0-9]", "", (artifact["owner_id"] or "").lower())[:8] or "anon"
    aid8 = re.sub(r"[^a-z0-9]", "", (artifact["id"] or "").lower())[:8] or "0"
    name = f"tb_{owner8}_{base[:40]}_{aid8}"[:64]
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
        raise HTTPException(400, "could not derive a safe server name")
    return name


# ── Persistence ─────────────────────────────────────────────────────────

def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS toolbuilder_artifacts (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            prompt TEXT NOT NULL,
            code TEXT NOT NULL,
            status TEXT NOT NULL,
            job_id TEXT,
            server_name TEXT,
            source TEXT,
            name TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tb_owner ON toolbuilder_artifacts(owner_id, updated_at DESC);
        """
    )
    c.commit()
    c.close()


def _insert(a):
    c = db._conn()
    cols = ("id,owner_id,kind,prompt,code,status,job_id,server_name,source,name,"
            "created_at,updated_at")
    c.execute(f"INSERT INTO toolbuilder_artifacts ({cols}) VALUES ({','.join('?' * 12)})",
              tuple(a[k] for k in cols.split(",")))
    c.commit()
    c.close()


def _save(a, **fields):
    a.update(fields)
    a["updated_at"] = time.time()
    fields["updated_at"] = a["updated_at"]
    sets = ", ".join(f"{k}=?" for k in fields)
    c = db._conn()
    c.execute(f"UPDATE toolbuilder_artifacts SET {sets} WHERE id=?",
              list(fields.values()) + [a["id"]])
    c.commit()
    c.close()


def _row(id):
    c = db._conn()
    r = c.execute("SELECT * FROM toolbuilder_artifacts WHERE id=?", (id,)).fetchone()
    c.close()
    return dict(r) if r else None


def _get(id, user):
    """404-not-403: the row only if the caller owns it (admins may also read —
    they are the approvers). A stranger sees the same 404 as a missing id."""
    a = _row(id)
    if not a or (a["owner_id"] != user["id"] and user["role"] != "admin"):
        raise HTTPException(404, "Not found")
    return a


# ── Register on approval (the ONLY path that writes a file / a server row) ──

def _register(a):
    """Write the approved server into the confined sandbox and register it as an
    isolated stdio subprocess. Reached from exactly one condition: the linked
    supervised job shows human_by (an admin approved it). Idempotent."""
    os.makedirs(SANDBOX, exist_ok=True)
    path = os.path.realpath(os.path.join(SANDBOX, f"srv_{a['id']}.py"))
    if not path.startswith(SANDBOX + os.sep):        # path confinement
        raise HTTPException(400, "path escapes sandbox")
    name = a.get("server_name") or _server_name(a, a.get("name"))
    code = a["code"] if a["kind"] == "mcp" else _wrap_tool(a["code"])
    with open(path, "w") as f:                        # WRITE only — never import/exec
        f.write(code)
    # command is the app's own interpreter (never user/model supplied); args is
    # a fixed [-u, <confined path>] list handed straight to the stdio transport.
    mcp.register_stdio(name, sys.executable, ["-u", path], owner_id=a["owner_id"])
    _save(a, status="registered", server_name=name)


def _resolve(a):
    """Poll the linked supervised job and reflect the human's decision onto the
    artifact. Supervisor has no post-approval callback, so this runs on every
    read. We key on human_by (an admin, enforced by supervisor._need_approver),
    never on the job's run result (the SQL executor mis-handles Python text and
    escalates — cosmetic; the app still never exec/imports the code)."""
    if a["status"] not in ("awaiting_approval",) or not a.get("job_id"):
        return a
    job = supervisor._get(a["job_id"])
    if not job:
        return a
    if job["status"] == "rejected":
        _save(a, status="rejected")
    elif job.get("human_by"):
        _register(a)      # idempotent; flips status to 'registered'
    return a


# ── API ─────────────────────────────────────────────────────────────────

class BuildIn(BaseModel):
    prompt: str
    context: str | None = None
    kind: str = "mcp"          # "mcp" | "tool"
    name: str | None = None    # optional hint for the server name


class SubmitIn(BaseModel):
    source: str | None = None  # RBAC anchor; must be an accessible source


def _public(a):
    d = dict(a)
    d.pop("owner_id", None)
    return d


def _card(a):
    return {k: a.get(k) for k in ("id", "kind", "prompt", "status", "server_name",
                                  "source", "job_id", "name", "created_at")}


@router.get("/sources")
def sources(user=Depends(current_user)):
    """Grounding preview for the UI: exactly the sources this role can reach."""
    srcs = accessible_sources(user)
    return {
        "sources": [{"name": s["connector"].name, "dialect": s["connector"].dialect,
                     "tables": s["allowed"][:40]} for s in srcs],
        "existing_servers": list(mcp.registered(user).keys()),
    }


@router.post("/build")
def build(body: BuildIn, user=Depends(current_user)):
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "Describe what the tool / MCP server should do")
    kind = body.kind if body.kind in ("mcp", "tool") else "mcp"

    spec = agent.llm_spec()
    used_mcp = list(agent.mcp_servers(user).keys())
    grounded = [s["connector"].name for s in accessible_sources(user)]

    note = None
    if not agent.llm_available(spec, user):
        code, mode = _scaffold(prompt, body.name), "fallback"
        note = "No LLM key — returned a scaffold. Connect a key for real generation."
    else:
        try:
            code, mode = _generate(prompt, body.context, user, spec, kind), "agent"
        except Exception as e:
            code, mode = _scaffold(prompt, body.name), "fallback"
            note = f"Generation error ({e}); returned a scaffold."

    now = time.time()
    a = {"id": str(uuid.uuid4()), "owner_id": user["id"], "kind": kind,
         "prompt": prompt, "code": code, "status": "draft", "job_id": None,
         "server_name": None, "source": None, "name": (body.name or None),
         "created_at": now, "updated_at": now}
    _insert(a)                          # DB only — NO file, NO registration, NO exec
    db.log_activity(user, "toolbuilder_build", prompt=prompt[:200])
    return {"id": a["id"], "kind": kind, "code": code, "status": "draft",
            "mode": mode, "mcp_servers": used_mcp, "grounded_sources": grounded,
            "note": note}


@router.get("")
def list_artifacts(user=Depends(current_user)):
    c = db._conn()
    rows = c.execute(
        "SELECT * FROM toolbuilder_artifacts WHERE owner_id=? ORDER BY updated_at DESC LIMIT 200",
        (user["id"],)).fetchall()
    c.close()
    arts = [_resolve(dict(r)) for r in rows]
    return {"artifacts": [_card(a) for a in arts]}


@router.get("/{id}")
def get_artifact(id: str, user=Depends(current_user)):
    a = _resolve(_get(id, user))
    job = None
    if a.get("job_id"):
        j = supervisor._get(a["job_id"])
        if j:
            job = {"id": j["id"], "status": j["status"], "human_by": j.get("human_by"),
                   "supervisor_reasons": supervisor._public(j).get("supervisor_reasons")}
    return {"artifact": _public(a), "job": job}


@router.post("/{id}/submit")
def submit(id: str, body: SubmitIn = SubmitIn(), user=Depends(current_user)):
    a = _get(id, user)
    if a["status"] not in ("draft", "rejected"):
        raise HTTPException(400, f"Artifact is {a['status']}, not submittable")

    srcs = accessible_sources(user)
    if not srcs:
        raise HTTPException(400, "No accessible data source to scope this tool to.")
    names = {s["connector"].name for s in srcs}
    anchor = (body.source or "").strip() or next(iter(sorted(names)))
    if anchor not in names:
        # Can't scope a built tool to a source the owner's role cannot reach.
        raise HTTPException(400, f"You cannot scope this tool to '{anchor}'.")

    job = supervisor.submit(kind="mcp_build", target=anchor, script=a["code"], user=user)
    _save(a, status="awaiting_approval", job_id=job["id"], source=anchor)
    db.log_activity(user, "toolbuilder_submit", prompt=a["prompt"][:200], source=anchor)
    return {"artifact": _public(_row(a["id"])),
            "job": {"id": job["id"], "status": job["status"],
                    "supervisor_reasons": supervisor._public(job).get("supervisor_reasons")}}


@router.delete("/{id}")
def delete_artifact(id: str, user=Depends(current_user)):
    a = _get(id, user)
    if a.get("server_name"):
        try:
            mcp.unregister(a["server_name"])
        except Exception:
            pass
    path = os.path.realpath(os.path.join(SANDBOX, f"srv_{a['id']}.py"))
    if path.startswith(SANDBOX + os.sep) and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass
    c = db._conn()
    c.execute("DELETE FROM toolbuilder_artifacts WHERE id=?", (id,))
    c.commit()
    c.close()
    return {"ok": True}
