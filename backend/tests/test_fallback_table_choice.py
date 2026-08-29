"""Keyless fallback targets the table the prompt names.

Without an LLM key the agent answers with a deterministic preview. In
whole-source mode it used to preview the first table (or every table) with no
regard to the question; now "show sales by region" previews `sales`.

Run from the backend directory:
    python -m pytest tests/test_fallback_table_choice.py -q
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-fallback-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import agent, db
from app.connectors.demo import DemoConnector, seed


@pytest.fixture(scope="module", autouse=True)
def _demo():
    db.init_db()
    seed()
    yield


def test_tables_named_matches_whole_phrases_only():
    tabs = ["ads_performance", "sales", "ecommerce_orders", "web-traffic"]
    assert agent._tables_named("show sales by region", tabs) == ["sales"]
    assert agent._tables_named("ecommerce orders by day", tabs) == ["ecommerce_orders"]
    assert agent._tables_named("web traffic last week", tabs) == ["web-traffic"]
    assert agent._tables_named("in order of revenue", tabs) == []        # no stemming
    assert agent._tables_named("performance review", tabs) == []         # partial ≠ named


def test_whole_source_fallback_previews_the_named_table():
    conn = DemoConnector()
    allowed = conn.list_tables()
    assert "sales" in allowed and allowed[0] != "sales"
    out = agent._fallback("show sales by region", conn, "*", allowed)
    assert out["mode"] == "fallback"
    assert out["sql"].lower().startswith("select * from sales")
    assert "region" in [c.lower() for c in out["columns"]]


def test_whole_source_fallback_without_a_named_table_is_unchanged():
    conn = DemoConnector()
    allowed = conn.list_tables()[:3]
    out = agent._fallback("what happened last week", conn, "*", allowed)
    assert out["mode"] == "fallback" and len(out["panels"]) == len(allowed)  # previews all
