"""Certified metrics in the metadata graph.

The semantic YAML (semantic.py) is the source of truth for what a metric
MEANS; the metadata graph knows what the warehouse actually HOLDS. This module
joins the two: every metric and dimension is linked to its source table and
to the columns its expression and filters reference, checked against the
graph's current record, and given a version history from the applied YAML
documents semantic.py already keeps.

  revenue (sum of quantity * unit_price * (1 - discount_rate))
    → table order_lines → columns quantity, unit_price, discount_rate
    → valid | broken: column discount_rate not found | unchecked (not discovered)

A broken definition is never answered from: the semantic layer declines it and
says why, and the source's metadata health lists it. A valid one is offered to
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

from . import db, queryguard, semantic

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
        cols = [r["name"] for r in c.execute(
            "SELECT name FROM meta_columns WHERE object_id=? ORDER BY ordinal",
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
    by_lower = {c.lower(): c for c in cols}
    refs = []
    for part in (d["expr"], d.get("filter"), d.get("model_filter")):
        for r in references(part or ""):
            if r not in refs:
                refs.append(r)
    used = [by_lower[r.lower()] for r in refs if r.lower() in by_lower]
    missing = [r for r in refs if r.lower() not in by_lower]
    reasons = [f"column {m} not found in {obj}" for m in missing]
    return {"status": "broken" if missing else "valid", "reasons": reasons,
            "object": obj, "columns": used, "missing": missing}


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
