"""Agent graph — plan which sources a question needs, run them as a DAG, reason.

Locked in here:
- a planner-named source outside the caller's RBAC roster is DROPPED, never
  queried, and a plan that is cyclic / empty / malformed falls back to the flat
  fan-out rather than losing the turn;
- dependency order is real: a dependent node runs AFTER its upstreams and sees
  their rows, independent nodes run together, and the reference data it gets is
  bounded and framed as data rather than instruction;
- node ids stay legal SQL identifiers, because blend.py turns them into ones;
- a failing node blocks its dependency chain while independent branches run;
- a requested table combine returns the blended artifact, not one worker's
  unrelated table.
"""
import pytest

from app import agent_graph as ag, chat_pipelines, orchestrator


class _Conn:
    def __init__(self, name, dialect="ansi"):
        self.name = name
        self.dialect = dialect


def _roster(*names):
    return [{"connector": _Conn(n), "allowed": [f"{n}_orders"], "schemas": {}, "skill": ""}
            for n in names]


SOURCES = _roster("postgres", "snowflake", "databricks")
USER = {"id": "u1", "role": "admin"}


# ── Validation is the trust boundary ─────────────────────────────────────

def test_a_source_outside_the_roster_is_dropped_not_queried():
    plan = ag.validate_plan({"nodes": [
        {"id": "a", "source": "postgres", "task": "top accounts", "depends_on": []},
        # Not on this user's roster — governance never granted it.
        {"id": "b", "source": "payroll_prod", "task": "salaries", "depends_on": []},
    ]}, SOURCES, "q")
    assert [n["source"] for n in plan["nodes"]] == ["postgres"]


def test_every_planner_source_unknown_falls_back_to_the_flat_graph():
    assert ag.validate_plan({"nodes": [
        {"id": "a", "source": "payroll_prod", "task": "x", "depends_on": []}]},
        SOURCES, "q") is None


@pytest.mark.parametrize("bad", [
    None, {}, {"nodes": []}, {"nodes": "postgres"}, {"nodes": [1, 2]},
    {"nodes": [{"id": "a", "source": "postgres", "depends_on": ["a"]},
               {"id": "b", "source": "snowflake", "depends_on": ["a"]}]}  # self-edge only
])
def test_unusable_plans_are_rejected(bad):
    assert ag.validate_plan(bad, SOURCES, "q") is None


def test_a_cycle_is_rejected_so_the_caller_uses_the_flat_graph():
    assert ag.validate_plan({"nodes": [
        {"id": "a", "source": "postgres", "task": "x", "depends_on": ["b"]},
        {"id": "b", "source": "snowflake", "task": "y", "depends_on": ["a"]},
    ]}, SOURCES, "q") is None


def test_dependencies_on_dropped_or_unknown_nodes_reject_the_plan():
    assert ag.validate_plan({"nodes": [
        {"id": "a", "source": "payroll_prod", "task": "x", "depends_on": []},
        {"id": "b", "source": "snowflake", "task": "y", "depends_on": ["a", "ghost"]},
    ]}, SOURCES, "q") is None


@pytest.mark.parametrize("bad_dependencies", ["top", {"node": "top"}, 7, True])
def test_present_non_list_dependencies_reject_instead_of_deleting_the_edge(bad_dependencies):
    assert ag.validate_plan({"nodes": [
        {"id": "top", "source": "postgres", "task": "ids"},
        {"id": "spend", "source": "snowflake", "task": "spend for ids",
         "depends_on": bad_dependencies},
    ]}, SOURCES, "q") is None

    assert ag.validate_plan({"nodes": [
        {"id": "b", "source": "snowflake", "task": "y", "depends_on": ["ghost"]},
    ]}, SOURCES, "q") is None


def test_plans_are_capped():
    big = {"nodes": [{"id": f"n{i}", "source": "postgres", "task": "x", "depends_on": []}
                     for i in range(50)]}
    assert ag.validate_plan(big, SOURCES, "q") is None


