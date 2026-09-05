"""Frame primitives: totality over (columns, rows) starts here.

Cell coercion (null / number / date), the sort keys that make weekdays and
months order semantically, hashable grouping keys, the aggregation table, the
one comparison primitive predicates and expressions share (coerce_compare),
and column typing (dtype_of / infer_fields / distinct_values / field_catalog).

Everything above this module assumes the invariants established here: a
ragged row reads as null, a bool is never a number, a leading null never
misclassifies a column. None of these functions raise on any input.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Optional

from .vocab import AGGS, CATEGORY_ORDERS, _MONTH_FULL, _WEEKDAY_FULL

_MEASURE_NAME_RE = re.compile(r"(?i)(^|_)(id|key|code|zip|year)$")
_TIME_NAME_RE = re.compile(r"(?i)date|time|day|month|year")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([ T]\d{1,2}:\d{2}(:\d{2})?)?")
_SLASH_DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})")
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


# ══════════════════════════════════════════════════════════════════════════
# Scalar helpers
# ══════════════════════════════════════════════════════════════════════════

def _is_null(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return False


def _to_num(v: Any) -> Optional[float]:
    """float or None. Bools are never numbers — a bool column is a dimension."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            f = float(s)
            return f if math.isfinite(f) else None
        except ValueError:
            pass
        if _THOUSANDS_RE.match(s):
            try:
                f = float(s.replace(",", ""))
                return f if math.isfinite(f) else None
            except ValueError:
                return None
    return None


def _cols(columns: Any) -> list:
    """Any junk → a usable column list. Totality starts at the boundary."""
    if not isinstance(columns, (list, tuple)):
        return []
    return [c if isinstance(c, str) else str(c) for c in columns]


def _rows(rows: Any) -> list:
    """Copy-and-normalize: every row becomes a list. Never raises."""
    if not isinstance(rows, (list, tuple)):
        return []
    return [list(r) if isinstance(r, (list, tuple)) else [r] for r in rows]


def _seq(rows: Any) -> list:
    """Read-only view for scan-only paths — no copy, so column scans stay O(n)."""
    return rows if isinstance(rows, (list, tuple)) else []


def _index(columns: Any, col: Any) -> int:
    """-1 when absent. Exact match first, then case-insensitive (LLM casing drift)."""
    if col is None or not isinstance(columns, (list, tuple)):
        return -1
    try:
        return columns.index(col)
    except ValueError:
        pass
    if not isinstance(col, str):
        return -1
    low = col.lower()
    for i, c in enumerate(columns):
        if isinstance(c, str) and c.lower() == low:
            return i
    return -1


def _cell(row: list, i: int) -> Any:
    """Ragged rows are data, not errors — a short row reads as null."""
    if i < 0 or not isinstance(row, (list, tuple)) or i >= len(row):
        return None
    return row[i]


