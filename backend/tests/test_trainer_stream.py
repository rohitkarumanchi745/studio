"""trainer.stream() must expose each rollout's `source` (and `tbl`) so the online
trainer can condition per-source (dialect + schema) instead of training source-
blind — a Databricks sample must never teach the sqlite/demo policy. The columns
already exist on agent_traces and add_trace stores them; this pins that stream()
surfaces them, additively, without dropping any pre-existing key.

Run from the backend directory:  python -m pytest tests/test_trainer_stream.py -q
"""
import concurrent.futures
import json

import pytest

from app import db, lightning, trainer

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}

# Keys every existing /training/rollouts consumer already relies on — adding
# source/tbl must not drop any of these.
LEGACY_KEYS = {"id", "created_at", "user_id", "role", "prompt", "action",
               "reward", "reward_source", "mode", "agents"}


@pytest.fixture(autouse=True)
def _isolated_database(tmp_path, monkeypatch):
    # app.db may already be imported by another collected test module. Set
    # the actual connection target, not an environment variable it no longer
    # reads, and give each test its own database rather than clearing a table.
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "trainer-stream.db"))
    monkeypatch.setattr(db, "IS_PG", False)


def _seed():
    db.init_db()
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


def test_stream_uses_exact_safe_agent_conditioning_and_keeps_root_prompt():
    db.init_db()
    db.add_trace(
        ADMIN, prompt="find customer spend", mode="agent:worker", source="demo",
        table="sales", sql="SELECT 1", reward=1.0,
        meta={"root_prompt": "find customer spend",
              "conditioning_prompt": "Question: summarize customer spend"})

    rollout = trainer.stream(since=0, limit=10)["rollouts"][0]
    assert rollout["prompt"] == "Question: summarize customer spend"
    assert rollout["root_prompt"] == "find customer spend"


def test_row_conditioned_graph_trace_is_redacted_and_never_streamed_or_emitted(
        tmp_path, monkeypatch):
    db.init_db()
    emitted = []
    monkeypatch.setattr(lightning, "_enqueue_emit", lambda trace_id: emitted.append(trace_id))
    sentinel = "private-account-8841"
    tid = lightning.record_agent_rollout(
        ADMIN, "conversation-1", "find customer spend", "Sales agent", "worker",
        {"text": "done", "sql": "SELECT 1", "columns": ["id"], "rows": [[1]],
         "chart": None, "panels": [], "errors": [], "_source": "demo"},
        conditioning_prompt=(
            f"REFERENCE DATA retrieved by another agent: [[{sentinel}]]\n"
            "Question: price those accounts"),
        graph_meta={
            "node_id": "seed_demo", "spawned_by": "seed", "depth": 1,
            "context_mode": "parent_rows", "spawned": ["leaf"],
            "spawn_requests": [{"source": "demo", "task": sentinel,
                                "context": "none", "reason": sentinel}],
            "spawn_rejections": [{"reason": sentinel}],
        })

    trace = lightning._trace(tid)
    encoded = json.dumps(trace["meta"])
    assert sentinel not in encoded
    assert trace["meta"]["conditioning_prompt"] == "find customer spend"
    assert trace["meta"]["conditioning_redacted"] == "parent_rows"
    assert trace["meta"]["global_train_eligible"] is False
    assert trace["meta"]["graph"] == {
        "node_id": "seed_demo", "spawned_by": "seed", "depth": 1,
        "dynamic": True, "context_mode": "parent_rows", "spawned": ["leaf"],
        "spawn_request_count": 1, "spawn_rejection_count": 1,
    }
    assert lightning.rollout_input(trace)["prompt"] == "find customer spend"
    assert emitted == []

    # A stale job created before this gate must also stop at delivery time.
    monkeypatch.setenv("STUDIO_AGL_URL", "https://agl.invalid")
    monkeypatch.setattr(lightning, "_schemas", lambda: pytest.fail(
        "ineligible trace reached the external delivery client"))
    assert lightning.emit_trace(tid) == {
        "skipped": "global_training_ineligible", "trace_id": tid}

    page = trainer.stream(since=0, limit=10)
    assert page["rollouts"] == []
    assert page["cursor"] == trace["training_revision"]
    path = tmp_path / "rollouts.jsonl"
    assert lightning.export_rollouts(path) == 0
    assert path.read_text() == ""


