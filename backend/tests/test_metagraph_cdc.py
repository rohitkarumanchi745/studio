"""Metadata change data capture: schema changes reach the graph as events.

Acceptance: alter, create or drop one table and exactly that table is re-read
— no whole-source rediscovery — with the relationships and certified metrics
that depend on it following. Events are accepted durably and once, applied in
a way that a late or reordered one cannot roll the graph back, retried and then
dead-lettered for replay; the poller's checkpoint moves only after acceptance;
a change too big for events (a mass drop) goes to a full rediscovery where the
hold applies; and a full discovery hands over to the stream without losing a
change made while it ran.
"""
import hashlib
import hmac
import json
import sqlite3
import time
import types
import uuid

import pytest
from fastapi import HTTPException

from test_metagraph_discovery import ADMIN, VIEWER, RetailConnector, env, stored  # noqa: F401


class TrackedRetail(RetailConnector):
    """The retail stand-in with what Postgres and Databricks offer change
    capture — a token per table, a one-table describe, a source-wide signal —
    and a record of which tables were read."""

    def __init__(self, path):
        super().__init__(path)
        self.reads = []
        self.signals_on = True
        self.after_describe = None

    def _master(self):
        with self._c() as c:
            return c.execute("SELECT name, type, sql FROM sqlite_master "
                             "WHERE type IN ('table','view') ORDER BY name").fetchall()

    def table_signals(self):
        if not self.signals_on:
            return None
        return {n: hashlib.sha256(f"{t}|{s}".encode()).hexdigest()[:16]
                for n, t, s in self._master()}

    def change_signal(self):
        return hashlib.sha256(repr(self._master()).encode()).hexdigest()

    def describe_relations(self):
        self.reads.append("*")
        out = super().describe_relations()
        if self.after_describe:
            hook, self.after_describe = self.after_describe, None
            hook()
        return out

    def describe_relation(self, name):
        self.reads.append(name)
        with self._c() as c:
            row = c.execute("SELECT type FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None:
            return None
        return {"kind": row[0], "namespace": "studio_dev.retail_qa",
                "columns": self.get_schema(name)}


@pytest.fixture()
def cdc(env, tmp_path):
    from app import connectors, metacdc
    from app.connectors import demo
    tracked = TrackedRetail(env.warehouse)
    env.mp.setitem(connectors._REGISTRY, "retail", tracked)
    env.mp.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo_warehouse.db"))
    env.mp.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "0")
    env.retail, env.cdc = tracked, metacdc
    env.mg.discover("retail", "test")
    tracked.reads.clear()
    return env


def warehouse(env, *statements):
    with sqlite3.connect(env.warehouse) as c:
        for sql in statements:
            c.execute(sql)


def tick(env):
    """What the scheduler queued for retail this pass (None when nothing)."""
    return env.mg.tick_once()["queued"].get("retail")


def events(env):
    with env.db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM meta_events WHERE source='retail' "
            "ORDER BY received_at, event_id").fetchall()]


def jobs_of(env, kind, status="queued"):
    with env.db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM background_jobs WHERE kind=? AND status=?", (kind, status)).fetchall()]


def discoveries(env):
    return [json.loads(j["payload"])["trigger"] for j in jobs_of(env, "metagraph_discover")
            if json.loads(j["payload"])["source"] == "retail"]


def drain(env, kinds=("metagraph_event", "metagraph_refresh", "metagraph_discover")):
    """What the worker does, without waiting out run_after or the backoff: run
    queued jobs, each attempt counted as claim() counts it, re-queuing a
    failure while attempts remain."""
    handlers, out = env.jobs.handlers(), []
    ph = ",".join("?" for _ in kinds)
    for _ in range(200):
        with env.db.connect() as c:
            row = c.execute(f"SELECT * FROM background_jobs WHERE status='queued' AND kind IN "
                            f"({ph}) ORDER BY created_at, id LIMIT 1", tuple(kinds)).fetchone()
        if row is None:
            return out
        job = {**dict(row), "attempts": row["attempts"] + 1}
        try:
            out.append(handlers[row["kind"]](json.loads(row["payload"]), job))
            status = "done"
        except Exception as exc:
            out.append(exc)
            status = "queued" if job["attempts"] < job["max_attempts"] else "failed"
        with env.db.connect() as c:
            c.execute("UPDATE background_jobs SET status=?, attempts=? WHERE id=?",
                      (status, job["attempts"], row["id"]))
            c.commit()
    raise AssertionError("the queue never drained")