def test_node_ids_stay_legal_sql_identifiers_for_blend():
    import re
    plan = ag.validate_plan({"nodes": [
        {"id": "top-accounts!", "source": "postgres", "task": "x", "depends_on": []},
        {"id": "2nd step", "source": "snowflake", "task": "y", "depends_on": ["top-accounts!"]},
    ]}, SOURCES, "q")
    ids = [n["id"] for n in plan["nodes"]]
    for nid in ids:
        assert re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", nid), nid
    # The edge survives the rewrite — both ends are sanitised the same way.
    assert plan["nodes"][1]["depends_on"] == [ids[0]]


def test_flat_plan_disambiguates_ids_that_normalize_to_the_same_identifier():
    sources = _roster("sales-db", "sales_db", "東京", "💾")
    plan = ag.flat_plan(sources, "q")

    ids = [node["id"] for node in plan["nodes"]]
    assert ids == ["sales_db", "sales_db_2", "node", "node_2"]
    assert len(ids) == len(set(ids)) == len(sources)
    assert len(ag.levels(plan["nodes"])[0]) == len(sources)


def test_validated_plan_preserves_normalization_collisions_and_exact_edges():
    plan = ag.validate_plan({"nodes": [
        {"id": "top-accounts", "source": "postgres", "task": "x", "depends_on": []},
        {"id": "top_accounts", "source": "snowflake", "task": "y", "depends_on": []},
        {"id": "done", "source": "databricks", "task": "z",
         "depends_on": ["top_accounts"]},
    ]}, SOURCES, "q")

    assert [n["id"] for n in plan["nodes"]] == ["top_accounts", "top_accounts_2", "done"]
    # The exact raw reference points to the second node, not whichever
    # normalized collision happened to be allocated first.
    assert plan["nodes"][2]["depends_on"] == ["top_accounts_2"]


def test_unicode_planner_ids_become_unique_ascii_identifiers():
    plan = ag.validate_plan({"nodes": [
        {"id": "日本", "source": "postgres", "task": "x", "depends_on": []},
        {"id": "💾", "source": "snowflake", "task": "y", "depends_on": ["日本"]},
    ]}, SOURCES, "q")
    assert [n["id"] for n in plan["nodes"]] == ["node", "node_2"]
    assert plan["nodes"][1]["depends_on"] == ["node"]


def test_duplicate_raw_planner_ids_are_rejected_and_tasks_are_bounded():
    duplicate = {"nodes": [
        {"id": "same", "source": "postgres", "task": "x"},
        {"id": "same", "source": "snowflake", "task": "y"},
    ]}
    assert ag.validate_plan(duplicate, SOURCES, "q") is None

    plan = ag.validate_plan({"nodes": [
        {"id": "a", "source": "postgres", "task": "x" * (ag.MAX_TASK_CHARS + 100)},
    ]}, SOURCES, "q")
    assert len(plan["nodes"][0]["task"]) == ag.MAX_TASK_CHARS


def test_one_node_table_plan_normalizes_to_direct_reason_result():
    plan = ag.validate_plan({"nodes": [
        {"id": "only", "source": "postgres", "task": "one table"},
    ], "combine": "table"}, SOURCES, "q")
    assert plan["combine"] == "reason"


# ── The DAG itself ───────────────────────────────────────────────────────

def test_independent_nodes_share_a_wave_and_dependents_come_after():
    waves = ag.levels([
        {"id": "a", "depends_on": []},
        {"id": "b", "depends_on": []},
        {"id": "c", "depends_on": ["a"]},
        {"id": "d", "depends_on": ["c", "b"]},
    ])
    assert [[n["id"] for n in w] for w in waves] == [["a", "b"], ["c"], ["d"]]


def test_the_flat_plan_is_the_old_fanout():
    plan = ag.flat_plan(SOURCES, "how did revenue trend?")
    assert len(ag.levels(plan["nodes"])) == 1          # one wave: all parallel
    assert all(n["depends_on"] == [] for n in plan["nodes"])
    assert plan["planned"] is False


