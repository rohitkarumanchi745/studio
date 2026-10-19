"""Maps: route_map, point_map, choropleth.

They exist only if every layer knows them: the agent's render_chart accepts
them and is told when to use them, the v2 spec keeps them, the D3 window
draws them, and without a model the fallback picks a map for a result that
plainly is one — trips with a start and an end, places with coordinates,
values per country or state. Keyword edits switch to a map by unmistakable
names only ("heat map" stays a heatmap; "roadmap" is not a map).

Run from the backend directory:
    python -m pytest tests/test_maps.py -q
"""
from app import agent, viz
from app.geo_columns import coordinate_pairs, map_for_columns, suggest_map, tokens

MAPS = ("route_map", "point_map", "choropleth")

TRIPS = (["from_stop", "to_stop", "from_lat", "from_lon", "to_lat", "to_lon", "riders"],
         [["Union Station", "Navy Pier", 41.8786, -87.6403, 41.8917, -87.6086, 120],
          ["Midway", "O'Hare", 41.7868, -87.7522, 41.9742, -87.9073, 80]])


def test_every_layer_knows_the_maps():
    for t in MAPS:
        assert t in agent.CHART_TYPES and t in agent.D3_CHART_TYPES and t in viz.CHART_TYPES
        spec = {"type": t, "x": "a", "y": ["b"]}
        assert viz.normalize_spec(spec)["type"] == t
        assert not any("chart type" in p for p in viz.validate_spec(spec, ["a", "b"]))
    assert tuple(agent.CHART_TYPES) == viz.CHART_TYPES
    # A map in a visual answer stays a map (the D3 window draws it).
    out = agent.d3_only({"chart": {"type": "route_map", "x": "a", "y": []}})
    assert out["chart"]["type"] == "route_map"


def test_coordinate_columns_pair_by_name_and_say_where_a_trip_starts_and_ends():
    assert tokens("PickupLatitude") == ["pickup", "latitude"]
    pairs = coordinate_pairs(*TRIPS)
    assert [(k, role) for k, role, *_ in pairs] == [("from", "origin"), ("to", "dest")]
    assert [p[1] for p in coordinate_pairs(["stop_lat", "stop_lon"], [[41.9, -87.6]])] == ["point"]
    assert [p[1] for p in coordinate_pairs(
        ["pickup_latitude", "pickup_longitude", "dropoff_latitude", "dropoff_longitude"],
        [[40.7, -74.0, 40.6, -73.9]])] == ["origin", "dest"]
    # Not degrees, or unpaired: not a place.
    assert coordinate_pairs(["lat", "lon"], [[400, 20]]) == []
    assert coordinate_pairs(["lat", "population"], [[40, 20]]) == []


def test_without_a_model_a_result_that_is_a_map_gets_one():
    assert agent._auto_chart(*TRIPS, "trips") == {
        "type": "route_map", "title": "Routes", "x": "from_stop", "y": ["riders"]}
    assert agent._auto_chart(["stop_name", "stop_lat", "stop_lon", "boardings"],
                             [["Howard", 42.019, -87.6728, 4000]], "stops")["type"] == "point_map"
    assert agent._auto_chart(["country", "revenue"], [["France", 10]], "sales") == {
        "type": "choropleth", "title": "revenue by country", "x": "country", "y": ["revenue"]}
    # Movement between countries, by name: routes, not one end shaded.
    assert agent._auto_chart(["origin_country", "destination_country", "passengers"],
                             [["US", "GB", 9000]], "flights") == {
        "type": "route_map", "title": "origin_country → destination_country",
        "x": "origin_country", "y": ["destination_country", "passengers"]}
    # Everything else is as before.
    assert agent._auto_chart(["region", "revenue"], [["West", 1]], "sales")["type"] == "bar"
    assert agent._auto_chart(["order_date", "revenue"], [["2026-01-01", 1]], "sales")["type"] == "line"
    assert suggest_map(["state_count"], [[3]]) is None


def _switched(prompt, columns=("region", "revenue")):
    chart = {"type": "bar", "x": columns[0], "y": [columns[-1]]}
    return agent._canvas_fallback(prompt, list(columns), chart)["chart"]["type"]


def test_keyword_edits_switch_to_a_map_by_its_name():
    assert _switched("show the routes as a flow map") == "route_map"
    assert _switched("make it a choropleth") == "choropleth"
    assert _switched("plot them as a bubble map") == "point_map"
    # "a map": the map the columns are for.
    assert _switched("show it on a map", TRIPS[0]) == "route_map"
    assert _switched("show it on a map", ("stop_lat", "stop_lon", "boardings")) == "point_map"
    assert _switched("show it on a map", ("state", "revenue")) == "choropleth"
    assert _switched("show it on a map", ("from_state", "to_state", "loads")) == "route_map"
    assert map_for_columns(["region", "revenue"]) == "point_map"
    # Not maps.
    assert _switched("turn this into a heat map") == "heatmap"
    assert _switched("what is on the product roadmap") == "bar"


def test_the_agent_is_told_when_to_draw_a_map():
    # The system prompt's rules and render_chart's argument docs, as written.
    source = open(agent.__file__).read()
    assert "movement\n  between places (transit, shipments, trips, flights) → route_map" in source
    assert "SELECT the coordinate columns with lat/lon in their names" in source
    assert "route_map (movement between places — transit," in source
