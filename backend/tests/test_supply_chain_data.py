"""Supply chain tracking data in the demo warehouse: shipment_tracking.

One row per position report — where a shipment was, and when — for ships on
real ocean lanes, freight trains, trucks and cargo flights. Locked in:
- it is shaped for a map over time: a latitude/longitude pair, a report
  time, and the shipment each report belongs to, in time order, with no
  jumps a ship, train, truck or plane could not make;
- at "now" some shipments are still moving and some have been delivered —
  at their destination, in a final report;
- it stays current: re-seeded once its newest report is a day old, never
  while it is fresh, and the same `now` always gives the same rows;
- it is small enough that the no-key preview reads all of it, and the
  existing maps can draw it.

Run from the backend directory:
    python -m pytest tests/test_supply_chain_data.py -q
"""
import datetime as dt
import math
import sqlite3

import pytest

from app.connectors import demo
from app.geo_columns import suggest_map

NOW = dt.datetime(2026, 11, 16, 12, 0)
COLUMNS = ["shipment_id", "ping_time", "latitude", "longitude", "mode", "carrier", "origin",
           "destination", "status", "speed_kmh", "units"]
STATUSES = {"In transit", "Delayed", "Delivered", "At port", "At terminal", "At DC", "At hub"}
# The fastest each mode moves, km/h, with room for the pace spread and jitter.
TOP_SPEED = {"Ocean": 60, "Rail": 70, "Truck": 110, "Air": 1100}


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "w.db"))
    demo.seed_supply_chain(NOW)
    c = sqlite3.connect(demo.WAREHOUSE_PATH)
    yield c
    c.close()


def _reports(c):
    cur = c.execute("SELECT * FROM shipment_tracking ORDER BY shipment_id, ping_time")
    return [d[0] for d in cur.description], cur.fetchall()


def _km(a, b):
    return demo._arc(a, b) * demo.EARTH_KM


def test_one_row_per_position_report(warehouse):
    columns, rows = _reports(warehouse)
    assert columns == COLUMNS
    shipments = {r[0] for r in rows}
    assert len(shipments) == 32
    # The no-key preview reads up to 2,000 rows: every report fits.
    assert 300 < len(rows) < 2000
    assert {r[4] for r in rows} == {"Ocean", "Rail", "Truck", "Air"}
    assert {r[8] for r in rows} <= STATUSES
    for r in rows:
        assert -90 <= r[2] <= 90 and -180 <= r[3] <= 180, r
        assert dt.datetime.fromisoformat(r[1]) <= NOW
        assert r[9] >= 0 and r[10] > 0


def test_each_shipment_moves_in_time_order_without_jumps(warehouse):
    _, rows = _reports(warehouse)
    by_id = {}
    for r in rows:
        by_id.setdefault(r[0], []).append(r)
    for sid, reports in by_id.items():
        times = [dt.datetime.fromisoformat(r[1]) for r in reports]
        assert times == sorted(times) and len(set(times)) == len(times), sid
        assert len(reports) >= 2, sid
        assert len({r[4] for r in reports} | {r[6] for r in reports} | {r[7] for r in reports}) == 3
        mode = reports[0][4]
        for a, b, ta, tb in zip(reports, reports[1:], times, times[1:]):
            hours = (tb - ta).total_seconds() / 3600
            km = _km((a[2], a[3]), (b[2], b[3]))
            assert km <= TOP_SPEED[mode] * hours + 25, (sid, a[1], b[1], round(km))
        # It went somewhere.
        assert _km((reports[0][2], reports[0][3]), (reports[-1][2], reports[-1][3])) > 50, sid


def test_at_now_some_are_moving_and_some_delivered_at_their_destination(warehouse):
    _, rows = _reports(warehouse)
    last = {}
    for r in rows:
        last[r[0]] = r                       # ordered by time within each shipment
    moving = [r for r in last.values() if r[8] in ("In transit", "Delayed")]
    delivered = [r for r in last.values() if r[8] == "Delivered"]
    assert len(moving) >= 8 and len(delivered) >= 8
    # Still moving means reporting now; delivered means a final report.
    assert all(r[1] == NOW.strftime("%Y-%m-%d %H:%M:%S") for r in moving)
    ends = {}
    for mode, carrier, kmh, every, waypoints in demo.SUPPLY_LANES:
        ends[(waypoints[0][0], waypoints[-1][0], mode)] = waypoints[-1][1:3]
    for r in delivered:
        lat, lon = ends[(r[6], r[7], r[4])]
        assert (r[2], r[3]) == (round(lat, 4), round(lon, 4)) and r[9] == 0
    # Only a delivered shipment has a Delivered report, and only its last.
    assert sum(r[8] == "Delivered" for r in rows) == len(delivered)


def test_the_same_now_gives_the_same_rows():
    assert demo.supply_chain_rows(NOW) == demo.supply_chain_rows(NOW)
    later = demo.supply_chain_rows(NOW + dt.timedelta(days=3))
    assert max(r[1] for r in later) == (NOW + dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")


def test_it_is_reseeded_only_once_a_day_old(warehouse):
    def newest():
        return warehouse.execute("SELECT MAX(ping_time), COUNT(*) FROM shipment_tracking").fetchone()

    first = newest()
    demo.seed_supply_chain(NOW + dt.timedelta(hours=23))     # still fresh: untouched
    assert newest() == first
    demo.seed_supply_chain(NOW + dt.timedelta(days=2))       # a day old: today's data
    assert newest()[0] == (NOW + dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    assert newest()[1] == len(demo.supply_chain_rows(NOW + dt.timedelta(days=2)))


def test_the_demo_source_serves_it_and_the_maps_can_draw_it(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "w.db"))
    conn = demo.DemoConnector()
    assert "shipment_tracking" in conn.list_tables()
    assert [c["name"] for c in conn.get_schema("shipment_tracking")] == COLUMNS
    c = sqlite3.connect(demo.WAREHOUSE_PATH)
    try:
        cur = c.execute("SELECT shipment_id, ping_time, mode, status, latitude, longitude "
                        "FROM shipment_tracking WHERE ping_time = (SELECT MAX(ping_time) "
                        "FROM shipment_tracking)")
        latest = ([d[0] for d in cur.description], [list(r) for r in cur.fetchall()])
    finally:
        c.close()
    # Where the moving shipments are now, as dots on a map.
    assert suggest_map(*latest)["type"] == "point_map"
    assert len(latest[1]) >= 8
