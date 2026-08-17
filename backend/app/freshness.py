"""Data freshness — "as of when" is a table's data current.

Studio queries live data on every question, but it doesn't own the warehouse's
ingestion schedule (dbt / Fivetran / Airflow, etc.). What it CAN do is read the
freshness a table records itself: if a table has a load/update timestamp column
(loaded_at, last_updated, etl_ts, …) or, failing that, a business date column,
`for_table` returns the MAX of it — the newest data present — plus the row
count. Detection is a name + type heuristic; the MAX query goes through the same
query guard + RBAC allowlist as everything else, so freshness can't touch a
table the role may not see.
"""
import os
import re

from fastapi import APIRouter, Depends, HTTPException

from . import queryguard, rbac, util
from .auth import current_user
from .catalog import _connector_or_400

router = APIRouter(tags=["freshness"])

MAX_ROWS = int(os.getenv("STUDIO_MAX_ROWS", "50000"))

# A time-typed column whose name looks like a load/ingest/update stamp is the
# best freshness signal; a plain date column is a fallback ("latest record").
_TIME_TYPE = re.compile(r"date|time|timestamp", re.I)
_LOAD_NAME = re.compile(
    r"load|ingest|etl|elt|dwh|dw_|refresh|snapshot|as_of|"
    r"updated|last_update|last_modified|modified|inserted|created_at|_at$|_ts$|_dt$",
    re.I)


# Some warehouses (and the SQLite demo) store dates as TEXT, so a name that
# reads like a date is a valid signal when no column is time-TYPED.
_DATE_NAME = re.compile(r"date|time|day|month|year|_at$|_ts$|_dt$", re.I)


def _is_time(col):
    return bool(_TIME_TYPE.search(col.get("type", "") or ""))


def detect_col(schema):
    """The best freshness column for a table, and whether it's a load stamp.
    Prefers a time-TYPED column; falls back to a date-NAMED one (TEXT dates are
    common). Returns (name, kind) where kind is 'load' | 'record', or
    (None, None)."""
    schema = schema or []
    times = [c["name"] for c in schema if _is_time(c)]
    if not times:   # no time-typed column — try names (TEXT dates)
        times = [c["name"] for c in schema if _DATE_NAME.search(c.get("name", "") or "")]
    if not times:
        return None, None
    load = [n for n in times if _LOAD_NAME.search(n or "")]
    if load:
        return load[0], "load"
    return times[0], "record"


def for_table(connector, allowed_tables, table):
    """Freshness of one table: MAX of its freshness column + row count. RBAC is
    the caller's `allowed_tables`; the query is guarded. Never raises — returns
    an `error`/`note` the caller surfaces."""
    if table not in allowed_tables:
        return {"table": table, "error": "no access"}
    try:
        col, kind = detect_col(connector.get_schema(table))
    except Exception as e:
        return {"table": table, "error": f"schema error: {str(e)[:120]}"}
    if not col:
        return {"table": table, "column": None, "latest": None, "rows": None,
                "note": "no date/timestamp column"}
    sql = f"SELECT MAX({col}) AS latest, COUNT(*) AS n FROM {table}"
    try:
        cleaned = queryguard.enforce_limit(queryguard.validate(sql, allowed_tables), MAX_ROWS)
        cols, rows = connector.run_query(cleaned)
    except Exception as e:
        return {"table": table, "column": col, "kind": kind, "error": str(e)[:160]}
    latest = rows[0][0] if rows else None
    n = rows[0][1] if rows else None
    return {"table": table, "column": col, "kind": kind, "latest": latest, "rows": n}


def for_source(user, source, limit=30):
    """Freshness of every accessible table in a source, probed concurrently."""
    conn = _connector_or_400(source)
    try:
        allowed = rbac.allowed_tables(user["role"], source, conn.list_tables())
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")
    if not allowed:
        raise HTTPException(403, "Your role has no access to this source")
    tabs = allowed[:limit]
    results = util.pmap(lambda t: for_table(conn, allowed, t), tabs, workers=6,
                        default={"error": "failed"})
    return {"source": source, "tables": results}


@router.get("/freshness/{source}")
def freshness(source: str, user=Depends(current_user)):
    """How current each accessible table in `source` is."""
    return for_source(user, source)
