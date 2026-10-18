"""Graph-grounded table selection: from a question to the tables that answer it.

The metadata graph knows every discovered table's columns and how the tables
join. This module ranks a source's tables against a question by their names
AND their columns — "net sales by customer segment" finds the table holding
net_sales and the one holding segment even though neither name appears — and
then adds the tables needed to JOIN them (retail_orders between customers and
order_items), with the join conditions as hints for the SQL agent.

Used by chat's single-source agent (chat._grounding), by the multi-source
planners' roster (agent_graph._roster_digest lists a source's most relevant
tables first), and by each planned node's worker.

Design:
- PERMISSION-BOUND. It only ever considers `allowed` — the tables the caller's
  role may read in this turn — and relationships between two of them.
- METADATA ONLY, NO WAREHOUSE CALL. Columns come from the graph's record, so
  grounding a wide source costs nothing at question time. A source that has
  not been discovered returns None and the caller falls back to name matching.
- RARE WORDS WEIGH MORE. A term every table shares ("retail") says nothing;
  each term is weighted by how few tables carry it.
- GOVERNED. Columns are shaped by governance.governed_schema, exactly as the
  catalog's schema endpoint shapes them.
- DESCRIPTIONS COUNT. A word a table or column description uses is evidence
  too, weaker than a name.
- BY MEANING, WHEN HARRIER IS THERE. With HARRIER_EMBED_URL set, each
  object's name, description and columns are embedded as it is discovered
  (index), and a question is matched by meaning as well as by words: "client
  turnover" finds customers and revenue. Retrieval stays verified: only the
  caller's tables, only vectors of the object as it is NOW (its fingerprint),
  and a table found by meaning alone must clear a higher bar and is labelled
  "≈meaning" so the agent checks its columns before relying on it.
"""
import json
import math
import os
import time

from . import db, embed, governance, identifiers, metarelations

SEMANTIC = "≈meaning"
_INSTRUCT = ("Given an analytics question, retrieve the database tables and views whose "
             "data answers it")


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

_STOP = {"the", "and", "for", "with", "show", "what", "which", "how", "many", "much", "per",
         "over", "last", "this", "that", "from", "table", "tables", "data", "chart", "level",
         "give", "get", "top", "all", "each", "most", "least", "by", "of", "in", "on", "to",
         "who", "whose", "are", "was", "were", "has", "have", "had", "did", "does", "than",
         "into", "across", "between", "where", "when", "our", "their", "its", "any", "every",
         "brought", "bring", "generate", "generated", "made", "make", "list", "find", "tell"}
_GENERIC_NAME = {"id", "key", "code", "no", "num", "number", "name", "fact", "dim", "tbl",
                 "vw", "view"}


def _words(text, vocab=None):
    """Singular, expanded words — a question's or an identifier's, in any
    naming convention (identifiers.py): OrderQty → order quantity, prod_desc →
    product description."""
    return [w for w in identifiers.words(text, vocab) if len(w) >= 2 and w not in _STOP]


def _terms(prompt):
    """Single words and adjacent pairs ("net sales" → net_sale)."""
    words = _words(prompt)
    singles = [w for w in words if len(w) >= 3]
    pairs = [f"{a}_{b}" for a, b in zip(words, words[1:])]
    return singles, pairs


def _objects(source, allowed):
    """{name: {"kind", "description", "fingerprint", "columns"}} for the
    discovered objects in `allowed`."""
    allowed = set(allowed or [])
    if not allowed:
        return {}
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, name, kind, description, fingerprint FROM meta_objects "
            "WHERE source=? AND removed_at IS NULL", (source,)).fetchall()]
        rows = [r for r in rows if r["name"] in allowed]
        out = {r["name"]: {"kind": r["kind"], "description": r["description"],
                           "fingerprint": r["fingerprint"], "columns": []} for r in rows}
        ids = {r["id"]: r["name"] for r in rows}
        keys = list(ids)
        for i in range(0, len(keys), 400):
            batch = keys[i:i + 400]
            ph = ",".join("?" for _ in batch)
            for col in c.execute(f"SELECT object_id, name, type, description FROM meta_columns "
                                 f"WHERE object_id IN ({ph}) ORDER BY object_id, ordinal",
                                 tuple(batch)).fetchall():
                entry = {"name": col["name"], "type": col["type"]}
                if col["description"]:
                    entry["description"] = col["description"]
                out[ids[col["object_id"]]]["columns"].append(entry)
    return out


