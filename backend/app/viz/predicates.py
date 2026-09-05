"""Predicates — the one filter implementation.

`filters`, `having`, dashboard slicers and cross-filter selections all resolve
to the same {col, op, value|values|lo+hi} shape and all go through match_row,
so a filter means exactly one thing wherever it is applied (§3.3).

Semantics worth knowing: string ops are case-insensitive; a null cell matches
ONLY ne, nin and isnull; ordering ops compare numerically when both sides
coerce, lexicographically only when neither does, and exclude the row when
just one side is numeric. `last_n` is relative to the column's own maximum,
never to now(), so a stale warehouse extract still shows its own last N days.

Unknown-column / unknown-op predicates are dropped by the callers before
matching (apply_filters, _st_filters, _valid_predicates) — match_row itself
answers False for them so it can never raise.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .frame import _cell, _cols, _index, _is_null, _parse_date, _rows, _to_num, coerce_compare
from .vocab import OPS

def _shift_back(d: datetime, n: int, unit: str) -> datetime:
    if unit == "day":
        return d - timedelta(days=n)
    if unit == "week":
        return d - timedelta(weeks=n)
    if unit in ("month", "quarter"):
        months = n * (3 if unit == "quarter" else 1)
        y, m = d.year, d.month - months
        while m <= 0:
            m += 12
            y -= 1
        day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28,
                          31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
        return d.replace(year=y, month=m, day=day)
    if unit == "year":
        try:
            return d.replace(year=d.year - n)
        except ValueError:                       # Feb 29
            return d.replace(year=d.year - n, day=28)
    return d - timedelta(days=n)


def _resolve_last_n(columns: list, rows: list, pred: dict) -> dict:
    """last_n is relative to the column's max value, not now() — a warehouse
    extract that ends last week must still show its own last 30 days."""
    out = dict(pred)
    i = _index(columns, pred.get("col"))
    n = _to_num(pred.get("value"))
    if i < 0 or n is None or n <= 0:
        out["_cutoff"] = None
        return out
    unit = pred.get("unit") or "day"
    vals = [_cell(r, i) for r in rows]
    dates = [d for d in (_parse_date(v) for v in vals) if d is not None]
    if dates:
        out["_cutoff"] = _shift_back(max(dates), int(n), unit).strftime("%Y-%m-%d %H:%M:%S")
        out["_cutoff_date"] = True
        return out
    nums = [x for x in (_to_num(v) for v in vals) if x is not None]
    if nums:
        out["_cutoff"] = max(nums) - n
        out["_cutoff_date"] = False
        return out
    out["_cutoff"] = None
    return out


def match_row(row: list, columns: list, predicate: dict) -> bool:
    """§3.3 semantics, exactly. Unknown op/col ⇒ False (callers drop those first).
    A null cell matches ONLY ne, nin and isnull."""
    if not isinstance(predicate, dict):
        return False
    i = _index(columns, predicate.get("col"))
    op = predicate.get("op") or "eq"
    if i < 0 or op not in OPS:
        return False
    v = _cell(row, i)

    if op == "isnull":
        return _is_null(v)
    if op == "notnull":
        return not _is_null(v)
    if _is_null(v):
        return op in ("ne", "nin")

    if op in ("in", "nin"):
        vals = predicate.get("values")
        if not isinstance(vals, (list, tuple)):
            vals = [] if predicate.get("value") is None else [predicate.get("value")]
        hit = str(v).lower() in {str(x).lower() for x in vals}
        return hit if op == "in" else not hit

    if op == "between":
        lo, hi = predicate.get("lo"), predicate.get("hi")
        c1, c2 = coerce_compare(v, lo), coerce_compare(v, hi)
        if c1 is None or c2 is None:
            return False
        return c1 >= 0 and c2 <= 0

    if op == "last_n":
        cutoff = predicate.get("_cutoff")
        if cutoff is None:
            return True                       # unresolved: apply_filters fills _cutoff
        if predicate.get("_cutoff_date"):
            d = _parse_date(v)
            if d is None:
                return False
            return d.strftime("%Y-%m-%d %H:%M:%S") >= str(cutoff)
        c = coerce_compare(v, cutoff)
        return c is not None and c >= 0

    val = predicate.get("value")
    if op in ("gt", "gte", "lt", "lte"):
        c = coerce_compare(v, val)
        if c is None:
            return False
        return {"gt": c > 0, "gte": c >= 0, "lt": c < 0, "lte": c <= 0}[op]

    sv, sval = str(v).lower(), str(val).lower()
    if op == "eq":
        return sv == sval
    if op == "ne":
        return sv != sval
    if op == "contains":
        return sval in sv
    if op == "ncontains":
        return sval not in sv
    if op == "startswith":
        return sv.startswith(sval)
    if op == "endswith":
        return sv.endswith(sval)
    return False


def apply_filters(columns: list, rows: list, filters: Optional[list]) -> tuple:
    """Total. Unknown-col / unknown-op predicates are dropped, never applied."""
    cols = _cols(columns)
    out = _rows(rows)
    if not isinstance(filters, (list, tuple)) or not filters:
        return cols, out
    for f in filters:
        if not isinstance(f, dict):
            continue
        if _index(cols, f.get("col")) < 0 or (f.get("op") or "eq") not in OPS:
            continue
        p = _resolve_last_n(cols, out, f) if f.get("op") == "last_n" else f
        out = [r for r in out if match_row(r, cols, p)]
    return cols, out


def _valid_predicates(columns: list, preds: Any, warn: list, where: str) -> list:
    keep = []
    for p in (preds or []) if isinstance(preds, (list, tuple)) else []:
        if not isinstance(p, dict):
            warn.append(f"{where}: a predicate is not an object — dropped")
            continue
        op = p.get("op") or "eq"
        if op not in OPS:
            warn.append(f"{where}: unknown op '{op}' — predicate dropped")
            continue
        if _index(columns, p.get("col")) < 0:
            warn.append(f"{where}: column '{p.get('col')}' is not available — predicate dropped")
            continue
        keep.append(p)
    return keep
