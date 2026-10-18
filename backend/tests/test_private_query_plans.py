"""Cached query plans are private: a plan is reused only for the user who
asked it. Reuse across users goes through the Verified SQL library
(test_verified_sql_first.py), never this cache. No reply carries another
user's prompt.

The gateway re-checks the asker's permissions on every replay either way —
this is about whose question and context a plan carries, not table access.
"""
import importlib
import json
import time
import uuid

import pytest

ALICE = {"id": "u-alice", "role": "analyst", "email": "alice@x.test"}
BOB = {"id": "u-bob", "role": "analyst", "email": "bob@x.test"}


@pytest.fixture()
def qc(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "plans.db"))
    monkeypatch.delenv("HARRIER_EMBED_URL", raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import qcache, queries
    _db.init_db()
    qcache.init_tables()
    queries.init_tables()
    ran = []
    monkeypatch.setattr(qcache, "_exec_full",
                        lambda user, source, sql: ran.append((user["id"], sql)) or (["n"], [[1]]))
    qcache.ran = ran
    return qcache


def _store(qc, user, prompt, sql="SELECT region, SUM(revenue) FROM sales GROUP BY 1"):
    return qc.store(user, "demo", "sales", prompt, {"sql": sql, "rows": [[1]], "text": "ok"},
                    reward=1.0)


def _share(qc, author, prompt, sql, *, visibility="org", verified=1, title="Revenue by region"):
    now = time.time()
    with qc.db.connect() as c:
        c.execute("INSERT INTO saved_queries (id, user_id, title, prompt, sql, source, "
                  "table_label, verified, visibility, author_role, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (str(uuid.uuid4()), author["id"], title, prompt, sql, "demo", "sales",
                   verified, visibility, author["role"], now, now))
        c.commit()


def test_a_plan_is_reused_only_for_the_user_who_asked_it(qc):
    _store(qc, ALICE, "revenue by region for my Q3 board deck")
    assert qc.lookup(BOB, "demo", "sales", "revenue by region for my Q3 board deck") is None
    own = qc.lookup(ALICE, "demo", "sales", "revenue by region for my Q3 board deck")
    assert own and own["cached"]["from_prompt"] == "revenue by region for my Q3 board deck"


def test_the_cache_never_replays_another_users_plan_even_when_shared(qc):
    _store(qc, ALICE, "revenue by region")
    _share(qc, ALICE, "revenue by region", "SELECT region, SUM(revenue) FROM sales GROUP BY 1")
    assert qc.lookup(BOB, "demo", "sales", "revenue by region") is None   # the library's job


def test_plans_from_before_owners_were_kept_are_never_replayed(qc):
    now = time.time()
    with qc.db.connect() as c:
        c.execute("INSERT INTO query_cache (id, role, source, table_scope, prompt, signature, "
                  "sql, chart, text, hits, seen, avg_reward, embedding, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (str(uuid.uuid4()), "analyst", "demo", "sales", "units by store",
                   json.dumps(qc._sig("units by store")), "SELECT 1", None, "ok", 0, 3, 1.0,
                   None, now, now))
        c.commit()
    assert qc.lookup(ALICE, "demo", "sales", "units by store") is None
    assert qc.scope_stats()["total_patterns"] == 1          # still counted for learning


def test_each_users_feedback_shapes_only_their_own_plan(qc):
    a = _store(qc, ALICE, "units by store")
    b = _store(qc, BOB, "units by store")
    assert a and b and a != b