def test_dynamic_child_without_parent_rows_is_still_redacted_and_excluded(monkeypatch):
    """The parent can inline a row value in a context='none' child task."""
    db.init_db()
    emitted = []
    monkeypatch.setattr(lightning, "_enqueue_emit", lambda trace_id: emitted.append(trace_id))
    sentinel = "customer-secret-921"
    tid = lightning.record_agent_rollout(
        ADMIN, "conversation-1", "root question", "Sales agent", "worker",
        {"text": "done", "sql": "SELECT 1", "columns": [], "rows": [],
         "chart": None, "panels": [], "errors": [], "_source": "demo"},
        conditioning_prompt=f"look up {sentinel}",
        graph_meta={
            "node_id": "seed_demo", "spawned_by": "seed", "depth": 1,
            "dynamic": True, "context_mode": "none", "spawned": [],
        })

    trace = lightning._trace(tid)
    assert sentinel not in json.dumps(trace["meta"])
    assert trace["meta"]["conditioning_prompt"] == "root question"
    assert trace["meta"]["conditioning_redacted"] == "dynamic_task"
    assert trace["meta"]["global_train_eligible"] is False
    assert emitted == []
    assert trainer.stream(since=0, limit=10)["rollouts"] == []


def test_stream_skips_legacy_reference_context_and_advances_past_it():
    db.init_db()
    private_id = db.add_trace(
        ADMIN, prompt="root", mode="agent:worker", source="demo", table="sales",
        sql="SELECT 1", reward=1.0,
        meta={"conditioning_prompt": "REFERENCE DATA: [[private-row]]\nQuestion: q"})
    safe_id = db.add_trace(
        ADMIN, prompt="safe", mode="agent:worker", source="demo", table="sales",
        sql="SELECT 2", reward=1.0,
        meta={"conditioning_prompt": "safe specialist task"})

    private_page = trainer.stream(since=0, limit=1)
    assert private_page["rollouts"] == []
    assert private_page["cursor"] > 0
    safe_page = trainer.stream(since=private_page["cursor"], limit=1)
    assert [row["id"] for row in safe_page["rollouts"]] == [safe_id]
    assert safe_page["cursor"] > private_page["cursor"]
    assert private_id != safe_id


def test_stream_excludes_legacy_aggregator_rows_with_inherited_worker_sql():
    db.init_db()
    db.add_trace(
        ADMIN, prompt="compare sources", mode="agent:aggregator", source="demo",
        table="sales", sql="SELECT * FROM sales", reward=1.0,
        meta={"conditioning_prompt": "Per-database answers: ..."})

    assert trainer.stream(since=0, limit=10)["rollouts"] == []


def test_feedback_after_cursor_is_emitted_as_a_new_revision():
    db.init_db()
    tid = db.add_trace(
        ADMIN, prompt="show sales", mode="agent", source="demo", table="sales",
        sql="SELECT * FROM sales", reward=0.75, reward_source="heuristic")
    first = trainer.stream(since=0, limit=10)
    assert first["rollouts"][0]["reward"] == 0.75

    assert db.set_trace_reward(
        tid, 0.0, user_id=ADMIN["id"], source="user") is True
    revised = trainer.stream(since=first["cursor"], limit=10)
    assert revised["count"] == 1
    assert revised["rollouts"][0]["id"] == tid
    assert revised["rollouts"][0]["reward"] == 0.0
    assert revised["rollouts"][0]["reward_source"] == "user"
    assert revised["cursor"] > first["cursor"]


def test_revision_cursor_pages_rows_even_when_wall_clock_timestamps_tie(monkeypatch):
    db.init_db()
    monkeypatch.setattr(db.time, "time", lambda: 1234.5)
    first_id = db.add_trace(
        ADMIN, prompt="first", mode="agent", source="demo", table="sales",
        sql="SELECT 1", reward=1.0)
    second_id = db.add_trace(
        ADMIN, prompt="second", mode="agent", source="demo", table="sales",
        sql="SELECT 2", reward=1.0)

    page_one = trainer.stream(since=0, limit=1)
    page_two = trainer.stream(since=page_one["cursor"], limit=1)
    assert [page_one["rollouts"][0]["id"], page_two["rollouts"][0]["id"]] == [
        first_id, second_id]


def test_concurrent_adapter_publishers_leave_one_monotonic_active_identity():
    trainer.init_tables()

    def publish(index):
        return trainer.publish(
            "global", "tool_call", f"/adapters/tool-call-{index}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(publish, range(8)))

    assert sorted(result["version"] for result in results) == list(range(1, 9))
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT version,status FROM training_adapters "
            "WHERE scope='global' AND kind='tool_call' ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in rows] == list(range(1, 9))
    assert [row["version"] for row in rows if row["status"] == "active"] == [8]
