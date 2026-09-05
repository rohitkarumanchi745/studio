"""Lakehouse write→read loop — flow + supervisor wiring.

The whole feature stays DORMANT without AWS creds + a Spark endpoint, so there
is no live infra here: submit_spark_job, the platform, and the objectstore
bridge (objectstore.register_output) are monkeypatched. What is proved is the
WIRING owned by flow.py + supervisor.py:

  * a spark_job flow DECLARES an S3 parquet output, it threads through
    request_approval into the job script, and on genuine (post-approval)
    SUCCESS the supervisor CALLS the bridge with an in-prefix URI and reports
    the registered dataset;
  * a default sink is derived under the source dataset's own prefix;
  * codegen is steered to read the source URI and write the sink dir;
  * _pipeline_view carries the output + registered dataset (and the refused
    error) so the Flow graph can draw the closed loop;
  * fail-safe (bad output surfaced, not registered; no output = no call) and
    idempotency (re-poll does not re-register);
  * the Spark payload is a body the Databricks Jobs API would actually accept —
    one sql_task per verified step with a unique task_key and a warehouse — and
    a missing DATABRICKS_WAREHOUSE_ID is a refusal, never a doomed submission.

Run from the backend directory:
    python -m pytest tests/test_lakehouse_flow.py -q
"""
import json
import os
import tempfile

os.environ["STUDIO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="studio-lakehouse-test-"), "studio.db")

import time
import uuid

import pytest

from app import db, email_service, flow, pybuild, supervisor
from app.connectors import databricks_conn, get_connector, objectstore

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
WAREHOUSE = "wh-0123456789abcdef"

# A distinctively named source dataset (avoids collision with other test
# modules' "orders" rows) inserted straight into the shared DB, where it wins
# over any env bootstrap. The default output sink + allowed prefix derive from
# its URI: s3://acme/orders/ .
SRC_NAME = "lake_orders_src"
SRC_URI = "s3://acme/orders/*.parquet"
OUT_NAME = f"{SRC_NAME}_out"
OUT_URI = f"s3://acme/orders/{OUT_NAME}/*.parquet"

SPEC = flow.PipelineSpec(request="enrich orders", source="s3",
                         steps=[flow.Step(name="enrich", table=SRC_NAME,
                                          sql=f"SELECT * FROM {SRC_NAME}")],
                         matched_tables=[SRC_NAME])
VALID = flow.ValidationResult(ok=True, checks=[], errors=[])


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    supervisor.init_tables()
    objectstore.init_tables()
    flow.init_tables()
    c = db._conn()
    c.execute("DELETE FROM objectstore_datasets WHERE source='s3' AND name=?", (SRC_NAME,))
    c.execute("INSERT INTO objectstore_datasets (id, source, name, uri, format, created_at) "
              "VALUES (?,?,?,?,?,?)",
              (str(uuid.uuid4()), "s3", SRC_NAME, SRC_URI, "parquet", time.time()))
    c.commit()
    c.close()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: {"mode": "test"})
    # A Spark submission needs the warehouse that runs its sql_tasks; without it
    # the flow REFUSES to build a body (tested on its own below).
    monkeypatch.setenv(databricks_conn.WAREHOUSE_VAR, WAREHOUSE)


@pytest.fixture
def bridge(monkeypatch):
    """Record every objectstore bridge call and emulate its prefix confinement:
    an in-prefix URI registers, anything else is refused (as objectstore's real
    _valid + prefix check would). The supervisor calls the 3-arg wrapper
    register_spark_output(output, job, user) — patch THAT (not the generic
    register_output, whose real signature is (user, source, name, uri, ...)),
    which is the name the /live and _run success paths actually resolve.
    raising=False: works whether or not the wrapper has been added yet."""
    calls = []

    def fake_register(output, job, user):
        calls.append({"output": output, "job": job})
        if str(output.get("uri", "")).startswith("s3://acme/orders/"):
            return {"registered": output["name"]}
        return {"error": f"output URI {output.get('uri')} is outside the allowed prefix"}

    monkeypatch.setattr(objectstore, "register_spark_output", fake_register, raising=False)
    return calls


@pytest.fixture
def spark_ok(monkeypatch):
    """Databricks connector present + submit_spark_job succeeds (submit-success)."""
    conn = get_connector("databricks")
    monkeypatch.setattr(conn, "configured", lambda: True)
    monkeypatch.setattr(conn, "submit_spark_job", lambda cfg: {"run_id": 4242})
    return conn


