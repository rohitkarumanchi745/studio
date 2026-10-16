"""Result-row retention is scheduled, monitored, and never removes history
without notice.

STUDIO_MESSAGE_ROWS_RETENTION_DAYS used to be applied once at every boot: the
next restart after it was set removed every older result row, unannounced
and unrecorded. Locked in here:
- boot never purges; a lease-guarded scheduler does, and every pass is
  recorded (status, eligible, purged, error) — a failure is recorded, not lost;
- the first purge that would remove existing history waits out a notice
  period; administrators can download exactly those rows, postpone, or
  confirm a backup to start sooner; lowering the retention later is again
  retroactive and gets a new notice;
- after that, rows are removed as they age; text, SQL and chart stay, and
  the message is marked rows_purged.
"""
import json
import os
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="studio-retention-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import chat, db, jobs, retention
from app.auth import current_user

ADMIN = {"id": "u-ret-admin", "email": "ret-admin@studio.test", "role": "admin", "name": "Ad"}
ANA = {"id": "u-ret-ana", "email": "ret-ana@studio.test", "role": "analyst", "name": "Ana"}
DAY = 86400


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    retention.init_tables()
    yield


def _wipe():
    with db.connect() as c:
        for t in ("messages", "conversations", "retention_runs", "retention_state"):
            c.execute(f"DELETE FROM {t}")
        c.commit()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # Before AND after: test modules can share one database, and an old answer
    # left here would be purged by the next module's retention test too.
    _wipe()
    monkeypatch.setenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS", "90")
    monkeypatch.setenv("STUDIO_RETENTION_NOTICE_DAYS", "7")
    yield
    _wipe()


def _answer(age_days, rows=(("EU", 125),)):
    cid = db.create_conversation(ANA["id"], "q")
    mid = db.add_message(cid, "assistant", {
        "text": "EU leads.", "sql": "SELECT region, revenue FROM sales", "columns": ["region", "revenue"],
        "rows": [list(r) for r in rows], "chart": {"type": "bar"},
        "panels": [{"sql": "SELECT 1", "columns": ["region"], "rows": [["EU"]], "chart": {"type": "bar"}}]})
    with db.connect() as c:
        c.execute("UPDATE messages SET created_at=? WHERE id=?", (time.time() - age_days * DAY, mid))
        c.commit()
    return mid


def _content(mid):
    with db.connect() as c:
        return json.loads(c.execute("SELECT content FROM messages WHERE id=?", (mid,)).fetchone()["content"])


def _runs():
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT status, eligible, purged, error FROM retention_runs ORDER BY started_at").fetchall()]


def test_off_by_default_and_boot_never_purges(monkeypatch):
    monkeypatch.delenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS")
    assert retention.tick_once() is None and not retention.ticker_enabled()
    # Retention on, an old answer, a fresh boot: its rows are still there.
    monkeypatch.setenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS", "30")
    old = _answer(400)
    import app.main as main
    main.init_state()
    assert _content(old)["rows"] == [["EU", 125]]
    assert retention.ticker_enabled()
    assert "row_retention" in {s["name"] for s in jobs._default_schedulers()}


def test_the_first_purge_waits_out_a_notice_then_runs():
    old, fresh = _answer(120), _answer(10)
    now = time.time()
    assert retention.tick_once(now) == "notice"
    assert _content(old)["rows"] == [["EU", 125]]                       # nothing removed yet
    st = retention.status()
    assert st["first_purge_pending"] and st["eligible"] == 1
    assert abs(st["first_purge_at"] - (now + 7 * DAY)) < 5
    assert retention.tick_once(now + 3 * DAY) == "notice"               # still waiting
    assert retention.tick_once(now + 8 * DAY) == "purged"               # notice is over
    gone = _content(old)
    assert gone["rows"] == [] and gone["panels"][0]["rows"] == [] and gone["rows_purged"] is True
    assert gone["text"] == "EU leads." and gone["sql"] and gone["chart"] == {"type": "bar"}
    assert _content(fresh)["rows"] == [["EU", 125]]
    assert [r["status"] for r in _runs()] == ["notice", "notice", "purged"]
    assert _runs()[-1]["purged"] == 1
    # From then on, rows go as they age — no new notice.
    later = _answer(95)
    assert retention.tick_once() == "purged" and _content(later)["rows_purged"] is True


def test_confirming_a_backup_starts_it_and_postponing_moves_it():
    old = _answer(200)
    retention.tick_once()
    first = retention.status()["first_purge_at"]
    out = retention.postpone(retention.Postpone(days=5), user=ADMIN)
    assert abs(out["first_purge_at"] - (first + 5 * DAY)) < 5
    assert retention.tick_once() == "notice" and _content(old)["rows"]
    out = retention.acknowledge(user=ADMIN)
    assert out["acknowledged_by"] == ADMIN["email"]
    assert retention.tick_once() == "purged" and _content(old)["rows_purged"] is True
    with pytest.raises(HTTPException):                                  # nothing waiting now
        retention.acknowledge(user=ADMIN)


def test_only_an_administrator_manages_it():
    _answer(200)
    retention.tick_once()
    for call in (lambda: retention.get_status(user=ANA), lambda: retention.acknowledge(user=ANA),
                 lambda: retention.postpone(retention.Postpone(days=3), user=ANA),
                 lambda: retention.backup(user=ANA)):
        with pytest.raises(HTTPException) as e:
            call()
        assert e.value.status_code == 403


def test_the_backup_is_exactly_what_the_purge_would_remove():
    old, fresh = _answer(150, rows=(("EU", 1), ("US", 2))), _answer(5)
    app = FastAPI()
    app.include_router(retention.router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: ADMIN
    r = TestClient(app).get("/api/admin/retention/backup")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert [line["message_id"] for line in lines] == [old]
    assert lines[0]["rows"] == [["EU", 1], ["US", 2]] and lines[0]["panels"][0]["rows"] == [["EU"]]
    assert any(a["action"] == "retention_backup" for a in db.list_activity(ADMIN["id"], limit=10))


def test_lowering_the_retention_is_retroactive_again(monkeypatch):
    _answer(120)
    now = time.time()
    retention.tick_once(now)
    assert retention.tick_once(now + 8 * DAY) == "purged"
    mid = _answer(45)                                  # inside 90 days, outside 30
    monkeypatch.setenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS", "30")
    assert retention.tick_once(now + 9 * DAY) == "notice"
    assert _content(mid)["rows"] and retention.status()["first_purge_pending"]


def test_with_nothing_old_enough_there_is_nothing_to_announce():
    _answer(10)
    assert retention.tick_once() == "nothing_to_purge"
    assert not retention.status()["first_purge_pending"]
    aged = _answer(91)                                 # rows ageing in later just expire
    assert retention.tick_once() == "purged" and _content(aged)["rows_purged"] is True


def test_a_failed_pass_is_recorded_not_raised(monkeypatch):
    _answer(10)
    retention.tick_once()                              # approved: nothing to announce
    _answer(100)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(chat, "purge_message_rows", boom)
    assert retention.tick_once() == "failed"
    assert _runs()[-1]["status"] == "failed" and "disk full" in _runs()[-1]["error"]
