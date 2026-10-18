"""The Verified SQL library in chat: a question first looks for SQL a person
already verified for it.

A saved query (queries.py) is SQL its author edited or accepted, verified
through the governed gateway — a real, authorized execution — and saved with
the business question it answers. This module puts that library on the chat
path. Precedence, every mode:

    certified semantic definitions → verified SQL → plan cache → agents

- EXACT MATCH — the same intent: the requirement matches closely, the same
  ranking direction, and the same parameters (numbers, years, quoted values;
  "revenue in 2024" never answers "revenue in 2023"). The saved SQL is
  re-validated and re-run AS THE ASKER (RBAC, guard, governance), and the
  answer names the query it came from.
- RELATED QUESTION — close but not the same: the agent is handed the saved
  query as a starting point — its tables, joins and calculations — to adapt
  and verify. It is never executed as the answer.

Candidates are the asker's own verified queries and those shared with the
organization, on the same source. A query is linked to the metadata graph by
the tables it reads: one whose table is gone is BROKEN and never used, and one
whose tables carry a semantic definition changed since it was verified may
only guide, not answer.

Verified is not certified: verification proves the query was authorized and
ran when it was saved, not that its calculation is right. The answer says so.

With HARRIER_EMBED_URL set, requirements are matched by meaning as well (an
index kept per query, refreshed when its requirement changes).
"""
import hashlib
import json
import logging
import os
import re
import time

from . import db, embed, queryguard

log = logging.getLogger("studio.verified_sql")

_INSTRUCT = "Retrieve saved analytics questions that ask for the same thing"


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def exact_threshold():
    return _env_float("STUDIO_VERIFIED_SQL_EXACT", 0.9)


