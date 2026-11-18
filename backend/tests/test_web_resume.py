"""Why "my agent is not performing web search", fixed where the code can fix it.

1. Anthropic runs web search in a server-side loop; when that loop hits its
   limit the reply comes back with stop_reason "pause_turn" and must be sent
   back, unchanged, to carry on. LangChain's loop ended the turn there, so
   the person got "Done." or a half sentence. Now the paused reply is resumed
   (STUDIO_WEB_PAUSE_RESUMES times at most) — with no new user message.
2. Claude got LangChain's default 4,096-token output cap, shared with
   adaptive thinking and the search: answers were cut off or empty. Claude's
   cap is now STUDIO_MAX_OUTPUT_TOKENS (16,000).
3. A failed search (a tool turned off for the Anthropic organization, too
   many requests…) and an answer cut short are now said in the activity
   feed and logged — not left looking like the agent never searched.

Run from the backend directory:
    python -m pytest tests/test_web_resume.py -q
"""
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app import agent, general_tools, progress
from app.connectors.demo import DemoConnector

ANA = {"id": "u-resume", "email": "resume@studio.test", "role": "analyst", "name": "R"}

PAUSED = AIMessage(content=[
    {"type": "text", "text": "Let me search."},
    {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "NYC weather today"}},
], response_metadata={"stop_reason": "pause_turn"})
DONE = AIMessage(content=[
    {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
        {"type": "web_search_result", "url": "https://weather.example/nyc", "title": "NYC weather"}]},
    {"type": "text", "text": "It is 12°C and cloudy in New York.", "citations": [
        {"type": "web_search_result_location", "url": "https://weather.example/nyc", "title": "NYC weather"}]},
], response_metadata={"stop_reason": "end_turn"})


class _Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        _SEEN.append([m.type for m in messages])
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


_SEEN = []


