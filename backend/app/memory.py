"""Agent memory about one user: saved notes (semantic) and past runs (episodic).

The agent's third kind of memory, procedural — how to do things — lives
elsewhere: learned rules (learned_rules.py), skill files (skills.py), cached
plans (qcache.py, pipeline_memory.py) and the BitNet adapter (trainer.py).

NOTES are facts the agent saved with its `remember` tool ("prefers bar
charts", "cares about the West region"). This module owns their lifecycle:

  - Write dedups. Remembering the same fact twice refreshes the existing note
    (keeping the newer wording) instead of stacking copies that crowd out
    everything else in the prompt.
  - Read ranks. Up to PROMPT_NOTES notes go into every system prompt. A user
    with that many or fewer gets all of them, newest first, exactly as before;
    past the cap, notes relevant to the current prompt win over merely recent
    ones (Harrier cosine when embeddings exist, term overlap otherwise).
  - The owner can see and delete them (GET/DELETE /api/memory), and the agent
    can drop a stale one with its `forget` tool. Every query is scoped by
    user_id; nobody, admins included, reads another user's notes here.
  - Storage is bounded: past MAX_NOTES the least recently refreshed go.

PAST RUNS are the user's own agent_traces. recall_runs() finds earlier
questions like the current one and returns what was asked, the SQL that
answered it, and how it went. Two invariants make that safe to hand a model:

  - Access is re-checked NOW. A run is returned only if its SQL still
    validates against the tables this user's role may read today (and the
    current table scope), so revoked access cannot resurface through history.
  - No result rows or answer text are returned — only the question and the
    SQL. The numbers of an old answer may be stale or since masked; the agent
    re-runs the SQL through the gateway to get current, governed values.
"""
import json
import os
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException

from . import db, embed, pipeline_memory
from .auth import current_user

router = APIRouter(prefix="/memory", tags=["memory"])

MAX_NOTES = int(os.getenv("STUDIO_MEMORY_MAX_NOTES", "200"))
PROMPT_NOTES = int(os.getenv("STUDIO_MEMORY_PROMPT_NOTES", "20"))
NOTE_CHARS = 500
# Same-fact thresholds. Lexical 0.85 keeps "prefers bar charts" and "prefers
# line charts" apart (3 of 5 terms shared = 0.6) while merging rephrasings
# that only add or drop filler words.
_DUP_JACCARD, _DUP_COSINE = 0.85, 0.95
_FORGET_JACCARD, _FORGET_COSINE = 0.5, 0.85
_RECALL_SCAN = 500
_STOP = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "to", "by", "from", "in", "on",
    "at", "with", "as", "is", "are", "was", "be", "it", "this", "that", "me",
    "my", "i", "you", "your", "please", "can", "could", "would", "show", "what",
    "how", "do", "does", "user", "their", "they",
})


def _clean(note):
    return " ".join(str(note or "").split())[:NOTE_CHARS]


def _norm(text):
    return _clean(text).casefold().rstrip(".!? ")


