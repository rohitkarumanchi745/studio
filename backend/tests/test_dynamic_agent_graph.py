"""Runtime delegation: workers grow the governed graph after execution starts.

These tests deliberately exercise the server-owned inbox and scheduler rather
than trusting a model-produced static plan.  Provider calls and warehouses are
stubbed; RBAC roster validation, wave construction, context hand-off, graph
serialization, and the optional agent tool all remain real.
"""
import sys
import threading
import types

import pytest

from app import agent, agent_graph as ag, orchestrator


class _Conn:
    def __init__(self, name, dialect="ansi"):
        self.name = name
        self.dialect = dialect


def _roster(*names):
    return [{"connector": _Conn(name), "allowed": [f"{name}_orders"],
             "schemas": {}, "skill": f"skill for {name}"}
            for name in names]


SOURCES = _roster("postgres", "snowflake", "databricks")
USER = {"id": "u-runtime-graph", "role": "admin", "email": "admin@example.test"}


def _seed(source="postgres", task="seed task", node_id="seed", sources=SOURCES):
    plan = ag.validate_plan({"nodes": [
        {"id": node_id, "source": source, "task": task, "depends_on": []},
    ]}, sources, task)
    assert plan is not None
    return plan


def _enable_frontier_delegation(monkeypatch):
    """Make execute() expose an inbox without constructing a real provider."""
    monkeypatch.setenv("STUDIO_AGENT_DYNAMIC_SPAWN", "1")
    monkeypatch.setattr(ag.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(ag.agent, "llm_spec", lambda: "openai:gpt-test")
    monkeypatch.setattr(ag.agent, "concrete_model_spec", lambda spec: spec)
    monkeypatch.setattr(ag.agent, "self_hosted", lambda spec: False)
    monkeypatch.setattr(ag.lightning, "record_agent_rollout", lambda *a, **k: None)


def _answer(source, rows=None, errors=None):
    return {
        "text": f"{source} answered",
        "sql": f"SELECT 1 /* {source} */",
        "columns": ["id", "note"],
        "rows": [[source, "ok"]] if rows is None else rows,
        "chart": None,
        "panels": [],
        "email": None,
        "errors": list(errors or []),
    }


def test_worker_spawns_child_absent_from_seed_in_next_wave_with_bounded_parent_rows(
        monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    seen = []

    def run_agent(prompt, connector, table, allowed, schemas, history, user,
                  model=None, skill_md=None, kag_first=False, delegation=None,
                  tool_profile="full"):
        seen.append({"source": connector.name, "prompt": prompt,
                     "profile": tool_profile, "delegation": delegation})
        if connector.name == "postgres":
            assert delegation is not None
            queued = delegation.propose(
                "snowflake", "price the returned account ids", "parent_rows",
                "the spend table lives in Snowflake")
            assert queued.startswith("Spawn request queued")
            rows = [[f"acct-{i}", "x" * 200] for i in range(55)]
            return _answer("postgres", rows=rows)
        return _answer("snowflake")

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(task="find candidate accounts"), SOURCES,
                     "root question", USER)

    assert [call["source"] for call in seen] == ["postgres", "snowflake"]
    assert seen[0]["profile"] == "graph_worker"
    assert seen[1]["profile"] == "graph_worker"
    child_prompt = seen[1]["prompt"]
    assert "REFERENCE DATA" in child_prompt
    assert '"acct-0"' in child_prompt and '"acct-19"' in child_prompt
    assert '"acct-20"' not in child_prompt and '"acct-54"' not in child_prompt
    assert "rows (20 of 55)" in child_prompt
    assert "x" * 200 not in child_prompt and "…" in child_prompt
    assert child_prompt.endswith("Question: price the returned account ids")

    assert out["order"] == ["seed", "seed_snowflake"]
    child = out["runtime_plan"]["nodes"][1]
    assert child == {
        "id": "seed_snowflake", "source": "snowflake",
        "task": "price the returned account ids", "depends_on": ["seed"],
        "kind": "agent", "dynamic": True, "spawned_by": "seed", "depth": 1,
        "context_mode": "parent_rows",
        "spawn_reason": "the spend table lives in Snowflake",
    }
    assert out["runtime_plan"]["dynamic"] is True


def test_seed_keeps_chat_history_but_spawned_specialist_gets_a_clean_context(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    histories = []

    def run_agent(prompt, connector, table, allowed, schemas, history, user,
                  model=None, skill_md=None, delegation=None, **kwargs):
        histories.append((connector.name, history))
        if connector.name == "postgres":
            delegation.propose("snowflake", "fresh specialist task", "none", "needed")
        return _answer(connector.name)

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    prior = [{"role": "user", "text": "compare last quarter"}]
    ag.execute(_seed(), SOURCES, "follow up", USER, history=prior)

    assert histories == [("postgres", prior), ("snowflake", [])]


def test_one_worker_can_spawn_two_children_and_server_ids_are_deterministic(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    calls = []

    def run_agent(prompt, connector, *args, delegation=None, tool_profile="full", **kwargs):
        calls.append((connector.name, prompt, tool_profile))
        if connector.name == "postgres":
            # Intentionally reverse lexical source order. The broker, not tool
            # completion order, owns deterministic node allocation.
            delegation.propose("snowflake", "snow task", "none", "snow work")
            delegation.propose("databricks", "dbx task", "none", "dbx work")
        return _answer(connector.name)

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(), SOURCES, "q", USER)

    runtime = out["runtime_plan"]["nodes"]
    assert [n["id"] for n in runtime] == [
        "seed", "seed_databricks", "seed_snowflake"]
    assert out["order"] == ["seed", "seed_databricks", "seed_snowflake"]
    assert {name for name, _, _ in calls[1:]} == {"databricks", "snowflake"}
    assert all(profile == "graph_worker" for _, _, profile in calls[1:])

    parent = _seed()["nodes"][0]
    parent_result = _answer("postgres")
    requests = [
        {"source": "snowflake", "task": "z task", "context": "none", "reason": "z"},
        {"source": "snowflake", "task": "a task", "context": "none", "reason": "a"},
        {"source": "databricks", "task": "m task", "context": "none", "reason": "m"},
    ]
    first, _ = ag.validate_child_requests(
        parent, requests, _seed(), SOURCES, parent_result)
    second, _ = ag.validate_child_requests(
        parent, list(reversed(requests)), _seed(), SOURCES, parent_result)
    assert [(n["id"], n["source"], n["task"]) for n in first] == [
        (n["id"], n["source"], n["task"]) for n in second]
    assert [n["id"] for n in first] == [
        "seed_databricks", "seed_snowflake", "seed_snowflake_2"]


def test_broker_rejects_unauthorized_sources_and_any_request_owned_graph_fields():
    plan = _seed()
    parent = plan["nodes"][0]
    requests = [
        {"source": "payroll_prod", "task": "read salaries", "context": "none",
         "reason": "wanted"},
        {"source": "snowflake", "task": "bypass", "context": "none", "reason": "x",
         "id": "root", "depends_on": [], "depth": -1, "credentials": "secret"},
    ]

    accepted, rejected = ag.validate_child_requests(
        parent, requests, plan, SOURCES, _answer("postgres"))

    assert accepted == []
    assert {r["reason"] for r in rejected} == {
        "source is not accessible", "spawn request contains unsupported fields"}
    # The earlier node-level inbox also refuses a source outside the exact
    # RBAC-filtered roster, so an invalid request never enters its queue.
    inbox = ag.SpawnInbox(["postgres", "snowflake"])
    assert "rejected" in inbox.propose("payroll_prod", "read salaries").lower()
    queued, early_rejections = inbox.drain()
    assert queued == [] and early_rejections[0]["source"] == "payroll_prod"


def test_empty_parent_can_spawn_independent_child_but_not_parent_rows_child(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    seen = []

    def run_agent(prompt, connector, *args, delegation=None, **kwargs):
        seen.append((connector.name, prompt))
        if connector.name == "postgres":
            delegation.propose("snowflake", "independent lookup", "none", "independent")
            delegation.propose("databricks", "lookup those ids", "parent_rows", "needs ids")
            return _answer("postgres", rows=[])
        return _answer(connector.name)

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(), SOURCES, "q", USER)

    assert [source for source, _ in seen] == ["postgres", "snowflake"]
    assert seen[1][1] == "independent lookup"
    assert "REFERENCE DATA" not in seen[1][1]
    assert [n["source"] for n in out["runtime_plan"]["nodes"]] == [
        "postgres", "snowflake"]
    assert any("parent returned no rows" in r["reason"]
               for r in out["runtime_plan"]["spawn_rejections"])


def test_recursive_delegation_stops_at_depth_ceiling(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    inbox_presence = []

    def run_agent(prompt, connector, *args, delegation=None, **kwargs):
        inbox_presence.append((connector.name, delegation is not None))
        if connector.name == "postgres":
            delegation.propose("snowflake", "step one", "parent_rows", "delegate")
        elif connector.name == "snowflake":
            delegation.propose("databricks", "step two", "parent_rows", "delegate again")
        elif connector.name == "databricks":
            assert delegation is None, "a max-depth worker must not receive spawn authority"
        return _answer(connector.name)

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(), SOURCES, "q", USER)

    assert inbox_presence == [
        ("postgres", True), ("snowflake", True), ("databricks", False)]
    nodes = out["runtime_plan"]["nodes"]
    assert [(n["id"], n["depth"]) for n in nodes] == [
        ("seed", 0), ("seed_snowflake", 1),
        ("seed_snowflake_databricks", ag.MAX_SPAWN_DEPTH)]


def test_child_validation_enforces_depth_and_total_node_ceiling():
    request = {"source": "snowflake", "task": "new work", "context": "none",
               "reason": "needed"}
    depth_parent = {**_seed()["nodes"][0], "depth": ag.MAX_SPAWN_DEPTH}
    accepted, rejected = ag.validate_child_requests(
        depth_parent, [request], {"nodes": [depth_parent]}, SOURCES,
        _answer("postgres"))
    assert accepted == []
    assert rejected == [{"reason": f"spawn depth limit ({ag.MAX_SPAWN_DEPTH}) reached"}]

    parent = _seed()["nodes"][0]
    nodes = [parent] + [
        {"id": f"occupied_{i}", "source": "postgres", "task": f"old {i}",
         "depends_on": [], "depth": 0}
        for i in range(ag.MAX_NODES - 2)
    ]
    almost_full = {"nodes": nodes}
    requests = [
        {"source": "snowflake", "task": "a", "context": "none", "reason": "a"},
        {"source": "databricks", "task": "b", "context": "none", "reason": "b"},
    ]
    accepted, rejected = ag.validate_child_requests(
        parent, requests, almost_full, SOURCES, _answer("postgres"))
    assert len(accepted) == 1
    assert len(nodes) + len(accepted) == ag.MAX_NODES
    assert any(r["reason"] == "graph node limit reached" for r in rejected)


def test_oversized_fallback_is_refused_before_any_worker_runs(monkeypatch):
    sources = _roster(*(f"source_{i}" for i in range(ag.MAX_NODES + 1)))
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(ag.agent, "llm_available", lambda *a, **k: False)
    monkeypatch.setattr(
        ag.agent, "run_agent",
        lambda *a, **k: pytest.fail("an oversized fallback executed a worker"))

    with pytest.raises(ag.GraphLimitExceeded):
        ag.flat_plan(sources, "all sources")

    out = orchestrator.run_orchestrated(
        "all sources", USER, [], sources=sources)
    assert out["agents_used"] == []
    assert out["errors"] and f"graph limit is {ag.MAX_NODES}" in out["errors"][0]
    assert next(n for n in out["graph"]["nodes"]
                if n["id"] == "__supervisor__")["status"] == "failed"


def test_failed_parent_request_is_drained_but_never_materialized(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    calls = []

    def run_agent(prompt, connector, *args, delegation=None, **kwargs):
        calls.append(connector.name)
        delegation.propose("snowflake", "must not run", "none", "parent fails")
        raise RuntimeError("warehouse is down")

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(), SOURCES, "q", USER)

    assert calls == ["postgres"]
    assert len(out["runtime_plan"]["nodes"]) == 1
    assert out["results"]["seed"]["_status"] == "failed"
    assert out["results"]["seed"]["_spawned"] == []
    assert any("failed or skipped parents cannot spawn" in r["reason"]
               for r in out["runtime_plan"]["spawn_rejections"])


def test_provider_fallback_cannot_commit_requests_emitted_before_failure(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    calls = []

    def run_agent(prompt, connector, *args, delegation=None, **kwargs):
        calls.append(connector.name)
        delegation.propose("snowflake", "must not run", "none", "turn crashed")
        result = _answer("postgres")
        result["model_error"] = {"detail": "provider disconnected"}
        return result

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(), SOURCES, "q", USER)

    assert calls == ["postgres"]
    assert len(out["runtime_plan"]["nodes"]) == 1
    assert out["results"]["seed"]["_spawned"] == []
    assert any("failed or skipped parents cannot spawn" in r["reason"]
               for r in out["runtime_plan"]["spawn_rejections"])


def test_lost_background_claim_discards_wave_before_topology_or_trace_publish(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    checks = 0
    recorded = []

    def check_claim():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ag.jobs.ClaimLost("reclaimed")

    def run_agent(prompt, connector, *args, delegation=None, **kwargs):
        delegation.propose("snowflake", "must be replayed by new owner", "none")
        return _answer(connector.name)

    monkeypatch.setattr(ag.jobs, "check_claim", check_claim)
    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    monkeypatch.setattr(
        ag.lightning, "record_agent_rollout",
        lambda *a, **k: recorded.append((a, k)))

    with pytest.raises(ag.jobs.ClaimLost):
        ag.execute(_seed(), SOURCES, "q", USER)
    assert recorded == []


def test_same_source_children_are_forced_into_separate_waves(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    seen = []
    active = 0
    max_active = 0
    guard = threading.Lock()

    def run_agent(prompt, connector, *args, delegation=None,
                  tool_profile="full", **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        try:
            seen.append((prompt, tool_profile))
            if prompt == "seed task":
                delegation.propose("postgres", "alpha specialist", "none", "alpha")
                delegation.propose("postgres", "beta specialist", "none", "beta")
            return _answer("postgres")
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)

    class _PoolMustNotBeNeeded:
        def __init__(self, *args, **kwargs):
            pytest.fail("same-connector nodes were put in one parallel wave")

    monkeypatch.setattr(ag.concurrent.futures, "ThreadPoolExecutor", _PoolMustNotBeNeeded)
    out = ag.execute(_seed(), SOURCES, "q", USER)

    assert [prompt for prompt, _ in seen] == [
        "seed task", "alpha specialist", "beta specialist"]
    assert [profile for _, profile in seen] == [
        "graph_worker", "graph_worker", "graph_worker"]
    assert out["order"] == ["seed", "seed_postgres", "seed_postgres_2"]
    assert max_active == 1


def test_runtime_graph_has_distinct_spawn_data_and_result_edges():
    plan = _seed()
    parent = plan["nodes"][0]
    requests = [
        {"source": "snowflake", "task": "needs rows", "context": "parent_rows",
         "reason": "join keys"},
        {"source": "databricks", "task": "independent", "context": "none",
         "reason": "separate metric"},
    ]
    children, rejected = ag.validate_child_requests(
        parent, requests, plan, SOURCES, _answer("postgres"))
    assert rejected == []
    runtime = {**plan, "nodes": plan["nodes"] + children, "dynamic": True}
    results = {node["id"]: {**_answer(node["source"]), "_status": "ok"}
               for node in runtime["nodes"]}

    graph = ag.describe(runtime, results)
    edges = {(e["from"], e["to"], e["kind"]) for e in graph["edges"]}
    assert ("__supervisor__", "seed", "spawn") in edges
    assert ("seed", "seed_snowflake", "spawn") in edges
    assert ("seed", "seed_snowflake", "data") in edges
    assert ("seed", "seed_databricks", "spawn") in edges
    assert ("seed", "seed_databricks", "data") not in edges
    assert ("seed", "__reason__", "result") in edges
    assert ("seed_snowflake", "__reason__", "result") in edges
    assert ("seed_databricks", "__reason__", "result") in edges
    assert {n["kind"] for n in graph["nodes"]} == {
        "supervisor", "agent", "reasoner"}


def test_dynamic_diagnostic_child_cannot_rewrite_table_blend_membership(monkeypatch):
    plan = ag.validate_plan({"nodes": [
        {"id": "left", "source": "postgres", "task": "left"},
        {"id": "right", "source": "snowflake", "task": "right"},
    ], "combine": "table"}, SOURCES, "join them")
    parent = plan["nodes"][0]
    children, rejected = ag.validate_child_requests(parent, [{
        "source": "databricks", "task": "diagnose inventory", "context": "none",
        "reason": "explain an anomaly",
    }], plan, SOURCES, _answer("postgres"))
    assert rejected == [] and len(children) == 1
    runtime = {**plan, "nodes": plan["nodes"] + children, "dynamic": True}
    results = {
        "left": {**_answer("postgres"), "_source": "postgres"},
        "right": {**_answer("snowflake"), "_source": "snowflake"},
        children[0]["id"]: {**_answer("databricks"), "_source": "databricks"},
    }
    captured = {}
    monkeypatch.setattr(
        ag.blend, "blend",
        lambda user, parts: captured.update({"user": user, "parts": parts}) or {
            "columns": [], "rows": [],
        })

    ag.blend_parts(runtime, results, USER)

    assert [part["name"] for part in captured["parts"]] == ["left", "right"]
    graph = ag.describe(runtime, results)
    membership = {n["id"]: n.get("blend_member") for n in graph["nodes"]
                  if n["kind"] == "agent"}
    assert membership == {"left": True, "right": True, children[0]["id"]: False}


def test_one_source_orchestrator_uses_runtime_graph_when_enabled(monkeypatch):
    one_source = SOURCES[:1]
    _enable_frontier_delegation(monkeypatch)
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(ag, "plan_graph",
                        lambda prompt, sources, user, model=None:
                        ag.flat_plan(sources, prompt))

    def run_agent(prompt, connector, *args, **kwargs):
        return _answer(connector.name)

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    monkeypatch.setattr(orchestrator, "_aggregate", lambda *a, **k: "combined")
    aggregate_rollouts = []
    monkeypatch.setattr(
        orchestrator.lightning, "record_agent_rollout",
        lambda *a, **k: aggregate_rollouts.append((a, k)))

    out = orchestrator.run_orchestrated(
        "one source question", USER, [], sources=one_source)

    assert out["mode"] == "orchestrated"
    assert out["text"] == "combined"
    assert out["graph"] is not None
    assert {n["kind"] for n in out["graph"]["nodes"]} == {
        "supervisor", "agent", "reasoner"}
    assert out["agents_used"] == ["postgres"]
    assert aggregate_rollouts and aggregate_rollouts[-1][0][4] == "aggregator"


def test_bitnet_worker_never_receives_delegation_authority(monkeypatch):
    monkeypatch.setenv("STUDIO_AGENT_DYNAMIC_SPAWN", "1")
    monkeypatch.setattr(ag.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(ag.agent, "concrete_model_spec", lambda spec: "openai:bitnet")
    monkeypatch.setattr(ag.agent, "self_hosted", lambda spec: True)
    monkeypatch.setattr(ag.lightning, "record_agent_rollout", lambda *a, **k: None)
    seen = []

    def run_agent(prompt, connector, *args, delegation=None, **kwargs):
        seen.append(delegation)
        return _answer(connector.name)

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(), SOURCES, "q", USER, model="bitnet")

    assert seen == [None]
    assert out["results"]["seed"]["_delegation_capable"] is False
    assert out["runtime_plan"]["dynamic"] is False


def test_bitnet_workers_can_be_selected_by_a_frontier_supervisor(monkeypatch):
    used = []

    class Reply:
        content = ('{"nodes":[{"id":"pg","source":"postgres",'
                   '"task":"query postgres","depends_on":[]}],'
                   '"combine":"reason","why":"one source is relevant"}')

    class LLM:
        def invoke(self, messages):
            return Reply()

    monkeypatch.setattr(ag.agent, "llm_spec", lambda: "anthropic:frontier")
    monkeypatch.setattr(
        ag.agent, "concrete_model_spec",
        lambda spec: "openai:bitnet" if spec == "bitnet" else spec)
    monkeypatch.setattr(
        ag.agent, "self_hosted", lambda spec: spec == "openai:bitnet")
    monkeypatch.setattr(
        ag.agent, "llm_available",
        lambda spec, user: used.append(("available", spec)) or True)
    monkeypatch.setattr(
        ag.agent, "make_llm",
        lambda spec, user, **kwargs: used.append(("make", spec)) or LLM())

    plan = ag.plan_graph("query postgres", SOURCES[:2], USER, model="bitnet")

    assert plan["planned"] is True
    assert [n["source"] for n in plan["nodes"]] == ["postgres"]
    assert ("available", "anthropic:frontier") in used
    assert ("make", "anthropic:frontier") in used


def test_dynamic_spawn_kill_switch_omits_the_capability(monkeypatch):
    _enable_frontier_delegation(monkeypatch)
    monkeypatch.setenv("STUDIO_AGENT_DYNAMIC_SPAWN", "0")
    seen = []

    def run_agent(prompt, connector, *args, delegation=None, **kwargs):
        seen.append(delegation)
        return _answer(connector.name)

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(_seed(), SOURCES, "q", USER)

    assert seen == [None]
    assert out["runtime_plan"]["dynamic"] is False


def test_lightning_persists_server_minted_graph_ancestry(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        ag.lightning.db, "add_trace",
        lambda *args, **kwargs: captured.update(kwargs) or "trace-graph")
    monkeypatch.setattr(ag.lightning, "_enqueue_emit", lambda *a, **k: None)
    graph_meta = {
        "node_id": "seed_snowflake", "spawned_by": "seed", "depth": 1,
        "dynamic": True, "context_mode": "none",
        "spawn_requests": [], "spawned": [], "spawn_rejections": [],
    }

    trace_id = ag.lightning.record_agent_rollout(
        USER, "conversation-1", "root question", "Snowflake agent", "worker",
        {**_answer("snowflake"), "_source": "snowflake"},
        conditioning_prompt="specialist task", graph_meta=graph_meta)

    assert trace_id == "trace-graph"
    assert captured["meta"]["graph"] == {
        "node_id": "seed_snowflake", "spawned_by": "seed", "depth": 1,
        "dynamic": True, "context_mode": "none", "spawned": [],
        "spawn_request_count": 0, "spawn_rejection_count": 0,
    }
    # A runtime parent can inline a warehouse value even with context=none.
    assert captured["meta"]["conditioning_prompt"] == "root question"
    assert captured["meta"]["conditioning_redacted"] == "dynamic_task"
    assert captured["meta"]["global_train_eligible"] is False


def _stub_langchain_tools(monkeypatch):
    try:
        import langchain_core.tools  # noqa: F401
        return
    except ImportError:
        pass
    package = types.ModuleType("langchain_core")
    package.__path__ = []
    tools = types.ModuleType("langchain_core.tools")
    tools.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "langchain_core", package)
    monkeypatch.setitem(sys.modules, "langchain_core.tools", tools)


def _tool_name(tool):
    return getattr(tool, "name", None) or getattr(tool, "__name__", "")


def _invoke_tool(tool, args):
    return tool.invoke(args) if hasattr(tool, "invoke") else tool(**args)


class _Message:
    type = "ai"
    content = "queued"


def test_frontier_agent_exposes_optional_spawn_tool_but_descendant_profile_is_closed(
        monkeypatch):
    """Exercise agent.py's real optional tool, not just SpawnInbox.propose()."""
    from app import freshness

    _stub_langchain_tools(monkeypatch)
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "concrete_model_spec", lambda spec: "openai:gpt-test")
    monkeypatch.setattr(agent, "self_hosted", lambda spec: False)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent.db, "list_memory", lambda *a, **k: [])
    monkeypatch.setattr(agent, "_recent_failure_notes", lambda *a, **k: [])
    # graph_worker must not even ask for arbitrary MCP tools.
    monkeypatch.setattr(agent, "mcp_servers", lambda *a, **k: pytest.fail(
        "descendant profile attempted to load MCP side effects"))
    captured = {}
    monkeypatch.setattr(
        freshness, "for_table",
        lambda user, source, table, connector=None:
        captured.update({"freshness_call": (user, source, table, connector)}) or {
            "table": table, "column": "loaded_at", "kind": "load",
            "latest": "2026-09-21", "rows": 4,
        })

    def fake_graph(llm, tools, system, spec, volatile=None):
        captured["names"] = {_tool_name(tool) for tool in tools}
        captured["volatile"] = volatile
        spawn = next(tool for tool in tools if _tool_name(tool) == "spawn_data_agent")
        freshness_tool = next(
            tool for tool in tools if _tool_name(tool) == "data_freshness")

        class Graph:
            def invoke(self, state, config=None):
                captured["reply"] = _invoke_tool(spawn, {
                    "source": "snowflake", "task": "child from real tool",
                    "context": "none", "reason": "specialist needed",
                })
                captured["freshness_reply"] = _invoke_tool(
                    freshness_tool, {"table": "postgres_orders"})
                return {"messages": [_Message()]}

        return Graph()

    monkeypatch.setattr(agent, "_graph", fake_graph)
    inbox = ag.SpawnInbox(["postgres", "snowflake"])
    out = agent.run_agent(
        "seed", _Conn("postgres"), "*", ["postgres_orders"], {}, [], USER,
        model="openai:gpt-test", delegation=inbox, tool_profile="graph_worker")

    assert captured["names"] == {
        "run_sql", "render_chart", "data_freshness", "spawn_data_agent"}
    assert "remember" not in captured["names"] and "email_report" not in captured["names"]
    assert "RUNTIME DELEGATION" in captured["volatile"]
    assert '"postgres"' in captured["volatile"]
    assert '"snowflake"' in captured["volatile"]
    assert captured["reply"].startswith("Spawn request queued")
    assert captured["freshness_call"][:3] == (
        USER, "postgres", "postgres_orders")
    assert captured["freshness_call"][3].name == "postgres"
    assert "2026-09-21" in captured["freshness_reply"]
    requests, rejected = inbox.drain()
    assert rejected == []
    assert requests == [{
        "source": "snowflake", "task": "child from real tool",
        "context": "none", "reason": "specialist needed",
    }]
    assert out["mode"] == "agent"
