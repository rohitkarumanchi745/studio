"""Chat result-row retention, staged so that turning it on surprises nobody.

STUDIO_MESSAGE_ROWS_RETENTION_DAYS (0 = keep forever) strips the stored result
rows from assistant messages older than N days; text, SQL, chart settings and
columns stay, and the UI shows "rows expired" with a re-run. That used to happen
once, at every boot: the next restart after the setting changed irreversibly
removed every older row, unannounced and unrecorded. Now:

- a lease-guarded scheduler (jobs._default_schedulers) runs the purge every
  STUDIO_RETENTION_TICK_SECONDS, never at boot, and records every pass in
  retention_runs (eligible, purged, status, error) — monitored, not silent;
- the FIRST purge that would remove existing history waits out a notice
  period (STUDIO_RETENTION_NOTICE_DAYS, default 7) from the pass that first
  found something to remove. Administrators see what will go and when, can
  download exactly those rows as a backup, postpone, or confirm a backup was
  taken to start sooner. Lowering the retention later (90 → 30 days) is again
  a retroactive purge and gets a new notice;
- once that first purge has happened, later passes remove rows as they age.
"""
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import chat, db
from .auth import current_user

log = logging.getLogger("studio.retention")

router = APIRouter(prefix="/admin/retention", tags=["retention"])

_TICK_SECONDS = int(os.getenv("STUDIO_RETENTION_TICK_SECONDS", "3600"))


def _notice_days():
    try:
        return max(0.0, float(os.getenv("STUDIO_RETENTION_NOTICE_DAYS", "7")))
    except ValueError:
        return 7.0