def _vocabulary(objects):
    """Per object: its name words and column words (singular, abbreviations
    expanded against the schema's own words), every column's full name, and
    the words of its descriptions — the units a question is matched against."""
    schema = metarelations._vocab(objects)
    vocab = {}
    for name, obj in objects.items():
        name_words = set(identifiers.table_words(name, schema)) - _GENERIC_NAME
        col_words, col_names, desc_words = set(), set(), set()
        for col in obj["columns"]:
            parts = [w for w in identifiers.words(col["name"], schema) if len(w) >= 2]
            col_words |= set(parts) - _GENERIC_NAME
            col_names.add("_".join(parts))
            desc_words |= set(_words(col.get("description") or ""))
        desc_words |= set(_words(obj.get("description") or ""))
        vocab[name] = (name_words, col_words, col_names, desc_words - col_words - name_words)
    return vocab


def score(prompt, objects, definitions=()):
    """[(table, score, why)] best first, for tables the question touches.
    `definitions` (semantic-layer metrics and dimensions) lend their names and
    synonyms to their table, weighted like the table's own name: "turnover",
    a synonym of a certified revenue metric, points at the revenue table."""
    singles, pairs = _terms(prompt)
    vocab = _vocabulary(objects)
    for d in definitions:
        if d.get("object") in vocab:
            name_words, col_words, col_names, desc_words = vocab[d["object"]]
            for phrase in [d["name"]] + list(d.get("synonyms") or []):
                parts = [w for w in identifiers.words(phrase) if len(w) >= 2]
                name_words = name_words | set(parts) - _GENERIC_NAME
                col_names = col_names | {"_".join(parts)}
            vocab[d["object"]] = (name_words, col_words, col_names, desc_words)
    n = max(1, len(vocab))

    def idf(term, field):
        df = sum(1 for v in vocab.values() if term in v[field])
        return math.log(1 + n / df) if df else 0.0

    out = []
    for table, (name_words, col_words, col_names, desc_words) in vocab.items():
        total, why = 0.0, []
        for term in dict.fromkeys(singles):
            if term in name_words:
                total += 3 * idf(term, 0)
                why.append(term)
            elif term in col_words:
                total += idf(term, 1)
                why.append(term)
            elif term in desc_words:
                # Said in a description, not a name: evidence, but weaker.
                total += 0.5 * idf(term, 3)
                why.append(term)
        for pair in dict.fromkeys(pairs):
            if pair in col_names:
                total += 2 * idf(pair, 2)
                why.append(pair)
        if total > 0:
            out.append((table, round(total, 3), why))
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


def _document(name, obj):
    """What Harrier reads for one object: its name in words, its description,
    and its columns with theirs."""
    parts = [" ".join(identifiers.words(name)) or name]
    if obj.get("description"):
        parts.append(obj["description"])
    cols = [" ".join(identifiers.words(c["name"])) +
            (f" ({c['description']})" if c.get("description") else "")
            for c in obj["columns"][:200]]
    if cols:
        parts.append("columns: " + ", ".join(cols))
    return ". ".join(parts)[:4000]


def index(source, budget=None):
    """Embed the source's objects whose metadata changed since they were last
    embedded (and forget objects that are gone). At most `budget` per call
    (STUDIO_METAGRAPH_EMBED_BATCH, 100), so a large source catches up a batch
    at a time; stops at the first failed call. Returns how many were embedded.
    Does nothing when Harrier is not configured."""
    if not embed.available():
        return 0
    budget = budget or max(1, int(_env_float("STUDIO_METAGRAPH_EMBED_BATCH", 100)))
    with db.connect() as c:
        names = [r["name"] for r in c.execute(
            "SELECT name FROM meta_objects WHERE source=? AND removed_at IS NULL",
            (source,)).fetchall()]
        known = {r["object"]: r["fingerprint"] for r in c.execute(
            "SELECT object, fingerprint FROM meta_embeddings WHERE source=?",
            (source,)).fetchall()}
        gone = [n for n in known if n not in set(names)]
        for n in gone:
            c.execute("DELETE FROM meta_embeddings WHERE source=? AND object=?", (source, n))
        c.commit()
    objects = _objects(source, names)
    todo = [n for n, o in sorted(objects.items()) if known.get(n) != o["fingerprint"]][:budget]
    done = 0
    for name in todo:
        vec = embed.embed(_document(name, objects[name]), kind="document")
        if vec is None:
            break                           # the endpoint is down: try again next time
        with db.connect() as c:
            c.execute("DELETE FROM meta_embeddings WHERE source=? AND object=?", (source, name))
            c.execute("INSERT INTO meta_embeddings (source, object, fingerprint, vector, "
                      "updated_at) VALUES (?,?,?,?,?)",
                      (source, name, objects[name]["fingerprint"], json.dumps(vec), time.time()))
            c.commit()
        done += 1
    return done


