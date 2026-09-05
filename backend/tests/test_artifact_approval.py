"""Code artifacts ride the approval lifecycle, NEVER the execution lifecycle.

A toolbuilder artifact (a generated MCP server / tool) is submitted as a
supervised job so an admin has to sign it — but its script is PYTHON, and the
supervisor's job executor is a WAREHOUSE executor. Before the fix the artifact
kind had no branch of its own and fell through to the SQL path, where each
"statement" of the generated Python was handed to `connector.run_script()`: an
approval flow for a deliverable riding a warehouse write path.

What is pinned here:

  * an artifact job's execution touches NO connector at all — the result only
    records who approved it, and `executed` is False;
  * dispatch is exhaustive: an UNKNOWN job kind raises instead of defaulting to
    run_script, so a future kind cannot silently inherit the warehouse path;
  * the approval invariant is unchanged — only an admin's approval registers
    the server, exactly once, and a rejection registers nothing.

Run from the backend directory:
    python -m pytest tests/test_artifact_approval.py -q
"""
import os
import tempfile

# Throwaway SQLite + sandbox dir BEFORE app modules compute their paths.
# setdefault for the sandbox: toolbuilder.SANDBOX is frozen at IMPORT time while
# mcp/sandbox re-read the env per call, so the whole test session must agree on
# one directory — whichever of this module and test_toolbuilder is imported
# first claims it, and the other reads it back out of the env.
_TMP = tempfile.mkdtemp(prefix="studio-artifact-approval-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")
os.environ.setdefault("STUDIO_TOOLBUILDER_DIR", os.path.join(_TMP, "sandbox"))

import time
import uuid

import pytest
from fastapi import HTTPException

from app import db, email_service, mcp, supervisor, toolbuilder
from app.connectors.demo import DemoConnector, seed
from app.toolbuilder import BuildIn, SubmitIn

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}


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
    """No LLM key (the scaffold path — real generation is not what's under
    test), no real email."""
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: {"mode": "test"})


@pytest.fixture
def no_warehouse(monkeypatch):
    """A connector stub that EXPLODES on contact. Nothing about approving a
    code deliverable may reach a data environment — not the write hook, not the
    Spark hook, not even the connector lookup.

    Patched on the CLASS, never on the shared connector INSTANCE: monkeypatch
    restores an instance attribute by re-setting it, which would leave a bound
    method shadowing the class for the rest of the session and quietly defeat
    another module's class-level patch."""
    def boom(*a, **k):
        raise AssertionError("an artifact approval must never touch a connector")

    for hook in ("run_script", "run_query", "submit_spark_job"):
        monkeypatch.setattr(DemoConnector, hook, boom)
    monkeypatch.setattr(supervisor, "connector_or_400", boom)


def _artifact_job(kind=supervisor.ARTIFACT_KIND, human_by=ADMIN["email"], script="print('x')"):
    now = time.time()
    return {"id": str(uuid.uuid4()), "user_id": ADMIN["id"], "requester_role": "admin",
            "requester_email": ADMIN["email"], "kind": kind, "target": "demo",
            "script": script, "risk": "artifact", "status": "running",
            "supervisor_decision": "needs_human", "supervisor_reasons": "[]",
            "attempts": 0, "max_retries": 2, "last_error": None, "result": None,
            "human_by": human_by, "created_at": now, "updated_at": now}


# ── 1. an artifact job never reaches the warehouse executor ──────────────

def test_artifact_job_never_runs_a_script(no_warehouse):
    a = toolbuilder.build(BuildIn(prompt="orders by region", kind="mcp"), user=ADMIN)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
    job = supervisor._get(sub["job"]["id"])
    assert job["kind"] == supervisor.ARTIFACT_KIND
    assert job["risk"] == "artifact"          # not "write" — no SQL was ever read
    assert job["status"] == "awaiting_approval"

    done = supervisor.approve(job["id"], user=ADMIN)   # the connector stub would raise
    assert done["status"] == "succeeded"
    # The "execution" is the decision, and says so.
    assert done["result"]["executed"] is False
    assert done["result"]["approved_by"] == ADMIN["email"]
    assert done["result"]["kind"] == supervisor.ARTIFACT_KIND


def test_execute_artifact_is_a_no_op_with_no_connector(no_warehouse):
    res = supervisor._execute(_artifact_job())
    assert res == {"kind": supervisor.ARTIFACT_KIND, "executed": False,
                   "approved_by": ADMIN["email"],
                   "detail": "Code artifact approved by an administrator; "
                             "nothing was executed."}


