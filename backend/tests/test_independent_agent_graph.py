"""Independent graph formation from one natural-language prompt.

The planning council is deliberately different from runtime worker spawning:
three isolated planners receive the same root question and RBAC-filtered roster,
produce whole candidate graphs, and a deterministic server-side gate selects a
validated candidate.  No planner sees a peer's proposal and proposals are never
spliced together.

Provider calls and warehouses are stubbed.  The concurrency, validation,
election, fallback, and serialized formation graph remain real.
"""
import json
import threading
import time

import pytest

from app import agent_graph as ag, orchestrator, roster


class _Conn:
    def __init__(self, name, dialect="ansi"):
        self.name = name
        self.dialect = dialect


def _roster(*names):
    return [
        {
            "connector": _Conn(name),
            "allowed": [f"{name}_orders", f"{name}_customers"],
            "schemas": {},
            "skill": f"governed skill for {name}",
        }
        for name in names
    ]


SOURCES = _roster("postgres", "snowflake", "databricks")
USER = {"id": "u-planning-council", "role": "analyst", "email": "a@example.test"}
PLANNER_IDS = {
    "source_mapper": "__planner_source_mapper__",
    "dependency_planner": "__planner_dependency_planner__",
    "minimalist": "__planner_minimalist__",
}


def _proposal(*, sources=("postgres", "snowflake"), dependent=True,
              marker="candidate"):
    nodes = []
    for index, source in enumerate(sources):
        nodes.append({
            "id": f"step_{index}",
            "source": source,
            "task": f"{marker}: query {source}",
            "depends_on": ["step_0"] if dependent and index else [],
        })
    return {
        "nodes": nodes,
        "combine": "reason",
        "why": f"{marker} private proposal explanation",
    }


def _message_text(messages):
    chunks = []
    for message in messages:
        if isinstance(message, tuple) and len(message) == 2:
            chunks.append(str(message[1]))
        else:
            chunks.append(str(getattr(message, "content", message)))
    return "\n".join(chunks)


def _planner_role(messages):
    text = _message_text(messages).casefold()
    matches = [role for role in PLANNER_IDS if role in text]
    assert len(matches) == 1, f"planner prompt does not identify one fixed role: {text[:300]}"
    return matches[0]


class _Reply:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, str) else json.dumps(payload)


