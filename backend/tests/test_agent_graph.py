"""Agent graph — plan which sources a question needs, run them as a DAG, reason.

Locked in here:
- a planner-named source outside the caller's RBAC roster is DROPPED, never
  queried, and a plan that is cyclic / empty / malformed falls back to the flat
  fan-out rather than losing the turn;
- dependency order is real: a dependent node runs AFTER its upstreams and sees
  their rows, independent nodes run together, and the reference data it gets is
  bounded and framed as data rather than instruction;
- node ids stay legal SQL identifiers, because blend.py turns them into ones;
- a failing node degrades the answer instead of aborting the graph.
"""
import pytest

from app import agent_graph as ag


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
def test_unusable_plans_are_rejected_or_stripped(bad):
    plan = ag.validate_plan(bad, SOURCES, "q")
    if plan is not None:
        # A self-edge is stripped rather than rejected; what must never survive
        # is a node depending on itself.
        assert all(n["id"] not in n["depends_on"] for n in plan["nodes"])


def test_a_cycle_is_rejected_so_the_caller_uses_the_flat_graph():
    assert ag.validate_plan({"nodes": [
        {"id": "a", "source": "postgres", "task": "x", "depends_on": ["b"]},
        {"id": "b", "source": "snowflake", "task": "y", "depends_on": ["a"]},
    ]}, SOURCES, "q") is None


def test_dependencies_on_dropped_nodes_are_pruned():
    plan = ag.validate_plan({"nodes": [
        {"id": "a", "source": "payroll_prod", "task": "x", "depends_on": []},
        {"id": "b", "source": "snowflake", "task": "y", "depends_on": ["a", "ghost"]},
    ]}, SOURCES, "q")
    # b survives; its edge to the dropped node does not, so it still runs.
    assert [n["id"] for n in plan["nodes"]] == ["b"]
    assert plan["nodes"][0]["depends_on"] == []


def test_plans_are_capped():
    big = {"nodes": [{"id": f"n{i}", "source": "postgres", "task": "x", "depends_on": []}
                     for i in range(50)]}
    assert len(ag.validate_plan(big, SOURCES, "q")["nodes"]) == ag.MAX_NODES


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


def test_a_failing_node_degrades_the_answer_instead_of_aborting_the_graph(monkeypatch):
    seen = []
    _fake_agent(monkeypatch, seen, fail=("postgres",))
    plan = ag.validate_plan({"nodes": [
        {"id": "top", "source": "postgres", "task": "x", "depends_on": []},
        {"id": "spend", "source": "snowflake", "task": "y", "depends_on": ["top"]},
    ]}, SOURCES, "q")

    out = ag.execute(plan, SOURCES, "q", USER)

    # The dependent still ran — with no reference data, since there was none.
    assert [s["source"] for s in seen] == ["postgres", "snowflake"]
    assert out["results"]["top"]["errors"]
    assert "REFERENCE DATA" not in seen[1]["prompt"]
    # And the failure is visible in the drawn graph.
    drawn = {n["id"]: n["status"] for n in out["graph"]["nodes"]}
    assert drawn["top"] == "failed" and drawn["spend"] == "ok"


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
