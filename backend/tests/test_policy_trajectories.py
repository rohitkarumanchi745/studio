import json
import sys
import types
import uuid

import pytest
from fastapi import HTTPException

from app import agent, db, policy_trajectories as pt, router as model_router, trainer


BASE_IDENTITY = {
    "training_model": "base-model",
    "training_revision": "1" * 40,
    "serving_sha256": "2" * 64,
}


def _user():
    return {"id": str(uuid.uuid4()), "role": "analyst", "name": "Policy Tester"}


def _airflow():
    inp = {"prompt": "Build a daily sales extract", "source": "demo"}
    target = {
        "version": 1, "name": "Sales extract", "dag_id": "sales_extract",
        "source": "demo", "schedule": None, "parameters": {},
        "tasks": [{"id": "read_sales", "name": "Read sales", "source": "demo",
                   "sql": "SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region",
                   "depends_on": []}],
        "missing": [],
    }
    return inp, target


def _graph():
    return (
        {"prompt": "Find top accounts then their spend", "sources": [
            {"source": "postgres", "dialect": "postgres", "tables": ["accounts"]},
            {"source": "snowflake", "dialect": "snowflake", "tables": ["spend"]},
        ]},
        {"version": 1,
         "nodes": [{"id": "top_accounts", "source": "postgres", "task": "Find top accounts"},
                   {"id": "account_spend", "source": "snowflake", "task": "Total their spend"}],
         "edges": [{"from": "top_accounts", "to": "account_spend"}],
         "combine": "reason"},
    )


def _recovery():
    return (
        {"prompt": "Run the sales DAG", "source": "demo", "failed_action": {
            "type": "airflow_dag", "dag_id": "sales_extract"},
         "failure": {"state": "failed", "error": "relation not found"},
         "attempt": 1, "history": []},
        {"version": 1, "decision": "retry", "reason": "The transient catalog refresh completed."},
    )


def _aggregate(text="Revenue was 100."):
    return (
        {"prompt": "Summarize revenue", "contributions": [{
            "id": "worker-one", "source": "demo", "text": "Revenue was 100",
            "columns": ["revenue"], "rows": [[100]],
        }]},
        {"version": 1, "text": text, "citations": ["worker-one"]},
    )


