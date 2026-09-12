"""Chat platform commands remain explicit, owned, and admin supervised."""
import copy
import json

import pytest
from fastapi import HTTPException

from app import chat_platforms as service


ANALYST = {"id": "platform-chat-analyst", "role": "analyst", "email": "analyst@test.invalid"}
ADMIN = {"id": "platform-chat-admin", "role": "admin", "email": "admin@test.invalid"}
VIEWER = {"id": "platform-chat-viewer", "role": "viewer"}


def previous(**updates):
    return {"execution_mode": "platform_run", "target": "airflow", "label": "Apache Airflow",
            "payload": {"dag_id": "Old_DAG", "conf": {"mode": "old"}},
            "requested_by": ANALYST["id"], "status": "failed", "job_id": "old-job", **updates}


@pytest.fixture(autouse=True)
def no_external_side_effects(monkeypatch):
    for platform in service.platforms.PLATFORMS.values():
        monkeypatch.setattr(platform, "configured", lambda: True)
        monkeypatch.setattr(platform, "trigger", lambda *a: pytest.fail("chat triggered an external platform"))
        monkeypatch.setattr(platform, "status", lambda *a: pytest.fail("unexpected network status"))
    monkeypatch.setattr(service.supervisor, "submit", lambda *a, **k: pytest.fail("unexpected job submission"))


@pytest.mark.parametrize("prompt,target,payload", [
    ('Trigger Airflow DAG Daily_Sales with conf {"partition":"US East","count":2}',
     "airflow", {"dag_id": "Daily_Sales", "conf": {"partition": "US East", "count": 2}}),
    ('Please run Apache Airflow DAG "Daily_Sales.v2"', "airflow", {"dag_id": "Daily_Sales.v2"}),
    ("Can you trigger Airflow DAG `Daily_Sales`?", "airflow", {"dag_id": "Daily_Sales"}),
    ("Run Databricks job 123", "databricks_jobs", {"job_id": 123}),
    ("Run Databricks job_id=123", "databricks_jobs", {"job_id": 123}),
    ("Run Airflow dag_id:Daily_Sales", "airflow", {"dag_id": "Daily_Sales"}),
    ('Run Databricks Jobs job "123" with {"job_parameters":{"Date":"2026-09-11"}}',
     "databricks_jobs", {"job_id": 123, "job_parameters": {"Date": "2026-09-11"}}),
    ('Start dbt Cloud job 456 with {"cause":"Daily Revenue","git_branch":"Release"}',
     "dbt_cloud", {"job_id": 456, "cause": "Daily Revenue", "git_branch": "Release"}),
    ('Submit Spark on Kubernetes {"main_file":"local:///App.py","image":"Repo/Spark:Tag"}',
     "k8s_spark", {"main_file": "local:///App.py", "image": "Repo/Spark:Tag"}),
])
def test_direct_commands_preserve_user_identifiers_and_payload(prompt, target, payload):
    request = service.intent(prompt)
    assert request == {"action": "submit", "target": target, "payload": payload, "missing": [], "prompt": prompt}


@pytest.mark.parametrize("prompt", [
    "How do I trigger Airflow DAG Daily_Sales?", "Can Airflow run pipelines?",
    "Explain how to run Databricks job 123", "Don't trigger Airflow DAG Daily_Sales",
    "Run Airflow DAG Daily_Sales but do not execute it", "Never run it",
    "Run Airflow DAG Daily_Sales without approval", "Trigger Airflow DAG Daily unless tests fail",
    'The document says "trigger Airflow DAG Daily_Sales"', "Thanks", "Create a pipeline for revenue",
    "Build and run a pipeline for revenue", "Run this pipeline", "Run it",
])
def test_questions_negation_and_local_pipeline_commands_do_not_submit(prompt):
    assert service.intent(prompt) is None


def test_missing_identifiers_are_not_invented_or_inherited():
    for prompt in ("Trigger Airflow DAG", "Run this pipeline on Airflow", "Run Airflow"):
        request = service.intent(prompt)
        assert request["payload"] == {}
        assert any("dag_id" in m for m in request["missing"])
    for prompt in ("Run Databricks job", "Trigger dbt Cloud job"):
        assert any("job_id" in m for m in service.intent(prompt)["missing"])
    request = service.intent("Run Airflow DAG", previous())
    assert request["payload"] == {}
    assert request["missing"]
    request = service.intent("", previous(), target="airflow", action="submit")
    assert request["payload"] == {} and request["missing"]