def _by_meaning(source, prompt, objects, qvec=None):
    """{table: cosine} for the objects with a CURRENT vector, or {} — no
    Harrier, nothing indexed, or the question could not be embedded. `qvec`:
    the question already embedded (one embedding serves every source)."""
    if not embed.available() or not objects:
        return {}
    with db.connect() as c:
        rows = c.execute("SELECT object, fingerprint, vector FROM meta_embeddings "
                         "WHERE source=?", (source,)).fetchall()
    vectors = {r["object"]: json.loads(r["vector"]) for r in rows
               if r["object"] in objects and r["fingerprint"] == objects[r["object"]]["fingerprint"]}
    if not vectors:
        return {}
    q = qvec if qvec is not None else query_vector(prompt)
    if q is None:
        return {}
    return {name: embed.cosine(q, vec) for name, vec in vectors.items()}


def _with_meaning(ranked, meaning):
    """The lexical ranking with similarity by meaning folded in: a table that
    also matched by words gains up to 4 points; one found by meaning alone
    enters only above STUDIO_METAGRAPH_MEANING_ONLY_MIN (0.65)."""
    floor = _env_float("STUDIO_METAGRAPH_MEANING_MIN", 0.5)
    alone = _env_float("STUDIO_METAGRAPH_MEANING_ONLY_MIN", 0.65)
    merged = {t: (s, list(w)) for t, s, w in ranked}
    for table, cos in meaning.items():
        if cos < floor:
            continue
        boost = round(4 * (cos - floor) / max(1e-6, 1 - floor), 3)
        if table in merged:
            merged[table] = (merged[table][0] + boost, merged[table][1] + [SEMANTIC])
        elif cos >= alone:
            merged[table] = (boost, [SEMANTIC])
    out = [(t, round(s, 3), w) for t, (s, w) in merged.items()]
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


def query_vector(prompt):
    """The question embedded for table retrieval, or None (no Harrier)."""
    if not embed.available():
        return None
    return embed.embed(prompt, kind="query", instruct=_INSTRUCT)


def discovered(source, allowed):
    """The metadata graph's record of `allowed` on `source`: {name: {"kind",
    "description", "fingerprint", "columns"}}, raw columns (govern them
    before showing or scoring them for a role)."""
    return _objects(source, allowed)


def relevance(source, prompt, objects, qvec=None):
    """[(table, score, why)] best first for one source's `objects`, by every
    signal grounding uses — names, columns, descriptions, the certified
    metrics' names and synonyms, and meaning (Harrier). What auto routing
    (autoroute.py) compares across sources."""
    ranked = score(prompt, objects, _definitions(source, objects))
    meaning = _by_meaning(source, prompt, objects, qvec)
    return _with_meaning(ranked, meaning) if meaning else ranked


def rank(source, prompt, allowed):
    """`allowed` reordered: the tables the question touches first (best first),
    then the rest in their given order. `allowed` unchanged when the source has
    no discovered metadata."""
    objects = _objects(source, allowed)
    if not objects:
        return list(allowed or [])
    hits = [t for t, _s, _w in score(prompt, objects)]
    return hits + [t for t in allowed if t not in set(hits)]


