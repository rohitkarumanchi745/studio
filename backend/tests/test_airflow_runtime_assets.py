"""Portable Airflow image tests; no Docker daemon or Airflow install required."""

import base64
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "deploy" / "airflow"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ASSETS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


runtime = _load("runtime")
healthcheck = _load("healthcheck")
smoke = _load("smoke")


def _env(tmp_path, role="scheduler"):
    marker = tmp_path / runtime.SHARED_SENTINEL
    marker.write_text(runtime.SHARED_SENTINEL_CONTENT)
    return {
        "AIRFLOW_ROLE": role,
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "postgresql+psycopg://airflow:secret@db/airflow",
        "AIRFLOW__CORE__FERNET_KEY": base64.urlsafe_b64encode(b"f" * 32).decode(),
        "AIRFLOW__API__SECRET_KEY": "a" * 32,
        "AIRFLOW__API_AUTH__JWT_SECRET": "j" * 32,
        "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": "http://airflow-api:8080/execution/",
        "AIRFLOW__CORE__DAGS_FOLDER": str(tmp_path),
        "STUDIO_AIRFLOW_REQUIRE_SHARED_DAGS_SENTINEL": "1",
    }


def test_image_and_providers_are_release_pinned():
    dockerfile = (ASSETS / "Dockerfile").read_text()
    requirements = (ASSETS / "requirements.txt").read_text().splitlines()
    assert "ARG AIRFLOW_VERSION=3.3.1" in dockerfile
    assert "apache/airflow:slim-${AIRFLOW_VERSION}-python3.12" in dockerfile
    assert '"apache-airflow==${AIRFLOW_VERSION}"' in dockerfile
    assert "${HOME}/constraints.txt" in dockerfile
    assert "apache-airflow-providers-common-sql==2.1.0" in requirements
    assert "apache-airflow-providers-postgres==7.0.1" in requirements
    assert "apache-airflow-providers-fab==3.8.0" in requirements
    assert "AIRFLOW__CORE__EXECUTOR=LocalExecutor" in dockerfile
    assert "install -d -o airflow -g root -m 0770 /opt/airflow/dags" in dockerfile
    # Airflow's DAG processor heartbeat can legitimately be 30-60s old; the
    # upstream-recommended 120s threshold prevents a permanently red probe.
    assert "AIRFLOW__DAG_PROCESSOR__HEALTH_CHECK_THRESHOLD=120" in dockerfile
    assert 'CMD ["python", "/opt/studio-airflow/runtime.py", "run"]' in dockerfile
    example = (ASSETS / "runtime.env.example").read_text()
    assert "_AIRFLOW_DB_MIGRATE=true" in example
    assert "_AIRFLOW_WWW_USER_CREATE=true" in example


def test_portable_compose_prepares_shared_volume_and_probes_dag_processor():
    compose = (ROOT / "deploy" / "portable" / "compose.yaml").read_text()
    assert "airflow-volume-init:" in compose
    assert "image: alpine:3.22.5" in compose
    assert 'user: "0:0"' in compose
    assert "chown 50000:0 /opt/airflow/dags" in compose
    assert "chmod 0770 /opt/airflow/dags" in compose
    assert "airflow-volume-init:\n        condition: service_completed_successfully" in compose
    dag_processor = compose.split("  airflow-dag-processor:", 1)[1].split("\n  recovery-model:", 1)[0]
    assert 'test: ["CMD", "python", "/opt/studio-airflow/healthcheck.py"]' in dag_processor
    worker = compose.split("  studio-worker:", 1)[1]
    assert "airflow-scheduler:\n        condition: service_healthy" in worker
    assert "airflow-dag-processor:\n        condition: service_healthy" in worker
    assert "STUDIO_WORKER_CLAIM_GATE_URL: http://lightning-controller:8082/readyz" in compose


def test_portable_build_context_excludes_generated_secret_files():
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
    assert "**/.env" in dockerignore
    assert "deploy/portable/.env" not in [
        path.as_posix() for path in ROOT.glob("deploy/portable/.env")
        if not any(pattern == "**/.env" for pattern in dockerignore)
    ]


