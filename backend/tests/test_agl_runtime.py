"""Deployment invariants for Studio's Agent Lightning 1.0.1 runtime."""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import os
import stat
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agl_runtime import RuntimeConfig, SnapshotStore, create_app
from app import recovery_planner


MODEL = "studio-recovery"
ENDPOINT = "http://recovery-model.internal/v1"
KEY = "test-only-lightning-key"


def trace_create(trace_id=None):
    trace_id = trace_id or str(uuid.uuid4())
    return {
        "input": {"data_id": trace_id, "prompt": "revenue by region", "source": "demo",
                  "table": "sales", "conversation_id": "conversation-1",
                  "history": [{"role": "user", "text": "show revenue"}]},
        "is_train": True,
        "config": None,
        "metadata": {"batch_idx": None, "sample_idx_in_batch": None,
                     "studio_trace_id": trace_id, "studio_user_id": "user-1",
                     "studio_role": "analyst", "mode": "agent", "model": "frontier",
                     "agents": ["Data Analyst"], "created_at": 1.0, "run_id": None,
                     "repairs_run_id": None, "execution_status": None, "action": None},
        "rollout_id": f"studio-{trace_id}",
    }


def recovery_create():
    user_id = "recovery-owner"
    request_id = "failed-run-1"
    action = {"type": "sql_pipeline", "steps": [{"name": "Sales", "source": "demo",
              "table": "sales", "sql": "SELECT SUM(revenue) FROM sales"}]}
    task = recovery_planner._task_input(prompt="Summarize revenue", action=action,
        error="Connection reset", history=[], schema={"sales": ["revenue"]}, model=MODEL)
    digest = hashlib.sha256(json.dumps(task, sort_keys=True).encode()).hexdigest()
    rollout_id = "studio-recovery-" + str(uuid.uuid5(
        uuid.NAMESPACE_URL, json.dumps([user_id, request_id])))
    task["recovery_rollout_id"] = rollout_id
    return {
        "input": task,
        "is_train": True,
        "config": {"timeout_seconds": 300, "local": {
            "agent_class": recovery_planner.AGENT_CLASS,
            "env_map": {"STUDIO_RECOVERY_TASK_JSON": "input"}}, "k8s": None},
        "metadata": {"batch_idx": None, "sample_idx_in_batch": None,
                     "mode": "pipeline_recovery", "studio_user_id": user_id,
                     "request_id": request_id, "input_digest": digest},
        "rollout_id": rollout_id,
    }


@pytest.fixture(autouse=True)
def clean_agl_store():
    from agentlightning.server import store

    for value in (store._rollouts, store._events, store._models):
        value.clear()
    store._terminal_order.clear()
    yield
    for value in (store._rollouts, store._events, store._models):
        value.clear()
    store._terminal_order.clear()


@pytest.fixture
def configured(monkeypatch, tmp_path):
    state = tmp_path / "agl" / "state.json"
    monkeypatch.setenv("STUDIO_AGL_RECOVERY_MODEL", MODEL)
    monkeypatch.setenv("STUDIO_AGL_MODEL_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("STUDIO_AGL_STATE_PATH", str(state))
    monkeypatch.setenv("AGL_KEY", KEY)
    monkeypatch.delenv("STUDIO_AGL_TOKEN", raising=False)
    monkeypatch.delenv("STUDIO_AGL_ALLOW_INSECURE", raising=False)
    monkeypatch.delenv("STUDIO_AGL_ALLOW_EPHEMERAL", raising=False)
    return state


@pytest.fixture
def upstream_ready(monkeypatch):
    original = httpx.AsyncClient.get

    async def fake_get(client, url, *args, **kwargs):
        if str(url) == ENDPOINT + "/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": MODEL}]},
                                  request=httpx.Request("GET", str(url)))
        return await original(client, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def test_configuration_fails_closed_without_key_or_durable_state(monkeypatch):
    env = {"STUDIO_AGL_RECOVERY_MODEL": MODEL, "STUDIO_AGL_MODEL_ENDPOINT": ENDPOINT}
    with pytest.raises(RuntimeError, match="AGL_KEY"):
        RuntimeConfig.from_env(env)
    env["AGL_KEY"] = KEY
    with pytest.raises(RuntimeError, match="STATE_PATH"):
        RuntimeConfig.from_env(env)


def test_ephemeral_and_insecure_modes_require_explicit_opt_in():
    config = RuntimeConfig.from_env({
        "STUDIO_AGL_RECOVERY_MODEL": MODEL,
        "STUDIO_AGL_MODEL_ENDPOINT": ENDPOINT,
        "STUDIO_AGL_ALLOW_INSECURE": "1",
        "STUDIO_AGL_ALLOW_EPHEMERAL": "true",
    })
    assert config.ephemeral is True and config.state_path is None and config.key == ""


@pytest.mark.parametrize("endpoint", [
    "https://user:password@example.test/v1",
    "https://example.test/v1?api_key=secret",
    "https://example.test/v1/chat/completions",
])
def test_model_endpoint_cannot_embed_credentials_or_a_leaf_route(endpoint, tmp_path):
    with pytest.raises(RuntimeError):
        RuntimeConfig.from_env({
            "STUDIO_AGL_RECOVERY_MODEL": MODEL,
            "STUDIO_AGL_MODEL_ENDPOINT": endpoint,
            "STUDIO_AGL_STATE_PATH": str(tmp_path / "state.json"),
            "AGL_KEY": KEY,
        })


def test_boot_registers_exact_proxy_model_and_readiness_checks_upstream(
        configured, upstream_ready):
    from agentlightning.server import store

    app = create_app()
    with TestClient(app) as client:
        assert app.state.proxy_router.model_name == MODEL
        registered = store._models[MODEL][ENDPOINT]
        assert registered.model == MODEL and registered.endpoint == ENDPOINT
        assert registered.version == 0
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "durable": True,
                                "model_registered": True,
                                "model_upstream_advertised": True,
                                "completion_verified": False}
        # Recovery does not depend on bridge-specific logprob/token support.
        prepared = app.state.proxy_router.prepare_body({"model": "ignored"}, "train")
        assert prepared["model"] == MODEL and "logprobs" not in prepared


