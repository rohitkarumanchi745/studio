#!/usr/bin/env python3
"""Generate and validate one harmless recovery decision through the model bridge.

Run this inside the Lightning server/controller network before enabling Studio's
automatic recovery.  It proves more than `/readyz`: the configured model must
complete the same bounded JSON contract used by PipelineRecoveryAgent.  No SQL
is executed and no database credential is available to this process.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _base_url() -> str:
    value = _required("STUDIO_AGL_MODEL_ENDPOINT").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username \
            or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("STUDIO_AGL_MODEL_ENDPOINT must be a plain HTTP(S) URL")
    return value


def _request(method: str, path: str, body: dict | None = None) -> dict:
    try:
        timeout = max(1, min(900, int(os.getenv("STUDIO_RECOVERY_SMOKE_TIMEOUT_S") or "300")))
    except ValueError:
        raise RuntimeError("STUDIO_RECOVERY_SMOKE_TIMEOUT_S must be an integer") from None
    data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
    request = urllib.request.Request(_base_url() + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
    except (OSError, urllib.error.HTTPError):
        # Upstream bodies and URLs may include deployment details. Keep the
        # operator-facing failure deliberately generic.
        raise RuntimeError("recovery model smoke request failed") from None
    if len(raw) > 1_000_000:
        raise RuntimeError("recovery model smoke response was too large")
    try:
        result = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("recovery model smoke response was invalid") from None
    if not isinstance(result, dict):
        raise RuntimeError("recovery model smoke response was invalid")
    return result


def validate_completion(body: dict, model: str, *, require_token_ids: bool = False) -> dict:
    try:
        if body.get("model") != model:
            raise ValueError
        choice = body["choices"][0]
        message = choice["message"]
        if message.get("tool_calls") or message.get("function_call"):
            raise ValueError
        from app.recovery_planner import _decision
        decision = _decision(message["content"])
        if require_token_ids and (not isinstance(body.get("prompt_token_ids"), list)
                or not isinstance(choice.get("token_ids"), list)):
            raise ValueError
    except (IndexError, KeyError, TypeError, ValueError):
        raise RuntimeError("recovery model did not satisfy the safe decision contract") from None
    return decision


def run() -> dict:
    from app.recovery_planner import _SYSTEM

    model = _required("STUDIO_AGL_RECOVERY_MODEL")
    models = _request("GET", "/models")
    if not any(isinstance(item, dict) and item.get("id") == model
               for item in models.get("data", [])):
        raise RuntimeError("recovery model is not advertised by the bridge")
    diagnostic = {
        "model": model,
        "objective": "Summarize authorized sales data",
        "action": {"type": "sql_pipeline", "steps": [{
            "name": "summary", "source": "postgres", "table": "sales",
            "sql": "SELECT missing_column FROM sales",
        }]},
        "error": "column missing_column does not exist",
        "authorized_schema": {},
        "history": [],
    }
    require_ids = (os.getenv("STUDIO_RECOVERY_SMOKE_REQUIRE_TOKEN_IDS") or "").strip().lower() \
        in {"1", "true", "yes", "on"}
    request_body = {
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": json.dumps(diagnostic, separators=(",", ":"))}],
        "stream": False,
        "temperature": 0,
        "max_tokens": 8192,
        "logprobs": False,
    }
    if require_ids:
        request_body["return_token_ids"] = True
    completion = _request("POST", "/chat/completions", request_body)
    decision = validate_completion(completion, model, require_token_ids=require_ids)
    return {"status": "ready", "model": model, "completion_verified": True,
            "decision": decision["decision"],
            "token_ids_verified": require_ids}


def main() -> int:
    try:
        print(json.dumps(run(), separators=(",", ":")), flush=True)
        return 0
    except RuntimeError as exc:
        print(json.dumps({"status": "not_ready", "detail": str(exc)}, separators=(",", ":")),
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