# ── The loop: declared output → approve → bridge called, dataset reported ──

def test_spark_job_flow_registers_declared_output(bridge, spark_ok):
    output = flow._resolve_output(SPEC, None)          # default sink under prefix
    assert output["uri"] == OUT_URI
    assert output["from_dataset"] == SRC_NAME

    dep, _ = flow.request_approval(ADMIN, SPEC, VALID, "databricks", "spark_job", output)
    assert dep.decision == "needs_human"               # spark jobs are human-gated
    # The declared output rode into the job script (immutable), not lost.
    body = json.loads(dep.script)
    assert body["output"]["name"] == OUT_NAME
    # ...and what surrounds it is a submission the Jobs API would accept.
    databricks_conn.validate_submission(body)

    job = supervisor.submit("spark_job", "databricks", dep.script, ADMIN)
    assert job["status"] == "awaiting_approval"
    assert bridge == []                                # not on submit — only on success

    done = supervisor.approve(job["id"], ADMIN)        # human admin approves → runs
    assert done["status"] == "succeeded"
    # The bridge was called once, with the in-prefix URI and the approving job.
    assert len(bridge) == 1
    assert bridge[0]["output"]["uri"] == OUT_URI
    assert bridge[0]["job"]["human_by"] == ADMIN["email"]
    # ...and the registered dataset is reported back in the job result.
    assert done["result"]["registered_dataset"] == {"registered": OUT_NAME}


# ── Caller-declared output is honoured and back-filled ──

def test_declared_output_is_backfilled():
    out = flow._resolve_output(
        SPEC, {"name": "orders_enriched", "uri": "s3://acme/orders/enriched/*.parquet"})
    assert out["source"] == "s3"
    assert out["format"] == "parquet"
    assert out["from_dataset"] == SRC_NAME
    assert out["name"] == "orders_enriched"


# ── Codegen is steered to read the source URI and write the sink dir ──

def test_generate_steers_pyspark_recipe(monkeypatch):
    seen = {}

    def fake_build(body, user):
        seen["prompt"] = body.prompt
        return {"python": "print('spark')", "mode": "scaffold"}

    monkeypatch.setattr(pybuild, "build", fake_build)
    output = flow._resolve_output(SPEC, None)
    art, _ = flow.generate(ADMIN, SPEC, "spark_job", output)
    assert art.code
    assert SRC_URI in seen["prompt"]                              # reads source
    assert f"s3://acme/orders/{OUT_NAME}/" in seen["prompt"]      # writes sink dir
    assert "mode='overwrite'" in seen["prompt"]


# ── The Flow graph carries the loop ──

def test_pipeline_view_shows_output_and_dataset():
    dep = {"target": "databricks", "kind": "spark_job", "risk": "job",
           "decision": "needs_human",
           "script": json.dumps({"tasks": [], "output": {
               "source": "s3", "name": "orders_out",
               "uri": "s3://acme/orders/orders_out/*.parquet", "format": "parquet"}})}
    ex = {"executor": "x", "status": "succeeded",
          "result": {"run_id": 1, "registered_dataset": {"registered": "orders_out"}}}
    pv = flow._pipeline_view({"spec": SPEC.model_dump(), "validation": VALID.model_dump(),
                              "deployment": dep, "execution": ex})
    assert pv["output"]["name"] == "orders_out"
    assert pv["registered_dataset"] == "orders_out"
    assert pv["register_error"] is None


def test_pipeline_view_surfaces_register_error():
    dep = {"target": "databricks", "kind": "spark_job",
           "script": json.dumps({"tasks": [], "output": {"name": "x", "uri": "s3://x/*.parquet"}})}
    ex = {"status": "succeeded",
          "result": {"registered_dataset": {"error": "outside the allowed prefix"}}}
    pv = flow._pipeline_view({"spec": SPEC.model_dump(), "deployment": dep, "execution": ex})
    assert pv["registered_dataset"] is None
    assert "prefix" in pv["register_error"]


# ── Fail-safe + idempotency (supervisor bridge) ──

