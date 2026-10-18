"""Metadata graph, phase 02: continuous synchronization.

Acceptance: create, alter or remove a test table and the graph updates
without manual intervention — the scheduler's tick finds the table changed
against the graph's record and queues a change event for it (metacdc).

Also locked in: fingerprints (signals) that have not moved queue nothing;
events (a connection's lifecycle, a supervised write, a signed webhook) queue a
rediscovery once; failures back off and retry; a large removal is held until
a later snapshot confirms it; tombstones are hidden at once and purged after
their grace period; the Neo4j mirror follows.
"""
import hashlib
import hmac
import json
import sqlite3
import time

import pytest

from test_metagraph_discovery import (ADMIN, VIEWER, RETAIL_TABLES, RetailConnector,  # noqa: F401
                                      env, neo4j, stored)


class SignalledRetail(RetailConnector):
    """The retail stand-in with a change signal, as Postgres/Databricks have."""
    def change_signal(self):
        if self.fail_listing:
            raise RuntimeError("warehouse unreachable")
        with self._c() as c:
            rows = c.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        return hashlib.sha256(repr(rows).encode()).hexdigest()


@pytest.fixture()
def sync(env, tmp_path):
    signalled = SignalledRetail(env.warehouse)
    from app import connectors
    from app.connectors import demo
    env.mp.setitem(connectors._REGISTRY, "retail", signalled)
    # The tick covers every live source, the built-in demo warehouse too; keep
    # its seed file in this test's directory.
    env.mp.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo_warehouse.db"))
    env.retail = signalled
    return env


def queued(env, source="retail"):
    with env.db.connect() as c:
        return [json.loads(r["payload"])["trigger"] for r in c.execute(
            "SELECT payload FROM background_jobs WHERE kind='metagraph_discover' "
            "AND status='queued' ORDER BY created_at").fetchall()
            if json.loads(r["payload"])["source"] == source]


def run_queued(env):
    """What the worker does: run every queued discovery, change event and
    refresh, then mark it done."""
    handlers, out = env.jobs.handlers(), []
    while True:
        with env.db.connect() as c:
            row = c.execute("SELECT * FROM background_jobs WHERE status='queued' AND kind IN "
                            "('metagraph_discover', 'metagraph_event', 'metagraph_refresh') "
                            "ORDER BY created_at, id LIMIT 1").fetchone()
        if row is None:
            return out
        try:
            out.append(handlers[row["kind"]](json.loads(row["payload"]), None))
            status = "done"
        except Exception:
            status = "failed"
        with env.db.connect() as c:
            c.execute("UPDATE background_jobs SET status=? WHERE id=?", (status, row["id"]))
            c.commit()


def tick(env, source="retail"):
    """What the scheduler queued for `source` this pass ({} when nothing)."""
    return {k: v for k, v in env.mg.tick_once()["queued"].items() if k == source}


def age(env, source="retail", **seconds_ago):
    """Move stored timestamps back, as if that much time had passed."""
    now = time.time()
    with env.db.connect() as c:
        for column, ago in seconds_ago.items():
            c.execute(f"UPDATE meta_sources SET {column}=? WHERE source=?", (now - ago, source))
        c.commit()


def warehouse(env, *statements):
    with sqlite3.connect(env.warehouse) as c:
        for sql in statements:
            c.execute(sql)


def live_names(env):
    return {r["name"] for r in env.mg.get_source("retail", user=ADMIN)["relations"]}


# ── The acceptance: changes reach the graph on their own ─────────────────

def test_a_source_nobody_saved_is_discovered_by_the_scheduler(sync):
    assert tick(sync) == {"retail": "first_discovery"}
    run_queued(sync)
    assert live_names(sync) == set(RETAIL_TABLES) | {"retail_sales_detail"}