def _parse_date(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    m = _ISO_DATE_RE.match(s)
    if m:
        try:
            y, mo = int(s[0:4]), int(s[5:7])
            d = int(s[8:10]) if len(s) >= 10 and s[7] == "-" else 1
            hh = mm = ss = 0
            tail = s[10:].strip().replace("T", " ").strip()
            if tail:
                parts = tail.split(":")
                hh = int(parts[0])
                mm = int(parts[1]) if len(parts) > 1 else 0
                ss = int(float(parts[2])) if len(parts) > 2 else 0
            return datetime(y, mo, d, hh % 24, mm % 60, ss % 60)
        except (ValueError, IndexError):
            return None
    m = _SLASH_DATE_RE.match(s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _US_DATE_RE.match(s)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _looks_date(v: Any) -> bool:
    return _parse_date(v) is not None


def _fmt_num(x: float) -> str:
    """Compact numeric label for bin ranges."""
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return f"{round(x, 4):g}"


def _sortable(v: Any, lut: Optional[dict] = None):
    """Numerics before strings; a known category order wins over both."""
    if lut is not None and v is not None:
        r = lut.get(str(v).strip().lower())
        if r is not None:
            return (0, float(r))
    n = _to_num(v)
    if n is not None:
        return (0, n)
    return (1, str(v))


def _category_lut(values) -> Optional[dict]:
    """{label_lower: rank} when every value belongs to one CATEGORY_ORDERS set."""
    seen = {str(v).strip().lower() for v in values if not _is_null(v)}
    if not seen:
        return None
    for name, order in CATEGORY_ORDERS.items():
        lut = {o.lower(): i for i, o in enumerate(order)}
        if name == "weekday":
            lut.update({o.lower(): i for i, o in enumerate(_WEEKDAY_FULL)})
        elif name == "month":
            lut.update({o.lower(): i for i, o in enumerate(_MONTH_FULL)})
        if seen <= set(lut):
            return lut
    return None


def _hkey(v: Any):
    """Hashable grouping key — unhashable cells collapse by their repr."""
    try:
        hash(v)
        return v
    except TypeError:
        return repr(v)


# ══════════════════════════════════════════════════════════════════════════
# Aggregation
# ══════════════════════════════════════════════════════════════════════════

def _percentile(nums: list, p: float) -> Optional[float]:
    if not nums:
        return None
    s = sorted(nums)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] * (hi - k) + s[hi] * (k - lo)


def _stddev(nums: list) -> Optional[float]:
    """Sample stddev (n-1), matching SQL's STDDEV default. None below 2 points."""
    if len(nums) < 2:
        return None
    mean = sum(nums) / len(nums)
    var = sum((x - mean) ** 2 for x in nums) / (len(nums) - 1)
    return math.sqrt(var)


def _aggregate(values: list, agg: str, n_rows: Optional[int] = None) -> Any:
    """Total over any cell content. Numeric aggs ignore non-numeric cells."""
    agg = agg if agg in AGGS else "sum"
    if agg == "count":
        return n_rows if n_rows is not None else len(values)
    if agg == "count_distinct":
        return len({_hkey(v) for v in values if not _is_null(v)})
    if agg == "first":
        return values[0] if values else None
    if agg == "last":
        return values[-1] if values else None
    if agg in ("min", "max"):
        vals = [v for v in values if not _is_null(v)]
        if not vals:
            return None
        nums = [v for v in vals if _to_num(v) is not None]
        pool = nums if nums else vals
        return (min if agg == "min" else max)(pool, key=lambda v: _sortable(v))
    nums = [n for n in (_to_num(v) for v in values) if n is not None]
    if not nums:
        return None
    if agg == "sum":
        return sum(nums)
    if agg == "avg":
        return sum(nums) / len(nums)
    if agg == "median":
        return _percentile(nums, 50)
    if agg == "p25":
        return _percentile(nums, 25)
    if agg == "p75":
        return _percentile(nums, 75)
    if agg == "p90":
        return _percentile(nums, 90)
    if agg == "stddev":
        return _stddev(nums)
    return sum(nums)



# ══════════════════════════════════════════════════════════════════════════
# Comparison — shared by predicates (match_row) and expressions (<, >, ...)
# ══════════════════════════════════════════════════════════════════════════

def coerce_compare(a: Any, b: Any) -> Optional[int]:
    """-1/0/1, or None when incomparable. Numeric-first; lexicographic fallback ONLY
    when NEITHER side coerces; single-side coercion ⇒ None ⇒ row excluded."""
    if _is_null(a) or _is_null(b):
        return None
    na, nb = _to_num(a), _to_num(b)
    if na is not None and nb is not None:
        return -1 if na < nb else (1 if na > nb else 0)
    if na is None and nb is None:
        sa, sb = str(a), str(b)
        return -1 if sa < sb else (1 if sa > sb else 0)
    return None


# ══════════════════════════════════════════════════════════════════════════
# Introspection
# ══════════════════════════════════════════════════════════════════════════

def dtype_of(rows: list, i: int, *, sample: int = 200) -> str:
    """'number' | 'string' | 'date' | 'bool' | 'empty', from non-null values only."""
    if i is None or i < 0:
        return "empty"
    vals, n = [], 0
    for r in _seq(rows):
        v = _cell(r, i)
        if _is_null(v) or (isinstance(v, str) and not v.strip()):
            continue
        vals.append(v)
        n += 1
        if n >= sample:
            break
    if not vals:
        return "empty"
    if all(isinstance(v, bool) for v in vals):
        return "bool"
    if all(_to_num(v) is not None for v in vals):
        return "number"
    if all(_looks_date(v) for v in vals):
        return "date"
    return "string"


def infer_fields(columns: list, rows: list, *, sample: int = 200) -> dict:
    """-> {"dimensions": [...], "measures": [...]}. dtype comes from up to `sample`
    NON-NULL values, never from row 0 (a leading null must not misclassify)."""
    cols = _cols(columns)
    dims, meas = [], []
    for i, c in enumerate(cols):
        name = c if isinstance(c, str) else str(c)
        dt = dtype_of(rows, i, sample=sample)
        if dt == "number" and not _MEASURE_NAME_RE.search(name):
            meas.append({"col": name, "label": name, "dtype": dt, "agg": "sum",
                         "format": "number"})
        else:
            role = "time" if (dt == "date" or _TIME_NAME_RE.search(name)) else "category"
            dims.append({"col": name, "label": name, "dtype": dt, "role": role})
    return {"dimensions": dims, "measures": meas}


def distinct_values(columns: list, rows: list, col: str, *, limit: int = 1000) -> list:
    i = _index(_cols(columns), col)
    if i < 0:
        return []
    seen, out = set(), []
    for r in _seq(rows):
        v = _cell(r, i)
        if _is_null(v):
            continue
        k = _hkey(v)
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= limit:
            break
    lut = _category_lut(out)
    return sorted(out, key=lambda v: _sortable(v, lut))


def field_catalog(columns: list, rows: list, spec: Optional[dict] = None, *,
                  limit: int = 200) -> list:
    """-> list[Field] (§2) minus `tiles`, which OWNER-2 fills in."""
    cols = _cols(columns)
    fields = infer_fields(cols, rows)
    labels, declared = {}, {}
    if isinstance(spec, dict) and isinstance(spec.get("fields"), dict):
        for kind in ("dimensions", "measures"):
            for f in spec["fields"].get(kind) or []:
                if isinstance(f, dict) and f.get("col"):
                    labels[f["col"]] = f.get("label") or f["col"]
                    declared[f["col"]] = "measure" if kind == "measures" else "dimension"
    kinds = {d["col"]: "dimension" for d in fields["dimensions"]}
    kinds.update({m["col"]: "measure" for m in fields["measures"]})
    roles = {d["col"]: d["role"] for d in fields["dimensions"]}

    out = []
    for i, c in enumerate(cols):
        name = c if isinstance(c, str) else str(c)
        kind = declared.get(name) or kinds.get(name, "dimension")
        dt = dtype_of(rows, i)
        count = len({_hkey(v) for v in (_cell(r, i) for r in _seq(rows)) if not _is_null(v)})
        show = None if (count > limit or dt == "number") else \
            distinct_values(cols, rows, name, limit=limit)
        lo = hi = None
        if dt in ("number", "date"):
            clean = [v for v in (_cell(r, i) for r in _seq(rows)) if not _is_null(v)]
            if clean:
                lut = _category_lut(clean)
                lo = min(clean, key=lambda v: _sortable(v, lut))
                hi = max(clean, key=lambda v: _sortable(v, lut))
        out.append({"col": name, "label": labels.get(name, name), "kind": kind, "dtype": dt,
                    "role": "measure" if kind == "measure" else roles.get(name, "category"),
                    "values": show, "value_count": count, "min": lo, "max": hi})
    return out