def test_compose_separates_warehouse_admin_reader_and_pipeline_writer():
    compose = (ROOT / "deploy" / "portable" / "compose.yaml").read_text()
    assert "POSTGRES_DSN: postgresql://${WAREHOUSE_READER_USER:-warehouse_reader}:${WAREHOUSE_READER_PASSWORD:" in compose
    assert "postgresql://${WAREHOUSE_PIPELINE_WRITER_USER:-warehouse_pipeline_writer}:${WAREHOUSE_PIPELINE_WRITER_PASSWORD:" in compose
    assert "POSTGRES_PASSWORD: ${WAREHOUSE_ADMIN_PASSWORD:" in compose
    assert "warehouse-init-roles.sh:/docker-entrypoint-initdb.d/20-studio-roles.sh:ro" in compose
    assert "STUDIO_AIRFLOW_OUTPUT_SCHEMA: pipeline_output" in compose
    assert 'STUDIO_AIRFLOW_REGISTRATION_TIMEOUT_SECONDS: "900"' in compose
    assert 'STUDIO_AIRFLOW_RUN_TIMEOUT_SECONDS: "86400"' in compose
    assert "options=-csearch_path%3Dpublic%2Cpipeline_output" in compose
    assert "-cstatement_timeout%3D900000%20-clock_timeout%3D30000" in compose
    assert 'STUDIO_AIRFLOW_TASK_TIMEOUT_SECONDS: "900"' in compose
    roles = (ROOT / "deploy" / "portable" / "warehouse-init-roles.sh").read_text()
    assert "SET search_path TO public, pipeline_output" in roles
    assert "SET statement_timeout TO %L" in roles
    assert "SET lock_timeout TO %L" in roles
    assert "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC" in roles
    assert compose.count("WAREHOUSE_READER_PASSWORD") >= 2
    assert compose.count("WAREHOUSE_PIPELINE_WRITER_PASSWORD") >= 2


def test_compose_database_health_waits_for_the_final_tcp_server():
    compose = (ROOT / "deploy" / "portable" / "compose.yaml").read_text()
    assert compose.count("pg_isready -h 127.0.0.1") == 3
    assert 'test: ["CMD-SHELL", "pg_isready -U ' not in compose


def test_portable_compose_gates_lightning_controller_on_a_real_completion():
    compose = (ROOT / "deploy" / "portable" / "compose.yaml").read_text()
    smoke_service = compose.split("  recovery-model-smoke:", 1)[1].split(
        "\n  lightning-server:", 1
    )[0]
    assert 'command: ["python", "scripts/smoke_recovery_model.py"]' in smoke_service
    assert "recovery-model:\n        condition: service_healthy" in smoke_service
    controller = compose.split("  lightning-controller:", 1)[1].split(
        "\n  studio-web:", 1
    )[0]
    assert "recovery-model-smoke:\n        condition: service_completed_successfully" in controller
    assert 'scripts/run_agent_lightning.py", "controller-check"' in controller

    bitnet = (ROOT / "deploy" / "portable" / "compose.bitnet.yaml").read_text()
    assert "recovery-model:" not in bitnet
    assert "STUDIO_RECOVERY_SMOKE_REQUIRE_TOKEN_IDS" not in bitnet
    assert 'STUDIO_REQUIRE_ADAPTER: "1"' in bitnet
    assert "STUDIO_ADAPTER_SHA256: ${STUDIO_BITNET_ADAPTER_SHA256:?" in bitnet
    assert bitnet.count("STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256:") == 2
    assert bitnet.count('STUDIO_REQUIRE_TOOL_ADAPTER_SHA256: "1"') == 2


def test_portable_compose_is_loopback_and_non_demo_by_default():
    compose = (ROOT / "deploy" / "portable" / "compose.yaml").read_text()
    generator = (ROOT / "deploy" / "portable" / "generate_env.py").read_text()
    assert "STUDIO_DEMO_MODE: ${STUDIO_DEMO_MODE:-0}" in compose
    assert '${STUDIO_BIND_ADDRESS:-127.0.0.1}:${STUDIO_PUBLIC_PORT:-8000}:8000' in compose
    assert '"STUDIO_DEMO_MODE": "0"' in generator
    assert '"STUDIO_BIND_ADDRESS": "127.0.0.1"' in generator


def test_portable_compose_keeps_lightning_token_out_of_public_web():
    compose = (ROOT / "deploy" / "portable" / "compose.yaml").read_text()
    shared = compose.split("x-studio-runtime:", 1)[1].split("\nservices:", 1)[0]
    web = compose.split("  studio-web:", 1)[1].split("\n  studio-worker:", 1)[0]
    worker = compose.split("  studio-worker:", 1)[1].split("\nvolumes:", 1)[0]
    assert "STUDIO_AGL_TOKEN" not in shared
    assert "STUDIO_AGL_TOKEN" not in web
    assert "STUDIO_AGL_TOKEN: ${AGL_KEY:?set AGL_KEY}" in worker
    assert "lightning-controller:\n        condition: service_healthy" in worker
    assert "http://127.0.0.1:8000/readyz" in web


