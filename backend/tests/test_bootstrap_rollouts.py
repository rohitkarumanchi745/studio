"""scripts/bootstrap_rollouts.py — the first rollout corpus, without traffic.

A new deployment cannot train a tool_call adapter (router.bitnet_ready's second
gate) because training needs reward-labeled rollouts and there is no usage yet.
The bootstrap script breaks that circle by pairing app/suggest.py's
schema-grounded questions with app/pipelines.py's deterministic drafter and
VERIFYING every pair through the real gateway.

What these tests pin is the part that makes the corpus worth training on:

  - every emitted rollout's SQL really executes through app/gateway.execute
    (a rollout that does not run teaches the policy to emit broken SQL);
  - --dry-run touches nothing;
  - a re-run tops up, never duplicates;
  - it refuses to dilute an organic preference signal without --force;
  - the rows come back out of the exact query app/trainer.py's
    /training/rollouts endpoint serves, with the fields the trainer needs;
  - RBAC bounds it: run as a viewer, get only a viewer's tables.

Run from the backend directory:
    python -m pytest tests/test_bootstrap_rollouts.py -q
"""
import importlib.util
import os
import tempfile

import pytest

# Point the app at a throwaway SQLite file BEFORE app.db computes DB_PATH.
os.environ["STUDIO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="studio-bootstrap-rollouts-test-"), "studio.db")

from app import db, gateway, trainer  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "scripts", "bootstrap_rollouts.py")


