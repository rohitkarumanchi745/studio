"""Private OpenAI-compatible bridge for Agent Lightning recovery rollouts.

Agent Lightning 1.0.1 forwards rollout model requests to an OpenAI-compatible
endpoint without forwarding an upstream Authorization header.  This service is
that endpoint.  In ``langchain`` mode it translates the small subset used by
``PipelineRecoveryAgent`` to a hosted provider. In ``passthrough`` mode it
injects a private credential before forwarding the same bounded request to
another OpenAI-compatible gateway. Unknown token-ID extension fields are
preserved when that upstream actually implements them.

It deliberately has no Studio database, warehouse, or execution tools.  Give
its container only the selected provider credential.  Network policy must
allow ingress from the Agent Lightning server only; there is intentionally no
public Service/port in the portable deployment.

Hosted APIs and Studio's pinned bitnet.cpp server do not return the token IDs
required by Agent Lightning's verl trainer. Decisions and observed rewards are
still valid durable trajectories; a future optimizer would need ``passthrough``
plus an independently verified token-ID-capable endpoint. A
mounted trained adapter remains a separate readiness condition: connectivity
alone does not prove that one is loaded.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from functools import partial
from typing import Any
from urllib.parse import urlsplit

import anyio
from fastapi import FastAPI, HTTPException, Request


log = logging.getLogger("studio.recovery_model_gateway")
MAX_REQUEST_BYTES = 160_000
MAX_MESSAGES = 32
MAX_CONTENT = 120_000
MAX_UPSTREAM_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_CONCURRENT_MODEL_CALLS = 4
_model_call_limiter = anyio.CapacityLimiter(MAX_CONCURRENT_MODEL_CALLS)

app = FastAPI(title="Studio recovery model bridge", version="1")


async def _bounded_request_body(request: Request) -> bytes:
    """Read an ASGI body without letting chunked input bypass the size cap."""
    length = request.headers.get("content-length")
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise HTTPException(400, "invalid Content-Length") from None
        if declared < 0:
            raise HTTPException(400, "invalid Content-Length")
        if declared > MAX_REQUEST_BYTES:
            raise HTTPException(413, "request is too large")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "request is too large")
    return bytes(raw)


def _alias() -> str:
    value = (os.getenv("STUDIO_AGL_RECOVERY_MODEL") or "").strip()
    if not value or len(value) > 200:
        raise RuntimeError("STUDIO_AGL_RECOVERY_MODEL is required")
    return value


def _upstream_spec() -> str:
    value = (os.getenv("STUDIO_RECOVERY_UPSTREAM_MODEL") or "").strip()
    if not value or ":" not in value or len(value) > 300:
        raise RuntimeError("STUDIO_RECOVERY_UPSTREAM_MODEL must be a provider:model spec")
    return value


def _provider_ready(spec: str) -> bool:
    provider = spec.split(":", 1)[0].lower()
    if provider == "anthropic":
        return bool(os.getenv("ANTHROPIC_API_KEY"))
    if provider == "openai":
        return bool(os.getenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL") or os.getenv("OPENAI_API_KEY"))
    # Other LangChain providers own their credential/configuration contract.
    return True


def _mode() -> str:
    value = (os.getenv("STUDIO_RECOVERY_GATEWAY_MODE") or "langchain").strip().lower()
    if value not in ("langchain", "passthrough"):
        raise RuntimeError("STUDIO_RECOVERY_GATEWAY_MODE must be langchain or passthrough")
    return value


def _upstream_timeout_seconds() -> int:
    """Leave time inside Lightning's outer recovery deadline to post a decision."""
    raw = (os.getenv("STUDIO_RECOVERY_UPSTREAM_TIMEOUT_S") or "240").strip()
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError("STUDIO_RECOVERY_UPSTREAM_TIMEOUT_S must be an integer") from None
    if not 1 <= value <= 840:
        raise RuntimeError(
            "STUDIO_RECOVERY_UPSTREAM_TIMEOUT_S must be between 1 and 840")
    return value


