"""Agents do any task, not only data questions.

"What time is it in New York?" came back as "I'm unable to browse the
internet…": a question that matched no source went to the SQL agents, whose
only tools were SQL. Locked in:
- the clock: cities, zones, abbreviations and offsets, daylight saving, and
  an offset's sign as people write it (GMT-3 is UTC-3, not POSIX Etc/GMT-3);
- the web: Anthropic's server tools for Claude models only (the current
  variant on current models), STUDIO_WEB_SEARCH=off removes them; what they
  did becomes activity steps and http(s) citations;
- an answer after a web search is the text after it, citation pieces joined;
- the general assistant runs on LangChain's create_agent with real tool calls:
  it answers, or hands a data question back (return_direct, no second model
  call); no model / a failure / the self-hosted adapter fall back to the data
  agents;
- every data agent has the clock; a standalone chat also the web; the rules
  say "data questions query, anything else is done";
- Auto: a question no source matched goes to the assistant first; its answer
  is saved as a direct answer; a handed-back one goes to every source.

Run from the backend directory:
    python -m pytest tests/test_general_agent.py -q
"""
import datetime as dt

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app import agent, chat, db, general_tools, governance, orchestrator, progress
from app.connectors.demo import DemoConnector

NOW = dt.datetime(2026, 11, 17, 18, 30, 5, tzinfo=dt.timezone.utc)   # a Tuesday, EST in force
ANA = {"id": "u-general", "email": "general@studio.test", "role": "analyst", "name": "G"}


# ── The clock ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("place,starts,where", [
    ("New York", "Tuesday, 17 November 2026, 13:30", "EST, UTC-05:00, America/New_York"),
    ("the time in new york?", "Tuesday, 17 November 2026, 13:30", "America/New_York"),
    ("NYC", "Tuesday, 17 November 2026, 13:30", "America/New_York"),
    ("America/New_York", "Tuesday, 17 November 2026, 13:30", "America/New_York"),
    ("PST", "Tuesday, 17 November 2026, 10:30", "PST, UTC-08:00, America/Los_Angeles"),
    ("Bengaluru", "Wednesday, 18 November 2026, 00:00", "IST, UTC+05:30, Asia/Kolkata"),
    ("London", "Tuesday, 17 November 2026, 18:30", "GMT, UTC+00:00, Europe/London"),
    ("Paris, France", "Tuesday, 17 November 2026, 19:30", "Europe/Paris"),
    ("Sao Paulo", "Tuesday, 17 November 2026, 15:30", "UTC-03:00, America/Sao_Paulo"),
    ("UTC+5:30", "Wednesday, 18 November 2026, 00:00", "UTC+05:30"),
    ("GMT-3", "Tuesday, 17 November 2026, 15:30", "UTC-03:00"),
    ("", "Tuesday, 17 November 2026, 18:30", "UTC"),
])
def test_the_time_anywhere(place, starts, where):
    line = general_tools.describe_time(place, NOW)
    assert line.startswith(starts) and where in line.split("— ", 1)[1].split(". ISO")[0]


def test_daylight_saving_is_the_zones_own():
    summer = dt.datetime(2026, 7, 1, 18, 0, tzinfo=dt.timezone.utc)
    assert "14:00" in general_tools.describe_time("New York", summer)
    assert "EDT, UTC-04:00" in general_tools.describe_time("New York", summer)


@pytest.mark.parametrize("place", ["Atlantis", "utc+99", "Etc/GMT-3", "GMT+5:75"])
def test_what_is_not_a_place_is_said_so(place):
    assert general_tools.resolve(place) == (None, None)
    assert "I don't know a time zone" in general_tools.describe_time(place, NOW)


def test_the_clock_tool_records_its_step():
    seen = []
    tool = general_tools.clock_tool(seen.append)
    out = tool.invoke({"place": "New York"})
    assert "America/New_York" in out and seen == ["checking the time in America/New_York"]
    assert "Never guess the date or time" in tool.description


# ── The web ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec,search,fetch", [
    ("anthropic:claude-sonnet-5", "web_search_20260209", "web_fetch_20260209"),
    ("anthropic:claude-sonnet-5-5", "web_search_20260209", "web_fetch_20260209"),
    ("anthropic:claude-opus-5-5", "web_search_20260209", "web_fetch_20260209"),
    ("anthropic:claude-fable-5-2", "web_search_20260209", "web_fetch_20260209"),
    ("anthropic:claude-haiku-4-5", "web_search_20250305", "web_fetch_20250910"),
])
def test_claude_models_get_the_web(spec, search, fetch, monkeypatch):
    monkeypatch.delenv("STUDIO_WEB_SEARCH", raising=False)
    monkeypatch.delenv("STUDIO_WEB_MAX_USES", raising=False)
    assert general_tools.web_tools(spec) == [
        {"type": search, "name": "web_search", "max_uses": 3},
        {"type": fetch, "name": "web_fetch", "max_uses": 3}]


