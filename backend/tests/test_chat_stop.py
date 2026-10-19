"""Stop, as in ChatGPT: a running chat turn can be stopped.

Proves, through the real API with the worker held back so the turn is still
running when Stop is pressed:
  - Stop answers the question at once with a "stopped" reply, marks the task
    stopped, and the worker's answer — produced later — is refused (the reply
    slot is taken) and the task stays stopped;
  - a read the stopped turn tries to make is refused at the gateway;
  - Stop is idempotent; an answer that already landed stays (the task reads
    done); only the task's owner can stop it.

Run from the backend directory:
    python -m pytest tests/test_chat_stop.py -q
"""
import json
import warnings

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_DEMO_MODE", "1")
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")          # the turn waits in the queue
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM"):
        monkeypatch.delenv(k, raising=False)
    import importlib

    from app import db as _db
    importlib.reload(_db)
    import app.main as main
    importlib.reload(main)
    from app import chat, db, gateway, progress
    from app.connectors import demo
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "warehouse.db"))
    with TestClient(main.app) as c:
        def login(email, password):
            tok = c.post("/api/auth/login", json={"email": email, "password": password}
                         ).json()["access_token"]
            return {"Authorization": f"Bearer {tok}"}
        ns = type("Env", (), {})()
        ns.c, ns.chat, ns.db, ns.gateway, ns.progress = c, chat, db, gateway, progress
        ns.analyst = login("analyst@studio.local", "analyst123")
        ns.admin = login("admin@studio.local", "admin123")

        def start(prompt="revenue by region", headers=None):
            r = c.post("/api/chat/background", headers=headers or ns.analyst,
                       json={"prompt": prompt, "source": "demo", "table": "*"})
            assert r.status_code == 202, r.text
            return r.json()

        def job_for(tid):
            with db.connect() as conn:
                row = dict(conn.execute("SELECT * FROM background_jobs WHERE id=?",
                                        (chat._task_job_id(tid),)).fetchone())
            row["payload"] = json.loads(row["payload"])
            return row

        def task(tid):
            with db.connect() as conn:
                return dict(conn.execute("SELECT * FROM chat_tasks WHERE id=?", (tid,)).fetchone())

        ns.start, ns.job_for, ns.task = start, job_for, task
        yield ns


def messages(env, cid, headers=None):
    return env.c.get(f"/api/conversations/{cid}/messages", headers=headers or env.analyst).json()


def test_stop_answers_at_once_and_the_late_answer_is_refused(env):
    started = env.start()
    tid, cid = started["task_id"], started["conversation_id"]
    r = env.c.post(f"/api/tasks/{tid}/stop", headers=env.analyst)
    assert r.json() == {"status": "stopped", "task_id": tid}
    assert env.c.get(f"/api/tasks/{tid}", headers=env.analyst).json()["status"] == "stopped"
    replies = [m for m in messages(env, cid) if m["role"] == "assistant"]
    assert len(replies) == 1 and replies[0]["content"]["mode"] == "stopped"
    assert replies[0]["content"]["activity_task_id"] == tid
    # The worker gets to the turn later: it does not answer, and Stop stands.
    job = env.job_for(tid)
    out = env.chat._chat_turn_job(job["payload"], job)
    assert out["status"] in ("stopped", "done")
    replies = [m for m in messages(env, cid) if m["role"] == "assistant"]
    assert [m["content"]["mode"] for m in replies] == ["stopped"]
    assert env.task(tid)["status"] == "stopped"
    # Nothing new to tell the user: the chat is not marked unread.
    convs = {x["id"]: x for x in env.c.get("/api/conversations", headers=env.analyst).json()}
    assert convs[cid]["running"] == 0 and convs[cid]["unseen"] == 0


def test_an_answer_written_after_stop_is_discarded(env, monkeypatch):
    started = env.start()
    tid, cid = started["task_id"], started["conversation_id"]
    # Stop lands while the turn is already under way (past the worker's stop
    # check, a model call running): its answer then finds the reply slot taken
    # and is dropped, and the job ends as stopped — not as a lost claim.
    def turn_then_answer(ctx, user):
        env.c.post(f"/api/tasks/{tid}/stop", headers=env.analyst)
        return env.chat._answer(ctx, {"text": "late answer", "mode": "agent"})
    monkeypatch.setattr(env.chat, "_run_turn", turn_then_answer)
    job = env.job_for(tid)
    assert env.chat._chat_turn_job(job["payload"], job) == {"status": "stopped", "task_id": tid}
    assert [m["content"].get("mode") for m in messages(env, cid) if m["role"] == "assistant"] \
        == ["stopped"]
    assert env.task(tid)["status"] == "stopped"


def test_a_stopped_turn_cannot_query(env):
    started = env.start()
    tid = started["task_id"]
    user = {"id": env.c.get("/api/auth/me", headers=env.analyst).json()["id"],
            "role": "analyst", "email": "analyst@studio.local", "name": "Analyst"}
    env.progress.bind(tid)
    try:
        env.gateway.execute(user, "demo", "SELECT COUNT(*) FROM sales", "agent_sql")  # runs
        env.c.post(f"/api/tasks/{tid}/stop", headers=env.analyst)
        from app.connectors.base import QueryCancelled
        with pytest.raises(QueryCancelled, match="Stopped by the user"):
            env.gateway.execute(user, "demo", "SELECT COUNT(*) FROM sales", "agent_sql")
    finally:
        env.progress.bind(None)
    # Outside a task, reads are unaffected.
    assert env.gateway.execute(user, "demo", "SELECT COUNT(*) FROM sales", "rerun").rows


def test_stop_is_idempotent_an_answer_that_landed_stays_and_only_the_owner_stops(env):
    started = env.start()
    tid, cid = started["task_id"], started["conversation_id"]
    assert env.c.post(f"/api/tasks/{tid}/stop", headers=env.admin).status_code == 404
    env.c.post(f"/api/tasks/{tid}/stop", headers=env.analyst)
    assert env.c.post(f"/api/tasks/{tid}/stop", headers=env.analyst).json()["status"] == "stopped"
    assert sum(1 for m in messages(env, cid) if m["role"] == "assistant") == 1
    # The answer got there first: Stop finds nothing to stop.
    second = env.start("revenue by month")
    tid2, cid2 = second["task_id"], second["conversation_id"]
    turn = env.chat._task_turn(tid2)
    env.db.add_message(cid2, "assistant", {"text": "the answer", "mode": "agent"}, reply_to=turn[1])
    assert env.c.post(f"/api/tasks/{tid2}/stop", headers=env.analyst).json()["status"] == "done"
    replies = [m["content"] for m in messages(env, cid2) if m["role"] == "assistant"]
    assert [r["text"] for r in replies] == ["the answer"]
    # A finished outcome never overwrites a stop.
    env.chat._finish_task(tid, "done")
    assert env.task(tid)["status"] == "stopped"
