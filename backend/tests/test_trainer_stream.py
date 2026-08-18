"""trainer.stream() must expose each rollout's `source` (and `tbl`) so the online
trainer can condition per-source (dialect + schema) instead of training source-
blind — a Databricks sample must never teach the sqlite/demo policy. The columns
already exist on agent_traces and add_trace stores them; this pins that stream()
surfaces them, additively, without dropping any pre-existing key.

Run from the backend directory:  python -m pytest tests/test_trainer_stream.py -q
"""
import os
import tempfile

# Point the app at a throwaway SQLite file BEFORE app.db computes DB_PATH.
os.environ["STUDIO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="studio-trainer-stream-test-"), "studio.db")

from app import db, trainer

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}

# Keys every existing /training/rollouts consumer already relies on — adding
# source/tbl must not drop any of these.
LEGACY_KEYS = {"id", "created_at", "user_id", "role", "prompt", "action",
               "reward", "reward_source", "mode", "agents"}


def _seed():
    db.init_db()
    c = db._conn()
    c.execute("DELETE FROM agent_traces")   # isolate: tests share the DB file
    c.commit()
    c.close()
    # Two different warehouses with different dialects; each a rewarded rollout.
    db.add_trace(ADMIN, prompt="revenue by month", mode="agent",
                 source="databricks", table="sales",
                 sql="SELECT date_trunc('month', ts) m, sum(amt) FROM sales GROUP BY 1",
                 reward=0.9, reward_source="heuristic")
    db.add_trace(ADMIN, prompt="revenue by month", mode="agent",
                 source="demo", table="sales",
                 sql="SELECT strftime('%Y-%m', ts) m, sum(amt) FROM sales GROUP BY 1",
                 reward=0.8, reward_source="heuristic")
    # A rewardless trace must still be excluded (unchanged behavior).
    db.add_trace(ADMIN, prompt="no reward here", mode="agent",
                 source="demo", table="sales", sql="SELECT 1", reward=None)


def test_stream_exposes_source_and_tbl():
    _seed()
    out = trainer.stream(since=0.0, limit=100)
    rollouts = out["rollouts"]
    # Only the two rewarded rollouts (rewardless one excluded, as before).
    assert len(rollouts) == 2
    for r in rollouts:
        assert "source" in r and "tbl" in r        # additive keys present
        assert LEGACY_KEYS.issubset(r.keys())      # no pre-existing key dropped
    by_source = {r["source"]: r for r in rollouts}
    assert set(by_source) == {"databricks", "demo"}
    # source travels with the right SQL/dialect, and tbl is carried through.
    assert "date_trunc" in by_source["databricks"]["action"]["sql"]
    assert "strftime" in by_source["demo"]["action"]["sql"]
    assert by_source["databricks"]["tbl"] == "sales"
    assert by_source["demo"]["tbl"] == "sales"


def test_stream_shape_unchanged_for_existing_consumers():
    """The cursor/count contract and the action sub-dict stay exactly as before."""
    _seed()
    out = trainer.stream(since=0.0, limit=100)
    assert set(out.keys()) == {"rollouts", "cursor", "count"}
    assert out["count"] == len(out["rollouts"]) == 2
    for r in out["rollouts"]:
        assert set(r["action"].keys()) == {"sql", "chart_type"}