def _dependent():
    inp = {"prompt": "Which top accounts spent most?",
           "task": "Total spend for the upstream accounts",
           "evidence": [{"id": "top-accounts", "source": "postgres",
                         "text": "Top accounts", "columns": ["account_id"],
                         "rows": [["a-1"], ["a-2"]]}],
           "source": "snowflake", "dialect": "snowflake",
           "allowed_tables": ["spend"],
           "schema": {"spend": [{"name": "account_id", "type": "text"},
                                 {"name": "amount", "type": "number"}]}}
    normalized = pt.normalize_contract_input(pt.DEPENDENT_AGENT, inp)
    item = json.dumps(normalized["evidence"][0], ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    prompt = (f"REFERENCE DATA {normalized['evidence'][0]['id']}\n{item}\n"
              f"ROOT USER REQUEST:\n{normalized['prompt']}\n"
              f"NODE TASK:\n{normalized['task']}")
    return inp, {"version": 1, "prompt": prompt, "citations": ["top-accounts"]}


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    pt.init_tables()
    trainer.init_tables()
    monkeypatch.setenv("STUDIO_TRAJECTORY_TRAINING", "user")
    monkeypatch.setenv("STUDIO_TENANT_ID", "test-tenant")
    monkeypatch.setenv("STUDIO_TRAJECTORY_BASE_MODEL", BASE_IDENTITY["training_model"])
    monkeypatch.setenv("STUDIO_TRAJECTORY_BASE_REVISION", BASE_IDENTITY["training_revision"])
    monkeypatch.setenv("STUDIO_TRAJECTORY_BASE_SHA256", BASE_IDENTITY["serving_sha256"])


@pytest.mark.parametrize("contract,factory", [
    (pt.AIRFLOW_DAG, _airflow),
    (pt.AGENT_GRAPH, _graph),
    (pt.RECOVERY_DECISION, _recovery),
    (pt.AGGREGATOR_OUTPUT, _aggregate),
    (pt.DEPENDENT_AGENT, _dependent),
])
def test_all_five_contracts_are_strict_canonical_and_versioned(contract, factory):
    inp, target = factory()
    normalized_input, normalized_target = pt.validate_contract_payload(contract, inp, target)
    assert json.loads(pt.policy_prompt(contract, normalized_input)) == {
        "protocol": pt.POLICY_PROTOCOL, "contract": contract, "version": 1,
        "input": normalized_input,
    }
    assert json.loads(pt.policy_target(contract, normalized_target)) == normalized_target


def test_evidence_normalization_is_idempotent_and_train_equals_serve(monkeypatch):
    user = _user()
    raw_input, raw_target = _dependent()
    first_input, first_target = pt.validate_contract_payload(
        pt.DEPENDENT_AGENT, raw_input, raw_target)
    second_input, second_target = pt.validate_contract_payload(
        pt.DEPENDENT_AGENT, first_input, first_target)
    assert second_input == first_input
    assert second_target == first_target
    token = first_input["evidence"][0]["id"]
    assert token.startswith("ev_") and token in first_target["prompt"]
    live_prompt = pt.policy_prompt(
        pt.DEPENDENT_AGENT, pt.normalize_contract_input(pt.DEPENDENT_AGENT, raw_input))

    stored = pt.capture(pt.DEPENDENT_AGENT, raw_input, raw_target, user=user,
                        scope="user", training_opt_in=True,
                        lineage=["conversation-secret", "run-secret"])
    page = pt.fetch_training_page(scope=stored["scope"])
    assert page["count"] == 1
    assert page["trajectories"][0]["prompt"] == live_prompt
    assert page["trajectories"][0]["completion"] == pt.policy_target(
        pt.DEPENDENT_AGENT, first_target)


def test_store_encrypts_content_hmacs_identity_and_is_idempotent():
    user = _user()
    inp, target = _airflow()
    first = pt.capture(pt.AIRFLOW_DAG, inp, target, user=user, scope="user",
                       training_opt_in=True, idempotency_key="same logical run")
    second = pt.capture(pt.AIRFLOW_DAG, inp, target, user=user, scope="user",
                        training_opt_in=True, idempotency_key="same logical run")
    assert first["id"] == second["id"]
    assert user["id"] not in first["scope"]
    with db.connect() as connection:
        row = connection.execute(
            "SELECT ciphertext,idempotency_key FROM policy_trajectories WHERE id=?",
            (first["id"],)).fetchone()
    assert inp["prompt"] not in row["ciphertext"]
    assert row["idempotency_key"] != "same logical run"
    envelope = json.loads(pt._fernet().decrypt(row["ciphertext"].encode()).decode())
    assert envelope["protocol"] == pt.STORAGE_PROTOCOL
    assert envelope["binding"] == {
        "id": first["id"], "revision": first["revision"],
        "contract": pt.AIRFLOW_DAG, "contract_version": 1,
        "scope": first["scope"], "reward": 1.0,
    }
    other = pt.capture(pt.AIRFLOW_DAG, inp, target, user=_user(), scope="user",
                       training_opt_in=True, idempotency_key="same logical run")
    assert other["id"] != first["id"]


@pytest.mark.parametrize("column,mutated", [
    ("id", "tr_" + "f" * 32),
    ("revision", 1_000_000),
    ("contract", pt.AGENT_GRAPH),
    ("contract_version", 99),
    ("scope", "user:" + "f" * 64),
    ("reward", -0.25),
])
def test_fetch_rejects_independently_modified_encrypted_metadata(column, mutated):
    user = _user()
    inp, target = _airflow()
    saved = pt.capture(
        pt.AIRFLOW_DAG, inp, target, user=user, scope="user", reward=0.75,
        training_opt_in=True, idempotency_key=f"metadata-binding-{column}-{uuid.uuid4()}")
    original_scope = saved["scope"]
    with db.connect() as connection:
        connection.execute(
            f"UPDATE policy_trajectories SET {column}=? WHERE id=?",
            (mutated, saved["id"]))
        connection.commit()

    query_scope = mutated if column == "scope" else original_scope
    page = pt.fetch_training_page(scope=query_scope)
    assert page["trajectories"] == []
    assert page["count"] == 0
    assert page["cursor"] == (mutated if column == "revision" else saved["revision"])
    assert page["has_more"] is False


def test_legacy_unbound_ciphertext_fails_closed_and_advances_cursor():
    user = _user()
    inp, target = _airflow()
    idem = f"legacy-envelope-{uuid.uuid4()}"
    saved = pt.capture(
        pt.AIRFLOW_DAG, inp, target, user=user, scope="user",
        training_opt_in=True, idempotency_key=idem)
    with db.connect() as connection:
        row = dict(connection.execute(
            "SELECT ciphertext FROM policy_trajectories WHERE id=?",
            (saved["id"],)).fetchone())
        envelope = json.loads(pt._fernet().decrypt(row["ciphertext"].encode()).decode())
        # This is the exact unbound body written before storage protocol v2.
        legacy_ciphertext = pt._fernet().encrypt(
            json.dumps(envelope["payload"], sort_keys=True,
                       separators=(",", ":")).encode()).decode()
        connection.execute(
            "UPDATE policy_trajectories SET ciphertext=? WHERE id=?",
            (legacy_ciphertext, saved["id"]))
        connection.commit()

    page = pt.fetch_training_page(scope=saved["scope"])
    assert page["trajectories"] == []
    assert page["cursor"] == saved["revision"]
    with pytest.raises(pt.ContractRejected, match="metadata binding"):
        pt.capture(pt.AIRFLOW_DAG, inp, target, user=user, scope="user",
                   training_opt_in=True, idempotency_key=idem)


def test_collection_mode_and_raw_evidence_scope_fail_closed(monkeypatch):
    user = _user()
    inp, target = _airflow()
    monkeypatch.setenv("STUDIO_TRAJECTORY_TRAINING", "off")
    assert pt.capture(pt.AIRFLOW_DAG, inp, target, user=user, scope="user",
                      training_opt_in=True) is None
    monkeypatch.setenv("STUDIO_TRAJECTORY_TRAINING", "tenant")
    assert pt.capture(pt.AIRFLOW_DAG, inp, target, user=user, scope="user",
                      training_opt_in=True) is None
    agg_input, agg_target = _aggregate()
    with pytest.raises(pt.ContractRejected, match="user-scoped"):
        pt.capture(pt.AGGREGATOR_OUTPUT, agg_input, agg_target, user=user,
                   scope="tenant", training_opt_in=True)


def test_scope_resolution_is_admin_only_bounded_and_returns_no_training_data():
    raw_user_id = str(uuid.uuid4())
    body = pt.TrainingScopeIn(scope=f"user:{raw_user_id}")
    with pytest.raises(HTTPException) as forbidden:
        pt.resolve_training_scope(body, user={"id": raw_user_id, "role": "analyst"})
    assert forbidden.value.status_code == 403

    resolved = pt.resolve_training_scope(
        body, user={"id": "trainer", "role": "admin"})
    assert resolved == {"scope": pt.user_scope(raw_user_id)}
    assert raw_user_id not in resolved["scope"]
    assert set(resolved) == {"scope"}

    with pytest.raises(HTTPException) as invalid:
        pt.resolve_training_scope(
            pt.TrainingScopeIn(scope="user:contains/a/slash"),
            user={"id": "trainer", "role": "admin"})
    assert invalid.value.status_code == 400


def test_training_get_refuses_readable_identity_in_query_string():
    admin = {"id": "trainer", "role": "admin"}
    with pytest.raises(HTTPException) as exposed:
        pt.training_trajectories("user:plain-user-id", user=admin)
    assert exposed.value.status_code == 400
    assert "opaque scope" in exposed.value.detail

    opaque = pt.user_scope("plain-user-id")
    page = pt.training_trajectories(opaque, since=0, limit=100, user=admin)
    assert page["scope"] == opaque and page["trajectories"] == []


def test_aggregator_rejects_numeric_and_qualitative_hallucinations():
    inp, target = _aggregate("Revenue was 999.")
    with pytest.raises(pt.ContractRejected, match="numeric claims"):
        pt.validate_contract_payload(pt.AGGREGATOR_OUTPUT, inp, target)
    inp, target = _aggregate("Profits collapsed catastrophically.")
    with pytest.raises(pt.ContractRejected, match="lexically grounded"):
        pt.validate_contract_payload(pt.AGGREGATOR_OUTPUT, inp, target)


def test_graph_cycles_and_unknown_dependent_citations_are_rejected():
    inp, target = _graph()
    target["edges"].append({"from": "account_spend", "to": "top_accounts"})
    with pytest.raises(pt.ContractRejected, match="cycle"):
        pt.validate_contract_payload(pt.AGENT_GRAPH, inp, target)
    inp, target = _dependent()
    target["citations"] = ["made-up"]
    with pytest.raises(pt.ContractRejected, match="unknown"):
        pt.validate_contract_payload(pt.DEPENDENT_AGENT, inp, target)


def test_dependent_prompt_does_not_rewrite_short_evidence_ids():
    inp, _ = _dependent()
    inp["evidence"][0]["id"] = "a"
    normalized = pt.normalize_contract_input(pt.DEPENDENT_AGENT, inp)
    token = normalized["evidence"][0]["id"]
    item = json.dumps(normalized["evidence"][0], ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    prompt = (f"REFERENCE DATA {token}: a banana and an account are data.\n{item}\n"
              f"{normalized['prompt']}\n{normalized['task']}")
    _, target = pt.validate_contract_payload(
        pt.DEPENDENT_AGENT, inp,
        {"version": 1, "prompt": prompt, "citations": ["a"]})
    assert target["prompt"] == prompt


def test_airflow_contract_accepts_runtime_boundaries_and_unordered_dependencies():
    inp, target = _airflow()
    target["dag_id"] = "sales.extract-v1"
    target["parameters"] = {f"p_{index}": "x" * 4000 for index in range(40)}
    tasks = []
    for index in range(12):
        tasks.append({"id": f"task_{index}", "name": f"Task {index}",
                      "source": "demo", "sql": f"SELECT {index} AS value",
                      "depends_on": [f"task_{index - 1}"] if index else []})
    # The governed runtime accepts an unordered DAG and topologically sorts it.
    target["tasks"] = list(reversed(tasks))
    _, normalized = pt.validate_contract_payload(pt.AIRFLOW_DAG, inp, target)
    assert normalized["dag_id"] == "sales.extract-v1"
    assert [task["id"] for task in normalized["tasks"]] == [f"task_{i}" for i in range(12)]
    target["tasks"] = tasks + [{"id": "task_12", "name": "Too many", "source": "demo",
                                "sql": "SELECT 12", "depends_on": ["task_11"]}]
    with pytest.raises(pt.ContractRejected, match="1-12"):
        pt.validate_contract_payload(pt.AIRFLOW_DAG, inp, target)


def test_fetch_is_byte_bounded_without_skipping_large_rows(monkeypatch):
    monkeypatch.setenv("STUDIO_TRAJECTORY_PAGE_BYTES", str(512 * 1024))
    user = _user()
    ids = []
    for index in range(7):
        inp, target = _aggregate(f"Revenue batch {index} was 100.")
        inp["prompt"] = f"Summarize revenue batch {index}"
        inp["contributions"][0]["id"] = f"worker-{index}"
        inp["contributions"][0]["text"] = f"Revenue batch {index} was 100"
        inp["contributions"][0]["columns"] = [f"column_{n}" for n in range(30)]
        inp["contributions"][0]["rows"] = [
            ["x" * 512 for _ in range(30)] for _ in range(12)]
        target["citations"] = [f"worker-{index}"]
        saved = pt.capture(pt.AGGREGATOR_OUTPUT, inp, target, user=user, scope="user",
                           training_opt_in=True, idempotency_key=f"large-{index}")
        ids.append(saved["id"])
    scope = pt.user_scope(user)
    found, cursor, pages = [], 0, 0
    while True:
        page = pt.fetch_training_page(scope=scope, after=cursor, limit=100)
        pages += 1
        found.extend(row["id"] for row in page["trajectories"])
        assert page["cursor"] >= cursor
        cursor = page["cursor"]
        if not page["has_more"]:
            break
        assert page["count"] > 0
    assert pages > 1
    assert found == ids


def test_fetch_derives_fernet_key_once_per_secret():
    pt._fernet_for_secret.cache_clear()
    user = _user()
    for index in range(3):
        inp, target = _airflow()
        inp["prompt"] += f" {index}"
        pt.capture(pt.AIRFLOW_DAG, inp, target, user=user, scope="user",
                   training_opt_in=True, idempotency_key=f"derive-{index}")
    before = pt._fernet_for_secret.cache_info()
    pt.fetch_training_page(scope=pt.user_scope(user))
    after = pt._fernet_for_secret.cache_info()
    assert before.misses == 1
    assert after.misses == 1


def _promotion(scope, artifact="a" * 64, dataset="b" * 64, base="base-model"):
    evidence = {contract: {"positive_cases": 5, "paired_cases": 5,
                           "baseline_passed": 4, "candidate_passed": 5,
                           "baseline_unsafe": 0, "candidate_unsafe": 0}
                for contract in pt.CONTRACTS}
    report = {"protocol": trainer.TRAJECTORY_EVAL_PROTOCOL,
              "passed": True, "safety_passed": True,
              "artifact_sha256": artifact, "dataset_sha256": dataset,
              "scope": scope, "base_identity": {
                  **BASE_IDENTITY, "training_model": base},
              "suite_sha256": "c" * 64,
              "capabilities": list(pt.CONTRACTS), "contracts": evidence}
    return {"dataset_sha256": dataset, "base_identity": report["base_identity"],
            "capabilities": list(pt.CONTRACTS),
            "evaluation": report}


def test_registry_recomputes_promotion_and_only_selects_exact_user(monkeypatch):
    monkeypatch.setenv("STUDIO_TRAJECTORY_EVAL_SUITE_SHA256", "c" * 64)
    user = _user()
    scope = pt.user_scope(user)
    published = trainer.publish(scope, "trajectory_policy", "/adapters/user-policy",
                                base_model="base-model", sha256="a" * 64,
                                metrics=_promotion(scope))
    selected = trainer.trajectory_adapter(user, pt.AGENT_GRAPH)
    assert selected["version"] == published["version"]
    assert selected["scope"] == scope
    assert selected["kind"] == "trajectory_policy"
    assert selected["base_identity"] == BASE_IDENTITY
    assert selected["capabilities"] == list(pt.CONTRACTS)
    active = trainer.active_adapter_endpoint(
        scope, "trajectory_policy", user={"id": "admin", "role": "admin"})["adapter"]
    assert active["id"] == published["id"]
    assert active["base_identity"] == BASE_IDENTITY
    assert active["metrics"]["promotion_evidence"]["artifact_sha256"] == "a" * 64
    monkeypatch.setenv("STUDIO_TRAJECTORY_EVAL_SUITE_SHA256", "d" * 64)
    assert trainer.trajectory_adapter(user, pt.AGENT_GRAPH) is None


def test_registry_refuses_tenant_forged_safety_and_low_pass_rate(monkeypatch):
    monkeypatch.setenv("STUDIO_TRAJECTORY_EVAL_SUITE_SHA256", "c" * 64)
    tenant = pt.tenant_scope("tenant-a")
    with pytest.raises(HTTPException, match="user scope"):
        trainer.publish(tenant, "trajectory_policy", "/adapters/tenant",
                        base_model="base-model", sha256="a" * 64,
                        metrics=_promotion(tenant))
    user_scope = pt.user_scope(_user())
    unsafe = _promotion(user_scope)
    unsafe["evaluation"]["contracts"][pt.AIRFLOW_DAG]["candidate_unsafe"] = 1
    with pytest.raises(HTTPException, match="promotion threshold"):
        trainer.publish(user_scope, "trajectory_policy", "/adapters/unsafe",
                        base_model="base-model", sha256="a" * 64, metrics=unsafe)
    weak = _promotion(user_scope)
    weak["evaluation"]["contracts"][pt.AGENT_GRAPH].update(
        paired_cases=10, positive_cases=5, baseline_passed=8, candidate_passed=8,
        baseline_unsafe=0, candidate_unsafe=0)
    with pytest.raises(HTTPException, match="promotion threshold"):
        trainer.publish(user_scope, "trajectory_policy", "/adapters/weak",
                        base_model="base-model", sha256="a" * 64, metrics=weak)


def test_registry_pins_full_base_identity_and_fails_closed_without_operator_pin(
        monkeypatch):
    monkeypatch.setenv("STUDIO_TRAJECTORY_EVAL_SUITE_SHA256", "c" * 64)
    scope = pt.user_scope(_user())
    drifted = _promotion(scope)
    drifted["base_identity"] = dict(drifted["base_identity"])
    drifted["evaluation"] = dict(drifted["evaluation"])
    drifted["base_identity"]["training_revision"] = "3" * 40
    drifted["evaluation"]["base_identity"] = drifted["base_identity"]
    with pytest.raises(HTTPException, match="deployment pin"):
        trainer.publish(scope, "trajectory_policy", "/adapters/drifted",
                        base_model="base-model", sha256="a" * 64, metrics=drifted)

    monkeypatch.delenv("STUDIO_TRAJECTORY_BASE_REVISION")
    with pytest.raises(HTTPException) as missing:
        trainer.publish(scope, "trajectory_policy", "/adapters/unpinned",
                        base_model="base-model", sha256="a" * 64,
                        metrics=_promotion(scope))
    assert missing.value.status_code == 503


@pytest.mark.parametrize("uri", [
    "relative/policy.gguf",
    "s3://bucket/policy.gguf",
    "https://user:secret@example.test/policy.gguf",
    "https://example.test/policy.gguf?mutable=1",
    "http://policy.example.test/policy.gguf",
    "/adapters/../secret/policy.gguf",
])
def test_trajectory_registry_refuses_ambiguous_or_credentialed_uris(uri):
    with pytest.raises(HTTPException):
        trainer._validate_trajectory_uri(uri)


@pytest.mark.parametrize("uri", [
    "/adapters/policy.gguf",
    r"D:\\adapters\\policy.gguf",
    "https://models.example.test/policy.gguf",
    "http://127.0.0.1:9000/policy.gguf",
    "http://[::1]:9000/policy.gguf",
])
def test_trajectory_registry_accepts_attestable_artifact_locations(uri):
    assert trainer._validate_trajectory_uri(uri) == uri


def test_policy_client_is_dormant_and_wire_is_exact(monkeypatch):
    monkeypatch.delenv("STUDIO_POLICY_TRUSTED_ENDPOINT", raising=False)
    with pytest.raises(agent.PolicyUnavailable, match="capture/offline"):
        agent.make_policy_llm(pt.AIRFLOW_DAG, _user())

    user = _user()
    adapter = {"uri": "/adapters/policy", "version": 1, "sha256": "a" * 64,
               "scope": pt.user_scope(user), "kind": "trajectory_policy",
               "base_model": "base-model", "base_identity": BASE_IDENTITY,
               "capabilities": list(pt.CONTRACTS)}
    monkeypatch.setenv("STUDIO_POLICY_TRUSTED_ENDPOINT", "1")
    monkeypatch.setenv("STUDIO_POLICY_LLM_BASE_URL", "http://127.0.0.1:9001/v1")
    monkeypatch.setenv("STUDIO_POLICY_LLM", "openai:trajectory-policy")
    monkeypatch.setattr(model_router, "trajectory_adapter", lambda *args: adapter)
    seen, sentinel = {}, object()

    def fake_init(spec, **kwargs):
        seen.update(spec=spec, kwargs=kwargs)
        return sentinel

    package = types.ModuleType("langchain")
    chat_models = types.ModuleType("langchain.chat_models")
    chat_models.init_chat_model = fake_init
    package.chat_models = chat_models
    monkeypatch.setitem(sys.modules, "langchain", package)
    monkeypatch.setitem(sys.modules, "langchain.chat_models", chat_models)
    assert agent.make_policy_llm(pt.AGENT_GRAPH, user) is sentinel
    assert seen["spec"] == "openai:trajectory-policy"
    assert seen["kwargs"]["extra_body"] == {
        "studio_adapters": {"trajectory_policy": adapter}}
    assert seen["kwargs"]["base_url"] == "http://127.0.0.1:9001/v1"


def test_policy_client_rejects_provider_confusion_and_remote_placeholder_key(monkeypatch):
    user = _user()
    adapter = {"uri": "/adapters/policy", "version": 1, "sha256": "a" * 64,
               "scope": pt.user_scope(user), "kind": "trajectory_policy",
               "base_model": "base-model", "base_identity": BASE_IDENTITY,
               "capabilities": list(pt.CONTRACTS)}
    monkeypatch.setenv("STUDIO_POLICY_TRUSTED_ENDPOINT", "1")
    monkeypatch.setenv("STUDIO_POLICY_LLM_BASE_URL", "https://policy.example/v1")
    monkeypatch.setattr(model_router, "trajectory_adapter", lambda *args: adapter)
    monkeypatch.setenv("STUDIO_POLICY_LLM", "anthropic:not-openai-compatible")
    with pytest.raises(agent.PolicyUnavailable, match="OpenAI-compatible"):
        agent.make_policy_llm(pt.AGENT_GRAPH, user)
    monkeypatch.setenv("STUDIO_POLICY_LLM", "openai:trajectory-policy")
    monkeypatch.delenv("STUDIO_POLICY_LLM_API_KEY", raising=False)
    with pytest.raises(agent.PolicyUnavailable, match="API_KEY"):
        agent.make_policy_llm(pt.AGENT_GRAPH, user)


def test_active_adapter_endpoint_is_admin_exact_and_missing_is_404():
    with pytest.raises(HTTPException) as forbidden:
        trainer.active_adapter_endpoint("user:" + "0" * 64, "trajectory_policy",
                                        user={"id": "u", "role": "analyst"})
    assert forbidden.value.status_code == 403
    with pytest.raises(HTTPException) as bad_kind:
        trainer.active_adapter_endpoint("user:" + "0" * 64, "unknown",
                                        user={"id": "a", "role": "admin"})
    assert bad_kind.value.status_code == 400
    with pytest.raises(HTTPException) as missing:
        trainer.active_adapter_endpoint("user:" + "0" * 64, "trajectory_policy",
                                        user={"id": "a", "role": "admin"})
    assert missing.value.status_code == 404
