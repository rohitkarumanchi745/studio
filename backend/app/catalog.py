"""Catalog: what sources/tables/columns the current user may see (RBAC-filtered)."""
import re
import time

from fastapi import APIRouter, Depends, HTTPException

from . import rbac
from .auth import current_user
from .connectors import all_sources, get_connector

router = APIRouter(prefix="/catalog", tags=["catalog"])

_STOP = {"the", "and", "for", "with", "show", "what", "which", "how", "many",
         "much", "per", "over", "last", "this", "that", "from", "table",
         "tables", "data", "chart", "level", "give", "get", "top", "all"}


def _tokens(text):
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) >= 3 and w not in _STOP}


def match_tables(prompt, table_schemas):
    """Rank tables by relevance to a business request: overlap between the
    prompt's terms and each table's name and column names (name hits weigh
    double). Deterministic — works with or without an LLM."""
    words = _tokens(prompt)
    out = []
    for table, cols in table_schemas.items():
        name_terms = _tokens(table.replace("_", " "))
        col_terms = {}
        for c in cols:
            for t in _tokens(str(c.get("name", "")).replace("_", " ")):
                col_terms.setdefault(t, str(c["name"]))
        name_hits = words & name_terms
        col_hits = words & set(col_terms)
        score = 2 * len(name_hits) + len(col_hits)
        if score > 0:
            out.append({
                "table": table,
                "score": score,
                "why": sorted(name_hits | {col_terms[h] for h in col_hits})[:6],
                "columns": [str(c.get("name", "")) for c in cols][:12],
            })
    out.sort(key=lambda m: (-m["score"], m["table"]))
    return out[:5]


@router.get("/sources")
def sources(user=Depends(current_user)):
    allowed = rbac.allowed_sources(user["role"])
    return [
        {**s, "allowed": s["name"] in allowed}
        for s in all_sources()
    ]


@router.get("/sources/{source}/tables")
def tables(source: str, user=Depends(current_user)):
    if source not in rbac.allowed_sources(user["role"]):
        raise HTTPException(403, "Your role has no access to this source")
    conn = _connector_or_400(source)
    try:
        names = conn.list_tables()
    except Exception as e:
        raise HTTPException(502, f"Could not list tables on {source}: {e}")
    return rbac.allowed_tables(user["role"], source, names)


@router.get("/sources/{source}/tables/{table}/schema")
def schema(source: str, table: str, user=Depends(current_user)):
    if not rbac.can_access(user["role"], source, table):
        raise HTTPException(403, "Your role has no access to this table")
    conn = _connector_or_400(source)
    try:
        return conn.get_schema(table)
    except Exception as e:
        raise HTTPException(502, f"Could not read schema: {e}")


@router.get("/sources/{source}/tables/{table}/sample")
def sample(source: str, table: str, user=Depends(current_user)):
    """Live preview of a matched table: a fresh sample of rows plus an
    auto-aggregated bar view. Queries run on every call — always the latest
    data, never a cache."""
    if not rbac.can_access(user["role"], source, table):
        raise HTTPException(403, "Your role has no access to this table")
    conn = _connector_or_400(source)
    try:
        cols = conn.get_schema(table)
        sample_sql = f"SELECT * FROM {table} LIMIT 50"
        s_columns, s_rows = conn.run_query(sample_sql)
    except Exception as e:
        raise HTTPException(502, f"Could not sample {table}: {e}")

    panels = []
    # Auto bar: first text-ish column vs first numeric column, aggregated in SQL.
    first = s_rows[0] if s_rows else []
    cat = num = None
    for i, c in enumerate(s_columns):
        v = first[i] if i < len(first) else None
        if c.lower() == "id" or c.lower().endswith("_id"):
            continue  # ids are numeric but summing them means nothing
        if num is None and isinstance(v, (int, float)) and not isinstance(v, bool):
            num = c
        elif cat is None and not isinstance(v, (int, float)):
            cat = c
    if cat and num:
        bar_sql = (f"SELECT {cat}, SUM({num}) AS total_{num} FROM {table} "
                   f"GROUP BY {cat} ORDER BY 2 DESC LIMIT 12")
        try:
            b_columns, b_rows = conn.run_query(bar_sql)
            panels.append({
                "sql": bar_sql, "columns": b_columns, "rows": b_rows,
                "chart": {"type": "bar", "title": f"{num} by {cat} — {table}",
                          "x": cat, "y": [f"total_{num}"]},
            })
        except Exception:
            pass  # no bar view — the sample table still renders

    panels.append({
        "sql": sample_sql, "columns": s_columns, "rows": s_rows,
        "chart": {"type": "table", "title": f"{table} — latest sample",
                  "x": s_columns[0] if s_columns else None, "y": []},
    })
    return {"table": table, "panels": panels, "fetched_at": time.time()}


def _connector_or_400(source):
    try:
        conn = get_connector(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")
    if not conn.configured():
        raise HTTPException(
            400,
            f"Source '{source}' is not configured. Set its credentials in the "
            f"backend environment (see .env.example) and install its connector package.",
        )
    return conn
