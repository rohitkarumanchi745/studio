"""The D3 & artifacts window — the chat, where every answer is drawn with D3.

Locked in here:
- the window draws every chart type with D3: d3_only keeps every type (an
  unknown one becomes a bar), puts every panel on the D3 engine and leaves
  custom visuals alone; the backend's list is the frontend's
  (renderers/D3View.jsx);
- in visual mode render_chart records D3 panels of any type, and the agent is
  told the window is D3-only — naming render_visual only when that tool is
  offered;
- a visual turn goes straight to the agent in visual mode (no semantic layer —
  save a certified metric, test_certified_everywhere.py — plan cache or BitNet
  routing, and nothing stored in the plan cache), its
  answer is stored D3-only, and its conversation's title carries the 🎨;
- the multi-agent path is coerced the same way;
- there, commands that would build or submit work are read as questions, and
  a visual turn cannot also carry a pipeline or platform action.
"""
import os
import pathlib
import re
import sys
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="studio-visual-chat-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException

from app import agent, chat, db, governance, orchestrator, qcache, semantic, visuals
from app import router as model_router
from app.connectors.demo import DemoConnector, seed

ANA = {"id": "u-vc-ana", "email": "vc-ana@studio.test", "role": "analyst", "name": "Ana"}
SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1"


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    governance.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac():
    governance._STATE.update(doc=None, yaml="", source=None)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


# ── d3_only ─────────────────────────────────────────────────────────────

def test_d3_only_keeps_every_type_and_sets_the_d3_engine():
    visual = {"title": "Rings", "code": "d3.select(el)"}
    result = {
        "chart": {"type": "sankey", "x": "from", "y": ["to", "n"], "title": "Flows"},
        "panels": [
            {"chart": {"type": "treemap"}, "rows": [["EU", 1]]},
            {"chart": {"type": "line"}, "engine": "plotly"},
            {"chart": {"type": "table", "title": "Rings"}, "visual": visual},
            {"chart": {"type": "made-up"}},
            {"chart": None},
        ],
    }
    out = agent.d3_only(result)
    assert out is result and out["visual_mode"] is True
    # D3 draws the whole catalog: a sankey stays a sankey, a line a line.
    assert out["chart"] == {"type": "sankey", "x": "from", "y": ["to", "n"], "title": "Flows"}
    assert [(p["chart"] or {}).get("type") for p in out["panels"]] == \
        ["treemap", "line", "table", "bar", None]
    assert [p["engine"] for p in out["panels"]] == ["d3"] * 5
    assert out["panels"][0]["rows"] == [["EU", 1]]
    assert out["panels"][2]["visual"] is visual
    # Every type the agent can name lands on one the window draws.
    for t in agent.CHART_TYPES:
        assert agent.d3_only({"chart": {"type": t}})["chart"]["type"] == t
    assert set(agent.D3_CHART_TYPES) == set(agent.CHART_TYPES)


def test_the_d3_types_are_the_frontends():
    d3view = (pathlib.Path(__file__).resolve().parents[2]
              / "frontend" / "src" / "components" / "renderers" / "D3View.jsx")
    if not d3view.exists():
        pytest.skip("frontend not checked out")
    source = d3view.read_text()
    sets = re.findall(r"export const D3_(?:ONLY|TYPES) = new Set\(\[(.*?)\]\)", source, re.S)
    assert len(sets) == 2
    assert set(re.findall(r'"([a-z_]+)"', "".join(sets))) == set(agent.D3_CHART_TYPES)
    assert set(agent.D3_CHART_TYPES) <= set(agent.CHART_TYPES)


# ── run_agent in visual mode ────────────────────────────────────────────

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


class _Msg:
    type = "ai"

    def __init__(self, text):
        self.content = text


def _name(t):
    return getattr(t, "__name__", "") or getattr(t, "name", "")


def _call(t, **kw):
    return t.invoke(kw) if hasattr(t, "invoke") else t(**kw)


def _window_note(system):
    """The D3 & artifacts note in a system prompt (the shared prompt above it
    mentions render_visual conditionally, for every chat)."""
    assert "D3 & ARTIFACTS WINDOW" in system
    return system.split("D3 & ARTIFACTS WINDOW", 1)[1]


