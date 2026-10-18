"""A certified metric is answered from its definition in every mode.

The single-source chat already answers from the semantic layer. Two modes
skipped it: All sources (the orchestrator fans out across agents) and the D3 &
artifacts window (the agent draws a custom visual). There, a prompt that
resolves — in exactly one source — to metrics that are all CERTIFIED is now
answered from their definitions; the D3 window draws it with D3. Uncertified
models keep the old behaviour in both. Applying a YAML says which definitions
the warehouse does not hold as written.
"""
import importlib
import types

import pytest

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANA = {"id": "u-ana", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}

CERTIFIED = """
models:
  - source: demo
    table: sales
    metrics:
      - name: net_revenue
        agg: sum
        expr: revenue
        synonyms: [revenue]
        certified: true
    dimensions:
      - name: region
        expr: region
"""
UNCERTIFIED = CERTIFIED.replace("        certified: true\n", "")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "HARRIER_EMBED_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import (agent, chat, connections, governance, jobs, metagraph, orchestrator,
                     queries, semantic)
    from app.connectors import demo
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    _db.init_db()
    for module in (chat, governance, semantic, queries, jobs, metagraph, connections):
        module.init_tables()
    demo.seed()
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n'
                    '  analyst: { sources: "*" }\n', "test")
    calls = {"agent": 0, "orchestrator": 0}

    def no_agent(**kw):
        calls["agent"] += 1
        return {"text": "agent", "sql": None, "columns": [], "rows": [], "chart": None,
                "panels": [], "errors": [], "agents": [], "mode": "agent", "model": "x"}

    def no_fan_out(*a, **k):
        calls["orchestrator"] += 1
        return {"text": "fanned out", "source": "*", "sql": None, "columns": [], "rows": [],
                "chart": None, "panels": [], "agents": [], "mode": "orchestrated"}
    monkeypatch.setattr(agent, "run_agent", no_agent)
    monkeypatch.setattr(orchestrator, "run_orchestrated", no_fan_out)
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user: [])
    monkeypatch.setattr(orchestrator, "ambiguous_tables", lambda prompt, sources: [])

    def apply(text):
        return semantic.apply_yaml(semantic.YamlIn(yaml=text), user=ADMIN)
    yield types.SimpleNamespace(chat=chat, semantic=semantic, metagraph=metagraph, calls=calls,
                                apply=apply, mp=monkeypatch)
    semantic._STATE.update(doc=None, yaml="", source=None)
    governance._STATE.update(doc=None, yaml="", source=None)


def ask(env, **kw):
    return env.chat.ask(env.chat.Ask(**kw), user=ANA)["message"]


def test_all_sources_answers_a_certified_metric_from_its_definition(env):
    env.apply(CERTIFIED)
    msg = ask(env, prompt="revenue by region", source="*", table="*")
    assert msg["served_by"] == "semantic" and msg["semantic"]["certified"] is True
    assert env.calls == {"agent": 0, "orchestrator": 0}            # no fan-out
    assert {r[0] for r in msg["rows"]} and "SUM(revenue)" in msg["sql"]


def test_the_d3_window_draws_a_certified_metric_from_its_definition(env):
    env.apply(CERTIFIED)
    msg = ask(env, prompt="revenue by region", source="demo", table="sales", visual=True)
    assert msg["served_by"] == "semantic" and msg["visual_mode"] is True
    assert [p["engine"] for p in msg["panels"]] == ["d3"]
    assert env.calls["agent"] == 0


def test_uncertified_models_keep_the_agents_in_both_modes(env):
    env.apply(UNCERTIFIED)
    ask(env, prompt="revenue by region", source="*", table="*")
    ask(env, prompt="revenue by region", source="demo", table="sales", visual=True)
    assert env.calls == {"agent": 1, "orchestrator": 1}


def test_a_certified_metric_in_several_sources_is_left_to_the_fan_out(env):
    env.apply(CERTIFIED)
    real = env.semantic.resolve
    env.mp.setattr(env.semantic, "resolve",
                   lambda source, prompt, table=None: real("demo", prompt, table))
    assert env.semantic.certified_answer(ANA, ["demo", "warehouse"], "revenue by region") is None
    assert env.semantic.certified_answer(ANA, ["demo"], "revenue by region")


def test_a_follow_up_is_never_answered_from_a_definition_out_of_context(env):
    env.apply(CERTIFIED)
    first = env.chat.ask(env.chat.Ask(prompt="revenue by region", source="*", table="*"),
                         user=ANA)
    # It would resolve on its own, but in this conversation it may mean more.
    env.chat.ask(env.chat.Ask(prompt="revenue by region, only the west", source="*",
                              table="*", conversation_id=first["conversation_id"]), user=ANA)
    assert env.calls["orchestrator"] == 1
    assert env.semantic.certified_answer(ANA, ["demo"], "revenue by region, only the west")


def test_applying_a_yaml_says_what_the_warehouse_does_not_hold(env):
    out = env.apply(CERTIFIED)
    assert any("unchecked" in w for w in out["warnings"])          # demo not discovered yet
    env.metagraph.discover("demo", "test")
    assert env.apply(CERTIFIED)["warnings"] == []
    broken = CERTIFIED.replace("expr: revenue", "expr: gross_revenue")
    warnings = env.apply(broken)["warnings"]
    assert warnings and "net_revenue" in warnings[0] and "broken" in warnings[0]
    assert "gross_revenue" in warnings[0]