def _load():
    spec = importlib.util.spec_from_file_location("bootstrap_rollouts", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


boot = _load()

ADMIN_EMAIL = "admin@studio.local"
VIEWER_EMAIL = "viewer@studio.local"
# Small enough to keep the suite fast; large enough that the round-robin
# spreads across several tables and more than one grain.
SMALL = ["--source", "demo", "--limit", "12", "--tables", "3"]


def _reset():
    """Fresh trace table for each test — the suite shares one DB file."""
    db.init_db()
    with db.connect() as c:
        c.execute("DELETE FROM agent_traces")
        c.commit()


def _traces(reward_source=None):
    with db.connect() as c:
        if reward_source:
            rows = c.execute("SELECT * FROM agent_traces WHERE reward_source=?",
                             (reward_source,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM agent_traces").fetchall()
    return [dict(r) for r in rows]


# ── It produces a corpus at all ─────────────────────────────────────────

def test_writes_verified_rollouts_for_the_demo_source():
    _reset()
    report = boot.run(SMALL)
    rows = _traces(boot.REWARD_SOURCE)
    assert report["written"] == len(rows) == 12
    assert report["failed"] == 0                      # nothing unrunnable was kept
    assert report["keep_rate"] > 0
    for r in rows:
        assert r["source"] == "demo"
        assert r["tbl"]
        assert r["prompt"].strip()
        assert "select" in r["sql"].lower()
        assert r["reward"] == boot.DEFAULT_REWARD     # trainable band, clearly synthetic
        assert r["reward_source"] == boot.REWARD_SOURCE
        assert r["mode"] == boot.MODE
        # scripts/train_online.py drops any mode starting with these.
        assert not r["mode"].startswith(("fallback", "error"))
        assert r["ok"] == 1


def test_every_emitted_rollout_executes_through_the_gateway():
    """The whole point of verification: re-running each stored statement as the
    same user must succeed. If this ever fails, the corpus is teaching the
    policy to emit SQL that does not run."""
    _reset()
    boot.run(SMALL)
    user = db.get_user_by_email(ADMIN_EMAIL)
    rows = _traces(boot.REWARD_SOURCE)
    assert rows
    for r in rows:
        res = gateway.execute(user, r["source"], r["sql"], "test_replay",
                              table_label=r["tbl"], max_rows=50)
        assert res.columns
        # The stored SQL is what the gateway would run again, byte for byte —
        # callers persist the CLEANED text, never the draft.
        assert res.sql == r["sql"]


def test_rollouts_are_readable_by_the_trainer_endpoint_query():
    """app/trainer.py's /training/rollouts serves stream(); a bootstrap row must
    arrive with everything scripts/train_online.py conditions a sample on."""
    _reset()
    boot.run(SMALL)
    out = trainer.stream(since=0.0, limit=500)
    assert out["count"] == 12
    for r in out["rollouts"]:
        assert r["source"] == "demo" and r["tbl"]
        assert r["prompt"] and r["action"]["sql"]
        assert r["reward"] == boot.DEFAULT_REWARD
        assert r["reward_source"] == boot.REWARD_SOURCE
        # meta-derived keys the stream promises its consumers
        assert r["agents"] == [] and r["history"] == []


# ── Bounded, idempotent, honest about writing ───────────────────────────

def test_dry_run_writes_nothing():
    _reset()
    before = len(_traces())
    report = boot.run(SMALL + ["--dry-run"])
    assert report["dry_run"] is True
    assert report["kept"] == 12          # it did the full generate + verify
    assert report["written"] == 0
    assert len(_traces()) == before      # and wrote not one row


def test_rerun_does_not_duplicate():
    _reset()
    # A limit large enough to exhaust the candidate pool, so the second run has
    # nothing new to add — the strict form of "does not duplicate".
    argv = ["--source", "demo", "--limit", "5000", "--tables", "3"]
    first = boot.run(argv)
    assert first["written"] > 0
    second = boot.run(argv)
    assert second["written"] == 0
    assert second["duplicate"] == first["written"]
    assert len(_traces(boot.REWARD_SOURCE)) == first["written"]
    # Identity is content, not a run id: same (source, table, prompt, sql).
    keys = {(r["source"], r["tbl"], r["prompt"], r["sql"])
            for r in _traces(boot.REWARD_SOURCE)}
    assert len(keys) == first["written"]


def test_limit_and_source_bound_the_run():
    _reset()
    report = boot.run(["--source", "demo", "--limit", "7", "--tables", "2"])
    assert report["written"] == 7
    assert report["sources"] == ["demo"]
    assert len({r["tbl"] for r in _traces(boot.REWARD_SOURCE)}) == 2  # round-robin spread


def test_unknown_source_filter_is_refused_not_silently_empty():
    _reset()
    with pytest.raises(SystemExit):
        boot.run(["--source", "not_a_source", "--limit", "5"])
    assert _traces(boot.REWARD_SOURCE) == []


# ── It refuses to dilute a real preference signal ───────────────────────

def test_refuses_when_organic_rollouts_exist_and_force_overrides():
    _reset()
    user = db.get_user_by_email(ADMIN_EMAIL)
    db.add_trace(user, prompt="a question a human actually asked", mode="agent",
                 source="demo", table="sales", sql="SELECT 1 FROM sales",
                 reward=0.9, reward_source="heuristic")

    with pytest.raises(SystemExit) as e:
        boot.run(SMALL)
    msg = str(e.value)
    assert "refusing" in msg and "--force" in msg          # says why, and the way out
    assert "DELETE FROM agent_traces" in msg               # and how to undo later
    assert _traces(boot.REWARD_SOURCE) == []               # nothing written

    # A dry run is always allowed: it writes nothing, so it dilutes nothing.
    assert boot.run(SMALL + ["--dry-run"])["written"] == 0
    assert _traces(boot.REWARD_SOURCE) == []

    report = boot.run(SMALL + ["--force"])
    assert report["written"] == 12
    assert report["organic_rollouts"] == 1
    # The organic rollout is untouched and still distinguishable.
    assert len(_traces("heuristic")) == 1


# ── RBAC: a bootstrap corpus is not a way around access control ─────────

def test_runs_as_the_given_user_and_respects_that_role():
    _reset()
    report = boot.run(["--user", VIEWER_EMAIL, "--source", "demo", "--limit", "50"])
    assert report["role"] == "viewer"
    assert report["written"] > 0
    # policies.POLICIES gives the viewer role exactly these demo tables.
    assert {r["tbl"] for r in _traces(boot.REWARD_SOURCE)} <= {"sales", "web_traffic"}
    assert all(r["user_id"] == db.get_user_by_email(VIEWER_EMAIL)["id"]
               for r in _traces(boot.REWARD_SOURCE))


def test_unknown_user_is_refused():
    _reset()
    with pytest.raises(SystemExit):
        boot.run(["--user", "nobody@studio.local", "--source", "demo"])
    assert _traces(boot.REWARD_SOURCE) == []


# ── Quality gates (pure, no database) ───────────────────────────────────

def _demo_columns():
    return [{"name": "order_date", "type": "TEXT"}, {"name": "region", "type": "TEXT"},
            {"name": "revenue", "type": "REAL"}]


def test_is_faithful_drops_sql_that_does_not_answer_the_question():
    """A ranking question answered with a time series executes fine and is still
    a bad rollout — it teaches the policy that every question is a trend."""
    trend = ("SELECT strftime('%Y-%m', order_date) AS month, SUM(revenue) AS total_revenue "
             "FROM sales GROUP BY strftime('%Y-%m', order_date) ORDER BY 1 LIMIT 500")
    cols = _demo_columns()

    ok, _ = boot.is_faithful("How has revenue trended by month?", trend, cols)
    assert ok

    # names a dimension the SQL never groups by
    ok, why = boot.is_faithful("Monthly revenue by region", trend, cols)
    assert not ok and "region" in why

    # asks for a grain the SQL does not bucket to
    ok, why = boot.is_faithful("Yearly revenue", trend, cols)
    assert not ok and "year" in why

    # a construct the deterministic drafter cannot express at all
    ok, why = boot.is_faithful("Top 10 region by revenue", trend, cols)
    assert not ok and "unsupported" in why

    # not an aggregate
    ok, why = boot.is_faithful("Monthly revenue", "SELECT * FROM sales LIMIT 200", cols)
    assert not ok and "aggregate" in why


def test_dateless_table_still_yields_a_faithful_pair():
    """A table with no date column can only be grouped by its first dimension,
    so the generator must ask a question that shape actually answers — a real
    warehouse has such tables even though the demo one does not."""
    class _Conn:
        name, dialect = "demo", "sqlite"

    cols = [{"name": "region", "type": "TEXT"}, {"name": "revenue", "type": "REAL"}]
    kept = [p for p in boot.pairs_for_table(_Conn(), "flat", cols) if p["faithful"]]
    assert kept
    for p in kept:
        assert "group by region" in p["sql"].lower()
        assert "region" in p["prompt"].lower()


def test_pairs_for_table_marks_the_unfaithful_ones():
    class _Conn:
        name, dialect = "demo", "sqlite"

    pairs = boot.pairs_for_table(_Conn(), "sales", _demo_columns())
    assert pairs, "the generator produced nothing for a normal schema"
    kept = [p for p in pairs if p["faithful"]]
    assert kept and len(kept) < len(pairs)      # it generates AND it filters
    for p in kept:
        assert "group by" in p["sql"].lower()


def test_sql_shape_collapses_names_but_not_construction():
    """The shape count is the honest measure of what the corpus can teach, so it
    must ignore which table/column a query names and keep how it is built."""
    a = ("SELECT strftime('%Y-%m', order_date) AS month, SUM(revenue) AS total_revenue "
         "FROM sales GROUP BY strftime('%Y-%m', order_date) ORDER BY 1 LIMIT 500")
    b = ("SELECT strftime('%Y-%m', event_date) AS month, SUM(minutes) AS total_minutes "
         "FROM downtime_events GROUP BY strftime('%Y-%m', event_date) ORDER BY 1 LIMIT 500")
    c = a.replace("'%Y-%m'", "'%Y'")                     # a different grain
    d = a.replace("SUM(revenue)", "SUM(revenue), region").replace(
        "GROUP BY strftime('%Y-%m', order_date)",
        "GROUP BY strftime('%Y-%m', order_date), region")
    assert boot.sql_shape(a) == boot.sql_shape(b)        # only names differ
    assert boot.sql_shape(a) != boot.sql_shape(c)        # different bucket literal
    assert boot.sql_shape(a) != boot.sql_shape(d)        # extra grouping column
    # A column literally named `date` must not read as SQL vocabulary.
    assert boot.sql_shape("SELECT SUM(x) FROM t GROUP BY date") == \
        boot.sql_shape("SELECT SUM(y) FROM u GROUP BY region")


def test_shape_report_counts_shapes_not_rows():
    _reset()
    report = boot.run(["--source", "demo", "--limit", "40", "--tables", "6"])
    sh = report["shape"]
    assert sh["rollouts"] == 40
    # Many rollouts, few shapes — that is the finding, and it must be visible.
    assert sh["distinct_shapes"] < sh["distinct_sql"] <= sh["rollouts"]
    assert 0 < sh["shape_fraction"] < 1
    assert sum(s["count"] for s in sh["shapes"]) == sh["rollouts"]
