"""Platform chat uses real persisted supervisor jobs, never live services."""
import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, chat, db, governance, jobs, keys, supervisor
from app.auth import current_user


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "chat-platform.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)
    db.init_db()
    for module in (chat, governance, jobs, keys, supervisor):
        module.init_tables()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: False)
    monkeypatch.setattr(agent, "run_agent", lambda *a, **k: pytest.fail("Platform prompt reached data agent"))
    monkeypatch.setattr(chat, "connector_or_400", lambda *a, **k: pytest.fail("Platform prompt inspected a warehouse"))
    monkeypatch.setattr(chat, "_checkpoint", lambda *a, **k: None)
    monkeypatch.setattr(chat.lightning, "record_chat_trace", lambda *a, **k: None)
    monkeypatch.setattr(supervisor.email_service, "send", lambda *a, **k: pytest.fail("Chat sent an email"))
    for platform in supervisor.platforms.PLATFORMS.values():
        monkeypatch.setattr(platform, "configured", lambda: True)
        monkeypatch.setattr(platform, "trigger", lambda *a, **k: pytest.fail("Chat bypassed admin approval"))
        monkeypatch.setattr(platform, "status", lambda *a, **k: pytest.fail("No external status request expected"))
    user = db.get_user_by_email("analyst@studio.local")
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as c:
        c.user = user
        yield c
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)


