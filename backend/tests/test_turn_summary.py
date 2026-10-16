"""Every answer is this turn's own.

Ask about one table, switch to another in the same chat and ask again: the
reply came back with no summary of its own — the agent's message list holds
the earlier turns as AI messages, so when this turn ended on a tool call
without writing anything, the chat showed the PREVIOUS answer (or "Done.").
Locked in here:
- the answer text is taken only from what this turn wrote;
- a turn that ran a query but wrote nothing gets a short summary of that
  query's result (one extra call), falling back to a plain sentence;
- a turn that wrote its own answer, or never queried, makes no extra call.
"""
import json
import sys
import types

import pytest

from app import agent, db
from app.connectors.demo import DemoConnector, seed

ANA = {"id": "u-turn", "email": "turn@studio.test", "role": "analyst"}
SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1"
EARLIER = [{"role": "user", "text": "orders by status"},
           {"role": "assistant", "text": "Most orders are shipped (412 of 500)."}]


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    seed()
    yield


class _Msg:
    def __init__(self, type_, content="", tool_calls=None):
        self.type, self.content, self.tool_calls = type_, content, tool_calls or []


def _as_message(m):
    if isinstance(m, tuple):
        role, text = m
        return _Msg("human" if role == "user" else "ai", text)
    return _Msg(getattr(m, "type", "ai"), getattr(m, "content", ""))


def test_only_this_turns_text_is_the_answer():
    earlier = [_Msg("human", "orders by status"), _Msg("ai", "Most orders are shipped.")]
    ended_on_a_tool = earlier + [_Msg("human", "revenue by region"),
                                 _Msg("ai", "", tool_calls=[{"name": "run_sql"}]),
                                 _Msg("tool", "{}"), _Msg("ai", "")]
    assert agent._final_text({"messages": ended_on_a_tool}) == ""
    wrote = ended_on_a_tool[:-1] + [_Msg("ai", "EU leads.")]
    assert agent._final_text({"messages": wrote}) == "EU leads."
    blocks = ended_on_a_tool[:-1] + [_Msg("ai", [{"type": "text", "text": "EU leads."}])]
    assert agent._final_text({"messages": blocks}) == "EU leads."


def _stub_langchain_tools(monkeypatch):
    try:
        import langchain_core.tools  # noqa: F401
        return
    except ImportError:
        pass
    pkg = types.ModuleType("langchain_core")
    pkg.__path__ = []
    tools = types.ModuleType("langchain_core.tools")
    tools.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "langchain_core", pkg)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", tools)


def _call(t, **kw):
    return t.invoke(kw) if hasattr(t, "invoke") else t(**kw)


def _name(t):
    return getattr(t, "__name__", "") or getattr(t, "name", "")


class _Summarizer:
    """The model behind the extra summary call."""

    def __init__(self, reply="EU leads with 125, ahead of the US at 90.", fail=False):
        self.reply, self.fail, self.calls = reply, fail, []

    def invoke(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("provider hiccup")
        return types.SimpleNamespace(content=self.reply, usage_metadata={
            "input_tokens": 40, "output_tokens": 12})


def _turn(monkeypatch, *, query=True, final="", llm=None):
    """One run_agent turn after an earlier one, with a scripted graph that
    returns the whole message list — earlier turns included — as LangGraph does."""
    _stub_langchain_tools(monkeypatch)
    llm = llm or _Summarizer()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **kw: llm)
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    monkeypatch.setattr(agent, "_cache_history",
                        lambda history, spec: [("user" if h["role"] == "user" else "assistant",
                                                h["text"]) for h in history])

    def fake_graph(llm_, tools, system, spec, volatile=None):
        class G:
            def invoke(self, state, config=None):
                out = [_as_message(m) for m in state["messages"]]
                if query:
                    by = {_name(t): t for t in tools}
                    _call(by["run_sql"], sql=SQL)
                    out += [_Msg("ai", "", tool_calls=[{"name": "run_sql"}]), _Msg("tool", "{}")]
                out.append(_Msg("ai", final))
                return {"messages": out}
        return G()

    monkeypatch.setattr(agent, "_graph", fake_graph)
    out = agent.run_agent("revenue by region", DemoConnector(), "sales", ["sales"],
                          {"sales": []}, EARLIER, ANA)
    return out, llm


def test_a_silent_turn_gets_a_summary_of_its_own_result(monkeypatch):
    out, llm = _turn(monkeypatch)
    assert out["text"] == "EU leads with 125, ahead of the US at 90."
    assert "Most orders are shipped" not in out["text"]          # not the earlier answer
    (system, user), = [m for m in llm.calls]
    sent = json.loads(user[1])
    assert sent["question"] == "revenue by region" and sent["columns"] == ["region", "revenue"]
    assert sent["row_count"] == len(out["rows"]) and sent["rows"]
    assert out["usage"]["output_tokens"] >= 12                    # the extra call is counted


def test_a_failed_summary_still_answers_for_this_turn(monkeypatch):
    out, _ = _turn(monkeypatch, llm=_Summarizer(fail=True))
    assert out["text"].startswith(f"The query returned {len(out['rows'])} row")
    assert "Most orders are shipped" not in out["text"]


def test_a_turn_that_wrote_its_answer_makes_no_extra_call(monkeypatch):
    out, llm = _turn(monkeypatch, final="Revenue is highest in the EU.")
    assert out["text"] == "Revenue is highest in the EU." and llm.calls == []


def test_a_turn_without_a_query_is_not_summarized(monkeypatch):
    out, llm = _turn(monkeypatch, query=False)
    assert out["text"] == "Done." and llm.calls == []