def test_named_new_dag_does_not_reuse_old_conf():
    prior = previous()
    before = copy.deepcopy(prior)
    request = service.intent("Rerun Airflow DAG New_DAG", prior)
    assert request["payload"] == {"dag_id": "New_DAG"}
    assert prior == before


def test_rerun_reference_can_replace_conf_without_mutating_history():
    prior = previous()
    request = service.intent('Rerun it with conf {"mode":"corrected"}', prior)
    assert request["payload"] == {"dag_id": "Old_DAG", "conf": {"mode": "corrected"}}
    assert not request["missing"]
    assert prior["payload"]["conf"] == {"mode": "old"}
    assert service.intent("Run it", prior)["payload"] == prior["payload"]


def test_missing_input_can_be_filled_without_repeating_command():
    prior = previous(status="needs_input", payload={"conf": {"date": "today"}}, job_id=None)
    request = service.intent("DAG Actual_DAG", prior)
    assert request["payload"] == {"dag_id": "Actual_DAG", "conf": {"date": "today"}}
    assert not request["missing"]
    assert service.intent("DAG Actual_DAG", previous()) is None


def test_changing_platform_does_not_reuse_prior_payload():
    request = service.intent("Run it on Databricks", previous())
    assert request["target"] == "databricks_jobs"
    assert request["payload"] == {} and request["missing"]


@pytest.mark.parametrize("prompt", ["Check status", "Show me the run status", "Is it done?", "How is the pipeline?"])
def test_status_uses_previous_platform_artifact(prompt):
    prior = previous()
    request = service.intent(prompt, prior)
    assert request["action"] == "status" and request["artifact"] == prior


@pytest.mark.parametrize("prompt", [
    'Run Airflow DAG Sales with conf {"mode":',
    'Run Airflow DAG Sales with conf {"mode":"new"} and then run All_Users',
    'Run Airflow DAG Sales tomorrow',
    'Run Airflow DAG Sales with {"dag_id":"Different"}',
])
def test_unparsed_or_conflicting_arguments_need_input(prompt):
    assert service.intent(prompt)["missing"]


def test_negation_inside_json_is_data_not_command():
    request = service.intent('Trigger Airflow DAG Sales with conf {"note":"do not overwrite"}')
    assert request["payload"]["conf"]["note"] == "do not overwrite"
    assert not request["missing"]


@pytest.mark.parametrize("target,payload", [
    ("airflow", {"dag_id": "Sales", "conf": {"region": "North"}}),
    ("databricks_jobs", {"job_id": 123, "job_parameters": {"key": "value"}}),
    ("databricks_jobs", {"run_name": "Sales", "tasks": [{"task_key": "step", "existing_cluster_id": "abc",
                                                            "notebook_task": {"notebook_path": "/Repos/Sales"}}]}),
    ("dbt_cloud", {"job_id": 99, "steps_override": ["dbt build"], "git_branch": "main"}),
    ("k8s_spark", {"main_file": "local:///Jobs.py", "image": "repo/spark:1", "arguments": ["--date", "today"]}),
    ("k8s_spark", {"kind": "SparkApplication", "apiVersion": "sparkoperator.k8s.io/v1beta2",
                   "metadata": {"name": "sales"}, "spec": {"type": "Python", "mainApplicationFile": "local:///Jobs.py",
                   "image": "repo/spark:1", "sparkVersion": "4.0.4"}}),
])
def test_explicit_ui_supports_complete_existing_platform_payloads(target, payload):
    request = service.intent("", target=target, payload=payload, action="submit")
    assert request["payload"] == payload and not request["missing"]


