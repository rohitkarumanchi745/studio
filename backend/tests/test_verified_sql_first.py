"""The Verified SQL library on the chat path.

Precedence: certified semantic definitions → verified SQL → plan cache →
agents. An EXACT match (same intent, values and ranking direction) is re-run
as the asker and named in the answer; a RELATED question hands the agent the
saved query to adapt; nothing broken, private to someone else, or out of the
asker's reach is ever used. Verified is not certified, and the answer says so.
"""
import importlib
import math
import re
import types

import pytest
from fastapi import HTTPException

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANA = {"id": "u-ana", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}
BEN = {"id": "u-ben", "email": "ben@studio.test", "role": "analyst", "name": "Ben"}
VIC = {"id": "u-vic", "email": "vic@studio.test", "role": "viewer", "name": "Vic"}

REGION_SQL = ("SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region "
              "ORDER BY revenue DESC")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_METAGRAPH_REFRESH_SECONDS", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "HARRIER_EMBED_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import (agent, chat, connections, governance, jobs, metagraph, orchestrator,
                     qcache, queries, semantic, verified_sql)
    from app.connectors import demo
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    _db.init_db()
    for module in (chat, governance, semantic, queries, jobs, metagraph, connections, qcache):
        module.init_tables()
    demo.seed()
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n'
                    '  analyst: { sources: "*" }\n'
                    '  viewer: { sources: { demo: [customers] } }\n', "test")
    calls = {"agent": [], "orchestrator": 0, "cache": 0}

    def fake_agent(**kw):
        calls["agent"].append(kw)
        return {"text": "agent answer", "sql": None, "columns": [], "rows": [], "chart": None,
                "panels": [], "errors": [], "agents": [], "mode": "agent", "model": "x"}

    def fan_out(*a, **k):
        calls["orchestrator"] += 1
        return {"text": "fanned out", "source": "*", "sql": None, "columns": [], "rows": [],
                "chart": None, "panels": [], "agents": [], "mode": "orchestrated"}
    real_lookup = qcache.lookup

    def counting_lookup(*a, **k):
        calls["cache"] += 1
        return real_lookup(*a, **k)
    monkeypatch.setattr(agent, "run_agent", fake_agent)
    monkeypatch.setattr(orchestrator, "run_orchestrated", fan_out)
    monkeypatch.setattr(orchestrator, "accessible_sources", lambda user: [])
    monkeypatch.setattr(orchestrator, "ambiguous_tables", lambda prompt, sources: [])
    monkeypatch.setattr(qcache, "lookup", counting_lookup)

    def save(user, prompt, sql=REGION_SQL, visibility="private"):
        return queries.create(queries.SaveIn(prompt=prompt, sql=sql, source="demo",
                                             table="sales", visibility=visibility), user=user)

    def ask(user, prompt, **kw):
        kw = {"source": "demo", "table": "sales", **kw}
        return chat.ask(chat.Ask(prompt=prompt, **kw), user=user)
    yield types.SimpleNamespace(db=_db, chat=chat, queries=queries, vs=verified_sql,
                                semantic=semantic, metagraph=metagraph, calls=calls,
                                save=save, ask=ask, mp=monkeypatch)
    semantic._STATE.update(doc=None, yaml="", source=None)
    governance._STATE.update(doc=None, yaml="", source=None)


def hints(env):
    return "\n".join(env.calls["agent"][-1]["join_hints"])


# ── Exact matches answer; they are re-run as the asker ──────────────────

def test_your_own_verified_query_answers_the_same_question(env):
    q = env.save(ANA, "revenue by region")
    msg = env.ask(ANA, "Revenue by region")["message"]
    assert msg["served_by"] == "verified_sql"
    v = msg["verified_sql"]
    assert (v["id"], v["match"], v["mine"], v["certified"]) == (q["id"], "exact", True, False)
    assert "not certified" in msg["text"] and msg["rows"]
    assert env.calls["agent"] == [] and env.calls["cache"] == 0      # before the plan cache


def test_an_org_shared_query_answers_others_and_a_private_one_never_does(env):
    env.save(BEN, "revenue by region")                                # Ben's, private
    assert env.ask(ANA, "revenue by region")["message"]["served_by"] != "verified_sql"
    assert "revenue by region" not in hints(env)                      # not even as a hint
    env.save(BEN, "revenue by region", visibility="org")
    msg = env.ask(ANA, "revenue by region")["message"]
    assert msg["served_by"] == "verified_sql"
    assert msg["verified_sql"]["mine"] is False and msg["verified_sql"]["shared"] is True
    assert "the organization's verified query" in msg["text"]


def test_it_is_revalidated_as_the_asker(env):
    env.save(ADMIN, "revenue by region", visibility="org")
    msg = env.ask(VIC, "revenue by region", table="customers")["message"]
    assert msg["served_by"] != "verified_sql" and not msg.get("rows")  # sales is not Vic's


# ── Close is not the same: related questions guide the agent ─────────────

