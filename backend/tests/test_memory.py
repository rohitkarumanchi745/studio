"""Agent memory: notes dedup, rank and stay private; recall re-checks access."""
import itertools
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, auth, db, embed, governance, memory
from app.connectors import demo, get_connector


USER = {"id": "mem-owner", "email": "owner@studio.test", "role": "viewer"}
OTHER = {"id": "mem-other", "email": "other@studio.test", "role": "viewer"}
SALES_SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    monkeypatch.delenv("HARRIER_EMBED_URL", raising=False)
    # A strictly increasing clock: ordering assertions must not depend on two
    # writes landing in different wall-clock ticks.
    clock = itertools.count(1_700_000_000)
    monkeypatch.setattr(memory, "time", types.SimpleNamespace(
        time=lambda: float(next(clock)), strftime=time.strftime, gmtime=time.gmtime))
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)
    db.init_db()
    governance.init_tables()
    demo.seed()
    yield
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)


def notes(user=USER):
    return [n["note"] for n in memory.list_notes(user["id"])]


def remember(*texts, user=USER):
    return [memory.add_note(user["id"], t) for t in texts]


# ── Notes: write ─────────────────────────────────────────────────────────

def test_remembering_the_same_fact_refreshes_it_with_the_newer_wording():
    first, again = remember("Prefers revenue in bar charts.", "  prefers revenue in BAR charts ")
    assert first["status"] == "saved" and again["status"] == "refreshed"
    assert again["id"] == first["id"]
    assert notes() == ["prefers revenue in BAR charts"]


def test_a_changed_preference_is_a_separate_note_not_a_merge():
    remember("prefers revenue in bar charts", "prefers revenue in line charts")
    assert notes() == ["prefers revenue in line charts", "prefers revenue in bar charts"]


def test_a_refreshed_note_moves_back_to_the_front():
    remember("cares about the West region", "wants amounts in EUR", "cares about the west region")
    assert notes() == ["cares about the west region", "wants amounts in EUR"]


def test_embeddings_merge_paraphrases_lexical_matching_would_miss(monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed", lambda text, kind="query":
                        [1.0, 0.0] if "west" in text.lower() else [0.0, 1.0])
    _, again = remember("cares about the West region", "focuses on western territories")
    assert again["status"] == "refreshed"
    assert notes() == ["focuses on western territories"]


def test_empty_notes_are_refused():
    with pytest.raises(ValueError):
        memory.add_note(USER["id"], "   ")


def test_storage_keeps_only_the_most_recent_notes(monkeypatch):
    monkeypatch.setattr(memory, "MAX_NOTES", 3)
    remember("likes pie charts", "works in finance", "wants amounts in EUR",
             "cares about the west region", "reports go to the CFO")
    assert notes() == ["reports go to the CFO", "cares about the west region",
                       "wants amounts in EUR"]


# ── Notes: read ──────────────────────────────────────────────────────────

def test_under_the_cap_every_note_reaches_the_prompt_newest_first():
    remember("likes pie charts", "works in finance")
    assert memory.notes_for_prompt(USER["id"], "anything", limit=5) == [
        "works in finance", "likes pie charts"]


def test_past_the_cap_a_relevant_old_note_beats_recent_irrelevant_ones():
    remember("cares about the west region", "likes pie charts", "works in finance",
             "wants amounts in EUR")
    picked = memory.notes_for_prompt(USER["id"], "revenue in the west region by month", limit=2)
    # The relevant note survives; the other slot goes to the newest note, and
    # the prompt still reads newest first.
    assert picked == ["wants amounts in EUR", "cares about the west region"]


# ── Notes: forget ────────────────────────────────────────────────────────

def test_forget_removes_only_the_closest_note():
    remember("prefers bar charts", "cares about the west region")
    assert memory.forget_matching(USER["id"], "bar charts preference") == "prefers bar charts"
    assert notes() == ["cares about the west region"]


def test_forget_without_a_close_match_removes_nothing():
    remember("prefers bar charts", "cares about the west region")
    assert memory.forget_matching(USER["id"], "quarterly board deck") is None
    assert memory.forget_matching(USER["id"], "") is None
    assert len(notes()) == 2


# ── Notes: ownership ─────────────────────────────────────────────────────

def test_notes_belong_to_their_owner():
    (mine,) = remember("prefers bar charts")
    assert notes(OTHER) == []
    assert memory.delete_note(OTHER["id"], mine["id"]) is False
    assert memory.clear_notes(OTHER["id"]) == 0
    assert memory.forget_matching(OTHER["id"], "prefers bar charts") is None
    assert memory.notes_for_prompt(OTHER["id"], "bar charts") == []
    assert notes() == ["prefers bar charts"]


