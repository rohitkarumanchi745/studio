"""Learned rules: guidance distilled from failed runs, live only once an admin approves.

This is the agent's procedural memory that changes without a training run
(Agent Lightning's APO idea: optimize the prompt, not the weights). An LLM
reads recent low-reward runs — SQL errors, rejected queries, thumbs-down —
and writes a short set of imperative rules ("aggregate before joining
web_traffic"). The approved set is the "Learned guidance" block in every
agent's system prompt.

Lifecycle, one table:

    proposed ──approve──▶ active ──(next approval / retire)──▶ superseded
        └──────reject──▶ rejected

  - The worker drafts proposals on its own (tick_once, lease-guarded like
    autopilot) once MIN_FAILURES new low-reward runs have accumulated since
    the last draft, so learning no longer waits for someone to run a script.
  - Nothing reaches a prompt without an admin. The evidence is every user's
    runs, and the output lands in every user's prompt, so a draft could
    quote one role's tables, filters or values to another. The distillation
    prompt asks for general rules; the approval step is what enforces it.
    An admin may edit a draft before approving it.
  - At most one active set (a partial unique index), so two concurrent
    approvals cannot both win. Each draft is a full replacement: the current
    active rules are part of its input, to be kept, revised or dropped.
  - Rules live in the database, not on local disk, so every web replica and
    worker serves the same set. A legacy prompts/system_learned.txt (written
    by older scripts/train_apo.py runs) is imported once as the active set.
"""
import logging
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user

log = logging.getLogger("studio.learned_rules")
router = APIRouter(prefix="/learned-rules", tags=["learned rules"])

MIN_FAILURES = int(os.getenv("STUDIO_LEARNED_RULES_MIN_FAILURES", "10"))
_TICK_SECONDS = int(os.getenv("STUDIO_LEARNED_RULES_TICK_SECONDS", "3600"))
_EVIDENCE_LIMIT = 40
_MAX_RULES, _MAX_RULE_CHARS = 8, 300
_LEGACY_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "system_learned.txt")


