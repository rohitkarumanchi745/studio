"""Automatic semantic discovery: descriptions, and search by meaning.

Locked in: table and column descriptions (catalog COMMENTs) are read, stored,
shown, searched, and part of the fingerprint — a changed description is a
change, while an object with none keeps the fingerprint it always had; a
question is grounded on description words (weaker than names); with Harrier
configured, objects are indexed as they are discovered and a question is also
matched by meaning, only against vectors of the object as it is now, a
meaning-only match clearing a higher bar and labelled so; change capture
sees a description change.
"""
import hashlib
import json
import math
import re
import sqlite3

import pytest

from test_metagraph_discovery import ADMIN, RetailConnector, env, stored  # noqa: F401

DESCRIPTIONS = {
    "retail_orders": ("One row per order as the customer placed it",
                      {"status": "placed, shipped, returned or cancelled"}),
    "retail_order_items": ("Order lines: net turnover in EUR after discounts",
                           {"net_sales": "net turnover per line, EUR"}),
}


class DescribedRetail(RetailConnector):
    """The retail stand-in with catalog descriptions, as Postgres COMMENTs and
    Databricks comments provide them."""

    def __init__(self, path):
        super().__init__(path)
        self.notes = {k: (v[0], dict(v[1])) for k, v in DESCRIPTIONS.items()}

    def describe_relations(self):
        out = super().describe_relations()
        for name, (table_note, col_notes) in self.notes.items():
            if name in out:
                if table_note:
                    out[name]["description"] = table_note
                for col in out[name]["columns"]:
                    if col["name"] in col_notes:
                        col["description"] = col_notes[col["name"]]
        return out


@pytest.fixture()
def described(env, tmp_path):
    from app import connectors
    from app.connectors import demo
    conn = DescribedRetail(env.warehouse)
    env.mp.setitem(connectors._REGISTRY, "retail", conn)
    env.mp.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    env.mp.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "0")
    env.retail = conn
    env.mg.discover("retail", "test")
    return env


def _object(env, name):
    return next(r for r in env.mg.get_source("retail", user=ADMIN)["relations"]
                if r["name"] == name)


def test_descriptions_are_read_stored_and_shown(described):
    orders = _object(described, "retail_orders")
    assert orders["description"] == "One row per order as the customer placed it"
    status = next(c for c in orders["columns"] if c["name"] == "status")
    assert status["description"] == "placed, shipped, returned or cancelled"
    assert "description" not in _object(described, "retail_stores")      # none: no key
    graph = described.mg.source_graph("retail", user=ADMIN)
    node = next(n for n in graph["nodes"] if n["id"] == "object:retail:retail_orders")
    assert node["description"].startswith("One row per order")


def test_search_finds_a_table_by_what_its_description_says(described):
    hits = described.mg.search("turnover", user=ADMIN)["results"]
    assert {(h["type"], h["object"], h.get("column")) for h in hits} >= {
        ("table", "retail_order_items", None), ("column", "retail_order_items", "net_sales")}


def test_an_object_without_descriptions_keeps_its_fingerprint(described):
    cols = [{"name": "store_id", "type": "INTEGER"}, {"name": "store_name", "type": "TEXT"}]
    old = hashlib.sha256(("table\n" + "\n".join(f"{c['name']}:{c['type']}" for c in cols))
                         .encode()).hexdigest()
    assert described.mg._fingerprint("table", cols) == old
    assert described.mg._fingerprint("table", cols, "Store master") != old


def test_a_changed_description_is_a_change(described):
    described.retail.notes["retail_orders"] = ("One row per order, net of returns", {})
    out = described.mg.discover("retail", "test")
    assert out["changed"] == 1
    orders = _object(described, "retail_orders")
    assert orders["description"] == "One row per order, net of returns"
    assert all("description" not in c for c in orders["columns"])


def test_a_question_is_grounded_on_description_words(described):
    from app import metaground
    ranked = metaground.score("total turnover",
                              metaground._objects("retail", stored(described)))
    assert [(t, w) for t, _s, w in ranked] == [("retail_order_items", ["turnover"])]
    scores = {t: s for t, s, _w in metaground.score(
        "store turnover", metaground._objects("retail", stored(described)))}
    assert scores["retail_stores"] > scores["retail_order_items"]       # a name beats a note