def ask(client, prompt, cid=None, **kwargs):
    response = client.post("/api/chat", json={"prompt": prompt, "source": "offline-warehouse",
        "table": "not-loaded", "conversation_id": cid, **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def count_jobs():
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) n FROM supervised_jobs").fetchone()["n"]


def latest_assistant(cid):
    return [m for m in db.list_messages(cid) if m["role"] == "assistant"][-1]


def test_platform_prompt_bypasses_warehouse_and_waits_for_admin(client):
    answer = ask(client, 'Trigger Airflow DAG Daily_Sales with conf {"date":"2026-09-11"}')
    message = answer["message"]
    artifact = message["platform_run"]
    assert message["mode"] == "platform_run"
    assert artifact["status"] == "awaiting_approval"
    assert artifact["payload"] == {"dag_id": "Daily_Sales", "conf": {"date": "2026-09-11"}}
    assert "has not started" in message["text"]
    assert count_jobs() == 1
    row = supervisor._get(artifact["job_id"])
    assert row["human_by"] is None and row["attempts"] == 0
    assert row["kind"] == "platform_run" and row["target"] == "airflow"
    assert json.loads(row["script"]) == artifact["payload"]


def test_missing_dag_is_a_card_and_followup_resolves_it(client):
    answer = ask(client, "Trigger Airflow DAG")
    artifact = answer["message"]["platform_run"]
    assert artifact["status"] == "needs_input" and artifact["missing"]
    assert count_jobs() == 0
    updated = ask(client, "DAG Actual_Daily", answer["conversation_id"])
    artifact = updated["message"]["platform_run"]
    assert artifact["payload"] == {"dag_id": "Actual_Daily"}
    assert artifact["status"] == "awaiting_approval"
    assert count_jobs() == 1


def test_unconfigured_platform_does_not_queue(client, monkeypatch):
    monkeypatch.setattr(supervisor.platforms.PLATFORMS["airflow"], "configured", lambda: False)
    result = ask(client, "Trigger Airflow DAG Sales")
    assert result["message"]["platform_run"]["status"] == "needs_configuration"
    assert count_jobs() == 0


def test_readonly_sql_recipe_cannot_become_external_code(client):
    result = ask(client, "Trigger Airflow DAG")
    cid = result["conversation_id"]
    db.add_message(cid, "assistant", {"text": "Revenue SQL", "author_role": "analyst", "source": "demo",
        "table": "sales", "sql": "SELECT * FROM sales", "pipeline": {
        "execution_mode": "read_only_sql", "source": "demo", "steps": [
            {"source": "demo", "table": "sales", "sql": "SELECT * FROM sales", "verified": True}]}})
    updated = ask(client, "Run this pipeline on Airflow", cid)
    artifact = updated["message"]["pipeline"]
    assert artifact["execution_mode"] == "airflow_dag"
    assert artifact["status"] in ("needs_input", "blocked")
    assert count_jobs() == 0


def test_explicit_message_action_reuses_exact_older_platform_version(client):
    first = ask(client, 'Trigger Airflow DAG First with conf {"mode":"first"}')
    cid = first["conversation_id"]
    first_id = latest_assistant(cid)["id"]
    ask(client, 'Trigger Airflow DAG Second with conf {"mode":"second"}', cid)
    rerun = ask(client, "Run this pipeline", cid, platform_action="submit", platform_message_id=first_id)
    assert rerun["message"]["platform_run"]["payload"] == {"dag_id": "First", "conf": {"mode": "first"}}
    assert rerun["message"]["platform_run"]["status"] == "awaiting_approval"
    assert count_jobs() == 3


def test_explicit_platform_version_cannot_come_from_other_conversation(client):
    first = ask(client, "Trigger Airflow DAG First")
    first_id = latest_assistant(first["conversation_id"])["id"]
    second = ask(client, "Trigger Airflow DAG Second")
    response = client.post("/api/chat", json={"prompt": "Run this pipeline", "source": "offline-warehouse", "table": "*",
        "conversation_id": second["conversation_id"], "platform_action": "submit", "platform_message_id": first_id})
    assert response.status_code == 404
    assert count_jobs() == 2


def test_same_role_shared_editor_cannot_see_or_rerun_private_platform_artifact(client):
    first = ask(client, 'Trigger Airflow DAG Private_DAG with conf {"secret_name":"private-parameter"}')
    cid = first["conversation_id"]
    message_id = latest_assistant(cid)["id"]
    uid = db.create_user("other-analyst@test.invalid", "password-for-test", "Other Analyst", role="analyst")
    recipient = db.get_user(uid)
    db.share_conversation(cid, uid, "edit")
    client.app.dependency_overrides[current_user] = lambda: recipient
    messages = client.get(f"/api/conversations/{cid}/messages")
    assert messages.status_code == 200
    assistant = messages.json()[-1]["content"]
    assert assistant["redacted"] and "platform_run" not in assistant
    assert "private-parameter" not in messages.text
    assert "Private_DAG" not in messages.text
    listings = client.get("/api/conversations")
    assert listings.status_code == 200
    assert "private-parameter" not in listings.text and "Private_DAG" not in listings.text
    _, history = chat._conversation(cid, recipient, "New question")
    assert "private-parameter" not in json.dumps(history) and "Private_DAG" not in json.dumps(history)
    response = client.post("/api/chat", json={"prompt": "Run this pipeline", "conversation_id": cid, "source": "*", "table": "*",
        "platform_action": "submit", "platform_message_id": message_id})
    assert response.status_code == 403 and count_jobs() == 1


def test_viewer_is_denied_before_persisting_or_queuing(client):
    viewer = db.get_user_by_email("viewer@studio.local")
    client.app.dependency_overrides[current_user] = lambda: viewer
    response = client.post("/api/chat", json={"prompt": "Trigger Airflow DAG Sales", "source": "*", "table": "*"})
    assert response.status_code == 403 and count_jobs() == 0


def test_queued_turn_context_is_anchored_before_its_own_message(client):
    first = ask(client, "Trigger Airflow DAG First")
    cid = first["conversation_id"]
    body = chat.Ask(prompt="Rerun it", source="offline-warehouse", table="*", conversation_id=cid)
    _, mid = chat._record_user_turn(body, client.user)
    later = copy.deepcopy(first["message"])
    later["platform_run"]["payload"] = {"dag_id": "Later"}
    db.add_message(cid, "assistant", later)
    ctx = chat._build_ctx(body, client.user, cid, user_message_id=mid)
    assert ctx["platform_request"]["payload"] == {"dag_id": "First"}


def test_background_retry_after_submission_deduplicates_supervised_job(client, monkeypatch):
    queued = client.post("/api/chat/background", json={
        "prompt": "Trigger Airflow DAG Once", "source": "offline-warehouse", "table": "*"})
    assert queued.status_code == 202, queued.text
    queued = queued.json()
    job = jobs.get(chat._task_job_id(queued["task_id"]))
    answer = chat._answer
    monkeypatch.setattr(chat, "_answer", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash after submit")))
    with pytest.raises(RuntimeError, match="crash after submit"):
        chat._chat_turn_job(job["payload"], {**job, "attempts": 1})
    assert count_jobs() == 1
    monkeypatch.setattr(chat, "_answer", answer)
    chat._chat_turn_job(job["payload"], {**job, "attempts": 2})
    assert count_jobs() == 1
    artifact = latest_assistant(queued["conversation_id"])["content"]["platform_run"]
    assert artifact["status"] == "awaiting_approval"


def test_status_chat_reports_stored_approval_without_triggering(client):
    first = ask(client, "Trigger Airflow DAG Daily")
    refreshed = ask(client, "Check status", first["conversation_id"])
    artifact = refreshed["message"]["platform_run"]
    assert artifact["job_id"] == first["message"]["platform_run"]["job_id"]
    assert artifact["status"] == "awaiting_approval"
    assert count_jobs() == 1