def _terms(text):
    return {w for w in re.findall(r"\w+", str(text or "").casefold())
            if len(w) > 1 and w not in _STOP}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def _vector(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _rows(c, user_id):
    return c.execute(
        "SELECT id, note, embedding, created_at, COALESCE(updated_at, created_at) AS ts "
        "FROM user_memory WHERE user_id=? ORDER BY ts DESC, id", (user_id,)).fetchall()


def _same_fact(clean, vec, row):
    if _norm(clean) == _norm(row["note"]):
        return True
    if vec is not None and (other := _vector(row["embedding"])) is not None:
        return embed.cosine(vec, other) >= _DUP_COSINE
    return _jaccard(_terms(clean), _terms(row["note"])) >= _DUP_JACCARD


# ── Notes ────────────────────────────────────────────────────────────────

def add_note(user_id, note):
    """Save a note, or refresh the one that already says it. Returns
    {"id", "note", "status": "saved" | "refreshed"}."""
    clean = _clean(note)
    if not clean:
        raise ValueError("empty note")
    vec = embed.embed(clean, kind="document") if embed.available() else None
    vec_json = json.dumps(vec) if vec else None
    now = time.time()
    with db.connect() as c:
        for row in _rows(c, user_id):
            if _same_fact(clean, vec, row):
                c.execute("UPDATE user_memory SET note=?, embedding=COALESCE(?, embedding), "
                          "updated_at=? WHERE id=? AND user_id=?",
                          (clean, vec_json, now, row["id"], user_id))
                c.commit()
                return {"id": row["id"], "note": clean, "status": "refreshed"}
        nid = str(uuid.uuid4())
        c.execute("INSERT INTO user_memory (id, user_id, note, embedding, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?)", (nid, user_id, clean, vec_json, now, now))
        # Bound storage: keep the MAX_NOTES most recently saved or refreshed.
        for row in _rows(c, user_id)[MAX_NOTES:]:
            c.execute("DELETE FROM user_memory WHERE id=? AND user_id=?", (row["id"], user_id))
        c.commit()
    return {"id": nid, "note": clean, "status": "saved"}


def list_notes(user_id):
    """The owner's notes, most recently saved or refreshed first."""
    with db.connect() as c:
        rows = _rows(c, user_id)
    return [{"id": r["id"], "note": r["note"], "created_at": r["created_at"],
             "updated_at": r["ts"]} for r in rows]


def delete_note(user_id, note_id):
    with db.connect() as c:
        n = c.execute("DELETE FROM user_memory WHERE id=? AND user_id=?",
                      (note_id, user_id)).rowcount
        c.commit()
    return n > 0


def clear_notes(user_id):
    with db.connect() as c:
        n = c.execute("DELETE FROM user_memory WHERE user_id=?", (user_id,)).rowcount
        c.commit()
    return n


def forget_matching(user_id, description):
    """Delete the ONE note that best matches `description` and return its
    text, or None when nothing matches closely enough. One at a time on
    purpose: a loose match must never wipe several unrelated notes."""
    want = _clean(description)
    if not want:
        return None
    with db.connect() as c:
        rows = _rows(c, user_id)
        q_vec = (embed.embed(want, kind="query")
                 if embed.available() and any(r["embedding"] for r in rows) else None)
        best, best_score = None, 0.0
        for r in rows:
            if _norm(want) == _norm(r["note"]):
                best, best_score = r, 2.0
                break
            other = _vector(r["embedding"])
            if q_vec is not None and other is not None:
                score = embed.cosine(q_vec, other)
                ok = score >= _FORGET_COSINE
            else:
                score = _jaccard(_terms(want), _terms(r["note"]))
                ok = score >= _FORGET_JACCARD
            if ok and score > best_score:
                best, best_score = r, score
        if best is None:
            return None
        c.execute("DELETE FROM user_memory WHERE id=? AND user_id=?", (best["id"], user_id))
        c.commit()
    return best["note"]


def notes_for_prompt(user_id, prompt, limit=None):
    """The notes to put in this turn's system prompt, newest first. All of
    them up to `limit`; past it, the ones most relevant to `prompt`."""
    limit = PROMPT_NOTES if limit is None else limit
    with db.connect() as c:
        rows = _rows(c, user_id)
    if len(rows) <= limit:
        return [r["note"] for r in rows]
    q_terms = _terms(prompt)
    q_vec = (embed.embed(prompt, kind="query")
             if embed.available() and any(r["embedding"] for r in rows) else None)

    def relevance(r):
        other = _vector(r["embedding"])
        if q_vec is not None and other is not None:
            return embed.cosine(q_vec, other)
        terms = _terms(r["note"])
        return len(q_terms & terms) / len(terms) if terms else 0.0

    # rows are newest first, so the index doubles as the recency tie-break.
    ranked = sorted(range(len(rows)), key=lambda i: (-relevance(rows[i]), i))[:limit]
    return [rows[i]["note"] for i in sorted(ranked)]


# ── Past runs ────────────────────────────────────────────────────────────

def _outcome(row):
    if row["reward_source"] == "user":
        return "user marked it helpful" if (row["reward"] or 0) >= 0.5 else "user marked it wrong"
    if not row["ok"] or row["error"]:
        return "ran with errors"
    return "ran successfully"


def recall_runs(user, query, *, source, table="*", exclude_conversation=None, limit=3):
    """This user's earlier questions on `source` that resemble `query`, as
    [{"asked_on", "question", "sql", "outcome"}], best match first. SQL that no
    longer passes today's access check is skipped, never returned."""
    q_terms = _terms(query)
    if not q_terms or not source:
        return []
    # A one-word query ("churn") matches on that word; longer ones need two
    # shared terms, so a lone common word does not drag in unrelated runs.
    need = min(2, len(q_terms))
    tables = [table] if table and table != "*" else None
    with db.connect() as c:
        rows = c.execute(
            "SELECT conversation_id, prompt, source, tbl, sql, ok, error, reward, "
            "reward_source, created_at FROM agent_traces "
            "WHERE user_id=? AND source=? AND sql IS NOT NULL AND prompt IS NOT NULL "
            "AND COALESCE(mode, '') <> 'pipeline' ORDER BY created_at DESC LIMIT ?",
            (user["id"], source, _RECALL_SCAN)).fetchall()
    found, seen = [], set()
    for r in rows:
        if exclude_conversation and r["conversation_id"] == exclude_conversation:
            continue
        terms = _terms(r["prompt"])
        if len(q_terms & terms) < need:
            continue
        key = (_norm(r["prompt"]), " ".join(r["sql"].split()))
        if key in seen:
            continue
        seen.add(key)
        action = {"steps": [{"name": "recall", "source": r["source"],
                             "table": r["tbl"], "sql": r["sql"]}]}
        try:
            if not pipeline_memory._sql_allowed(user, action, source, tables):
                continue
        except Exception:
            continue   # fails validation today (or the source is gone): not recallable
        found.append((_jaccard(q_terms, terms), r))
    # Stable sort: equal scores keep newest-first order from the query.
    found.sort(key=lambda item: item[0], reverse=True)
    return [{"asked_on": time.strftime("%Y-%m-%d", time.gmtime(r["created_at"])),
             "question": r["prompt"], "sql": r["sql"], "outcome": _outcome(r)}
            for _score, r in found[:limit]]


# ── API: the owner's view of their notes ─────────────────────────────────

@router.get("")
def my_notes(user=Depends(current_user)):
    return {"notes": list_notes(user["id"])}


@router.delete("/{note_id}")
def forget_note(note_id: str, user=Depends(current_user)):
    if not delete_note(user["id"], note_id):
        raise HTTPException(404, "Note not found")
    return {"deleted": 1}


@router.delete("")
def forget_all(user=Depends(current_user)):
    return {"deleted": clear_notes(user["id"])}
