"""Lakehouse write→read loop — supervisor wiring for Spark and platform jobs.

The whole feature stays DORMANT without AWS creds + a Spark endpoint, so there
is no live infra here: submit_spark_job, the platform, and the objectstore
bridge (objectstore.register_output) are monkeypatched. What is proved is the
WIRING owned by supervisor.py:

  * on genuine (post-approval) SUCCESS the supervisor CALLS the bridge for a
    declared output and reports the registered dataset;
  * fail-safe (bad output surfaced, not registered; no output = no call) and
    idempotency (re-poll does not re-register);
  * a Spark body is checked BEFORE it is posted, and Studio's private `output`
    key is never sent to the Jobs API.

Run from the backend directory:
    python -m pytest tests/test_spark_output_bridge.py -q
"""
import json
import os
import tempfile

os.environ["STUDIO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="studio-spark-bridge-test-"), "studio.db")

import time
import uuid

import pytest

from app import db, email_service, supervisor
from app.connectors import databricks_conn, get_connector, objectstore

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
WAREHOUSE = "wh-0123456789abcdef"

# A distinctively named source dataset (avoids collision with other test
# modules' "orders" rows) inserted straight into the shared DB, where it wins
# over any env bootstrap. The allowed prefix derives from its URI:
# s3://acme/orders/ .
SRC_NAME = "lake_orders_src"
SRC_URI = "s3://acme/orders/*.parquet"
OUT_NAME = f"{SRC_NAME}_out"
OUT_URI = f"s3://acme/orders/{OUT_NAME}/*.parquet"



def _persist_user(user):
    """Approval re-checks the requester's CURRENT account, so the people these
    tests submit as must exist, as every real requester does."""
    with db.connect() as c:
        # Test modules share one database, and several use the same email under
        # different ids; taking the real one here broke whichever module came
        # later. Approval looks the requester up by id, so an id-derived
        # address — unique by construction — is all these rows need.
        email = f"{user['id']}@persisted.test"
        c.execute(
            "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (user["id"], email, db.UNUSABLE_PASSWORD_HASH, user["name"],
             user["role"], 1, time.time()))
        c.commit()


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    supervisor.init_tables()
    objectstore.init_tables()
    _persist_user(ADMIN)
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
    # A Spark submission needs the warehouse that runs its sql_tasks.
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

    good = {"run_name": "studio: x", "tasks": [{"task_key": "enrich", "sql_task": {
        "warehouse_id": WAREHOUSE, "query": {"query_text": "SELECT 1"}}}]}
    good["output"] = {"name": "orders_out", "uri": OUT_URI}
    assert conn.submit_spark_job(good) == {"run_id": 7}
    assert len(posted) == 1
    assert "output" not in posted[0]           # Studio-private key never sent
    assert posted[0]["tasks"][0]["sql_task"]["warehouse_id"] == WAREHOUSE