def _upstream_base_url() -> str:
    value = (os.getenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL") or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username \
            or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("STUDIO_RECOVERY_UPSTREAM_BASE_URL must be a plain HTTP(S) API base URL")
    return value


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _tool_adapter() -> dict[str, Any] | None:
    """Return the operator-pinned adapter identity sent to Studio's gateway."""
    uri = (os.getenv("STUDIO_RECOVERY_TOOL_ADAPTER_URI") or "").strip()
    if not uri:
        if _truthy("STUDIO_RECOVERY_REQUIRE_TOOL_ADAPTER"):
            raise RuntimeError("a trained recovery tool adapter is required")
        return None
    if len(uri) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in uri):
        raise RuntimeError("STUDIO_RECOVERY_TOOL_ADAPTER_URI is invalid")
    parsed = urlsplit(uri)
    if parsed.scheme in {"http", "https"} and (not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError("STUDIO_RECOVERY_TOOL_ADAPTER_URI must be a stable URL without credentials, query, or fragment")
    try:
        version = int((os.getenv("STUDIO_RECOVERY_TOOL_ADAPTER_VERSION") or "1").strip())
    except ValueError:
        raise RuntimeError("STUDIO_RECOVERY_TOOL_ADAPTER_VERSION must be an integer") from None
    if not 1 <= version <= 2**31 - 1:
        raise RuntimeError("STUDIO_RECOVERY_TOOL_ADAPTER_VERSION is out of range")
    sha256 = (os.getenv("STUDIO_RECOVERY_TOOL_ADAPTER_SHA256") or "").strip().lower()
    if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
        raise RuntimeError("STUDIO_RECOVERY_TOOL_ADAPTER_SHA256 must be 64 hexadecimal characters")
    if _truthy("STUDIO_RECOVERY_REQUIRE_TOOL_ADAPTER") and not sha256:
        raise RuntimeError("a pinned recovery tool adapter SHA-256 is required")
    adapter = {"uri": uri, "version": version}
    if sha256:
        adapter["sha256"] = sha256
    return adapter


def _upstream_health_url() -> str:
    base = _upstream_base_url()
    return base[:-3] + "/health" if base.endswith("/v1") else base + "/health"


def readiness() -> tuple[bool, dict[str, Any]]:
    try:
        alias, spec, mode = _alias(), _upstream_spec(), _mode()
        _upstream_timeout_seconds()
        adapter = None
        if mode == "passthrough":
            _upstream_base_url()
            adapter = _tool_adapter()
    except RuntimeError as exc:
        return False, {"status": "not_ready", "detail": str(exc)}
    ready = _provider_ready(spec)
    return ready, {
        "status": "ready" if ready else "not_ready",
        "model": alias,
        "provider": spec.split(":", 1)[0],
        "mode": mode,
        "token_id_fields_preserved": mode == "passthrough",
        "training_ready": False,
        "tool_adapter_configured": adapter is not None,
        "detail": None if ready else "the selected provider credential or endpoint is missing",
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    ready, body = readiness()
    if not ready:
        raise HTTPException(503, body)
    return body


@app.get("/v1/models")
async def models():
    ready, body = readiness()
    if not ready:
        raise HTTPException(503, body)
    if _mode() == "passthrough":
        try:
            upstream = await _upstream_models()
            actual = _upstream_spec().split(":", 1)[1]
            if not any(isinstance(item, dict) and item.get("id") == actual
                       for item in upstream.get("data", [])):
                raise ValueError("configured model is absent")
            await _verify_required_adapter()
        except Exception as exc:
            log.warning("recovery model readiness failed (%s)", type(exc).__name__)
            raise HTTPException(503, "recovery model upstream is unavailable") from None
    return {"object": "list", "data": [{"id": _alias(), "object": "model", "owned_by": "studio"}]}


def _content(value: Any) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if not isinstance(block, dict) or block.get("type") not in ("text", "input_text") \
                    or not isinstance(block.get("text"), str):
                raise ValueError("only text message blocks are supported")
            parts.append(block["text"])
        text = "".join(parts)
    else:
        raise ValueError("message content must be text")
    if len(text) > MAX_CONTENT:
        raise ValueError("message content is too large")
    return text


def _messages(raw: Any) -> list[tuple[str, str]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MESSAGES:
        raise ValueError("messages must be a non-empty bounded list")
    result = []
    total = 0
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in ("system", "user", "assistant"):
            raise ValueError("unsupported message role")
        text = _content(item.get("content"))
        total += len(text)
        if total > MAX_CONTENT:
            raise ValueError("messages are too large")
        role = "human" if item["role"] == "user" else item["role"]
        result.append((role, text))
    return result


def _reply_text(reply: Any) -> str:
    content = getattr(reply, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str):
                parts.append(block.text)
            else:
                raise ValueError("provider returned non-text content")
        return "".join(parts)
    raise ValueError("provider returned invalid content")


def _usage(reply: Any) -> dict[str, int]:
    raw = getattr(reply, "usage_metadata", None) or {}
    prompt = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
    completion = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
    total = int(raw.get("total_tokens") or prompt + completion)
    return {"prompt_tokens": max(0, prompt), "completion_tokens": max(0, completion),
            "total_tokens": max(0, total)}


def _invoke(messages: list[tuple[str, str]], *, temperature: float, max_tokens: int):
    from langchain.chat_models import init_chat_model

    kwargs: dict[str, Any] = {
        "temperature": temperature, "max_tokens": max_tokens,
        "timeout": _upstream_timeout_seconds(),
    }
    base_url = (os.getenv("STUDIO_RECOVERY_UPSTREAM_BASE_URL") or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
        kwargs["api_key"] = (os.getenv("STUDIO_RECOVERY_UPSTREAM_API_KEY") or "studio-private").strip()
    return init_chat_model(_upstream_spec(), **kwargs).invoke(messages)


def _upstream_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = (os.getenv("STUDIO_RECOVERY_UPSTREAM_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


async def _upstream_models() -> dict[str, Any]:
    body = await _bounded_upstream_json(
        "GET", _upstream_base_url() + "/models",
        timeout=min(30, _upstream_timeout_seconds()))
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise ValueError("invalid upstream model list")
    return body


async def _bounded_upstream_json(method: str, url: str, *, json_body: Any = None,
                                 timeout: int) -> Any:
    """Read one upstream JSON response without permitting unbounded buffering."""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        async with client.stream(method, url, headers=_upstream_headers(),
                                 json=json_body) as response:
            response.raise_for_status()
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_UPSTREAM_RESPONSE_BYTES:
                    raise ValueError("upstream response is too large")
    return json.loads(raw or b"{}")


async def _verify_required_adapter() -> None:
    """Verify the exact adapter that the BitNet engine was launched with."""
    expected = _tool_adapter()
    if not _truthy("STUDIO_RECOVERY_REQUIRE_TOOL_ADAPTER"):
        return
    if expected is None:  # pragma: no cover - _tool_adapter already rejects it
        raise ValueError("adapter absent")
    timeout = min(30, _upstream_timeout_seconds())
    body = await _bounded_upstream_json("GET", _upstream_health_url(), timeout=timeout)
    mounted = body.get("mounted_adapter") if isinstance(body, dict) else None
    applied = body.get("applied_adapter") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("ok") is not True \
            or not isinstance(mounted, dict) or not isinstance(applied, dict) \
            or any(mounted.get(key) != value for key, value in expected.items()) \
            or any(applied.get(key) != value for key, value in expected.items()):
        raise ValueError("configured recovery adapter is not confirmed active")


async def _passthrough(body: dict[str, Any]) -> dict[str, Any]:
    """Forward the narrow recovery request while injecting the private key.

    A compatible upstream may return Agent Lightning's optional token-ID
    extension and this bridge will preserve it. Passthrough alone is not proof
    that the selected engine implements those fields or that a trainer exists.
    """
    outgoing = _passthrough_payload(body)
    timeout = _upstream_timeout_seconds()
    result = await _bounded_upstream_json(
        "POST", _upstream_base_url() + "/chat/completions",
        json_body=outgoing, timeout=timeout)
    if not isinstance(result, dict) or not isinstance(result.get("choices"), list) or not result["choices"]:
        raise ValueError("invalid upstream completion")
    # Consumers address the registered alias; never leak the upstream model ID.
    result["model"] = _alias()
    return result


def _passthrough_payload(body: dict[str, Any]) -> dict[str, Any]:
    allowed = {"messages", "stream", "max_tokens", "temperature", "return_token_ids", "logprobs"}
    outgoing = {key: value for key, value in body.items() if key in allowed}
    outgoing["model"] = _upstream_spec().split(":", 1)[1]
    adapter = _tool_adapter()
    if adapter is not None:
        outgoing["studio_adapters"] = {"tool_call": adapter}
    return outgoing


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    raw = await _bounded_request_body(request)
    if not raw:
        raise HTTPException(400, "request is empty")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get("model") != _alias():
            raise ValueError("unknown model")
        if body.get("stream") not in (None, False):
            raise ValueError("streaming is not supported")
        if any(key in body for key in ("tools", "tool_choice", "functions", "function_call")):
            raise ValueError("tools are not supported")
        messages = _messages(body.get("messages"))
        temperature = float(body.get("temperature", 0.2))
        max_tokens = int(body.get("max_tokens", 8192))
        if not 0 <= temperature <= 2 or not 1 <= max_tokens <= 8192:
            raise ValueError("invalid generation bounds")
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(400, "invalid recovery model request") from None
    if not _provider_ready(_upstream_spec()):
        raise HTTPException(503, "recovery model provider is not configured")
    try:
        if _mode() == "passthrough":
            return await _passthrough(body)
        # LangChain clients are synchronous. Calling one directly from this
        # async route would stall health checks and every concurrent rollout.
        reply = await anyio.to_thread.run_sync(
            partial(_invoke, messages, temperature=temperature, max_tokens=max_tokens),
            limiter=_model_call_limiter,
        )
        text = _reply_text(reply)
        if len(text) > MAX_CONTENT:
            raise ValueError("provider response is too large")
    except Exception as exc:
        # Provider exception messages can contain endpoints or request metadata.
        log.warning("recovery model request failed (%s)", type(exc).__name__)
        raise HTTPException(502, "recovery model request failed") from None
    created = int(time.time())
    return {
        "id": "chatcmpl-studio-" + uuid.uuid4().hex,
        "object": "chat.completion",
        "created": created,
        "model": _alias(),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": _usage(reply),
    }