def columns_of(env, name):
    rel = next(r for r in env.mg.get_source("retail", user=ADMIN)["relations"]
               if r["name"] == name)
    return [c["name"] for c in rel["columns"]]


def event(eid, asset="retail_stores", etype="schema_changed", **more):
    return {"event_id": eid, "source": "retail", "asset": asset, "event_type": etype, **more}


def ingest(env, *evts, origin="webhook"):
    return env.cdc.ingest(list(evts), origin)


# ── The acceptance: one changed table, one event, one table re-read ──────

def test_an_altered_table_becomes_one_event_and_only_that_table_is_reread(cdc):
    assert tick(cdc) is None and events(cdc) == []
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
    assert tick(cdc) is None                               # no whole-source rediscovery
    assert discoveries(cdc) == []
    [e] = events(cdc)
    assert (e["asset"], e["event_type"], e["origin"], e["status"]) == \
        ("retail_stores", "schema_changed", "poller", "accepted")
    drain(cdc)
    assert columns_of(cdc, "retail_stores") == ["store_id", "store_name", "region", "opened_on"]
    assert cdc.retail.reads == ["retail_stores"]           # no bulk describe, no other table
    assert events(cdc)[0]["status"] == "applied"
    assert json.loads(events(cdc)[0]["result"])["change"] == "changed"
    assert tick(cdc) is None and len(events(cdc)) == 1     # the checkpoint moved


def test_a_created_table_and_a_dropped_one_arrive_as_their_own_events(cdc):
    warehouse(cdc, "CREATE TABLE retail_promotions (promo_id INTEGER, name TEXT)",
              "DROP TABLE retail_returns")
    assert tick(cdc) is None
    assert {(e["asset"], e["event_type"]) for e in events(cdc)} == {
        ("retail_promotions", "asset_created"), ("retail_returns", "asset_dropped")}
    drain(cdc)
    objs = stored(cdc)
    assert objs["retail_promotions"]["removed_at"] is None
    assert objs["retail_promotions"]["kind"] == "table"           # from describe_relation
    assert objs["retail_promotions"]["namespace"] == "studio_dev.retail_qa"
    assert objs["retail_returns"]["removed_at"] is not None
    assert cdc.retail.reads == ["retail_promotions"]
    assert cdc.mg.get_source("retail", user=ADMIN)["removed"][0]["name"] == "retail_returns"


def test_dependent_metrics_and_relationships_follow_a_table_change(cdc):
    from app import queries, semantic
    queries.init_tables()
    semantic.init_tables()
    try:
        semantic.apply_yaml(semantic.YamlIn(yaml="""
models:
  - source: retail
    table: retail_inventory
    metrics:
      - name: stock_on_hand
        agg: sum
        expr: on_hand
        certified: true
"""), user=ADMIN)

        def stock():
            return next(m for m in cdc.mg.list_metrics(user=ADMIN)["metrics"]
                        if m["name"] == "stock_on_hand")
        assert stock()["status"] == "valid"
        warehouse(cdc, "ALTER TABLE retail_inventory DROP COLUMN on_hand")
        tick(cdc), drain(cdc)
        assert stock()["status"] == "broken"
        assert "on_hand" in " ".join(stock()["reasons"])

        warehouse(cdc, "CREATE TABLE retail_suppliers (supplier_id INTEGER, name TEXT)",
                  "ALTER TABLE retail_products ADD COLUMN supplier_id INTEGER")
        tick(cdc), drain(cdc)
        rels = cdc.mg.source_relationships("retail", user=ADMIN)["relationships"]
        assert any(r["from_object"] == "retail_products" and r["to_object"] == "retail_suppliers"
                   and r["from_columns"] == ["supplier_id"] for r in rels)
        assert sorted(cdc.retail.reads) == ["retail_inventory", "retail_products",
                                            "retail_suppliers"]
    finally:
        semantic._STATE.update(doc=None, yaml="", source=None)


def test_a_data_change_only_marks_when_the_table_last_changed(cdc):
    before = stored(cdc)
    with cdc.db.connect() as c:
        rels = [dict(r) for r in c.execute("SELECT * FROM meta_relationships").fetchall()]
    ingest(cdc, event("rows-2", "retail_orders", "data_changed", occurred_at=1_800_000_000))
    ingest(cdc, event("rows-1", "retail_orders", "data_changed",
                      occurred_at="2023-11-14T22:13:20Z"))           # older, arrives later
    drain(cdc)
    assert stored(cdc) == before and cdc.retail.reads == []
    with cdc.db.connect() as c:
        assert [dict(r) for r in c.execute("SELECT * FROM meta_relationships").fetchall()] == rels
    orders = next(r for r in cdc.mg.get_source("retail", user=ADMIN)["relations"]
                  if r["name"] == "retail_orders")
    assert orders["data_changed_at"] == 1_800_000_000                # never moved back


