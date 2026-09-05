"""Catalog: what sources/tables/columns the current user may see (RBAC-filtered).

Two kinds of endpoint live here. Metadata (tables, schema, skill) reads the
connector's catalog directly, with governance's denied columns stripped so the
UI and the suggestion LLM never learn a column exists that no result may
carry. Anything that returns ROWS (sample, the per-table sample behind
suggestions) goes through gateway.execute like every other data path: the
table name here comes straight from the URL, so before the gateway a role
with a "*" policy could read any object the warehouse exposed, and the rows
skipped deny/mask before being shown — and sent to an external model.
"""
import re
import time

from fastapi import APIRouter, Depends, HTTPException

from . import gateway, governance, rbac, skills, suggest
from .auth import current_user
from .connectors import all_sources
# match_tables (prompt → ranked tables) lives in matching.py, a leaf module, so
# chat / pipelines can rank without importing this router. Re-exported for one
# release so `from .catalog import match_tables` keeps working.
from .matching import match_tables  # noqa: F401
from .queryguard import QueryRejected
from .sources import connector_or_400

router = APIRouter(prefix="/catalog", tags=["catalog"])

# A table path segment must LOOK like a table name (optionally qualified)
# before it is spliced into SQL. The gateway's guard is the real boundary —
# it resolves every FROM target against the role's allowlist — but a segment
# such as `sales UNION SELECT ...` would parse as a legal reference to `sales`
# followed by engine-dependent garbage, and surface as a 502 from the
# warehouse instead of a clear 4xx here.
_TABLE_NAME = re.compile(r"^[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*$")


def _table_name_or_400(table):
    if not _TABLE_NAME.match(table or ""):
        raise HTTPException(400, "Invalid table name")
    return table


def _governed_schema(source, table, cols):
    """Schema metadata minus governance's denied columns. Masked columns stay:
    their values are masked at result time, and the column is not a secret."""
    deny = governance.column_rules(source, table)["deny"]
    if not deny:
        return cols
    return [c for c in cols if str(c.get("name", "")).lower() not in deny]