def test_api_lists_and_deletes_only_the_callers_notes():
    app = FastAPI()
    app.include_router(memory.router, prefix="/api")
    app.dependency_overrides[auth.current_user] = lambda: USER
    (mine,) = remember("prefers bar charts")
    remember("works in finance")
    (theirs,) = remember("likes pie charts", user=OTHER)
    client = TestClient(app)

    listed = client.get("/api/memory").json()["notes"]
    assert [n["note"] for n in listed] == ["works in finance", "prefers bar charts"]
    assert set(listed[0]) == {"id", "note", "created_at", "updated_at"}

    assert client.delete(f"/api/memory/{theirs['id']}").status_code == 404
    assert client.delete(f"/api/memory/{mine['id']}").json() == {"deleted": 1}
    assert client.delete("/api/memory").json() == {"deleted": 1}
    assert notes() == [] and notes(OTHER) == ["likes pie charts"]


# ── Past runs ────────────────────────────────────────────────────────────

def trace(prompt, sql=SALES_SQL, *, user=USER, cid="c-old", **kw):
    return db.add_trace(user, conversation_id=cid, prompt=prompt, mode="agent",
                        source=kw.pop("source", "demo"), table="*", sql=sql, **kw)


def recall(query, user=USER, **kw):
    return memory.recall_runs(user, query, source=kw.pop("source", "demo"), **kw)


def test_recall_returns_the_question_and_sql_never_rows_or_answers():
    trace("revenue by region last quarter")
    trace("top pages by visits", "SELECT * FROM web_traffic LIMIT 5")
    (run,) = recall("revenue by region")
    assert run == {"asked_on": run["asked_on"], "question": "revenue by region last quarter",
                   "sql": SALES_SQL, "outcome": "ran successfully"}
    assert len(run["asked_on"]) == 10   # YYYY-MM-DD


def test_recall_skips_other_users_and_the_current_conversation():
    trace("revenue by region", user=OTHER)
    trace("revenue by region this month", cid="c-now")
    assert recall("revenue by region", exclude_conversation="c-now") == []
    assert len(recall("revenue by region")) == 1


def test_recall_rechecks_access_as_of_today():
    trace("revenue by region")
    governance._set("version: 1\nroles:\n  viewer:\n    sources:\n"
                    "      demo: [web_traffic]\n", "test")
    assert recall("revenue by region") == []


def test_recall_respects_the_current_table_scope():
    trace("revenue by region")
    assert recall("revenue by region", table="sales")
    assert recall("revenue by region", table="web_traffic") == []


def test_recall_stays_on_the_requested_source():
    trace("revenue by region")
    assert recall("revenue by region", source="snowflake") == []


def test_explicit_feedback_is_reported_as_the_outcome():
    trace("revenue by region", reward=0.0, reward_source="user")
    assert recall("revenue by region")[0]["outcome"] == "user marked it wrong"


def test_one_word_queries_match_but_longer_ones_need_two_shared_terms():
    trace("churn by month", "SELECT COUNT(*) AS n FROM sales")
    assert len(recall("churn")) == 1
    assert recall("churn forecast accuracy") == []


def test_repeated_runs_of_the_same_question_are_recalled_once():
    trace("revenue by region")
    trace("Revenue by region.")
    trace("revenue by region by quarter")
    # Exact match first, then the partial one; the duplicate pair appears once.
    assert [r["question"].casefold().rstrip(".") for r in recall("revenue by region")] == [
        "revenue by region", "revenue by region by quarter"]


# ── Wiring into the agent ────────────────────────────────────────────────

def test_agent_turn_uses_ranked_notes_and_offers_memory_tools(monkeypatch):
    pytest.importorskip("langchain_core")
    remember("prefers bar charts")
    trace("revenue by region", cid="c-old")
    trace("revenue by region today", cid="c-now")
    seen = {}

    def fake_graph(llm, tools, system, spec, volatile=None):
        by_name = {t.name: t for t in tools}
        seen["tools"] = set(by_name)
        seen["volatile"] = volatile
        seen["remember"] = by_name["remember"].invoke({"note": "Prefers bar charts"})
        seen["forget"] = by_name["forget"].invoke({"note": "pie charts"})
        seen["recall"] = by_name["recall_past_work"].invoke({"query": "revenue by region"})

        class Graph:
            def invoke(self, *_a, **_kw):
                return {"messages": [types.SimpleNamespace(type="ai", content="done")]}
        return Graph()

    monkeypatch.setattr(agent, "llm_available", lambda *a, **kw: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **kw: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda *a, **kw: [])
    monkeypatch.setattr(agent, "_graph", fake_graph)
    out = agent.run_agent("revenue by region", get_connector("demo"), "*", ["sales"],
                          {"sales": []}, [], {**USER, "name": "Owner"},
                          model="anthropic:test", conversation_id="c-now")

    assert out["text"] == "done"
    assert {"remember", "forget", "recall_past_work"} <= seen["tools"]
    assert "- prefers bar charts" in seen["volatile"]
    assert seen["remember"] == "Already noted — refreshed."
    assert seen["forget"] == "No saved note matches that."
    assert '"question": "revenue by region"' in seen["recall"]
    assert "today" not in seen["recall"]   # the current conversation is excluded
    assert notes() == ["Prefers bar charts"]