def related_threshold():
    return _env_float("STUDIO_VERIFIED_SQL_RELATED", 0.5)


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS saved_query_embeddings (
            query_id TEXT PRIMARY KEY,
            text_hash TEXT NOT NULL,
            vector TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        """)
        c.commit()


# ── Matching ─────────────────────────────────────────────────────────────

_PARAM = re.compile(r"'[^']*'|\"[^\"]*\"|\b\d+(?:[.,]\d+)?\b")


def parameters(text):
    """The values a question pins: numbers (years, amounts, top-N) and quoted
    strings. Two questions with different ones are different questions."""
    return frozenset(m.group(0).strip("'\"").lower() for m in _PARAM.finditer(text or ""))


def _intent(prompt):
    """A question's words without its values and its ranking direction —
    those two are compared on their own (match), so a different year or
    "lowest" for "highest" makes a question RELATED, never unrecognisable."""
    from . import qcache
    return [t for t in qcache._sig(prompt) if not t.startswith("~") and not t.isdigit()]


def _similarity(prompt, sig, q_vec, row, vectors):
    from . import qcache
    vec = vectors.get(row["id"])
    if q_vec and vec:
        return embed.cosine(q_vec, vec)
    return qcache._jaccard(_intent(prompt), _intent(row["prompt"]))


def _candidates(user, source, table_scope):
    try:
        with db.connect() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM saved_queries WHERE verified=1 AND source=? "
                "AND (user_id=? OR visibility='org')",
                (source, user.get("id") or "")).fetchall()]
    except Exception:
        # No library in this deployment (or it cannot be read): no match —
        # a chat turn never fails for it.
        log.warning("verified SQL library could not be read", exc_info=True)
        return []
    return [r for r in rows if table_scope in (None, "*")
            or (r.get("table_label") or "*") in ("*", table_scope)]


def dependencies(row):
    """The query's link to the metadata graph: (status, reasons, tables) —
    "valid" when every table it reads is a live object of its source,
    "broken" when one is gone, "unchecked" when the source has not been
    discovered. Read on every use, so a dropped table breaks it at once."""
    tables = sorted(queryguard.base_tables(row["sql"]))
    with db.connect() as c:
        live = {r["name"].lower(): r["name"] for r in c.execute(
            "SELECT name FROM meta_objects WHERE source=? AND removed_at IS NULL",
            (row["source"],)).fetchall()}
    if not live:
        return "unchecked", [], tables
    names = {n.split(".")[-1]: n for n in live}       # bare names, as the guard reads them
    missing = [t for t in tables if t not in live and t not in names]
    if missing:
        return "broken", [f"table {t} is no longer in {row['source']}" for t in missing], tables
    return "valid", [], [live.get(t) or live[names[t]] for t in tables]


def _changed_definitions(row, tables):
    """Semantic definitions on the query's tables changed after it was
    verified — the calculation it encodes may be out of date."""
    try:
        from . import metametrics
        out = []
        for d in metametrics.for_source(row["source"], set(tables)):
            versions = [v for v in metametrics.history(d["source"], d["table"], d["name"],
                                                       d["kind"]) if not v.get("removed")]
            if versions and versions[-1]["applied_at"] > (row.get("verified_at") or 0):
                out.append(d["name"])
        return out
    except Exception:
        return []


def match(user, source, table_scope, prompt):
    """The best verified query for this question, or None:
        {"query": row, "score", "kind": "exact" | "related", "why": [...]}
    Broken queries are never returned."""
    from . import qcache
    sig = qcache._sig(prompt)
    if not sig or not user.get("id"):
        return None
    rows = _candidates(user, source, table_scope)
    if not rows:
        return None
    vectors = _vectors(rows)
    q_vec = embed.embed(prompt, kind="query", instruct=_INSTRUCT) if vectors else None
    scored = sorted(((_similarity(prompt, sig, q_vec, r, vectors), r) for r in rows),
                    key=lambda t: (-t[0], -(t[1].get("verified_at") or 0)))
    for score, row in scored:
        if score < related_threshold():
            return None
        status, reasons, tables = dependencies(row)
        if status == "broken":
            continue
        # Every way it is not THIS question — any one keeps it from answering.
        why = []
        if qcache._direction(prompt) != qcache._direction(row["prompt"]):
            why.append("it ranks the other way")
        if parameters(prompt) != parameters(row["prompt"]):
            why.append("its values differ")
        changed = _changed_definitions(row, tables)
        if changed:
            why.append(f"the definition of {', '.join(changed)} changed since it was verified")
        kind = "exact" if score >= exact_threshold() and not why else "related"
        return {"query": row, "score": round(score, 3), "kind": kind, "why": why,
                "status": status}
    return None


def best_exact(user, sources, prompt):
    """(source, match) for the best exact match over several sources (All
    sources), or (None, None)."""
    best = (None, None)
    for source in sources:
        m = match(user, source, "*", prompt)
        if m and m["kind"] == "exact" and (best[1] is None or m["score"] > best[1]["score"]):
            best = (source, m)
    return best


# ── Using a match ────────────────────────────────────────────────────────

def provenance(m, user):
    q = m["query"]
    return {"id": q["id"], "title": q["title"], "match": m["kind"],
            "similarity": m["score"], "mine": q["user_id"] == user.get("id"),
            "shared": q["visibility"] == "org", "verified_at": q.get("verified_at"),
            "certified": False, "dependencies": m.get("status")}


def answer(user, source, m):
    """Answer from an EXACT match: the saved SQL re-validated and re-run as
    the asker. None when it no longer runs for them (the caller falls through
    to the next tier)."""
    from . import gateway, roster
    from .agent import _auto_chart
    q = m["query"]
    try:
        # The same gate as any read — RBAC, guard, governance, audit — as the
        # ASKER, under its own audit purpose.
        out = gateway.execute(user, source, q["sql"], "verified_sql",
                              table_label=q.get("table_label") or "*")
    except Exception:
        return None
    res = {"sql": out.sql}
    columns, rows = list(out.columns), list(out.rows)
    chart = _auto_chart(columns, rows, q.get("table_label") or q["title"]) or {
        "type": "table", "title": q["title"],
        "x": columns[0] if columns else None, "y": columns[1:2]}
    whose = "your" if q["user_id"] == user.get("id") else "the organization's"
    text = (f"Answered with {whose} verified query “{q['title']}”, re-run now against "
            f"current data. Verified means it ran when it was saved; its calculation is "
            f"not certified.")
    panel = {"sql": res["sql"], "columns": columns, "rows": rows, "chart": chart}
    return {"text": text, "sql": res["sql"], "columns": columns, "rows": rows, "chart": chart,
            "panels": [panel], "mode": "verified_sql", "model": None,
            "served_by": "verified_sql", "email": None, "errors": [],
            "agents": [roster.SQL_VERIFIER], "verified_sql": provenance(m, user)}


def hint_lines(m):
    """What the agent is told about a RELATED verified query."""
    q = m["query"]
    lines = [f"A verified query answers a related question — “{q['prompt'][:300]}”"
             + (f" (not this one: {'; '.join(m['why'])})" if m["why"] else "") + ":",
             q["sql"][:2000],
             "Reuse its tables, joins and calculations where they fit and adapt the rest. "
             "It was verified to run, not certified — make sure your query answers THIS "
             "question."]
    return lines


# ── Harrier index of requirements ────────────────────────────────────────

def _text_hash(row):
    return hashlib.sha256((row.get("prompt") or "").encode()).hexdigest()


def _vectors(rows, budget=5):
    """{query id: vector} for these queries — those indexed for their current
    requirement, plus up to `budget` newly embedded now. {} without Harrier."""
    if not embed.available() or not rows:
        return {}
    with db.connect() as c:
        ph = ",".join("?" for _ in rows)
        known = {r["query_id"]: (r["text_hash"], json.loads(r["vector"])) for r in c.execute(
            f"SELECT query_id, text_hash, vector FROM saved_query_embeddings "
            f"WHERE query_id IN ({ph})", tuple(r["id"] for r in rows)).fetchall()}
    out = {}
    for row in rows:
        h = _text_hash(row)
        hit = known.get(row["id"])
        if hit and hit[0] == h:
            out[row["id"]] = hit[1]
        elif budget > 0:
            budget -= 1
            vec = index_one(row)
            if vec:
                out[row["id"]] = vec
    return out


def index_one(row):
    """Embed one query's requirement (best-effort). Returns the vector or None."""
    if not embed.available():
        return None
    vec = embed.embed(row.get("prompt") or "", kind="document")
    if not vec:
        return None
    with db.connect() as c:
        c.execute("DELETE FROM saved_query_embeddings WHERE query_id=?", (row["id"],))
        c.execute("INSERT INTO saved_query_embeddings (query_id, text_hash, vector, updated_at) "
                  "VALUES (?,?,?,?)", (row["id"], _text_hash(row), json.dumps(vec), time.time()))
        c.commit()
    return vec


def forget(query_id):
    with db.connect() as c:
        c.execute("DELETE FROM saved_query_embeddings WHERE query_id=?", (query_id,))
        c.commit()
