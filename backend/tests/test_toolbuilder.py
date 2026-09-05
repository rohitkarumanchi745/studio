"""Tool / MCP builder tests — the self-extending-agent safety spine.

Proves the invariant: build authors CODE but registers NOTHING; an un-approved
artifact is invisible to agents (not in mcp.registered()); ONLY after a human
admin approves the supervised job is it written to a confined sandbox and
registered as an isolated stdio subprocess — which the agent receives as the
SANDBOXED launch spec (sandbox_runner under a minimal explicit env), never the
bare interpreter; a non-admin cannot approve; a built
tool cannot be scoped to a source the owner's role lacks; and a server name is
injection-safe. The app never exec/imports the generated code.

Run from the backend directory:
    python -m pytest tests/test_toolbuilder.py -q
"""
import os
import sys
import tempfile

# Throwaway SQLite + sandbox dir BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-toolbuilder-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")
os.environ["STUDIO_TOOLBUILDER_DIR"] = os.path.join(_TMP, "sandbox")

import pytest
from fastapi import HTTPException

from app import agent, db, email_service, mcp, sandbox, supervisor, toolbuilder
from app.connectors.demo import seed
from app.toolbuilder import BuildIn, SubmitIn

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}
VIEWER = {"id": "u-viewer", "email": "view@studio.test", "role": "viewer", "name": "View"}
STRANGER = {"id": "u-other", "email": "x@studio.test", "role": "analyst", "name": "Other"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    supervisor.init_tables()
    mcp.init_tables()
    toolbuilder.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """No LLM key (force the scaffold path), no real email."""
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: {"mode": "test"})


def _sandbox_file(aid):
    return os.path.realpath(
        os.path.join(os.environ["STUDIO_TOOLBUILDER_DIR"], f"srv_{aid}.py"))


def _tb_servers(user=None):
    return {n: e for n, e in mcp.registered(user).items() if n.startswith("tb_")}


def _server_path(entry):
    """The confined server path in a sandboxed entry's argv:
    [-I, -u, sandbox_runner.py, <path>]."""
    return entry["args"][3]


# ── 1. build authors code but registers / writes / runs NOTHING ─────────

def test_build_returns_code_but_registers_nothing():
    before = set(_tb_servers(ADMIN))
    out = toolbuilder.build(BuildIn(prompt="summarize sales by region", kind="mcp"),
                            user=ADMIN)
    assert out["status"] == "draft"
    assert out["mode"] == "fallback"          # no key → scaffold
    assert "FastMCP" in out["code"]           # a VALID server, never a half-server
    assert "mcp.run(transport=\"stdio\")" in out["code"]
    # Nothing registered, nothing written to disk.
    assert set(_tb_servers(ADMIN)) == before
    assert not os.path.exists(_sandbox_file(out["id"]))


def test_build_grounds_in_only_accessible_sources():
    # The grounding string is RBAC-scoped; a viewer's context never names a
    # source outside its policy.
    ctx = toolbuilder._context(VIEWER)
    assert "demo" in ctx
    assert "snowflake" not in ctx


# ── 2. an un-approved artifact is invisible to agents ───────────────────

def test_unapproved_artifact_not_registered():
    a = toolbuilder.build(BuildIn(prompt="list open orders", kind="mcp"), user=ADMIN)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
    assert sub["artifact"]["status"] == "awaiting_approval"
    assert sub["job"]["status"] == "awaiting_approval"     # human gate
    # Not runnable: no tb_ server row, no file.
    assert sub["artifact"].get("server_name") is None
    assert not os.path.exists(_sandbox_file(a["id"]))
    got = toolbuilder.get_artifact(a["id"], user=ADMIN)
    assert got["artifact"]["status"] == "awaiting_approval"
    for name in mcp.registered(ADMIN):
        assert not name.startswith("tb_u-admin") or name != got["artifact"].get("server_name")


