"""Authorization is decided on the user's access NOW, not when something was saved.

Approving a SQL-write, Spark or artifact job used to act on the requester's
role as stamped at submission, so access lost since then still counted.

Locked in here:
- stored SQL shown from history is checked against the reader's CURRENT role;
- approving any job re-checks the requester's current account: removed,
  unverified, or no longer able to use the target → refused, and the job keeps
  waiting. A job's reads run under the requester's current role.
"""
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="studio-current-access-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException

from app import chat, db, governance, stored_access, supervisor
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


# ── Stored SQL ───────────────────────────────────────────────────────────

def test_chat_checks_stored_sql_against_the_current_role():
    assert chat._stored_step_allowed is stored_access.stored_step_allowed
    assert stored_access.stored_step_allowed("viewer", {"source": "demo", "sql": "SELECT 1 FROM sales"})
    assert not stored_access.stored_step_allowed("viewer", {"source": "demo",
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
