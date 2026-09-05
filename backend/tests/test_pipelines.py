"""Pipelines: an unverified step is never presented as verified, and the
deterministic drafter answers the prompt that was actually asked.

Two honesty properties are pinned here:

  * build() returns ONLY steps that verified (RBAC + guard + a real
    execution). Everything that failed comes back in "dropped" WITH its error,
    and when nothing verified "steps" is empty — build() used to fall back to
    `... or steps`, handing every failed step back as if it had passed, which
    the UI then stamped "✓ verified". Saving re-verifies server-side and
    refuses the whole body on the first failure.
  * the drafter honours the prompt: "monthly revenue by region" buckets by
    month, sums revenue, groups by region, and does not step over tables the
    request never mentioned. An LLM-drafted step that ignores a named grain is
    WARNED (intent_warnings), never dropped — it verified, so it is real data;
    it just may not be the breakdown that was asked for.

The VIEWER role is used throughout because its built-in policy is exactly
{demo: sales, web_traffic} — that pins routing to the demo warehouse without
monkeypatching the router, so these assertions are about the drafter, not
about which source happened to win.

No LLM key is needed (or used): _llm_steps is only reached when one is
configured, and the one test that exercises it monkeypatches it.

Run from the backend directory:
    python -m pytest tests/test_pipelines.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-pipelines-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest
from fastapi import HTTPException

from app import db, email_service, governance, grains, pipelines, queries
from app.connectors.demo import seed

# viewer: demo/{sales, web_traffic} only — see app/policies.py.
VIEWER = {"id": "u-pl-viewer", "email": "view@studio.test", "role": "viewer", "name": "V"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    queries.init_tables()
    pipelines.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    """Built-in RBAC, no LLM key, no outbound mail."""
    governance._STATE.update(doc=None, yaml="", source=None)
    for v in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SMTP_HOST"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(email_service, "send", lambda *a, **k: {"mode": "test"})
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


def _sql(draft, table):
    return next(s["sql"] for s in draft["steps"] if s["table"] == table)


# ── Prompt fidelity: the drafted SQL answers the question asked ─────────

def test_monthly_revenue_by_region_buckets_by_month_on_sales_only():
    d = pipelines.build(VIEWER, "monthly revenue by region")
    assert d["source"] == "demo"
    assert [s["table"] for s in d["steps"]] == ["sales"]
    sql = _sql(d, "sales")
    assert "strftime('%Y-%m'" in sql          # monthly, not daily
    assert "region" in sql                     # the dimension the prompt named
    assert "SUM(revenue)" in sql               # the measure the prompt named
    assert "GROUP BY" in sql and "AS month" in sql
    # ...and no step on a table the request never mentioned.
    tables = {s["table"] for s in d["steps"] + d["dropped"]}
    assert "customers" not in tables and "web_traffic" not in tables
    assert all(s["verified"] for s in d["steps"])
    assert d["steps"][0]["intent_warnings"] == []


def test_weekly_visits_per_page_buckets_by_week_on_web_traffic():
    d = pipelines.build(VIEWER, "weekly visits per page")
    assert [s["table"] for s in d["steps"]] == ["web_traffic"]
    sql = _sql(d, "web_traffic")
    assert "strftime('%Y-%W'" in sql
    assert "page" in sql and "SUM(visits)" in sql
    assert "sales" not in sql


def test_no_grain_in_the_prompt_keeps_the_previous_shape():
    """Nothing about grain detection may change a request that never asked for
    a bucket: first measure over the first date column, exactly as before."""
    d = pipelines.build(VIEWER, "revenue by region")
    sql = _sql(d, "sales")
    assert "strftime" not in sql
    assert sql.startswith("SELECT order_date, SUM(revenue) AS total_revenue FROM sales")
    assert d["steps"][0]["intent_warnings"] == []


def test_draft_sql_uses_date_trunc_off_sqlite():
    """The bucket follows the connector's dialect, so a warehouse source gets
    DATE_TRUNC rather than SQLite's strftime."""
    class Fake:
        dialect = "snowflake"

    cols = [{"name": "order_date", "type": "DATE"}, {"name": "region", "type": "TEXT"},
            {"name": "revenue", "type": "REAL"}]
    sql = pipelines._draft_sql(Fake(), "sales", cols, "quarterly revenue by region")
    assert "DATE_TRUNC('quarter', order_date)" in sql
    assert "region" in sql and "SUM(revenue)" in sql


# ── Intent warnings: warn, never drop ──────────────────────────────────

def test_llm_step_that_ignores_the_grain_is_warned_not_dropped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda spec, user: True)
    monkeypatch.setattr(
        pipelines, "_llm_steps",
        lambda user, source, schemas, prompt, spec: [{
            "name": "Daily revenue", "table": "sales",
            "sql": "SELECT strftime('%Y-%m-%d', order_date) AS day, SUM(revenue) AS rev "
                   "FROM sales GROUP BY 1"}])
    d = pipelines.build(VIEWER, "monthly revenue by region")
    assert len(d["steps"]) == 1                        # kept: it verified
    assert d["steps"][0]["verified"] is True
    assert d["steps"][0]["intent_warnings"] == ["does not bucket by month"]
    assert d["dropped"] == []


def test_llm_step_that_honours_the_grain_is_not_warned(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda spec, user: True)
    monkeypatch.setattr(
        pipelines, "_llm_steps",
        lambda user, source, schemas, prompt, spec: [{
            "name": "Monthly revenue", "table": "sales",
            "sql": "SELECT strftime('%Y-%m', order_date) AS month, SUM(revenue) AS rev "
                   "FROM sales GROUP BY 1"}])
    d = pipelines.build(VIEWER, "monthly revenue by region")
    assert d["steps"][0]["intent_warnings"] == []