def test_other_models_and_a_switched_off_server_get_no_web(monkeypatch):
    for spec in ("openai:gpt-5", "openai:bitnet", "", None):
        assert general_tools.web_tools(spec) == []
    for off in ("off", "0", "false", "NO"):
        monkeypatch.setenv("STUDIO_WEB_SEARCH", off)
        assert general_tools.web_tools("anthropic:claude-sonnet-5") == []
    monkeypatch.setenv("STUDIO_WEB_SEARCH", "on")
    monkeypatch.setenv("STUDIO_WEB_MAX_USES", "50")
    assert general_tools.web_tools("anthropic:claude-sonnet-5")[0]["max_uses"] == 10
    monkeypatch.setenv("STUDIO_WEB_MAX_USES", "lots")
    assert general_tools.web_tools("anthropic:claude-sonnet-5")[0]["max_uses"] == 3


SEARCHED = AIMessage(content=[
    {"type": "text", "text": "Let me look that up."},
    {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "NYC weather today"}},
    {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
        {"type": "web_search_result", "url": "https://weather.example/nyc", "title": "NYC weather"},
        {"type": "web_search_result", "url": "javascript:alert(1)", "title": "bad"},
        {"type": "web_search_result", "url": "https://other.example/x", "title": "Other"}]},
    {"type": "server_tool_use", "id": "s2", "name": "web_fetch", "input": {"url": "https://weather.example/nyc"}},
    {"type": "web_fetch_tool_result", "tool_use_id": "s2", "content": {
        "type": "web_fetch_result", "url": "https://weather.example/nyc",
        "content": {"type": "document", "title": "NYC weather"}}},
    {"type": "text", "text": "It is 12°C ", "citations": [
        {"type": "web_search_result_location", "url": "https://weather.example/nyc",
         "title": "NYC weather", "cited_text": "12°C"}]},
    {"type": "text", "text": "and cloudy."},
])


def test_what_the_web_tools_did_becomes_steps_and_citations():
    steps, citations = general_tools.web_activity([("user", "x"), SEARCHED])
    assert steps == ["searched the web for “NYC weather today”", "read https://weather.example/nyc"]
    # The pages the answer cites; javascript: never.
    assert citations == [{"source": "NYC weather", "url": "https://weather.example/nyc", "kind": "web"}]
    uncited = AIMessage(content=SEARCHED.content[:3] + [{"type": "text", "text": "Done."}])
    _, found = general_tools.web_activity([uncited])
    assert [c["url"] for c in found] == ["https://weather.example/nyc", "https://other.example/x"]
    assert general_tools.web_activity([AIMessage(content="plain")]) == ([], [])


def test_an_answer_after_a_search_is_the_text_after_it():
    assert agent._final_text({"messages": [SEARCHED]}) == "It is 12°C and cloudy."
    plain = AIMessage(content=[{"type": "text", "text": "One"}, {"type": "text", "text": " two."}])
    assert agent._final_text({"messages": [plain]}) == "One two."
    ends_on_tool = AIMessage(content=[{"type": "text", "text": "Searching"},
                                      {"type": "server_tool_use", "id": "s", "name": "web_search",
                                       "input": {}}])
    assert agent._final_text({"messages": [ends_on_tool]}) == "Searching"


# ── The general assistant, on LangChain's create_agent ──────────────────

class _Model(GenericFakeChatModel):
    """A scripted chat model: its replies in order, and the tools bound to it."""

    def bind_tools(self, tools, **kwargs):
        _BOUND.append([t.get("type") if isinstance(t, dict) else t.name for t in tools])
        return self


_BOUND = []


@pytest.fixture
def scripted(monkeypatch):
    _BOUND.clear()
    monkeypatch.delenv("STUDIO_WEB_SEARCH", raising=False)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    state = {}

    def make(spec, user=None, **kw):
        state["model"] = _Model(messages=iter(state["replies"]))
        return state["model"]
    monkeypatch.setattr(agent, "make_llm", make)

    def run(replies, spec="anthropic:claude-sonnet-5", **kw):
        state["replies"] = list(replies)
        return agent.run_general("what time is it in New York?", ANA, [], spec,
                                 sources=["demo", "shop"], **kw)
    run.state = state
    return run