def test_readiness_response_never_discloses_endpoint_or_key(configured, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("contains-sensitive-upstream-detail")

    monkeypatch.setattr(httpx.AsyncClient, "get", unavailable)
    with TestClient(create_app()) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "stage": "model_upstream_unavailable"}
        assert KEY not in response.text and ENDPOINT not in response.text


def test_mutations_are_atomically_persisted_and_restored(configured, upstream_ready):
    headers = {"Authorization": f"Bearer {KEY}"}
    body = trace_create()
    rollout_id = body["rollout_id"]
    app = create_app()
    with TestClient(app) as client:
        created = client.post("/api/rollouts", headers=headers, json=[body])
        assert created.status_code == 201
        event = client.post(f"/api/rollouts/{rollout_id}/attempt/0/events", headers=headers,
                            json={"event_type": "studio.test", "data": {"answer": 1}})
        assert event.status_code == 200

    assert configured.exists()
    assert stat.S_IMODE(configured.stat().st_mode) == 0o600
    document = json.loads(configured.read_text())
    assert document["rollouts"][rollout_id]["input"] == body["input"]
    assert document["events"][rollout_id]["0"][0]["event_type"] == "studio.test"

    from agentlightning.server import store
    store._rollouts.clear()
    store._events.clear()
    store._models.clear()
    with TestClient(create_app()) as client:
        restored = client.get(f"/api/rollouts/{rollout_id}", headers=headers)
        assert restored.status_code == 200
        assert restored.json()["rollout"]["input"] == body["input"]
        assert store._models[MODEL][ENDPOINT].model == MODEL


def test_exact_recovery_agent_contract_is_admitted(configured, upstream_ready):
    body = recovery_create()
    headers = {"Authorization": f"Bearer {KEY}"}
    with TestClient(create_app()) as client:
        response = client.post("/api/rollouts", headers=headers, json=[body])
        assert response.status_code == 201
        stored = response.json()[0]
        assert stored["rollout_id"] == body["rollout_id"]
        assert stored["config"]["local"] == body["config"]["local"]


@pytest.mark.parametrize("mutation", [
    lambda body: body["config"]["local"].update(agent_class="os.system"),
    lambda body: body["config"]["local"].update(env_map={"PATH": "input.error"}),
    lambda body: body["config"].update(k8s={"image": "attacker/image"}),
    lambda body: body["input"].update(model="other-model"),
    lambda body: body["input"].update(extra="untrusted"),
    lambda body: body["metadata"].update(mode="other"),
    lambda body: body["metadata"].update(input_digest="0" * 64),
    lambda body: body.update(rollout_id="studio-recovery-" + str(uuid.uuid4())),
])
def test_executable_rollout_variants_are_rejected_without_persistence(
        configured, upstream_ready, mutation):
    from agentlightning.server import store

    body = copy.deepcopy(recovery_create())
    mutation(body)
    headers = {"Authorization": f"Bearer {KEY}"}
    with TestClient(create_app()) as client:
        response = client.post("/api/rollouts", headers=headers, json=[body])
        assert response.status_code == 403
        assert response.json() == {"detail": "Rollout contract is not permitted"}
        assert body["rollout_id"] not in store._rollouts


