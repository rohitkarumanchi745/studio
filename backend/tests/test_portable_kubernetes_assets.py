"""Structural checks for the cloud-neutral Kubernetes deployment assets."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
KUBE = ROOT / "deploy" / "portable" / "kubernetes"


def _documents(relative_path):
    with (KUBE / relative_path).open(encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document]


def _resource(relative_path, kind, name):
    for document in _documents(relative_path):
        if document.get("kind") == kind and document.get("metadata", {}).get("name") == name:
            return document
    raise AssertionError(f"missing {kind} {name} in {relative_path}")


def _named(items, name):
    return next(item for item in items if item.get("name") == name)


def _env(container):
    return {item["name"]: item.get("value") for item in container.get("env", [])}


def _allowed_sources(policy):
    return {
        tuple(sorted(source["podSelector"]["matchLabels"].items()))
        for rule in policy["spec"]["ingress"]
        for source in rule.get("from", [])
    }


def _selector(**labels):
    return tuple(sorted(labels.items()))


def test_airflow_uses_a_non_admin_api_user_and_read_only_dag_mounts():
    values = _documents("airflow-values.yaml")[0]
    assert values["images"]["airflow"]["digest"].startswith("sha256:")
    assert len(values["images"]["airflow"]["digest"]) == 71
    assert values["createUserJob"]["defaultUser"]["role"] == "User"
    assert "--role User" in values["createUserJob"]["args"][0]
    assert "Admin" not in values["createUserJob"]["args"][0]
    assert values["dags"]["persistence"]["enabled"] is False

    for component in ("apiServer", "scheduler", "dagProcessor"):
        volume = _named(values[component]["extraVolumes"], "studio-generated-dags")
        claim = volume["persistentVolumeClaim"]
        assert claim == {"claimName": "studio-airflow-dags", "readOnly": True}

        mount = _named(values[component]["extraVolumeMounts"], "studio-generated-dags")
        assert mount["mountPath"] == "/opt/airflow/dags"
        assert mount["readOnly"] is True


def test_worker_proves_controller_and_atomic_hard_link_access_to_shared_dag_claim():
    worker = _resource("base/runtime.yaml", "Deployment", "studio-worker")
    pod_spec = worker["spec"]["template"]["spec"]
    assert pod_spec["securityContext"]["fsGroupChangePolicy"] == "Always"

    init = _named(pod_spec["initContainers"], "dependencies-ready")
    assert init["command"] == ["python", "/opt/studio-probes/readiness.py"]
    assert _named(init["volumeMounts"], "dags")["readOnly"] is False

    container = _named(pod_spec["containers"], "studio-worker")
    expected_probe = ["python", "/opt/studio-probes/readiness.py"]
    assert container["startupProbe"]["exec"]["command"] == expected_probe
    assert container["readinessProbe"]["exec"]["command"] == expected_probe
    dag_mount = _named(container["volumeMounts"], "dags")
    assert dag_mount["mountPath"] == "/opt/airflow/dags"
    assert dag_mount["readOnly"] is False

    probes = _resource("base/config.yaml", "ConfigMap", "studio-worker-probes")
    runtime_config = _resource("base/config.yaml", "ConfigMap", "studio-runtime-config")
    assert runtime_config["data"]["STUDIO_AIRFLOW_OUTPUT_SCHEMA"] == "pipeline_output"
    assert runtime_config["data"]["STUDIO_AIRFLOW_REGISTRATION_TIMEOUT_SECONDS"] == "900"
    assert runtime_config["data"]["STUDIO_AIRFLOW_RUN_TIMEOUT_SECONDS"] == "86400"
    assert runtime_config["data"]["STUDIO_AGL_RECOVERY_TIMEOUT_S"] == "300"
    readiness = probes["data"]["readiness.py"]
    compile(readiness, "studio-worker-readiness.py", "exec")
    assert 'for name in ("DATABASE_URL", "POSTGRES_DSN")' in readiness
    assert 'psycopg.connect(os.environ[name], connect_timeout=5)' in readiness
    assert 'os.environ["REDIS_URL"]' in readiness
    assert 'airflow + "/auth/token"' in readiness
    assert 'airflow + "/api/v2/monitor/health"' in readiness
    assert 'os.environ["STUDIO_AGL_URL"].rstrip("/") + "/readyz"' in readiness
    assert 'os.environ["STUDIO_WORKER_CLAIM_GATE_URL"]' in readiness
    assert 'os.access(dags, os.W_OK | os.X_OK)' in readiness
    assert 'tempfile.mkstemp(prefix=".studio-write-check-", dir=dags)' in readiness
    assert "os.link(candidate, linked)" in readiness
    assert "os.unlink(path)" in readiness


def test_airflow_writer_connection_keeps_authorized_inputs_first():
    secrets = _resource("secrets.example.yaml", "Secret", "airflow-connections")
    connection = secrets["stringData"]["AIRFLOW_CONN_STUDIO_POSTGRES"]
    assert "warehouse_pipeline_writer:" in connection
    assert "options=-csearch_path%3Dpublic%2Cpipeline_output" in connection
    assert "-cstatement_timeout%3D900000%20-clock_timeout%3D30000" in connection
    config = _resource("base/config.yaml", "ConfigMap", "studio-runtime-config")
    assert config["data"]["STUDIO_AIRFLOW_TASK_TIMEOUT_SECONDS"] == "900"


def test_recovery_controller_is_gated_by_completion_and_live_poll_heartbeat():
    controller = _resource("base/runtime.yaml", "Deployment", "lightning-controller")
    pod = controller["spec"]["template"]["spec"]
    init = _named(pod["initContainers"], "recovery-model-smoke")
    assert init["command"] == ["python", "scripts/smoke_recovery_model.py"]
    env = _env(init)
    assert env["STUDIO_AGL_MODEL_ENDPOINT"] == "http://recovery-model:8081/v1"
    assert env["STUDIO_AGL_RECOVERY_MODEL"] == "studio-recovery"
    container = _named(pod["containers"], "lightning-controller")
    assert _named(container["ports"], "health")["containerPort"] == 8082
    expected = ["python", "scripts/run_agent_lightning.py", "controller-check"]
    assert container["startupProbe"]["exec"]["command"] == expected
    assert container["readinessProbe"]["exec"]["command"] == expected
    assert container["livenessProbe"]["exec"]["command"] == expected
    service = _resource("base/runtime.yaml", "Service", "lightning-controller")
    assert service["spec"]["ports"][0]["port"] == 8082

    worker = _resource("base/runtime.yaml", "Deployment", "studio-worker")
    worker_env = _env(_named(worker["spec"]["template"]["spec"]["containers"],
                            "studio-worker"))
    config = _resource("base/config.yaml", "ConfigMap", "studio-runtime-config")
    assert config["data"]["STUDIO_WORKER_CLAIM_GATE_URL"] == \
        "http://lightning-controller:8082/readyz"
    assert "STUDIO_WORKER_CLAIM_GATE_URL" not in worker_env  # inherited through envFrom


def test_bitnet_overlay_pins_uri_version_and_sha256_end_to_end():
    resources = _documents("overlays/bitnet/bitnet.yaml")
    config = next(item for item in resources
                  if item.get("kind") == "ConfigMap"
                  and item.get("metadata", {}).get("name") == "bitnet-adapter-config")
    assert set(config["data"]) == {"baseRevision", "baseSha256", "url", "version", "sha256"}
    assert len(config["data"]["baseRevision"]) == 40
    assert len(config["data"]["baseSha256"]) == 64
    assert len(config["data"]["sha256"]) == 64

    serving = _resource("overlays/bitnet/bitnet.yaml", "Deployment", "bitnet-serving")
    serving_env = _env(_named(serving["spec"]["template"]["spec"]["containers"],
                              "bitnet-serving"))
    assert serving_env["STUDIO_REQUIRE_ADAPTER"] == "1"
    for name in ("STUDIO_ADAPTER_URL", "STUDIO_ADAPTER_VERSION", "STUDIO_ADAPTER_SHA256"):
        assert name in serving_env

    kustomization = _documents("overlays/bitnet/kustomization.yaml")[0]
    assert not any("recovery-model" in patch.get("path", "")
                   or "lightning" in patch.get("path", "")
                   for patch in kustomization["patches"])

    for patch_file, component in (("overlays/bitnet/studio-web-patch.yaml", "studio-web"),
                                  ("overlays/bitnet/studio-worker-patch.yaml", "studio-worker")):
        workload = _resource(patch_file, "Deployment", component)
        container = _named(workload["spec"]["template"]["spec"]["containers"], component)
        application_env = _env(container)
        assert "STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256" in application_env
        assert application_env["STUDIO_REQUIRE_TOOL_ADAPTER_SHA256"] == "1"


def test_services_stay_private_and_ingress_is_an_opt_in_studio_only_template():
    services = [
        document
        for relative_path in ("base/runtime.yaml", "overlays/bitnet/bitnet.yaml")
        for document in _documents(relative_path)
        if document.get("kind") == "Service"
    ]
    assert {service["metadata"]["name"] for service in services} == {
        "studio-web",
        "recovery-model",
        "lightning-server",
        "lightning-controller",
        "bitnet-serving",
    }
    assert all(service["spec"].get("type") == "ClusterIP" for service in services)
    assert _documents("airflow-values.yaml")[0]["apiServer"]["service"]["type"] == "ClusterIP"

    base = _documents("base/kustomization.yaml")[0]
    assert "../ingress.example.yaml" not in base["resources"]
    assert all("ingress" not in resource.lower() for resource in base["resources"])
    ingress = _resource("ingress.example.yaml", "Ingress", "studio")
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert [path["backend"]["service"]["name"] for path in paths] == ["studio-web"]


def test_network_policies_cover_every_private_runtime_endpoint():
    policies = {
        document["metadata"]["name"]: document
        for relative_path in ("base/network-policy.yaml", "overlays/bitnet/bitnet.yaml")
        for document in _documents(relative_path)
        if document.get("kind") == "NetworkPolicy"
    }
    assert set(policies) == {
        "studio-default-deny-ingress",
        "studio-web-ingress",
        "lightning-server-ingress",
        "lightning-controller-ingress",
        "recovery-model-ingress",
        "airflow-private-ingress",
        "bitnet-serving-ingress",
    }
    assert policies["studio-default-deny-ingress"]["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/part-of": "studio"
    }
    assert "ingress" not in policies["studio-default-deny-ingress"]["spec"]
    assert "from" not in policies["studio-web-ingress"]["spec"]["ingress"][0]

    expected_targets_and_ports = {
        "studio-web-ingress": ("studio", "web", 8000),
        "lightning-server-ingress": ("agent-lightning", "server", 8080),
        "lightning-controller-ingress": ("agent-lightning", "controller", 8082),
        "recovery-model-ingress": ("recovery-model", "model-bridge", 8081),
        "bitnet-serving-ingress": ("bitnet-serving", "learned-sql-model", 9000),
    }
    for name, (application, component, port) in expected_targets_and_ports.items():
        policy = policies[name]["spec"]
        assert policy["podSelector"]["matchLabels"] == {
            "app.kubernetes.io/name": application,
            "app.kubernetes.io/component": component,
        }
        assert any(
            rule_port["port"] == port
            for rule in policy["ingress"]
            for rule_port in rule.get("ports", [])
        )

    airflow = policies["airflow-private-ingress"]["spec"]
    assert airflow["podSelector"]["matchLabels"] == {"tier": "airflow", "release": "airflow"}
    assert any(
        rule_port["port"] == 8080
        for rule in airflow["ingress"]
        for rule_port in rule.get("ports", [])
    )

    assert _allowed_sources(policies["lightning-server-ingress"]) == {
        _selector(**{"app.kubernetes.io/name": "studio", "app.kubernetes.io/component": "worker"}),
        _selector(
            **{
                "app.kubernetes.io/name": "agent-lightning",
                "app.kubernetes.io/component": "controller",
            }
        ),
    }
    assert _allowed_sources(policies["recovery-model-ingress"]) == {
        _selector(
            **{
                "app.kubernetes.io/name": "agent-lightning",
                "app.kubernetes.io/component": component,
            }
        )
        for component in ("server", "controller")
    }
    assert _allowed_sources(policies["lightning-controller-ingress"]) == {
        _selector(**{"app.kubernetes.io/name": "studio", "app.kubernetes.io/component": "worker"})
    }
    assert _allowed_sources(policies["bitnet-serving-ingress"]) == {
        _selector(
            **{
                "app.kubernetes.io/name": application,
                "app.kubernetes.io/component": component,
            }
        )
        for application, component in (
            ("studio", "web"),
            ("studio", "worker"),
        )
    }
    assert _allowed_sources(policies["airflow-private-ingress"]) == {
        _selector(tier="airflow", release="airflow"),
        _selector(**{"app.kubernetes.io/name": "studio", "app.kubernetes.io/component": "web"}),
        _selector(**{"app.kubernetes.io/name": "studio", "app.kubernetes.io/component": "worker"}),
    }


def test_public_web_has_no_agent_lightning_bearer_token():
    web = _resource("base/runtime.yaml", "Deployment", "studio-web")
    worker = _resource("base/runtime.yaml", "Deployment", "studio-worker")
    web_env = _env(_named(web["spec"]["template"]["spec"]["containers"], "studio-web"))
    worker_env = _env(_named(worker["spec"]["template"]["spec"]["containers"], "studio-worker"))
    assert "STUDIO_AGL_TOKEN" not in web_env
    assert worker_env["STUDIO_AGL_TOKEN"] is None
    web_container = _named(web["spec"]["template"]["spec"]["containers"], "studio-web")
    assert web_container["startupProbe"]["httpGet"]["path"] == "/readyz"
    assert web_container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert web_container["livenessProbe"]["httpGet"]["path"] == "/health"
    assert web["spec"]["strategy"] == {"type": "Recreate"}


def test_example_separates_studio_reader_from_airflow_pipeline_writer():
    text = (KUBE / "secrets.example.yaml").read_text()
    assert "POSTGRES_DSN: postgresql://warehouse_reader:" in text
    assert "AIRFLOW_CONN_STUDIO_POSTGRES: postgresql://warehouse_pipeline_writer:" in text