def test_create_alter_and_remove_reach_the_graph_without_anyone_asking(sync):
    tick(sync), run_queued(sync)
    assert tick(sync) == {}                                  # nothing moved: nothing queued

    warehouse(sync, "CREATE TABLE retail_promotions (promo_id INTEGER, name TEXT)")
    assert tick(sync) == {}                     # one table's event, not a whole rediscovery
    assert queued(sync) == []
    run_queued(sync)
    assert "retail_promotions" in live_names(sync)

    warehouse(sync, "ALTER TABLE retail_promotions ADD COLUMN starts_on TEXT")
    assert tick(sync) == {}
    run_queued(sync)
    promo = next(r for r in sync.mg.get_source("retail", user=ADMIN)["relations"]
                 if r["name"] == "retail_promotions")
    assert [c["name"] for c in promo["columns"]] == ["promo_id", "name", "starts_on"]

    warehouse(sync, "DROP TABLE retail_promotions")
    assert tick(sync) == {}
    run_queued(sync)
    detail = sync.mg.get_source("retail", user=ADMIN)
    assert "retail_promotions" not in {r["name"] for r in detail["relations"]}
    assert detail["removed"][0]["name"] == "retail_promotions"     # a tombstone, for admins
    assert tick(sync) == {}


def test_a_source_without_a_signal_is_rediscovered_on_the_full_sync_cadence(env):
    tick(env), run_queued(env)                               # RetailConnector: no signal
    assert tick(env) == {}
    age(env, ok_at=7 * 3600)
    assert tick(env) == {"retail": "scheduled"}


def test_a_change_is_followed_even_by_a_quiet_source(sync):
    tick(sync), run_queued(sync)
    age(sync, ok_at=60)
    warehouse(sync, "CREATE TABLE retail_gift_cards (card_id INTEGER)")
    tick(sync), run_queued(sync)
    assert "retail_gift_cards" in live_names(sync)


def test_a_source_that_cannot_be_listed_falls_back_to_its_change_signal(sync):
    tick(sync), run_queued(sync)
    warehouse(sync, "CREATE TABLE retail_gift_cards (card_id INTEGER)")
    real = sync.retail.list_tables
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("listing timed out")
        return real()
    sync.retail.list_tables = flaky
    assert tick(sync) == {"retail": "change_detected"}


# ── Dedupe, staleness, retries ───────────────────────────────────────────

def test_one_discovery_at_a_time(sync):
    assert tick(sync) == {"retail": "first_discovery"}
    assert tick(sync) == {}                                  # already queued
    assert queued(sync) == ["first_discovery"]
    age(sync, queued_at=2 * 3600)                            # the queued one was lost
    assert tick(sync) == {"retail": "first_discovery"}


def test_a_failing_source_backs_off_then_retries(sync):
    tick(sync), run_queued(sync)
    sync.retail.fail_listing = True
    warehouse(sync, "CREATE TABLE retail_x (a INTEGER)")
    sync.mg.enqueue("retail", "manual")
    run_queued(sync)
    assert sync.mg._state("retail")["status"] == "failed"
    assert tick(sync) == {}                                  # backing off
    age(sync, started_at=3600)
    sync.retail.fail_listing = False
    assert tick(sync) == {"retail": "retry"}
    run_queued(sync)
    assert "retail_x" in live_names(sync)


def test_a_partial_snapshot_does_not_loop(sync):
    sync.retail.bulk = False
    sync.retail.fail_columns = {"retail_orders"}
    tick(sync), run_queued(sync)
    assert sync.mg._state("retail")["status"] == "partial"
    assert tick(sync) == {}                                  # signal recorded: no re-run


# ── Safe deletion ────────────────────────────────────────────────────────

BIG_DROP = ["retail_returns", "retail_inventory", "retail_stores", "retail_customers",
            "retail_products"]