@pytest.mark.parametrize("is_train", [True, False])
def test_normal_trace_builder_is_admitted_but_cannot_add_a_runner(
        configured, upstream_ready, is_train):
    from agentlightning.server import store
    from app import lightning

    trace_id = str(uuid.uuid4())
    trace = {"id": trace_id, "prompt": "revenue by region", "source": "demo",
             "tbl": "sales", "conversation_id": "conversation-1", "user_id": "user-1",
             "role": "analyst", "mode": "agent", "model": "frontier", "created_at": 1.0,
             "meta": {"history": [{"role": "user", "text": "show revenue"}],
                      "agents": ["Data Analyst"]}}
    schemas = lightning._schemas()
    body = schemas.RolloutCreate(rollout_id=lightning.rollout_id_for(trace_id),
        input=lightning.rollout_input(trace), is_train=is_train,
        metadata=lightning.rollout_metadata(trace)).model_dump(mode="json")
    assert set(body) == {"input", "is_train", "config", "metadata", "rollout_id"}
    headers = {"Authorization": f"Bearer {KEY}"}
    with TestClient(create_app()) as client:
        assert client.post("/api/rollouts", headers=headers, json=[body]).status_code == 201
        executable = copy.deepcopy(body)
        executable["rollout_id"] = "studio-" + str(uuid.uuid4())
        executable["input"]["data_id"] = executable["rollout_id"].removeprefix("studio-")
        executable["metadata"]["studio_trace_id"] = executable["input"]["data_id"]
        executable["config"] = {"timeout_seconds": 60, "local": {
            "agent_class": "path.to.ArbitraryAgent", "env_map": {"HOME": "input"}},
            "k8s": None}
        response = client.post("/api/rollouts", headers=headers, json=[executable])
        assert response.status_code == 403
        assert executable["rollout_id"] not in store._rollouts


def test_controller_view_excludes_data_only_trace_lifecycle_rows():
    from scripts.run_agent_lightning import _controller_rollouts

    trace = trace_create()
    trace["status"] = {"state": "running"}
    recovery = recovery_create()
    recovery["status"] = {"state": "queuing"}
    assert _controller_rollouts([trace, recovery]) == [recovery]


def test_controller_health_requires_a_fresh_real_rollout_poll(tmp_path, monkeypatch):
    from scripts.run_agent_lightning import (
        _RecoveryOnlyApi, _controller_heartbeat_status,
    )

    heartbeat = tmp_path / "controller-heartbeat.json"
    monkeypatch.setenv("STUDIO_AGL_CONTROLLER_HEARTBEAT_PATH", str(heartbeat))
    assert _controller_heartbeat_status()["stage"] == "controller_not_polled"

    recovery = recovery_create()
    recovery["status"] = {"state": "queuing"}

    class Delegate:
        async def get(self, path, *args, **kwargs):
            assert path == "/api/studio/recovery-rollouts"
            return httpx.Response(200, json=[recovery],
                                  request=httpx.Request(
                                      "GET", "http://lightning/api/studio/recovery-rollouts"))

    response = asyncio.run(_RecoveryOnlyApi(Delegate()).get("/api/rollouts"))
    assert response.json() == [recovery]
    assert stat.S_IMODE(heartbeat.stat().st_mode) == 0o600
    assert _controller_heartbeat_status()["status"] == "ready"

    stale = json.loads(heartbeat.read_text())
    stale["at"] = 0
    heartbeat.write_text(json.dumps(stale))
    os.chmod(heartbeat, 0o600)
    assert _controller_heartbeat_status()["stage"] == "controller_heartbeat_stale"