def _read_rows(user, source, sql, purpose, table, max_rows):
    """One governed read for the catalog, with this router's error mapping:
    a guard/RBAC rejection is the caller's 403, an unknown or unconfigured
    source keeps its own status, and anything the warehouse raised is a 502."""
    try:
        return gateway.execute(user, source, sql, purpose,
                               table_label=table, max_rows=max_rows)
    except QueryRejected as e:
        raise HTTPException(403, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not sample {table}: {e}")


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


@router.get("/sources/{source}/skill")
def source_skill(source: str, user=Depends(current_user)):
    """The skill file briefing this source's agent for the current user's
    role — regenerated automatically when schema or access changes."""
    if source not in rbac.allowed_sources(user["role"]):
        raise HTTPException(403, "Your role has no access to this source")
    conn = _connector_or_400(source)
    try:
        names = conn.list_tables()
    except Exception as e:
        raise HTTPException(502, f"Could not list tables on {source}: {e}")
    allowed = rbac.allowed_tables(user["role"], source, names)
    schemas = {}
    for t in allowed[:10]:
        try:
            schemas[t] = _governed_schema(source, t, conn.get_schema(t))
        except Exception:
            schemas[t] = []
    return {"source": source, "role": user["role"],
            "skill": skills.get_skill(conn, user["role"], allowed, schemas)}


@router.get("/sources/{source}/suggestions")
def source_suggestions(source: str, table: str = "*", user=Depends(current_user)):
    """Starter questions for the current selection, grounded in its schema.

    table="*" spreads the suggestions across the tables this role can see, so
    whole-source mode still offers something concrete to click.
    """
    if not rbac.can_access(user["role"], source, table):
        raise HTTPException(403, "Your role has no access to this table")
    try:
        conn, allowed = gateway.scope(user, source, table_label=table)
    except QueryRejected as e:
        raise HTTPException(403, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not list tables on {source}: {e}")

    targets = [t for t in _selected_tables(table) if t in allowed] or allowed[:3]
    out = []
    for t in targets[:3]:
        # The schema and the sample rows must agree column-for-column: the
        # suggester reads sample values by position. Both drop the same denied
        # columns (by name), so what the LLM sees is exactly what a result
        # for this role would carry.
        try:
            cols = _governed_schema(source, t, conn.get_schema(t))
            rows = gateway.execute(user, source, f"SELECT * FROM {t} LIMIT 3",
                                   "catalog_suggest", table_label=t, max_rows=3).rows
        except Exception:
            continue
        for q in suggest.suggestions_for(conn, t, cols, rows):
            out.append({"question": q, "table": t})
    # Round-robin across tables so one wide table cannot crowd the list out.
    by_table = {}
    for item in out:
        by_table.setdefault(item["table"], []).append(item)
    mixed, i = [], 0
    while len(mixed) < suggest.MAX_SUGGESTIONS and any(v[i:] for v in by_table.values()):
        for v in by_table.values():
            if i < len(v) and len(mixed) < suggest.MAX_SUGGESTIONS:
                mixed.append(v[i])
        i += 1
    return {"source": source, "table": table, "suggestions": mixed}


def _selected_tables(label):
    """'sales' -> [sales]; 'sales, web_traffic' -> both; '*' -> []."""
    if not label or label in ("*", "all tables", "all sources"):
        return []
    return [t.strip() for t in str(label).split(",") if t.strip()]


@router.get("/sources/{source}/tables/{table}/schema")
def schema(source: str, table: str, user=Depends(current_user)):
    if not rbac.can_access(user["role"], source, table):
        raise HTTPException(403, "Your role has no access to this table")
    _table_name_or_400(table)
    conn = _connector_or_400(source)
    try:
        cols = conn.get_schema(table)
    except Exception as e:
        raise HTTPException(502, f"Could not read schema: {e}")
    return _governed_schema(source, table, cols)


@router.get("/sources/{source}/tables/{table}/sample")
def sample(source: str, table: str, user=Depends(current_user)):
    """Live preview of a matched table: a fresh sample of rows plus an
    auto-aggregated bar view. Queries run on every call — always the latest
    data, never a cache."""
    if not rbac.can_access(user["role"], source, table):
        raise HTTPException(403, "Your role has no access to this table")
    _table_name_or_400(table)
    sample = _read_rows(user, source, f"SELECT * FROM {table} LIMIT 50",
                        "catalog_sample", table, 50)
    sample_sql, s_columns, s_rows = sample.sql, sample.columns, sample.rows

    panels = []
    # Auto bar: first text-ish column vs first numeric column, aggregated in
    # SQL. Chosen from the GOVERNED result, so a denied column can never be the
    # dimension; masked columns are skipped too — a "***" cell reads as text,
    # and grouping or summing over it is a chart of nothing.
    masked = governance.column_rules(source, table)["mask"]
    first = s_rows[0] if s_rows else []
    cat = num = None
    for i, c in enumerate(s_columns):
        v = first[i] if i < len(first) else None
        if c.lower() == "id" or c.lower().endswith("_id"):
            continue  # ids are numeric but summing them means nothing
        if c.lower() in masked:
            continue
        if num is None and isinstance(v, (int, float)) and not isinstance(v, bool):
            num = c
        elif cat is None and not isinstance(v, (int, float)):
            cat = c
    if cat and num:
        bar_sql = (f"SELECT {cat}, SUM({num}) AS total_{num} FROM {table} "
                   f"GROUP BY {cat} ORDER BY 2 DESC LIMIT 12")
        try:
            bar = gateway.execute(user, source, bar_sql, "catalog_sample",
                                  table_label=table, max_rows=12)
            panels.append({
                "sql": bar.sql, "columns": bar.columns, "rows": bar.rows,
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


# Moved to sources.connector_or_400 so the gateway can resolve a connector
# without importing this router; kept as an alias for existing importers.
_connector_or_400 = connector_or_400