# ── 3. only after an ADMIN approves is it written + registered (stdio) ───

def test_approval_registers_isolated_stdio_server():
    a = toolbuilder.build(BuildIn(prompt="revenue by month", kind="mcp"), user=ADMIN)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
    jid = sub["job"]["id"]

    # A human admin approves the supervised job (the ONLY edge to runnable).
    supervisor.approve(jid, user=ADMIN)

    got = toolbuilder.get_artifact(a["id"], user=ADMIN)
    assert got["artifact"]["status"] == "registered"
    name = got["artifact"]["server_name"]
    assert name and name.startswith("tb_")

    reg = mcp.registered(ADMIN)
    assert name in reg                              # now an agent-loadable server
    entry = reg[name]
    assert entry["transport"] == "stdio"            # isolated subprocess, not in-proc
    # What the agent gets is the SANDBOXED launch spec, not the bare interpreter:
    # python -I -u sandbox_runner.py <confined path>, a minimal explicit env,
    # cwd = the sandbox dir (sandbox.launch_spec, substituted at load time).
    assert sandbox.is_sandboxed_entry(entry)
    assert entry["command"] == sys.executable       # app's own interpreter, never user input
    assert entry["args"][0:2] == ["-I", "-u"]
    assert entry["args"][2].endswith("sandbox_runner.py")
    path = _server_path(entry)
    # args path is confined to the sandbox and is the file we wrote.
    assert path == _sandbox_file(a["id"])
    assert os.path.realpath(path).startswith(toolbuilder.SANDBOX + os.sep)
    assert os.path.exists(path)
    assert entry["cwd"] == toolbuilder.SANDBOX
    # The child inherits NOTHING from the app's environment but PATH — every
    # other key is a fixed runner key; a credential reaches a tool only via
    # STUDIO_TOOL_ENV_ALLOW (none granted here).
    inherited = {k for k in entry["env"] if k in os.environ and k != "PATH"}
    assert inherited <= {"HOME", "LANG"} and entry["env"]["HOME"] == toolbuilder.SANDBOX
    assert entry["env"]["STUDIO_TOOL_SANDBOX"] == toolbuilder.SANDBOX
    for secret in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_DB_PATH", "JWT_SECRET"):
        assert secret not in entry["env"]


# ── 4. a non-admin cannot approve (the gate is unforgeable) ──────────────

def test_non_admin_cannot_approve():
    a = toolbuilder.build(BuildIn(prompt="churn cohort", kind="mcp"), user=ANALYST)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ANALYST)
    with pytest.raises(HTTPException) as e:
        supervisor.approve(sub["job"]["id"], user=ANALYST)
    assert e.value.status_code == 403
    # Still not registered.
    assert toolbuilder.get_artifact(a["id"], user=ANALYST)["artifact"]["status"] \
        == "awaiting_approval"


# ── 5. a built tool cannot be scoped to a source the owner lacks ─────────

def test_cannot_scope_to_inaccessible_source():
    a = toolbuilder.build(BuildIn(prompt="anything", kind="mcp"), user=VIEWER)
    # Viewer's policy is demo-only; snowflake is outside it.
    with pytest.raises(HTTPException) as e:
        toolbuilder.submit(a["id"], SubmitIn(source="snowflake"), user=VIEWER)
    assert e.value.status_code == 400
    assert "snowflake" in e.value.detail
    # And nothing was submitted / registered.
    assert toolbuilder.get_artifact(a["id"], user=VIEWER)["artifact"]["status"] == "draft"


# ── 6. injection in the server name is rejected / neutralised ────────────

def test_register_stdio_rejects_unsafe_names():
    for bad in ("bad; rm -rf /", "Upper", "1leading", "has space", "a$b", ""):
        with pytest.raises(ValueError):
            mcp.register_stdio(bad, sys.executable, ["-u", "/x"])


