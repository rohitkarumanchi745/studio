"""The D3-drawn chart types — network, beeswarm, calendar, circle_pack.

They exist only if every layer knows them: the agent's render_chart accepts
them, the v2 spec keeps them (an unknown type is silently normalized to "bar"),
and the no-LLM keyword fallback can switch to them. The fallback reacts to
unmistakable chart NAMES only: "revenue by ad network" or "by calendar month"
names a column, not a chart, and must leave the chart alone.
"""
from app import agent, viz

D3_TYPES = ("network", "beeswarm", "calendar", "circle_pack")


def test_the_agent_and_the_spec_know_the_d3_types():
    for t in D3_TYPES:
        assert t in agent.CHART_TYPES and t in viz.CHART_TYPES
    # vocab.CHART_TYPES is documented as a literal copy of the agent's list.
    assert tuple(agent.CHART_TYPES) == viz.CHART_TYPES


def test_the_spec_keeps_a_d3_type_instead_of_normalizing_it_away():
    for t in D3_TYPES:
        spec = {"type": t, "x": "a", "y": ["b"]}
        assert viz.normalize_spec(spec)["type"] == t
        assert not any("chart type" in p for p in viz.validate_spec(spec, ["a", "b"]))


def _switched(prompt):
    chart = {"type": "bar", "x": "region", "y": ["revenue"]}
    return agent._canvas_fallback(prompt, ["region", "revenue"], chart)["chart"]["type"]


def test_keyword_edits_switch_to_d3_types_by_their_names():
    assert _switched("show it as a network graph") == "network"
    assert _switched("make it a force graph") == "network"
    assert _switched("try a beeswarm") == "beeswarm"
    assert _switched("calendar heatmap please") == "calendar"
    assert _switched("use circle packing") == "circle_pack"
    assert _switched("packed bubbles instead") == "circle_pack"
    # The older types still win where they did.
    assert _switched("turn this into a heatmap") == "heatmap"
    assert _switched("make it a bubble chart") == "bubble"


def test_a_column_named_like_a_chart_does_not_switch_it():
    assert _switched("revenue by ad network") == "bar"
    assert _switched("break it down by calendar month") == "bar"
