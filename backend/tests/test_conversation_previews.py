"""Recent cards draw each conversation's real latest chart.

GET /api/conversations/previews returns, for each of up to eight conversations
the user can see, the latest assistant answer that has rows to draw — its
chart, columns and up to PREVIEW_ROWS rows — read the way the thread is
(redacted for the reader at read time). It never marks a conversation read,
never answers for one the user cannot see, and skips answers whose rows
expired.

Run from the backend directory:
    python -m pytest tests/test_conversation_previews.py -q
"""
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_DEMO_MODE", "1")
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    from app import db as _db
    importlib.reload(_db)
    import app.main as main
    importlib.reload(main)
    from app import chat, db
    with TestClient(main.app) as c:
        def login(email, password):
            tok = c.post("/api/auth/login", json={"email": email, "password": password}).json()["access_token"]
            return {"Authorization": f"Bearer {tok}"}
        ns = type("Env", (), {})()
        ns.c, ns.db, ns.chat = c, db, chat
        ns.analyst = login("analyst@studio.local", "analyst123")
        ns.admin = login("admin@studio.local", "admin123")
        ns.me = c.get("/api/auth/me", headers=ns.analyst).json()
        yield ns


def _conversation(env, user_id, *answers):
    cid = env.db.create_conversation(user_id, "🎨 a visual")
    env.db.add_message(cid, "user", {"text": "q"})
    for a in answers:
        env.db.add_message(cid, "assistant", a)
    return cid


ROWS = [[f"R{i}", i] for i in range(600)]
BAR = {"type": "bar", "x": "region", "y": ["revenue"]}
# Answers as the agent stores them: a source and the SQL that produced the rows
# (read-time redaction checks the reader may still run it).
SALES = {"source": "demo", "table": "sales", "sql": "SELECT region, SUM(revenue) FROM sales GROUP BY region"}


def test_the_latest_drawable_chart_capped_and_nothing_marked_read(env):
    me = env.me["id"]
    older = {**SALES, "text": "first", "columns": ["a"], "rows": [[1]], "chart": {"type": "kpi"}}
    latest = {**SALES, "text": "second", "columns": ["region", "revenue"], "rows": ROWS, "chart": BAR,
              "panels": [{**SALES, "columns": ["region", "revenue"], "rows": ROWS, "chart": BAR}]}
    cid = _conversation(env, me, older, latest)
    with env.db.connect() as c:
        seen_before = dict(c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone())
    out = env.c.get(f"/api/conversations/previews?ids={cid}", headers=env.analyst).json()
    assert out[cid]["chart"] == BAR and out[cid]["columns"] == ["region", "revenue"]
    assert len(out[cid]["rows"]) == env.chat.PREVIEW_ROWS == 400 and out[cid]["row_count"] == 600
    with env.db.connect() as c:
        seen_after = dict(c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone())
    assert seen_after == seen_before                                   # a glance is not a read


def test_expired_rows_are_skipped_and_others_conversations_absent(env):
    me = env.me["id"]
    drawable = {**SALES, "columns": ["a"], "rows": [[1]], "chart": {"type": "kpi"}}
    expired = {**SALES, "columns": ["a"], "rows": [], "rows_purged": True, "chart": {"type": "bar"}}
    cid = _conversation(env, me, drawable, expired)                  # falls back to the earlier one
    nothing = _conversation(env, me, {"text": "just words"})
    admins = _conversation(env, env.c.get("/api/auth/me", headers=env.admin).json()["id"], drawable)
    out = env.c.get(f"/api/conversations/previews?ids={cid},{nothing},{admins},nope",
                    headers=env.analyst).json()
    assert list(out) == [cid] and out[cid]["chart"] == {"type": "kpi"}
    assert env.c.get("/api/conversations/previews", headers=env.analyst).json() == {}


def test_rows_the_reader_may_not_see_are_not_drawn(env):
    # A viewer may not read `customers`: a chart over it is withheld at read
    # time, so Recent has nothing to draw for it either.
    viewer = env.c.post("/api/auth/login", json={"email": "viewer@studio.local",
                                                 "password": "viewer123"}).json()["access_token"]
    vid = env.c.get("/api/auth/me", headers={"Authorization": f"Bearer {viewer}"}).json()["id"]
    cid = _conversation(env, vid, {"source": "demo", "table": "customers", "sql": "SELECT name FROM customers",
                                   "columns": ["name"], "rows": [["Ana"]], "chart": {"type": "table"}})
    out = env.c.get(f"/api/conversations/previews?ids={cid}",
                    headers={"Authorization": f"Bearer {viewer}"}).json()
    assert out == {}
