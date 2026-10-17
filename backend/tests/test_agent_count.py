"""The UI can show how many agents a task is using — while it runs.

A finished answer already lists its agents (planners, workers, Aggregator).
While a task runs, the UI only had progress labels ("Snowflake agent: running
SQL…"); counting agents from prose would be guesswork. Each step now names the
agent that emitted it. Locked in here:
- a step carries `agent` when one acted, and none otherwise;
- every step a worker agent emits names that worker;
- the planning step names each planner, so they count too, and the Aggregator
  names itself.
"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-agent-count-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import agent, chat, db, orchestrator, progress
from app.connectors.demo import DemoConnector, seed

ANA = {"id": "u-ac-ana", "email": "ac-ana@studio.test", "role": "analyst", "name": "Ana"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    seed()
    yield


@pytest.fixture()
def task():
    tid = "task-agent-count"
    with db.connect() as c:
        c.execute("DELETE FROM chat_tasks WHERE id=?", (tid,))
        c.execute("INSERT INTO chat_tasks (id, conversation_id, user_id, prompt, status, steps, "
                  "created_at) VALUES (?,?,?,?,?,?,?)",
                  (tid, "c-ac", ANA["id"], "q", "running", "[]", 0))
        c.commit()
    progress.bind(tid)
    yield tid
    progress.bind(None)


def _steps(tid):
    with db.connect() as c:
        return json.loads(c.execute("SELECT steps FROM chat_tasks WHERE id=?", (tid,)).fetchone()["steps"])


def test_a_step_names_the_agent_that_acted(task):
    progress.emit("Snowflake agent: running SQL", agent="Snowflake agent")
    progress.emit("reading the question")
    first, second = _steps(task)
    assert first["agent"] == "Snowflake agent" and first["label"].startswith("Snowflake agent:")
    assert "agent" not in second


def test_every_step_a_worker_emits_names_it(task, monkeypatch):
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **kw: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})

    class _Msg:
        type = "ai"

        def __init__(self, text):
            self.content = text

    def fake_graph(llm, tools, system, spec, volatile=None):
        run_sql = next(t for t in tools if getattr(t, "name", "") == "run_sql")

        class G:
            def invoke(self, state, config=None):
                run_sql.invoke({"sql": "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1"})
                return {"messages": [_Msg("done")]}
        return G()

    monkeypatch.setattr(agent, "_graph", fake_graph)
    agent.run_agent("revenue by region", DemoConnector(), "sales", ["sales"], {"sales": []}, [], ANA)
    steps = _steps(task)
    assert len(steps) >= 2
    assert {s.get("agent") for s in steps} == {"Demo agent"}


def test_the_planners_and_the_aggregator_are_named_too(task):
    plan = {"nodes": [{"id": "a"}, {"id": "b"}], "formation": {"planners": [
        {"agent": "Source Mapper", "status": "valid", "selected": True},
        {"agent": "Dependency Planner", "status": "valid"},
        {"agent": "Minimal Graph Planner", "status": "invalid"}]}}
    orchestrator._announce_formation(plan)
    named = [s["agent"] for s in _steps(task) if s.get("agent")]
    assert named == ["Source Mapper", "Dependency Planner", "Minimal Graph Planner"]
    assert _steps(task)[-1]["label"] == "3 independent planner(s) formed a 2-agent graph"
    labels = [s["label"] for s in _steps(task)]
    assert "Source Mapper: proposed the graph that was chosen" in labels
    assert "Minimal Graph Planner: could not propose a usable graph" in labels