# ── Intake: durable, once, validated ─────────────────────────────────────

def test_intake_records_the_event_with_its_job_and_ignores_a_repeat(cdc):
    assert ingest(cdc, event("evt-1")) == {"accepted": ["evt-1"], "duplicates": [],
                                           "rejected": []}
    assert ingest(cdc, event("evt-1")) == {"accepted": [], "duplicates": ["evt-1"],
                                           "rejected": []}
    assert len(jobs_of(cdc, "metagraph_event")) == 1

    def no_queue(*a, **k):
        raise RuntimeError("queue unavailable")
    cdc.mp.setattr(cdc.jobs, "enqueue", no_queue)
    with pytest.raises(RuntimeError):
        ingest(cdc, event("evt-2"))
    assert cdc.cdc.get_event("evt-2") is None             # no job, no event: never half-taken


@pytest.mark.parametrize("raw, reason", [
    ({"source": "retail", "event_type": "schema_changed"}, "event_id"),
    (event("bad id!"), "event_id"),
    (event("e1", etype="renamed"), "event_type"),
    ({**event("e2"), "source": "elsewhere"}, "unknown source"),
    ({**event("e3"), "tenant_id": "another-deployment"}, "another tenant"),
    (event("e4", asset=None, etype="data_changed"), "needs an asset"),
    ({**event("e5"), "occurred_at": "yesterday"}, "occurred_at"),
    ({**event("e6"), "source_version": True}, "source_version"),
    ("not an object", "object"),
])
def test_a_malformed_event_is_rejected_with_the_reason(cdc, raw, reason):
    out = cdc.cdc.ingest([raw], "webhook")
    assert out["accepted"] == [] and reason in out["rejected"][0]["reason"]
    assert events(cdc) == []


def test_an_event_without_a_table_is_about_the_whole_source(cdc):
    ingest(cdc, event("whole", asset=None), {**event("own", asset=None), "tenant_id": "studio"})
    assert [e["event_type"] for e in events(cdc)] == ["source_changed", "source_changed"]
    warehouse(cdc, "CREATE TABLE retail_promotions (promo_id INTEGER)")
    drain(cdc)
    assert [e["status"] for e in events(cdc)] == ["applied", "superseded"]   # one rediscovery
    assert "retail_promotions" in stored(cdc)


# ── Ordering: a late event cannot roll the graph back ────────────────────

def test_a_whole_source_event_is_covered_by_a_rediscovery_already_queued(cdc):
    cdc.mg.enqueue("retail", "manual")
    ingest(cdc, event("whole", asset=None))
    drain(cdc, kinds=("metagraph_event",))
    e = cdc.cdc.get_event("whole")
    assert e["status"] == "superseded" and "already queued" in e["result"]
    assert discoveries(cdc) == ["manual"]


def test_an_event_that_arrived_before_a_full_read_needs_no_work(cdc):
    ingest(cdc, event("early"))
    cdc.mg.discover("retail", "test")                # started after the event arrived
    drain(cdc)
    [e] = events(cdc)
    assert e["status"] == "superseded" and "after the event arrived" in e["result"]
    assert cdc.retail.reads == ["*"]                 # only the full discovery read anything


def test_same_origin_order_skips_stale_events_and_other_origins_are_not_compared(cdc):
    def status(eid):
        drain(cdc)
        return cdc.cdc.get_event(eid)["status"]
    assert status(ingest(cdc, event("v5", source_version=5))["accepted"][0]) == "applied"
    assert status(ingest(cdc, event("v3", source_version=3))["accepted"][0]) == "superseded"
    assert status(ingest(cdc, event("v7", source_version="7"))["accepted"][0]) == "applied"
    assert status(ingest(cdc, event("t2", "retail_orders", occurred_at=2_000))["accepted"][0]) \
        == "applied"
    assert status(ingest(cdc, event("t1", "retail_orders", occurred_at=1_000))["accepted"][0]) \
        == "superseded"
    # Studio's own events count on a different clock: never compared.
    assert status(ingest(cdc, event("s1", source_version=1), origin="studio")["accepted"][0]) \
        == "applied"