def test_fifty_inert_rollouts_cannot_hide_recovery_from_controller(
        configured, upstream_ready, tmp_path, monkeypatch):
    from scripts.run_agent_lightning import _RecoveryOnlyApi

    heartbeat = tmp_path / "controller-heartbeat.json"
    monkeypatch.setenv("STUDIO_AGL_CONTROLLER_HEARTBEAT_PATH", str(heartbeat))
    headers = {"Authorization": f"Bearer {KEY}"}
    query = [("state_in", "queuing"), ("state_in", "running"), ("limit", "50")]
    recovery = recovery_create()

    with TestClient(create_app()) as client:
        assert client.get("/api/studio/recovery-rollouts", params=query).status_code == 401
        client.headers.update(headers)
        for _ in range(50):
            assert client.post("/api/rollouts", json=[trace_create()]).status_code == 201
        assert client.post("/api/rollouts", json=[recovery]).status_code == 201

        stock = client.get("/api/rollouts", params=query)
        assert stock.status_code == 200 and len(stock.json()) == 50
        assert recovery["rollout_id"] not in {row["rollout_id"] for row in stock.json()}

        class Delegate:
            async def get(self, path, *args, **kwargs):
                response = client.get(path, params=kwargs.get("params"))
                return httpx.Response(
                    response.status_code, content=response.content,
                    headers=response.headers,
                    request=httpx.Request("GET", "http://lightning" + path))

        response = asyncio.run(_RecoveryOnlyApi(Delegate()).get(
            "/api/rollouts", params=httpx.QueryParams(query)))
        assert response.status_code == 200
        assert [row["rollout_id"] for row in response.json()] == [recovery["rollout_id"]]
        assert heartbeat.exists()


def test_failed_snapshot_prevents_controller_poll_heartbeat(
        configured, upstream_ready, tmp_path, monkeypatch):
    from scripts.run_agent_lightning import _RecoveryOnlyApi

    heartbeat = tmp_path / "controller-heartbeat.json"
    monkeypatch.setenv("STUDIO_AGL_CONTROLLER_HEARTBEAT_PATH", str(heartbeat))
    application = create_app()
    with TestClient(application) as client:
        client.headers.update({"Authorization": f"Bearer {KEY}"})
        application.state.studio_agl_snapshots.error = True

        class Delegate:
            async def get(self, path, *args, **kwargs):
                response = client.get(path, params=kwargs.get("params"))
                return httpx.Response(
                    response.status_code, content=response.content,
                    headers=response.headers,
                    request=httpx.Request("GET", "http://lightning" + path))

        response = asyncio.run(_RecoveryOnlyApi(Delegate()).get(
            "/api/rollouts", params=httpx.QueryParams([
                ("state_in", "queuing"), ("state_in", "running"), ("limit", "50")])))
        assert response.status_code == 503
        assert response.json()["stage"] == "state_persistence_failed"
        assert not heartbeat.exists()


def test_corrupt_or_public_state_is_rejected(configured, monkeypatch):
    configured.parent.mkdir(parents=True)
    configured.write_text("not json")
    os.chmod(configured, 0o600)
    with pytest.raises(RuntimeError, match="corrupt"):
        with TestClient(create_app()):
            pass

    configured.write_text(json.dumps({"version": 1, "rollouts": {}, "events": {},
                                      "terminal_order": []}))
    os.chmod(configured, 0o644)
    with pytest.raises(RuntimeError, match="private regular file"):
        with TestClient(create_app()):
            pass


def test_snapshot_cannot_boot_a_preexisting_arbitrary_python_runner(configured):
    from agentlightning.schemas import (
        Rollout, RolloutConfig, RolloutLifecycleStatus, RolloutLocalConfig,
    )

    rollout_id = "legacy-executable"
    rollout = Rollout(rollout_id=rollout_id, input={"payload": "ignored"},
        config=RolloutConfig(local=RolloutLocalConfig(
            agent_class="attacker.module.Agent", env_map={"PATH": "input"})),
        status=RolloutLifecycleStatus(created_at=1.0, updated_at=1.0))
    configured.parent.mkdir(parents=True)
    configured.write_text(json.dumps({"version": 1,
        "rollouts": {rollout_id: rollout.model_dump(mode="json")},
        "events": {rollout_id: {}}, "terminal_order": []}))
    os.chmod(configured, 0o600)
    with pytest.raises(RuntimeError, match="forbidden executable rollout"):
        with TestClient(create_app()):
            pass


def test_state_lock_rejects_a_second_replica(configured):
    config = RuntimeConfig.from_env()
    first, second = SnapshotStore(config), SnapshotStore(config)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another server replica"):
            second.acquire()
    finally:
        first.release()


def test_http_model_registry_mutations_are_forbidden_and_readiness_stays_green(
        configured, upstream_ready):
    from agentlightning.server import store

    headers = {"Authorization": f"Bearer {KEY}"}
    with TestClient(create_app()) as client:
        deleted = client.delete("/api/models", headers=headers)
        assert deleted.status_code == 403
        registered = client.post("/api/models", headers=headers, json=[{
            "model": "attacker-model", "endpoint": "http://169.254.169.254/latest",
            "version": 0,
        }])
        assert registered.status_code == 403
        assert set(store._models) == {MODEL}
        response = client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"