def test_change_capture_sees_a_description_change(described):
    from app import metacdc

    def events():
        with described.db.connect() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM meta_events WHERE source='retail'").fetchall()]
    described.mg.tick_once()
    assert events() == []                         # described tables, nothing changed: quiet
    described.retail.notes["retail_orders"] = ("Orders, one row each", {})
    described.mg.tick_once()
    assert [e["asset"] for e in events()] == ["retail_orders"]
    with described.db.connect() as c:
        rows = c.execute("SELECT * FROM background_jobs WHERE kind='metagraph_event'").fetchall()
    for row in rows:
        described.jobs.handlers()["metagraph_event"](json.loads(row["payload"]),
                                                     {**dict(row), "attempts": 1})
    assert _object(described, "retail_orders")["description"] == "Orders, one row each"
    assert metacdc.status("retail")["capture"] == "bulk"


# ── By meaning, with Harrier ─────────────────────────────────────────────

CONCEPTS = [{"customer", "client", "buyer", "shopper"}, {"revenue", "turnover", "sale", "sales",
            "income"}, {"store", "shop", "branch", "outlet"}, {"product", "item", "sku"},
            {"order", "purchase"}, {"return", "refund"}, {"inventory", "stock", "hand"}]


def fake_embed(text, kind="query", instruct=None):
    """A stand-in for Harrier: concepts, not spellings — "client" and
    "customer" land on the same axis."""
    words = re.findall(r"[a-z]+", text.lower())
    vec = [sum(1 for w in words if w in group or w.rstrip("s") in group) for group in CONCEPTS]
    vec.append(0.3)                          # every text shares a little
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def harrier(described, monkeypatch):
    from app import embed
    calls = []
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed",
                        lambda text, kind="query", instruct=None:
                        calls.append(kind) or fake_embed(text, kind, instruct))
    described.embed_calls = calls
    return described


def test_objects_are_indexed_and_reindexed_only_when_they_change(harrier):
    from app import metaground
    assert metaground.index("retail") == 8
    assert metaground.index("retail") == 0                       # nothing changed
    harrier.retail.notes["retail_stores"] = ("Physical shops", {})
    harrier.mg.discover("retail", "test")                         # discovery re-indexes it
    with harrier.db.connect() as c:
        fps = {r["object"]: r["fingerprint"] for r in c.execute(
            "SELECT object, fingerprint FROM meta_embeddings").fetchall()}
    assert fps["retail_stores"] == stored(harrier)["retail_stores"]["fingerprint"]


def test_a_question_is_matched_by_meaning(harrier):
    from app import metaground
    metaground.index("retail")
    allowed = list(stored(harrier))
    out = metaground.ground("retail", "how much do clients spend", allowed)
    assert out and "retail_customers" in out["tables"]
    assert metaground.SEMANTIC in out["matched"]["retail_customers"]   # labelled as such
    lexical = metaground.score("how much do clients spend",
                               metaground._objects("retail", allowed))
    assert "retail_customers" not in {t for t, _s, _w in lexical}       # words alone: no


def test_a_vector_of_an_older_version_is_never_trusted(harrier):
    from app import metaground
    metaground.index("retail")
    with harrier.db.connect() as c:
        c.execute("UPDATE meta_embeddings SET fingerprint='old' WHERE object='retail_customers'")
        c.commit()
    out = metaground.ground("retail", "how much do clients spend", list(stored(harrier)))
    assert not out or "retail_customers" not in out["tables"]


def test_indexing_is_bounded_and_stops_when_harrier_is_down(harrier, monkeypatch):
    from app import embed, metaground
    assert metaground.index("retail", budget=3) == 3
    monkeypatch.setattr(embed, "embed", lambda *a, **k: None)
    assert metaground.index("retail") == 0                        # down: nothing, no error
    assert metaground.ground("retail", "store sales", list(stored(harrier)))   # words still work


def test_without_harrier_nothing_is_indexed_or_asked(described, monkeypatch):
    from app import embed, metaground
    monkeypatch.setattr(embed, "embed", lambda *a, **k: pytest.fail("Harrier was called"))
    assert metaground.index("retail") == 0
    assert metaground.ground("retail", "store sales", list(stored(described)))
