"""Authorization is decided on the user's access NOW, not when something was saved.

Two places trusted a past decision:
- an org-shared pipeline showed its prompt, step SQL and runs to every
  signed-in user, whether or not their role could read its tables (running it
  re-verified; viewing did not);
- approving a SQL-write, Spark or artifact job acted on the requester's role
  as stamped at submission, so access lost since then still counted.

Locked in here:
- a non-owner sees an org-shared pipeline — in the list, by id, its runs, or
  to run it — only when their CURRENT role may read every step (the same rule
  as a pipeline shared inside a chat); otherwise it is not found, as a private
  one is. The owner always sees their own. Access granted later shows it.
- approving any job re-checks the requester's current account: removed,
  unverified, or no longer able to use the target → refused, and the job keeps
  waiting. A job's reads run under the requester's current role.
"""
import json
import os
import tempfile
import time
import uuid

_TMP = tempfile.mkdtemp(prefix="studio-current-access-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException

from app import chat, db, governance, pipelines, supervisor
from app.connectors.demo import seed

ADMIN = {"id": "u-ca-admin", "email": "ca-admin@studio.test", "role": "admin", "name": "Ad"}
ANA = {"id": "u-ca-ana", "email": "ca-ana@studio.test", "role": "analyst", "name": "Ana"}
VIEWER = {"id": "u-ca-view", "email": "ca-view@studio.test", "role": "viewer", "name": "Vi"}

POLICY = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*" } }
  viewer: { sources: { demo: [sales, web_traffic] } }
"""


def _persist_user(user, verified=1):
    with db.connect() as c:
        c.execute("DELETE FROM users WHERE id=?", (user["id"],))
        c.execute(
            "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user["id"], user["email"], db.UNUSABLE_PASSWORD_HASH, user["name"],
             user["role"], verified, time.time()))
        c.commit()


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    pipelines.init_tables()
    supervisor.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def _people():
    governance._set(POLICY, "test")
    for user in (ADMIN, ANA, VIEWER):
        _persist_user(user)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


# ── Org-shared pipelines ─────────────────────────────────────────────────

def _pipeline(owner, sql, visibility="org", name="p"):
    pid = str(uuid.uuid4())
    steps = [{"name": "s1", "source": "demo", "table": "*", "sql": sql}]
    with db.connect() as c:
        c.execute("INSERT INTO pipelines (id, user_id, name, prompt, source, steps, visibility, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (pid, owner["id"], name, f"prompt for {name}", "demo", json.dumps(steps),
                   visibility, time.time(), time.time()))
        c.commit()
    return pid


def _listed(user):
    return {p["id"] for p in pipelines.listing(user=user)["pipelines"]}


def test_an_org_shared_pipeline_on_tables_the_reader_cannot_see_is_not_found():
    secret = _pipeline(ANA, "SELECT segment, COUNT(*) FROM customers GROUP BY 1", name="secret")
    ok = _pipeline(ANA, "SELECT region, SUM(revenue) FROM sales GROUP BY 1", name="ok")
    # The viewer may read sales but not customers.
    assert ok in _listed(VIEWER) and secret not in _listed(VIEWER)
    assert pipelines.get(ok, user=VIEWER)["prompt"] == "prompt for ok"
    for call in (lambda: pipelines.get(secret, user=VIEWER),
                 lambda: pipelines.runs(secret, user=VIEWER),
                 lambda: pipelines.trigger(secret, user=VIEWER)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 404                      # not found, not forbidden
    # The owner and a role that can read customers still see it.
    assert secret in _listed(ANA) and secret in _listed(ADMIN)
    assert pipelines.get(secret, user=ADMIN)["prompt"] == "prompt for secret"


def test_access_granted_later_shows_it_and_a_private_one_stays_private():
    pid = _pipeline(ANA, "SELECT city FROM customers", name="later")
    private = _pipeline(ANA, "SELECT region FROM sales", visibility="private", name="mine")
    assert pid not in _listed(VIEWER)
    governance._set(POLICY.replace("[sales, web_traffic]", "[sales, web_traffic, customers]"), "test")
    assert pid in _listed(VIEWER)                              # decided now, not at save
    assert private not in _listed(VIEWER) and private not in _listed(ADMIN)


def test_chat_shares_the_same_rule():
    assert chat._stored_pipeline_step_allowed is pipelines.stored_step_allowed
    assert pipelines.stored_step_allowed("viewer", {"source": "demo", "sql": "SELECT 1 FROM sales"})
    assert not pipelines.stored_step_allowed("viewer", {"source": "demo",
                                                        "sql": "SELECT 1 FROM customers"})


# ── Job approval ─────────────────────────────────────────────────────────

WRITE = "INSERT INTO sales_archive SELECT * FROM sales"


def _awaiting():
    job = supervisor.submit(supervisor.SQL_KIND, "demo", WRITE, ANA, notify=False)
    assert job["status"] == "awaiting_approval"
    return job


def _refused(job):
    with pytest.raises(HTTPException) as e:
        supervisor.approve(job["id"], user=ADMIN)
    assert e.value.status_code == 403
    assert supervisor._get(job["id"])["status"] == "awaiting_approval"   # still waiting


def test_approving_a_write_rechecks_the_requester_now():
    job = _awaiting()
    governance._set(POLICY.replace("analyst: { sources: { demo: \"*\" } }",
                                   "analyst: { sources: {} }"), "test")
    _refused(job)                                  # lost access to the target since


def test_a_removed_or_unverified_requester_cannot_be_approved():
    job = _awaiting()
    _persist_user(ANA, verified=0)
    _refused(job)
    with db.connect() as c:
        c.execute("DELETE FROM users WHERE id=?", (ANA["id"],))
        c.commit()
    _refused(job)


def test_a_requester_who_still_has_access_is_approved():
    job = _awaiting()
    out = supervisor.approve(job["id"], user=ADMIN)
    assert out["human_by"] == ADMIN["email"]
    assert out["status"] != "awaiting_approval"


def test_a_jobs_reads_run_under_the_requesters_current_role():
    job = supervisor._get(_awaiting()["id"])
    assert supervisor._requester(job)["role"] == "analyst"
    with db.connect() as c:
        c.execute("UPDATE users SET role='viewer' WHERE id=?", (ANA["id"],))
        c.commit()
    assert supervisor._requester(job)["role"] == "viewer"          # demoted since
    assert job["requester_role"] == "analyst"                       # the record stays
    with db.connect() as c:
        c.execute("DELETE FROM users WHERE id=?", (ANA["id"],))
        c.commit()
    assert supervisor._requester(job)["role"] is None                # no access at all