def _enable_council(monkeypatch, replies, *, count=3, finish_order=None,
                    require_parallel=False):
    """Install isolated fake model instances and return their observations."""
    monkeypatch.setenv("STUDIO_AGENT_GRAPH_PLANNERS", str(count))
    monkeypatch.setattr(ag.agent, "llm_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(ag.agent, "self_hosted", lambda *args, **kwargs: False)
    monkeypatch.setattr(ag.agent, "concrete_model_spec", lambda spec: spec)

    made = []
    calls = {}
    lock = threading.Lock()
    barrier = threading.Barrier(count) if require_parallel and count > 1 else None
    condition = threading.Condition()
    turn = {"index": 0}

    class _LLM:
        def __init__(self, instance):
            self.instance = instance

        def invoke(self, messages):
            role = _planner_role(messages)
            with lock:
                calls[role] = {
                    "messages": messages,
                    "instance": self.instance,
                }
            if barrier is not None:
                # A sequential implementation fails quickly instead of hanging
                # the suite: all independent planners must be in flight here.
                barrier.wait(timeout=3)
            if finish_order is not None:
                with condition:
                    assert set(finish_order) == set(replies)
                    ready = condition.wait_for(
                        lambda: finish_order[turn["index"]] == role, timeout=3)
                    assert ready, f"planner {role} never reached its completion turn"
                    turn["index"] += 1
                    condition.notify_all()
            reply = replies[role]
            if isinstance(reply, BaseException):
                raise reply
            return _Reply(reply)

    def make_llm(spec, user, **kwargs):
        instance = object()
        made.append(instance)
        return _LLM(instance)

    monkeypatch.setattr(ag.agent, "make_llm", make_llm)
    return {"made": made, "calls": calls}


def _plan(monkeypatch, replies, **kwargs):
    observed = _enable_council(monkeypatch, replies, **kwargs)
    plan = ag.plan_graph(
        "Which high-value accounts increased spend?", SOURCES, USER,
        model="openai:test-planner")
    return plan, observed


def test_three_planners_start_independently_with_the_same_prompt_and_roster(monkeypatch):
    replies = {
        role: _proposal(marker=f"output-only-{role}") for role in PLANNER_IDS
    }
    plan, observed = _plan(
        monkeypatch, replies, count=3, require_parallel=True)

    assert set(observed["calls"]) == set(PLANNER_IDS)
    assert len(observed["made"]) == len({id(item) for item in observed["made"]}) == 3
    for role, call in observed["calls"].items():
        text = _message_text(call["messages"])
        assert "Which high-value accounts increased spend?" in text
        for source in SOURCES:
            assert source["connector"].name in text
            for table in source["allowed"]:
                assert table in text
        # A council member gets the root prompt and governed roster, never a
        # peer's output. Its role-specific system instructions are allowed.
        for peer in PLANNER_IDS:
            if peer != role:
                assert f"output-only-{peer}" not in text

    formation = plan["formation"]
    assert formation["mode"] == "independent_election"
    assert {item["role"] for item in formation["planners"]} == set(PLANNER_IDS)
    assert sum(bool(item["selected"]) for item in formation["planners"]) == 1
    assert all(set(item) == {
        "id", "role", "agent", "status", "selected", "nodes", "edges"
    } for item in formation["planners"])
    # Formation metadata is structural. Model-authored tasks and explanations
    # are not retained in the reusable observability object.
    assert "output-only" not in json.dumps(formation, sort_keys=True)


def test_structural_election_is_deterministic_across_completion_orders(monkeypatch):
    replies = {
        # These agree structurally but intentionally differ in model prose.
        "source_mapper": _proposal(marker="mapper-text"),
        "dependency_planner": _proposal(marker="dependency-text"),
        "minimalist": _proposal(
            sources=("postgres", "databricks"), dependent=False,
            marker="minority-text"),
    }
    first, _ = _plan(
        monkeypatch, replies, count=3, require_parallel=True,
        finish_order=["minimalist", "dependency_planner", "source_mapper"])
    first_shape = {
        "nodes": first["nodes"], "combine": first["combine"],
        "selected": first["formation"]["selected"],
        "agreement": first["formation"]["agreement"],
        "planners": first["formation"]["planners"],
    }

    # Replace the fakes and force the exact opposite completion order. Thread
    # timing must not become a topology-selection input.
    second, _ = _plan(
        monkeypatch, replies, count=3, require_parallel=True,
        finish_order=["source_mapper", "dependency_planner", "minimalist"])
    second_shape = {
        "nodes": second["nodes"], "combine": second["combine"],
        "selected": second["formation"]["selected"],
        "agreement": second["formation"]["agreement"],
        "planners": second["formation"]["planners"],
    }

    assert first_shape == second_shape
    assert {node["source"] for node in first["nodes"]} == {
        "postgres", "snowflake"}
    # Election selects one complete topology, but NO planner prose becomes an
    # executable worker task. The server owns the scoped task strings.
    assert all(node["task"].startswith(("Answer the root request", "Use the bounded"))
               for node in first["nodes"])
    assert not any(marker in node["task"] for node in first["nodes"]
                   for marker in ("mapper-text", "dependency-text", "minority-text"))


def test_invalid_unauthorized_and_cyclic_proposals_are_not_election_candidates(monkeypatch):
    replies = {
        "source_mapper": _proposal(marker="safe-choice"),
        "dependency_planner": _proposal(
            sources=("postgres", "payroll_prod"), marker="unauthorized-secret"),
        "minimalist": {
            "nodes": [
                {"id": "a", "source": "postgres", "task": "cycle-a",
                 "depends_on": ["b"]},
                {"id": "b", "source": "snowflake", "task": "cycle-b",
                 "depends_on": ["a"]},
            ],
            "combine": "reason",
            "why": "cyclic-secret",
        },
    }
    plan, _ = _plan(monkeypatch, replies)

    assert plan["planned"] is True
    assert {node["source"] for node in plan["nodes"]} == {
        "postgres", "snowflake"}
    formation = plan["formation"]
    by_role = {item["role"]: item for item in formation["planners"]}
    assert by_role["source_mapper"]["status"] == "valid"
    assert by_role["source_mapper"]["selected"] is True
    assert by_role["dependency_planner"]["status"] == "invalid"
    assert by_role["minimalist"]["status"] == "invalid"
    serialized = json.dumps(formation, sort_keys=True)
    assert "payroll_prod" not in serialized
    assert "unauthorized-secret" not in serialized
    assert "cyclic-secret" not in serialized


def test_one_planner_failure_does_not_erase_two_valid_votes(monkeypatch):
    replies = {
        "source_mapper": _proposal(marker="valid-one"),
        "dependency_planner": RuntimeError("provider-secret-must-not-persist"),
        "minimalist": _proposal(marker="valid-two"),
    }
    plan, _ = _plan(monkeypatch, replies)

    assert plan["planned"] is True
    by_role = {item["role"]: item for item in plan["formation"]["planners"]}
    assert by_role["dependency_planner"]["status"] == "failed"
    assert sum(item["status"] == "valid" for item in by_role.values()) == 2
    assert sum(bool(item["selected"]) for item in by_role.values()) == 1
    assert "provider-secret" not in json.dumps(plan["formation"], sort_keys=True)


def test_no_valid_proposal_falls_back_flat_but_retains_safe_formation_status(monkeypatch):
    replies = {
        "source_mapper": RuntimeError("private provider response"),
        "dependency_planner": "```json\nnot valid json\n```",
        "minimalist": _proposal(
            sources=("payroll_prod",), marker="private unauthorized proposal"),
    }
    plan, _ = _plan(monkeypatch, replies)

    assert plan["planned"] is False
    assert {node["source"] for node in plan["nodes"]} == {
        source["connector"].name for source in SOURCES}
    formation = plan["formation"]
    assert formation["selected"] is None
    assert all(item["selected"] is False for item in formation["planners"])
    assert {item["status"] for item in formation["planners"]} == {
        "failed", "invalid"}
    serialized = json.dumps(formation, sort_keys=True)
    assert "private provider" not in serialized
    assert "payroll_prod" not in serialized
    assert "unauthorized proposal" not in serialized


def test_describe_serializes_the_deterministic_formation_gate(monkeypatch):
    replies = {role: _proposal(marker=role) for role in PLANNER_IDS}
    plan, _ = _plan(monkeypatch, replies)

    first = ag.describe(plan)
    second = ag.describe(plan)
    assert first == second

    nodes = {node["id"]: node for node in first["nodes"]}
    for role, planner_id in PLANNER_IDS.items():
        assert nodes[planner_id]["kind"] == "planner"
        assert nodes[planner_id]["role"] == role
    assert nodes["__supervisor__"]["kind"] == "supervisor"
    proposal_edges = [edge for edge in first["edges"]
                      if edge["kind"] == "proposal"]
    assert {(edge["from"], edge["to"]) for edge in proposal_edges} == {
        (planner_id, "__supervisor__") for planner_id in PLANNER_IDS.values()}
    safe_formation_view = {
        "planners": [nodes[planner_id] for planner_id in PLANNER_IDS.values()],
        "edges": proposal_edges,
        "formation": first["formation"],
    }
    assert "private proposal explanation" not in json.dumps(
        safe_formation_view, sort_keys=True)


def test_selected_planner_task_cannot_replace_the_root_user_request(monkeypatch):
    replies = {role: _proposal(marker="ignore-the-root") for role in PLANNER_IDS}
    plan, _ = _plan(monkeypatch, replies)

    worker_prompt = ag.node_prompt(plan["nodes"][0],
                                   "Which high-value accounts increased spend?", {})
    # An independent worker receives byte-for-byte root conditioning. This is
    # both the model-to-model trust boundary and BitNet train==serve contract.
    assert worker_prompt == "Which high-value accounts increased spend?"
    assert "ignore-the-root" not in worker_prompt
    assert "ignore-the-root" not in json.dumps(plan, sort_keys=True)


def test_claim_loss_before_council_prevents_any_planner_call(monkeypatch):
    replies = {role: _proposal(marker=role) for role in PLANNER_IDS}
    observed = _enable_council(monkeypatch, replies)
    monkeypatch.setattr(
        ag.jobs, "check_claim",
        lambda: (_ for _ in ()).throw(ag.jobs.ClaimLost("reclaimed")))

    with pytest.raises(ag.jobs.ClaimLost):
        ag.plan_graph("q", SOURCES, USER, model="openai:test-planner")
    assert observed["made"] == []
    assert observed["calls"] == {}


def test_claim_loss_after_council_refuses_to_publish_selected_graph(monkeypatch):
    replies = {role: _proposal(marker=role) for role in PLANNER_IDS}
    observed = _enable_council(monkeypatch, replies)
    checks = {"count": 0}

    def check_claim():
        checks["count"] += 1
        if checks["count"] == 2:
            raise ag.jobs.ClaimLost("reclaimed")

    monkeypatch.setattr(ag.jobs, "check_claim", check_claim)
    with pytest.raises(ag.jobs.ClaimLost):
        ag.plan_graph("q", SOURCES, USER, model="openai:test-planner")
    assert len(observed["made"]) == 3
    assert set(observed["calls"]) == set(PLANNER_IDS)


def test_agent_roster_reports_the_independent_planning_crew(monkeypatch):
    monkeypatch.setattr(roster, "all_sources", lambda: [])
    summary = roster.summary(USER)

    planners = summary["graph_planners"]
    assert [item["planner_role"] for item in planners] == list(PLANNER_IDS)
    assert all(item["role"] == "planner" for item in planners)


def test_single_planner_is_still_bounded_by_whole_council_deadline(monkeypatch):
    monkeypatch.setenv("STUDIO_AGENT_GRAPH_PLANNERS", "1")
    monkeypatch.setattr(ag, "planner_timeout_seconds", lambda: 0.05)
    monkeypatch.setattr(ag.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(ag.agent, "self_hosted", lambda *a, **k: False)
    monkeypatch.setattr(ag.agent, "concrete_model_spec", lambda spec: spec)
    release = threading.Event()
    finished = threading.Event()
    seen_kwargs = []

    class SlowLLM:
        def invoke(self, messages):
            release.wait(timeout=2)
            finished.set()
            return _Reply(_proposal(marker="late"))

    def make_llm(spec, user, **kwargs):
        seen_kwargs.append(kwargs)
        return SlowLLM()

    monkeypatch.setattr(ag.agent, "make_llm", make_llm)
    started = time.monotonic()
    try:
        plan = ag.plan_graph("q", SOURCES, USER, model="openai:test")
    finally:
        release.set()
    assert time.monotonic() - started < 0.75
    assert finished.wait(timeout=1)
    assert plan["planned"] is False
    assert plan["formation"]["planners"][0]["status"] == "failed"
    assert seen_kwargs == [{"timeout": 0.05}]


def test_dependency_context_has_total_character_and_column_budgets():
    columns = [f"column_{i}_" + "x" * 100 for i in range(100)]
    rows = [["value" * 100 for _ in columns] for _ in range(100)]
    results = {
        f"parent_{i}": {"_source": "postgres", "text": "answer" * 200,
                        "columns": columns, "rows": rows}
        for i in range(12)
    }
    block = ag.context_block(
        {"depends_on": list(results)}, results)

    # The framing sits outside the payload budget, hence the small allowance.
    assert len(block) <= ag.MAX_CONTEXT_CHARS + 400
    assert "column_29" in block
    assert "column_30" not in block


def test_aggregate_result_conditioning_never_enters_remote_training(monkeypatch):
    captured = {}
    emitted = []
    monkeypatch.setattr(
        ag.lightning.db, "add_trace",
        lambda *args, **kwargs: captured.update(kwargs) or "aggregate-trace")
    monkeypatch.setattr(
        ag.lightning, "_enqueue_emit", lambda trace_id: emitted.append(trace_id))
    sentinel = "TENANT_ROW_SENTINEL_9481"

    trace_id = ag.lightning.record_agent_rollout(
        USER, "conversation", "root question", "Aggregator", "aggregator",
        {"text": "answer", "sql": None, "columns": [], "rows": [],
         "chart": None, "panels": [], "errors": [], "source": "*"},
        conditioning_prompt=f"worker answer={sentinel}; sql=SELECT secret",
        graph_meta={"aggregate": True})

    assert trace_id == "aggregate-trace"
    meta = captured["meta"]
    assert meta["conditioning_prompt"] == "root question"
    assert meta["conditioning_redacted"] == "aggregate_results"
    assert meta["global_train_eligible"] is False
    assert sentinel not in json.dumps(meta, sort_keys=True)
    assert emitted == []


def test_legacy_aggregator_trace_is_blocked_at_remote_emit_boundary(monkeypatch):
    legacy = {
        "id": "legacy-aggregate",
        "meta": {"role": "aggregator",
                 "conditioning_prompt": "old worker answer TENANT_SECRET"},
    }
    assert ag.lightning.global_training_eligible(legacy["meta"]) is False
    monkeypatch.setattr(ag.lightning, "emit_enabled", lambda: True)
    monkeypatch.setattr(ag.lightning, "_trace", lambda trace_id: legacy)
    monkeypatch.setattr(
        ag.lightning, "_schemas",
        lambda: pytest.fail("ineligible legacy aggregate reached remote delivery"))

    out = ag.lightning.emit_trace("legacy-aggregate")
    assert out == {"skipped": "global_training_ineligible",
                   "trace_id": "legacy-aggregate"}


def test_exact_root_council_seed_remains_trainable_for_reuse(monkeypatch):
    captured = {}
    emitted = []
    monkeypatch.setattr(
        ag.lightning.db, "add_trace",
        lambda *args, **kwargs: captured.update(kwargs) or "seed-trace")
    monkeypatch.setattr(
        ag.lightning, "_enqueue_emit", lambda trace_id: emitted.append(trace_id))
    root = "revenue by region"

    ag.lightning.record_agent_rollout(
        USER, "conversation", root, "Postgres agent", "worker",
        {"text": "answer", "sql": "SELECT region, sum(revenue) FROM sales",
         "columns": ["region", "revenue"], "rows": [["west", 1]],
         "chart": None, "panels": [], "errors": [], "_source": "postgres"},
        conditioning_prompt=root,
        graph_meta={"node_id": "pg", "spawned_by": "__supervisor__",
                    "depth": 0, "dynamic": False, "context_mode": "none"})

    assert captured["meta"]["conditioning_prompt"] == root
    assert captured["meta"]["global_train_eligible"] is True
    assert emitted == ["seed-trace"]


def test_bitnet_gets_exact_root_then_failed_worker_escalates_to_frontier(monkeypatch):
    plan = ag.validate_plan({"nodes": [
        {"id": "pg", "source": "postgres", "task": "planner prose",
         "depends_on": []},
    ], "combine": "reason"}, SOURCES, "root question", strict_sources=True)
    seen = []
    monkeypatch.setattr(ag.agent, "llm_spec", lambda: "anthropic:frontier")
    monkeypatch.setattr(
        ag.agent, "concrete_model_spec",
        lambda spec: "openai:bitnet" if spec == "bitnet" else spec)
    monkeypatch.setattr(
        ag.agent, "self_hosted", lambda spec: spec == "openai:bitnet")
    monkeypatch.setattr(ag.agent, "llm_available", lambda *args, **kwargs: True)
    monkeypatch.setattr(ag.lightning, "record_agent_rollout", lambda *a, **k: None)

    def run_agent(prompt, connector, *args, model=None, delegation=None, **kwargs):
        seen.append((prompt, model, delegation))
        if model == "bitnet":
            return {"text": "bitnet failed", "sql": None, "columns": [],
                    "rows": [], "chart": None, "panels": [],
                    "errors": ["adapter miss"]}
        return {"text": "frontier answer", "sql": "SELECT 1", "columns": ["n"],
                "rows": [[1]], "chart": None, "panels": [], "errors": []}

    monkeypatch.setattr(ag.agent, "run_agent", run_agent)
    out = ag.execute(plan, SOURCES, "root question", USER, model="bitnet")

    assert [(prompt, model) for prompt, model, _ in seen] == [
        ("root question", "bitnet"), ("root question", "anthropic:frontier")]
    assert all(delegation is None for _, _, delegation in seen)
    assert out["results"]["pg"]["served_by"] == "frontier"
    assert out["results"]["pg"]["_escalated_from"] == "bitnet"


def test_lost_claim_during_reasoning_cannot_write_aggregator_trace(monkeypatch):
    plan = ag.validate_plan({"nodes": [
        {"id": "pg", "source": "postgres", "task": "q", "depends_on": []},
    ]}, SOURCES, "q")
    result = {"_source": "postgres", "_node": "pg", "_status": "ok",
              "text": "worker answer", "sql": "SELECT 1", "columns": ["n"],
              "rows": [[1]], "chart": None, "panels": [], "errors": []}
    run = {"results": {"pg": result}, "order": ["pg"],
           "runtime_plan": plan, "graph": ag.describe(plan, {"pg": result})}
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "1")
    monkeypatch.setattr(orchestrator.agent_graph, "plan_graph", lambda *a, **k: plan)
    monkeypatch.setattr(orchestrator.agent_graph, "execute", lambda *a, **k: run)
    monkeypatch.setattr(orchestrator, "_aggregate", lambda *a, **k: "terminal answer")
    monkeypatch.setattr(
        orchestrator.lightning, "record_agent_rollout",
        lambda *a, **k: pytest.fail("stale owner wrote an aggregator trace"))
    checks = {"count": 0}

    def check_claim():
        checks["count"] += 1
        if checks["count"] == 4:
            raise ag.jobs.ClaimLost("reclaimed during reasoner")

    monkeypatch.setattr(orchestrator.jobs, "check_claim", check_claim)
    with pytest.raises(ag.jobs.ClaimLost):
        orchestrator.run_orchestrated("q", USER, [], sources=SOURCES[:1])


@pytest.mark.parametrize("configured, expected", [
    ("0", 1), ("1", 1), ("2", 2), ("3", 3), ("999", 3), ("invalid", 3),
])
def test_planner_count_is_clamped_to_the_server_policy(
        monkeypatch, configured, expected):
    replies = {role: _proposal(marker=role) for role in PLANNER_IDS}
    plan, observed = _plan(monkeypatch, replies, count=configured)

    assert len(observed["made"]) == expected
    assert len(observed["calls"]) == expected
    assert len(plan["formation"]["planners"]) == expected
    assert 1 <= len(plan["formation"]["planners"]) <= 3