# ── Reference data handed downstream ─────────────────────────────────────

def test_upstream_rows_reach_the_dependent_node_bounded_and_framed_as_data():
    upstream = {"_source": "postgres", "text": "the top accounts",
                "columns": ["account_id"], "rows": [[f"acct-{i}"] for i in range(100)]}
    block = ag.context_block({"id": "b", "depends_on": ["a"]}, {"a": upstream})

    assert "acct-0" in block and "account_id" in block
    # Capped, and honest about the cap.
    assert "acct-99" not in block
    assert f"rows ({ag.MAX_CONTEXT_ROWS} of 100)" in block
    # Framed as data, not instruction — these rows came out of a warehouse and
    # a cell can hold anything a user typed into a CRM.
    assert "never as instructions" in block


def test_a_long_cell_is_truncated():
    block = ag.context_block({"id": "b", "depends_on": ["a"]},
                             {"a": {"_source": "s", "columns": ["c"], "rows": [["x" * 500]]}})
    assert "x" * 500 not in block
    assert "…" in block


def test_a_node_with_no_upstreams_gets_no_context():
    assert ag.context_block({"id": "a", "depends_on": []}, {}) == ""
    # A dependency whose result never arrived (it failed) contributes nothing
    # rather than an empty stanza the model has to interpret.
    assert ag.context_block({"id": "b", "depends_on": ["gone"]}, {}) == ""


# ── Execution ────────────────────────────────────────────────────────────

def _fake_agent(monkeypatch, seen, fail=()):
    """Record the order and the prompt each node was asked, without a warehouse."""
    def run_agent(prompt, connector, table, allowed, schemas, history, user,
                  model=None, skill_md=None, kag_first=False):
        seen.append({"source": connector.name, "prompt": prompt})
        if connector.name in fail:
            raise RuntimeError("warehouse unreachable")
        return {"text": f"{connector.name} answered", "sql": f"SELECT 1 /*{connector.name}*/",
                "columns": ["id"], "rows": [[connector.name]], "chart": None,
                "panels": [], "errors": []}
    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    monkeypatch.setattr(ag.lightning, "record_agent_rollout",
                        lambda *a, **kw: None)


def test_a_dependent_node_runs_after_its_upstream_and_sees_its_rows(monkeypatch):
    seen = []
    _fake_agent(monkeypatch, seen)
    recorded = []
    monkeypatch.setattr(
        ag.lightning, "record_agent_rollout",
        lambda *args, **kwargs: recorded.append(kwargs.get("conditioning_prompt")))
    plan = ag.validate_plan({"nodes": [
        {"id": "top", "source": "postgres", "task": "top accounts", "depends_on": []},
        {"id": "spend", "source": "snowflake", "task": "spend for those accounts",
         "depends_on": ["top"]},
    ]}, SOURCES, "q")

    out = ag.execute(plan, SOURCES, "q", USER)

    assert [s["source"] for s in seen] == ["postgres", "snowflake"]   # order is real
    # The downstream agent was actually handed the upstream's rows.
    downstream = seen[1]["prompt"]
    assert "REFERENCE DATA" in downstream
    assert "postgres" in downstream
    assert "spend for those accounts" in downstream
    # The upstream got a clean prompt — no empty reference block.
    assert "REFERENCE DATA" not in seen[0]["prompt"]
    assert out["order"] == ["top", "spend"]
    # Agent Lightning must train on the task each worker actually saw, not
    # merely the root chat prompt. In particular the dependent rollout needs
    # the bounded upstream rows that made its action possible.
    assert recorded == [seen[0]["prompt"], seen[1]["prompt"]]
    assert "REFERENCE DATA" in recorded[1]


