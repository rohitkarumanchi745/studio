"""Data freshness — "as of when" is a table's data current.

Studio queries live data on every question, but it doesn't own the warehouse's
ingestion schedule (dbt / Fivetran / Airflow, etc.). What it CAN do is read the
freshness a table records itself: if a table has a load/update timestamp column
(loaded_at, last_updated, etl_ts, …) or, failing that, a business date column,
`for_table` returns the MAX of it — the newest data present — plus the row
count. Detection is a name + type heuristic; the MAX query goes through
gateway.execute like every other read, so freshness passes RBAC, the query
guard and governance and can't touch a table the role may not see. It is the
one caller that opts out of the audit row: a metadata probe fanned out over up
to 30 tables must not write 30 audit rows per page load.
"""
import re

from fastapi import APIRouter, Depends, HTTPException

from . import gateway, rbac, util
from .auth import current_user
from .queryguard import QueryRejected
from .sources import connector_or_400

router = APIRouter(tags=["freshness"])

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


def for_table(user, source, table, connector=None):
    """Freshness of one table: MAX of its freshness column + row count. The
    probe runs through gateway.execute, which derives the role's allowlist
    itself — callers never pass one. `connector` is an optional already-resolved
    connector so a fan-out over 30 tables resolves the source once, not 30
    times; it is only used for the schema read (metadata, not row-returning).
    Never raises — returns an `error`/`note` the caller surfaces."""
    # Fail fast on a table the role can't see: no schema read, no query, and
    # the same "no access" the old allowlist check gave. The gateway would
    # reject the query anyway; this just keeps column names of a denied table
    # out of the probe entirely.
    if not rbac.can_access((user or {}).get("role"), source, table):
        return {"table": table, "error": "no access"}
    try:
        connector = connector or connector_or_400(source)
        col, kind = detect_col(connector.get_schema(table))
    except Exception as e:
        return {"table": table, "error": f"schema error: {str(e)[:120]}"}
    if not col:
        return {"table": table, "column": None, "latest": None, "rows": None,
                "note": "no date/timestamp column"}
    sql = f"SELECT MAX({col}) AS latest, COUNT(*) AS n FROM {table}"
    try:
        # audit=False: a metadata probe, not a user read — see module docstring.
        # RBAC / guard / governance still apply; only the audit row is skipped.
        res = gateway.execute(user, source, sql, "freshness", table_label=table,
                              max_rows=1, audit=False)
    except Exception as e:
        return {"table": table, "column": col, "kind": kind, "error": str(e)[:160]}
    rows = res.rows
    latest = rows[0][0] if rows else None
    n = rows[0][1] if rows else None
    return {"table": table, "column": col, "kind": kind, "latest": latest, "rows": n}


def for_source(user, source, limit=30):
    """Freshness of every accessible table in a source, probed concurrently.
    gateway.scope resolves the connector and the role's table list once; each
    probe then re-derives its own access inside gateway.execute."""
    try:
        conn, allowed = gateway.scope(user, source)
    except QueryRejected as e:
        raise HTTPException(403, str(e))
    except HTTPException:
        raise                                   # unknown / unconfigured source
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")
    tabs = allowed[:limit]
    results = util.pmap(lambda t: for_table(user, source, t, connector=conn), tabs,
                        workers=6, default={"error": "failed"})
    return {"source": source, "tables": results}


@router.get("/freshness/{source}")
def freshness(source: str, user=Depends(current_user)):
    """How current each accessible table in `source` is."""
    return for_source(user, source)
