"""The D3 & artifacts home's starts mean what they say on the server.

The home (frontend VisualHome.jsx QUICK_STARTS) fills the composer with a
phrase to finish. Read straight from that file, each phrase — finished the way
a person would — is checked against what the server does with it:

  - Automate asks for an Autopilot agent on an interval Autopilot keeps
    (weekly), with a clean goal — no time of day it would silently drop;
  - Table asks for a table (the D3 window shows one);
  - Chart, Table, Dashboard, Map and Custom visual are questions, never
    mistaken for an Autopilot request.

And the demo data has places, so Map has something to draw: Chicago 'L'
rides with each end's coordinates, shipments between countries, US stores.

Run from the backend directory:
    python -m pytest tests/test_visual_starts.py -q
"""
import os
import pathlib
import re
import sqlite3

import pytest

from app import agent, autopilot_intent
from app.geo_columns import suggest_map

HOME = (pathlib.Path(__file__).resolve().parents[2]
        / "frontend" / "src" / "components" / "VisualHome.jsx")


def _starts():
    if not HOME.exists():
        pytest.skip("frontend not checked out")
    block = HOME.read_text().split("export const QUICK_STARTS", 1)[1].split("];", 1)[0]
    return dict(re.findall(r'key: "(\w+)".*?text: "([^"]+)"', block, re.S))


def _finished(text, what="revenue by region"):
    return text.replace("…", what)


def test_every_start_is_read_from_the_home():
    assert set(_starts()) == {"chart", "table", "dashboard", "map", "custom", "automate"}


def test_automate_is_a_weekly_agent_with_a_clean_goal():
    draft = autopilot_intent.parse(_finished(_starts()["automate"]))
    assert draft["trigger_type"] == "schedule"
    assert draft["trigger_config"] == {"interval_seconds": 604800}
    assert draft["goal"] == "Chart revenue by region"


def test_the_other_starts_are_questions_and_table_asks_for_a_table():
    starts = _starts()
    for key in ("chart", "table", "dashboard", "map", "custom"):
        assert autopilot_intent.parse(_finished(starts[key])) is None, key
    assert agent.asked_for_table(_finished(starts["table"]))
    assert not agent.asked_for_table(_finished(starts["chart"]))


def test_the_demo_data_has_places_for_every_map(tmp_path, monkeypatch):
    from app.connectors import demo
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "w.db"))
    demo.seed()
    demo.seed()                                            # idempotent
    c = sqlite3.connect(demo.WAREHOUSE_PATH)

    def result(sql):
        cur = c.execute(sql)
        return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]

    trips = result("SELECT line, from_stop, to_stop, from_lat, from_lon, to_lat, to_lon, "
                   "SUM(riders) AS riders FROM transit_trips GROUP BY 1, 2, 3, 4, 5, 6, 7")
    assert suggest_map(*trips)["type"] == "route_map"
    assert len(trips[1]) == 20                             # 10 segments, both directions
    stores = result("SELECT store, state, latitude, longitude, revenue FROM stores")
    assert suggest_map(*stores)["type"] == "point_map" and len(stores[1]) == 26
    lanes = result("SELECT origin_country, destination_country, SUM(units) AS units "
                   "FROM shipments GROUP BY 1, 2")
    assert suggest_map(*lanes) == {"type": "route_map",
                                   "title": "origin_country → destination_country",
                                   "x": "origin_country", "y": ["destination_country", "units"]}
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 18