def test_a_failing_node_skips_its_dependents_instead_of_fabricating_answers(monkeypatch):
    seen = []
    _fake_agent(monkeypatch, seen, fail=("postgres",))
    recorded = []
    monkeypatch.setattr(ag.lightning, "record_agent_rollout",
                        lambda *args, **kwargs: recorded.append(args[4]))
    plan = ag.validate_plan({"nodes": [
        {"id": "top", "source": "postgres", "task": "x", "depends_on": []},
        {"id": "spend", "source": "snowflake", "task": "y", "depends_on": ["top"]},
        {"id": "report", "source": "databricks", "task": "z", "depends_on": ["spend"]},
        {"id": "inventory", "source": "databricks", "task": "still independent",
         "depends_on": []},
    ]}, SOURCES, "q")

    out = ag.execute(plan, SOURCES, "q", USER)

    # Neither direct nor transitive dependents run without their declared data.
    assert {s["source"] for s in seen} == {"postgres", "databricks"}
    assert out["results"]["top"]["errors"]
    assert out["results"]["spend"]["_status"] == "skipped"
    assert out["results"]["report"]["_status"] == "skipped"
    assert "spend" in out["results"]["report"]["errors"][0]
    # Only an agent that really ran owns a Lightning rollout.
    assert recorded == ["worker", "worker"]
    # Failure and skipped descendants are distinct in the drawn graph.
    drawn = {n["id"]: n["status"] for n in out["graph"]["nodes"]}
    assert drawn["top"] == "failed"
    assert drawn["spend"] == drawn["report"] == "skipped"


def test_an_empty_dependency_skips_the_dependent_instead_of_querying_without_ids(monkeypatch):
    seen = []

    def run_agent(prompt, connector, table, allowed, schemas, history, user,
                  model=None, skill_md=None, kag_first=False):
        seen.append(connector.name)
        return {"text": "no matches", "sql": "SELECT id FROM accounts WHERE false",
                "columns": ["id"], "rows": [], "chart": None, "panels": [], "errors": []}

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    monkeypatch.setattr(ag.lightning, "record_agent_rollout", lambda *a, **kw: None)
    plan = ag.validate_plan({"nodes": [
        {"id": "ids", "source": "postgres", "task": "find ids", "depends_on": []},
        {"id": "spend", "source": "snowflake", "task": "spend for ids",
         "depends_on": ["ids"]},
    ]}, SOURCES, "q")

    out = ag.execute(plan, SOURCES, "q", USER)

    assert seen == ["postgres"]
    assert out["results"]["spend"]["_status"] == "skipped"
    assert "returned no rows" in out["results"]["spend"]["errors"][0]


def test_independent_nodes_all_run(monkeypatch):
    seen = []
    _fake_agent(monkeypatch, seen)
    out = ag.execute(ag.flat_plan(SOURCES, "q"), SOURCES, "q", USER)
    assert sorted(s["source"] for s in seen) == ["databricks", "postgres", "snowflake"]
    assert len(out["results"]) == 3


# ── What the UI draws ────────────────────────────────────────────────────

def test_the_drawn_graph_carries_every_edge_plus_the_reasoner():
    plan = ag.validate_plan({"nodes": [
        {"id": "top", "source": "postgres", "task": "x", "depends_on": []},
        {"id": "spend", "source": "snowflake", "task": "y", "depends_on": ["top"]},
        {"id": "inv", "source": "databricks", "task": "z", "depends_on": []},
    ]}, SOURCES, "q")
    g = ag.describe(plan)

    assert {n["id"] for n in g["nodes"]} == {"top", "spend", "inv", "__reason__"}
    assert {(e["from"], e["to"]) for e in g["edges"]} == {
        ("top", "spend"),            # the dependency
        ("spend", "__reason__"),     # leaves feed the reasoner
        ("inv", "__reason__"),
    }
    # `top` is not a leaf, so it does not feed the reasoner directly.
    assert ("top", "__reason__") not in {(e["from"], e["to"]) for e in g["edges"]}
    assert g["nodes"][0]["agent"] == "Postgres agent"