def test_a_late_event_re_reads_the_current_state(cdc):
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT",
              "ALTER TABLE retail_stores ADD COLUMN closed_on TEXT")
    ingest(cdc, event("ancient", occurred_at=1))        # about some long-gone version
    drain(cdc)
    assert columns_of(cdc, "retail_stores")[-2:] == ["opened_on", "closed_on"]


def test_an_older_full_read_never_overwrites_a_newer_table_read(cdc):
    started = time.time()
    objects, complete, signal, _tokens = cdc.mg._read_source("retail")   # an older read
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT",
              "CREATE TABLE retail_promotions (promo_id INTEGER)")
    assert cdc.mg.discover_asset("retail", "retail_stores")["change"] == "changed"
    assert cdc.mg.discover_asset("retail", "retail_promotions")["change"] == "added"
    snapshot = str(uuid.uuid4())
    cdc.mg._start("retail", snapshot, "test", started)
    cdc.mg._write("retail", snapshot, objects, complete, started, signal)
    assert columns_of(cdc, "retail_stores")[-1] == "opened_on"         # kept
    assert stored(cdc)["retail_promotions"]["removed_at"] is None        # not tombstoned

    # The other way round: a table read that began before a newer record.
    cdc.mp.setattr(cdc.mg, "time", types.SimpleNamespace(time=lambda: started - 60))
    assert cdc.mg.discover_asset("retail", "retail_stores")["change"] == "superseded"


# ── Failures: retried, then dead, then replayed ──────────────────────────

def test_a_failing_event_is_retried_then_dead_and_can_be_replayed(cdc):
    cdc.retail.fail_columns = {"retail_stores"}
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
    tick(cdc)
    drain(cdc)
    [e] = events(cdc)
    assert (e["status"], e["attempts"]) == ("dead", 4)
    assert "permission denied" in e["error"]
    health = next(r for r in cdc.mg.metadata_health(user=ADMIN)["sources"]
                  if r["source"] == "retail")
    assert health["freshness"] == "stale" and health["dead_events"] == 1
    assert any("failed on every attempt" in r for r in health["reasons"])
    assert [x["event_id"] for x in cdc.cdc.list_events(status="dead", user=ADMIN)["events"]] \
        == [e["event_id"]]

    cdc.retail.fail_columns = set()
    assert cdc.cdc.replay_event(e["event_id"], user=ADMIN)["job_id"]
    drain(cdc)
    assert cdc.cdc.get_event(e["event_id"])["status"] == "applied"
    assert columns_of(cdc, "retail_stores")[-1] == "opened_on"
    health = next(r for r in cdc.mg.metadata_health(user=ADMIN)["sources"]
                  if r["source"] == "retail")
    assert health["dead_events"] == 0 and health["freshness"] == "fresh"


def test_the_real_queue_runs_an_event(cdc):
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
    ingest(cdc, event("via-queue"))
    assert cdc.jobs.run_one("test-worker", kinds=["metagraph_event"])
    e = cdc.cdc.get_event("via-queue")
    assert (e["status"], e["attempts"]) == ("applied", 1)
    assert columns_of(cdc, "retail_stores")[-1] == "opened_on"


def test_an_event_for_a_source_that_went_away_is_ignored(cdc):
    ingest(cdc, event("gone"))
    cdc.mp.setattr(cdc.mg, "_live_sources", lambda: ["demo"])
    drain(cdc)
    assert cdc.cdc.get_event("gone")["status"] == "ignored"


# ── The poller's checkpoint ──────────────────────────────────────────────

def test_the_checkpoint_moves_only_after_the_events_are_accepted(cdc):
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
    real_ingest, real_save = cdc.cdc.ingest, cdc.cdc.save_offsets

    def down(*a, **k):
        raise RuntimeError("database unavailable")
    cdc.mp.setattr(cdc.cdc, "ingest", down)
    tick(cdc)                                             # the failure is logged, not raised
    assert events(cdc) == []
    cdc.mp.setattr(cdc.cdc, "ingest", real_ingest)
    tick(cdc)
    assert [e["asset"] for e in events(cdc)] == ["retail_stores"]   # found again

    # Accepted, then a crash before the checkpoint: the same id comes again.
    warehouse(cdc, "ALTER TABLE retail_products ADD COLUMN brand TEXT")
    cdc.mp.setattr(cdc.cdc, "save_offsets", down)
    tick(cdc)
    cdc.mp.setattr(cdc.cdc, "save_offsets", real_save)
    tick(cdc)
    assert [e["asset"] for e in events(cdc)] == ["retail_stores", "retail_products"]


