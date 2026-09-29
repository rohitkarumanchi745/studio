"""Complete-trajectory trainer: privacy, balance, eval and release gates."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "train_trajectory_policy.py"
_ONLINE = _ROOT / "scripts" / "train_online.py"


def _load(tmp_path=None, env=None):
    values = {
        "STUDIO_TRAJECTORY_OUTPUT_DIR": str(tmp_path or "/tmp/studio-trajectory-test"),
        "STUDIO_TRAJECTORY_TRAIN_MIN_NEW_PER_CONTRACT": "1",
        "STUDIO_TRAJECTORY_TRAIN_MIN_PAIRS_PER_CONTRACT": "1",
        "STUDIO_TRAJECTORY_TRAIN_MAX_LENGTH": "4096",
        "STUDIO_TRAJECTORY_TRAIN_MODE": "sft",
        "STUDIO_TRAJECTORY_ALLOW_PEFT_PUBLICATION": "1",
        "STUDIO_TRAJECTORY_BASE_MODEL": "example/policy-base",
        "STUDIO_TRAJECTORY_BASE_REVISION": "1" * 40,
        "STUDIO_TRAJECTORY_BASE_SHA256": "2" * 64,
    }
    values.update(env or {})
    saved = dict(os.environ)
    try:
        os.environ.update(values)
        spec = importlib.util.spec_from_file_location(
            f"trajectory_trainer_{id(values)}_{os.urandom(3).hex()}", _SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.environ.clear()
        os.environ.update(saved)


class FakeTokenizer:
    eos_token = "<eos>"

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        assert tokenize is False and add_generation_prompt is True
        return "".join(f"<{m['role']}>{m['content']}" for m in messages) + "<assistant>"

    def __call__(self, text, add_special_tokens=False, truncation=False):
        assert truncation is False
        # Character tokens make length assertions exact and deterministic.
        return {"input_ids": list(range(len(text) + int(add_special_tokens)))}


def _scope(kind="user", character="a"):
    return f"{kind}:{character * 64}"


def _row(module, contract, n=1, *, scope=None, reward=1.0, prompt=None,
         target=None, revision=None):
    scope = scope or _scope()
    inp = {"request": prompt or f"request {n}"}
    target = target or {"answer": f"result {n}", "version": 1}
    envelope = {
        "protocol": module.POLICY_PROTOCOL,
        "contract": contract,
        "version": 1,
        "input": inp,
    }
    return {
        "id": f"tr_{contract}_{n}",
        "revision": revision or n,
        "contract": contract,
        "contract_version": 1,
        "scope": scope,
        "reward": reward,
        "system": module.POLICY_SYSTEM,
        "prompt": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
        "completion": json.dumps(target, sort_keys=True, separators=(",", ":")),
    }


def _five(module, *, scope=None, offset=0, reward=1.0):
    return [_row(module, contract, offset + index + 1, scope=scope, reward=reward,
                 revision=offset + index + 1)
            for index, contract in enumerate(module.CONTRACTS)]


def _report(module, *, artifact="b" * 64, dataset="c" * 64,
            scope=None, base=None, request_id="request", passed=19,
            cases=20, unsafe=0):
    # ``unsafe`` toggles the independent evaluator's safety assertion.  The
    # compact registry contract records that assertion plus paired task counts;
    # the registry recomputes the task thresholds from those counts.
    return {
        "protocol": module.EVALUATION_PROTOCOL,
        "request_id": request_id,
        "artifact_sha256": artifact,
        "base_identity": base or module.base_identity(),
        "suite_sha256": "d" * 64,
        "dataset_sha256": dataset,
        "scope": scope or _scope(),
        "passed": True,
        "safety_passed": unsafe == 0,
        "capabilities": list(module.CONTRACTS),
        "contracts": {
            contract: {"positive_cases": cases, "paired_cases": cases,
                       "baseline_passed": 18, "candidate_passed": passed,
                       "baseline_unsafe": 0, "candidate_unsafe": unsafe}
            for contract in module.CONTRACTS},
    }


def _config(module):
    return {
        "command": ["evaluator"],
        "suite_sha256": "d" * 64,
        "min_cases_per_contract": 20,
        "min_pass_rate": 0.9,
        "max_unsafe_rate": 0.0,
        "max_unsafe_rate_increase": 0.0,
        "timeout_seconds": 10,
    }


_EVALUATOR = r"""
import argparse, json
p = argparse.ArgumentParser()
p.add_argument('--request', required=True)
p.add_argument('--report', required=True)
a = p.parse_args()
with open(a.request, encoding='utf-8') as h:
    request = json.load(h)
report = {k: request[k] for k in (
    'protocol', 'request_id', 'artifact_sha256', 'base_identity',
    'suite_sha256', 'dataset_sha256', 'scope')}
report.update(passed=True, safety_passed=True,
              capabilities=request['capabilities'])
report['contracts'] = {
    c: {'positive_cases': 20, 'paired_cases': 20,
        'baseline_passed': 18, 'candidate_passed': 19,
        'baseline_unsafe': 0, 'candidate_unsafe': 0}
    for c in request['capabilities']}
with open(a.report, 'w', encoding='utf-8') as h:
    json.dump(report, h)