@pytest.fixture
def scripted(monkeypatch):
    _SEEN.clear()
    for k in ("STUDIO_WEB_SEARCH", "FOUNDRY_PROJECT_ENDPOINT", "GEMINI_API_KEY", "BRAVE_SEARCH_API_KEY",
              "STUDIO_WEB_PAUSE_RESUMES"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    state = {}

    def run(*replies):
        state["replies"] = iter(replies)
        monkeypatch.setattr(agent, "make_llm", lambda *a, **k: _Model(messages=state["replies"]))
        return agent.run_general("weather in NYC today?", ANA, [], "anthropic:claude-sonnet-5", sources=["demo"])
    run.state = state
    return run


@pytest.fixture
def feed(monkeypatch):
    seen = []
    monkeypatch.setattr(progress, "emit_for", lambda tid, label, **kw: seen.append((label, kw.get("kind"))))
    return seen


# ── 1. A paused search is resumed ───────────────────────────────────────

def test_a_paused_search_is_resumed_and_answers(scripted, feed):
    out = scripted(PAUSED, DONE)
    assert out["text"] == "It is 12°C and cloudy in New York."
    assert out["citations"] == [{"source": "NYC weather", "url": "https://weather.example/nyc", "kind": "web"}]
    # Resumed with the paused reply sent back as it was — no new user message.
    assert _SEEN == [["system", "human"], ["system", "human", "ai"]]
    assert ("Studio assistant: searched the web for “NYC weather today”", "web") in feed
    assert not any(kind == "error" for _, kind in feed)


def test_resumes_are_bounded_and_a_still_paused_turn_is_said(scripted, feed, monkeypatch):
    monkeypatch.setenv("STUDIO_WEB_PAUSE_RESUMES", "1")
    paused2 = AIMessage(content=[{"type": "text", "text": "Still searching."}],
                        response_metadata={"stop_reason": "pause_turn"})
    out = scripted(PAUSED, paused2, DONE)
    assert len(_SEEN) == 2 and out["text"] == "Still searching."
    assert next(scripted.state["replies"]).content == DONE.content          # never asked for
    assert ("Studio assistant: the answer was cut short (pause_turn)", "error") in feed
    monkeypatch.setenv("STUDIO_WEB_PAUSE_RESUMES", "0")
    scripted(PAUSED, DONE)
    assert len(_SEEN) == 3                                                   # no resume at all


def test_a_data_agent_resumes_too(monkeypatch, feed):
    from app import db
    db.init_db()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    calls = []

    def fake_graph(llm, tools, system, spec, volatile=None):
        class G:
            def invoke(self, state, config=None):
                calls.append([getattr(m, "type", m[0] if isinstance(m, tuple) else "?")
                              for m in state["messages"]])
                head = list(state["messages"])
                return {"messages": head + [PAUSED if len(calls) == 1 else DONE]}
        return G()
    monkeypatch.setattr(agent, "_graph", fake_graph)
    out = agent.run_agent("weather?", DemoConnector(), "*", ["sales"], {"sales": []}, [], ANA,
                          model="anthropic:claude-sonnet-5")
    assert len(calls) == 2 and calls[1][-1] == "ai"
    assert out["text"] == "It is 12°C and cloudy in New York."


# ── 2. Room to answer ───────────────────────────────────────────────────

def test_claude_gets_room_to_think_search_and_answer(monkeypatch):
    seen = {}
    import langchain.chat_models as cm
    monkeypatch.setattr(cm, "init_chat_model", lambda spec, **kw: seen.setdefault(spec, kw))
    monkeypatch.delenv("STUDIO_MAX_OUTPUT_TOKENS", raising=False)
    agent.make_llm("anthropic:claude-sonnet-5")
    assert seen["anthropic:claude-sonnet-5"]["max_tokens"] == 16000
    seen.clear()
    monkeypatch.setenv("STUDIO_MAX_OUTPUT_TOKENS", "32000")
    agent.make_llm("anthropic:claude-sonnet-5")
    assert seen["anthropic:claude-sonnet-5"]["max_tokens"] == 32000
    seen.clear()
    agent.make_llm("anthropic:claude-sonnet-5", max_tokens=2000)             # a caller's own wins
    assert seen["anthropic:claude-sonnet-5"]["max_tokens"] == 2000
    seen.clear()
    agent.make_llm("openai:gpt-5")
    assert "max_tokens" not in seen["openai:gpt-5"]


# ── 3. Trouble is said ──────────────────────────────────────────────────

def test_a_failed_search_is_said_with_what_to_check(scripted, feed):
    failed = AIMessage(content=[
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "q"}},
        {"type": "web_search_tool_result", "tool_use_id": "s1",
         "content": {"type": "web_search_tool_result_error", "error_code": "unavailable"}},
        {"type": "text", "text": "I couldn't search the web."}], response_metadata={"stop_reason": "end_turn"})
    out = scripted(failed)
    assert out["text"] == "I couldn't search the web."
    errors = [label for label, kind in feed if kind == "error"]
    assert errors == ["Studio assistant: web search failed (unavailable) — check that web search is "
                      "turned on for your Anthropic organization (Console → Settings → Capabilities)"]
    assert general_tools.web_errors([failed]) == ["unavailable"]
    rate = AIMessage(content=[{"type": "web_search_tool_result", "tool_use_id": "x",
                               "content": {"error_code": "too_many_requests"}},
                              {"type": "web_fetch_tool_result", "tool_use_id": "y",
                               "content": {"type": "web_fetch_tool_error", "error_code": "url_not_accessible"}},
                              {"type": "web_search_tool_result", "tool_use_id": "z",
                               "content": {"error_code": "too_many_requests"}}])
    assert general_tools.web_errors([rate]) == ["too_many_requests", "url_not_accessible"]


def test_an_answer_cut_off_by_the_cap_is_said(scripted, feed):
    cut = AIMessage(content="It is 12°C and", response_metadata={"stop_reason": "max_tokens"})
    scripted(cut)
    assert ("Studio assistant: the answer was cut short (max_tokens)", "error") in feed