def init_tables():
    with db.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS retention_runs (
            id TEXT PRIMARY KEY, started_at REAL NOT NULL, finished_at REAL,
            days INTEGER NOT NULL, cutoff REAL NOT NULL, status TEXT NOT NULL,
            eligible INTEGER, purged INTEGER, error TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS retention_state (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()


def ticker_enabled():
    return (os.getenv("STUDIO_RETENTION_TICKER", "1").lower() not in ("0", "false", "no")
            and chat._retention_days() > 0)


# ── State ────────────────────────────────────────────────────────────────

def _state():
    with db.connect() as c:
        return {r["key"]: json.loads(r["value"])
                for r in c.execute("SELECT key, value FROM retention_state").fetchall()}


def _set(**values):
    with db.connect() as c:
        for key, value in values.items():
            if value is None:
                c.execute("DELETE FROM retention_state WHERE key=?", (key,))
            else:
                c.execute("INSERT INTO retention_state (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, json.dumps(value)))
        c.commit()


def _record(started, days, cutoff, status, eligible=None, purged=None, error=None):
    with db.connect() as c:
        c.execute("INSERT INTO retention_runs (id, started_at, finished_at, days, cutoff, status, "
                  "eligible, purged, error) VALUES (?,?,?,?,?,?,?,?,?)",
                  (str(uuid.uuid4()), started, time.time(), days, cutoff, status,
                   eligible, purged, (error or None) and str(error)[:500]))
        c.commit()
    return status


def _eligible(cutoff):
    count, oldest = 0, None
    for m in chat.expiring_messages(cutoff):
        count += 1
        oldest = m["created_at"] if oldest is None else min(oldest, m["created_at"])
    return count, oldest


# ── The scheduled pass ───────────────────────────────────────────────────

def tick_once(now=None):
    """One retention pass. Never raises — a failure is recorded and logged.
    Returns the recorded status (None when retention is off)."""
    days = chat._retention_days()
    if days <= 0:
        return None
    now = time.time() if now is None else now
    cutoff = now - days * 86400
    try:
        eligible, _oldest = _eligible(cutoff)
        state = _state()
        approved = state.get("approved_days")
        retroactive = approved is None or days < approved
        if retroactive:
            if eligible == 0:
                # Nothing old enough to remove: there is no history to announce,
                # and rows will now expire as they age.
                _set(approved_days=days, first_purge_at=None, acknowledged_by=None,
                     acknowledged_at=None)
                return _record(now, days, cutoff, "nothing_to_purge", eligible, 0)
            first = state.get("first_purge_at")
            if first is None or state.get("notice_for_days") != days:
                # A new notice — first time, or the retention changed since.
                first = now + _notice_days() * 86400
                _set(first_purge_at=first, notice_for_days=days, acknowledged_by=None,
                     acknowledged_at=None)
                log.warning("retention: %s messages' result rows (older than %s days) will be "
                            "removed from %s unless postponed", eligible, days,
                            time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(first)))
            if now < first and not state.get("acknowledged_at"):
                return _record(now, days, cutoff, "notice", eligible, 0)
        purged = chat.purge_message_rows(days, raise_errors=True)
        if retroactive:
            _set(approved_days=days, first_purge_at=None)
        log.info("retention: removed result rows from %s messages older than %s days",
                 purged, days)
        return _record(now, days, cutoff, "purged", eligible, purged)
    except Exception as e:
        log.exception("retention: pass failed")
        try:
            return _record(now, days, cutoff, "failed", error=e)
        except Exception:
            return "failed"


# ── Administration ───────────────────────────────────────────────────────

def _admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Only an administrator can manage data retention")


def status():
    days = chat._retention_days()
    state = _state()
    eligible, oldest = _eligible(time.time() - days * 86400) if days > 0 else (0, None)
    with db.connect() as c:
        runs = [dict(r) for r in c.execute(
            "SELECT started_at, finished_at, days, status, eligible, purged, error "
            "FROM retention_runs ORDER BY started_at DESC LIMIT 20").fetchall()]
    approved = state.get("approved_days")
    pending = days > 0 and eligible > 0 and (approved is None or days < approved)
    return {"days": days, "enabled": days > 0, "tick_seconds": _TICK_SECONDS,
            "notice_days": _notice_days(), "eligible": eligible, "oldest_eligible_at": oldest,
            "first_purge_pending": pending,
            "first_purge_at": state.get("first_purge_at") if pending else None,
            "acknowledged_by": state.get("acknowledged_by") if pending else None,
            "approved_days": approved, "runs": runs}


@router.get("")
def get_status(user=Depends(current_user)):
    _admin(user)
    return status()


@router.get("/backup")
def backup(user=Depends(current_user)):
    """Exactly the rows the next purge would remove, as JSON Lines — the
    backup to take before the first retroactive purge. Audited."""
    _admin(user)
    days = chat._retention_days()
    if days <= 0:
        raise HTTPException(400, "Retention is off: nothing would be removed")
    cutoff = time.time() - days * 86400
    db.log_activity(user, "retention_backup", prompt=f"rows older than {days} days")

    def lines():
        for m in chat.expiring_messages(cutoff):
            content = m["content"]
            yield json.dumps({
                "message_id": m["id"], "conversation_id": m["conversation_id"],
                "created_at": m["created_at"], "sql": content.get("sql"),
                "columns": content.get("columns"), "rows": content.get("rows"),
                "panels": [{k: p.get(k) for k in ("sql", "columns", "rows")}
                           for p in content.get("panels") or [] if isinstance(p, dict)],
            }, default=str) + "\n"

    name = time.strftime("studio-result-rows-%Y%m%d.jsonl", time.gmtime())
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/acknowledge")
def acknowledge(user=Depends(current_user)):
    """An administrator confirms a backup exists: the pending first purge may
    run on the next pass instead of waiting out the notice."""
    _admin(user)
    if not status()["first_purge_pending"]:
        raise HTTPException(400, "No first purge is waiting")
    _set(acknowledged_by=user.get("email") or user["id"], acknowledged_at=time.time())
    db.log_activity(user, "retention_acknowledge", prompt="backup confirmed")
    return status()


class Postpone(BaseModel):
    days: int = 7


@router.post("/postpone")
def postpone(body: Postpone, user=Depends(current_user)):
    _admin(user)
    if not 1 <= body.days <= 90:
        raise HTTPException(400, "Postpone by 1 to 90 days")
    current = status()
    if not current["first_purge_pending"]:
        raise HTTPException(400, "No first purge is waiting")
    start = max(time.time(), float(current["first_purge_at"] or 0))
    _set(first_purge_at=start + body.days * 86400, acknowledged_by=None, acknowledged_at=None)
    db.log_activity(user, "retention_postpone", prompt=f"{body.days} days")
    return status()