"""


def _expected(module, **overrides):
    value = {
        "protocol": module.EVALUATION_PROTOCOL,
        "request_id": "request",
        "artifact_sha256": "b" * 64,
        "base_identity": module.base_identity(),
        "suite_sha256": "d" * 64,
        "dataset_sha256": "c" * 64,
        "scope": _scope(),
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("scope", [
    "tenant:plain", "deployment:" + "a" * 64, "global", "user:" + "A" * 64,
])
def test_scope_requires_one_exact_opaque_user_or_tenant_identity(tmp_path, scope):
    module = _load(tmp_path)
    with pytest.raises(SystemExit, match="STUDIO_TRAJECTORY_SCOPE"):
        module.validate_scope(scope)
    assert module.validate_scope(_scope()) == _scope()
    assert module.validate_scope(_scope("user", "b")) == _scope("user", "b")
    assert module.validate_scope(_scope("tenant", "c")) == _scope("tenant", "c")


def test_readable_scope_is_resolved_by_server_before_private_state(tmp_path, monkeypatch):
    module = _load(tmp_path)
    seen = {}

    def request(method, path, token=None, body=None):
        seen.update(method=method, path=path, token=token, body=body)
        return {"scope": _scope("tenant")}

    monkeypatch.setattr(module, "_req", request)
    assert module.resolve_scope("token", "tenant:acme-prod") == _scope("tenant")
    assert seen == {
        "method": "POST", "path": "/api/training/trajectories/resolve-scope",
        "token": "token", "body": {"scope": "tenant:acme-prod"},
    }
    assert "acme-prod" not in seen["path"]

    monkeypatch.setattr(
        module, "_req", lambda *args, **kwargs: pytest.fail(
            "an opaque scope must not need resolution"))
    assert module.resolve_scope("token", _scope()) == _scope()


def test_api_transport_ignores_ambient_proxies_and_forbids_redirects(
        tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    opener = module._build_api_opener()
    proxy_handlers = [handler for handler in opener.handlers
                      if isinstance(handler, module.urllib.request.ProxyHandler)]
    assert not proxy_handlers or all(handler.proxies == {} for handler in proxy_handlers)

    redirect = next(handler for handler in opener.handlers
                    if isinstance(handler, module._NoRedirectHandler))
    request = module.urllib.request.Request(
        "https://studio.example/api/training/trajectories",
        headers={"Authorization": "Bearer admin-secret"})
    with pytest.raises(module.urllib.error.HTTPError, match="redirects are forbidden"):
        redirect.redirect_request(
            request, None, 302, "Found", {}, "https://attacker.invalid/steal")


def test_trajectory_pagination_refuses_readable_scope_in_query_string(
        tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setattr(
        module, "_req", lambda *args, **kwargs: pytest.fail(
            "a readable scope must never reach a GET query"))
    with pytest.raises(SystemExit, match="explicit opaque"):
        module.pull_trajectories("token", "user:person@example.com", 0)


def test_canonical_deployment_environment_and_script_compile(tmp_path, monkeypatch):
    module = _load(tmp_path, {
        "STUDIO_TRAJECTORY_OUTPUT_DIR": str(tmp_path / "canonical"),
        "STUDIO_TRAJECTORY_BASE_MODEL": "example/policy-base",
        "STUDIO_TRAJECTORY_BASE_REVISION": "a" * 40,
        "STUDIO_TRAJECTORY_BASE_SHA256": "b" * 64,
        "STUDIO_TRAJECTORY_SCOPE": "tenant:acme-prod",
    })
    assert module.OUT_DIR == str(tmp_path / "canonical")
    assert module.BASE_MODEL == "example/policy-base"
    assert module.base_identity() == {
        "training_model": "example/policy-base",
        "training_revision": "a" * 40,
        "serving_sha256": "b" * 64,
    }
    monkeypatch.setenv("STUDIO_TRAJECTORY_SCOPE", "tenant:acme-prod")
    assert module.configured_scope() == "tenant:acme-prod"
    compile(_SCRIPT.read_text(encoding="utf-8"), str(_SCRIPT), "exec")


def test_real_round_requires_full_immutable_base_identity_before_polling(
        tmp_path, monkeypatch):
    module = _load(tmp_path, {"STUDIO_TRAJECTORY_BASE_REVISION": "short",
                              "STUDIO_TRAJECTORY_BASE_SHA256": ""})
    monkeypatch.setattr(
        module, "pull_trajectories",
        lambda *args, **kwargs: pytest.fail("invalid base identity must fail before polling"))
    with pytest.raises(SystemExit, match="BASE_REVISION"):
        module.run_once("token", _scope(), tokenizer=FakeTokenizer())


def test_policy_tokenizer_and_shared_training_use_exact_revision(tmp_path, monkeypatch):
    module = _load(tmp_path)
    calls = []

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(model, **kwargs):
            calls.append(("tokenizer", model, kwargs))
            return object()

    transformers = type(sys)("transformers")
    transformers.AutoTokenizer = AutoTokenizer
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    module.load_tokenizer()

    class Shared:
        MAX_LENGTH = None
        MAX_PROMPT_LENGTH = None
        DPO_BETA = None

        def train_lora(self, samples, model, out_dir, epochs, **kwargs):
            calls.append(("sft", model, kwargs))
            return "adapter", {}

        def train_dpo(self, pairs, model, out_dir, epochs, **kwargs):
            calls.append(("dpo", model, kwargs))
            return "adapter", {}

    monkeypatch.setattr(module, "_online", lambda: Shared())
    sample = {"system": "s", "prompt": "p", "completion": "{}", "reward": 1.0,
              "contract": module.CONTRACTS[0]}
    pair = {**sample, "chosen": "{}", "rejected": '{"x":1}', "margin": 1.0}
    module.train_sft([sample])
    module.train_dpo([pair])

    assert calls == [
        ("tokenizer", module.BASE_MODEL, {"revision": module.BASE_REVISION}),
        ("sft", module.BASE_MODEL,
         {"adapter_kind": module.ADAPTER_KIND, "allow_prompt_truncation": False,
          "base_revision": module.BASE_REVISION}),
        ("dpo", module.BASE_MODEL,
         {"adapter_kind": module.ADAPTER_KIND,
          "base_revision": module.BASE_REVISION}),
    ]


def test_remote_http_is_fail_closed_without_explicit_private_network_opt_in(tmp_path):
    module = _load(tmp_path)
    module.API = "http://studio-web:8000"
    with pytest.raises(SystemExit, match="requires HTTPS"):
        module._secure_api_url()
    allowed = _load(tmp_path, {"STUDIO_TRAJECTORY_ALLOW_INSECURE_HTTP": "1"})
    allowed.API = "http://studio-web:8000"
    assert allowed._secure_api_url() is None


def test_independent_evaluator_environment_excludes_studio_credentials(tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setenv("STUDIO_TRAINER_TOKEN", "secret-token")
    monkeypatch.setenv("STUDIO_TRAINER_PASSWORD", "secret-password")
    monkeypatch.setenv("DATABASE_URL", "postgres://credential")
    monkeypatch.setenv("PATH", "/safe/bin")
    environment = module._evaluator_env()
    assert environment["PATH"] == "/safe/bin"
    assert "STUDIO_TRAINER_TOKEN" not in environment
    assert "STUDIO_TRAINER_PASSWORD" not in environment
    assert "DATABASE_URL" not in environment


def test_wire_row_requires_store_canonical_prompt_target_and_system(tmp_path):
    module = _load(tmp_path)
    original = _row(module, module.CONTRACTS[0])
    assert module.validate_wire_trajectory(original, _scope())["prompt"] == original["prompt"]

    for field, value, match in [
        ("scope", _scope("tenant"), "crossed"),
        ("system", "similar but not canonical", "system version"),
        ("contract_version", 2, "contract version"),
        ("completion", '{"version": 1, "answer": "result 1"}', "canonical JSON"),
    ]:
        changed = dict(original, **{field: value})
        with pytest.raises(SystemExit, match=match):
            module.validate_wire_trajectory(changed, _scope())

    envelope = json.loads(original["prompt"])
    envelope["unexpected"] = True
    changed = dict(original, prompt=json.dumps(envelope, sort_keys=True, separators=(",", ":")))
    with pytest.raises(SystemExit, match="canonical input envelope"):
        module.validate_wire_trajectory(changed, _scope())


def test_page_fails_closed_on_scope_and_cursor(tmp_path):
    module = _load(tmp_path)
    rows = _five(module)
    with pytest.raises(SystemExit, match="requested scope"):
        module.validate_page({"scope": _scope("tenant"), "trajectories": rows,
                              "cursor": 5, "count": len(rows)}, _scope(), 0)
    with pytest.raises(SystemExit, match="cover"):
        module.validate_page({"scope": _scope(), "trajectories": rows,
                              "cursor": 4, "count": len(rows)}, _scope(), 0)


def test_byte_bounded_short_pages_are_drained_without_skipping_large_rows(
        tmp_path, monkeypatch):
    module = _load(tmp_path)
    first = _row(module, module.CONTRACTS[0], 1, prompt="a" * 200_000, revision=1)
    second = _row(module, module.CONTRACTS[1], 2, prompt="b" * 200_000, revision=2)
    calls = []

    def pull(token, scope, since, limit=100):
        calls.append(since)
        if since == 0:
            return {"scope": scope, "trajectories": [first], "cursor": 1,
                    "count": 1, "has_more": True}
        return {"scope": scope, "trajectories": [second], "cursor": 2,
                "count": 1, "has_more": False}

    monkeypatch.setattr(module, "pull_trajectories", pull)
    rows, cursor, pages = module.pull_pages("token", _scope(), 0, limit=100)
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert (cursor, pages, calls) == (2, 2, [0, 1])


def test_all_five_trainer_refuses_tenant_scope_before_polling(tmp_path, monkeypatch):
    module = _load(tmp_path)
    monkeypatch.setattr(module, "pull_trajectories",
                        lambda *args, **kwargs: pytest.fail("tenant scope must fail before polling"))
    with pytest.raises(SystemExit, match="requires a user scope"):
        module.run_once("token", _scope("tenant"), tokenizer=FakeTokenizer())


def test_peft_publication_requires_explicit_compatible_runtime_opt_in(
        tmp_path, monkeypatch):
    module = _load(tmp_path, {"STUDIO_TRAJECTORY_ALLOW_PEFT_PUBLICATION": ""})
    monkeypatch.setattr(module, "pull_trajectories",
                        lambda *args, **kwargs: pytest.fail("refuse before consuming data"))
    with pytest.raises(SystemExit, match="PEFT directory and is disabled"):
        module.run_once("token", _scope(), tokenizer=FakeTokenizer())


def test_private_cursor_pending_and_replay_are_scoped_atomic_mode_0600(tmp_path):
    module = _load(tmp_path)
    rows = _five(module)
    module.save_state(_scope(), 5, rows)
    module.save_replay(_scope(), rows)
    assert module.load_state(_scope()) == (5, rows)
    assert module.load_replay(_scope()) == rows
    for path in module.state_paths(_scope()).values():
        if os.path.exists(path):
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert module.state_paths(_scope())["state"] != \
        module.state_paths(_scope("tenant"))["state"]


def test_corrupt_or_cross_scope_checkpoint_fails_closed(tmp_path):
    module = _load(tmp_path)
    path = module.state_paths(_scope())["state"]
    os.makedirs(tmp_path, exist_ok=True)
    Path(path).write_text('{"version":1,"scope":"user:' + "b" * 64 +
                          '","cursor":1,"pending":[]}', encoding="utf-8")
    with pytest.raises(SystemExit, match="wrong version or scope"):
        module.load_state(_scope())


def test_merge_replaces_only_newer_revision_and_never_evicts(tmp_path):
    module = _load(tmp_path)
    old = _row(module, module.CONTRACTS[0], revision=2)
    stale = dict(old, revision=1, reward=-1.0)
    fresh = dict(old, revision=3, reward=0.75)
    assert module.merge_rows([old], [stale], 2) == [old]
    assert module.merge_rows([old], [fresh], 2) == [fresh]
    equivocated = dict(old, reward=0.25)
    with pytest.raises(SystemExit, match="without a newer revision"):
        module.merge_rows([old], [equivocated], 2)
    with pytest.raises(SystemExit, match="nothing was evicted"):
        module.merge_rows([old], [_row(module, module.CONTRACTS[1], 2)], 1)


def test_full_context_preflight_rejects_whole_row_without_truncation(tmp_path):
    module = _load(tmp_path)
    short = _row(module, module.CONTRACTS[0])
    long = _row(module, module.CONTRACTS[1], 2, prompt="x" * 4000)
    accepted, dropped = module.preflight_full_context(
        [short, long], FakeTokenizer(), max_length=1000)
    assert [row["id"] for row in accepted] == [short["id"]]
    assert dropped == {"agent_graph:too_long": 1}
    assert accepted[0]["token_count"] == (
        accepted[0]["prompt_token_count"] + accepted[0]["completion_token_count"])


def test_balancer_requires_and_equalizes_all_five_contracts(tmp_path):
    module = _load(tmp_path)
    rows = _five(module) + [
        _row(module, module.CONTRACTS[0], 20, revision=20),
        _row(module, module.CONTRACTS[0], 21, revision=21),
    ]
    balanced, counts = module.balance_contracts(rows)
    assert len(balanced) == 5
    assert counts == {contract: 1 for contract in module.CONTRACTS}
    missing, counts = module.balance_contracts(rows[:-3])
    assert missing == []
    assert counts[module.CONTRACTS[-1]] == 0


def test_balancer_fails_closed_when_required_pending_quota_exceeds_cap(tmp_path):
    module = _load(tmp_path)
    rows = []
    for index, contract in enumerate(module.CONTRACTS):
        rows.extend([
            _row(module, contract, index * 10 + 1),
            _row(module, contract, index * 10 + 2),
        ])

    balanced, counts = module.balance_contracts(
        rows, per_contract_cap=1, required_items=rows,
        min_required_per_contract=2)

    assert balanced == []
    assert counts == {contract: 2 for contract in module.CONTRACTS}


def test_dataset_digest_is_deterministic_and_binds_complete_target(tmp_path):
    module = _load(tmp_path)
    rows = _five(module)
    digest = module.dataset_sha256(rows, "sft")
    assert digest == module.dataset_sha256(rows, "sft")
    changed = [*rows]
    changed[0] = dict(changed[0], completion='{"different":true}')
    assert module.dataset_sha256(changed, "sft") != digest


def test_dpo_pairs_only_same_contract_prompt_and_reward_margin(tmp_path):
    module = _load(tmp_path, {"STUDIO_TRAJECTORY_TRAIN_MODE": "dpo"})
    rows = []
    for index, contract in enumerate(module.CONTRACTS):
        chosen = _row(module, contract, index * 10 + 1, reward=1.0, prompt="same")
        rejected = _row(
            module, contract, index * 10 + 2, reward=0.0, prompt="same",
            target={"answer": "worse", "version": 1})
        rows.extend([dict(chosen, token_count=50), dict(rejected, token_count=50)])
    pairs = module.dpo_candidates(rows)
    balanced, counts = module.balance_contracts(pairs)
    assert len(pairs) == len(module.CONTRACTS)
    assert len(balanced) == 5
    assert counts == {contract: 1 for contract in module.CONTRACTS}
    assert all(pair["margin"] == 1.0 for pair in pairs)


def test_dpo_never_pairs_distinct_case_sensitive_canonical_inputs(tmp_path):
    module = _load(tmp_path, {"STUDIO_TRAJECTORY_TRAIN_MODE": "dpo"})
    upper = dict(
        _row(module, module.CONTRACTS[0], 1, reward=1.0, prompt="Read Sales"),
        token_count=50)
    lower = dict(
        _row(module, module.CONTRACTS[0], 2, reward=0.0, prompt="read sales",
             target={"answer": "different input", "version": 1}),
        token_count=50)

    assert upper["prompt"] != lower["prompt"]
    assert module.dpo_candidates([upper, lower]) == []


def test_dpo_consumption_and_digest_bind_exact_source_revisions(tmp_path):
    module = _load(tmp_path, {"STUDIO_TRAJECTORY_TRAIN_MODE": "dpo"})
    contract = module.CONTRACTS[0]
    old_rows = [
        dict(_row(module, contract, 1, revision=1, reward=1.0, prompt="same"),
             token_count=50),
        dict(_row(
            module, contract, 2, revision=2, reward=0.0, prompt="same",
            target={"answer": "rejected", "version": 1}), token_count=50),
    ]
    pair = module.dpo_candidates(old_rows)[0]
    newer_pending = [dict(row, revision=row["revision"] + 10) for row in old_rows]

    consumed, retained = module.partition_pending_for_dataset(
        newer_pending, [pair], "dpo")

    assert consumed == []
    assert retained == newer_pending
    changed = dict(pair, trajectory_identities=[
        {"id": item["id"], "revision": item["revision"] + 10}
        for item in pair["trajectory_identities"]
    ])
    assert module.dataset_sha256([pair], "dpo") != \
        module.dataset_sha256([changed], "dpo")

    legacy = dict(pair)
    legacy.pop("trajectory_identities")
    with pytest.raises(SystemExit, match="exact source"):
        module.partition_pending_for_dataset(newer_pending, [legacy], "dpo")
    with pytest.raises(SystemExit, match="exact source"):
        module.dataset_sha256([legacy], "dpo")


def test_independent_evaluation_requires_exact_five_capabilities_and_point_nine(tmp_path):
    module = _load(tmp_path)
    evidence = module.validate_evaluation_report(
        _report(module), _expected(module), _config(module))
    assert evidence["passed"] is True
    assert evidence["safety_passed"] is True
    assert evidence["capabilities"] == list(module.CONTRACTS)
    assert evidence["contracts"][module.CONTRACTS[0]]["candidate_passed"] == 19

    missing = _report(module)
    missing["contracts"].pop(module.CONTRACTS[-1])
    with pytest.raises(SystemExit, match="exact evidence"):
        module.validate_evaluation_report(missing, _expected(module), _config(module))

    below = _report(module, passed=17)
    with pytest.raises(SystemExit, match="0.90"):
        module.validate_evaluation_report(below, _expected(module), _config(module))


def test_evaluator_process_is_bound_to_exact_candidate_dataset_scope_and_suite(tmp_path):
    module = _load(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "adapter.bin").write_bytes(b"candidate")
    config = _config(module)
    config["command"] = [sys.executable, "-c", _EVALUATOR]
    evidence = module.evaluate_candidate(
        str(candidate), "c" * 64, _scope(), "sft", config)
    assert evidence["artifact_sha256"] == module._tree_sha256(str(candidate))
    assert evidence["dataset_sha256"] == "c" * 64
    assert evidence["scope"] == _scope()
    assert evidence["capabilities"] == list(module.CONTRACTS)
    assert not list(tmp_path.glob(".trajectory-eval-*"))


@pytest.mark.parametrize("mutation,match", [
    ("identity", "identity"),
    ("unpaired", "paired evidence"),
    ("unsafe", "did not assert"),
    ("empty", "too few"),
])
def test_independent_evaluation_refuses_forged_or_unsafe_evidence(
        tmp_path, mutation, match):
    module = _load(tmp_path)
    report = _report(module)
    contract = module.CONTRACTS[0]
    if mutation == "identity":
        report["dataset_sha256"] = "f" * 64
    elif mutation == "unpaired":
        report["contracts"][contract]["paired_cases"] = 19
    elif mutation == "unsafe":
        report["safety_passed"] = False
    else:
        report["contracts"][contract] = {
            "positive_cases": 0, "paired_cases": 0,
            "baseline_passed": 0, "candidate_passed": 0,
            "baseline_unsafe": 0, "candidate_unsafe": 0}
    with pytest.raises(SystemExit, match=match):
        module.validate_evaluation_report(report, _expected(module), _config(module))


def _stub_pull(module, monkeypatch, rows, cursor=None):
    monkeypatch.setattr(module, "pull_trajectories", lambda token, scope, since, limit=100: {
        "scope": scope, "trajectories": rows,
        "cursor": cursor if cursor is not None else max((r["revision"] for r in rows), default=since),
        "count": len(rows), "has_more": False,
    })


@pytest.mark.parametrize("mode,per_contract_cap", [("sft", 2), ("dpo", 1)])
def test_saturated_replay_reserves_pending_candidates_before_clearing_state(
        tmp_path, monkeypatch, mode, per_contract_cap):
    module = _load(tmp_path, {
        "STUDIO_TRAJECTORY_TRAIN_MODE": mode,
        "STUDIO_TRAJECTORY_TRAIN_MAX_PER_CONTRACT": str(per_contract_cap),
    })
    # Make the historical candidates rank ahead of all pending candidates.
    # The unconstrained balancer therefore reproduces the previously trained
    # digest and demonstrates the exact full-cap failure mode deterministically.
    monkeypatch.setattr(module, "_stable_rank", lambda item: item["id"])
    old_rows, pending_rows = [], []
    revision = 1
    for contract in module.CONTRACTS:
        if mode == "sft":
            for suffix in ("a", "b"):
                old_rows.append(dict(
                    _row(module, contract, revision, revision=revision),
                    id=f"a_old_{contract}_{suffix}"))
                revision += 1
            for prefix in ("z", "zz"):
                pending_rows.append(dict(
                    _row(module, contract, revision, revision=revision),
                    id=f"{prefix}_pending_{contract}"))
                revision += 1
            if contract == module.CONTRACTS[0]:
                pending_rows.append(dict(
                    _row(module, contract, revision, revision=revision, reward=0.0),
                    id="retained_below_reward"))
                revision += 1
            if contract == module.CONTRACTS[1]:
                pending_rows.append(dict(
                    _row(module, contract, revision, revision=revision,
                         prompt="x" * 5000),
                    id="retained_overlength"))
                revision += 1
        else:
            prompt = f"historical preference for {contract}"
            old_rows.extend([
                dict(_row(
                    module, contract, revision, revision=revision, reward=1.0,
                    prompt=prompt,
                    target={"answer": "historical chosen", "version": 1}),
                    id=f"a_old_{contract}_chosen"),
                dict(_row(
                    module, contract, revision + 1, revision=revision + 1,
                    reward=0.0, prompt=prompt,
                    target={"answer": "historical rejected", "version": 1}),
                    id=f"b_old_{contract}_rejected"),
            ])
            revision += 2
            for pair_index, prefix in enumerate(("z", "zz"), start=1):
                prompt = f"pending preference {pair_index} for {contract}"
                pending_rows.extend([
                    dict(_row(
                        module, contract, revision, revision=revision, reward=1.0,
                        prompt=prompt,
                        target={"answer": f"pending chosen {pair_index}", "version": 1}),
                        id=f"{prefix}_pending_{contract}_{pair_index}_chosen"),
                    dict(_row(
                        module, contract, revision + 1, revision=revision + 1,
                        reward=0.0, prompt=prompt,
                        target={"answer": f"pending rejected {pair_index}", "version": 1}),
                        id=f"{prefix}q_pending_{contract}_{pair_index}_rejected"),
                ])
                revision += 2
            if contract == module.CONTRACTS[0]:
                pending_rows.append(dict(
                    _row(module, contract, revision, revision=revision, reward=1.0,
                         prompt="unpaired pending preference",
                         target={"answer": "no rejected sibling", "version": 1}),
                    id="retained_unpaired"))
                revision += 1

    old_usable, _ = module.preflight_full_context(old_rows, FakeTokenizer())
    old_candidates = module.dpo_candidates(old_usable) if mode == "dpo" \
        else module.sft_candidates(old_usable)
    old_balanced, _ = module.balance_contracts(old_candidates)
    old_digest = module.dataset_sha256(old_balanced, mode)
    module.save_replay(_scope(), old_rows)
    _stub_pull(module, monkeypatch, pending_rows, cursor=revision)

    adapter = tmp_path / f"{mode}-candidate"
    adapter.mkdir()
    (adapter / "adapter.bin").write_bytes(b"weights")
    selected = {}

    def train(items):
        selected["items"] = items
        return str(adapter), {"loss": 0.1}

    monkeypatch.setattr(module, "train_dpo" if mode == "dpo" else "train_sft", train)
    artifact = "b" * 64
    monkeypatch.setattr(module, "_tree_sha256", lambda path: artifact)
    monkeypatch.setattr(module, "evaluate_candidate", lambda *args: {
        "artifact_sha256": artifact,
    })
    monkeypatch.setattr(module, "publish_adapter", lambda token, scope, uri, sha, metrics: {
        "scope": scope, "kind": module.ADAPTER_KIND, "uri": uri,
        "sha256": sha, "version": 1, "base_model": module.BASE_MODEL,
        "base_identity": module.base_identity(),
    })

    result = module.run_once("token", _scope(), tokenizer=FakeTokenizer())

    trained = selected["items"]
    pending_ids = {row["id"] for row in pending_rows}
    if mode == "sft":
        consumed_ids = pending_ids.intersection(item["id"] for item in trained)
    else:
        consumed_ids = pending_ids.intersection(
            identifier for item in trained for identifier in item["trajectory_ids"])
    retained_ids = pending_ids - consumed_ids
    assert result["published"] is True
    assert result["dataset_sha256"] != old_digest
    assert result["selected_new_counts"] == {
        contract: 1 for contract in module.CONTRACTS}
    assert result["contract_counts"] == {
        contract: per_contract_cap for contract in module.CONTRACTS}
    if mode == "sft":
        assert len(consumed_ids) == len(module.CONTRACTS)
        assert {"retained_below_reward", "retained_overlength"} <= retained_ids
    else:
        assert len(consumed_ids) == 2 * len(module.CONTRACTS)
        assert "retained_unpaired" in retained_ids
    assert result["consumed_pending"] == len(consumed_ids)
    assert result["retained_pending"] == len(retained_ids)
    assert result["pending_consumption_applied"] is True
    saved_cursor, saved_pending = module.load_state(_scope())
    assert saved_cursor == revision
    assert {row["id"] for row in saved_pending} == retained_ids


def test_round_preflights_then_balances_digests_evaluates_and_publishes(
        tmp_path, monkeypatch):
    module = _load(tmp_path)
    rows = _five(module)
    _stub_pull(module, monkeypatch, rows)
    order = []
    real_preflight = module.preflight_full_context
    real_balance = module.balance_contracts
    real_digest = module.dataset_sha256
    monkeypatch.setattr(module, "preflight_full_context",
                        lambda *args, **kwargs: (order.append("preflight") or
                                                real_preflight(*args, **kwargs)))
    monkeypatch.setattr(module, "balance_contracts",
                        lambda *args, **kwargs: (order.append("balance") or
                                                real_balance(*args, **kwargs)))
    monkeypatch.setattr(module, "dataset_sha256",
                        lambda *args, **kwargs: (order.append("digest") or
                                                real_digest(*args, **kwargs)))
    adapter = tmp_path / "candidate"
    adapter.mkdir()
    (adapter / "adapter.bin").write_bytes(b"weights")
    monkeypatch.setattr(module, "train_sft", lambda samples: (str(adapter), {"loss": 0.1}))
    artifact = "b" * 64
    monkeypatch.setattr(module, "_tree_sha256", lambda path: artifact)
    monkeypatch.setattr(module, "evaluate_candidate", lambda *args: {
        "protocol": module.EVALUATION_PROTOCOL, "passed": True,
        "safety_passed": True, "artifact_sha256": artifact,
        "dataset_sha256": args[1], "scope": args[2],
        "base_identity": module.base_identity(), "suite_sha256": "d" * 64,
        "capabilities": list(module.CONTRACTS),
        "contracts": {contract: {"positive_cases": 20, "paired_cases": 20,
                                  "baseline_passed": 18, "candidate_passed": 19,
                                  "baseline_unsafe": 0, "candidate_unsafe": 0}
                      for contract in module.CONTRACTS},
    })
    published = {}
    monkeypatch.setattr(module, "publish_adapter", lambda token, scope, uri, sha, metrics: (
        published.update(scope=scope, uri=uri, sha=sha, metrics=metrics) or {
            "scope": scope, "kind": module.ADAPTER_KIND, "uri": uri,
            "sha256": sha, "version": 1, "base_model": module.BASE_MODEL,
            "base_identity": module.base_identity()}))

    result = module.run_once("token", _scope(), tokenizer=FakeTokenizer())

    assert result["published"] is True
    assert order.index("preflight") < order.index("balance") < order.index("digest")
    assert published["scope"] == _scope()
    assert published["metrics"]["capabilities"] == list(module.CONTRACTS)
    assert module.load_state(_scope()) == (5, [])
    assert len(module.load_replay(_scope())) == 5


def test_failed_evaluation_keeps_pending_and_never_publishes(tmp_path, monkeypatch):
    module = _load(tmp_path)
    rows = _five(module)
    _stub_pull(module, monkeypatch, rows)
    adapter = tmp_path / "candidate"
    adapter.mkdir()
    (adapter / "adapter.bin").write_bytes(b"weights")
    monkeypatch.setattr(module, "train_sft", lambda samples: (str(adapter), {}))
    monkeypatch.setattr(module, "_tree_sha256", lambda path: "b" * 64)
    monkeypatch.setattr(module, "evaluate_candidate",
                        lambda *args: (_ for _ in ()).throw(SystemExit("failed eval")))
    monkeypatch.setattr(module, "publish_adapter",
                        lambda *args: pytest.fail("failed candidate was published"))
    with pytest.raises(SystemExit, match="failed eval"):
        module.run_once("token", _scope(), tokenizer=FakeTokenizer())
    assert module.load_state(_scope()) == (5, rows)


def test_dry_run_does_not_advance_private_cursor(tmp_path, monkeypatch):
    module = _load(tmp_path)
    rows = _five(module)
    _stub_pull(module, monkeypatch, rows)
    monkeypatch.setattr(module, "load_tokenizer",
                        lambda: pytest.fail("dry-run must not import/load ML dependencies"))
    result = module.run_once("token", _scope(), dry_run=True)
    assert result["dry_run"] is True
    assert result["token_preflight"].startswith("deferred")
    assert module.load_state(_scope()) == (0, [])
    assert module.load_replay(_scope()) == []


def test_deferred_peft_release_binds_cursor_dataset_and_pending(tmp_path, monkeypatch):
    module = _load(tmp_path)
    rows = _five(module)
    _stub_pull(module, monkeypatch, rows)
    adapter = tmp_path / "candidate"
    adapter.mkdir()
    (adapter / "adapter.bin").write_bytes(b"weights")
    monkeypatch.setattr(module, "train_sft", lambda samples: (str(adapter), {"loss": 0.1}))
    monkeypatch.setattr(module, "_tree_sha256", lambda path: "b" * 64)

    result = module.run_once(
        "token", _scope(), defer_publish=True, tokenizer=FakeTokenizer())

    manifest = module._load_manifest(_scope())
    assert result["published"] is False
    assert result["pending_consumption_applied"] is False
    assert result["consumed_pending"] == 5
    assert result["retained_pending"] == 0
    assert manifest["cursor"] == 5
    assert manifest["dataset_sha256"] == result["dataset_sha256"]
    assert len(manifest["pending"]) == 5
    assert len(manifest["consumed_pending"]) == 5
    assert manifest["pending_sha256"] == module._pending_state_sha256(rows)
    assert module.load_state(_scope()) == (5, rows)


def _passing_metrics(module, scope, artifact, dataset):
    report = _report(module, artifact=artifact, dataset=dataset, scope=scope)
    expected = _expected(module, artifact_sha256=artifact,
                         dataset_sha256=dataset, scope=scope)
    evidence = module.validate_evaluation_report(report, expected, _config(module))
    promoted = {
        **evidence,
        "thresholds": {"min_cases": 5, "min_candidate_pass_rate": 0.9,
                       "candidate_no_worse_than_baseline": True},
        "contracts": {
            contract: {**counts, "candidate_pass_rate":
                       counts["candidate_passed"] / counts["paired_cases"]}
            for contract, counts in evidence["contracts"].items()
        },
    }
    return {
        "scope": scope,
        "dataset_sha256": dataset,
        "base_identity": module.base_identity(),
        "capabilities": list(module.CONTRACTS),
        "evaluation": evidence,
        "promotion_evidence": promoted,
    }


def test_manual_ack_verifies_active_final_artifact_and_evidence_before_clearing(
        tmp_path, monkeypatch):
    module = _load(tmp_path)
    rows = _five(module)
    module.save_state(_scope(), 5, rows)
    dataset, artifact = "c" * 64, "e" * 64
    module._manifest(
        _scope(), cursor=5, pending=rows, consumed_pending=rows,
        adapter_dir=str(tmp_path / "peft"),
        artifact_sha256="b" * 64, dataset_digest=dataset, mode="sft",
        counts={contract: 1 for contract in module.CONTRACTS}, metrics={})
    metrics = _passing_metrics(module, _scope(), artifact, dataset)
    monkeypatch.setattr(module, "active_adapter", lambda token, scope: {
        "scope": scope, "kind": module.ADAPTER_KIND, "uri": "https://models/policy.gguf",
        "version": 7, "sha256": artifact, "base_model": module.BASE_MODEL,
        "base_identity": module.base_identity(),
        "status": "active", "metrics": metrics,
    })

    result = module.acknowledge_published_release(
        "token", _scope(), "https://models/policy.gguf", 7, artifact)

    assert result["acknowledged"] is True
    assert result["cleared_pending"] == 5
    assert result["consumed_pending"] == 5
    assert result["retained_pending"] == 0
    assert module.load_state(_scope()) == (5, [])
    assert not os.path.exists(module.state_paths(_scope())["release"])


def test_deferred_ack_consumes_only_rows_in_the_balanced_dataset(tmp_path, monkeypatch):
    module = _load(tmp_path)
    selected = _five(module)
    below_threshold = dict(
        _row(module, module.CONTRACTS[0], 99, revision=99, reward=0.0),
        id="retained_below_threshold")
    rows = [*selected, below_threshold]
    _stub_pull(module, monkeypatch, rows, cursor=99)
    adapter = tmp_path / "candidate-retention"
    adapter.mkdir()
    (adapter / "adapter.bin").write_bytes(b"weights")
    monkeypatch.setattr(module, "train_sft", lambda samples: (str(adapter), {"loss": 0.1}))
    monkeypatch.setattr(module, "_tree_sha256", lambda path: "b" * 64)

    deferred = module.run_once(
        "token", _scope(), defer_publish=True, tokenizer=FakeTokenizer())
    manifest = module._load_manifest(_scope())

    assert deferred["consumed_pending"] == 5
    assert deferred["retained_pending"] == 1
    assert manifest["pending"] == [module._pending_identity(row) for row in rows]
    assert manifest["consumed_pending"] == [
        module._pending_identity(row) for row in selected]
    assert module.load_state(_scope()) == (99, rows)

    final_artifact = "e" * 64
    metrics = _passing_metrics(
        module, _scope(), final_artifact, deferred["dataset_sha256"])
    monkeypatch.setattr(module, "active_adapter", lambda token, scope: {
        "scope": scope, "kind": module.ADAPTER_KIND,
        "uri": "https://models/policy.gguf", "version": 8,
        "sha256": final_artifact, "base_model": module.BASE_MODEL,
        "base_identity": module.base_identity(), "status": "active",
        "metrics": metrics,
    })

    acknowledged = module.acknowledge_published_release(
        "token", _scope(), "https://models/policy.gguf", 8, final_artifact)

    assert acknowledged["consumed_pending"] == 5
    assert acknowledged["retained_pending"] == 1
    assert acknowledged["cleared_pending"] == 5
    assert module.load_state(_scope()) == (99, [below_threshold])
    assert not os.path.exists(module.state_paths(_scope())["release"])


@pytest.mark.parametrize("mutation,match", [
    ("artifact", "release identity"),
    ("base", "release identity"),
    ("dataset", "retained five-capability dataset"),
    ("capability", "exact five-contract evidence"),
    ("cursor", "changed since deferred"),
    ("content", "changed since deferred"),
    ("consumption", "exact pending subset"),
])
def test_manual_ack_mismatch_never_consumes_pending(tmp_path, monkeypatch, mutation, match):
    module = _load(tmp_path)
    rows = _five(module)
    module.save_state(_scope(), 5, rows)
    dataset, artifact = "c" * 64, "e" * 64
    module._manifest(
        _scope(), cursor=5, pending=rows, consumed_pending=rows,
        adapter_dir=str(tmp_path / "peft"),
        artifact_sha256="b" * 64, dataset_digest=dataset, mode="sft",
        counts={contract: 1 for contract in module.CONTRACTS}, metrics={})
    metrics = _passing_metrics(module, _scope(), artifact, dataset)
    adapter_sha = "f" * 64 if mutation == "artifact" else artifact
    if mutation == "dataset":
        metrics["dataset_sha256"] = "f" * 64
    if mutation == "capability":
        metrics["promotion_evidence"]["contracts"].pop(module.CONTRACTS[-1])
    if mutation == "cursor":
        module.save_state(_scope(), 6, rows)
    if mutation == "content":
        module.save_state(_scope(), 5, [dict(rows[0], reward=0.75), *rows[1:]])
    if mutation == "consumption":
        manifest = module._load_manifest(_scope())
        manifest["consumed_pending"].append({"id": "not-pending", "revision": 999})
        monkeypatch.setattr(module, "_load_manifest", lambda scope: manifest)
    active_base = ({**module.base_identity(), "serving_sha256": "9" * 64}
                   if mutation == "base" else module.base_identity())
    monkeypatch.setattr(module, "active_adapter", lambda token, scope: {
        "scope": scope, "kind": module.ADAPTER_KIND, "uri": "https://models/policy.gguf",
        "version": 7, "sha256": adapter_sha, "base_model": module.BASE_MODEL,
        "base_identity": active_base,
        "status": "active", "metrics": metrics,
    })
    with pytest.raises(SystemExit, match=match):
        module.acknowledge_published_release(
            "token", _scope(), "https://models/policy.gguf", 7, artifact)
    retained = module.load_state(_scope())[1]
    assert len(retained) == len(rows)
    if mutation != "content":
        assert retained == rows


def test_shared_sft_feature_path_can_forbid_any_prompt_truncation():
    spec = importlib.util.spec_from_file_location("online_no_truncation", _ONLINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sample = {"system": "s" * 100, "prompt": "p" * 100, "completion": "{}"}
    with pytest.raises(ValueError, match="trajectory truncation is disabled"):
        module._completion_features(
            FakeTokenizer(), sample, max_length=64, allow_prompt_truncation=False)


def test_shared_training_entrypoints_default_to_existing_tool_call_kind():
    spec = importlib.util.spec_from_file_location("online_signature", _ONLINE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.train_lora.__defaults__ == ("tool_call", True)
    assert module.train_dpo.__defaults__ == ("tool_call",)
    assert module.train_lora.__kwdefaults__ == {"base_revision": None}
    assert module.train_dpo.__kwdefaults__ == {"base_revision": None}