def test_a_mass_drop_goes_to_a_full_rediscovery_where_the_hold_applies(cdc):
    warehouse(cdc, "DROP VIEW retail_sales_detail", "DROP TABLE retail_returns",
              "DROP TABLE retail_inventory", "DROP TABLE retail_stores",
              "DROP TABLE retail_customers")
    assert tick(cdc) == "large_change" and events(cdc) == []
    drain(cdc)
    state = cdc.mg._state("retail")
    assert state["status"] == "held" and len(json.loads(state["held_removals"])) == 5
    assert all(o["removed_at"] is None for o in stored(cdc).values())


def test_more_changed_tables_than_events_are_worth_go_to_a_full_rediscovery(cdc):
    cdc.mp.setenv("STUDIO_METAGRAPH_MAX_TABLE_EVENTS", "1")
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT",
              "ALTER TABLE retail_products ADD COLUMN brand TEXT")
    assert tick(cdc) == "large_change" and events(cdc) == []


def test_a_change_made_while_a_full_discovery_runs_arrives_as_an_event(cdc):
    cdc.retail.after_describe = lambda: warehouse(
        cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
    cdc.mg.discover("retail", "test")
    assert "opened_on" not in columns_of(cdc, "retail_stores")      # the snapshot missed it
    assert tick(cdc) is None
    assert [e["asset"] for e in events(cdc)] == ["retail_stores"]
    drain(cdc)
    assert columns_of(cdc, "retail_stores")[-1] == "opened_on"


def test_a_table_whose_columns_could_not_be_read_is_reread_on_its_own(cdc):
    cdc.retail.fail_columns = {"retail_stores"}
    cdc.retail.bulk = False
    cdc.mg.discover("retail", "test")                  # partial: retail_stores unread
    cdc.retail.fail_columns = set()
    tick(cdc)
    assert [(e["asset"], e["event_type"]) for e in events(cdc)] == \
        [("retail_stores", "asset_created")]


def test_a_source_without_table_tokens_keeps_its_source_wide_signal(cdc):
    cdc.retail.signals_on = False
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
    assert tick(cdc) == "change_detected" and events(cdc) == []


def test_a_source_discovered_before_change_capture_starts_its_checkpoint_safely(cdc):
    cdc.cdc.reset_offsets("retail")                    # as an upgraded deployment has it
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT")
    assert tick(cdc) == "change_detected"              # caught by the source-wide signal
    assert cdc.cdc.offsets("retail")[0] is not None     # and the checkpoint has begun
    assert events(cdc) == []


def test_a_burst_of_table_changes_costs_one_refresh(cdc):
    from app import metarelations
    cdc.mp.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "1000000000")
    calls = []
    real = metarelations.relate
    cdc.mp.setattr(metarelations, "relate", lambda *a, **k: calls.append(a) or real(*a, **k))
    warehouse(cdc, "ALTER TABLE retail_stores ADD COLUMN opened_on TEXT",
              "ALTER TABLE retail_products ADD COLUMN brand TEXT",
              "CREATE TABLE retail_promotions (promo_id INTEGER)")
    tick(cdc)
    drain(cdc, kinds=("metagraph_event",))
    [refresh] = jobs_of(cdc, "metagraph_refresh")
    assert refresh["run_after"] > time.time() and calls == []
    drain(cdc, kinds=("metagraph_refresh",))
    assert len(calls) == 1


# ── Scope: what an event can and cannot do ───────────────────────────────

def test_an_asset_resolves_to_the_stored_name_whatever_its_spelling(cdc):
    names = ["retail_orders", "retail_stores", "Orders", "orders"]
    assert cdc.mg.match_asset("retail_orders", names) == "retail_orders"
    assert cdc.mg.match_asset("RETAIL_STORES", names) == "retail_stores"
    assert cdc.mg.match_asset("studio_dev.retail_qa.retail_stores", names) == "retail_stores"
    assert cdc.mg.match_asset("ORDERS", names) is None             # two fit: neither
    assert cdc.mg.match_asset("orders", names) == "orders"
    assert cdc.mg.match_asset("retail_orders", ["main.a.retail_orders"]) == "main.a.retail_orders"
    assert cdc.mg.match_asset("nope", names) is None


