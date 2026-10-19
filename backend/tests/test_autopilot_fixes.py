"""Autopilot, made to work the way it says it does.

Proves, through the real API on a private copy of the demo warehouse:
  - a NEW-DATA trigger works: its first poll records where to start, a poll
    with nothing new does not fire, a newer row fires;
  - an agent's declared tables hold even for SQL that names a second table in
    a comma join, and a trigger that cannot be checked says why — as one
    failed run, then on the agent — instead of looking "not met";
  - an action the supervisor refuses is reported on the run;
  - a goal a certified metric answers is answered from its definition (as in
    chat), but only inside the agent's declared tables; a verified query
    answers its exact question;
  - a poll that does not fire writes no run; fired runs are alerts until
    seen, a manual run is not; alerts and runs are the owner's own;
  - an agent keeps its newest runs; a changed trigger starts its watch over;
  - migration 23 backfills seen_at and drops the old "skipped" rows.

Run from the backend directory:
    python -m pytest tests/test_autopilot_fixes.py -q
"""
import sqlite3
import time
import warnings

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")

CERTIFIED = """
models:
  - source: demo
    table: sales
    metrics:
      - name: net_revenue
        agg: sum
        expr: revenue
        synonyms: [revenue]
        certified: true
    dimensions:
      - name: region
        expr: region
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    monkeypatch.setenv("STUDIO_DEMO_MODE", "1")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL",
              "SMTP_HOST", "HARRIER_EMBED_URL"):
        monkeypatch.delenv(k, raising=False)
    import importlib

    from app import db as _db
    importlib.reload(_db)                  # this test's own database, not the last one's
    import app.main as main
    importlib.reload(main)
    from app import autopilot, semantic
    from app.connectors import demo
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    with TestClient(main.app) as c:
        def login(email, password):
            tok = c.post("/api/auth/login", json={"email": email, "password": password}
                         ).json()["access_token"]
            return {"Authorization": f"Bearer {tok}"}
        ns = type("Env", (), {})()
        ns.c, ns.ap, ns.semantic, ns.warehouse = c, autopilot, semantic, demo.WAREHOUSE_PATH
        ns.admin = login("admin@studio.local", "admin123")
        ns.analyst = login("analyst@studio.local", "analyst123")

        def make(headers=None, **kw):
            r = c.post("/api/autopilot", headers=headers or ns.admin,
                       json={"source": "demo", **kw})
            assert r.status_code == 201, r.text
            return r.json()["id"]

        def poll(aid, trigger=None):
            d = autopilot._get_agent(aid)
            return autopilot._run_agent_now(d, trigger or d["trigger_type"])

        def runs(aid, headers=None):
            return c.get(f"/api/autopilot/{aid}/runs", headers=headers or ns.admin).json()["runs"]

        ns.make, ns.poll, ns.runs = make, poll, runs
        yield ns
    semantic._STATE.update(doc=None, yaml="", source=None)


def agent(env, aid, headers=None):
    return env.c.get(f"/api/autopilot/{aid}", headers=headers or env.admin).json()


# ── New data ─────────────────────────────────────────────────────────────

def test_a_new_data_trigger_fires_on_a_newer_row_only(env):
    aid = env.make(name="new visits", goal="summarize web traffic", trigger_type="event",
                   tables=["web_traffic"], trigger_config={"table": "web_traffic"})
    first = env.poll(aid)
    assert first["status"] == "checked" and first["check"]["status"] == "watching"
    latest = first["check"]["value"]
    again = env.poll(aid)
    assert again["check"] == {"status": "no new data", "value": latest}
    assert env.runs(aid) == []
    with sqlite3.connect(env.warehouse) as w:
        w.execute("INSERT INTO web_traffic VALUES ('2999-01-01', '/new', 1, 0.5)")
    fired = env.poll(aid)
    assert fired["status"] == "done", fired
    assert agent(env, aid)["last_check"] == {"status": "fired", "value": "2999-01-01"}
    assert [r["status"] for r in env.runs(aid)] == ["done"]


# ── Scope, and a check that cannot be made ───────────────────────────────

def test_declared_tables_hold_against_a_comma_join_and_the_failure_is_said(env):
    aid = env.make(name="scoped", goal="revenue", trigger_type="threshold", tables=["sales"],
                   trigger_config={"sql": "SELECT COUNT(*) AS n FROM sales, customers",
                                   "op": "gt", "value": 0})
    run = env.poll(aid)
    assert run["status"] == "failed"
    assert "outside this agent's scope (sales)" in run["summary"]
    # Failing again is not news: no second run, the agent says it.
    assert env.poll(aid)["status"] == "checked"
    assert [r["status"] for r in env.runs(aid)] == ["failed"]
    check = agent(env, aid)["last_check"]
    assert check["status"] == "error" and "outside this agent's scope" in check["detail"]
    # Fixed, it checks again.
    env.c.patch(f"/api/autopilot/{aid}", headers=env.admin, json={"trigger_config": {
        "sql": "SELECT COUNT(*) AS n FROM sales", "op": "lt", "value": 0}})
    assert env.poll(aid)["check"]["status"] == "not met"


def test_a_metric_that_cannot_be_evaluated_is_not_reported_as_not_met(env):
    aid = env.make(name="unknown", goal="revenue", trigger_type="threshold", tables=["sales"],
                   trigger_config={"metric_prompt": "zorblax velocity", "op": "gt", "value": 0})
    run = env.poll(aid)
    assert run["status"] == "failed"
    assert "no defined metric matches “zorblax velocity” on demo" in run["summary"]


def test_an_action_the_supervisor_refuses_is_reported(env, monkeypatch):
    from app import supervisor

    def refuse(*a, **k):
        raise RuntimeError("supervisor down")
    monkeypatch.setattr(supervisor, "submit", refuse)
    aid = env.make(name="act", goal="revenue", trigger_type="manual", tables=["sales"],
                   trigger_config={"action": {"kind": "sql_script",
                                              "script": "UPDATE sales SET revenue = 0"}})
    run = env.c.post(f"/api/autopilot/{aid}/run", headers=env.admin).json()
    assert run["status"] == "done" and run["proposed_job_id"] is None
    assert "the governed action could not be proposed: supervisor down" in run["summary"]
    assert run["result"]["action_error"] == "supervisor down"


# ── The same answers as chat ─────────────────────────────────────────────

def test_a_certified_metric_answers_as_in_chat_inside_the_agents_tables(env):
    owner = {"id": "x", "role": "admin", "email": "admin@studio.local", "name": "Admin"}
    env.semantic.apply_yaml(env.semantic.YamlIn(yaml=CERTIFIED), user=owner)
    aid = env.make(name="pulse", goal="revenue by region", trigger_type="manual", tables=["sales"])
    run = env.c.post(f"/api/autopilot/{aid}/run", headers=env.admin).json()
    assert run["result"]["served_by"] == "semantic"
    assert "SUM(revenue)" in run["result"]["sql"]
    chat = env.c.post("/api/chat", headers=env.admin, json={
        "prompt": "revenue by region", "source": "demo", "table": "*"}).json()["message"]
    assert chat["served_by"] == "semantic"
    assert sorted(map(tuple, chat["rows"])) == sorted(map(tuple, run["result"]["rows"]))
    # An agent scoped elsewhere does not borrow the sales definition — one
    # table or several.
    for tables in (["web_traffic"], ["web_traffic", "customers"]):
        other = env.make(name="traffic", goal="revenue by region", trigger_type="manual",
                         tables=tables)
        run = env.c.post(f"/api/autopilot/{other}/run", headers=env.admin).json()
        assert run["result"]["served_by"] != "semantic", tables
        assert "sales" not in (run["result"]["sql"] or "").lower(), tables


def test_a_verified_query_answers_its_exact_question(env):
    saved = env.c.post("/api/queries", headers=env.admin, json={
        "prompt": "visits per page", "source": "demo", "table": "web_traffic",
        "sql": "SELECT page, SUM(visits) AS visits FROM web_traffic GROUP BY page",
        "title": "Visits per page"})
    assert saved.status_code == 201, saved.text
    aid = env.make(name="visits", goal="visits per page", trigger_type="manual",
                   tables=["web_traffic"])
    run = env.c.post(f"/api/autopilot/{aid}/run", headers=env.admin).json()
    assert run["result"]["served_by"] == "verified_sql", run["result"]
    assert "GROUP BY page" in run["result"]["sql"]


# ── Alerts ───────────────────────────────────────────────────────────────

def test_fired_runs_are_the_owners_alerts_until_seen(env):
    mine = env.make(headers=env.analyst, name="daily", goal="revenue by region",
                    trigger_type="schedule", tables=["sales"],
                    trigger_config={"interval_seconds": 3600})
    run = env.poll(mine)
    assert run["status"] == "done" and run["seen_at"] is None
    alerts = env.c.get("/api/autopilot/alerts", headers=env.analyst).json()
    assert alerts["unread"] == 1
    assert alerts["alerts"][0]["agent_name"] == "daily" and alerts["alerts"][0]["id"] == run["id"]
    # A manual run is something its owner is watching: not an alert.
    env.c.post(f"/api/autopilot/{mine}/run", headers=env.analyst)
    assert env.c.get("/api/autopilot/alerts", headers=env.analyst).json()["unread"] == 1
    # Another user (even an admin) is not alerted about it, nor can mark it.
    assert env.c.get("/api/autopilot/alerts", headers=env.admin).json()["unread"] == 0
    env.c.post("/api/autopilot/alerts/seen", headers=env.admin, json={})
    assert env.c.get("/api/autopilot/alerts", headers=env.analyst).json()["unread"] == 1
    # The run in full: its owner and an admin may read it.
    full = env.c.get(f"/api/autopilot/runs/{run['id']}", headers=env.analyst).json()
    assert full["result"]["columns"] and full["agent_name"] == "daily"
    assert env.c.get(f"/api/autopilot/runs/{run['id']}", headers=env.admin).status_code == 200
    # Seen, it is no longer unread.
    env.c.post("/api/autopilot/alerts/seen", headers=env.analyst, json={"run_ids": [run["id"]]})
    assert env.c.get("/api/autopilot/alerts", headers=env.analyst).json()["unread"] == 0


def test_another_users_run_is_not_found(env):
    viewer_tok = env.c.post("/api/auth/login", json={"email": "viewer@studio.local",
                                                     "password": "viewer123"}).json()["access_token"]
    aid = env.make(headers=env.analyst, name="mine", goal="revenue", trigger_type="manual",
                   tables=["sales"])
    run = env.c.post(f"/api/autopilot/{aid}/run", headers=env.analyst).json()
    r = env.c.get(f"/api/autopilot/runs/{run['id']}",
                  headers={"Authorization": f"Bearer {viewer_tok}"})
    assert r.status_code == 404


# ── Housekeeping ─────────────────────────────────────────────────────────

def test_an_agent_keeps_its_newest_runs(env, monkeypatch):
    monkeypatch.setattr(env.ap, "_KEEP_RUNS", 3)
    aid = env.make(name="busy", goal="revenue", trigger_type="manual", tables=["sales"])
    ids = [env.c.post(f"/api/autopilot/{aid}/run", headers=env.admin).json()["id"]
           for _ in range(5)]
    kept = [r["id"] for r in env.runs(aid)]
    assert kept == ids[::-1][:3]


def test_a_changed_trigger_starts_its_watch_over(env, monkeypatch):
    aid = env.make(name="guard", goal="revenue", trigger_type="threshold", tables=["sales"],
                   trigger_config={"sql": "SELECT SUM(revenue) AS v FROM sales", "op": "gt",
                                   "value": 0})
    assert env.poll(aid)["status"] == "done"                        # crossed: latched
    assert env.poll(aid)["check"]["status"] == "met"
    env.c.patch(f"/api/autopilot/{aid}", headers=env.admin, json={"trigger_config": {
        "sql": "SELECT SUM(revenue) AS v FROM sales", "op": "gt", "value": 1}})
    a = agent(env, aid)
    assert a["last_check"] is None and a["last_checked_at"] is None
    assert env.poll(aid)["status"] == "done"                        # a new condition, a new crossing


def test_migration_23_backfills_seen_and_drops_skipped_rows(env):
    from app import db, migrations
    with db.connect() as c:
        now = time.time()
        for rid, status, finished in (("r1", "done", now), ("r2", "skipped", now),
                                      ("r3", "failed", None)):
            c.execute("INSERT INTO autopilot_runs (id, agent_id, owner_id, status, started_at, "
                      "finished_at, seen_at) VALUES (?,?,?,?,?,?,NULL)",
                      (rid, "a", "u", status, now - 5, finished))
        c.commit()
        migrations._m23_autopilot_checks_and_alerts(c, False)
        c.commit()
        rows = {r["id"]: r["seen_at"] for r in c.execute(
            "SELECT id, seen_at FROM autopilot_runs WHERE agent_id='a'").fetchall()}
    assert set(rows) == {"r1", "r3"}
    assert rows["r1"] == pytest.approx(now) and rows["r3"] == pytest.approx(now - 5)
