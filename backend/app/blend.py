"""Cross-source blend — combine several sources into ONE table.

Chat is scoped to one source, and "all sources" fans out to per-source agents
that return SEPARATE panels. Neither gives you a single table joining, say,
Databricks inventory to demo sales — so neither can be charted as one thing.

You cannot write one SQL across Databricks + SQLite + BigQuery, so this
federates instead: each PART is fetched through Studio's normal gate
(queries.verify_sql = RBAC + query guard + real execution + governance
masking), the already-filtered rows are materialized into a throwaway in-memory
DuckDB, and ONE combine SQL runs over just those parts.

Two properties make that safe:
- A part can only ever contain data the requesting role could already query
  itself — the gate runs per part, before anything reaches DuckDB.
- The combine SQL is validated by the same hardened queryguard with an
  allowlist of ONLY the part names, and the DuckDB connection is locked down
  (no filesystem, no network) once the parts are loaded. So the combine step
  cannot reach a warehouse table, a file, or a bucket.

Cross-engine types line up for free: every part's rows go through
connectors.base.jsonify_rows first, which already normalizes dates to ISO
strings and Decimals to numbers. A date from Databricks and a date from SQLite
arrive as the same string, so joining on it works.
"""
import os
import re

from fastapi import HTTPException

from . import queries, queryguard, util
from .connectors.base import jsonify_rows

# One blend is one in-memory table: cap it so a careless cartesian join cannot
# take the container down with it.
MAX_ROWS = int(os.getenv("STUDIO_BLEND_MAX_ROWS", "200000"))
MEMORY_LIMIT = os.getenv("STUDIO_BLEND_MEMORY", "1GB")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_name(name, taken):
    """Part names become SQL identifiers in generated DDL, so they are checked
    against a strict pattern rather than escaped — nothing exotic is worth the
    injection surface."""
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise HTTPException(400, f"Invalid part name '{name}': use letters, digits and _ (starting with a letter)")
    if name.lower() in taken:
        raise HTTPException(400, f"Duplicate part name '{name}'")
    if name.lower() in queryguard.FORBIDDEN.pattern:  # cheap sanity, not security
        pass
    return name


def _duck_type(values):
    """A DuckDB column type from the Python values actually present. Everything
    arrives JSON-normalized (jsonify_rows), so the interesting cases are only
    int / float / bool / str; unknown or mixed falls back to VARCHAR, which
    still compares and groups correctly."""
    seen = set()
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            seen.add("BOOLEAN")
        elif isinstance(v, int):
            seen.add("BIGINT")
        elif isinstance(v, float):
            seen.add("DOUBLE")
        else:
            seen.add("VARCHAR")
    if not seen:
        return "VARCHAR"
    if seen == {"BIGINT"}:
        return "BIGINT"
    if seen <= {"BIGINT", "DOUBLE"}:
        return "DOUBLE"          # mixed ints and floats
    if seen == {"BOOLEAN"}:
        return "BOOLEAN"
    return "VARCHAR"


def _quote(ident):
    return '"' + str(ident).replace('"', '""') + '"'


def _fetch(user, part):
    """One part, through the normal gate, with all its rows."""
    name, source = part["name"], part.get("source")
    sql = (part.get("sql") or "").strip()
    table = part.get("table")
    if not sql:
        if not table:
            raise HTTPException(400, f"Part '{name}' needs a table or sql")
        sql = f"SELECT * FROM {table}"
    res = queries.verify_sql(user, source, table, sql, full_rows=True)
    if not res.get("ok"):
        # Abort the whole blend: a partial blend is a wrong answer, not a
        # degraded one, and silently dropping a part would misstate the data.
        raise HTTPException(400, f"Part '{name}' ({source}) failed: {res.get('error')}")
    return {
        "name": name, "source": source, "table": table,
        "sql": res["sql"], "columns": res["columns"],
        "rows": jsonify_rows(res["rows"]), "row_count": res["row_count"],
    }


def default_combine(parts):
    """The combine SQL to use when the caller gives none.

    - identical column sets  -> UNION ALL (stacking the same shape)
    - exactly one column common to every part -> INNER JOIN on it
    - anything else -> None, and the caller must ask for explicit SQL, because
      guessing a join key is how you silently produce a wrong number.
    """
    names = [p["name"] for p in parts]
    colsets = [[c for c in p["columns"]] for p in parts]
    if all(set(c) == set(colsets[0]) for c in colsets):
        cols = ", ".join(_quote(c) for c in colsets[0])
        return " UNION ALL ".join(f"SELECT {cols} FROM {_quote(n)}" for n in names)

    common = set(colsets[0])
    for c in colsets[1:]:
        common &= set(c)
    if len(common) != 1:
        return None
    key = next(iter(common))
    sel, frm = [], _quote(names[0])
    for p in parts:
        for c in p["columns"]:
            # Disambiguate collisions: the key once, other repeats prefixed.
            alias = c if (c == key and p is parts[0]) else (c if c != key else None)
            if alias is None:
                continue
            out = c if sum(c in cs for cs in colsets) == 1 else f"{p['name']}_{c}"
            sel.append(f"{_quote(p['name'])}.{_quote(c)} AS {_quote(out)}")
    for p in parts[1:]:
        frm += (f" INNER JOIN {_quote(p['name'])} ON "
                f"{_quote(parts[0]['name'])}.{_quote(key)} = {_quote(p['name'])}.{_quote(key)}")
    return f"SELECT {', '.join(sel)} FROM {frm}"