@pytest.mark.parametrize("asked, why", [
    ("revenue by region in 2023", "its values differ"),
    ("lowest revenue by region", "it ranks the other way"),
])
def test_a_question_that_differs_is_guided_not_answered(env, asked, why):
    env.save(ANA, "revenue by region in 2024" if "2023" in asked
             else "highest revenue by region")
    msg = env.ask(ANA, asked)["message"]
    assert msg["served_by"] != "verified_sql" and len(env.calls["agent"]) == 1
    assert REGION_SQL in hints(env) and why in hints(env) and "not certified" in hints(env)
    assert msg["verified_sql"]["match"] == "related"


def test_an_unrelated_question_gets_no_verified_query(env):
    env.save(ANA, "revenue by region")
    msg = env.ask(ANA, "which customers live in Austin")["message"]
    assert "verified_sql" not in msg and "verified query" not in hints(env)


def test_a_follow_up_is_never_answered_from_the_library(env):
    env.save(ANA, "revenue by region")
    first = env.ask(ANA, "units by product")
    msg = env.ask(ANA, "revenue by region",
                  conversation_id=first["conversation_id"])["message"]
    assert msg["served_by"] != "verified_sql"


# ── Linked to the metadata graph ─────────────────────────────────────────

def test_a_query_whose_table_is_gone_is_never_used(env):
    env.metagraph.discover("demo", "test")
    q = env.save(ANA, "revenue by region")
    assert env.queries.get(q["id"], user=ANA)["dependencies"]["status"] == "valid"
    with env.db.connect() as c:
        c.execute("UPDATE meta_objects SET removed_at=1 WHERE source='demo' AND name='sales'")
        c.commit()
    dep = env.queries.get(q["id"], user=ANA)["dependencies"]
    assert dep["status"] == "broken" and "sales" in dep["reasons"][0]
    msg = env.ask(ANA, "revenue by region")["message"]
    assert msg["served_by"] != "verified_sql" and "verified query" not in hints(env)


def test_a_definition_changed_since_verification_only_guides(env):
    env.metagraph.discover("demo", "test")
    env.save(ANA, "revenue by region")
    env.semantic.apply_yaml(env.semantic.YamlIn(yaml="""
models:
  - source: demo
    table: sales
    metrics:
      - name: net_rev
        agg: sum
        expr: revenue
"""), user=ADMIN)
    msg = env.ask(ANA, "revenue by region")["message"]
    assert msg["served_by"] != "verified_sql"
    assert "definition of net_rev changed" in hints(env)


# ── Precedence, in every mode ────────────────────────────────────────────

def test_a_certified_metric_comes_before_a_verified_query(env):
    env.save(ANA, "revenue by region")
    env.semantic.apply_yaml(env.semantic.YamlIn(yaml="""
models:
  - source: demo
    table: sales
    metrics:
      - {name: revenue, agg: sum, expr: revenue, certified: true}
    dimensions:
      - {name: region, expr: region}
"""), user=ADMIN)
    assert env.ask(ANA, "revenue by region")["message"]["served_by"] == "semantic"


def test_all_sources_and_the_d3_window_answer_from_it_too(env):
    env.save(ANA, "revenue by region")
    msg = env.ask(ANA, "revenue by region", source="*", table="*")["message"]
    assert msg["served_by"] == "verified_sql" and env.calls["orchestrator"] == 0
    msg = env.ask(ANA, "revenue by region", visual=True)["message"]
    assert msg["served_by"] == "verified_sql" and msg["visual_mode"] is True
    assert [p["engine"] for p in msg["panels"]] == ["d3"] and env.calls["agent"] == []


# ── By meaning, with Harrier ─────────────────────────────────────────────

CONCEPTS = [{"revenue", "turnover", "sales", "income"}, {"region", "territory", "area"},
            {"customer", "client"}, {"product", "item"}]


def fake_embed(text, kind="query", instruct=None):
    words = re.findall(r"[a-z]+", text.lower())
    vec = [sum(1 for w in words if w in g) for g in CONCEPTS] + [0.2]
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def test_requirements_are_matched_by_meaning_and_kept_indexed(env):
    from app import embed
    embedded = []
    env.mp.setattr(embed, "available", lambda: True)
    env.mp.setattr(embed, "embed", lambda text, kind="query", instruct=None:
                   embedded.append(kind) or fake_embed(text))
    q = env.save(ANA, "revenue by region")
    assert embedded == ["document"]                                   # indexed on save
    msg = env.ask(ANA, "turnover per territory")["message"]
    assert msg["served_by"] == "verified_sql" and msg["verified_sql"]["id"] == q["id"]
    env.queries.update(q["id"], env.queries.UpdateIn(prompt="units by product"), user=ANA)
    assert embedded.count("document") == 2                            # re-indexed on edit
    env.queries.remove(q["id"], user=ANA)
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM saved_query_embeddings").fetchone()["n"] == 0