def _run_agent(monkeypatch, chart_types, visual_mode=True, visual_request=None):
    """run_agent with a scripted graph: one query, then render_chart for each
    type in turn, then render_visual when a request is given. Returns the
    result and what the graph (and the visual writer) saw."""
    _stub_langchain_tools(monkeypatch)
    seen = {"replies": [], "generate": []}

    def fake_generate(llm, columns, rows, request, title="", **kw):
        seen["generate"].append(kw)
        return {"title": title, "code": "d3.select(el)", "explanation": ""}

    monkeypatch.setattr(visuals, "generate", fake_generate)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **kw: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})

    def fake_graph(llm, tools, system, spec, volatile=None):
        seen["system"] = system
        seen["tools"] = [_name(t) for t in tools]

        class G:
            def invoke(self, state, config=None):
                by = {_name(t): t for t in tools}
                _call(by["run_sql"], sql=SQL)
                for t in chart_types:
                    seen["replies"].append(_call(by["render_chart"], chart_type=t,
                                                 title="Revenue", x="region", y=["revenue"]))
                if visual_request:
                    _call(by["render_visual"], title="Revenue", request=visual_request)
                return {"messages": [_Msg("done")]}
        return G()

    monkeypatch.setattr(agent, "_graph", fake_graph)
    out = agent.run_agent("revenue by region", DemoConnector(), "sales", ["sales"],
                          {"sales": []}, [], ANA, visual_mode=visual_mode)
    return out, seen


def test_in_visual_mode_every_type_is_recorded_as_a_d3_panel(monkeypatch):
    out, seen = _run_agent(monkeypatch, ["line", "sankey", "boxplot"],
                           visual_request="a radial view")
    assert [r.split(" recorded")[0] for r in seen["replies"]] == \
        ["Chart panel 1", "Chart panel 2", "Chart panel 3"]
    assert [(p["chart"]["type"], p["engine"]) for p in out["panels"][:3]] == \
        [("line", "d3"), ("sankey", "d3"), ("boxplot", "d3")]
    note = _window_note(seen["system"])
    assert "render_visual" in note and "render_visual" in seen["tools"]
    assert "draws them all" in note and "line chart" not in note
    assert seen["generate"] == [{}]                           # no extra rules for visuals


def test_outside_visual_mode_nothing_changes(monkeypatch):
    out, seen = _run_agent(monkeypatch, ["sankey", "line"], visual_mode=False,
                           visual_request="a radial view")
    assert [r.split(" recorded")[0] for r in seen["replies"]] == ["Chart panel 1", "Chart panel 2"]
    assert "engine" not in out["panels"][0]
    assert "D3 & ARTIFACTS WINDOW" not in seen["system"]
    assert seen["generate"] == [{}]


@pytest.mark.parametrize("proposed,drawn", [("sankey", "sankey"), ("line", "line")])
def test_a_self_hosted_model_is_never_told_of_render_visual(monkeypatch, proposed, drawn):
    """BitNet answers in one JSON action, with no retry: whatever chart it
    proposes is drawn with D3."""
    _stub_langchain_tools(monkeypatch)
    prompts = []

    class _BitNet:
        def invoke(self, messages):
            prompts.append(messages[0][1])
            return _Msg('[{"tool": "run_sql", "sql": "%s"}, '
                        '{"tool": "render_chart", "chart_type": "%s"}]' % (SQL, proposed))

    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **kw: _BitNet())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    monkeypatch.setattr(agent, "self_hosted", lambda spec: True)
    monkeypatch.setattr(agent, "_auto_chart", lambda columns, rows, table: {
        "type": "bar", "title": "Revenue", "x": "region", "y": ["revenue"]})
    out = agent.run_agent("revenue by region", DemoConnector(), "sales", ["sales"],
                          {"sales": []}, [], ANA, visual_mode=True)
    assert "render_visual" not in _window_note(prompts[0])
    assert [(p["chart"]["type"], p["engine"]) for p in out["panels"]] == [(drawn, "d3")]


# ── a visual turn, through the chat endpoint ────────────────────────────

ANSWER = {
    "text": "EU leads.", "sql": SQL, "columns": ["region", "revenue"],
    "rows": [["EU", 125.0], ["US", 90.0]],
    "chart": {"type": "treemap", "x": "region", "y": ["revenue"], "title": "Share"},
    "panels": [{"sql": SQL, "columns": ["region", "revenue"], "rows": [["EU", 125.0], ["US", 90.0]],
                "chart": {"type": "treemap", "x": "region", "y": ["revenue"], "title": "Share"}}],
    "email": None, "errors": [], "citations": [], "usage": {}, "agents": [], "mode": "agent",
    "model": "anthropic:test",
}


@pytest.fixture()
def turns(monkeypatch):
    """Record every run_agent call; no other tier may answer a visual turn."""
    calls = []

    def fake_run_agent(**kw):
        calls.append(kw)
        return {**ANSWER, "panels": [dict(p) for p in ANSWER["panels"]]}

    def never(*a, **k):
        raise AssertionError("a visual turn reached another tier")

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    monkeypatch.setattr(semantic, "answer", never)
    monkeypatch.setattr(qcache, "lookup", never)
    monkeypatch.setattr(qcache, "store", never)
    monkeypatch.setattr(model_router, "choose", never)
    return calls


def _stored(cid):
    return [m["content"] for m in db.list_messages(cid) if m["role"] == "assistant"][-1]


