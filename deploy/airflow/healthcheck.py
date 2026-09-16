#!/usr/bin/env python3
"""Role-specific Docker/Kubernetes health check for the Airflow 3 image."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request


JOB_TYPES = {
    "scheduler": "SchedulerJob",
    "dag-processor": "DagProcessorJob",
    "triggerer": "TriggererJob",
}


def _timeout(env: dict[str, str]) -> int:
    try:
        value = int(env.get("AIRFLOW_HEALTH_TIMEOUT_SECONDS", "8"))
    except ValueError as exc:
        raise RuntimeError("AIRFLOW_HEALTH_TIMEOUT_SECONDS must be an integer") from exc
    if not 1 <= value <= 30:
        raise RuntimeError("AIRFLOW_HEALTH_TIMEOUT_SECONDS must be between 1 and 30")
    return value


def api_health(env: dict[str, str], *, opener=urllib.request.urlopen) -> None:
    port = env.get("AIRFLOW__API__PORT") or env.get("PORT") or "8080"
    url = env.get("AIRFLOW_HEALTH_URL") or f"http://127.0.0.1:{port}/api/v2/monitor/health"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with opener(request, timeout=_timeout(env)) as response:
        if getattr(response, "status", 200) != 200:
            raise RuntimeError(f"Airflow health endpoint returned HTTP {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or (payload.get("metadatabase") or {}).get("status") != "healthy":
        raise RuntimeError("Airflow metadata database is not healthy")


def job_health(role: str, env: dict[str, str], *, runner=subprocess.run) -> None:
    runner(
        (
            "airflow",
            "jobs",
            "check",
            "--job-type",
            JOB_TYPES[role],
            "--hostname",
            socket.gethostname(),
        ),
        env=env,
        timeout=_timeout(env),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def check(env: dict[str, str] | None = None) -> None:
    values = dict(os.environ if env is None else env)
    role = values.get("AIRFLOW_ROLE", "").strip()
    if role == "api-server":
        api_health(values)
    elif role in JOB_TYPES:
        job_health(role, values)
    elif role == "init":
        # A one-shot init container normally exits before health checks begin.
        subprocess.run(("airflow", "db", "check"), env=values, timeout=_timeout(values), check=True)
    else:
        raise RuntimeError("AIRFLOW_ROLE does not identify a health-checkable component")


def main() -> int:
    try:
        check()
        return 0
    except Exception as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
