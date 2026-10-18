"""Certified metrics in the metadata graph.

The semantic YAML (semantic.py) is the source of truth for what a metric
MEANS; the metadata graph knows what the warehouse actually HOLDS. This module
joins the two: every metric and dimension is linked to its source table and
to the columns its expression and filters reference, checked against the
graph's current record, and given a version history from the applied YAML
documents semantic.py already keeps.

  revenue (sum of quantity * unit_price * (1 - discount_rate))
    → table order_lines → columns quantity, unit_price, discount_rate
    → valid | broken: column discount_rate not found
            | drifted: column quantity changed type from INTEGER to TEXT
            | unchecked (not discovered)

DRIFT. When a definition is first seen valid, the types of the columns it reads
are recorded as its baseline. A later change of a column's type family (a
number becoming text) makes it drifted; a change of detail inside a family
(INT → BIGINT, DECIMAL(10,2) → DECIMAL(12,2)) is noted but does not. A new
version of the definition gets a new baseline; an administrator or the table's
owner can revalidate a drifted one against the columns as they are now.

A broken or drifted definition is never answered from: the semantic layer
declines it and says why, and the source's metadata health lists it. A valid one is offered to
the SQL agent as the certified way to compute that measure, and its synonyms
point graph grounding at its table.

Design:
- ANY SCHEMA. Nothing here knows a table name. References are read with the
  query guard's tokenizer (quoted "UnitPrice" and bare unit_price alike) and
  matched to the table's discovered columns case-insensitively; a function
  call, a table qualifier, a type after AS, a typed literal or a SQL keyword
  is never mistaken for a column.
- ON DEMAND. Links and checks are computed from the YAML and the graph's
  record when asked — no second copy to drift.
- PERMISSION-AWARE. A definition is shown only to a role that may read its
  table.
"""
import hashlib
import json

import time

from . import db, queryguard, semantic
from .metarelations import _family

