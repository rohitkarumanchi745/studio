"""The flow reports what it ACTUALLY ran — never what it merely generated.

Studio's standing invariant is that it never executes model-generated Python
in-process. The flow honours that (nothing here executes an artifact) but used
to hide it: the Validator only compile()d the artifact under a check called
"python syntax", the executor deployed the RECONSTRUCTED SQL steps, and a run
whose generated Python would raise on its first line was still filed as a
plain "succeeded" with the artifact sitting next to a green tick.

These tests pin the reporting, not new behaviour:

  * the artifact is PARSED, never run — proved with an artifact that would
    write a marker file if anything ever executed it (it must not exist);
  * the check is named "python syntax (static only — not executed)" and
    ValidationResult.artifact_status says which of parse / guard / nothing
    happened;
  * DeploymentRequest declares what it deploys (`deploys`) and states
    `artifact_deployed=False`, and its script is the verified SQL — the
    artifact's code never appears in it;
  * an end-to-end run that produced a Python artifact is
    "succeeded_sql_only", and the pipeline view and the digest email both say
    the SQL steps ran and the artifact did not.

No LLM key is needed: pybuild.build is monkeypatched, and the deploy target is
the demo warehouse, whose sql_script deploy is a read-only auto-approval.

Run from the backend directory:
    python -m pytest tests/test_flow_honesty.py -q
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-flowhonesty-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app import db, email_service, flow, governance, pipelines, pybuild, queries, supervisor
from app.connectors.demo import seed

ANALYST = {"id": "u-fh-analyst", "email": "ana@studio.test", "role": "analyst", "name": "A"}

MARKER = os.path.join(_TMP, "artifact-was-executed")

# Compiles cleanly, and would blow up (after leaving a marker) the instant
# anything ran it. Nothing in Studio ever does.
EXPLODING_PY = (
    "import pathlib\n"
    f"pathlib.Path({MARKER!r}).write_text('ran')\n"
    "raise RuntimeError('this artifact fails at runtime')\n"
)

SPEC = flow.PipelineSpec(
    request="monthly revenue by region", source="demo",
    steps=[flow.Step(name="Extract sales", table="sales",
                     sql="SELECT region, SUM(revenue) AS total_revenue "
                         "FROM sales GROUP BY region")],
    matched_tables=["sales"])


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    queries.init_tables()
    pipelines.init_tables()
    supervisor.init_tables()
    flow.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    """Built-in RBAC, no LLM key, and every outbound email captured."""
    governance._STATE.update(doc=None, yaml="", source=None)
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(v, raising=False)
    sent = []

    def capture(to, subject, html, **k):
        sent.append({"to": to, "subject": subject, "html": html})
        return {"mode": "test"}

    monkeypatch.setattr(email_service, "send", capture)
    yield sent
    governance._STATE.update(doc=None, yaml="", source=None)
    if os.path.exists(MARKER):
        os.remove(MARKER)


def _artifact(code=EXPLODING_PY, language="python"):
    return flow.GeneratedArtifact(language=language, code=code, mode="scaffold")


# ── The artifact is parsed, never executed ─────────────────────────────

def test_python_artifact_is_parsed_not_executed():
    res, _ = flow.validate(ANALYST, SPEC, _artifact())
    assert res.ok is True                       # it PARSES — that is all
    assert res.artifact_status == "syntax_checked_not_executed"
    check = next(c for c in res.checks if not c.name.startswith("step: "))
    assert check.name == "python syntax (static only — not executed)"
    assert check.ok is True and "not run" in (check.detail or "")
    # The proof: an artifact that writes this file the moment it runs.
    assert not os.path.exists(MARKER), "the flow executed the generated artifact"


def test_a_syntax_error_still_fails_validation_under_the_new_name():
    res, _ = flow.validate(ANALYST, SPEC, _artifact("def broken(:\n"))
    assert res.ok is False
    assert res.artifact_status == "syntax_checked_not_executed"
    assert any(e.startswith("python syntax (static only") for e in res.errors)


def test_sql_artifact_is_guarded_and_an_empty_one_is_neither():
    res, _ = flow.validate(ANALYST, SPEC, _artifact("SELECT 1", language="sql"))
    assert res.artifact_status == "sql_guarded"
    res, _ = flow.validate(ANALYST, SPEC, _artifact(""))
    assert res.artifact_status == "none"


# ── The deployment declares what it deploys ────────────────────────────

def test_deployment_deploys_the_sql_steps_never_the_artifact():
    valid = flow.ValidationResult(ok=True, artifact_status="syntax_checked_not_executed")
    dep, _ = flow.request_approval(ANALYST, SPEC, valid, "demo", "sql_script")
    assert dep.deploys == "sql_steps"
    assert dep.artifact_deployed is False
    assert dep.script == SPEC.steps[0].sql          # the verified SQL, verbatim
    assert "RuntimeError" not in dep.script         # ...and none of the artifact


def test_spark_job_declares_a_spark_deploy_and_still_no_artifact():
    valid = flow.ValidationResult(ok=True, artifact_status="syntax_checked_not_executed")
    dep, _ = flow.request_approval(ANALYST, SPEC, valid, "databricks", "spark_job")
    assert dep.deploys == "spark_job"
    assert dep.artifact_deployed is False


def test_a_rejected_deployment_also_carries_the_declaration():
    invalid = flow.ValidationResult(ok=False, errors=["step: nope"],
                                    artifact_status="syntax_checked_not_executed")
    dep, _ = flow.request_approval(ANALYST, SPEC, invalid, "demo", "sql_script")
    assert dep.decision == "reject"
    assert dep.deploys == "sql_steps" and dep.artifact_deployed is False


def test_deployed_note_names_what_ran():
    dep = flow.DeploymentRequest(target="demo", kind="sql_script", deploys="sql_steps")
    note = flow.deployed_note(SPEC, dep, "syntax_checked_not_executed", "succeeded")
    assert "deployed the 1 verified SQL step" in note
    assert "was not executed" in note
    # No artifact → no caveat to make.
    assert "not executed" not in flow.deployed_note(SPEC, dep, "none", "succeeded")
    # Nothing shipped → say nothing shipped.
    assert flow.deployed_note(SPEC, dep, "none", "deploy_failed").startswith("nothing was deployed")
    spark = flow.DeploymentRequest(target="databricks", kind="spark_job", deploys="spark_job")
    assert "Spark job" in flow.deployed_note(SPEC, spark, "none", "succeeded")


# ── End to end: a run with a Python artifact is not a plain success ────

def _run(monkeypatch, code):
    monkeypatch.setattr(pybuild, "build",
                        lambda body, user: {"python": code, "mode": "scaffold"})
    return flow.run_flow(ANALYST, "monthly revenue by region", "demo", "sql_script")


def test_run_with_a_python_artifact_is_succeeded_sql_only(monkeypatch, mailbox):
    out = _run(monkeypatch, EXPLODING_PY)
    # The SQL really did deploy and run...
    assert out["execution"]["status"] == "succeeded"
    assert out["deployment"]["deploys"] == "sql_steps"
    assert out["deployment"]["artifact_deployed"] is False
    # ...but the headline must not read as if the artifact ran too.
    assert out["status"] == "succeeded_sql_only"
    assert not os.path.exists(MARKER), "the flow executed the generated artifact"

    pv = out["pipeline"]
    assert pv["artifact_deployed"] is False
    assert pv["artifact_status"] == "syntax_checked_not_executed"
    assert "verified SQL step" in pv["deployed"]
    assert "was not executed" in pv["deployed"]
    assert out["execution"]["deployed"] == pv["deployed"]

    # The stage view says it too, on the stage that makes the artifact.
    codegen = next(s for s in out["stages"] if s["stage"] == "codegen")
    assert "does not execute generated Python" in codegen["note"]
    execute = next(s for s in out["stages"] if s["stage"] == "execute")
    assert execute["deployed"] == pv["deployed"]

    # ...and so does the digest email: no "succeeded" full stop.
    digest = [m for m in mailbox if m["subject"].startswith("Studio pipeline")][-1]
    assert "succeeded sql only" in digest["subject"]
    assert "was not executed" in digest["html"]


def test_a_refetched_run_reports_the_same_thing(monkeypatch, mailbox):
    out = _run(monkeypatch, EXPLODING_PY)
    again = flow.get(out["id"], ANALYST)
    assert again["status"] == "succeeded_sql_only"
    assert again["validation"]["artifact_status"] == "syntax_checked_not_executed"
    assert again["pipeline"]["deployed"] == out["pipeline"]["deployed"]
    assert again["pipeline"]["artifact_deployed"] is False


def test_a_run_with_no_python_artifact_is_still_a_plain_success(monkeypatch, mailbox):
    """The new status is scoped to runs that produced an artifact — a flow with
    nothing generated must keep reading exactly as it did before."""
    out = _run(monkeypatch, "")
    assert out["status"] == "succeeded"
    assert out["validation"]["artifact_status"] == "none"
    assert "not executed" not in (out["pipeline"]["deployed"] or "")