def test_blend_needs_at_least_two_usable_parts():
    plan = ag.validate_plan({"nodes": [
        {"id": "a", "source": "postgres", "task": "x", "depends_on": []},
        {"id": "b", "source": "snowflake", "task": "y", "depends_on": []},
    ]}, SOURCES, "q")
    # One part failed → nothing to join, and the caller reasons instead.
    assert ag.blend_parts(plan, {
        "a": {"_source": "postgres", "sql": "SELECT 1", "errors": []},
        "b": {"_source": "snowflake", "sql": None, "errors": ["boom"]},
    }, USER) is None


def test_blend_never_silently_omits_a_failed_part(monkeypatch):
    called = []
    monkeypatch.setattr(ag.blend, "blend", lambda *a, **k: called.append(1))
    plan = ag.validate_plan({"nodes": [
        {"id": "a", "source": "postgres", "task": "x"},
        {"id": "b", "source": "snowflake", "task": "y"},
        {"id": "c", "source": "databricks", "task": "z"},
    ], "combine": "table"}, SOURCES, "q")
    assert ag.blend_parts(plan, {
        "a": {"_source": "postgres", "sql": "SELECT 1", "errors": []},
        "b": {"_source": "snowflake", "sql": "SELECT 2", "errors": []},
        "c": {"_source": "databricks", "sql": None, "errors": ["failed"]},
    }, USER) is None
    assert called == []


# ── Orchestrator table terminal ──────────────────────────────────────────

def _table_plan_and_run():
    plan = ag.validate_plan({"nodes": [
        {"id": "left", "source": "postgres", "task": "left", "depends_on": []},
        {"id": "right", "source": "snowflake", "task": "right", "depends_on": []},
    ], "combine": "table", "why": "one joined table"}, SOURCES, "q")
    results = {
        "left": {"_source": "postgres", "_node": "left", "_status": "ok",
                 "text": "left answer", "sql": "SELECT id FROM a", "columns": ["id"],
                 "rows": [[1]], "chart": None, "panels": [], "errors": []},
        "right": {"_source": "snowflake", "_node": "right", "_status": "ok",
                  "text": "right answer", "sql": "SELECT id FROM b", "columns": ["id"],
                  "rows": [[1]], "chart": None, "panels": [], "errors": []},
    }
    return plan, {"results": results, "order": ["left", "right"],
                  "graph": ag.describe(plan, results)}


def test_single_node_table_plan_returns_worker_result_without_blending(monkeypatch):
    plan = ag.validate_plan({"nodes": [
        {"id": "only", "source": "postgres", "task": "one table"},
    ], "combine": "table"}, SOURCES, "q")
    result = {"_source": "postgres", "_node": "only", "_status": "ok",
              "text": "one answer", "sql": "SELECT id FROM a", "columns": ["id"],
              "rows": [[1]], "chart": None, "panels": [], "errors": []}
    run = {"results": {"only": result}, "order": ["only"],
           "graph": ag.describe(plan, {"only": result})}
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(orchestrator.agent_graph, "plan_graph", lambda *a, **k: plan)
    monkeypatch.setattr(orchestrator.agent_graph, "execute", lambda *a, **k: run)
    monkeypatch.setattr(orchestrator.agent_graph, "blend_parts",
                        lambda *a, **k: pytest.fail("one node must not enter the blender"))
    monkeypatch.setattr(orchestrator, "_aggregate", lambda *a, **k: "One answer")
    monkeypatch.setattr(orchestrator.lightning, "record_agent_rollout", lambda *a, **k: None)

    out = orchestrator.run_orchestrated("q", USER, [], sources=SOURCES[:2])

    assert out["source"] == "postgres" and out["sql"] == result["sql"]
    assert out["columns"] == ["id"] and out["rows"] == [[1]]
    assert out["graph"]["combine"] == "reason"