def init_tables():
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS learned_rules (
                id TEXT PRIMARY KEY,
                rules TEXT NOT NULL,
                status TEXT NOT NULL,         -- proposed | active | rejected | superseded
                evidence_count INTEGER NOT NULL DEFAULT 0,
                evidence_through REAL,        -- newest failure the draft saw
                model TEXT,
                created_at REAL NOT NULL,
                decided_at REAL,
                decided_by TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_learned_rules_one_active
                ON learned_rules(status) WHERE status='active';
            """
        )
        empty = c.execute("SELECT 1 FROM learned_rules LIMIT 1").fetchone() is None
    if empty:
        _import_legacy_file()


def _import_legacy_file():
    try:
        with open(_LEGACY_PATH, encoding="utf-8") as f:
            text = _sanitize(f.read())
    except OSError:
        return
    if not text:
        return
    now = time.time()
    with db.connect() as c:
        try:
            c.execute("INSERT INTO learned_rules (id, rules, status, model, created_at, "
                      "decided_at, decided_by) VALUES (?,?,?,?,?,?,?)",
                      (str(uuid.uuid4()), text, "active", "imported:system_learned.txt",
                       now, now, "import"))
            c.commit()
        except Exception:
            c.rollback()   # another replica imported it first


def _sanitize(text):
    """Keep only '- ' bullet lines, bounded in count and length."""
    rules = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            rules.append("- " + line[2:].strip()[:_MAX_RULE_CHARS])
        if len(rules) == _MAX_RULES:
            break
    return "\n".join(r for r in rules if r != "- ")


def _row(r):
    return {k: r[k] for k in ("id", "rules", "status", "evidence_count", "evidence_through",
                              "model", "created_at", "decided_at", "decided_by")}


# ── Drafting ─────────────────────────────────────────────────────────────
# The agent reads the active set through db.active_learned_rules(), not this
# module: drafting imports agent, so the read path must not import us back.

def _last_draft_through(c):
    r = c.execute("SELECT MAX(evidence_through) AS t FROM learned_rules").fetchone()
    return r["t"] or 0.0


def _draft_prompt(failures, current):
    evidence = "\n".join(
        f"- prompt: {(t['prompt'] or '')[:150]!r}\n  sql: {(t['sql'] or 'none')[:200]}\n"
        f"  error: {(t['error'] or 'none')[:200]}  reward: {t['reward']} ({t['reward_source']})"
        for t in failures)
    return (
        "You maintain the system prompt of a SQL analytics agent used by many people "
        "with different data access. Below are the agent's current learned rules and its "
        "recent low-reward runs (bad SQL, errors, user thumbs-down). Write the complete "
        f"new rule set: AT MOST {_MAX_RULES} short imperative rules that prevent these "
        "kinds of failures. Keep current rules that still apply, revise or drop the rest. "
        "Rules must be general: never quote user questions, customer or product names, "
        "literal filter values, or numbers from the runs. Output only the rules as '- ' "
        "bullet lines, no preamble.\n\n"
        f"Current rules:\n{current or '(none)'}\n\nRecent failures:\n{evidence}")


def draft(force=False):
    """Draft a proposal from low-reward runs newer than the last draft.
    Returns (proposal_row, None) or (None, reason). `force` drafts from the
    most recent failures even when too few are new (an admin's explicit ask)."""
    from . import agent
    spec = agent.llm_spec()
    if not agent.llm_available(spec):
        return None, "no LLM key is configured on the server"
    with db.connect() as c:
        since = _last_draft_through(c)
    failures = db.list_traces(limit=_EVIDENCE_LIMIT, max_reward=0.4)
    fresh = [t for t in failures if t["created_at"] > since]
    if not failures or (not force and len(fresh) < MIN_FAILURES):
        return None, (f"{len(fresh)} new low-reward runs since the last draft; "
                      f"{MIN_FAILURES} needed")
    reply = agent.make_llm(spec).invoke(_draft_prompt(failures, db.active_learned_rules()))
    rules = _sanitize(agent._reply_content(reply))
    if not rules:
        return None, "the model returned no usable rules"
    now = time.time()
    rid = str(uuid.uuid4())
    with db.connect() as c:
        # One pending draft at a time: an admin reviews the newest evidence.
        c.execute("UPDATE learned_rules SET status='superseded', decided_at=?, "
                  "decided_by='newer draft' WHERE status='proposed'", (now,))
        c.execute("INSERT INTO learned_rules (id, rules, status, evidence_count, "
                  "evidence_through, model, created_at) VALUES (?,?,?,?,?,?,?)",
                  (rid, rules, "proposed", len(failures),
                   max(t["created_at"] for t in failures), spec, now))
        c.commit()
        row = c.execute("SELECT * FROM learned_rules WHERE id=?", (rid,)).fetchone()
    return _row(row), None


def ticker_enabled():
    """STUDIO_LEARNED_RULES_TICKER kill-switch (default on)."""
    return os.getenv("STUDIO_LEARNED_RULES_TICKER", "1").lower() not in ("0", "false", "no")


def tick_once():
    """One scheduler pass (jobs.py, under the "learned_rules" lease): draft a
    proposal if enough new failures have accumulated. Never raises."""
    try:
        proposal, reason = draft()
        if proposal:
            log.info("learned rules: drafted proposal %s from %d runs",
                     proposal["id"], proposal["evidence_count"])
        else:
            log.debug("learned rules: no draft (%s)", reason)
    except Exception:
        log.exception("learned rules: drafting failed")


# ── Decisions ────────────────────────────────────────────────────────────

def approve(rule_id, admin_id, rules=None):
    """Make a proposal the active set, optionally with admin-edited text."""
    now = time.time()
    with db.connect() as c:
        r = c.execute("SELECT status, rules FROM learned_rules WHERE id=?", (rule_id,)).fetchone()
        if r is None:
            raise KeyError(rule_id)
        if r["status"] != "proposed":
            raise ValueError(f"only a proposed rule set can be approved (this one is {r['status']})")
        text = _sanitize(rules) if rules is not None else r["rules"]
        if not text:
            raise ValueError("the rules must be '- ' bullet lines")
        # Supersede + activate commit together or not at all: a proposal that
        # changed status meanwhile (or a concurrent approval tripping the
        # one-active index) must not leave the agent with no rules.
        try:
            c.execute("UPDATE learned_rules SET status='superseded', decided_at=?, decided_by=? "
                      "WHERE status='active'", (now, admin_id))
            activated = c.execute(
                "UPDATE learned_rules SET status='active', rules=?, decided_at=?, "
                "decided_by=? WHERE id=? AND status='proposed'",
                (text, now, admin_id, rule_id)).rowcount
        except Exception:
            activated = 0
        if activated != 1:
            c.rollback()
            raise RuntimeError("the rule sets changed while approving; reload and retry")
        c.commit()


def reject(rule_id, admin_id):
    with db.connect() as c:
        n = c.execute("UPDATE learned_rules SET status='rejected', decided_at=?, decided_by=? "
                      "WHERE id=? AND status='proposed'",
                      (time.time(), admin_id, rule_id)).rowcount
        c.commit()
    return n > 0


def retire(admin_id):
    """Take the active set out of every prompt without replacing it."""
    with db.connect() as c:
        n = c.execute("UPDATE learned_rules SET status='superseded', decided_at=?, decided_by=? "
                      "WHERE status='active'", (time.time(), admin_id)).rowcount
        c.commit()
    return n > 0


def overview(limit=20):
    with db.connect() as c:
        active = c.execute("SELECT * FROM learned_rules WHERE status='active'").fetchone()
        rest = c.execute("SELECT * FROM learned_rules WHERE status<>'active' "
                         "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"active": _row(active) if active else None,
            "history": [_row(r) for r in rest],
            "min_failures": MIN_FAILURES}


# ── API (admin only) ─────────────────────────────────────────────────────

def _admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admins only")
    return user


class Approval(BaseModel):
    rules: str | None = None


@router.get("")
def get_rules(user=Depends(current_user)):
    _admin(user)
    return overview()


@router.post("/draft")
def draft_now(user=Depends(current_user)):
    _admin(user)
    proposal, reason = draft(force=True)
    return {"proposal": proposal, "reason": reason}


@router.post("/{rule_id}/approve")
def approve_rules(rule_id: str, body: Approval | None = None, user=Depends(current_user)):
    _admin(user)
    try:
        approve(rule_id, user["id"], body.rules if body else None)
    except KeyError:
        raise HTTPException(404, "Unknown rule set")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return overview()


@router.post("/{rule_id}/reject")
def reject_rules(rule_id: str, user=Depends(current_user)):
    _admin(user)
    if not reject(rule_id, user["id"]):
        raise HTTPException(404, "No proposed rule set with that id")
    return overview()


@router.post("/retire")
def retire_rules(user=Depends(current_user)):
    _admin(user)
    retire(user["id"])
    return overview()