def ground(source, prompt, allowed, *, limit=6, min_confidence=0.6, cutoff=0.35):
    """The tables a question needs on one source, the join conditions between
    them, and their governed columns:
        {"tables": [...], "matched": {table: [terms]}, "bridges": [...],
         "joins": [relationship, ...], "schemas": {table: columns}}
    None when the source has no discovered metadata, or nothing matches.
    Only tables in `allowed`, and only joins between two of them."""
    objects = _objects(source, allowed)
    if not objects:
        return None
    defs = _definitions(source, objects)
    ranked = score(prompt, objects, defs)
    meaning = _by_meaning(source, prompt, objects)
    if meaning:
        ranked = _with_meaning(ranked, meaning)
    if not ranked:
        return None
    top = ranked[0][1]
    picked = [(t, s, w) for t, s, w in ranked if s >= cutoff * top]
    # Every term of the question needs a home: a table that is the best match
    # for a term no picked table covers joins even when another table matches
    # so many terms that it lifts the cutoff above it.
    covered = {term for _t, _s, why in picked for term in why}
    for t, s, why in ranked:
        new = set(why) - covered
        if new and all(t != p[0] for p in picked):
            picked.append((t, s, why))
            covered |= new
    picked = picked[:limit]
    matched = {t: w for t, _s, w in picked}
    tables = [t for t, _s, _w in picked]
    rels = metarelations.current(source, set(objects))
    # Joins connect base tables; a matching VIEW is offered alongside — it may
    # already answer the question with no join at all.
    base = [t for t in tables if objects[t]["kind"] != "view"]
    joins = []
    if len(base) > 1:
        joins, _missing = metarelations.join_path(rels, base, min_confidence)
        for rel in joins:
            for t in (rel["from_object"], rel["to_object"]):
                if t not in tables:
                    tables.append(t)
    views = {v: [r["to_object"] for r in rels
                 if r["kind"] == "derived_from" and r["from_object"] == v]
             for v in tables if objects[v]["kind"] == "view"}
    bridges = [t for t in tables if t not in matched]
    schemas = {t: governance.governed_schema(source, t, objects[t]["columns"])
               for t in tables}
    # The certified definitions on the grounded tables whose name or synonym
    # the question uses: the agent is told how the measure is computed.
    asked = set(_words(prompt))
    metrics = [d for d in defs if d["object"] in tables and asked & {
        w for phrase in [d["name"]] + list(d.get("synonyms") or [])
        for w in identifiers.words(phrase)}]
    return {"tables": tables, "matched": matched, "bridges": bridges, "joins": joins,
            "views": views, "metrics": metrics,
            "schemas": {t: cols for t, cols in schemas.items() if cols}}


def _definitions(source, objects):
    """Valid or unchecked semantic definitions on these tables — a broken or
    drifted one never steers grounding or the agent."""
    try:
        from . import metametrics
        return [d for d in metametrics.for_source(source, set(objects))
                if d["status"] not in metametrics.INVALID and d.get("object") in objects]
    except Exception:
        return []


def join_hint_lines(joins, views=None, metrics=None):
    """The join conditions as the agent reads them, with why each is believed,
    then any matching view and the tables it already joins, then the certified
    definitions the question asks for."""
    from . import metametrics
    lines = []
    for rel in joins:
        on = " AND ".join(f"{rel['from_object']}.{a} = {rel['to_object']}.{b}"
                          for a, b in zip(rel["from_columns"], rel["to_columns"]))
        if metarelations.trusted(rel):
            lines.append(f"{on}  ({rel['state']}, confidence {rel['confidence']})")
        else:
            # A guess from column names, not a business relationship: the
            # agent is told so, and to check the values match before using it.
            lines.append(f"{on}  (UNVERIFIED — inferred from column names, confidence "
                         f"{rel['confidence']}; check the columns hold the same values "
                         f"before relying on it)")
    for view, reads in (views or {}).items():
        if reads:
            lines.append(f"{view} is a view over {', '.join(reads)} — it may answer this "
                         f"without joins")
    for d in metrics or []:
        lines.append(metametrics.describe(d) + " — use this definition")
    return lines


def worker_context(source, question, allowed, loaded=()):
    """(focus schemas, join hint lines) for one data worker's question — what
    chat gives its agent, for a planned node or a fan-out worker. Empty when
    the source has no discovered metadata or nothing matches. Never raises."""
    try:
        grounded = ground(source, question, allowed)
    except Exception:
        return {}, []
    if not grounded:
        return {}, []
    loaded = set(loaded or ())
    return ({t: c for t, c in grounded["schemas"].items() if t not in loaded},
            join_hint_lines(grounded["joins"], grounded["views"], grounded["metrics"]))
