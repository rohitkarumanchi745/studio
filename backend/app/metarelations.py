"""Relationships in the metadata graph: how a source's tables join, and what
each view or table is derived from — with the evidence for every edge.

Evidence, strongest first:

  declared  a foreign key the catalog declares                  confidence 1.0
  observed  a join in a view definition                                    0.9
            a join in Studio's own successful queries          0.75 → 0.9 by count
  inferred  a naming pattern — retail_orders.customer_id
            → retail_customers.customer_id                          up to 0.85

Every relationship carries its state (declared / observed / inferred, or an
administrator's verified / rejected), a confidence, and a list of evidence
labels saying exactly why it is believed. A review outlives recomputation:
a rejected candidate stays rejected, a verified one stays while both of its
tables exist.

Design:
- METADATA AND STUDIO'S OWN HISTORY ONLY. Keys from the catalog, view text,
  the SQL Studio itself ran (audit_log). No value is sampled from a table.
- PURE CORE. extract_joins, infer and build take plain data and return plain
  data; relate() does the reading and the one-transaction write.
- TOKENS, NOT REGEXES. Join predicates are read from queryguard's tokenizer,
  so comments and string literals cannot forge one.
- PERMISSION-AWARE. A relationship is shown only to a role that may read both
  of its tables (metagraph.py applies that on every read).
"""
import hashlib
import json
import logging
import re
import time
import uuid

from . import db, queryguard

log = logging.getLogger("studio.metagraph")

_KEY_SUFFIXES = ("_id", "_key", "_code", "_no", "_number", "_num", "id")
_JOIN_STOP = {"on", "join", "inner", "left", "right", "full", "outer", "cross", "where",
              "group", "order", "limit", "union", "having", "using", "natural", "lateral",
              "window", "qualify", "except", "intersect", "as", "with", "select", "from",
              "and", "or", "not", "in", "is", "null", "between", "like", "case", "when",
              "then", "else", "end", "set", "values", "into", "fetch", "offset"}
