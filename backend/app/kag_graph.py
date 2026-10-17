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
- PROVENANCE PER CHUNK: an entity mention (kag_mentions) and a relationship
  (kag_edge_mentions) are each recorded against the chunk that says it. Counts
  are never bumped blindly: an entity's `mentions` and an edge's `weight` are
  recomputed from the evidence that survives, and a row no evidence supports is
  deleted. So deleting document A removes what only A said, keeps what B also
  says, and re-ingesting a document never inflates a count. (kag_edges.source_name
  is a legacy column: one name could not describe a relationship several
  documents support, and nothing reads it now.)
- DELETION IS NOT OPTIONAL: removing a document or collection removes its graph
  evidence in the SAME transaction as its chunks, whether or not the graph is
  enabled — turning the layer off must not strand data that a delete can no
  longer reach. Retrieval also trusts only evidence whose chunk still exists.
"""
import os
import re
import uuid
import time

from . import db, jobs


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
        CREATE TABLE IF NOT EXISTS kag_edge_mentions (
            collection TEXT NOT NULL,
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            relation TEXT NOT NULL,
            access_scope TEXT NOT NULL,
            chunk_id TEXT NOT NULL,
            source_name TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_kag_edge_mention_key
            ON kag_edge_mentions(collection, src, dst, relation, access_scope);
        CREATE INDEX IF NOT EXISTS idx_kag_edge_mention_chunk ON kag_edge_mentions(chunk_id);
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


# ── Evidence: extract, write, remove, recount ────────────────────────────
#
# kag.py owns the transaction. Extraction happens BEFORE it opens (the LLM path
# makes network calls); write / remove_source / remove_collection take the
# caller's open connection, so a document's chunks and its graph evidence are
# replaced or deleted together and a failure rolls both back.

_BATCH = 400          # bound every IN (...) list well under SQLite's variable cap

_ENTITY_SUPPORT = (
    "(SELECT COUNT(*) FROM kag_mentions m WHERE m.collection = kag_entities.collection "
    "AND m.norm = kag_entities.norm AND m.access_scope = kag_entities.access_scope)")
_EDGE_SUPPORT = (
    "(SELECT COUNT(*) FROM kag_edge_mentions m WHERE m.collection = kag_edges.collection "
    "AND m.src = kag_edges.src AND m.dst = kag_edges.dst "
    "AND m.relation = kag_edges.relation AND m.access_scope = kag_edges.access_scope)")


def _serialize(c, collection):
    """Take this collection's graph lock for the rest of the caller's
    transaction. Every count is recomputed from COMMITTED evidence, so two
    writers in one collection must not interleave: an ingest and a delete that
    both touch an entity could otherwise each miss the other's rows, and a
    count of 0 would delete an entity that still has evidence. SQLite already
    runs one writer at a time; Postgres gets a transaction-scoped advisory lock
    keyed on the collection (re-entrant, released at commit or rollback)."""
    if not db.IS_PG:
        return
    import hashlib
    key = int.from_bytes(hashlib.sha256(("kag_graph:" + collection).encode()).digest()[:8],
                         "big", signed=True)
    c.execute("SELECT pg_advisory_xact_lock(?)", (key,))


def _batches(items):
    items = list(items)
    for i in range(0, len(items), _BATCH):
        yield items[i:i + _BATCH]


def extract(chunks, user=None):
    """Extract evidence for chunks [{id, source_name, text, access_scope, page}].
    Call OUTSIDE any transaction. Best-effort per chunk: a chunk that fails to
    extract contributes nothing; it never fails the ingest. [] when disabled."""
    if not enabled():
        return []
    out = []
    for ch in chunks:
        try:
            entities, edges = _extract(ch.get("text") or "", user)
        except Exception:
            continue
        if entities:
            out.append({**ch, "entities": entities, "edges": edges})
    return out


def write(c, collection, evidence):
    """Record extracted evidence on the caller's open transaction, then bring
    every entity/edge it touched in line with the evidence that supports it."""
    _serialize(c, collection)
    now = time.time()
    touched = set()
    for ev in evidence:
        scope, cid, source = ev["access_scope"], ev["id"], ev.get("source_name")
        page = str(ev["page"]) if ev.get("page") else None
        for norm, name in ev["entities"].items():
            c.execute(
                "INSERT INTO kag_mentions (collection, norm, chunk_id, source_name, "
                "page, access_scope) VALUES (?,?,?,?,?,?)",
                (collection, norm, cid, source, page, scope))
            c.execute(
                "INSERT INTO kag_entities (id, collection, norm, name, etype, access_scope, "
                "mentions, created_at) VALUES (?,?,?,?,?,?,0,?) ON CONFLICT DO NOTHING",
                (str(uuid.uuid4()), collection, norm, name, "term", scope, now))
            touched.add(norm)
        for src, dst, rel in ev["edges"]:
            c.execute(
                "INSERT INTO kag_edge_mentions (collection, src, dst, relation, "
                "access_scope, chunk_id, source_name) VALUES (?,?,?,?,?,?,?)",
                (collection, src, dst, rel, scope, cid, source))
            c.execute(
                "INSERT INTO kag_edges (id, collection, src, dst, relation, access_scope, "
                "weight, source_name, created_at) VALUES (?,?,?,?,?,?,0,NULL,?) "
                "ON CONFLICT DO NOTHING",
                (str(uuid.uuid4()), collection, src, dst, rel, scope, now))
            touched.update((src, dst))
    recount(c, collection, touched)


def _touched_by(c, collection, where, params):
    """Entity names whose evidence matches `where` — read BEFORE it is deleted."""
    touched = set()
    for r in c.execute(f"SELECT DISTINCT norm FROM kag_mentions WHERE collection=? AND {where}",
                       (collection, *params)).fetchall():
        touched.add(r["norm"])
    for r in c.execute(f"SELECT DISTINCT src, dst FROM kag_edge_mentions WHERE collection=? "
                       f"AND {where}", (collection, *params)).fetchall():
        touched.update((r["src"], r["dst"]))
    return touched


def _drop_evidence(c, collection, where, params):
    touched = _touched_by(c, collection, where, params)
    c.execute(f"DELETE FROM kag_mentions WHERE collection=? AND {where}", (collection, *params))
    c.execute(f"DELETE FROM kag_edge_mentions WHERE collection=? AND {where}",
              (collection, *params))
    return touched


def remove_source(c, collection, source_name, chunk_ids=()):
    """Delete one document's graph evidence on the caller's open transaction —
    by its name and by its chunk ids — and recount everything it touched: what
    only this document supported goes, what another document supports stays."""
    _serialize(c, collection)
    touched = _drop_evidence(c, collection, "source_name=?", (source_name,))
    for batch in _batches(chunk_ids):
        ph = ",".join("?" for _ in batch)
        touched |= _drop_evidence(c, collection, f"chunk_id IN ({ph})", tuple(batch))
    recount(c, collection, touched)


def remove_collection(c, collection):
    """Delete a collection's whole graph on the caller's open transaction."""
    _serialize(c, collection)
    for table in ("kag_mentions", "kag_edge_mentions", "kag_edges", "kag_entities"):
        c.execute(f"DELETE FROM {table} WHERE collection=?", (collection,))


def recount(c, collection, norms=None):
    """Set each entity's `mentions` and each edge's `weight` to the evidence
    that supports it now, and delete what nothing supports. `norms` limits the
    pass to those entities and every edge touching them; None means the whole
    collection (rebuild, repair)."""
    if norms is None:
        groups = [(" ", " ", ())]
    else:
        groups = []
        for batch in _batches(sorted(norms)):
            ph = ",".join("?" for _ in batch)
            groups.append((f" AND norm IN ({ph}) ", f" AND (src IN ({ph}) OR dst IN ({ph})) ",
                           tuple(batch)))
    for ent_where, edge_where, names in groups:
        edge_params = names + names if names else ()
        c.execute(f"UPDATE kag_entities SET mentions = {_ENTITY_SUPPORT} "
                  f"WHERE collection=?{ent_where}", (collection, *names))
        c.execute(f"DELETE FROM kag_entities WHERE collection=?{ent_where}AND mentions = 0",
                  (collection, *names))
        c.execute(f"UPDATE kag_edges SET weight = {_EDGE_SUPPORT} "
                  f"WHERE collection=?{edge_where}", (collection, *edge_params))
        c.execute(f"DELETE FROM kag_edges WHERE collection=?{edge_where}AND weight = 0",
                  (collection, *edge_params))


# ── Compatibility wrappers (one chunk / one source, own transaction) ──────

def clear_source(collection, source_name):
    """Drop one source's graph evidence and recount (own transaction)."""
    with db.connect() as c:
        remove_source(c, collection, source_name)
        c.commit()