@pytest.mark.parametrize("role", ["init", "api-server", "scheduler", "dag-processor", "triggerer"])
def test_runtime_accepts_explicit_airflow3_roles(role, tmp_path):
    env = _env(tmp_path, role)
    if role == "init":
        (tmp_path / runtime.SHARED_SENTINEL).unlink()
    if role not in {"scheduler", "triggerer"}:
        env.pop("AIRFLOW__CORE__EXECUTION_API_SERVER_URL")
    assert runtime.validate(role, env) == tmp_path


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda env: env.update(AIRFLOW_ROLE="webserver"), "AIRFLOW_ROLE"),
        (lambda env: env.update(AIRFLOW__DATABASE__SQL_ALCHEMY_CONN="sqlite:////tmp/airflow.db"), "PostgreSQL"),
        (lambda env: env.update(AIRFLOW__CORE__FERNET_KEY="not-a-key"), "Fernet"),
        (lambda env: env.update(AIRFLOW__API__SECRET_KEY="short"), "32 characters"),
        (lambda env: env.pop("AIRFLOW__CORE__EXECUTION_API_SERVER_URL"), "EXECUTION_API_SERVER_URL"),
    ],
)
def test_runtime_rejects_unsafe_production_config(mutation, match, tmp_path):
    env = _env(tmp_path)
    mutation(env)
    with pytest.raises(runtime.ConfigurationError, match=match):
        runtime.validate(env["AIRFLOW_ROLE"], env)


def test_scheduler_refuses_an_uninitialized_or_forged_dag_mount(tmp_path):
    env = _env(tmp_path)
    marker = tmp_path / runtime.SHARED_SENTINEL
    marker.unlink()
    with pytest.raises(runtime.ConfigurationError, match="same mounted volume"):
        runtime.validate("scheduler", env)
    marker.write_text("wrong image\n")
    with pytest.raises(runtime.ConfigurationError, match="invalid runtime sentinel"):
        runtime.validate("scheduler", env)


def test_init_marks_shared_dag_mount_idempotently(tmp_path):
    marker = tmp_path / runtime.SHARED_SENTINEL
    runtime.mark_shared(tmp_path)
    assert marker.read_text() == runtime.SHARED_SENTINEL_CONTENT
    runtime.mark_shared(tmp_path)
    marker.chmod(0o600)
    marker.write_text("forged")
    with pytest.raises(runtime.ConfigurationError, match="refusing to replace"):
        runtime.mark_shared(tmp_path)


def test_api_health_checks_public_v2_endpoint_and_metadata_database():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"metadatabase": {"status": "healthy"}}).encode()

    calls = []

    def open_(request, timeout):
        calls.append((request.full_url, timeout))
        return Response()

    healthcheck.api_health({"AIRFLOW__API__PORT": "9876"}, opener=open_)
    assert calls == [("http://127.0.0.1:9876/api/v2/monitor/health", 8)]


def test_component_health_targets_the_local_airflow_job(tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))

    healthcheck.job_health("dag-processor", _env(tmp_path, "dag-processor"), runner=run)
    command, options = calls[0]
    assert command[:5] == ("airflow", "jobs", "check", "--job-type", "DagProcessorJob")
    assert command[5] == "--hostname"
    assert options["check"] is True
    assert options["timeout"] == 8


def test_api_health_rejects_a_live_server_with_a_broken_database():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"metadatabase":{"status":"unhealthy"}}'

    with pytest.raises(RuntimeError, match="metadata database"):
        healthcheck.api_health({}, opener=lambda *args, **kwargs: Response())


def test_post_deploy_smoke_requires_the_whole_cluster_and_optional_dag(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/auth/token"):
            return {"access_token": "short-lived-token"}
        if url.endswith("/monitor/health"):
            return {
                "metadatabase": {"status": "healthy"},
                "scheduler": {"status": "healthy"},
                "dag_processor": {"status": "healthy"},
            }
        return {"dag_id": "studio_demo__abc", "is_paused": False, "has_import_errors": False}

    monkeypatch.setattr(smoke, "_request", request)
    result = smoke.smoke({
        "AIRFLOW_URL": "https://airflow.example.test/",
        "AIRFLOW_USERNAME": "studio",
        "AIRFLOW_PASSWORD": "secret",
        "AIRFLOW_SMOKE_DAG_ID": "studio_demo__abc",
    })
    assert result == {
        "status": "ready",
        "components": ["metadatabase", "scheduler", "dag_processor"],
        "dag_id": "studio_demo__abc",
    }
    assert calls[0][2]["body"] == {"username": "studio", "password": "secret"}
    assert calls[-1][1].endswith("/api/v2/dags/studio_demo__abc")
