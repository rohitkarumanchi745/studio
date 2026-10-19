"""Activity steps say what they are doing and where.

A step records its kind (querying, reading a schema, drawing a chart, a
certified metric, …) and, for work on a source, the source, its type (the
connection's type, or a built-in source's own name) and its engine — so the
chat and the D3 & artifacts window show that database's icon. An unknown kind
is dropped; a source that cannot be resolved keeps just its name.
"""
import importlib
import json
import time

import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_SECRET", "progress-kinds-secret")
    monkeypatch.setenv("STUDIO_SQL_ALLOW_LOCAL_FILES", "1")
    from app import db as _db
    importlib.reload(_db)
    from app import chat, connections, progress
    from app.connectors import demo
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    _db.init_db()
    chat.init_tables()
    connections.init_tables()
    with _db.connect() as c:
        c.execute("INSERT INTO chat_tasks (id, conversation_id, user_id, status, seen, steps, "
                  "created_at) VALUES ('t1', 'c1', 'u1', 'running', 0, '[]', ?)", (time.time(),))
        c.commit()

    def steps():
        with _db.connect() as c:
            return json.loads(c.execute("SELECT steps FROM chat_tasks WHERE id='t1'").fetchone()["steps"])
    return type("Env", (), {"progress": progress, "connections": connections, "steps": staticmethod(steps),
                            "tmp": tmp_path})


def test_a_step_on_a_source_carries_its_type_and_engine(env):
    from app.connectors import get_connector
    env.progress.emit_for("t1", "Demo agent: running SQL on demo", agent="Demo agent",
                          kind="query", source=get_connector("demo"))
    env.progress.emit_for("t1", "rendering a bar chart", kind="chart")
    env.progress.emit_for("t1", "something odd", kind="teleport")
    env.progress.emit_for("t1", "Ghost agent: failed", kind="error", source="no_such_source")
    first, second, third, fourth = env.steps()
    assert first["kind"] == "query" and first["source"] == "demo"
    assert first["source_type"] == "demo" and first["engine"] == "sqlite"
    assert second == {**second, "kind": "chart"} and "source" not in second
    assert "kind" not in third                                      # unknown kinds are dropped
    assert fourth["kind"] == "error" and fourth["source"] == "no_such_source"
    assert "source_type" not in fourth


def test_a_saved_connection_reports_its_type(env):
    import sqlite3
    path = env.tmp / "shop.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE orders (id INTEGER)")
    admin = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
    env.connections.create_connection(env.connections.ConnIn(
        name="shop", ctype="sql", config={"url": f"sqlite:///{path}"}), user=admin)
    described = env.progress.describe("shop")
    assert described == {"source": "shop", "source_type": "sql", "engine": "sqlite"}