def index_chunk(collection, chunk_id, source_name, text, access_scope, page=None, user=None):
    """Extract and record one chunk's evidence (own transaction). Best-effort:
    never raises into a caller."""
    try:
        evidence = extract([{"id": chunk_id, "source_name": source_name, "text": text,
                             "access_scope": access_scope, "page": page}], user)
        if not evidence:
            return
        with db.connect() as c:
            write(c, collection, evidence)
            c.commit()
    except Exception:
        pass  # graph indexing is an enhancement; the caller must still succeed


# ── Rebuild: re-derive the graph from the chunks that exist now ──────────

def _chunks_of(collection):
    import json
    with db.connect() as c:
        rows = c.execute("SELECT id, source_name, chunk_text, metadata, access_scope "
                         "FROM kag_chunks WHERE collection=?", (collection,)).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except Exception:
            meta = {}
        out.append({"id": r["id"], "source_name": r["source_name"], "text": r["chunk_text"],
                    "access_scope": r["access_scope"],
                    "page": meta.get("page") or meta.get("sheet")})
    return out


def _collections_with_graph_or_chunks():
    names = set()
    with db.connect() as c:
        for table in ("kag_chunks", "kag_entities", "kag_edges", "kag_mentions",
                      "kag_edge_mentions"):
            names |= {r["collection"] for r in
                      c.execute(f"SELECT DISTINCT collection FROM {table}").fetchall()}
    return sorted(names)