def test_a_large_removal_is_held_then_confirmed(sync):
    tick(sync), run_queued(sync)
    sync.retail.fail_columns = set()
    warehouse(sync, "DROP VIEW retail_sales_detail",
              *[f"DROP TABLE {t}" for t in BIG_DROP])
    tick(sync), run_queued(sync)
    state = sync.mg._state("retail")
    assert state["status"] == "held"
    assert set(json.loads(state["held_removals"])) == set(BIG_DROP) | {"retail_sales_detail"}
    assert len(live_names(sync)) == 8                        # nothing removed yet
    assert sync.mg.get_source("retail", user=ADMIN)["held"] == 6
    assert tick(sync) == {}                                  # waits for the delay
    age(sync, held_at=3600)
    assert tick(sync) == {"retail": "confirm_removals"}
    run_queued(sync)
    assert live_names(sync) == {"retail_orders", "retail_order_items"}
    assert sync.mg._state("retail")["held_removals"] is None


def test_a_held_removal_that_comes_back_removes_nothing(sync):
    tick(sync), run_queued(sync)
    sync.retail.fail_listing = False
    real = sync.retail.list_tables
    sync.retail.list_tables = lambda: ["retail_orders"]          # a half-broken listing
    sync.mg.enqueue("retail", "manual"), run_queued(sync)
    assert sync.mg._state("retail")["status"] == "held"
    sync.retail.list_tables = real                                 # it recovers
    age(sync, held_at=3600)
    tick(sync), run_queued(sync)
    assert len(live_names(sync)) == 8
    state = sync.mg._state("retail")
    assert state["status"] == "ok" and state["held_removals"] is None


def test_a_small_removal_applies_at_once(sync):
    tick(sync), run_queued(sync)
    warehouse(sync, "DROP TABLE retail_returns")
    tick(sync), run_queued(sync)
    assert "retail_returns" not in live_names(sync)


def test_tombstones_are_purged_after_their_grace(sync):
    tick(sync), run_queued(sync)
    warehouse(sync, "DROP TABLE retail_returns")
    tick(sync), run_queued(sync)
    assert "retail_returns" in stored(sync)                        # a tombstone
    with sync.db.connect() as c:
        c.execute("UPDATE meta_objects SET removed_at=? WHERE name='retail_returns'",
                  (time.time() - 31 * 86400,))
        c.commit()
    assert sync.mg.tick_once()["purged"] == 1
    assert "retail_returns" not in stored(sync)
    with sync.db.connect() as c:
        orphans = c.execute("SELECT COUNT(*) n FROM meta_columns WHERE object_id NOT IN "
                            "(SELECT id FROM meta_objects)").fetchone()["n"]
    assert orphans == 0


def test_old_snapshots_are_pruned(sync):
    sync.mp.setenv("STUDIO_METAGRAPH_SNAPSHOTS_KEPT", "10")
    for _ in range(14):
        sync.mg.discover("retail", "manual")
    sync.mg.tick_once()
    with sync.db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM meta_snapshots").fetchone()["n"] == 10


# ── Connection lifecycle ─────────────────────────────────────────────────

def test_a_disconnected_source_is_hidden_and_leaves_neo4j(sync, neo4j):
    tick(sync), run_queued(sync)
    sync.mg._on_source_event("retail", "disconnected")
    assert sync.mg._state("retail")["status"] == "disconnected"
    assert "MATCH (s:StudioSource {tenant: $tenant, name: $source}) DETACH DELETE s" in \
        json.dumps(neo4j.requests[-1]["body"])
    from app import connectors
    sync.mp.delitem(connectors._REGISTRY, "retail")              # gone from the catalog
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:                      # the viewer's grant remains
        sync.mg.get_source("retail", user=VIEWER)
    assert e.value.status_code == 404


def test_a_removed_source_becomes_tombstones(sync):
    tick(sync), run_queued(sync)
    sync.mg._on_source_event("retail", "removed")
    assert all(o["removed_at"] is not None for o in stored(sync).values())


def test_reconnecting_queues_a_rediscovery(sync):
    sync.mg._on_source_event("retail", "reconnected")
    assert queued(sync) == ["reconnected"]


# ── Events: supervised writes and the webhook ────────────────────────────

