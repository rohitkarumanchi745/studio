"""KAG on a graph — entities and relations extracted from ingested documents,
stored as a portable in-DB graph, and used to augment retrieval.

Chunk RAG answers "which passage"; the graph answers "what connects to what" —
multi-hop links between the entities the documents mention. Both feed the same
grounded, cited answer.

Design:
- PORTABLE by default: nodes/edges live in the app DB (SQLite or Postgres), so
  this works with no Neo4j and no LLM key — exactly prod's situation. Neo4j is a
  pluggable backend for later; the retrieval API here is backend-agnostic.
- EXTRACTION: the LLM when a key is present and STUDIO_KAG_GRAPH_LLM is on;
  otherwise a deterministic fallback (capitalized-phrase entities + intra-sentence
  co-occurrence relations). Never blocks or fails ingest — best-effort.
- RBAC BY CONSTRUCTION: every entity/edge/mention carries the access_scope of the
  chunk it came from, and retrieval filters with the SAME kag._scope_sql predicate
  as chunk search — so a private 'u:' scope is owner-only even for an admin, and
  no query uses a SQL wildcard (which would break psycopg on Postgres).
- INERT: extracted names/relations are quoted reference data, never instructions.
"""
import os
import re
import uuid
import time

from . import db


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS kag_entities (
            id TEXT PRIMARY KEY,
            collection TEXT NOT NULL,
            norm TEXT NOT NULL,
            name TEXT NOT NULL,
            etype TEXT,
            access_scope TEXT NOT NULL,
            mentions INTEGER NOT NULL DEFAULT 1,
            created_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kag_ent_key
            ON kag_entities(collection, norm, access_scope);
        CREATE TABLE IF NOT EXISTS kag_edges (
            id TEXT PRIMARY KEY,
            collection TEXT NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            relation TEXT NOT NULL,
            access_scope TEXT NOT NULL,
            weight INTEGER NOT NULL DEFAULT 1,
            source_name TEXT,
            created_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_kag_edge_key
            ON kag_edges(collection, src, dst, relation, access_scope);
        CREATE INDEX IF NOT EXISTS idx_kag_edge_src ON kag_edges(collection, src);
        CREATE INDEX IF NOT EXISTS idx_kag_edge_dst ON kag_edges(collection, dst);
        CREATE TABLE IF NOT EXISTS kag_mentions (
            collection TEXT NOT NULL,
            norm TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            source_name TEXT,
            page TEXT,
            access_scope TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kag_mention_norm ON kag_mentions(collection, norm);
        CREATE INDEX IF NOT EXISTS idx_kag_mention_chunk ON kag_mentions(chunk_id);
        """)
        c.commit()


def enabled():
    """The graph layer is always on for indexing/retrieval (portable, no deps).
    STUDIO_KAG_GRAPH=0 disables it entirely (indexing becomes a no-op)."""
    return os.getenv("STUDIO_KAG_GRAPH", "1").lower() in ("1", "true", "yes")


# ── Extraction ───────────────────────────────────────────────────────────

_CAP = re.compile(r"\b([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,3})\b")
_SENT = re.compile(r"[.!?\n]+")
_WORD = re.compile(r"[A-Za-z0-9]+")
_STOP = {
    "the", "this", "that", "these", "those", "a", "an", "and", "but", "or", "if",
    "when", "while", "for", "to", "of", "in", "on", "at", "by", "as", "is", "are",
    "was", "were", "be", "we", "you", "it", "they", "he", "she", "our", "your",
    "their", "his", "her", "its", "i", "each", "all", "any", "no", "not", "must",
    "may", "can", "will", "shall", "should", "acme",
}
_MAX_ENTITIES = 40
_MAX_EDGES = 60


def _norm(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _clean_entity(raw):
    """Trim a leading stopword (sentence-initial capitalization) and reject
    junk. Returns a display name or None."""
    parts = raw.strip().split()
    while parts and parts[0].lower() in _STOP:
        parts = parts[1:]
    name = " ".join(parts).strip(" .-&")
    if len(name) < 3 or not any(ch.isalpha() for ch in name):
        return None
    if _norm(name) in _STOP:
        return None
    return name


def _extract_deterministic(text):
    """Capitalized-phrase entities + intra-sentence co-occurrence relations.
    No LLM, no network. Returns (entities:{norm:name}, edges:[(src,dst,rel)])."""
    entities, edges = {}, []
    seen_edge = set()
    for sentence in _SENT.split(text or ""):
        found = []
        for m in _CAP.finditer(sentence):
            name = _clean_entity(m.group(1))
            if not name:
                continue
            n = _norm(name)
            entities.setdefault(n, name)
            if n not in found:
                found.append(n)
            if len(entities) >= _MAX_ENTITIES:
                break
        # co-occurrence edges between distinct entities in the sentence
        for i in range(len(found)):
            for j in range(i + 1, len(found)):
                a, b = sorted((found[i], found[j]))
                if (a, b) in seen_edge:
                    continue
                seen_edge.add((a, b))
                edges.append((a, b, "related_to"))
                if len(edges) >= _MAX_EDGES:
                    return entities, edges
    return entities, edges


def _extract_llm(text, user):
    """LLM extraction → same shape. Returns None on any failure (caller falls
    back to deterministic). Only used when STUDIO_KAG_GRAPH_LLM is on."""
    import json
    from . import agent
    if not agent.llm_available(agent.llm_spec(), user):
        return None
    try:
        llm = agent.make_llm(agent.llm_spec(), user)
        sys = ("Extract entities and relations from the text. Return ONLY JSON: "
               '{"entities":[{"name":"..","type":".."}],'
               '"relations":[{"source":"..","relation":"..","target":".."}]}. '
               "Use only names explicitly present; no commentary.")
        reply = llm.invoke([("system", sys), ("user", text[:6000])])
        raw = reply.content if isinstance(reply.content, str) else "".join(
            b.get("text", "") for b in reply.content if isinstance(b, dict))
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        data = json.loads(raw)
    except Exception:
        return None
    entities = {}
    for e in (data.get("entities") or [])[:_MAX_ENTITIES]:
        name = _clean_entity(str(e.get("name", "")))
        if name:
            entities[_norm(name)] = name
    edges = []
    for r in (data.get("relations") or [])[:_MAX_EDGES]:
        s, t = _norm(str(r.get("source", ""))), _norm(str(r.get("target", "")))
        rel = re.sub(r"\s+", "_", str(r.get("relation", "related_to")).strip().lower())[:40] or "related_to"
        if s and t and s != t and s in entities and t in entities:
            edges.append((s, t, rel))
    return (entities, edges) if entities else None


def _extract(text, user=None):
    if os.getenv("STUDIO_KAG_GRAPH_LLM", "0").lower() in ("1", "true", "yes"):
        out = _extract_llm(text, user)
        if out is not None:
            return out
    return _extract_deterministic(text)


# ── Indexing (called from kag.ingest_bytes, best-effort) ──────────────────

def clear_source(collection, source_name):
    """Drop this source's graph rows before re-indexing (mirrors the chunk
    DELETE in ingest so re-ingest never duplicates)."""
    if not enabled():
        return
    with db.connect() as c:
        c.execute("DELETE FROM kag_mentions WHERE collection=? AND source_name=?",
                  (collection, source_name))
        c.execute("DELETE FROM kag_edges WHERE collection=? AND source_name=?",
                  (collection, source_name))
        c.commit()
    # Prune entities that no longer have any mention in this collection.
    _prune_orphans(collection)


def _prune_orphans(collection):
    with db.connect() as c:
        c.execute(
            "DELETE FROM kag_entities WHERE collection=? AND norm NOT IN "
            "(SELECT DISTINCT norm FROM kag_mentions WHERE collection=?)",
            (collection, collection))
        c.commit()


def index_chunk(collection, chunk_id, source_name, text, access_scope, page=None, user=None):
    """Extract entities/relations from one chunk and upsert them, scoped to the
    chunk's access_scope. Best-effort: never raises into ingest."""
    if not enabled():
        return
    try:
        entities, edges = _extract(text, user)
        if not entities:
            return
        now = time.time()
        with db.connect() as c:
            for norm, name in entities.items():
                # upsert entity (bump mentions on conflict), portable across dialects
                cur = c.execute(
                    "UPDATE kag_entities SET mentions = mentions + 1 "
                    "WHERE collection=? AND norm=? AND access_scope=?",
                    (collection, norm, access_scope))
                if not cur.rowcount:
                    c.execute(
                        "INSERT INTO kag_entities (id, collection, norm, name, etype, "
                        "access_scope, mentions, created_at) VALUES (?,?,?,?,?,?,1,?)",
                        (str(uuid.uuid4()), collection, norm, name, "term",
                         access_scope, now))
                c.execute(
                    "INSERT INTO kag_mentions (collection, norm, chunk_id, source_name, "
                    "page, access_scope) VALUES (?,?,?,?,?,?)",
                    (collection, norm, chunk_id, source_name, str(page) if page else None,
                     access_scope))
            for src, dst, rel in edges:
                cur = c.execute(
                    "UPDATE kag_edges SET weight = weight + 1 WHERE collection=? AND "
                    "src=? AND dst=? AND relation=? AND access_scope=?",
                    (collection, src, dst, rel, access_scope))
                if not cur.rowcount:
                    c.execute(
                        "INSERT INTO kag_edges (id, collection, src, dst, relation, "
                        "access_scope, weight, source_name, created_at) "
                        "VALUES (?,?,?,?,?,?,1,?,?)",
                        (str(uuid.uuid4()), collection, src, dst, rel, access_scope,
                         source_name, now))
            c.commit()
    except Exception:
        pass  # graph indexing is an enhancement; ingest must still succeed


# ── Retrieval (RBAC-scoped; parameterized + substr only, psycopg-safe) ────

def _reachable_entities(role, user_id, collection=None, limit=2000):
    """All entities the caller may reach, as {norm: {name, etype, mentions}}."""
    from . import kag, rbac
    if not rbac.kag_scopes_for(role, user_id):
        return {}
    pred, params = kag._scope_sql(role, user_id)
    where = pred
    if collection is not None:
        where += " AND collection=?"
        params = params + [collection]
    with db.connect() as c:
        rows = c.execute(
            f"SELECT norm, name, etype, mentions FROM kag_entities WHERE {where} "
            f"ORDER BY mentions DESC LIMIT ?", tuple(params + [limit])).fetchall()
    return {r["norm"]: {"name": r["name"], "etype": r["etype"],
                        "mentions": r["mentions"]} for r in rows}


def graph_search(query, role, user_id=None, collection=None, hops=1, k=8):
    """Seed on entities named in the query, walk `hops` of RBAC-scoped edges, and
    return the connected subgraph + the source chunks that mention it — grounding
    for connection-shaped questions. Fail-closed and wildcard-free."""
    if not enabled():
        return {}
    reachable = _reachable_entities(role, user_id, collection)
    if not reachable:
        return {}
    q_tokens = {w for w in _WORD.findall((query or "").lower()) if len(w) > 2}
    if not q_tokens:
        return {}
    # seed: reachable entities whose name shares a token with the query (Python
    # match (parameterized IN only; no wildcard ever reaches psycopg)
    seeds = [n for n in reachable
             if q_tokens & set(_WORD.findall(n)) or _norm(n) in q_tokens]
    seeds = sorted(seeds, key=lambda n: -reachable[n]["mentions"])[:k]
    if not seeds:
        return {}

    from . import kag
    pred, sparams = kag._scope_sql(role, user_id)
    frontier, nodes, rels = set(seeds), set(seeds), []
    with db.connect() as c:
        for _ in range(max(1, hops)):
            if not frontier:
                break
            ph = ",".join("?" for _ in frontier)
            fp = list(frontier)
            cbind = ([collection] if collection is not None else [])
            cclause = " AND collection=?" if collection is not None else ""
            rows = c.execute(
                f"SELECT src, dst, relation, weight FROM kag_edges WHERE {pred}{cclause} "
                f"AND (src IN ({ph}) OR dst IN ({ph}))",
                tuple(sparams + cbind + fp + fp)).fetchall()
            nxt = set()
            for r in rows:
                rels.append({"source": r["src"], "relation": r["relation"],
                             "target": r["dst"], "weight": r["weight"]})
                for side in (r["src"], r["dst"]):
                    if side not in nodes:
                        nodes.add(side)
                        nxt.add(side)
            frontier = nxt
        # provenance: source chunks mentioning any node in the subgraph
        sources = []
        node_list = list(nodes)[:60]
        if node_list:
            ph = ",".join("?" for _ in node_list)
            cbind = ([collection] if collection is not None else [])
            cclause = " AND collection=?" if collection is not None else ""
            mrows = c.execute(
                f"SELECT DISTINCT source_name, chunk_id FROM kag_mentions WHERE {pred}{cclause} "
                f"AND norm IN ({ph}) LIMIT 20",
                tuple(sparams + cbind + node_list)).fetchall()
            sources = [{"source": r["source_name"], "chunk_id": r["chunk_id"]} for r in mrows]
    return {
        "entities": [{"name": reachable.get(n, {}).get("name", n),
                      "norm": n, "mentions": reachable.get(n, {}).get("mentions")}
                     for n in list(nodes)[:40]],
        "relations": rels[:60],
        "sources": sources,
        "seeds": seeds,
    }