# ── The core honesty fix: unverified is never returned as verified ──────

def test_every_step_failing_returns_no_steps_and_all_the_reasons(monkeypatch):
    monkeypatch.setattr(queries, "verify_sql",
                        lambda *a, **k: {"ok": False, "error": "boom: table is on fire"})
    d = pipelines.build(VIEWER, "monthly revenue by region")
    assert d["steps"] == []                            # NOT "or steps"
    assert d["dropped"] and len(d["dropped"]) >= 1
    assert all(s["verified"] is False for s in d["dropped"])
    assert all(s["error"] == "boom: table is on fire" for s in d["dropped"])
    assert d["lineage"]["steps"] == []                 # nothing to draw


def test_only_the_passing_step_is_kept(monkeypatch):
    real = queries.verify_sql

    def one_bad(user, source, table, sql, *a, **k):
        if table == "web_traffic":
            return {"ok": False, "error": "column visits not permitted"}
        return real(user, source, table, sql, *a, **k)

    monkeypatch.setattr(queries, "verify_sql", one_bad)
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda spec, user: True)
    monkeypatch.setattr(
        pipelines, "_llm_steps",
        lambda user, source, schemas, prompt, spec: [
            {"name": "Sales", "table": "sales", "sql": "SELECT region FROM sales LIMIT 5"},
            {"name": "Traffic", "table": "web_traffic", "sql": "SELECT page FROM web_traffic"},
        ])
    d = pipelines.build(VIEWER, "revenue and traffic")
    assert [s["name"] for s in d["steps"]] == ["Sales"]
    assert [s["name"] for s in d["dropped"]] == ["Traffic"]
    assert d["dropped"][0]["error"] == "column visits not permitted"


# ── Saving re-verifies server-side and refuses a bad body ──────────────

def test_save_refuses_a_step_that_does_not_verify():
    body = pipelines.SaveIn(
        prompt="sneak in customers", source="demo",
        # A client-claimed "verified: True" on a table this role may not read.
        steps=[{"name": "PII", "table": "customers", "verified": True,
                "sql": "SELECT * FROM customers LIMIT 5"}])
    with pytest.raises(HTTPException) as e:
        pipelines.create(body, VIEWER)
    assert e.value.status_code == 400
    assert "does not verify" in e.value.detail


def test_save_refuses_an_empty_pipeline_and_an_oversized_one():
    with pytest.raises(HTTPException) as e:
        pipelines.create(pipelines.SaveIn(prompt="x", source="demo", steps=[]), VIEWER)
    assert e.value.status_code == 400

    too_many = [{"name": f"s{i}", "table": "sales", "sql": "SELECT 1 FROM sales"}
                for i in range(pipelines.MAX_STEPS + 1)]
    with pytest.raises(HTTPException) as e:
        pipelines.create(pipelines.SaveIn(prompt="x", source="demo", steps=too_many), VIEWER)
    # Truncating silently would save fewer steps than were submitted.
    assert e.value.status_code == 400 and "at most" in e.value.detail


def test_a_verified_draft_saves_and_keeps_its_steps():
    d = pipelines.build(VIEWER, "monthly revenue by region")
    saved = pipelines.create(
        pipelines.SaveIn(name="Monthly revenue", prompt=d["prompt"], source=d["source"],
                         steps=d["steps"]), VIEWER)
    assert len(saved["steps"]) == len(d["steps"])
    assert all(s["verified"] for s in saved["steps"])
    pipelines.remove(saved["id"], VIEWER)


# ── grains: the shared vocabulary both agent.py and pipelines.py use ────

def test_grain_detection_and_bucket_expressions():
    assert grains.detect("monthly revenue by region") == "month"
    assert grains.detect("annual spend") == "year"
    assert grains.detect("revenue by region") is None
    assert grains.bucket_expr("sqlite", "d", "month") == "strftime('%Y-%m', d)"
    assert grains.bucket_expr("postgres", "d", "week") == "DATE_TRUNC('week', d)"
    assert grains.bucket_expr("sqlite", "d", "nonsense") is None
    # sqlite has no strftime quarter — it is composed, and recognised again.
    q = grains.bucket_expr("sqlite", "d", "quarter")
    assert "strftime('%Y'" in q and grains.has_bucket(q, "quarter")


def test_has_bucket_does_not_confuse_a_finer_format_for_a_coarser_one():
    """The whole point of the intent check: '%Y-%m-%d' is daily, and matching
    it as a substring of the monthly '%Y-%m' would silence the warning."""
    assert grains.has_bucket("SELECT strftime('%Y-%m', d) FROM t", "month")
    assert not grains.has_bucket("SELECT strftime('%Y-%m-%d', d) FROM t", "month")
    assert grains.has_bucket("SELECT DATE_TRUNC('month', d) FROM t", "month")
    assert grains.has_bucket("SELECT DATE_TRUNC(d, MONTH) FROM t", "month")
    assert grains.has_bucket("SELECT EXTRACT(month FROM d) FROM t", "month")
    assert not grains.has_bucket("SELECT DATE_TRUNC('day', d) FROM t", "month")


def test_agent_still_reads_its_granularity_table_from_grains():
    """agent.py's keyless fallback and the pipeline drafter must bucket by the
    SAME table — that is why it moved into the leaf module."""
    from app import agent
    assert agent._GRAN_FMT is grains.SQLITE_FMT
    assert agent._GRAN_FMT["month"] == "%Y-%m"
