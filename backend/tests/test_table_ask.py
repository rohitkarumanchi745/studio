"""Asking for a table in the D3 & artifacts window gets a table.

The window always visualizes, so a model told "ALWAYS visualize" tended to
draw a bar for "show it as a table". Now the agent is told a table is the
visual, and an explicit ask wins over whatever chart came back — the
model's, the keyless fallback's, a verified query's — before the answer is
shown. A sentence that merely names a database table ("rows in the sales
table") is not an ask.

Run from the backend directory:
    python -m pytest tests/test_table_ask.py -q
"""
import pytest

from app import agent


@pytest.mark.parametrize("prompt", [
    "show revenue by region as a table",
    "Give me sales by month in a table",
    "put it into a table please",
    "table view of orders by status",
    "show the results in tabular form",
    "show me a table of revenue by region",
    "list a table of top customers",
    "as a data table",
])
def test_an_ask_for_a_table(prompt):
    assert agent.asked_for_table(prompt)


@pytest.mark.parametrize("prompt", [
    "how many sales are in the sales table",
    "count the rows in the table",
    "which table has revenue",
    "revenue by region",
    "show a bar chart of sales",
    "join the orders table with customers",
    None,
])
def test_not_an_ask_for_a_table(prompt):
    assert not agent.asked_for_table(prompt)


def _answer():
    chart = {"type": "bar", "title": "Revenue", "x": "region", "y": ["revenue"]}
    return {"chart": dict(chart), "panels": [
        {"columns": ["region", "revenue"], "rows": [["West", 1]], "chart": dict(chart)},
        {"columns": ["region", "revenue"], "rows": [["West", 1]], "chart": dict(chart),
         "visual": {"title": "Rings", "code": "root.append('svg')"}},
    ]}


def test_the_window_shows_a_table_when_one_is_asked_for():
    out = agent.d3_only(_answer(), "show revenue by region as a table")
    assert out["chart"]["type"] == "table"
    assert [p["chart"]["type"] for p in out["panels"]] == ["table", "table"]
    assert out["panels"][1]["visual"] is None            # not the custom visual instead
    assert all(p["engine"] == "d3" for p in out["panels"]) and out["visual_mode"]
    # The rest of the spec stays, so switching back to a chart works.
    assert out["chart"]["x"] == "region" and out["chart"]["y"] == ["revenue"]


def test_otherwise_the_chart_is_left_as_it_was():
    out = agent.d3_only(_answer(), "revenue by region")
    assert [p["chart"]["type"] for p in out["panels"]] == ["bar", "bar"]
    assert out["panels"][1]["visual"]["title"] == "Rings"
    # An unknown type still becomes a bar; no prompt at all is fine.
    odd = agent.d3_only({"chart": {"type": "mystery", "x": "a", "y": []}})
    assert odd["chart"]["type"] == "bar"


def test_the_agent_is_told_a_table_is_the_visual():
    source = open(agent.__file__).read()
    assert "When the user asks for a table" in source
    assert "the table is the visual" in source


# ── Any chart named in the question ─────────────────────────────────────

STORES = (["store", "latitude", "longitude", "revenue"], [["NYC", 40.7, -74.0, 5], ["LA", 34.0, -118.2, 7]])


@pytest.mark.parametrize("prompt, kind", [
    ("Show revenue by region as a bar chart", "bar"),
    ("revenue share as a pie chart", "pie"),
    ("show it as a stacked bar chart", "stacked_bar"),
    ("an area chart of revenue", "area"),
    ("use a sankey", "sankey"),
    ("revenue heat map", "heatmap"),
    ("Show stores on a map", "point_map"),           # the map the data's places call for
    ("show it as a table", "table"),
])
def test_a_named_chart(prompt, kind):
    assert agent.asked_chart(prompt, *STORES) == kind


@pytest.mark.parametrize("prompt", [
    "line items by order", "show the sales funnel", "sales in the area", "barcode scans by store",
    "revenue in area 51",
    "Write a custom interactive D3 visual of revenue by region",
    "Build a dashboard of revenue with a view by day, by month and by year",
    "Every week, chart revenue by region",
])
def test_no_chart_named(prompt):
    assert agent.asked_chart(prompt, *STORES) is None


def test_a_map_is_asked_for_only_where_the_data_has_places():
    assert agent.asked_chart("Show sales on a map", ["region", "revenue"], [["West", 1]]) is None


def test_the_window_draws_the_chart_named_and_keeps_a_custom_visual():
    out = agent.d3_only(_answer(), "Show revenue by region as a pie chart")
    assert out["chart"]["type"] == "pie"
    assert [p["chart"]["type"] for p in out["panels"]] == ["pie", "pie"]
    assert out["panels"][1]["visual"]["title"] == "Rings"    # the visual written stays
    # On a map: each panel from its own data.
    stores = {"chart": {"type": "bar", "x": "store", "y": ["revenue"]}, "columns": STORES[0],
              "rows": STORES[1], "panels": [{"columns": STORES[0], "rows": STORES[1],
                                             "chart": {"type": "bar", "x": "store", "y": ["revenue"]}}]}
    out = agent.d3_only(stores, "Show stores on a map")
    assert out["chart"]["type"] == "point_map" and out["panels"][0]["chart"]["type"] == "point_map"