_HISTORY_LIMIT = 2000


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta_relationships (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            kind TEXT NOT NULL,
            from_object TEXT NOT NULL,
            from_columns TEXT NOT NULL,
            to_object TEXT NOT NULL,
            to_columns TEXT NOT NULL,
            directed INTEGER NOT NULL,
            state TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence TEXT NOT NULL,
            review TEXT,
            reviewed_by TEXT,
            reviewed_at REAL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            removed_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_rel_key ON meta_relationships(
            source, kind, from_object, from_columns, to_object, to_columns);
        CREATE INDEX IF NOT EXISTS idx_meta_rel_source ON meta_relationships(source);
        """)
        c.commit()


# ── Naming ───────────────────────────────────────────────────────────────

def _bare(name):
    return str(name).split(".")[-1].lower()


def _stem(column):
    """'customer_id' → 'customer'; None for a column that does not look like a key."""
    col = column.lower()
    for suffix in _KEY_SUFFIXES:
        if col.endswith(suffix) and len(col) > len(suffix) + 1:
            return col[: -len(suffix)].rstrip("_") or None
    return None


def _forms(stem):
    forms = {stem, stem + "s", stem + "es"}
    if stem.endswith("y"):
        forms.add(stem[:-1] + "ies")
    return forms


def _named_for(table, stem):
    """Is `table` the table a `<stem>_id` column points at? retail_customers is
    named for 'customer'; retail_order_items is not named for 'order'."""
    bare = _bare(table)
    return any(bare == f or bare.endswith("_" + f) for f in _forms(stem))


def _family(sql_type):
    t = (sql_type or "").lower()
    if any(k in t for k in ("int", "number", "numeric", "decimal", "double", "float", "real",
                            "bigint", "long", "short", "byte")):
        return "number"
    if any(k in t for k in ("char", "text", "string", "varchar", "uuid")):
        return "text"
    if any(k in t for k in ("date", "time")):
        return "time"
    if "bool" in t:
        return "bool"
    return t or "unknown"


# ── Joins read from SQL ──────────────────────────────────────────────────

def _ref_end(toks, i):
    """Index just past a dotted reference starting at i."""
    j = i + 1
    while j + 1 < len(toks) and toks[j] == ("punct", ".") and toks[j + 1][0] == "word":
        j += 2
    return j


def _aliases(toks, resolve):
    """{alias or table name (lower): object} for every FROM/JOIN target."""
    out, order = {}, []
    for parts, at in queryguard._table_refs(toks):
        obj = resolve([p.text for p in parts])
        if obj is None:
            continue
        order.append((at, obj))
        out[parts[-1].text.lower()] = obj
        j = _ref_end(toks, at)
        if j < len(toks) and toks[j][0] == "word" and toks[j][1].lower() == "as":
            j += 1
        if j < len(toks) and toks[j][0] == "word" and toks[j][1].lower() not in _JOIN_STOP:
            out[toks[j][1].lower()] = obj
    return out, [obj for _, obj in sorted(order)]


def _qualified_column(toks, i):
    """(qualifier parts, column, index past) for `a.b` / `s.a.b` at i, or None."""
    if i >= len(toks) or toks[i][0] != "word":
        return None
    parts, j = [toks[i][1]], i + 1
    while j + 1 < len(toks) and toks[j] == ("punct", ".") and toks[j + 1][0] == "word":
        parts.append(toks[j + 1][1])
        j += 2
    if len(parts) < 2:
        return None
    return parts[:-1], parts[-1], j


def extract_joins(sql, resolve):
    """[((table, column), (table, column)), ...] equi-joins in `sql` between two
    different known tables: `x.col = y.col` predicates whose qualifiers name a
    FROM/JOIN target (or its alias), and `JOIN t USING (col)`. `resolve(parts)`
    maps a written reference to a known object name, or None."""
    try:
        toks, _ = queryguard._tokens(sql)
    except Exception:
        return []
    aliases, ordered = _aliases(toks, resolve)
    if len(set(aliases.values())) < 2:
        return []
    out = []
    for i in range(len(toks)):
        left = _qualified_column(toks, i)
        if not left or (i > 0 and toks[i - 1] == ("punct", ".")):
            continue
        _q, _c, j = left
        if j >= len(toks) or toks[j] != ("punct", "="):
            continue
        right = _qualified_column(toks, j + 1)
        if not right:
            continue
        a = aliases.get(left[0][-1].lower())
        b = aliases.get(right[0][-1].lower())
        if a and b and a != b:
            out.append(((a, left[1]), (b, right[1])))
    # JOIN t USING (c1, c2): the joined table and the one before it share them.
    for parts, at in queryguard._table_refs(toks):
        obj = resolve([p.text for p in parts])
        j = _ref_end(toks, at)
        if j + 1 < len(toks) and toks[j][0] == "word" and toks[j][1].lower() == "as":
            j += 2
        elif j < len(toks) and toks[j][0] == "word" and toks[j][1].lower() not in _JOIN_STOP:
            j += 1
        if obj is None or j + 1 >= len(toks) or toks[j][0] != "word" \
                or toks[j][1].lower() != "using" or toks[j + 1] != ("punct", "("):
            continue
        before = [o for o in ordered[:ordered.index(obj)] if o != obj] if obj in ordered else []
        if not before:
            continue
        k = j + 2
        while k < len(toks) and toks[k] != ("punct", ")"):
            if toks[k][0] == "word":
                out.append(((before[-1], toks[k][1]), (obj, toks[k][1])))
            k += 1
    return out


def tables_read(sql, resolve):
    """Every known object `sql` reads (a view's lineage)."""
    try:
        toks, _ = queryguard._tokens(sql)
    except Exception:
        return []
    found = []
    for parts, _ in queryguard._table_refs(toks):
        obj = resolve([p.text for p in parts])
        if obj is not None and obj not in found:
            found.append(obj)
    return found


def resolver(objects):
    """resolve(parts) for a source's objects: a full dotted name matches
    exactly; a bare name matches the one object whose last part it is."""
    full = {name.lower(): name for name in objects}
    by_bare = {}
    for name in objects:
        by_bare.setdefault(_bare(name), []).append(name)

    def resolve(parts):
        written = ".".join(parts).lower()
        if written in full:
            return full[written]
        candidates = by_bare.get(parts[-1].lower(), [])
        return candidates[0] if len(candidates) == 1 else None
    return resolve


# ── Building the relationship set (pure) ─────────────────────────────────

def _columns(objects, name):
    return {c["name"].lower(): c for c in objects.get(name, {}).get("columns") or []}


def _orient(a, a_cols, b, b_cols, objects, pks):
    """(child, child cols, parent, parent cols, directed): the side holding the
    reference is the child. Known when the other side's columns are its
    declared primary key or it is the table the column is named for."""
    def parent_side(table, cols, other_cols):
        if [c.lower() for c in pks.get(table, [])] == [c.lower() for c in cols]:
            return True
        stems = {_stem(c) for c in other_cols} | {_stem(c) for c in cols}
        return any(s and _named_for(table, s) for s in stems)
    if parent_side(b, b_cols, a_cols) and not parent_side(a, a_cols, b_cols):
        return a, a_cols, b, b_cols, True
    if parent_side(a, a_cols, b_cols) and not parent_side(b, b_cols, a_cols):
        return b, b_cols, a, a_cols, True
    (x, xc), (y, yc) = sorted([(a, a_cols), (b, b_cols)])
    return x, xc, y, yc, False


def infer(objects, pks):
    """Name-pattern candidates: a key-like column in one table pointing at the
    table named for it. {key: [evidence]} keyed like build()."""
    tables = [n for n, o in objects.items() if o.get("kind") != "view"]
    out = {}
    for child in tables:
        for col in objects[child].get("columns") or []:
            stem = _stem(col["name"])
            if not stem:
                continue
            for parent in tables:
                if parent == child or not _named_for(parent, stem):
                    continue
                pcols = _columns(objects, parent)
                if col["name"].lower() in pcols:
                    pcol, conf, why = pcols[col["name"].lower()], 0.75, "same column name"
                elif "id" in pcols:
                    pcol, conf, why = pcols["id"], 0.65, f"{_bare(parent)}.id"
                else:
                    continue
                labels = [f"name pattern: {_bare(child)}.{col['name']} points at "
                          f"{_bare(parent)} ({why})"]
                parent_key = [c.lower() for c in pks.get(parent, [])]
                if parent_key == [pcol["name"].lower()]:
                    conf += 0.1
                    labels.append(f"{_bare(parent)}.{pcol['name']} is its declared primary key")
                elif (objects[parent].get("columns") or [{}])[0].get("name", "").lower() \
                        == pcol["name"].lower():
                    conf += 0.05
                    labels.append(f"{_bare(parent)}.{pcol['name']} is its first column")
                fa, fb = _family(col.get("type")), _family(pcol.get("type"))
                if fa == fb or "unknown" in (fa, fb):
                    labels.append(f"types match ({col.get('type') or '?'} ↔ "
                                  f"{pcol.get('type') or '?'})")
                else:
                    conf -= 0.3
                    labels.append(f"types differ ({col.get('type')} ↔ {pcol.get('type')})")
                key = ("join", child, (col["name"],), parent, (pcol["name"],), True)
                out.setdefault(key, []).append(
                    {"type": "name_pattern", "label": "; ".join(labels),
                     "confidence": round(min(conf, 0.85), 2)})
    return out


def build(objects, constraints=None, views=None, lineage=None, history=()):
    """All relationships for one source, merged by edge:
    {key: {"evidence": [...], "state", "confidence"}} where key is
    (kind, from_object, from_columns, to_object, to_columns, directed)."""
    constraints = constraints or {}
    pks = {t: list(c) for t, c in (constraints.get("primary_keys") or {}).items()
           if t in objects}
    edges = {}

    def add(key, evidence):
        edges.setdefault(key, []).append(evidence)

    for fk in constraints.get("foreign_keys") or []:
        if fk.get("from") in objects and fk.get("to") in objects:
            add(("join", fk["from"], tuple(fk["from_columns"]), fk["to"],
                 tuple(fk["to_columns"]), True),
                {"type": "declared_fk", "label": f"declared foreign key {fk.get('name') or ''}"
                 .strip(), "confidence": 1.0})

    resolve = resolver(objects)

    def joins(sql):
        seen = set()
        for (a, ac), (b, bc) in extract_joins(sql, resolve):
            if ac.lower() not in _columns(objects, a) or bc.lower() not in _columns(objects, b):
                continue
            ca = _columns(objects, a)[ac.lower()]["name"]
            cb = _columns(objects, b)[bc.lower()]["name"]
            child, ccols, parent, pcols, directed = _orient(a, (ca,), b, (cb,), objects, pks)
            key = ("join", child, ccols, parent, pcols, directed)
            if key not in seen:
                seen.add(key)
                yield key

    for view, sql in (views or {}).items():
        # A view's joins relate its tables even when the view itself is not in
        # the catalog (Postgres lists base tables only). Its name is evidence
        # only where it is an object, so current() can hide it from a role
        # that may not see it.
        known = view in objects
        for key in joins(sql):
            add(key, {"type": "view_join", "confidence": 0.9,
                      **({"label": f"joined this way in view {_bare(view)}", "object": view}
                         if known else {"label": "joined this way in a view definition"})})
        if not known:
            continue
        for table in tables_read(sql, resolve):
            if table != view:
                add(("derived_from", view, (), table, (), True),
                    {"type": "view_lineage", "label": f"view {_bare(view)} reads {_bare(table)}",
                     "confidence": 1.0})

    for upstream, downstream in lineage or []:
        if upstream in objects and downstream in objects and upstream != downstream:
            add(("derived_from", downstream, (), upstream, (), True),
                {"type": "engine_lineage", "label": "recorded by the engine's lineage",
                 "confidence": 1.0})

    counts = {}
    for sql in history:
        for key in joins(sql):
            counts[key] = counts.get(key, 0) + 1
    for key, n in counts.items():
        add(key, {"type": "query_join",
                  "label": f"joined this way in {n} successful Studio "
                           f"{'query' if n == 1 else 'queries'}",
                  "confidence": round(min(0.9, 0.75 + 0.03 * (n - 1)), 2)})

    for key, evidence in infer(objects, pks).items():
        for e in evidence:
            add(key, e)

    # An undirected observation and a directed one of the same pair are one edge.
    directed = {(k[0], k[1], k[2], k[3], k[4]) for k in edges if k[5]}
    merged = {}
    for key, evidence in edges.items():
        if not key[5]:
            for flipped in ((key[0], key[1], key[2], key[3], key[4]),
                            (key[0], key[3], key[4], key[1], key[2])):
                if flipped in directed:
                    key = flipped + (True,)
                    break
        merged.setdefault(key, []).extend(evidence)

    out = {}
    for key, evidence in merged.items():
        types = {e["type"] for e in evidence}
        state = ("declared" if "declared_fk" in types or key[0] == "derived_from"
                 else "observed" if types & {"view_join", "query_join"} else "inferred")
        out[key] = {"evidence": evidence, "state": state,
                    "confidence": max(e["confidence"] for e in evidence)}
    return out


# ── Reading a source and writing its relationships ───────────────────────

def _objects(source):
    with db.connect() as c:
        rows = c.execute("SELECT id, name, kind FROM meta_objects WHERE source=? "
                         "AND removed_at IS NULL", (source,)).fetchall()
        objects = {r["name"]: {"kind": r["kind"], "columns": []} for r in rows}
        ids = {r["id"]: r["name"] for r in rows}
        keys = list(ids)
        for i in range(0, len(keys), 400):
            batch = keys[i:i + 400]
            ph = ",".join("?" for _ in batch)
            for col in c.execute(f"SELECT object_id, name, type FROM meta_columns WHERE "
                                 f"object_id IN ({ph}) ORDER BY object_id, ordinal",
                                 tuple(batch)).fetchall():
                objects[ids[col["object_id"]]]["columns"].append(
                    {"name": col["name"], "type": col["type"]})
    return objects


def _history(source):
    """The SQL Studio itself ran successfully against this source, newest first."""
    with db.connect() as c:
        rows = c.execute("SELECT sql FROM audit_log WHERE source=? AND ok=1 AND sql IS NOT NULL "
                         "ORDER BY created_at DESC LIMIT ?", (source, _HISTORY_LIMIT)).fetchall()
    return [r["sql"] for r in rows if r["sql"] and "join" in r["sql"].lower()]


def _read(connector, method):
    """One optional evidence read: None when the connector does not offer it
    or the read fails — evidence is additive, never a reason to stop."""
    call = getattr(connector, method, None)
    if call is None:
        return None
    try:
        return call()
    except Exception:
        log.warning("metagraph: %s failed", method, exc_info=True)
        return None


def relate(source, lock=None):
    """Recompute one source's relationships from its current objects, its
    catalog's keys, its view definitions and lineage, and Studio's query
    history; apply them in one transaction. Returns counts."""
    from .connectors import get_connector
    objects = _objects(source)
    connector = get_connector(source)
    rels = build(objects,
                 constraints=_read(connector, "describe_constraints"),
                 views=_read(connector, "view_definitions"),
                 lineage=_read(connector, "lineage"),
                 history=_history(source))
    now = time.time()
    added = updated = removed = 0
    with db.connect() as c:
        if lock:
            lock(c, source)
        existing = {(r["kind"], r["from_object"], r["from_columns"], r["to_object"],
                     r["to_columns"]): dict(r) for r in c.execute(
            "SELECT * FROM meta_relationships WHERE source=?", (source,)).fetchall()}
        seen = set()
        for key, rel in rels.items():
            kind, frm, fcols, to, tcols, directed = key
            k = (kind, frm, json.dumps(list(fcols)), to, json.dumps(list(tcols)))
            seen.add(k)
            row = existing.get(k)
            evidence = json.dumps(rel["evidence"])
            if row is None:
                c.execute(
                    "INSERT INTO meta_relationships (id, source, kind, from_object, from_columns, "
                    "to_object, to_columns, directed, state, confidence, evidence, "
                    "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_rel_id(source, k), source, kind, frm, k[2], to, k[4],
                     1 if directed else 0, rel["state"], rel["confidence"], evidence, now, now))
                added += 1
            else:
                c.execute(
                    "UPDATE meta_relationships SET directed=?, state=?, confidence=?, evidence=?, "
                    "last_seen_at=?, removed_at=NULL WHERE id=?",
                    (1 if directed else 0, rel["state"], rel["confidence"], evidence, now,
                     row["id"]))
                updated += 1
        for k, row in existing.items():
            if k in seen or row["removed_at"] is not None:
                continue
            # A verified relationship outlives its evidence while both tables exist.
            if row["review"] == "verified" and row["from_object"] in objects \
                    and row["to_object"] in objects:
                continue
            c.execute("UPDATE meta_relationships SET removed_at=? WHERE id=?", (now, row["id"]))
            removed += 1
        c.commit()
    return {"relationships": len(rels), "added": added, "updated": updated, "removed": removed}


def _rel_id(source, key):
    return "rel-" + hashlib.sha256(json.dumps([source, *key]).encode()).hexdigest()[:24]


# ── Reading them back ────────────────────────────────────────────────────

def _effective(row):
    """A review overrides what the evidence says."""
    out = dict(row)
    out["from_columns"] = json.loads(row["from_columns"])
    out["to_columns"] = json.loads(row["to_columns"])
    out["evidence"] = json.loads(row["evidence"])
    out["directed"] = bool(row["directed"])
    if row["review"] == "verified":
        out["state"], out["confidence"] = "verified", 1.0
        out["evidence"] = out["evidence"] + [{"type": "review",
                                              "label": "verified by an administrator",
                                              "confidence": 1.0}]
    elif row["review"] == "rejected":
        out["state"] = "rejected"
        out["evidence"] = out["evidence"] + [{"type": "review",
                                              "label": "rejected by an administrator",
                                              "confidence": 0.0}]
    for k in ("removed_at", "reviewed_by", "review", "source"):
        out.pop(k, None)
    return out


def _redact(evidence, visible):
    """Evidence naming an object the reader may not see (a view) keeps its
    weight but loses the name."""
    out = []
    for e in evidence:
        if e.get("object") and e["object"] not in visible:
            e = {k: v for k, v in e.items() if k != "object"}
            e["label"] = "joined this way in a view definition"
        out.append(e)
    return out


def current(source, visible, *, include_rejected=False):
    """Current relationships whose BOTH tables are in `visible`."""
    with db.connect() as c:
        rows = c.execute("SELECT * FROM meta_relationships WHERE source=? AND removed_at IS NULL "
                         "ORDER BY kind, from_object, to_object", (source,)).fetchall()
    out = []
    for row in rows:
        if row["from_object"] not in visible or row["to_object"] not in visible:
            continue
        rel = _effective(row)
        if rel["state"] == "rejected" and not include_rejected:
            continue
        rel["evidence"] = _redact(rel["evidence"], visible)
        out.append(rel)
    return out


def review(source, rel_id, decision, user):
    if decision not in ("verified", "rejected", "clear"):
        raise ValueError("decision must be verified, rejected or clear")
    with db.connect() as c:
        cur = c.execute("UPDATE meta_relationships SET review=?, reviewed_by=?, reviewed_at=? "
                        "WHERE id=? AND source=?",
                        (None if decision == "clear" else decision, user["id"], time.time(),
                         rel_id, source))
        c.commit()
    return bool(getattr(cur, "rowcount", 0))


def join_path(relationships, tables, min_confidence=0.6):
    """The cheapest join tree connecting `tables`, over join relationships at or
    above `min_confidence`: each hop costs 1 + (1 - confidence), so fewer and
    surer joins win. Returns (joins, missing) — `missing` are tables no path
    reaches."""
    import heapq
    graph = {}
    for rel in relationships:
        if rel["kind"] != "join" or rel["state"] == "rejected" \
                or rel["confidence"] < min_confidence:
            continue
        cost = 1.0 + (1.0 - rel["confidence"])
        graph.setdefault(rel["from_object"], []).append((rel["to_object"], cost, rel))
        graph.setdefault(rel["to_object"], []).append((rel["from_object"], cost, rel))
    targets = list(dict.fromkeys(tables))
    if not targets:
        return [], []
    tree, used, missing = {targets[0]}, [], []
    for target in targets[1:]:
        if target in tree:
            continue
        # Dijkstra from the whole tree to this target.
        dist, prev, heap = {t: 0.0 for t in tree}, {}, [(0.0, t) for t in tree]
        heapq.heapify(heap)
        while heap:
            d, node = heapq.heappop(heap)
            if node == target or d > dist.get(node, float("inf")):
                if node == target:
                    break
                continue
            for nxt, cost, rel in graph.get(node, []):
                nd = d + cost
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt], prev[nxt] = nd, (node, rel)
                    heapq.heappush(heap, (nd, nxt))
        if target not in dist:
            missing.append(target)
            continue
        node = target
        while node not in tree:
            parent, rel = prev[node]
            if rel not in used:
                used.append(rel)
            tree.add(node)
            node = parent
    return used, missing
