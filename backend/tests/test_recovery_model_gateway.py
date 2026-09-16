from types import SimpleNamespace
import sys
import types

import anyio
import httpx
import json
import pytest
from fastapi.testclient import TestClient

from app import recovery_model_gateway as gateway


def _configured(monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_RECOVERY_MODEL", "studio-recovery")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-secret")


def test_readiness_fails_closed_without_model(monkeypatch):
    monkeypatch.delenv("STUDIO_AGL_RECOVERY_MODEL", raising=False)
    monkeypatch.delenv("STUDIO_RECOVERY_UPSTREAM_MODEL", raising=False)
    response = TestClient(gateway.app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "not_ready"


def test_model_list_exposes_only_lightning_alias(monkeypatch):
    _configured(monkeypatch)
    response = TestClient(gateway.app).get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"] == [
        {"id": "studio-recovery", "object": "model", "owned_by": "studio"}
    ]


def test_chat_bridge_returns_openai_shape_and_ignores_agl_training_extras(monkeypatch):
    _configured(monkeypatch)
    seen = {}

    def invoke(messages, **kwargs):
        seen.update(messages=messages, kwargs=kwargs)
        return SimpleNamespace(content='{"decision":"retry","reason":"temporary"}',
                               usage_metadata={"input_tokens": 11, "output_tokens": 7})

    monkeypatch.setattr(gateway, "_invoke", invoke)
    response = TestClient(gateway.app).post("/v1/chat/completions", json={
        "model": "studio-recovery",
        "messages": [{"role": "system", "content": "diagnose"},
                     {"role": "user", "content": "failure"}],
        "stream": False,
        "temperature": 1,
        "max_tokens": 200,
        "return_token_ids": True,
        "logprobs": True,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"].startswith("{")
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert seen == {"messages": [("system", "diagnose"), ("human", "failure")],
                    "kwargs": {"temperature": 1.0, "max_tokens": 200}}


def test_langchain_upstream_timeout_is_inside_recovery_deadline(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_TIMEOUT_S", "240")
    seen = {}

    class Model:
        def invoke(self, messages):
            seen["messages"] = messages
            return SimpleNamespace(content="{}")

    package = types.ModuleType("langchain")
    chat_models = types.ModuleType("langchain.chat_models")
    chat_models.init_chat_model = (
        lambda spec, **kwargs: seen.update(spec=spec, kwargs=kwargs) or Model())
    package.chat_models = chat_models
    monkeypatch.setitem(sys.modules, "langchain", package)
    monkeypatch.setitem(sys.modules, "langchain.chat_models", chat_models)
    gateway._invoke([("human", "failure")], temperature=0.2, max_tokens=100)
    assert seen["kwargs"]["timeout"] == 240


@pytest.mark.parametrize("value", ["0", "841", "forever"])
def test_invalid_upstream_timeout_fails_readiness(monkeypatch, value):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_TIMEOUT_S", value)
    assert TestClient(gateway.app).get("/readyz").status_code == 503


def test_chat_bridge_rejects_wrong_model_tools_and_oversized_input(monkeypatch):
    _configured(monkeypatch)
    client = TestClient(gateway.app)
    base = {"model": "wrong", "messages": [{"role": "user", "content": "x"}]}
    assert client.post("/v1/chat/completions", json=base).status_code == 400
    base["model"] = "studio-recovery"
    base["tools"] = []
    assert client.post("/v1/chat/completions", json=base).status_code == 400
    assert client.post("/v1/chat/completions", content=b"x" * (gateway.MAX_REQUEST_BYTES + 1)).status_code == 413


def test_provider_failures_do_not_leak_exception_text(monkeypatch):
    _configured(monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("Bearer secret-that-must-not-leak")

    monkeypatch.setattr(gateway, "_invoke", fail)
    response = TestClient(gateway.app).post("/v1/chat/completions", json={
        "model": "studio-recovery", "messages": [{"role": "user", "content": "x"}]
    })
    assert response.status_code == 502
    assert "secret-that-must-not-leak" not in response.text


def test_passthrough_preserves_training_metadata_and_rewrites_alias(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_GATEWAY_MODE", "passthrough")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL", "http://bitnet:9000/v1")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_MODEL", "openai:bitnet")
    seen = {}

    async def passthrough(body):
        seen.update(body)
        return {"model": "studio-recovery", "choices": [{
            "index": 0, "message": {"role": "assistant", "content": "{}"},
            "token_ids": [4, 5], "logprobs": {"content": [{"logprob": -0.1}]},
            "finish_reason": "stop",
        }], "prompt_token_ids": [1, 2, 3]}

    monkeypatch.setattr(gateway, "_passthrough", passthrough)
    response = TestClient(gateway.app).post("/v1/chat/completions", json={
        "model": "studio-recovery", "messages": [{"role": "user", "content": "x"}],
        "return_token_ids": True, "logprobs": True,
    })
    assert response.status_code == 200
    assert response.json()["prompt_token_ids"] == [1, 2, 3]
    assert response.json()["choices"][0]["token_ids"] == [4, 5]
    assert seen["return_token_ids"] is True


def test_passthrough_requires_plain_upstream_url(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_GATEWAY_MODE", "passthrough")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL", "https://user:secret@example.test/v1")
    response = TestClient(gateway.app).get("/readyz")
    assert response.status_code == 503
    assert "secret" not in response.text


def test_passthrough_injects_exact_trained_adapter_identity(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_GATEWAY_MODE", "passthrough")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL", "http://bitnet:9000/v1")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_MODEL", "openai:bitnet")
    monkeypatch.setenv("STUDIO_RECOVERY_REQUIRE_TOOL_ADAPTER", "1")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_URI", "https://models.example/tool_call.gguf")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_VERSION", "7")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_SHA256", "a" * 64)
    outgoing = gateway._passthrough_payload({
        "model": "studio-recovery", "messages": [{"role": "user", "content": "x"}],
        "return_token_ids": True, "untrusted_extra": "drop-me",
    })
    assert outgoing["model"] == "bitnet"
    assert outgoing["studio_adapters"] == {"tool_call": {
        "uri": "https://models.example/tool_call.gguf", "version": 7,
        "sha256": "a" * 64,
    }}
    assert outgoing["return_token_ids"] is True
    assert "untrusted_extra" not in outgoing


def test_required_trained_adapter_fails_closed_when_absent(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_GATEWAY_MODE", "passthrough")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL", "http://bitnet:9000/v1")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_MODEL", "openai:bitnet")
    monkeypatch.setenv("STUDIO_RECOVERY_REQUIRE_TOOL_ADAPTER", "1")
    monkeypatch.delenv("STUDIO_RECOVERY_TOOL_ADAPTER_URI", raising=False)
    response = TestClient(gateway.app).get("/readyz")
    assert response.status_code == 503
    assert "trained recovery tool adapter is required" in response.text


def test_required_trained_adapter_fails_closed_without_digest(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_GATEWAY_MODE", "passthrough")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL", "http://bitnet:9000/v1")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_MODEL", "openai:bitnet")
    monkeypatch.setenv("STUDIO_RECOVERY_REQUIRE_TOOL_ADAPTER", "1")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_URI",
                       "https://models.example/tool_call.gguf")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_VERSION", "7")
    monkeypatch.delenv("STUDIO_RECOVERY_TOOL_ADAPTER_SHA256", raising=False)
    response = TestClient(gateway.app).get("/readyz")
    assert response.status_code == 503
    assert "SHA-256" in response.text


def test_required_adapter_verification_needs_mounted_and_applied_identity(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setenv("STUDIO_RECOVERY_GATEWAY_MODE", "passthrough")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL", "http://bitnet:9000/v1")
    monkeypatch.setenv("STUDIO_RECOVERY_UPSTREAM_MODEL", "openai:bitnet")
    monkeypatch.setenv("STUDIO_RECOVERY_REQUIRE_TOOL_ADAPTER", "1")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_URI", "https://models.example/tool.gguf")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_VERSION", "7")
    monkeypatch.setenv("STUDIO_RECOVERY_TOOL_ADAPTER_SHA256", "a" * 64)
    expected = {"uri": "https://models.example/tool.gguf", "version": 7,
                "sha256": "a" * 64}
    health = {"ok": True, "mounted_adapter": expected,
              "applied_adapter": {**expected, "sha256": "b" * 64}}

    class Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield json.dumps(health).encode()

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    with pytest.raises(ValueError, match="confirmed active"):
        anyio.run(gateway._verify_required_adapter)

    health["applied_adapter"] = expected
    anyio.run(gateway._verify_required_adapter)
