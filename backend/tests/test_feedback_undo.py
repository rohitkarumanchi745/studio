"""A 👍/👎 can be taken back.

Feedback overwrote a run's reward and could never be undone. Score 0 now
takes it back. Locked in here:
- the run returns to its own heuristic score, rebuilt from the stored trace
  exactly as it was first scored (so votes cast before this change undo too);
- its part in the semantic-cache aggregate is put back: a replaced
  contribution is replaced back, an added one is removed — and a later vote
  adds again;
- taking back is idempotent, owner-only, and bumps the training revision so
  the trainer sees the run as unrated again.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-feedback-undo-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import chat, db, lightning, qcache
from app.connectors.demo import seed

ANA = {"id": "u-fb-ana", "email": "fb-ana@studio.test", "role": "analyst"}
BO = {"id": "u-fb-bo", "email": "fb-bo@studio.test", "role": "analyst"}
SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1"
PROMPT = "revenue by region for the feedback test"


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    qcache.init_tables()
    seed()
    yield


def _trace(tid):
    with db.connect() as c:
        return dict(c.execute("SELECT * FROM agent_traces WHERE id=?", (tid,)).fetchone())


def _cache(cid):
    with db.connect() as c:
        return dict(c.execute("SELECT seen, avg_reward FROM query_cache WHERE id=?", (cid,)).fetchone())


@pytest.mark.parametrize("result", [
    {"mode": "agent", "text": "EU leads.", "sql": SQL, "rows": [["EU", 1]],
     "chart": {"type": "bar"}, "panels": [{}, {}], "errors": ["retry"]},
    {"mode": "agent", "text": "(Agent error: boom)", "sql": None, "rows": [], "chart": None,
     "panels": [], "errors": []},
    {"mode": "agent", "text": "No data.", "sql": SQL, "rows": [], "chart": {"type": "table"},
     "panels": [], "errors": ["a", "b", "c"]},
    {"mode": "fallback", "text": "Preview.", "sql": SQL, "rows": [["x", 1]], "chart": None,
     "panels": [], "errors": []},
])
def test_the_score_rebuilt_from_a_trace_is_the_score_it_was_given(result):
    tid = lightning.record_chat_trace(ANA, None, PROMPT, result, 120)
    row = _trace(tid)
    import json
    assert lightning.heuristic_reward_of_trace(row, json.loads(row["meta"])) == \
        lightning.heuristic_reward(result) == row["reward"]


def _voted_run(reward=0.9):
    tid = db.add_trace(ANA, prompt=PROMPT, mode="agent", source="demo", table="sales",
                       sql=SQL, ok=True, row_count=3, chart_type="bar", panel_count=2,
                       reward=reward, reward_source="heuristic", meta={"errors": ["retry"]})
    return tid


def test_taking_feedback_back_restores_the_runs_own_score():
    tid = _voted_run()
    before = _trace(tid)
    chat.feedback(chat.Feedback(trace_id=tid, score=-1, note="wrong region"), user=ANA)
    voted = _trace(tid)
    assert (voted["reward"], voted["reward_source"]) == (0.0, "user")
    assert "wrong region" in voted["meta"]

    out = chat.feedback(chat.Feedback(trace_id=tid, score=0), user=ANA)
    assert out == {"ok": True, "cleared": True}
    back = _trace(tid)
    assert (back["reward"], back["reward_source"]) == (before["reward"], "heuristic")
    assert "wrong region" not in back["meta"]
    assert back["training_revision"] > voted["training_revision"]   # the trainer re-reads it
    # Idempotent: nothing left to take back.
    assert chat.feedback(chat.Feedback(trace_id=tid, score=0), user=ANA)["cleared"]
    assert _trace(tid)["reward"] == before["reward"]


def test_only_the_owner_can_take_it_back_and_scores_are_bounded():
    tid = _voted_run()
    chat.feedback(chat.Feedback(trace_id=tid, score=1), user=ANA)
    with pytest.raises(HTTPException) as e:
        chat.feedback(chat.Feedback(trace_id=tid, score=0), user=BO)
    assert e.value.status_code == 404
    assert _trace(tid)["reward_source"] == "user"
    with pytest.raises(ValidationError):
        chat.Feedback(trace_id=tid, score=2)


def test_a_replaced_cache_contribution_is_replaced_back():
    result = {"sql": SQL, "text": "Revenue", "rows": [["West", 10]], "chart": None,
              "panels": [], "errors": []}
    cache_id = qcache.store(ANA, "demo", "sales", PROMPT + " replace", result, reward=0.75)
    tid = db.add_trace(ANA, prompt=PROMPT, mode="agent", source="demo", table="sales",
                       sql=SQL, ok=True, row_count=1, reward=0.75, reward_source="heuristic",
                       meta={"qcache_id": cache_id})
    start = _cache(cache_id)
    assert db.set_trace_reward(tid, 0.0, user_id=ANA["id"], source="user")
    assert _cache(cache_id)["avg_reward"] == pytest.approx(0.0)
    chat.feedback(chat.Feedback(trace_id=tid, score=0), user=ANA)
    restored = lightning.heuristic_reward_of_trace(_trace(tid), {})
    assert _cache(cache_id) == {"seen": start["seen"],
                                "avg_reward": pytest.approx(restored)}


def test_an_added_cache_contribution_is_removed_and_a_new_vote_adds_again():
    result = {"sql": SQL, "text": "Revenue", "rows": [["West", 10]], "chart": None,
              "panels": [], "errors": []}
    cache_id = qcache.store(ANA, "demo", "sales", PROMPT + " add", result, reward=0.75)
    start = _cache(cache_id)
    tid = db.add_trace(ANA, prompt=PROMPT, mode="cached", source="demo", table="sales",
                       sql=SQL, reward=None, reward_source="heuristic",
                       meta={"qcache_id": cache_id, "qcache_feedback_mode": "add"})
    assert db.set_trace_reward(tid, 1.0, user_id=ANA["id"], source="user")
    assert _cache(cache_id)["seen"] == start["seen"] + 1
    chat.feedback(chat.Feedback(trace_id=tid, score=0), user=ANA)
    assert _cache(cache_id) == {"seen": start["seen"], "avg_reward": pytest.approx(start["avg_reward"])}
    assert _trace(tid)["reward"] is None                 # a cache hit had no score of its own
    # Voting again counts once more, as a first vote did.
    assert db.set_trace_reward(tid, 0.0, user_id=ANA["id"], source="user")
    assert _cache(cache_id)["seen"] == start["seen"] + 1