@pytest.fixture
def feed(monkeypatch):
    """Steps emitted through progress, by label and kind."""
    seen = []
    monkeypatch.setattr(progress, "emit_for", lambda tid, label, **kw: seen.append((label, kw.get("kind"))))
    monkeypatch.setattr(progress, "emit", lambda label, **kw: seen.append((label, kw.get("kind"))))
    return seen


def test_the_assistant_tells_the_time(scripted, feed):
    out = scripted([
        AIMessage(content="", tool_calls=[{"name": "current_time", "args": {"place": "New York"},
                                           "id": "c1"}]),
        AIMessage(content="It is 1:30 PM in New York."),
    ])
    assert out["text"] == "It is 1:30 PM in New York."
    assert out["mode"] == "general" and out["agents"] == [agent.GENERAL_AGENT]
    assert out["sql"] is None and out["panels"] == [] and out["model"] == "anthropic:claude-sonnet-5"
    assert ("Studio assistant: checking the time in America/New_York", "clock") in feed
    # The model was offered the clock, the hand-off and Claude's web tools.
    assert _BOUND[0] == ["current_time", "use_data_sources", "web_search_20260209", "web_fetch_20260209"]


def test_the_assistant_searches_the_web_and_cites_it(scripted, feed):
    out = scripted([SEARCHED])
    assert out["text"] == "It is 12°C and cloudy."
    assert out["citations"] == [{"source": "NYC weather", "url": "https://weather.example/nyc",
                                 "kind": "web"}]
    assert ("Studio assistant: searched the web for “NYC weather today”", "web") in feed


def test_a_data_question_is_handed_back_without_another_model_call(scripted):
    out = scripted([AIMessage(content="", tool_calls=[{"name": "use_data_sources", "args": {}, "id": "h"}]),
                    AIMessage(content="never read")])
    assert out is None
    assert next(scripted.state["model"].messages).content == "never read"


def test_without_a_search_the_assistant_reads_pages_and_says_so(scripted, monkeypatch):
    seen = {}
    real = agent._graph

    def spy(llm, tools, system, spec, volatile=None):
        seen["system"] = system
        return real(llm, tools, system, spec, volatile)
    monkeypatch.setattr(agent, "_graph", spy)
    scripted([AIMessage(content="ok")], spec="openai:gpt-5")
    # No Bing configured, not Claude: Studio's own page reader, no search.
    assert _BOUND[0] == ["current_time", "use_data_sources", "web_fetch"]
    assert general_tools.FETCH_ONLY_RULE in seen["system"] and "demo, shop" in seen["system"]
    monkeypatch.setenv("STUDIO_WEB_SEARCH", "off")
    scripted([AIMessage(content="ok")])
    assert _BOUND[-1] == ["current_time", "use_data_sources"]
    assert general_tools.NO_WEB_RULE in seen["system"]


def test_no_model_a_failure_or_the_adapter_fall_back_to_the_data_agents(scripted, monkeypatch):
    assert scripted([AIMessage(content="")]) is None                     # wrote nothing
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: 1 / 0)
    assert scripted([]) is None                                          # failed
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: False)
    assert scripted([]) is None                                          # no model
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "self_hosted", lambda spec: True)
    assert scripted([]) is None                                          # SQL-only adapter


# ── Every data agent: the clock; a standalone chat: the web ─────────────

@pytest.fixture
def data_agent(monkeypatch):
    db.init_db()
    governance.init_tables()
    monkeypatch.delenv("STUDIO_WEB_SEARCH", raising=False)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **kw: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    seen = {}

    def fake_graph(llm, tools, system, spec, volatile=None):
        seen["tools"] = [t.get("type") if isinstance(t, dict) else t.name for t in tools]
        seen["system"] = system

        class G:
            def invoke(self, state, config=None):
                return {"messages": [state["messages"][-1], SEARCHED]}
        return G()
    monkeypatch.setattr(agent, "_graph", fake_graph)

    def run(spec="anthropic:claude-sonnet-5", profile="full"):
        out = agent.run_agent("weather in NYC?", DemoConnector(), "*", ["sales"], {"sales": []}, [],
                              ANA, model=spec, tool_profile=profile)
        return out, seen
    return run


