"""Auto routing — the question picks its source, and so its tables.

With Auto on (the chat toolbar's switch) nobody picks a source or a table:
each question is matched against what every source the user's role may query
contains, and goes where its answer is.

The match is metaground's — table and column names, descriptions, the
certified metrics' names and synonyms, and meaning when Harrier is
configured — read from the metadata graph's record, so routing costs no
warehouse query. (A source not discovered yet is matched by its table names,
listed once.) Every term of the question that some source covers must be
covered by the route:

- ONE source covers them all → it answers, exactly as if it had been picked;
  the agent's own grounding then chooses the tables. When the conversation
  was already there, a tie keeps it there.
- They are SPLIT (one term only here, another only there) → the multi-agent
  path, its roster limited to the sources the question needs.
- Two sources cover them about EQUALLY (the same tables in two places) → the
  multi-agent path over those, which asks which one was meant before it
  guesses (orchestrator.ambiguous_tables).
- NOTHING matches → a follow-up stays where the conversation was; a first
  question goes to the only usable source, or across every source as "all
  sources" always has.

Permission-bound: only sources the role may query, only tables it may read,
only columns governance lets it see — a route never names more than the
catalog would show this user.
"""
import logging

from . import db, governance, metaground, rbac, util

log = logging.getLogger("studio.autoroute")

#: At most this many sources answer one auto-routed question together.
MAX_SOURCES = 4
#: A second source covering every term with at least this share of the best
#: one's strength is a tie: Studio asks rather than picks.
TIE = 0.8
#: Tables named per source in a route (what the chat shows as looked at).
SHOWN_TABLES = 4


def usable(user):
    """Configured sources this user's role may query — names only."""
    from .connectors import all_sources
    allowed = set(rbac.allowed_sources(user["role"]))
    return [s["name"] for s in all_sources() if s.get("configured") and s["name"] in allowed]


def _objects(user, source):
    """{table: {"kind", "description", "fingerprint", "columns"}} this user may
    read on `source`, columns governed — the metadata graph's record, else the
    source's table names (not discovered yet). None when it cannot be listed."""
    with db.connect() as c:
        names = [r["name"] for r in c.execute(
            "SELECT name FROM meta_objects WHERE source=? AND removed_at IS NULL",
            (source,)).fetchall()]
    if names:
        objects = metaground.discovered(source, rbac.allowed_tables(user["role"], source, names))
    else:
        from .connectors import get_connector
        try:
            listed = get_connector(source).list_tables()
        except Exception:
            log.info("auto routing: %s could not be listed", source, exc_info=True)
            return None
        objects = {t: {"kind": "table", "description": None, "fingerprint": None,
                       "columns": []}
                   for t in rbac.allowed_tables(user["role"], source, listed)}
    for table, obj in objects.items():
        obj["columns"] = governance.governed_schema(source, table, obj["columns"])
    return objects


def _assess(user, source, prompt, qvec):
    objects = _objects(user, source)
    if not objects:
        return None
    ranked = metaground.relevance(source, prompt, objects, qvec)
    return {"source": source,
            "terms": {w for _t, _s, why in ranked for w in why if w != metaground.SEMANTIC},
            "strength": round(sum(s for _t, s, _w in ranked[:3]), 3),
            "tables": [t for t, _s, _w in ranked[:SHOWN_TABLES]]}


def _single(hit, why):
    return {"mode": "single", "source": hit["source"], "tables": hit["tables"],
            "terms": sorted(hit["terms"]), "why": why}


def _multi(hits, why):
    return {"mode": "multi", "sources": [h["source"] for h in hits],
            "tables": {h["source"]: h["tables"] for h in hits},
            "terms": sorted(set().union(*(h["terms"] for h in hits))), "why": why}


def route(user, prompt, previous=None):
    """Where this question goes: {"mode": "single", "source", "tables",
    "terms", "why"}, {"mode": "multi", "sources", "tables", "terms", "why"},
    or {"mode": "all", "why"}. `previous`: this conversation's last route,
    which a follow-up naming nothing new keeps."""
    names = usable(user)
    qvec = metaground.query_vector(prompt)
    assessed = [a for a in util.pmap(lambda s: _assess(user, s, prompt, qvec), names,
                                     workers=6) if a]
    hits = [a for a in assessed if a["strength"] > 0]
    if not hits:
        if previous and previous.get("mode") in ("single", "multi") and (
                set(previous.get("sources") or [previous.get("source")]) <= set(names)):
            # The earlier question's matched words are not this one's.
            return {**previous, "kept": True, "terms": [],
                    "why": "a follow-up — staying where this conversation was"}
        if len(names) == 1:
            return {"mode": "single", "source": names[0], "tables": [], "terms": [],
                    "why": "the only source you can use"}
        return {"mode": "all", "why": "no source's tables match the question, so every "
                                      "source is asked"}

    union = set().union(*(h["terms"] for h in hits))
    full = sorted((h for h in hits if h["terms"] >= union),
                  key=lambda h: (-h["strength"], h["source"]))
    if full:
        best = full[0]
        stay = previous.get("source") if previous and previous.get("mode") == "single" else None
        kept = next((h for h in full if h["source"] == stay
                     and h["strength"] >= TIE * best["strength"]), None)
        if kept is not None:
            return _single(kept, f"{kept['source']} has what the question asks for, and this "
                                 "conversation was already there")
        rivals = [h for h in full[1:] if h["strength"] >= TIE * best["strength"]]
        if rivals:
            tied = [best] + rivals[:MAX_SOURCES - 1]
            return _multi(tied, "more than one source has what the question asks for: "
                                + ", ".join(h["source"] for h in tied))
        return _single(best, f"{best['source']} has what the question asks for")

    # No one source covers the question: the fewest that do, best first.
    pool = sorted(hits, key=lambda h: (-len(h["terms"]), -h["strength"], h["source"]))
    chosen, left = [pool[0]], union - pool[0]["terms"]
    while left and len(chosen) < MAX_SOURCES:
        nxt = max((h for h in pool if h not in chosen),
                  key=lambda h: (len(h["terms"] & left), h["strength"]), default=None)
        if nxt is None or not nxt["terms"] & left:
            break
        chosen.append(nxt)
        left -= nxt["terms"]
    if len(chosen) == 1:
        return _single(chosen[0], f"{chosen[0]['source']} has the most of what the "
                                  "question asks for")
    return _multi(chosen, "the question needs " + ", ".join(h["source"] for h in chosen))


def previous_route(messages):
    """Where this conversation's latest question went — its auto route, or
    the source it was asked on by hand — from its messages (the caller has
    already checked the user may read them). None for a new conversation or
    one last asked across all sources by hand."""
    for m in reversed(messages or []):
        if m["role"] != "user":
            continue
        content = m["content"] or {}
        if content.get("auto_route"):
            return content["auto_route"]
        source = content.get("source")
        if source and source != "*":
            return {"mode": "single", "source": source, "tables": [], "terms": []}
        return None
    return None