def test_out_of_prefix_output_is_refused_not_registered(bridge, spark_ok):
    # A spark job that declares a sink in someone else's bucket.
    script = json.dumps({"tasks": [], "output": {
        "source": "s3", "name": "evil", "uri": "s3://someone-elses-bucket/x/*.parquet",
        "format": "parquet", "from_dataset": "orders"}})
    job = supervisor.submit("spark_job", "databricks", script, ADMIN)
    done = supervisor.approve(job["id"], ADMIN)
    assert done["status"] == "succeeded"               # job still succeeds
    assert "error" in done["result"]["registered_dataset"]   # refused + surfaced
    assert bridge[-1]["output"]["name"] == "evil"      # bridge saw it and said no


def test_no_declared_output_means_no_registration(bridge, spark_ok):
    job = supervisor.submit("spark_job", "databricks",
                            json.dumps({"tasks": [{"name": "s", "sql": "SELECT 1"}]}), ADMIN)
    done = supervisor.approve(job["id"], ADMIN)
    assert done["status"] == "succeeded"
    assert "registered_dataset" not in (done["result"] or {})
    assert bridge == []                                # bridge never called


def test_bridge_is_idempotent(bridge, spark_ok):
    script = json.dumps({"tasks": [], "output": {
        "source": "s3", "name": "orders_out",
        "uri": "s3://acme/orders/orders_out/*.parquet", "format": "parquet"}})
    job = supervisor.submit("spark_job", "databricks", script, ADMIN)
    row = supervisor._get(job["id"])
    row["human_by"] = ADMIN["email"]
    row["result"] = json.dumps({"run_id": 1})
    first = supervisor._bridge_output(row, ADMIN)
    second = supervisor._bridge_output(row, ADMIN)     # re-poll after success
    assert first == {"registered": "orders_out"}
    assert second == first
    assert len(bridge) == 1                            # not re-registered / re-logged


# ── platform_run: /live registers only on GENUINE terminal success ──

def test_platform_run_registers_on_live_success(bridge, monkeypatch):
    from app import platforms

    class FakePlatform:
        def status(self, run_ref):
            return {"state": "succeeded", "detail": "done", "url": "u", "metrics": {}}

        def logs(self, run_ref):
            return "log"

        def quality(self, run_ref):
            return []

    monkeypatch.setattr(platforms, "get_platform", lambda name: FakePlatform())

    now = supervisor.time.time()
    job = {
        "id": "job-live", "user_id": ADMIN["id"], "requester_role": "admin",
        "requester_email": ADMIN["email"], "kind": "platform_run",
        "target": "databricks_jobs",
        "script": json.dumps({"tasks": [], "output": {
            "source": "s3", "name": "orders_out",
            "uri": "s3://acme/orders/orders_out/*.parquet", "format": "parquet"}}),
        "risk": "job", "status": "running", "supervisor_decision": "needs_human",
        "supervisor_reasons": json.dumps([]), "attempts": 0, "max_retries": 2,
        "last_error": None, "result": json.dumps({"run_ref": "r-1"}),
        "human_by": ADMIN["email"], "created_at": now, "updated_at": now,
    }
    supervisor._insert(job)

    live = supervisor.live_job("job-live", ADMIN)
    assert live["state"] == "succeeded"
    assert len(bridge) == 1                            # registered on real terminal success
    assert live["job"]["result"]["registered_dataset"] == {"registered": "orders_out"}
    # A second poll must not re-register (idempotent).
    supervisor.live_job("job-live", ADMIN)
    assert len(bridge) == 1


# ── The Spark body is a submission the Jobs API would accept ─────────────

TWO_STEPS = flow.PipelineSpec(
    request="stage then enrich orders", source="s3",
    steps=[flow.Step(name="stage orders", table=SRC_NAME, sql=f"SELECT * FROM {SRC_NAME}"),
           flow.Step(name="enrich orders", table=SRC_NAME,
                     sql=f"SELECT 1 FROM {SRC_NAME}")],
    matched_tables=[SRC_NAME])