def test_artifact_without_an_approver_fails_closed(no_warehouse):
    """Policy makes every artifact job human-gated, so execution without an
    approver means the gate was bypassed — it raises rather than 'succeeding'
    an unsigned artifact."""
    with pytest.raises(RuntimeError, match="without a human approver"):
        supervisor._execute(_artifact_job(human_by=None))


# ── 2. an unknown kind raises instead of inheriting the script path ──────

def test_unknown_job_kind_raises_instead_of_running_a_script(no_warehouse):
    with pytest.raises(RuntimeError, match="Unknown job kind"):
        supervisor._execute(_artifact_job(kind="some_future_kind", script="SELECT 1"))
    # ...and the closed set is what dispatch is written against.
    assert set(supervisor.KINDS) == {"sql_script", "spark_job", "platform_run",
                                     supervisor.ARTIFACT_KIND}


def test_unknown_kind_escalates_rather_than_executing(no_warehouse):
    """Through the retry loop the same refusal shows up as an escalation — a
    human decision — never as a silently executed script."""
    job = _artifact_job(kind="some_future_kind", script="DROP TABLE orders")
    supervisor._insert(job)
    supervisor._run(job)
    assert supervisor._get(job["id"])["status"] == "escalated"
    assert "Unknown job kind" in supervisor._get(job["id"])["last_error"]


def test_artifact_kind_is_not_submittable_over_http():
    """The artifact kind is minted by toolbuilder next to the row it approves;
    accepting one over the jobs API would create an approval with no
    deliverable behind it."""
    with pytest.raises(HTTPException) as e:
        supervisor.submit_job(
            supervisor.SubmitIn(kind=supervisor.ARTIFACT_KIND, target="demo",
                                script="print('x')"), user=ADMIN)
    assert e.value.status_code == 400


# ── 3. the approval invariant is unchanged: admin approval registers ONCE ─

def test_approval_registers_the_server_exactly_once(no_warehouse, monkeypatch):
    calls = []
    real = mcp.register_stdio
    monkeypatch.setattr(mcp, "register_stdio",
                        lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])

    a = toolbuilder.build(BuildIn(prompt="register once", kind="mcp"), user=ADMIN)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
    assert calls == []                                   # nothing before approval

    supervisor.approve(sub["job"]["id"], user=ADMIN)
    got = toolbuilder.get_artifact(a["id"], user=ADMIN)["artifact"]
    assert got["status"] == "registered"
    name = got["server_name"]
    assert calls.count(name) == 1

    # _resolve runs on every read; a registered artifact must not re-register.
    # (Counted by name: a list read resolves this owner's OTHER artifacts too.)
    toolbuilder.get_artifact(a["id"], user=ADMIN)
    toolbuilder.list_artifacts(user=ADMIN)
    assert calls.count(name) == 1
    assert name in mcp.registered(ADMIN)


def test_rejection_registers_nothing(no_warehouse):
    a = toolbuilder.build(BuildIn(prompt="never approved", kind="mcp"), user=ANALYST)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ANALYST)
    supervisor.reject(sub["job"]["id"], user=ADMIN)

    got = toolbuilder.get_artifact(a["id"], user=ANALYST)["artifact"]
    assert got["status"] == "rejected"
    assert got["server_name"] is None
    assert not os.path.exists(
        os.path.join(os.environ["STUDIO_TOOLBUILDER_DIR"], f"srv_{a['id']}.py"))
    assert not [n for n in mcp.registered(ANALYST) if n.startswith("tb_")]


def test_non_admin_cannot_approve_an_artifact(no_warehouse):
    a = toolbuilder.build(BuildIn(prompt="analyst tries", kind="mcp"), user=ANALYST)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ANALYST)
    with pytest.raises(HTTPException) as e:
        supervisor.approve(sub["job"]["id"], user=ANALYST)
    assert e.value.status_code == 403
    assert toolbuilder.get_artifact(a["id"], user=ANALYST)["artifact"]["status"] \
        == "awaiting_approval"


def test_registration_requires_an_artifact_job(no_warehouse):
    """`code → runnable` has one edge: an admin's human_by on THIS artifact's
    own job. A job of another kind carrying the id must not open it."""
    a = toolbuilder.build(BuildIn(prompt="wrong job kind", kind="mcp"), user=ADMIN)
    sub = toolbuilder.submit(a["id"], SubmitIn(source="demo"), user=ADMIN)
    other = _artifact_job(kind="sql_script", script="SELECT 1")
    supervisor._insert(other)
    toolbuilder._save(toolbuilder._row(a["id"]), job_id=other["id"])

    got = toolbuilder.get_artifact(a["id"], user=ADMIN)["artifact"]
    assert got["status"] == "awaiting_approval"          # not registered
    assert got["server_name"] is None
    assert supervisor._get(sub["job"]["id"])["status"] == "awaiting_approval"