# Words in an expression that are SQL, never a column.
_SQL_WORDS = {
    "and", "or", "not", "null", "is", "in", "like", "ilike", "between", "as", "case",
    "when", "then", "else", "end", "distinct", "true", "false", "interval", "cast",
    "exists", "all", "any", "some", "asc", "desc", "nulls", "first", "last", "over",
    "partition", "by", "order", "rows", "range", "unbounded", "preceding", "following",
    "current", "row", "filter", "within", "group", "escape", "collate", "at", "zone",
    "year", "quarter", "month", "week", "day", "hour", "minute", "second", "epoch",
    "date", "time", "timestamp", "int", "integer", "bigint", "smallint", "decimal",
    "numeric", "float", "double", "real", "varchar", "char", "text", "string", "boolean",
    "bool", "precision", "from", "for", "using", "on", "to", "with", "without",
}


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta_metric_baselines (
            source TEXT NOT NULL,
            tbl TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            signature TEXT NOT NULL,
            columns TEXT NOT NULL,
            recorded_at REAL NOT NULL,
            recorded_by TEXT,
            PRIMARY KEY (source, tbl, kind, name, signature)
        );
        """)
        c.commit()


INVALID = ("broken", "drifted")


def references(expr):
    """Column names `expr` reads, in order, deduplicated. A word followed by
    "(" is a function, one followed by "." a qualifier, one after AS a type or
    alias, one before a string literal a typed literal (DATE '2024-01-01')."""
    if not expr or expr.strip() == "*":
        return []
    try:
        toks, _ = queryguard._tokens(expr)
    except Exception:
        return []
    out = []
    for i, (kind, text) in enumerate(toks):
        if kind not in ("word", "ident"):
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        prev = toks[i - 1] if i > 0 else None
        if nxt in (("punct", "("), ("punct", ".")):
            continue
        if kind == "word":
            if text.lower() in _SQL_WORDS:
                continue
            if nxt and nxt[0] == "str":
                continue
            if prev and prev[0] == "word" and prev[1].lower() == "as":
                continue
        if text not in out:
            out.append(text)
    return out


def definitions():
    """Every metric and dimension of the active semantic document."""
    out = []
    for model in semantic.all_models():
        for kind, group in (("metric", model["metrics"]), ("dimension", model["dimensions"])):
            for d in group.values():
                out.append({
                    "kind": kind, "source": model["source"], "table": model["table"],
                    "name": d["name"], "agg": d.get("agg"), "expr": d["expr"],
                    "filter": d.get("filter"), "model_filter": model.get("filter"),
                    "grain": d.get("grain"), "description": d.get("description") or "",
                    "synonyms": d.get("synonyms") or [], "owner": d.get("owner"),
                    "certified": bool(d.get("certified")), "declared_version": d.get("version"),
                })
    return out


def _signature(d):
    return hashlib.sha256(json.dumps(
        [d.get("agg"), d["expr"], d.get("filter"), d.get("model_filter"), d.get("grain")]
    ).encode()).hexdigest()[:16]


def _table_columns(source, table):
    """(object name, [columns]) for `table` in the graph's record — matched as
    the catalog names it, else by its last part, case-insensitively; None if
    the source has a record but not this table; "undiscovered" if no record."""
    with db.connect() as c:
        rows = c.execute("SELECT id, name FROM meta_objects WHERE source=? "
                         "AND removed_at IS NULL", (source,)).fetchall()
        if not rows:
            return "undiscovered"
        want = str(table).lower()
        hit = next((r for r in rows if r["name"].lower() == want), None)
        if hit is None:
            bare = [r for r in rows if r["name"].split(".")[-1].lower() == want.split(".")[-1]]
            hit = bare[0] if len(bare) == 1 else None
        if hit is None:
            return None
        cols = [(r["name"], r["type"]) for r in c.execute(
            "SELECT name, type FROM meta_columns WHERE object_id=? ORDER BY ordinal",
            (hit["id"],)).fetchall()]
    return hit["name"], cols


def check(d):
    """{status, reasons, object, columns, missing} for one definition:
    valid, broken (and why), or unchecked (its source is not discovered)."""
    found = _table_columns(d["source"], d["table"])
    if found == "undiscovered":
        return {"status": "unchecked", "reasons": ["its source has not been discovered yet"],
                "object": None, "columns": [], "missing": []}
    if found is None:
        return {"status": "broken", "reasons": [f"table {d['table']} not found in {d['source']}"],
                "object": None, "columns": [], "missing": []}
    obj, cols = found
    by_lower = {name.lower(): name for name, _t in cols}
    types = {name.lower(): ctype for name, ctype in cols}
    refs = []
    for part in (d["expr"], d.get("filter"), d.get("model_filter")):
        for r in references(part or ""):
            if r not in refs:
                refs.append(r)
    used = [by_lower[r.lower()] for r in refs if r.lower() in by_lower]
    missing = [r for r in refs if r.lower() not in by_lower]
    if missing:
        return {"status": "broken", "object": obj, "columns": used, "missing": missing,
                "reasons": [f"column {m} not found in {obj}" for m in missing],
                "baseline_at": None}
    baseline = _baseline(d, obj)
    reasons, notes = [], []
    if baseline:
        before = {k.lower(): v for k, v in baseline["columns"].items()}
        for col in used:
            was, now = before.get(col.lower()), types.get(col.lower())
            if was is None or was == now:
                continue
            if _family(was) != _family(now):
                reasons.append(f"column {col} changed type from {was} to {now} since this "
                               f"definition was validated")
            else:
                notes.append(f"column {col} type detail changed ({was} → {now})")
    return {"status": "drifted" if reasons else "valid", "reasons": reasons + notes,
            "object": obj, "columns": used, "missing": [],
            "baseline_at": baseline["recorded_at"] if baseline else None}


def _baseline(d, obj):
    with db.connect() as c:
        row = c.execute("SELECT columns, recorded_at FROM meta_metric_baselines WHERE "
                        "source=? AND tbl=? AND kind=? AND name=? AND signature=?",
                        (d["source"], obj.lower(), d["kind"], d["name"].lower(),
                         _signature(d))).fetchone()
    return {"columns": json.loads(row["columns"]), "recorded_at": row["recorded_at"]} \
        if row else None


def _record_baseline(c, d, obj, columns, user_id=None):
    c.execute("DELETE FROM meta_metric_baselines WHERE source=? AND tbl=? AND kind=? AND "
              "name=?", (d["source"], obj.lower(), d["kind"], d["name"].lower()))
    c.execute("INSERT INTO meta_metric_baselines (source, tbl, kind, name, signature, columns, "
              "recorded_at, recorded_by) VALUES (?,?,?,?,?,?,?,?)",
              (d["source"], obj.lower(), d["kind"], d["name"].lower(), _signature(d),
               json.dumps(columns), time.time(), user_id))


def _current_types(source, obj, used):
    found = _table_columns(source, obj)
    if not isinstance(found, tuple):
        return None
    types = {name.lower(): ctype for name, ctype in found[1]}
    return {col: types.get(col.lower()) for col in used}


def ensure_baselines(source=None):
    """Record a baseline for every valid definition that has none for its
    current version — after a discovery, after a YAML apply, and on the
    scheduler's tick. Never overwrites an existing baseline: that is what
    lets a later type change show as drift."""
    made = 0
    for d in definitions():
        if source is not None and d["source"] != source:
            continue
        result = check(d)
        if result["status"] != "valid" or result["baseline_at"] is not None:
            continue
        types = _current_types(d["source"], result["object"], result["columns"])
        if types is None:
            continue
        with db.connect() as c:
            _record_baseline(c, d, result["object"], types)
            c.commit()
        made += 1
    return made


def revalidate(d, user):
    """Accept the columns a definition reads as they are now: its baseline is
    replaced. Refused while a column is missing — that needs a new definition."""
    result = check(d)
    if result["status"] == "broken":
        raise ValueError("; ".join(result["reasons"]))
    types = _current_types(d["source"], result["object"], result["columns"])
    with db.connect() as c:
        _record_baseline(c, d, result["object"], types, user["id"])
        c.commit()
    return check(d)


def history(source, table, name, kind="metric", limit=50):
    """Each distinct definition this metric has had, oldest first, from the
    applied YAML documents: [{version, applied_at, applied_by, agg, expr,
    filter, declared_version}] and a final {"removed": True} entry if it was
    taken out."""
    with db.connect() as c:
        rows = c.execute("SELECT yaml, applied_by, applied_at FROM semantic_models "
                         "ORDER BY applied_at DESC LIMIT ?", (limit,)).fetchall()
    versions, last = [], None
    for row in reversed(rows):
        ok, _errors, doc = semantic.validate(row["yaml"])
        if not ok:
            continue
        found = None
        for model in doc["models"]:
            if model["source"] == source and model["table"].lower() == str(table).lower():
                group = model["metrics"] if kind == "metric" else model["dimensions"]
                d = group.get(str(name).lower())
                if d:
                    found = {"agg": d.get("agg"), "expr": d["expr"], "filter": d.get("filter"),
                             "model_filter": model.get("filter"), "grain": d.get("grain"),
                             "declared_version": d.get("version")}
        if found is None:
            if last is not None and not (versions and versions[-1].get("removed")):
                versions.append({"removed": True, "applied_at": row["applied_at"],
                                 "applied_by": row["applied_by"]})
                last = None
            continue
        sig = _signature({**found, "expr": found["expr"]})
        if sig != last:
            versions.append({"version": sum(1 for v in versions if not v.get("removed")) + 1,
                             "applied_at": row["applied_at"], "applied_by": row["applied_by"],
                             **found})
            last = sig
    return versions


def describe(d):
    """One line an agent reads: the certified way to compute a measure."""
    if d["kind"] == "metric":
        body = "COUNT(*)" if d["expr"].strip() == "*" else f"{(d['agg'] or 'sum').upper()}({d['expr']})"
    else:
        body = d["expr"] + (f" by {d['grain']}" if d.get("grain") else "")
    where = " AND ".join(x for x in (d.get("model_filter"), d.get("filter")) if x)
    cert = "certified" if d.get("certified") else "defined"
    return (f"{d['kind']} {d['name']} on {d['table']} ({cert}): {body}"
            + (f" WHERE {where}" if where else ""))


def for_source(source, visible=None):
    """Definitions on `source` (only on tables in `visible` when given), each
    with its check."""
    out = []
    for d in definitions():
        if d["source"] != source:
            continue
        result = check(d)
        table = result["object"] or d["table"]
        if visible is not None and table not in visible:
            continue
        out.append({**d, **result})
    return out
