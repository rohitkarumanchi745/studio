"""agent.py runs every query through gateway.execute.

The agent's run_sql tool, the keyless fallback previews and canvas
composition used to call the connector themselves (validate → enforce_limit
→ run_query → governance). Now each is ONE gateway.execute call, so RBAC,
the query guard, the row cap, governance masking and the audit row are
applied in the gateway's order — and a caller with no bound user gets no
rows (fail closed) rather than an ungoverned read.

Also pins the fix for the unbound `user` in edit_canvas / compose_canvas:
both take `user=None`, pass it to make_llm (BYOK keys), and still work
without one.

Run from the backend directory:
    python -m pytest tests/test_agent_gateway.py -q
"""
import json
import os
import sys
import tempfile
import types

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-agent-gateway-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import agent, db, governance
from app.connectors.demo import DemoConnector, seed

ANALYST = {"id": "u-agent-analyst", "email": "ana@studio.test", "role": "analyst"}
VIEWER = {"id": "u-agent-viewer", "email": "view@studio.test", "role": "viewer"}

GOV_YAML = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*" } }
  viewer: { sources: { demo: [sales, web_traffic] } }
compliance:
  demo:
    customers:
      deny_columns: [name]
      mask_columns: [lifetime_value]
"""

COLUMNS = ["region", "revenue"]
ROWS = [["East", 10], ["West", 30], ["North", 20], ["South", 5]]
CHART = {"type": "bar", "title": "Revenue by region", "x": "region", "y": ["revenue"]}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    seed()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac():
    """Each test starts on the built-in policies with no governance doc."""
    governance._STATE.update(doc=None, yaml="", source=None)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


def _audit_rows(action, user=None):
    rows = db.list_activity(user["id"] if user else None, limit=1000)
    return [r for r in rows if r["action"] == action]


class _Reply:
    def __init__(self, obj):
        self.content = json.dumps(obj)


class _FakeLLM:
    def __init__(self, obj):
        self.obj = obj
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return _Reply(self.obj)


def _fake_make_llm(monkeypatch, reply):
    """Stub make_llm with a model that always answers `reply`; records the
    `user` it was handed so a test can assert the BYOK plumbing."""
    seen = {}

    def make_llm(spec, user=None, **kw):
        seen["spec"], seen["user"] = spec, user
        return _FakeLLM(reply)

    monkeypatch.setattr(agent, "make_llm", make_llm)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    return seen


# ── edit_canvas ─────────────────────────────────────────────────────────

def test_edit_canvas_without_user_or_llm_uses_rule_based_fallback(monkeypatch):
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: False)
    out = agent.edit_canvas("top 2 sorted by revenue desc", COLUMNS, ROWS, CHART, user=None)
    assert out["note"].startswith("Applied")
    assert out["columns"] == COLUMNS
    assert [r[0] for r in out["rows"]] == ["West", "North"]
    assert out["chart"]["type"] == "bar"


def test_edit_canvas_passes_user_to_make_llm(monkeypatch):
    seen = _fake_make_llm(monkeypatch, {"spec": {"type": "line"}, "note": "now a line"})
    out = agent.edit_canvas("make it a line", COLUMNS, ROWS, CHART, user=ANALYST)
    assert seen["user"] is ANALYST
    assert out["chart"]["type"] == "line"
    assert out["note"] == "now a line"
    assert not any("LLM edit failed" in w for w in out["warnings"])


def test_edit_canvas_with_llm_and_no_user_still_works(monkeypatch):
    seen = _fake_make_llm(monkeypatch, {"spec": {"type": "line"}, "note": "now a line"})
    out = agent.edit_canvas("make it a line", COLUMNS, ROWS, CHART)
    assert seen["user"] is None
    assert out["chart"]["type"] == "line"


# ── compose_canvas ──────────────────────────────────────────────────────

_PANEL_SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1"
_COMPOSE_REPLY = {
    "panels": [
        {"title": "By region (fresh)", "sql": _PANEL_SQL,
         "spec": {"type": "bar", "x": "region", "y": ["revenue"]}},
        {"title": "Current", "sql": None,
         "spec": {"type": "line", "x": "region", "y": ["revenue"]}},
    ],
    "note": "two views",
}


def test_compose_canvas_without_user_skips_sql_panels_and_writes_no_audit(monkeypatch):
    _fake_make_llm(monkeypatch, _COMPOSE_REPLY)
    before = len(_audit_rows("canvas_compose"))
    out = agent.compose_canvas("monthly and by region", COLUMNS, ROWS, CHART,
                               connector=DemoConnector(), allowed_tables=["sales"],
                               schemas={"sales": DemoConnector().get_schema("sales")},
                               user=None)
    # The fresh-SQL panel is skipped (fail closed); the reuse panel survives.
    assert len(out["panels"]) == 1 and out["panels"][0]["sql"] is None
    assert any("no user bound — panel skipped" in w for w in out["warnings"])
    assert "1 view shown; 1 skipped" in out["note"]
    assert len(_audit_rows("canvas_compose")) == before      # no query, no audit row


def test_compose_canvas_with_only_sql_panel_and_no_user_raises_with_warning(monkeypatch):
    _fake_make_llm(monkeypatch, {"panels": [_COMPOSE_REPLY["panels"][0]], "note": "x"})
    with pytest.raises(ValueError, match="no user bound — panel skipped"):
        agent.compose_canvas("by region", COLUMNS, ROWS, CHART, connector=DemoConnector())


def test_compose_canvas_with_user_executes_through_gateway_and_audits(monkeypatch):
    seen = _fake_make_llm(monkeypatch, _COMPOSE_REPLY)
    before = len(_audit_rows("canvas_compose", ANALYST))
    out = agent.compose_canvas("monthly and by region", COLUMNS, ROWS, CHART,
                               connector=DemoConnector(), allowed_tables=["sales"],
                               schemas={"sales": DemoConnector().get_schema("sales")},
                               user=ANALYST)
    assert seen["user"] is ANALYST
    fresh = out["panels"][0]
    assert fresh["rows"] and fresh["columns"] == ["region", "revenue"]
    assert fresh["sql"].upper().startswith("SELECT REGION") and "LIMIT" in fresh["sql"].upper()
    assert len(out["panels"]) == 2 and not out["warnings"]
    rows = _audit_rows("canvas_compose", ANALYST)
    assert len(rows) == before + 1
    assert rows[0]["ok"] == 1 and rows[0]["source"] == "demo" and rows[0]["sql"] == fresh["sql"]


def test_compose_canvas_viewer_sql_on_customers_is_rejected_not_run(monkeypatch):
    _fake_make_llm(monkeypatch, {"panels": [
        {"title": "PII", "sql": "SELECT name FROM customers",
         "spec": {"type": "table", "x": "name", "y": []}},
        {"title": "Current", "sql": None, "spec": {"type": "bar", "x": "region", "y": ["revenue"]}},
    ], "note": "x"})
    out = agent.compose_canvas("names", COLUMNS, ROWS, CHART, connector=DemoConnector(),
                               user=VIEWER)
    assert len(out["panels"]) == 1
    assert any(w.startswith("PII: rejected") for w in out["warnings"])
    rejected = _audit_rows("canvas_compose", VIEWER)
    assert rejected and rejected[0]["ok"] == 0


# ── keyless fallback previews ───────────────────────────────────────────

def test_fallback_preview_applies_governance_deny_columns():
    governance._set(GOV_YAML, "test")
    assert governance.loaded()
    conn = DemoConnector()
    allowed = conn.list_tables()
    out = agent._fallback("show me customers", conn, "customers", allowed, ANALYST)
    assert out["mode"] == "fallback" and out["rows"]
    cols = [c.lower() for c in out["columns"]]
    assert "name" not in cols and "city" in cols                     # denied column stripped
    lv = out["columns"].index("lifetime_value")
    assert all(r[lv] == "***" for r in out["rows"])                  # masked column
    assert out["sql"] == out["panels"][0]["sql"]
    row = _audit_rows("fallback_preview", ANALYST)[0]
    assert row["ok"] == 1 and row["tbl"] == "customers" and row["sql"] == out["sql"]


def test_fallback_whole_source_previews_the_named_table_through_gateway():
    # Table CHOICE still follows the prompt (tests/test_fallback_table_choice.py
    # pins it pre-gateway); execution and audit now come from the gateway.
    conn = DemoConnector()
    allowed = conn.list_tables()
    assert "sales" in allowed and allowed[0] != "sales"
    out = agent._fallback("show sales by region", conn, "*", allowed, ANALYST)
    assert out["sql"].lower().startswith("select * from sales")
    assert "region" in [c.lower() for c in out["columns"]] and out["rows"]
    assert _audit_rows("fallback_preview", ANALYST)[0]["tbl"] == "sales"


def test_fallback_multi_table_previews_go_through_gateway():
    governance._set(GOV_YAML, "test")
    conn = DemoConnector()
    allowed = [t for t in conn.list_tables() if t in ("customers", "sales", "web_traffic")]
    out = agent._fallback("what happened last week", conn, "*", allowed, ANALYST)
    assert out["mode"] == "fallback" and len(out["panels"]) == len(allowed)
    by_title = {p["chart"]["title"]: p for p in out["panels"]}
    assert "name" not in by_title["customers"]["columns"]
    assert all("LIMIT" in p["sql"].upper() for p in out["panels"])


def test_fallback_multi_table_previews_drop_tables_the_role_cannot_read():
    # allowed_tables only chooses WHAT to preview; the gateway decides what the
    # role may actually read — a stale/over-wide list yields no PII panel.
    conn = DemoConnector()
    out = agent._fallback("what happened", conn, "*", ["customers", "sales", "web_traffic"], VIEWER)
    titles = {p["chart"]["title"] for p in out["panels"]}
    assert titles == {"sales", "web_traffic"}
    rej = [r for r in _audit_rows("fallback_preview", VIEWER) if r["ok"] == 0]
    assert rej and rej[0]["tbl"] == "customers"


def test_fallback_granularity_panels_probe_is_unaudited_and_panels_are_audited():
    conn = DemoConnector()
    before = len(_audit_rows("fallback_preview", ANALYST))
    out = agent._fallback("revenue at month level and year level", conn, "sales",
                          conn.list_tables(), ANALYST)
    assert out["mode"] == "fallback" and len(out["panels"]) == 2
    assert [p["chart"]["x"] for p in out["panels"]] == ["month", "year"]
    assert all(p["rows"] and "LIMIT" in p["sql"].upper() for p in out["panels"])
    # Two audited reads (one per granularity); the LIMIT 1 type probe adds none.
    assert len(_audit_rows("fallback_preview", ANALYST)) == before + 2


def test_fallback_without_user_fails_closed():
    conn = DemoConnector()
    out = agent._fallback("show sales", conn, "sales", conn.list_tables(), None)
    assert out["rows"] == [] and out["text"].startswith("Could not query sales")


# ── run_agent: viewer asking for customers ──────────────────────────────

def test_viewer_run_agent_fallback_on_customers_is_rejected(monkeypatch):
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: False)
    out = agent.run_agent("show customers", DemoConnector(), "customers",
                          ["customers"], {"customers": []}, [], VIEWER)
    assert out["mode"] == "fallback" and out["rows"] == []
    assert "no access" in out["text"]
    row = _audit_rows("fallback_preview", VIEWER)[0]
    assert row["ok"] == 0 and row["tbl"] == "customers"


def _stub_langchain_tools(monkeypatch):
    """run_agent decorates its tools with langchain_core.tools.tool at call
    time. A one-line stand-in (the decorator returns the function itself)
    lets the inner tools be exercised without the langchain stack installed;
    the real package is left alone when it is present."""
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


def test_viewer_run_sql_tool_on_customers_is_rejected(monkeypatch):
    """The inner run_sql tool goes through the gateway: a viewer asking for
    customers is refused with QUERY REJECTED and the rejection is audited
    under agent_sql; nothing reaches the connector."""
    _stub_langchain_tools(monkeypatch)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **kw: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    outputs = {}

    def fake_graph(llm, tools, system, spec, volatile=None):
        run_sql = next(t for t in tools if getattr(t, "__name__", "") == "run_sql")

        class G:
            def invoke(self, state, config=None):
                outputs["pii"] = run_sql("SELECT name FROM customers")
                outputs["ok"] = run_sql("SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1")
                return {"messages": [_Msg("done")]}
        return G()

    monkeypatch.setattr(agent, "_graph", fake_graph)
    out = agent.run_agent("customers?", DemoConnector(), "*", ["sales", "web_traffic"],
                          {"sales": []}, [], VIEWER)
    assert outputs["pii"].startswith("QUERY REJECTED")
    assert out["mode"] == "agent" and any(e.startswith("rejected") for e in out["errors"])
    # The permitted query still ran through the gateway and was audited.
    preview = json.loads(outputs["ok"])
    assert preview["columns"] == ["region", "revenue"] and preview["total_rows"] > 0
    assert out["sql"].upper().endswith(f"LIMIT {agent.MAX_ROWS}") and out["rows"]
    rows = _audit_rows("agent_sql", VIEWER)
    assert {r["ok"] for r in rows} == {0, 1}
    assert any("customers" in (r["sql"] or "") and r["ok"] == 0 for r in rows)