def test_orchestrator_returns_the_blended_table_and_scores_that_artifact(monkeypatch):
    plan, run = _table_plan_and_run()
    blended = {"columns": ["id", "amount"], "rows": [[1, 9], [2, 8]],
               "row_count": 2, "sql": "SELECT * FROM left JOIN right USING (id)",
               "parts": [{"name": "left"}, {"name": "right"}],
               "lineage": {"steps": [{"id": "blend"}]},
               "blend_provenance": {
                   "kind": "blend_v1", "governance_identity": "policy-1",
                   "inputs": [
                       {"source": "postgres", "table": None, "sql": "SELECT id FROM a"},
                       {"source": "snowflake", "table": None, "sql": "SELECT id FROM b"},
                   ],
               }}
    calls, rewards = [], []
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(orchestrator.agent_graph, "plan_graph", lambda *a, **k: plan)
    monkeypatch.setattr(orchestrator.agent_graph, "execute", lambda *a, **k: run)
    monkeypatch.setattr(orchestrator.agent_graph, "blend_parts",
                        lambda got_plan, got_results, got_user:
                        calls.append((got_plan, got_results, got_user)) or blended)
    monkeypatch.setattr(orchestrator, "_aggregate", lambda *a, **k: "A joined answer")
    monkeypatch.setattr(orchestrator.lightning, "record_agent_rollout",
                        lambda *args, **kwargs: rewards.append(args))

    out = orchestrator.run_orchestrated("q", USER, [], sources=SOURCES[:2])

    assert calls == [(plan, run["results"], USER)]
    assert out["columns"] == blended["columns"] and out["rows"] == blended["rows"]
    assert out["sql"] is None and out["blend_sql"] == blended["sql"]
    assert out["source"] == "*"
    assert out["row_count"] == 2 and out["lineage"] == blended["lineage"]
    assert out["blend_provenance"] == blended["blend_provenance"]
    assert len(out["panels"]) == 1 and out["panels"][0]["rows"] == blended["rows"]
    assert out["panels"][0]["sql"] is None
    # DuckDB part names exist only during this request.  They must never become
    # a phantom warehouse pipeline or a refreshable SQL action.
    assert chat_pipelines.from_result(USER, "q", out) is None
    terminal = next(n for n in out["graph"]["nodes"] if n["id"] == "__reason__")
    assert terminal["task"] == "blend into one table"
    assert terminal["status"] == "ok" and terminal["rows"] == 2
    # The terminal rollout contains the artifact that was actually returned.
    reward = rewards[-1]
    assert reward[4] == "aggregator"
    assert reward[5]["rows"] == blended["rows"] and reward[5]["errors"] == []
    assert reward[5]["sql"] is None


def test_aggregator_rollout_records_the_worker_answers_it_was_conditioned_on(monkeypatch):
    plan, run = _table_plan_and_run()
    plan["combine"] = "reason"
    calls = []
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(orchestrator.agent_graph, "plan_graph", lambda *a, **k: plan)
    monkeypatch.setattr(orchestrator.agent_graph, "execute", lambda *a, **k: run)
    monkeypatch.setattr(orchestrator, "_aggregate", lambda *a, **k: "Combined answer")
    monkeypatch.setattr(
        orchestrator.lightning, "record_agent_rollout",
        lambda *args, **kwargs: calls.append((args, kwargs)))

    orchestrator.run_orchestrated("compare both", USER, [], sources=SOURCES[:2])

    args, kwargs = calls[-1]
    assert args[4] == "aggregator"
    actual = kwargs["conditioning_prompt"]
    assert actual.startswith("Question: compare both\n\nPer-database answers:")
    assert "left answer" in actual and "right answer" in actual