def _materialize(con, part):
    """Create one part as a typed DuckDB table and insert its rows."""
    cols = part["columns"]
    rows = part["rows"]
    types = [_duck_type([r[i] if i < len(r) else None for r in rows]) for i in range(len(cols))]
    ddl = ", ".join(f"{_quote(c)} {t}" for c, t in zip(cols, types))
    con.execute(f"CREATE TABLE {_quote(part['name'])} ({ddl})")
    if rows:
        placeholders = ", ".join("?" * len(cols))
        con.executemany(
            f"INSERT INTO {_quote(part['name'])} VALUES ({placeholders})",
            [list(r[:len(cols)]) + [None] * (len(cols) - len(r)) for r in rows])


def _harden(con):
    """Once the parts are loaded the connection needs nothing external, so shut
    that door: a combine SQL must not be able to read a file or a URL even if
    the guard were bypassed. Best-effort — older DuckDB lacks some settings."""
    for stmt in (f"SET memory_limit='{MEMORY_LIMIT}'",
                 "SET enable_external_access=false",
                 "SET lock_configuration=true"):
        try:
            con.execute(stmt)
        except Exception:
            pass


def lineage(parts, combined_label="blended table"):
    """source -> table -> part -> blend, in the graph shape Lineage.jsx renders
    (pipelines.lineage builds the same structure for pipeline steps)."""
    sources, tables, steps, edges = {}, {}, [], []
    for i, p in enumerate(parts):
        src = p.get("source") or "?"
        sources.setdefault(src, {"id": f"s:{src}", "label": src})
        tname = (p.get("table") or p["name"]).lower()
        tid = f"t:{src}.{tname}"
        if tid not in tables:
            tables[tid] = {"id": tid, "label": tname, "source": src}
            edges.append({"from": f"s:{src}", "to": tid})
        sid = f"step:{i}"
        steps.append({"id": sid, "label": p["name"], "index": i,
                      "tables": [tid], "failed": False})
        edges.append({"from": tid, "to": sid})
    # The blend itself is the final step every part feeds.
    bid = f"step:{len(parts)}"
    steps.append({"id": bid, "label": combined_label, "index": len(parts),
                  "tables": [], "failed": False})
    for i in range(len(parts)):
        edges.append({"from": f"step:{i}", "to": bid})
    return {"sources": list(sources.values()), "tables": list(tables.values()),
            "steps": steps, "edges": edges, "multi_source": len(sources) > 1}


def blend(user, parts, combine_sql=None):
    """Fetch every part through the gate, combine them in DuckDB, return one
    table. Raises HTTPException with an actionable message on any failure."""
    import duckdb

    if not parts or len(parts) < 2:
        raise HTTPException(400, "A blend needs at least two parts")

    taken, prepared = set(), []
    for i, p in enumerate(parts):
        name = _safe_name(p.get("name") or f"{p.get('source', 'part')}_{p.get('table', i)}", taken)
        taken.add(name.lower())
        prepared.append({**p, "name": name})

    # Independent per part → fetch concurrently; the gate runs inside each.
    fetched = util.pmap(lambda p: _fetch(user, p), prepared, workers=6)
    if any(f is None for f in fetched):
        # pmap swallows per-item errors; re-run the failures serially so the
        # user gets the real reason instead of "something went wrong".
        fetched = [f if f is not None else _fetch(user, p) for f, p in zip(fetched, prepared)]

    total = sum(f["row_count"] for f in fetched)
    if total > MAX_ROWS:
        raise HTTPException(400, f"Blend is too large ({total} rows > {MAX_ROWS}); "
                                 "filter or aggregate the parts first")

    con = duckdb.connect(":memory:")
    try:
        for f in fetched:
            _materialize(con, f)
        _harden(con)

        sql = (combine_sql or "").strip() or default_combine(fetched)
        if not sql:
            shapes = "; ".join(f"{f['name']}({', '.join(f['columns'][:6])})" for f in fetched)
            raise HTTPException(
                400, "These parts have neither a shared shape to stack nor a single "
                     f"common column to join on, so the combine SQL must be explicit. Parts: {shapes}")

        # The allowlist is ONLY the parts: the combine step cannot reach a
        # warehouse table, a file or a bucket.
        cleaned = queryguard.validate(sql, [f["name"] for f in fetched])
        cleaned = queryguard.enforce_limit(cleaned, MAX_ROWS)
        res = con.execute(cleaned)
        columns = [d[0] for d in res.description]
        rows = jsonify_rows(res.fetchmany(MAX_ROWS))
    except queryguard.QueryRejected as e:
        raise HTTPException(400, f"Combine SQL rejected: {e}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Blend failed: {str(e)[:300]}")
    finally:
        try:
            con.close()
        except Exception:
            pass

    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "sql": cleaned,
        "parts": [{k: f[k] for k in ("name", "source", "table", "columns", "row_count")}
                  for f in fetched],
        "lineage": lineage(fetched),
    }