def test_an_event_for_a_table_outside_the_source_changes_nothing(cdc):
    before = stored(cdc)
    ingest(cdc, event("outside", "secret_schema.payroll"))
    drain(cdc)
    e = cdc.cdc.get_event("outside")
    assert e["status"] == "ignored" and json.loads(e["result"])["change"] == "absent"
    assert stored(cdc) == before


def test_a_removal_is_never_taken_from_a_listing_that_cannot_prove_it(cdc):
    warehouse(cdc, "DROP TABLE retail_returns")
    cdc.retail.tables_truncated = True
    assert cdc.mg.discover_asset("retail", "retail_returns")["change"] == "deferred"
    cdc.retail.tables_truncated = False
    with cdc.db.connect() as c:
        c.execute("UPDATE meta_sources SET held_removals=? WHERE source='retail'",
                  (json.dumps(["retail_inventory"]),))
        c.commit()
    assert cdc.mg.discover_asset("retail", "retail_returns")["change"] == "deferred"
    assert stored(cdc)["retail_returns"]["removed_at"] is None


def test_a_removed_connection_forgets_its_checkpoint(cdc):
    assert cdc.cdc.offsets("retail")[0] is not None
    cdc.mg._on_source_event("retail", "removed")
    assert cdc.cdc.offsets("retail")[0] is None


def test_settled_events_are_pruned_and_dead_ones_kept(cdc):
    ingest(cdc, event("old-applied"), event("old-dead"))
    with cdc.db.connect() as c:
        c.execute("UPDATE meta_events SET status='applied', received_at=1 "
                  "WHERE event_id='old-applied'")
        c.execute("UPDATE meta_events SET status='dead', received_at=1 WHERE event_id='old-dead'")
        c.commit()
    assert cdc.cdc.prune(time.time()) == 1
    assert [e["event_id"] for e in events(cdc)] == ["old-dead"]


# ── The webhook and the administration endpoints ─────────────────────────

def _signed(body, secret="hook-secret"):
    return {"x-studio-signature": "sha256=" + hmac.new(secret.encode(), body,
                                                        hashlib.sha256).hexdigest()}


def test_the_webhook_takes_one_event_or_a_batch_and_still_the_old_form(cdc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(cdc.mg.router, prefix="/api")
    app.include_router(cdc.cdc.router, prefix="/api")
    client = TestClient(app)
    cdc.mp.setenv("STUDIO_METAGRAPH_WEBHOOK_SECRET", "hook-secret")

    def post(payload, secret="hook-secret"):
        body = json.dumps(payload).encode()
        return client.post("/api/metagraph/events", content=body, headers=_signed(body, secret))

    one = {**event("dbt-1"), "tenant_id": "studio", "occurred_at": "2026-10-17T18:00:00Z"}
    assert post(one, "wrong").status_code == 401
    r = post(one)
    assert r.status_code == 202 and r.json()["accepted"] == ["dbt-1"]
    assert post(one).json()["duplicates"] == ["dbt-1"]
    r = post({"events": [event("dbt-2"), {**event("dbt-3"), "tenant_id": "another"}]})
    assert r.status_code == 202 and r.json()["accepted"] == ["dbt-2"]
    assert r.json()["rejected"] == [{"event_id": "dbt-3",
                                     "reason": "the event is for another tenant"}]
    assert post({"events": [{**event("dbt-4"), "source": "nope"}]}).status_code == 400
    assert post({"events": []}).status_code == 400
    assert post({"source": "retail"}).json() == {"queued": ["retail"], "unknown": []}


def test_the_event_log_and_change_capture_status_are_for_administrators(cdc):
    ingest(cdc, event("e-1"))
    for call in (lambda u: cdc.cdc.list_events(user=u), lambda u: cdc.cdc.cdc_status(user=u),
                 lambda u: cdc.cdc.replay_event("e-1", user=u),
                 lambda u: cdc.cdc.reset_cdc("retail", user=u)):
        with pytest.raises(HTTPException) as exc:
            call(VIEWER)
        assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        cdc.cdc.list_events(status="lost", user=ADMIN)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        cdc.cdc.replay_event("no-such-event", user=ADMIN)
    assert exc.value.status_code == 404
    retail = next(s for s in cdc.cdc.cdc_status(user=ADMIN)["sources"] if s["source"] == "retail")
    assert retail["per_table"] and retail["tables_tracked"] == 8
    assert retail["events"] == {"accepted": 1}
    assert cdc.cdc.reset_cdc("retail", user=ADMIN)["job_id"]
    assert cdc.cdc.offsets("retail")[0] is None and discoveries(cdc) == ["cdc_reset"]