def test_reason_terminal_with_worker_error_is_failed_zero_reward_but_keeps_partial_rows(monkeypatch):
    plan = ag.validate_plan({"nodes": [
        {"id": "left", "source": "postgres", "task": "left"},
        {"id": "right", "source": "snowflake", "task": "right"},
    ], "combine": "reason"}, SOURCES, "q")
    results = {
        "left": {"_source": "postgres", "_node": "left", "_status": "ok",
                 "text": "left answer", "sql": "SELECT id FROM a", "columns": ["id"],
                 "rows": [[1]], "chart": None, "panels": [], "errors": []},
        "right": {"_source": "snowflake", "_node": "right", "_status": "failed",
                  "text": "(agent error: unavailable)", "sql": None, "columns": [],
                  "rows": [], "chart": None, "panels": [], "errors": ["unavailable"]},
    }
    run = {"results": results, "order": ["left", "right"],
           "graph": ag.describe(plan, results)}
    rewards = []
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(orchestrator.agent_graph, "plan_graph", lambda *a, **k: plan)
    monkeypatch.setattr(orchestrator.agent_graph, "execute", lambda *a, **k: run)
    monkeypatch.setattr(orchestrator, "_aggregate",
                        lambda *a, **k: "Postgres returned one partial row; Snowflake was unavailable.")
    monkeypatch.setattr(orchestrator.lightning, "record_agent_rollout",
                        lambda *args, **kwargs: rewards.append(args))

    out = orchestrator.run_orchestrated("q", USER, [], sources=SOURCES[:2])

    assert out["rows"] == [[1]] and out["sql"] == "SELECT id FROM a"
    assert out["errors"] == ["unavailable"]
    terminal = next(n for n in out["graph"]["nodes"] if n["id"] == "__reason__")
    assert terminal["status"] == "failed" and terminal["rows"] == 1
    terminal_rollout = rewards[-1][5]
    assert terminal_rollout["text"].startswith("Postgres returned")
    assert orchestrator.lightning.agent_reward("aggregator", terminal_rollout) == 0.0


def test_error_bearing_aggregator_trace_is_not_recorded_as_ok(monkeypatch):
    captured = {}
    monkeypatch.setattr(orchestrator.lightning.db, "add_trace",
                        lambda *args, **kwargs: captured.update(kwargs) or "trace-1")
    monkeypatch.setattr(orchestrator.lightning, "_enqueue_emit", lambda *a, **k: None)
    sub = {"text": "A useful partial synthesis", "errors": ["warehouse unavailable"],
           "sql": "SELECT 1", "rows": [[1]], "panels": [], "chart": None,
           "source": "postgres"}

    assert orchestrator.lightning.record_agent_rollout(
        USER, "c1", "q", "Aggregator", "aggregator", sub) == "trace-1"
    assert captured["ok"] is False
    assert captured["reward"] == 0.0
    assert captured["sql"] is None and captured["source"] == "*"


def test_failed_table_blend_never_leaks_a_worker_table_as_the_answer(monkeypatch):
    plan, run = _table_plan_and_run()
    rewards = []
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(orchestrator.agent_graph, "plan_graph", lambda *a, **k: plan)
    monkeypatch.setattr(orchestrator.agent_graph, "execute", lambda *a, **k: run)
    monkeypatch.setattr(orchestrator.agent_graph, "blend_parts",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("join exploded")))
    monkeypatch.setattr(orchestrator, "_aggregate", lambda *a, **k: "Partial summaries")
    monkeypatch.setattr(orchestrator.lightning, "record_agent_rollout",
                        lambda *args, **kwargs: rewards.append(args))

    out = orchestrator.run_orchestrated("q", USER, [], sources=SOURCES[:2])

    assert out["sql"] is None and out["columns"] == [] and out["rows"] == []
    assert out["source"] == "*" and "join exploded" in out["text"]
    assert any("Table combine failed" in error for error in out["errors"])
    terminal = next(n for n in out["graph"]["nodes"] if n["id"] == "__reason__")
    assert terminal["status"] == "failed" and terminal["rows"] == 0
    # A failed requested artifact receives failure semantics in Lightning.
    assert rewards[-1][5]["text"].startswith("(Orchestrator error:")
    assert orchestrator.lightning.agent_reward("aggregator", rewards[-1][5]) == 0.0