def rebuild(collection=None, user=None):
    """Re-derive the graph from the chunks that exist NOW — the repair for
    graphs written before per-chunk provenance, and for evidence whose chunk is
    gone. Per collection: extract its chunks outside any transaction, then in
    one transaction drop evidence whose chunk is gone, replace the evidence of
    every chunk just extracted, and recount the whole collection (which also
    drops relationships with no evidence at all). A chunk ingested after the
    read keeps the evidence its own ingest wrote. With the layer disabled
    nothing is re-extracted; dead evidence is still removed."""
    names = [collection] if collection is not None else _collections_with_graph_or_chunks()
    stats = {"collections": 0, "chunks": 0}
    for name in names:
        chunks = _chunks_of(name)
        evidence = extract(chunks, user) if enabled() else None
        with db.connect() as c:
            _serialize(c, name)
            dead = ("NOT EXISTS (SELECT 1 FROM kag_chunks k "
                    "WHERE k.id = {t}.chunk_id AND k.collection = {t}.collection)")
            c.execute(f"DELETE FROM kag_mentions WHERE collection=? AND "
                      f"{dead.format(t='kag_mentions')}", (name,))
            c.execute(f"DELETE FROM kag_edge_mentions WHERE collection=? AND "
                      f"{dead.format(t='kag_edge_mentions')}", (name,))
            if evidence is not None:
                current = {r["id"] for r in c.execute(
                    "SELECT id FROM kag_chunks WHERE collection=?", (name,)).fetchall()}
                redone = [ch["id"] for ch in chunks if ch["id"] in current]
                for batch in _batches(redone):
                    ph = ",".join("?" for _ in batch)
                    _drop_evidence(c, name, f"chunk_id IN ({ph})", tuple(batch))
                write(c, name, [ev for ev in evidence if ev["id"] in current])
                stats["chunks"] += len(redone)
            recount(c, name)
            c.commit()
        stats["collections"] += 1
    return stats


@jobs.handler("kag_graph_rebuild")
def _rebuild_job(payload, job=None):
    return rebuild((payload or {}).get("collection"))


# ── Retrieval (RBAC-scoped; parameterized + substr only, psycopg-safe) ────

# Retrieval trusts only evidence whose chunk still exists: a deleted document
# can never surface through an entity, a relationship, or a source reference,
# even if some row outlived it.
_LIVE_CHUNK = ("EXISTS (SELECT 1 FROM kag_chunks k WHERE k.id = kag_mentions.chunk_id "
               "AND k.collection = kag_mentions.collection)")
_ENTITY_EVIDENCE = (
    "EXISTS (SELECT 1 FROM kag_mentions m JOIN kag_chunks k ON k.id = m.chunk_id "
    "WHERE m.collection = kag_entities.collection AND m.norm = kag_entities.norm "
    "AND m.access_scope = kag_entities.access_scope)")
_EDGE_EVIDENCE = (
    "EXISTS (SELECT 1 FROM kag_edge_mentions m JOIN kag_chunks k ON k.id = m.chunk_id "
    "WHERE m.collection = kag_edges.collection AND m.src = kag_edges.src "
    "AND m.dst = kag_edges.dst AND m.relation = kag_edges.relation "
    "AND m.access_scope = kag_edges.access_scope)")


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
            f"AND {_ENTITY_EVIDENCE} ORDER BY mentions DESC LIMIT ?",
            tuple(params + [limit])).fetchall()
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
                f"AND (src IN ({ph}) OR dst IN ({ph})) AND {_EDGE_EVIDENCE}",
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
                f"AND norm IN ({ph}) AND {_LIVE_CHUNK} LIMIT 20",
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
