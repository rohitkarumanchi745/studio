#!/usr/bin/env python3
"""Read-only post-deploy smoke check for an Airflow 3 cluster used by Studio."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _request(method: str, url: str, *, token: str | None = None, body: dict | None = None) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def smoke(env: dict[str, str] | None = None) -> dict:
    values = os.environ if env is None else env
    base = values.get("AIRFLOW_URL", "").rstrip("/")
    username = values.get("AIRFLOW_USERNAME", "")
    password = values.get("AIRFLOW_PASSWORD", "")
    if not base.startswith(("http://", "https://")) or not username or not password:
        raise RuntimeError("AIRFLOW_URL, AIRFLOW_USERNAME, and AIRFLOW_PASSWORD are required")
    token_doc = _request("POST", f"{base}/auth/token", body={"username": username, "password": password})
    token = token_doc.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Airflow auth endpoint returned no access_token")
    health = _request("GET", f"{base}/api/v2/monitor/health", token=token)
    required = ("metadatabase", "scheduler", "dag_processor")
    unhealthy = [name for name in required if (health.get(name) or {}).get("status") != "healthy"]
    if unhealthy:
        raise RuntimeError(f"Airflow components are not healthy: {', '.join(unhealthy)}")
    result = {"status": "ready", "components": list(required)}
    dag_id = values.get("AIRFLOW_SMOKE_DAG_ID", "").strip()
    if dag_id:
        encoded = urllib.parse.quote(dag_id, safe="")
        dag = _request("GET", f"{base}/api/v2/dags/{encoded}", token=token)
        if dag.get("dag_id") != dag_id or dag.get("is_paused", True) or dag.get("has_import_errors"):
            raise RuntimeError("smoke DAG is missing, paused, or has import errors")
        result["dag_id"] = dag_id
    return result


def main() -> int:
    try:
        print(json.dumps(smoke(), sort_keys=True))
        return 0
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Airflow smoke check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