def test_a_supervised_write_queues_one_rediscovery(sync):
    tick(sync), run_queued(sync)
    from app import supervisor

    def events():
        with sync.db.connect() as c:
            return [(r["event_id"], r["event_type"], r["origin"]) for r in c.execute(
                "SELECT * FROM meta_events ORDER BY received_at").fetchall()]

    job = {"id": "job-1", "kind": supervisor.SQL_KIND, "target": "retail", "script": "x"}
    supervisor._notify_metadata(job)
    supervisor._notify_metadata(job)                              # a polled status, again
    assert events() == [("job-job-1", "source_changed", "studio")]
    warehouse(sync, "CREATE TABLE retail_promotions (promo_id INTEGER)")
    handler = sync.jobs.handlers()["metagraph_event"]
    assert handler({"event_id": "job-job-1"}, None) == {"status": "applied"}
    assert "retail_promotions" in live_names(sync)               # the source was rediscovered
    dag = {"id": "dag-1", "kind": supervisor.DAG_KIND, "target": "airflow",
           "script": json.dumps({"plan": {"source": "retail"}})}
    supervisor._notify_metadata(dag)
    assert events()[-1] == ("job-dag-1", "source_changed", "studio")


def _signed(body, secret="hook-secret"):
    return {"x-studio-signature": "sha256=" + hmac.new(secret.encode(), body,
                                                        hashlib.sha256).hexdigest()}


def test_the_webhook_is_off_until_a_secret_is_set(sync):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(sync.mg.router, prefix="/api")
    client = TestClient(app)
    body = json.dumps({"source": "retail"}).encode()
    assert client.post("/api/metagraph/events", content=body).status_code == 404
    sync.mp.setenv("STUDIO_METAGRAPH_WEBHOOK_SECRET", "hook-secret")
    assert client.post("/api/metagraph/events", content=body,
                       headers=_signed(body, "wrong")).status_code == 401
    ok = client.post("/api/metagraph/events", content=body, headers=_signed(body))
    assert ok.status_code == 202 and ok.json() == {"queued": ["retail"], "unknown": []}
    again = client.post("/api/metagraph/events", content=body, headers=_signed(body))
    assert again.json()["queued"] == []                           # already queued
    body = json.dumps({"sources": ["retail", "nope"]}).encode()
    assert client.post("/api/metagraph/events", content=body,
                       headers=_signed(body)).json()["unknown"] == ["nope"]


# ── Mirror retries, scheduler wiring, migration ──────────────────────────

def test_a_failed_mirror_is_retried_by_the_tick(sync, neo4j):
    neo4j.errors = [{"code": "Neo.TransientError", "message": "busy"}]
    tick(sync), run_queued(sync)
    assert sync.mg._state("retail")["mirror_status"] == "failed"
    neo4j.errors = []
    tick(sync)
    assert sync.mg._state("retail")["mirror_status"] == "ok"


def test_the_scheduler_runs_the_tick_under_its_own_lease(sync):
    names = {s["name"]: s for s in sync.jobs._default_schedulers()}
    assert names["metagraph_sync"]["fn"] is sync.mg.tick_once
    sync.mp.setenv("STUDIO_METAGRAPH_TICKER", "0")
    assert not names["metagraph_sync"]["enabled"]()


def test_migration_adds_the_sync_columns_to_an_old_table(sync):
    with sync.db.connect() as c:
        c.execute("DROP TABLE meta_sources")
        c.execute("CREATE TABLE meta_sources (source TEXT PRIMARY KEY, status TEXT NOT NULL, "
                  "snapshot_id TEXT, started_at REAL, ok_at REAL, error TEXT, "
                  "mirror_status TEXT, mirror_error TEXT, mirrored_at REAL)")
        sync.migrations._m18_metagraph_sync_columns(c, sync.db.IS_PG)
        c.commit()
        cols = {r[1] for r in c.execute("PRAGMA table_info(meta_sources)").fetchall()}
    assert {"queued_at", "synced_signal", "checked_at", "held_removals", "held_at"} <= cols
