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