def test_a_chat_agent_has_the_clock_and_the_web_and_cites_it(data_agent):
    out, seen = data_agent()
    assert "current_time" in seen["tools"] and "run_sql" in seen["tools"]
    assert seen["tools"][-2:] == ["web_search_20260209", "web_fetch_20260209"]
    assert "For questions about this data, always call run_sql" in seen["system"]
    assert "Not every request is about this database" in seen["system"]
    assert general_tools.WEB_RULE in seen["system"]
    assert out["text"] == "It is 12°C and cloudy."
    assert out["citations"] == [{"source": "NYC weather", "url": "https://weather.example/nyc", "kind": "web"}]


def test_a_graph_worker_keeps_a_closed_tool_set_with_the_clock(data_agent):
    _, seen = data_agent(profile="graph_worker")
    assert "current_time" in seen["tools"] and not any(t.startswith("web_") for t in seen["tools"])
    assert general_tools.NO_WEB_RULE in seen["system"]
    _, seen = data_agent(spec="openai:gpt-5")
    assert "web_fetch" in seen["tools"] and "web_search" not in seen["tools"]
    assert general_tools.FETCH_ONLY_RULE in seen["system"]


# ── Auto: the assistant first when no source matched ───────────────────

def test_who_asks_the_assistant_first():
    assert chat._ask_assistant_first({"auto_route": {"mode": "all", "why": "no source matched"}})
    assert not chat._ask_assistant_first({"auto_route": {"mode": "all", "kept": True}})
    assert not chat._ask_assistant_first({"auto_route": {"mode": "single", "source": "demo"}})
    assert not chat._ask_assistant_first({"auto_route": None})      # "all sources" picked by hand
    assert not chat._ask_assistant_first({})


@pytest.fixture
def turn(monkeypatch, feed):
    db.init_db()
    chat.init_tables()
    monkeypatch.setattr(chat, "_usable_sources", lambda user: ["demo"])
    monkeypatch.setattr(chat.semantic, "certified_answer", lambda *a, **k: None)
    from app import verified_sql
    monkeypatch.setattr(verified_sql, "best_exact", lambda *a, **k: (None, None))
    fanned = []

    def fanout(prompt, user, history, model, conversation_id=None, sources=None):
        fanned.append(prompt)
        return {"text": "Revenue is up.", "sql": "SELECT 1", "columns": [], "rows": [], "panels": [],
                "mode": "orchestrated", "agents": []}
    monkeypatch.setattr(orchestrator, "run_orchestrated", fanout)
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user, **kw: [])
    monkeypatch.setattr(orchestrator, "ambiguous_tables", lambda prompt, sources: [])

    def run(general, route=None):
        monkeypatch.setattr(agent, "run_general", lambda *a, **k: general)
        cid = db.create_conversation(ANA["id"], "t")
        ctx = {"mode": "*", "cid": cid, "model": None, "prompt": "what time is it in New York?",
               "history": [], "only": None, "allow_ambiguous": False,
               "auto_route": route if route is not None else {"mode": "all", "why": "no match"}}
        chat._run_turn(ctx, ANA)
        return db.list_messages(cid)[-1]["content"], fanned
    return run


def test_a_direct_answer_is_saved_as_one(turn):
    general = {"text": "It is 1:30 PM in New York.", "sql": None, "columns": [], "rows": [],
               "panels": [], "citations": [], "mode": "general", "agents": [agent.GENERAL_AGENT],
               "model": "anthropic:claude-sonnet-5"}
    saved, fanned = turn(general)
    assert fanned == []                                   # no source was asked
    assert saved["text"] == "It is 1:30 PM in New York." and saved["mode"] == "general"
    assert saved["table"] == "general" and saved["source"] == "*"
    assert saved["auto_route"]["mode"] == "general"
    assert "answered directly" in saved["auto_route"]["why"]


def test_a_handed_back_question_asks_every_source(turn, feed):
    saved, fanned = turn(None)
    assert fanned == ["what time is it in New York?"]
    assert saved["text"] == "Revenue is up." and saved["table"] == "all sources"
    assert ("this is about your data — asking every source", "route") in feed


def test_a_hand_picked_all_sources_question_skips_the_assistant(turn, monkeypatch):
    monkeypatch.setattr(agent, "run_general", lambda *a, **k: pytest.fail("assistant asked"))
    cid = db.create_conversation(ANA["id"], "t")
    chat._run_turn({"mode": "*", "cid": cid, "model": None, "prompt": "revenue?", "history": [],
                    "only": None, "allow_ambiguous": False, "auto_route": None}, ANA)
    assert db.list_messages(cid)[-1]["content"]["text"] == "Revenue is up."