@pytest.mark.parametrize("target,payload", [
    ("unknown", {}), ("airflow", {"dag_id": "../other"}), ("airflow", {"dag_id": "Sales", "conf": []}),
    ("databricks_jobs", {"job_id": True}), ("databricks_jobs", {"job_id": "123/evil"}),
    ("databricks_jobs", {"tasks": []}), ("databricks_jobs", {"tasks": [{"task_key": "made-up"}]}),
    ("databricks_jobs", {"tasks": [{"task_key": "one", "notebook_task": {"notebook_path": "/foo"}}], "job_id": 4}),
    ("dbt_cloud", {"job_id": 0}), ("dbt_cloud", {"job_id": 7, "steps_override": "dbt run"}),
    ("k8s_spark", {"main_file": "local:///app.py"}),
    ("k8s_spark", {"kind": "Pod", "spec": {}}),
    ("k8s_spark", {"main_file": "app.jar", "image": "spark", "type": "Scala"}),
    ("k8s_spark", {"main_file": "app.py", "image": "spark", "arguments": "--date"}),
])
def test_invalid_payloads_need_input_and_never_queue(target, payload):
    request = service.intent("", target=target, payload=payload, action="submit")
    artifact = service.submit(request, ANALYST, request_id="bad")
    assert artifact["status"] == "needs_input" and artifact["missing"]
    assert "job_id" not in artifact


def test_missing_credentials_return_configuration_card_without_queue(monkeypatch):
    monkeypatch.setattr(service.platforms.PLATFORMS["airflow"], "configured", lambda: False)
    artifact = service.submit(service.intent("Run Airflow DAG Sales"), ANALYST, request_id="unconfigured")
    assert artifact["status"] == "needs_configuration"
    assert "credentials" in artifact["missing"][0]
    assert "job_id" not in artifact


def test_viewer_cannot_submit_even_with_complete_payload():
    with pytest.raises(HTTPException) as err:
        service.submit(service.intent("Run Airflow DAG Sales"), VIEWER, request_id="forbidden")
    assert err.value.status_code == 403


def test_submission_passes_stable_id_and_never_approves_or_emails(monkeypatch):
    calls = []

    def submit(kind, target, script, user, *, job_id, notify, learning_context):
        calls.append((kind, target, script, user, job_id, notify))
        return {"id": job_id, "kind": kind, "target": target, "status": "awaiting_approval",
                "requester_email": "sensitive", "script": script, "result": {"secret": "hidden"},
                "supervisor_reasons": '["Human approval required"]'}

    monkeypatch.setattr(service.supervisor, "submit", submit)
    request = service.intent("Run Airflow DAG Sales")
    a = service.submit(request, ANALYST, request_id="request-1")
    b = service.submit(request, ANALYST, request_id="request-1")
    c = service.submit(request, ANALYST, request_id="request-2")
    assert a["job_id"] == b["job_id"] != c["job_id"]
    assert a["status"] == "awaiting_approval"
    assert calls[0][0:2] == ("platform_run", "airflow")
    assert json.loads(calls[0][2]) == {"dag_id": "Sales"}
    assert calls[0][-1] is False
    assert a["requested_by"] == ANALYST["id"]
    assert not {"script", "requester_email"} & a["job"].keys()
    assert a["job"]["result"] == {}
    assert a["job"]["supervisor_reasons"] == ["Human approval required"]


def test_service_revalidates_payload_even_if_caller_omits_missing():
    artifact = service.submit({"action": "submit", "target": "airflow", "payload": {}}, ANALYST, request_id="bad")
    assert artifact["status"] == "needs_input"


def test_refresh_polls_owned_job_and_keeps_terminal_truth(monkeypatch):
    job = {"id": "old-job", "kind": "platform_run", "target": "airflow", "status": "running"}
    monkeypatch.setattr(service.supervisor, "get_job", lambda jid, *, user: job)
    seen = []

    def live(jid, *, user):
        seen.append((jid, user))
        return {"job": {**job, "status": "failed"}, "state": "failed", "detail": "Task extract failed",
                "url": "https://airflow.example/run", "metrics": {"duration": 10}, "quality": [],
                "logs": "large log must not be attached"}

    monkeypatch.setattr(service.supervisor, "live_job", live)
    artifact = service.submit(service.intent("Check status", previous()), ANALYST, request_id="status")
    assert artifact["status"] == "failed" and artifact["state"] == "failed"
    assert artifact["detail"] == "Task extract failed"
    assert "logs" not in artifact
    assert seen == [("old-job", ANALYST)]