def test_spark_payload_is_a_real_jobs_submission():
    """Every verified step becomes a sql_task with a task_key and the configured
    warehouse. The old body ({"tasks": [{"name", "sql"}]}) carried none of the
    three things the Jobs API requires, so it could only ever 400."""
    dep, _ = flow.request_approval(ADMIN, TWO_STEPS, VALID, "databricks", "spark_job")
    assert dep.decision == "needs_human"          # the gate is unchanged
    body = json.loads(dep.script)

    databricks_conn.validate_submission(body)     # round-trips the shape assertion
    assert body["run_name"].startswith("studio: ")
    assert len(body["tasks"]) == 2
    keys = [t["task_key"] for t in body["tasks"]]
    assert keys == ["stage_orders", "enrich_orders"]
    for t, step in zip(body["tasks"], TWO_STEPS.steps):
        assert set(t) == {"task_key", "sql_task"}          # a task type, nothing else
        assert t["sql_task"]["warehouse_id"] == WAREHOUSE  # compute is present
        assert t["sql_task"]["query"]["query_text"] == step.sql
    # No stray legacy keys the endpoint would reject.
    assert all("name" not in t and "sql" not in t for t in body["tasks"])


def test_duplicate_step_names_yield_unique_task_keys():
    """Two steps that slug to the same key must stay two tasks: the API rejects
    a duplicate task_key, and collapsing them would silently drop a verified
    statement."""
    spec = flow.PipelineSpec(
        request="twice", source="s3",
        steps=[flow.Step(name="load!", sql="SELECT 1"),
               flow.Step(name="load?", sql="SELECT 2"),
               flow.Step(name="load", sql="SELECT 3")],
        matched_tables=[])
    body = json.loads(flow.request_approval(
        ADMIN, spec, VALID, "databricks", "spark_job")[0].script)
    keys = [t["task_key"] for t in body["tasks"]]
    assert len(keys) == len(set(keys)) == 3
    databricks_conn.validate_submission(body)
    # Every statement survived, one per task.
    assert [t["sql_task"]["query"]["query_text"] for t in body["tasks"]] == [
        "SELECT 1", "SELECT 2", "SELECT 3"]


def test_missing_warehouse_is_refused_not_submitted(monkeypatch):
    """No DATABRICKS_WAREHOUSE_ID ⇒ the request is REFUSED with that variable
    named. Nothing is supervised and nothing is posted: a body the API would
    reject is worse than an honest "not configured"."""
    monkeypatch.delenv(databricks_conn.WAREHOUSE_VAR, raising=False)

    def boom(*a, **k):                       # policy is never even consulted
        raise AssertionError("supervise() must not run on an unbuildable payload")

    monkeypatch.setattr(supervisor, "supervise", boom)
    dep, _ = flow.request_approval(ADMIN, SPEC, VALID, "databricks", "spark_job")
    assert dep.decision == "reject"
    assert dep.script == ""
    assert databricks_conn.WAREHOUSE_VAR in " ".join(dep.reasons)


def test_submit_spark_job_validates_before_posting(monkeypatch):
    """submit_spark_job checks the body BEFORE any HTTP call, so an invalid
    payload never reaches the workspace — and the Studio-private `output` key
    is stripped from what is posted (the API rejects fields it does not know)."""
    import urllib.request

    posted = []

    class FakeResponse:
        def read(self):
            return b'{"run_id": 7}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        posted.append(json.loads(req.data.decode()))
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("DATABRICKS_SERVER_HOSTNAME", "example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "t-secret")
    conn = get_connector("databricks")

    # The pre-fix body: no task_key, no task type, no compute.
    with pytest.raises(ValueError):
        conn.submit_spark_job({"tasks": [{"name": "enrich", "sql": "SELECT 1"}]})
    assert posted == []                        # nothing was posted

    for bad in ({}, {"tasks": []},
                {"tasks": [{"task_key": "a", "sql_task": {"query": {"query_text": "SELECT 1"}}}]},
                {"tasks": [{"task_key": "a", "notebook_task": {"notebook_path": "/x"}}]},
                {"tasks": [{"task_key": "dup", "sql_task": {"warehouse_id": WAREHOUSE}},
                           {"task_key": "dup", "sql_task": {"warehouse_id": WAREHOUSE}}]}):
        with pytest.raises(ValueError):
            conn.submit_spark_job(bad)
    assert posted == []

    good = databricks_conn.build_submission([("enrich", "SELECT 1")], run_name="studio: x")
    good["output"] = {"name": "orders_out", "uri": OUT_URI}
    assert conn.submit_spark_job(good) == {"run_id": 7}
    assert len(posted) == 1
    assert "output" not in posted[0]           # Studio-private key never sent
    assert posted[0]["tasks"][0]["sql_task"]["warehouse_id"] == WAREHOUSE