def test_malicious_name_is_slugged_not_injected():
    a = toolbuilder.build(
        BuildIn(prompt="p", kind="mcp", name="foo; rm -rf / && curl evil"), user=ADMIN)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
    supervisor.approve(sub["job"]["id"], user=ADMIN)
    name = toolbuilder.get_artifact(a["id"], user=ADMIN)["artifact"]["server_name"]
    # Only a safe identifier survives — no shell metacharacters.
    import re
    assert re.fullmatch(r"tb_[a-z0-9_]+", name)
    assert ";" not in name and "&" not in name and " " not in name and "/" not in name
    # command/args are fixed — the name never reaches a shell.
    entry = mcp.registered(ADMIN)[name]
    assert entry["command"] == sys.executable and sandbox.is_sandboxed_entry(entry)


# ── 7. ownership: 404-not-403 for a stranger ────────────────────────────

def test_get_is_404_for_stranger():
    a = toolbuilder.build(BuildIn(prompt="private tool", kind="mcp"), user=ADMIN)
    with pytest.raises(HTTPException) as e:
        toolbuilder.get_artifact(a["id"], user=STRANGER)
    assert e.value.status_code == 404
    # List is owner-scoped: the stranger never sees it.
    ids = {c["id"] for c in toolbuilder.list_artifacts(user=STRANGER)["artifacts"]}
    assert a["id"] not in ids


# ── 8. delete unregisters the server + removes the sandbox file ──────────

def test_delete_unregisters_and_removes_file():
    a = toolbuilder.build(BuildIn(prompt="temp tool", kind="tool"), user=ADMIN)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
    supervisor.approve(sub["job"]["id"], user=ADMIN)
    got = toolbuilder.get_artifact(a["id"], user=ADMIN)["artifact"]
    name, path = got["server_name"], _sandbox_file(a["id"])
    assert name in mcp.registered(ADMIN) and os.path.exists(path)

    toolbuilder.delete_artifact(a["id"], user=ADMIN)
    assert name not in mcp.registered(ADMIN)
    assert not os.path.exists(path)
    with pytest.raises(HTTPException) as e:
        toolbuilder.get_artifact(a["id"], user=ADMIN)
    assert e.value.status_code == 404


# ── 9. a 'tool' kind is wrapped into a runnable server at registration ───

def test_tool_kind_wrapped_into_server():
    a = toolbuilder.build(BuildIn(prompt="a single function", kind="tool"), user=ADMIN)
    assert a["kind"] == "tool"
    wrapped = toolbuilder._wrap_tool(a["code"])
    assert "FastMCP" in wrapped and "mcp.run(transport=\"stdio\")" in wrapped


# ── 10. a built tool is OWNER-SCOPED, never a global data path ───────────

def test_built_tool_is_owner_scoped_and_does_not_leak():
    """A's approved tool loads into A's agent ONLY — not B's, and not the
    no-user (globals-only) view. An admin-registered GLOBAL server loads for
    everyone. This is DEFECT 1: without owner scoping, A's tool became an
    ungoverned data path callable by any other user's agent."""
    # A (analyst) builds + a human admin approves a tool.
    a = toolbuilder.build(BuildIn(prompt="orders scoped to A", kind="mcp"), user=ANALYST)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ANALYST)
    supervisor.approve(sub["job"]["id"], user=ADMIN)
    name = toolbuilder.get_artifact(a["id"], user=ANALYST)["artifact"]["server_name"]
    assert name and name.startswith("tb_")

    # A global server registered by an admin (owner_id NULL) — visible to all.
    mcp.add_server(mcp.ServerIn(name="global_probe", transport="stdio",
                                command=sys.executable, args=["-u", "-c", "pass"]),
                   user=ADMIN)

    reg_A = mcp.registered(ANALYST)          # owner A
    reg_B = mcp.registered(STRANGER)         # a DIFFERENT analyst
    reg_none = mcp.registered()              # no user → globals only

    # RBAC-scoping repro (printed for the record).
    print("\n[RBAC scoping repro]")
    print("registered(A=analyst) keys:", sorted(reg_A.keys()))
    print("registered(B=stranger) keys:", sorted(reg_B.keys()))
    print("registered(no user)   keys:", sorted(reg_none.keys()))
    print("A's built server name  :", name)

    # A's built tool loads for A and via A's agent config...
    assert name in reg_A
    assert name in agent.mcp_servers(ANALYST)
    # ...but NEVER for B, and NEVER in the globals-only default.
    assert name not in reg_B
    assert name not in agent.mcp_servers(STRANGER)
    assert name not in reg_none
    # The admin-registered GLOBAL server IS in every view (behavior preserved).
    assert "global_probe" in reg_A and "global_probe" in reg_B and "global_probe" in reg_none