def test_refresh_enforces_ownership_and_never_polls_mismatched_target(monkeypatch):
    monkeypatch.setattr(service.supervisor, "live_job", lambda *a, **k: pytest.fail("polled unauthorized run"))
    with pytest.raises(HTTPException) as err:
        service.refresh(previous(), VIEWER)
    assert err.value.status_code == 404
    monkeypatch.setattr(service.supervisor, "get_job", lambda *a, **k: {"kind": "sql_script", "target": "airflow"})
    with pytest.raises(HTTPException) as err:
        service.refresh(previous(), ANALYST)
    assert err.value.status_code == 404


def test_status_without_prior_run_needs_input():
    artifact = service.submit(service.intent("Check Airflow status"), ANALYST, request_id="status")
    assert artifact["status"] == "needs_input" and "job_id" not in artifact


def test_status_of_another_platform_does_not_poll_previous_run():
    request = service.intent("Check Databricks status", previous())
    assert request["artifact"] is None
    assert request["target"] == "databricks_jobs"
    artifact = service.submit(request, ANALYST, request_id="other-status")
    assert artifact["status"] == "needs_input" and artifact["target"] == "databricks_jobs"


def test_excessively_long_numeric_identifier_needs_input():
    assert service.intent("Run Databricks job " + "1" * 5000)["missing"]


def test_invalid_actions_cannot_be_used_to_approve():
    with pytest.raises(HTTPException):
        service.intent("", target="airflow", payload={"dag_id": "Sales"}, action="approve")
    with pytest.raises(HTTPException):
        service.submit({"action": "approve", "target": "airflow", "payload": {"dag_id": "Sales"}}, ANALYST, request_id="bad")


def test_failed_run_followup_links_learning_context_without_modifying_payload(monkeypatch):
    prior = previous(job={"result": {"run_ref": "Old_DAG:external-run", "state": "failed"}})
    request = service.intent('Rerun it with conf {"mode":"fixed"}', prior)
    request["conversation_id"] = "conversation-1"
    assert request["repairs_run_id"] == "old-job:Old_DAG:external-run"
    captured = {}
    monkeypatch.setattr(service.supervisor, "get_job", lambda *a, **k: {
        "id": "old-job", "kind": "platform_run", "target": "airflow", "status": "failed",
        "result": {"run_ref": "Old_DAG:external-run"}})

    def submit(kind, target, script, user, **kwargs):
        captured.update(kwargs)
        assert json.loads(script) == {"dag_id": "Old_DAG", "conf": {"mode": "fixed"}}
        return {"id": kwargs["job_id"], "status": "awaiting_approval"}

    monkeypatch.setattr(service.supervisor, "submit", submit)
    service.submit(request, ANALYST, request_id="fixed")
    assert captured["learning_context"] == {"prompt": request["prompt"], "conversation_id": "conversation-1",
                                            "repairs_run_id": "old-job:Old_DAG:external-run"}
    assert "repairs_run_id" not in service.intent("Run Databricks job 123", prior)
    assert "repairs_run_id" not in service.intent("Run Airflow DAG Different_DAG", prior)
    assert "previous_artifact" not in service.intent("Run Airflow DAG Different_DAG", prior)


def test_repair_reads_terminal_state_even_if_chat_snapshot_is_stale(monkeypatch):
    prior = previous(status="running", job={"result": {"run_ref": "Old_DAG:external-run", "state": "running"}})
    request = service.intent('Rerun it with conf {"mode":"fixed"}', prior)
    assert "repairs_run_id" not in request
    monkeypatch.setattr(service.supervisor, "get_job", lambda *a, **k: {
        "id": "old-job", "kind": "platform_run", "target": "airflow", "status": "failed",
        "result": {"run_ref": "Old_DAG:external-run", "state": "failed"}})
    captured = {}

    def submit(kind, target, script, user, **kwargs):
        captured.update(kwargs)
        return {"id": kwargs["job_id"], "status": "awaiting_approval"}

    monkeypatch.setattr(service.supervisor, "submit", submit)
    service.submit(request, ANALYST, request_id="fixed")
    assert captured["learning_context"]["repairs_run_id"] == "old-job:Old_DAG:external-run"