def _title(cid):
    return next(c["title"] for c in db.list_conversations(ANA["id"]) if c["id"] == cid)


def test_a_visual_turn_goes_straight_to_the_agent_and_is_stored_d3_only(turns):
    out = chat.ask(chat.Ask(prompt="revenue share by region", source="demo", table="sales",
                            visual=True), user=ANA)
    assert len(turns) == 1 and turns[0]["visual_mode"] is True and turns[0]["model"] is None
    msg = out["message"]
    assert msg["served_by"] == "frontier" and msg["visual_mode"] is True
    stored = _stored(out["conversation_id"])
    assert stored["chart"]["type"] == "treemap"                  # D3 draws treemaps too
    assert [(p["chart"]["type"], p["engine"]) for p in stored["panels"]] == [("treemap", "d3")]
    assert stored["visual_mode"] is True
    assert _title(out["conversation_id"]) == "🎨 revenue share by region"

    # A follow-up keeps going to the agent in visual mode, with the history.
    chat.ask(chat.Ask(prompt="now as a beeswarm", source="demo", table="sales", visual=True,
                      conversation_id=out["conversation_id"]), user=ANA)
    assert len(turns) == 2 and turns[1]["visual_mode"] is True and turns[1]["history"]


def test_an_ordinary_turn_is_untouched(monkeypatch, turns):
    monkeypatch.setattr(semantic, "answer", lambda *a, **k: None)
    monkeypatch.setattr(qcache, "lookup", lambda *a, **k: None)
    monkeypatch.setattr(qcache, "store", lambda *a, **k: None)
    monkeypatch.setattr(model_router, "choose", lambda *a, **k: ("frontier", None))
    out = chat.ask(chat.Ask(prompt="revenue share by region", source="demo", table="sales"),
                   user=ANA)
    assert turns[0]["visual_mode"] is False
    stored = _stored(out["conversation_id"])
    assert stored["chart"]["type"] == "treemap" and "visual_mode" not in stored
    assert "engine" not in stored["panels"][0]
    assert not _title(out["conversation_id"]).startswith("🎨")


def test_the_engines_in_the_model_menu_give_way_to_the_frontier_model(monkeypatch, turns):
    monkeypatch.setattr(agent, "available_models",
                        lambda user: [{"spec": "bitnet"}, {"spec": "kag"}])
    for engine in ("bitnet", "kag"):
        chat.ask(chat.Ask(prompt="revenue by region", source="demo", table="sales",
                          visual=True, model=engine), user=ANA)
    assert [(c["model"], c["visual_mode"], c["kag_first"]) for c in turns] == \
        [(None, True, False), (None, True, False)]


def test_the_multi_agent_answer_is_coerced_to_d3_too(monkeypatch, turns):
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user: ["demo"])
    monkeypatch.setattr(orchestrator, "ambiguous_tables", lambda prompt, sources: [])
    monkeypatch.setattr(orchestrator, "run_orchestrated", lambda *a, **k: {
        "text": "Flows across sources.", "source": "*", "sql": None, "columns": [], "rows": [],
        "chart": None,
        "panels": [{"source": "demo", "sql": SQL, "columns": ["region", "revenue"],
                    "rows": [["EU", 125.0]],
                    "chart": {"type": "funnel", "x": "region", "y": ["revenue"]}}],
        "agents": [], "mode": "orchestrated"})
    out = chat.ask(chat.Ask(prompt="revenue across everything", source="*", table="*",
                            visual=True), user=ANA)
    stored = _stored(out["conversation_id"])
    assert [(p["chart"]["type"], p["engine"]) for p in stored["panels"]] == [("funnel", "d3")]
    assert stored["visual_mode"] is True


@pytest.mark.parametrize("prompt", [
    "build a pipeline for revenue by region",
    "run the pipeline",
    "submit the sales_daily DAG on Airflow",
])
def test_commands_are_questions_in_the_window(turns, prompt):
    out = chat.ask(chat.Ask(prompt=prompt, source="demo", table="sales", visual=True), user=ANA)
    assert len(turns) == 1 and turns[0]["visual_mode"] is True
    stored = _stored(out["conversation_id"])
    assert not stored.get("pipeline") and not stored.get("platform_run")


@pytest.mark.parametrize("extra", [
    {"pipeline_action": "build"},
    {"pipeline_action": "build", "pipeline_mode": "airflow_dag"},
    {"platform_target": "airflow"},
    {"platform_payload": {}},
    {"repository_id": "repo-1"},
])
def test_a_visual_turn_cannot_carry_a_pipeline_or_platform_action(turns, extra):
    with pytest.raises(HTTPException) as e:
        chat.ask(chat.Ask(prompt="revenue by region", source="demo", table="sales",
                          visual=True, **extra), user=ANA)
    assert e.value.status_code == 400 and "D3 & artifacts" in e.value.detail
    assert turns == []