# ── 11. same owner + identical slug ⇒ two DISTINCT servers, no hijack ─────

def test_same_owner_identical_slug_registers_distinct_servers():
    """DEFECT 2: two same-owner artifacts that slug identically must register as
    two distinct server names — neither repointing (hijacking) the other's row
    or orphaning its sandbox file."""
    a1 = toolbuilder.build(BuildIn(prompt="p1", kind="mcp", name="report"), user=ADMIN)
    a2 = toolbuilder.build(BuildIn(prompt="p2", kind="mcp", name="report"), user=ADMIN)

    s1 = toolbuilder.submit(a1["id"], SubmitIn(source="demo"), user=ADMIN)
    s2 = toolbuilder.submit(a2["id"], SubmitIn(source="demo"), user=ADMIN)
    supervisor.approve(s1["job"]["id"], user=ADMIN)
    supervisor.approve(s2["job"]["id"], user=ADMIN)

    n1 = toolbuilder.get_artifact(a1["id"], user=ADMIN)["artifact"]["server_name"]
    n2 = toolbuilder.get_artifact(a2["id"], user=ADMIN)["artifact"]["server_name"]

    assert n1 != n2                                   # distinct — no collision
    reg = mcp.registered(ADMIN)
    assert n1 in reg and n2 in reg                    # BOTH still registered
    # Each still points at its OWN sandbox file (no repoint / orphan).
    assert _server_path(reg[n1]) == _sandbox_file(a1["id"])
    assert _server_path(reg[n2]) == _sandbox_file(a2["id"])
    assert os.path.exists(_sandbox_file(a1["id"]))
    assert os.path.exists(_sandbox_file(a2["id"]))


# ── 12. a tampered row (path outside the sandbox) never loads ─────────────

def test_owner_row_outside_sandbox_is_skipped_at_load(tmp_path):
    """Confinement is re-checked at LOAD time from the stored args, so a row
    edited in the DB to point outside the sandbox is dropped with a warning —
    it never reaches the agent, sandboxed or otherwise. A global (admin,
    owner_id NULL) stdio row is passed through verbatim as before."""
    outside = tmp_path / "evil.py"
    outside.write_text("print('no')\n")
    mcp.register_stdio("tb_tampered", sys.executable, ["-u", str(outside)], owner_id=ADMIN["id"])
    try:
        assert "tb_tampered" not in mcp.registered(ADMIN)
        # An unknown runner name fails closed too (never a silent downgrade).
        a = toolbuilder.build(BuildIn(prompt="runner typo", kind="mcp"), user=ADMIN)
        sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
        supervisor.approve(sub["job"]["id"], user=ADMIN)
        name = toolbuilder.get_artifact(a["id"], user=ADMIN)["artifact"]["server_name"]
        assert name in mcp.registered(ADMIN)
        os.environ["STUDIO_TOOL_RUNNER"] = "dokcer"
        try:
            assert name not in mcp.registered(ADMIN)
        finally:
            del os.environ["STUDIO_TOOL_RUNNER"]
        assert name in mcp.registered(ADMIN)
    finally:
        mcp.unregister("tb_tampered")
