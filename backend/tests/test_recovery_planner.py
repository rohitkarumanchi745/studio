"""Real Lightning rollout routes/controller entry point; only the model is fake."""
import contextlib
import copy
import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import lightning, recovery_planner as planner

USER = {"id": "recovery-owner", "role": "analyst"}
ACTION = {"type": "sql_pipeline", "steps": [{"name": "Sales", "source": "postgres", "table": "sales", "sql": "SELECT SUM(revenue) FROM sales"}]}


@pytest.fixture
def server(monkeypatch):
    from agentlightning.server import store
    from agentlightning.server.routes import events, rollouts
    store._events.clear()
    store._rollouts.clear()
    store._terminal_order.clear()
    app = FastAPI()
    app.include_router(rollouts.router, prefix="/api")
    app.include_router(events.router, prefix="/api")
    requests = []
    reply = {"decision": "retry", "reason": "Observed transient connection reset."}

    @app.post("/proxy/rollout/{rid}/attempt/{aid}/mode/train/openai/v1/chat/completions")
    async def model(rid: str, aid: str, request: Request):
        requests.append(await request.json())
        return {"choices": [{"message": {"content": json.dumps(reply)}}]}

    with TestClient(app) as client:
        monkeypatch.setenv("STUDIO_AGL_URL", "http://testserver")
        monkeypatch.setenv("STUDIO_AGL_RECOVERY_MODEL", "registered-recovery-model")
        monkeypatch.setattr(lightning, "_client", lambda: contextlib.nullcontext(client))
        monkeypatch.setattr(planner, "_proxy_client", lambda: contextlib.nullcontext(client))
        yield client, requests, reply
    store._events.clear()
    store._rollouts.clear()
    store._terminal_order.clear()


def diagnose(**kw):
    return planner.diagnose(USER, prompt=kw.pop("prompt", "Summarize revenue"), action=kw.pop("action", ACTION),
                            error=kw.pop("error", "Connection reset"), request_id=kw.pop("request_id", "failure-1"), **kw)


def run_agent(server, monkeypatch, rid):
    from agentlightning.controller.local_reconciler import _build_env_from_map, _run_local_reconciler_worker
    client, _, _ = server
    row = client.get(f"/api/rollouts/{rid}").json()["rollout"]
    assert row["config"]["local"]["agent_class"] == planner.AGENT_CLASS
    for key, value in _build_env_from_map(row["input"], row["config"]["local"]["env_map"]).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGL_OPENAI_BASE_URL", f"http://testserver/proxy/rollout/{rid}/attempt/0/mode/train/openai/v1")
    monkeypatch.setenv("AGL_EVENT_URL", f"http://testserver/api/rollouts/{rid}/attempt/0/events")
    monkeypatch.setenv("AGL_KEY", "test-key")
    assert client.patch(f"/api/rollouts/{rid}", json={"status": {"state": "running", "last_attempt_id": "0"}}).status_code == 200
    assert _run_local_reconciler_worker(planner.AGENT_CLASS) == 0
    assert client.patch(f"/api/rollouts/{rid}", json={"status": {"state": "succeeded"}}).status_code == 200


def test_controller_entrypoint_produces_decision_without_execution_reward(server, monkeypatch):
    client, requests, _ = server
    first = diagnose()
    assert first["decision"] == "pending" and not requests
    assert diagnose() == first
    rid = first["rollout_id"]
    run_agent(server, monkeypatch, rid)
    decision = diagnose()
    assert decision["decision"] == "retry" and decision["rollout_id"] == rid
    assert len(requests) == 1 and requests[0]["model"] == "registered-recovery-model"
    assert json.loads(requests[0]["messages"][1]["content"])["action"] == ACTION
    stored = client.get(f"/api/rollouts/{rid}/events").json()
    assert [event["event_type"] for event in stored] == [planner.DECISION_EVENT]
    assert len(client.get("/api/rollouts?state_in=succeeded").json()) == 1


@pytest.mark.parametrize("status,reward", [("failed", 0), ("success", 1)])
def test_physical_outcome_rewards_same_decision_once(server, monkeypatch, status, reward):
    client, _, _ = server
    rid = diagnose()["rollout_id"]
    run_agent(server, monkeypatch, rid)
    for _ in range(2):
        assert planner.record_outcome(rid, run_id="child-run", status=status)["reward"] == reward
    events = client.get(f"/api/rollouts/{rid}/events").json()
    assert len(events) == 3
    assert next(e["data"]["value"] for e in events if e["event_type"] == "reward") == reward
    with pytest.raises(ValueError, match="cannot change"):
        planner.record_outcome(rid, run_id="child-run", status="success" if status == "failed" else "failed")


def test_repair_contains_typed_action_not_local_second_model(server, monkeypatch):
    repaired = copy.deepcopy(ACTION)
    repaired["steps"][0]["sql"] += " LIMIT 10"
    server[2].update(decision="repair", action=repaired)
    rid = diagnose()["rollout_id"]
    run_agent(server, monkeypatch, rid)
    assert diagnose()["action"] == repaired


def test_configuration_missing_never_uses_local_model(server, monkeypatch):
    monkeypatch.delenv("STUDIO_AGL_RECOVERY_MODEL")
    assert diagnose()["decision"] == "escalate" and not server[1]


def test_request_identity_cannot_adopt_a_different_rollout_input(server):
    diagnose()
    result = diagnose(prompt="Different business goal")
    assert result["decision"] == "escalate" and "does not match" in result["reason"]


def test_controller_absent_times_out_without_execution(server):
    from agentlightning.server import store
    rid = diagnose()["rollout_id"]
    store._rollouts[rid].status.created_at = time.time() - 1000
    assert diagnose()["decision"] == "escalate" and not server[1]


@pytest.mark.parametrize("reply", [{}, {"decision": "execute", "reason": "go"},
    {"decision": "retry", "reason": "", "action": ACTION}, {"decision": "repair", "reason": "go"},
    {"decision": "retry", "reason": "go", "sql": "DROP TABLE sales"}])
def test_invalid_agent_output_escalates(server, monkeypatch, reply):
    server[2].clear()
    server[2].update(reply)
    rid = diagnose()["rollout_id"]
    run_agent(server, monkeypatch, rid)
    assert diagnose()["decision"] == "escalate"


def test_worker_refuses_non_rollout_proxy(server, monkeypatch):
    rid = diagnose()["rollout_id"]
    task = server[0].get(f"/api/rollouts/{rid}").json()["rollout"]["input"]
    monkeypatch.setenv("STUDIO_RECOVERY_TASK_JSON", json.dumps(task))
    monkeypatch.setenv("AGL_OPENAI_BASE_URL", "https://external.example/v1")
    monkeypatch.setenv("AGL_EVENT_URL", "https://external.example/events")
    with pytest.raises(RuntimeError, match="configuration"):
        planner.PipelineRecoveryAgent().run()
    assert not server[1]


def test_credentials_are_redacted_in_diagnostic_transport(server):
    rid = diagnose(error="password=hunter2 Bearer abc123 https://user:pass@host api_key=key123")["rollout_id"]
    payload = json.dumps(server[0].get(f"/api/rollouts/{rid}").json()["rollout"]["input"])
    assert all(value not in payload for value in ("hunter2", "abc123", "user:pass", "key123"))


@pytest.mark.parametrize("kwargs", [{"prompt": ""}, {"error": ""}, {"action": {}}, {"history": "untrusted"}])
def test_invalid_diagnostics_do_not_enqueue(server, kwargs):
    assert diagnose(**kwargs)["decision"] == "escalate"
    assert server[0].get("/api/rollouts?state_in=queuing").json() == []
