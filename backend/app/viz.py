"""Measure & transform engine. Pure functions over (columns, rows). No I/O.

Owns the v2 chart spec: its vocabulary, its normalization, and the one
implementation of every data operation the spec can express. `transform` is
data and runs here, once, server-side; `format` is pixels and runs in the
browser. Neither ever does the other's job.

Two rules the rest of the system depends on:
  * Nothing here throws. Unknown key → dropped + warning; bad column
    reference → stage skipped + warning; unparseable input → identity. A
    hallucinating LLM must degrade, never 500 and never blank a chart.
  * `x` and `y` name columns in the FINAL, post-transform frame;
    output_columns() is the authority on what those names are.

Stdlib only. No imports from agent/db/chat/catalog — agent imports viz, not
the reverse.
"""
from __future__ import annotations

import ast
import copy
import math
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

# ── Vocabulary — single source of truth; the LLM prompt is generated from it ──
VERSION: int = 2

TRANSFORM_ORDER: tuple = ("derive", "bin", "filters", "unpivot", "group", "having",
                          "table_calc", "top_n", "sort", "pivot", "limit")

# Literal copy of agent.CHART_TYPES order (22 types).
CHART_TYPES: tuple = (
    "bar", "hbar", "stacked_bar", "line", "area", "stacked_area", "combo",
    "pie", "donut", "scatter", "bubble", "heatmap", "treemap", "funnel",
    "radar", "gauge", "kpi", "histogram", "boxplot", "waterfall", "sankey",
    "table",
)

OPS: tuple = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "between",
              "contains", "ncontains", "startswith", "endswith", "isnull",
              "notnull", "last_n")

AGGS: tuple = ("sum", "avg", "min", "max", "count", "count_distinct", "median",
               "p25", "p75", "p90", "stddev", "first", "last")

TABLE_CALCS: tuple = ("percent_of_total", "running_total", "cumulative_percent",
                      "difference", "percent_difference", "rank", "dense_rank",
                      "percentile", "moving_average", "index", "percent_of_max",
                      "z_score")

DATE_PARTS: tuple = ("hour", "day", "week", "month", "quarter", "year", "weekday")

# Label sets that must sort semantically, not alphabetically.
CATEGORY_ORDERS: dict = {
    "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "month": ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "quarter": ["Q1", "Q2", "Q3", "Q4"],
}
_WEEKDAY_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTH_FULL = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

MAX_OUTPUT_ROWS: int = 50_000
MAX_OUTPUT_COLS: int = 512
MAX_PIVOT_COLUMNS: int = 50
MAX_GROUPS: int = 5_000

MAX_EXPR_CHARS = 2_000     # expression source cap
MAX_EXPR_NODES = 500       # AST node budget — blocks pathological nesting
MAX_POW_EXPONENT = 8       # ** is capped so 2**999999 can never be built
MAX_EXPR_STR_CHARS = 64_000    # ceiling on any string an expression PRODUCES
MAX_DERIVE_SECONDS = 2.0       # wall-clock budget for the whole derive stage

# Accepted top-level keys. Anything else is dropped with a warning.
SPEC_KEYS: tuple = ("v", "type", "title", "x", "y", "fields", "transform", "format", "interaction")
FORMAT_KEYS: tuple = ("number", "date", "labels", "axes", "legend", "palette", "series",
                      "reference_lines", "conditional_colors", "annotations", "title",
                      "tooltip", "data_zoom", "totals", "empty_text")

_INTERACTION_DEFAULTS: dict = {
    "cross_filter": "filter",
    "affected_by": "auto",
    "self_highlight": True,
    "multi_select": True,
    "drill": {"hierarchy": [], "level": 0},
}

_CROSS_FILTER_MODES = ("filter", "highlight", "none")
_MEASURE_NAME_RE = re.compile(r"(?i)(^|_)(id|key|code|zip|year)$")
_TIME_NAME_RE = re.compile(r"(?i)date|time|day|month|year")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?([ T]\d{1,2}:\d{2}(:\d{2})?)?")
_SLASH_DATE_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})")
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")


class VizError(ValueError):
    """Raised only by compile_expr and by strict=True. Every other path warns."""


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
# Expressions — ast whitelist, compiled to closures. Never eval/exec.
# ══════════════════════════════════════════════════════════════════════════

def _cap_len(n: int) -> None:
    """The source caps bound an expression's INPUT; nesting amplifies its OUTPUT, so
    every string an expression produces is bounded here too."""
    if n > MAX_EXPR_STR_CHARS:
        raise VizError(f"expression produced a string longer than {MAX_EXPR_STR_CHARS} characters")


def _cap_str(s):
    if isinstance(s, str):
        _cap_len(len(s))
    return s


def _f_round(x, nd=0):
    n = _to_num(x)
    d = _to_num(nd)
    if n is None:
        return None
    try:
        return round(n, int(d or 0))
    except (ValueError, OverflowError):
        return None


def _f_minmax(pick):
    def fn(*args):
        vals = args[0] if len(args) == 1 and isinstance(args[0], (list, tuple)) else args
        nums = [n for n in (_to_num(v) for v in vals) if n is not None]
        if nums:
            return pick(nums)
        clean = [v for v in vals if not _is_null(v)]
        return pick(clean, key=lambda v: str(v)) if clean else None
    return fn


def _f_num1(fn):
    def wrapped(x):
        n = _to_num(x)
        if n is None:
            return None
        try:
            return fn(n)
        except (ValueError, OverflowError, ZeroDivisionError):
            return None
    return wrapped


def _f_log(x, base=None):
    n = _to_num(x)
    if n is None or n <= 0:
        return None
    try:
        if base is None:
            return math.log(n)
        b = _to_num(base)
        return math.log(n, b) if b and b > 0 and b != 1 else None
    except (ValueError, ZeroDivisionError):
        return None


def _f_str1(fn):
    def wrapped(x):
        if _is_null(x):
            return None
        try:
            return _cap_str(fn(str(x)))
        except (TypeError, ValueError):
            return None
    return wrapped


def _f_replace(s, find, repl=""):
    if _is_null(s):
        return None
    try:
        src, sub, rep = str(s), str(find), str(repl)
    except (TypeError, ValueError):
        return None
    # Sized BEFORE the copy exists — the produced string is what nesting amplifies.
    hits = src.count(sub) if sub else len(src) + 1
    _cap_len(len(src) + hits * (len(rep) - len(sub)))
    return src.replace(sub, rep)


def _f_contains(s, sub):
    if _is_null(s) or _is_null(sub):
        return False
    return str(sub).lower() in str(s).lower()


def _f_startswith(s, sub):
    if _is_null(s) or _is_null(sub):
        return False
    return str(s).lower().startswith(str(sub).lower())


def _f_endswith(s, sub):
    if _is_null(s) or _is_null(sub):
        return False
    return str(s).lower().endswith(str(sub).lower())


def _f_len(x):
    if _is_null(x):
        return 0
    try:
        return len(x) if isinstance(x, (str, list, tuple)) else len(str(x))
    except TypeError:
        return 0


def _f_coalesce(*args):
    for a in args:
        if not _is_null(a):
            return a
    return None


def _f_nz(v, default=0):
    return default if _is_null(v) else v


def _f_iif(cond, a, b=None):
    return a if _truthy(cond) else b


def _f_safe_div(a, b):
    na, nb = _to_num(a), _to_num(b)
    if na is None or nb in (None, 0.0):
        return None
    return na / nb


def _f_to_date(v):
    d = _parse_date(v)
    return d.strftime("%Y-%m-%d") if d else None


def _f_datepart(part):
    def fn(v):
        d = _parse_date(v)
        if not d:
            return None
        if part == "year":
            return d.year
        if part == "month":
            return d.month
        if part == "day":
            return d.day
        return CATEGORY_ORDERS["weekday"][d.weekday()]   # short name, matches bin labels
    return fn


def _f_clamp(v, lo, hi):
    n, a, b = _to_num(v), _to_num(lo), _to_num(hi)
    if n is None:
        return None
    if a is not None and n < a:
        return a
    if b is not None and n > b:
        return b
    return n


EXPR_FUNCS: dict = {
    "abs": lambda x: (lambda n: None if n is None else abs(n))(_to_num(x)),
    "round": _f_round,
    "min": _f_minmax(min),
    "max": _f_minmax(max),
    "floor": _f_num1(math.floor),
    "ceil": _f_num1(math.ceil),
    "sqrt": _f_num1(lambda n: math.sqrt(n) if n >= 0 else None),
    "log": _f_log,
    "coalesce": _f_coalesce,
    "nz": _f_nz,
    "lower": _f_str1(str.lower),
    "upper": _f_str1(str.upper),
    "trim": _f_str1(str.strip),
    "len": _f_len,
    "replace": _f_replace,
    "contains": _f_contains,
    "startswith": _f_startswith,
    "endswith": _f_endswith,
    "iif": _f_iif,
    "safe_div": _f_safe_div,
    "to_number": _to_num,
    "to_date": _f_to_date,
    "year": _f_datepart("year"),
    "month": _f_datepart("month"),
    "day": _f_datepart("day"),
    "weekday": _f_datepart("weekday"),
    "clamp": _f_clamp,
}


def _truthy(v: Any) -> bool:
    if _is_null(v):
        return False
    if isinstance(v, bool):
        return v
    n = _to_num(v)
    if n is not None:
        return n != 0
    return bool(v)


def _bin_add(a, b):
    if isinstance(a, str) and isinstance(b, str):
        _cap_len(len(a) + len(b))
        return a + b
    na, nb = _to_num(a), _to_num(b)
    return None if na is None or nb is None else na + nb


def _bin_arith(fn):
    def op(a, b):
        na, nb = _to_num(a), _to_num(b)
        if na is None or nb is None:
            return None
        try:
            return fn(na, nb)
        except (ZeroDivisionError, ValueError, OverflowError):
            return None
    return op


def _bin_pow(a, b):
    na, nb = _to_num(a), _to_num(b)
    if na is None or nb is None or abs(nb) > MAX_POW_EXPONENT:
        return None
    try:
        return na ** nb
    except (ZeroDivisionError, ValueError, OverflowError):
        return None


_BINOPS = {
    ast.Add: _bin_add,
    ast.Sub: _bin_arith(lambda a, b: a - b),
    ast.Mult: _bin_arith(lambda a, b: a * b),
    ast.Div: _bin_arith(lambda a, b: a / b),
    ast.FloorDiv: _bin_arith(lambda a, b: a // b),
    ast.Mod: _bin_arith(lambda a, b: a % b),
    ast.Pow: _bin_pow,
}

_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PLACEHOLDER = "_vizcol_%d_"


def _cmp_eq(a, b) -> bool:
    if _is_null(a) or _is_null(b):
        return _is_null(a) and _is_null(b)
    na, nb = _to_num(a), _to_num(b)
    if na is not None and nb is not None:
        return na == nb
    return str(a) == str(b)


def _cmp_order(a, b, want) -> bool:
    c = coerce_compare(a, b)
    return False if c is None else c in want


def _cmp_in(a, b) -> bool:
    try:
        if isinstance(b, (list, tuple, set)):
            return any(_cmp_eq(a, x) for x in b)
        if isinstance(b, str):
            return str(a).lower() in b.lower()
    except (TypeError, ValueError):
        return False
    return False


_CMPOPS = {
    ast.Eq: _cmp_eq,
    ast.NotEq: lambda a, b: not _cmp_eq(a, b),
    ast.Lt: lambda a, b: _cmp_order(a, b, (-1,)),
    ast.LtE: lambda a, b: _cmp_order(a, b, (-1, 0)),
    ast.Gt: lambda a, b: _cmp_order(a, b, (1,)),
    ast.GtE: lambda a, b: _cmp_order(a, b, (1, 0)),
    ast.In: _cmp_in,
    ast.NotIn: lambda a, b: not _cmp_in(a, b),
}


def compile_expr(expr: str, columns: list) -> Callable[[list], Any]:
    """ast.parse + node whitelist. Raises VizError on a disallowed node or unknown
    name. The returned callable takes a row and returns a scalar; it swallows
    per-row arithmetic/type errors and returns None, but raises VizError when a row
    would produce a string over MAX_EXPR_STR_CHARS — a cap that must not degrade to
    a silent null, because the memory has already been asked for."""
    if not isinstance(expr, str) or not expr.strip():
        raise VizError("expression is empty")
    if len(expr) > MAX_EXPR_CHARS:
        raise VizError(f"expression exceeds {MAX_EXPR_CHARS} characters")

    cols = _cols(columns)
    aliases: dict = {}

    def sub(m):
        name = m.group(1)
        key = _PLACEHOLDER % len(aliases)
        aliases[key] = name
        return key

    src = _BACKTICK_RE.sub(sub, expr)

    try:
        # Warning is caught too: under `python -W error` a SyntaxWarning from hostile
        # input would otherwise escape as something other than VizError.
        tree = ast.parse(src, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError, Warning) as e:
        raise VizError(f"cannot parse expression: {e}") from None

    n_nodes = sum(1 for _ in ast.walk(tree))
    if n_nodes > MAX_EXPR_NODES:
        raise VizError("expression is too complex")

    lut = {c: i for i, c in enumerate(cols) if isinstance(c, str)}
    lut_low = {c.lower(): i for i, c in enumerate(cols) if isinstance(c, str)}

    def resolve(name: str) -> int:
        real = aliases.get(name, name)
        if real in lut:
            return lut[real]
        i = lut_low.get(real.lower())
        if i is None:
            raise VizError(f"unknown column '{real}' in expression")
        return i

    return _compile_node(tree, resolve)


def _compile_node(node, resolve) -> Callable[[list], Any]:
    """Whitelist by construction — any node without a branch here is rejected."""
    if isinstance(node, ast.Expression):
        return _compile_node(node.body, resolve)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool, type(None))):
            v = node.value
            return lambda row: v
        raise VizError("only string, number, bool and null literals are allowed")

    if isinstance(node, ast.Name):
        i = resolve(node.id)
        return lambda row: _cell(row, i)

    if isinstance(node, ast.BinOp):
        fn = _BINOPS.get(type(node.op))
        if fn is None:
            raise VizError(f"operator {type(node.op).__name__} is not allowed")
        if isinstance(node.op, ast.Pow) and isinstance(node.right, ast.Constant):
            e = _to_num(node.right.value)
            if e is None or abs(e) > MAX_POW_EXPONENT:
                raise VizError(f"exponent must be a number ≤ {MAX_POW_EXPONENT}")
        left, right = _compile_node(node.left, resolve), _compile_node(node.right, resolve)
        return lambda row: _guard(fn, left(row), right(row))

    if isinstance(node, ast.UnaryOp):
        operand = _compile_node(node.operand, resolve)
        if isinstance(node.op, ast.USub):
            return lambda row: (lambda n: None if n is None else -n)(_to_num(operand(row)))
        if isinstance(node.op, ast.UAdd):
            return lambda row: _to_num(operand(row))
        if isinstance(node.op, ast.Not):
            return lambda row: not _truthy(operand(row))
        raise VizError(f"unary {type(node.op).__name__} is not allowed")

    if isinstance(node, ast.BoolOp):
        parts = [_compile_node(v, resolve) for v in node.values]
        if isinstance(node.op, ast.And):
            def and_(row):
                last = True
                for p in parts:
                    last = p(row)
                    if not _truthy(last):
                        return last
                return last
            return and_
        def or_(row):
            last = False
            for p in parts:
                last = p(row)
                if _truthy(last):
                    return last
            return last
        return or_

    if isinstance(node, ast.Compare):
        left = _compile_node(node.left, resolve)
        ops = []
        for op, comp in zip(node.ops, node.comparators):
            fn = _CMPOPS.get(type(op))
            if fn is None:
                raise VizError(f"comparison {type(op).__name__} is not allowed")
            ops.append((fn, _compile_node(comp, resolve)))

        def cmp_(row):
            a = left(row)
            for fn, rhs in ops:
                b = rhs(row)
                if not _guard(fn, a, b):
                    return False
                a = b
            return True
        return cmp_

    if isinstance(node, ast.IfExp):
        test = _compile_node(node.test, resolve)
        body = _compile_node(node.body, resolve)
        orelse = _compile_node(node.orelse, resolve)
        return lambda row: body(row) if _truthy(test(row)) else orelse(row)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise VizError("only bare function calls from the allowed list are permitted")
        fn = EXPR_FUNCS.get(node.func.id)
        if fn is None:
            raise VizError(f"unknown function '{node.func.id}'")
        if node.keywords:
            raise VizError("keyword arguments are not allowed")
        args = []
        for a in node.args:
            if isinstance(a, ast.Starred):
                raise VizError("argument unpacking is not allowed")
            args.append(_compile_node(a, resolve))

        def call_(row):
            try:
                return fn(*[a(row) for a in args])
            except VizError:
                raise                             # a size cap is fatal, not a per-row null
            except MemoryError:
                raise VizError("expression exhausted memory") from None
            except Exception:
                return None
        return call_

    if isinstance(node, (ast.List, ast.Tuple)):
        items = [_compile_node(e, resolve) for e in node.elts]
        return lambda row: [i(row) for i in items]

    raise VizError(f"{type(node).__name__} is not allowed in an expression")


def _guard(fn, a, b):
    try:
        return fn(a, b)
    except VizError:
        raise
    except MemoryError:
        raise VizError("expression exhausted memory") from None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════
# Predicates — the one filter implementation
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


# ══════════════════════════════════════════════════════════════════════════
# Transform normalization
# ══════════════════════════════════════════════════════════════════════════

def _as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def _as_str_list(v) -> list:
    return [x if isinstance(x, str) else str(x) for x in _as_list(v) if x is not None]


def _dir(v, default="asc") -> str:
    s = str(v or default).lower()
    return "desc" if s.startswith("desc") else "asc"


def _pyliteral(s: Any) -> str:
    return repr("" if s is None else str(s))


def _default_measure_as(col: str, agg: str) -> str:
    if agg == "count":
        return "count"
    if agg in ("sum", "avg", "min", "max", "first", "last"):
        return col
    return f"{agg}_{col}"


def normalize_transform(transform: Optional[dict]) -> dict:
    """v1 flat shape → canonical v2 with defaults filled. Always returns a dict."""
    if not isinstance(transform, dict) or not transform:
        return {}
    t = dict(transform)
    out: dict = {}

    # ── v1 upgrades (§1.2) ──
    f = t.pop("filter", None)
    if isinstance(f, dict) and f.get("column"):
        t.setdefault("filters", [])
        if isinstance(t["filters"], list):
            t["filters"] = list(t["filters"]) + [{"col": f.get("column"),
                                                  "op": f.get("op") or "eq",
                                                  "value": f.get("value")}]
    rep = t.pop("replace", None)
    if isinstance(rep, dict) and rep.get("column"):
        expr = (f"replace(`{rep['column']}`, {_pyliteral(rep.get('find'))}, "
                f"{_pyliteral(rep.get('replace'))})")
        t["derive"] = list(t.get("derive") or []) + [
            {"as": rep["column"], "expr": expr, "kind": "dimension"}]
    s = t.get("sort")
    if isinstance(s, dict) and s.get("column"):
        t["sort"] = [{"col": s["column"], "dir": _dir(s.get("dir")), "nulls": "last"}]
    n = t.get("top_n")
    if isinstance(n, bool):
        t["top_n"] = None
    elif isinstance(n, (int, float)) and not isinstance(n, bool):
        # `by` is resolved at run time (first numeric column) or by sanitize_spec.
        t["top_n"] = {"by": None, "n": int(n), "dir": "top"} if int(n) > 0 else None

    # ── canonical v2 ──
    derive = []
    for d in _as_list(t.get("derive")):
        if isinstance(d, dict) and d.get("as") and d.get("expr"):
            derive.append({"as": str(d["as"]), "expr": str(d["expr"]),
                           "kind": "dimension" if d.get("kind") == "dimension" else "measure",
                           "agg": d.get("agg") if d.get("agg") in AGGS else "sum"})
    if derive:
        out["derive"] = derive

    bins = []
    for b in _as_list(t.get("bin")):
        if not isinstance(b, dict) or not b.get("col"):
            continue
        part = b.get("date_part") if b.get("date_part") in DATE_PARTS else None
        size, count = _to_num(b.get("size")), _to_num(b.get("count"))
        if part is None and size is None and count is None:
            continue
        bins.append({"col": str(b["col"]), "as": str(b.get("as") or b["col"]),
                     "date_part": part,
                     "size": size if (size and size > 0) else None,
                     "count": int(count) if (count and count > 0) else None,
                     "labels": b.get("labels") if b.get("labels") in ("range", "lower", "mid") else "range"})
    if bins:
        out["bin"] = bins

    for key in ("filters", "having"):
        preds = [p for p in _as_list(t.get(key)) if isinstance(p, dict) and p.get("col")]
        norm = []
        for p in preds:
            q = {"col": str(p["col"]), "op": p.get("op") if p.get("op") in OPS else "eq"}
            for k in ("value", "values", "lo", "hi", "unit", "origin", "source_tile"):
                if p.get(k) is not None:
                    q[k] = p[k]
            q.setdefault("origin", "spec")
            norm.append(q)
        if norm:
            out[key] = norm

    up = t.get("unpivot")
    if isinstance(up, dict) and (up.get("keep") is not None or up.get("cols") is not None):
        out["unpivot"] = {"keep": _as_str_list(up.get("keep")),
                          "cols": _as_str_list(up["cols"]) if up.get("cols") is not None else None,
                          "name_as": str(up.get("name_as") or "series"),
                          "value_as": str(up.get("value_as") or "value"),
                          "drop_nulls": up.get("drop_nulls") is not False}

    g = t.get("group")
    if isinstance(g, dict) and ("by" in g or "measures" in g):
        measures = []
        for m in _as_list(g.get("measures")):
            if not isinstance(m, dict):
                continue
            agg = m.get("agg") if m.get("agg") in AGGS else "sum"
            col = str(m.get("col") or "")
            if not col and agg != "count":
                continue
            measures.append({"col": col, "agg": agg,
                             "as": str(m.get("as") or _default_measure_as(col, agg))})
        out["group"] = {"by": _as_str_list(g.get("by")), "measures": measures,
                        "keep_other_cols": bool(g.get("keep_other_cols"))}

    calcs = []
    for c in _as_list(t.get("table_calc")):
        if not isinstance(c, dict) or c.get("calc") not in TABLE_CALCS:
            continue
        col = str(c.get("col") or "")
        if not col:
            continue
        win = _to_num(c.get("window"))
        calcs.append({"col": col, "calc": c["calc"],
                      "as": str(c.get("as") or f"{col}_{c['calc']}"),
                      "partition_by": _as_str_list(c.get("partition_by")),
                      "order_by": str(c["order_by"]) if c.get("order_by") else None,
                      "dir": _dir(c.get("dir")),
                      "window": max(2, int(win)) if win else 3,
                      "stage": "post_top_n" if c.get("stage") == "post_top_n" else "pre_top_n"})
    if calcs:
        out["table_calc"] = calcs

    tn = t.get("top_n")
    if isinstance(tn, dict):
        n = _to_num(tn.get("n"))
        if n and n > 0:
            other = tn.get("other") if isinstance(tn.get("other"), dict) else {}
            out["top_n"] = {"by": str(tn["by"]) if tn.get("by") else None, "n": int(n),
                            "dir": "bottom" if str(tn.get("dir", "top")).lower() == "bottom" else "top",
                            "within": _as_str_list(tn.get("within")),
                            "other": {"enabled": bool(other.get("enabled")),
                                      "label": str(other.get("label") or "Other"),
                                      "dim": str(other["dim"]) if other.get("dim") else None,
                                      "agg": other.get("agg") if other.get("agg") in
                                             ("sum", "avg", "min", "max") else "sum"}}

    sort = []
    for s in _as_list(t.get("sort")):
        if isinstance(s, str):
            sort.append({"col": s, "dir": "asc", "nulls": "last"})
        elif isinstance(s, dict) and (s.get("col") or s.get("column")):
            sort.append({"col": str(s.get("col") or s.get("column")), "dir": _dir(s.get("dir")),
                         "nulls": "first" if s.get("nulls") == "first" else "last"})
    if sort:
        out["sort"] = sort

    p = t.get("pivot")
    if isinstance(p, dict) and p.get("columns") and p.get("values"):
        mx = _to_num(p.get("max_columns"))
        out["pivot"] = {"index": _as_str_list(p.get("index")), "columns": str(p["columns"]),
                        "values": str(p["values"]),
                        "agg": p.get("agg") if p.get("agg") in AGGS else "sum",
                        "fill": p.get("fill"),
                        "max_columns": int(mx) if mx and mx > 0 else MAX_PIVOT_COLUMNS}

    lim = _to_num(t.get("limit"))
    if lim and lim > 0:
        out["limit"] = int(lim)

    return out


# ══════════════════════════════════════════════════════════════════════════
# Stages — each takes/returns a frame {columns, rows, other}
# ══════════════════════════════════════════════════════════════════════════

def _frame(columns, rows) -> dict:
    return {"columns": _cols(columns),
            "rows": _rows(rows),
            "other": set()}


def _st_derive(fr, cfg, warn):
    deadline = time.monotonic() + MAX_DERIVE_SECONDS
    for d in cfg or []:
        try:
            fn = compile_expr(d["expr"], fr["columns"])
            # Evaluated into a column BEFORE it is attached: a mid-way abort must not
            # leave half the rows widened.
            vals = []
            for r in fr["rows"]:
                vals.append(fn(r))
                if time.monotonic() > deadline:
                    raise VizError(f"derive exceeded its {MAX_DERIVE_SECONDS:g}s budget")
        except VizError as e:
            warn.append(f"derive '{d.get('as')}': {e} — skipped")
            continue
        name, i = d["as"], _index(fr["columns"], d["as"])
        if i < 0:
            fr["columns"].append(name)
            for r, v in zip(fr["rows"], vals):
                r.append(v)
        else:
            for r, v in zip(fr["rows"], vals):
                while len(r) < len(fr["columns"]):
                    r.append(None)
                r[i] = v
    return fr


def _bin_date(v, part):
    d = _parse_date(v)
    if d is None:
        return None
    if part == "hour":
        return d.strftime("%Y-%m-%d %H")
    if part == "day":
        return d.strftime("%Y-%m-%d")
    if part == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if part == "month":
        return d.strftime("%Y-%m")
    if part == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if part == "year":
        return str(d.year)
    if part == "weekday":
        return CATEGORY_ORDERS["weekday"][d.weekday()]
    return None


def _st_bin(fr, cfg, warn):
    for b in cfg or []:
        i = _index(fr["columns"], b["col"])
        if i < 0:
            warn.append(f"bin: column '{b['col']}' not found — skipped")
            continue
        vals = [_cell(r, i) for r in fr["rows"]]
        if b.get("date_part"):
            out = [_bin_date(v, b["date_part"]) for v in vals]
        else:
            nums = [n for n in (_to_num(v) for v in vals) if n is not None]
            if not nums:
                warn.append(f"bin: column '{b['col']}' has no numeric values — skipped")
                continue
            lo_all, hi_all = min(nums), max(nums)
            size = b.get("size")
            if not size:
                cnt = max(1, int(b.get("count") or 10))
                size = (hi_all - lo_all) / cnt if hi_all > lo_all else 1.0
                nbins = cnt
            else:
                # 1e-9 absorbs the float error in an exact multiple, which would
                # otherwise buy a phantom bin.
                nbins = max(1, math.ceil((hi_all - lo_all) / size - 1e-9))
            size = size or 1.0
            out = []
            for v in vals:
                n = _to_num(v)
                if n is None:
                    out.append(None)
                    continue
                # Bins are half-open except the last, which closes on the maximum —
                # without the clamp the max opens a bin of its own.
                k = min(math.floor((n - lo_all) / size), nbins - 1)
                lo = k * size + lo_all
                hi = lo + size
                if b.get("labels") == "lower":
                    out.append(lo)
                elif b.get("labels") == "mid":
                    out.append((lo + hi) / 2)
                else:
                    out.append(f"{_fmt_num(lo)}–{_fmt_num(hi)}")
        name, j = b["as"], _index(fr["columns"], b["as"])
        if j < 0:
            fr["columns"].append(name)
            for r, v in zip(fr["rows"], out):
                r.append(v)
        else:
            for r, v in zip(fr["rows"], out):
                while len(r) < len(fr["columns"]):
                    r.append(None)
                r[j] = v
    return fr


def _st_filters(fr, cfg, warn):
    # Filters by index so a top_n "Other" row keeps its pin through post-stage injection.
    cols, keep = fr["columns"], list(range(len(fr["rows"])))
    for f in cfg or []:
        if not isinstance(f, dict):
            continue
        if _index(cols, f.get("col")) < 0 or (f.get("op") or "eq") not in OPS:
            warn.append(f"filters: '{(f or {}).get('col')}' is not filterable here — dropped")
            continue
        p = _resolve_last_n(cols, fr["rows"], f) if f.get("op") == "last_n" else f
        keep = [i for i in keep if match_row(fr["rows"][i], cols, p)]
    other = fr.get("other") or set()
    fr["rows"] = [fr["rows"][i] for i in keep]
    fr["other"] = {n for n, i in enumerate(keep) if i in other}
    return fr


def _st_unpivot(fr, cfg, warn):
    cols = fr["columns"]
    keep = [c for c in (cfg.get("keep") or []) if _index(cols, c) >= 0]
    if cfg.get("cols") is None:
        value_cols = [c for c in cols
                      if c not in keep and dtype_of(fr["rows"], _index(cols, c)) == "number"]
    else:
        value_cols = [c for c in cfg["cols"] if _index(cols, c) >= 0 and c not in keep]
    if not value_cols:
        warn.append("unpivot: no value columns — skipped")
        return fr
    name_as, value_as = cfg.get("name_as") or "series", cfg.get("value_as") or "value"
    ki = [_index(cols, c) for c in keep]
    out_rows = []
    for r in fr["rows"]:
        head = [_cell(r, i) for i in ki]
        for c in value_cols:
            v = _cell(r, _index(cols, c))
            if cfg.get("drop_nulls") is not False and _is_null(v):
                continue
            out_rows.append(head + [c, v])
    fr["columns"] = keep + [name_as, value_as]
    fr["rows"] = out_rows
    return fr


def _st_group(fr, cfg, warn):
    cols = fr["columns"]
    by = [c for c in (cfg.get("by") or []) if _index(cols, c) >= 0]
    for c in (cfg.get("by") or []):
        if _index(cols, c) < 0:
            warn.append(f"group: column '{c}' not found — ignored")
    measures = []
    for m in cfg.get("measures") or []:
        if m["agg"] != "count" and _index(cols, m["col"]) < 0:
            warn.append(f"group: measure column '{m['col']}' not found — dropped")
            continue
        measures.append(m)
    if not by and not measures:
        warn.append("group: nothing to group — skipped")
        return fr

    bi = [_index(cols, c) for c in by]
    others = [c for c in cols if c not in by] if cfg.get("keep_other_cols") else []
    buckets: dict = {}
    for r in fr["rows"]:
        key = tuple(_hkey(_cell(r, i)) for i in bi)
        b = buckets.get(key)
        if b is None:
            if len(buckets) >= MAX_GROUPS:
                warn.append(f"group: capped at {MAX_GROUPS} groups")
                continue
            b = buckets[key] = {"head": [_cell(r, i) for i in bi], "rows": []}
        b["rows"].append(r)

    out_cols = list(by) + [m["as"] for m in measures] + list(others)
    out_rows = []
    for b in buckets.values():
        row = list(b["head"])
        for m in measures:
            j = _index(cols, m["col"])
            vals = [_cell(r, j) for r in b["rows"]] if j >= 0 else []
            row.append(_aggregate(vals, m["agg"], n_rows=len(b["rows"])))
        for c in others:
            j = _index(cols, c)
            row.append(_cell(b["rows"][0], j) if b["rows"] else None)
        out_rows.append(row)
    fr["columns"], fr["rows"] = out_cols, out_rows
    return fr


def _st_having(fr, cfg, warn):
    return _st_filters(fr, cfg, warn)


def _calc_series(vals: list, calc: str, cfg: dict) -> list:
    """One partition, already in display order. Returns one value per input position."""
    n = len(vals)
    nums = [_to_num(v) for v in vals]
    clean = [x for x in nums if x is not None]
    total = sum(clean) if clean else 0.0
    out: list = [None] * n

    if calc == "index":
        return [i + 1 for i in range(n)]
    if calc in ("rank", "dense_rank", "percentile"):
        order = sorted(range(n), key=lambda i: _sortable(vals[i]),
                       reverse=(cfg.get("dir") == "desc"))
        rank, dense, prev = 0, 0, object()
        ranks: dict = {}
        for pos, i in enumerate(order):
            key = _sortable(vals[i])
            if key != prev:
                rank, dense, prev = pos + 1, dense + 1, key
            ranks[i] = (rank, dense)
        if calc == "rank":
            return [ranks[i][0] for i in range(n)]
        if calc == "dense_rank":
            return [ranks[i][1] for i in range(n)]
        return [((ranks[i][0] - 1) / (n - 1)) if n > 1 else 0.0 for i in range(n)]

    run = 0.0
    for i, x in enumerate(nums):
        if calc == "percent_of_total":
            out[i] = (x / total) if (x is not None and total) else None
        elif calc == "running_total":
            if x is not None:
                run += x
            out[i] = run
        elif calc == "cumulative_percent":
            if x is not None:
                run += x
            out[i] = (run / total) if total else None
        elif calc == "difference":
            prev = nums[i - 1] if i else None
            out[i] = (x - prev) if (i and x is not None and prev is not None) else None
        elif calc == "percent_difference":
            prev = nums[i - 1] if i else None
            out[i] = ((x - prev) / prev) if (i and x is not None and prev) else None
        elif calc == "moving_average":
            w = max(2, int(cfg.get("window") or 3))
            win = [y for y in nums[max(0, i - w + 1):i + 1] if y is not None]
            out[i] = (sum(win) / len(win)) if win else None
        elif calc == "percent_of_max":
            mx = max(clean) if clean else None
            out[i] = (x / mx) if (x is not None and mx) else None
        elif calc == "z_score":
            sd = _stddev(clean)
            mean = (sum(clean) / len(clean)) if clean else None
            out[i] = ((x - mean) / sd) if (x is not None and sd and mean is not None) else None
    return out


def _st_table_calc(fr, cfg, warn):
    for c in cfg or []:
        cols = fr["columns"]
        i = _index(cols, c["col"])
        if i < 0:
            warn.append(f"table_calc: column '{c['col']}' not found — skipped")
            continue
        pi = [_index(cols, p) for p in c.get("partition_by") or [] if _index(cols, p) >= 0]
        oi = _index(cols, c["order_by"]) if c.get("order_by") else -1
        parts: dict = {}
        for idx, r in enumerate(fr["rows"]):
            parts.setdefault(tuple(_hkey(_cell(r, k)) for k in pi), []).append(idx)
        result: list = [None] * len(fr["rows"])
        for idxs in parts.values():
            order = idxs
            if oi >= 0:
                lut = _category_lut([_cell(fr["rows"][k], oi) for k in idxs])
                order = sorted(idxs, key=lambda k: _sortable(_cell(fr["rows"][k], oi), lut),
                               reverse=(c.get("dir") == "desc"))
            vals = [_cell(fr["rows"][k], i) for k in order]
            for k, v in zip(order, _calc_series(vals, c["calc"], c)):
                result[k] = v
        name, j = c["as"], _index(cols, c["as"])
        if j < 0:
            if len(cols) >= MAX_OUTPUT_COLS:
                warn.append(f"table_calc: column cap {MAX_OUTPUT_COLS} reached — '{name}' skipped")
                continue
            fr["columns"].append(name)
            for r, v in zip(fr["rows"], result):
                r.append(v)
        else:
            for r, v in zip(fr["rows"], result):
                while len(r) < len(fr["columns"]):
                    r.append(None)
                r[j] = v
    return fr


def _numeric_cols(columns, rows) -> list:
    return [c for c in columns if dtype_of(rows, _index(columns, c)) == "number"]


def _rank_key(v, rev: bool):
    """Ranking key for top_n. Numbers always beat non-numerics, which always beat
    nulls, whichever end `rev` is keeping — a NULL must never win a "top n"."""
    if _is_null(v):
        return (0 if rev else 2, "")
    n = _to_num(v)
    if n is None:
        return (1, str(v))
    return (2 if rev else 0, n)


def _st_top_n(fr, cfg, warn):
    cols, rows = fr["columns"], fr["rows"]
    by = cfg.get("by")
    i = _index(cols, by)
    if i < 0:
        nums = _numeric_cols(cols, rows)
        if not nums:
            warn.append("top_n: no numeric column to rank on — skipped")
            return fr
        i = _index(cols, nums[0])
        if by:
            warn.append(f"top_n: column '{by}' not found — ranked on '{cols[i]}'")
    n = int(cfg.get("n") or 0)
    if n <= 0:
        return fr
    within = [c for c in cfg.get("within") or [] if _index(cols, c) >= 0]
    wi = [_index(cols, c) for c in within]
    other = cfg.get("other") or {}
    rev = cfg.get("dir", "top") != "bottom"

    groups: dict = {}
    for r in rows:
        groups.setdefault(tuple(_hkey(_cell(r, k)) for k in wi), []).append(r)

    kept, tails = [], []
    for gk, grp in groups.items():
        ordered = sorted(grp, key=lambda r: _rank_key(_cell(r, i), rev), reverse=rev)
        kept.extend(ordered[:n])
        if other.get("enabled") and len(ordered) > n:
            tails.append((gk, grp[0], ordered[n:]))

    # Keep the caller's row order among survivors; the sort stage owns display order.
    keep_ids = {id(r) for r in kept}
    out = [r for r in rows if id(r) in keep_ids]
    fr["other"] = set()
    for gk, sample, tail in tails:
        dim = other.get("dim")
        di = _index(cols, dim) if dim else -1
        row = []
        for ci, c in enumerate(cols):
            if ci == di:
                row.append(other.get("label") or "Other")
            elif ci in wi:
                row.append(_cell(sample, ci))            # keep the group's identity
            elif dtype_of(rows, ci) == "number":
                row.append(_aggregate([_cell(r, ci) for r in tail], other.get("agg") or "sum"))
            else:
                row.append(None)
        if di < 0:
            # No dim named: label the first non-numeric column so the row is readable.
            for ci, c in enumerate(cols):
                if ci not in wi and dtype_of(rows, ci) != "number":
                    row[ci] = other.get("label") or "Other"
                    break
        out.append(row)
        fr["other"].add(len(out) - 1)
    fr["rows"] = out
    return fr


def _st_sort(fr, cfg, warn):
    cols, rows = fr["columns"], fr["rows"]
    keys = [k for k in (cfg or []) if _index(cols, k.get("col")) >= 0]
    for k in (cfg or []):
        if _index(cols, k.get("col")) < 0:
            warn.append(f"sort: column '{k.get('col')}' not found — ignored")
    if not keys:
        return fr
    idx = list(range(len(rows)))
    for k in reversed(keys):                     # stable successive sorts = multi-key
        i = _index(cols, k["col"])
        lut = _category_lut([_cell(r, i) for r in rows])
        rev = k.get("dir") == "desc"
        nulls_last = k.get("nulls", "last") != "first"
        nrank = 1 if nulls_last != rev else 0
        idx.sort(key=lambda p: ((nrank, (0, 0.0)) if _is_null(_cell(rows[p], i))
                                else (1 - nrank, _sortable(_cell(rows[p], i), lut))),
                 reverse=rev)
    other = fr.get("other") or set()
    if other:                                    # the top_n "Other" row is always last
        idx.sort(key=lambda p: 1 if p in other else 0)
    fr["rows"] = [rows[p] for p in idx]
    fr["other"] = {n for n, p in enumerate(idx) if p in other}
    return fr


def _st_pivot(fr, cfg, warn):
    cols, rows = fr["columns"], fr["rows"]
    ci, vi = _index(cols, cfg.get("columns")), _index(cols, cfg.get("values"))
    if ci < 0 or vi < 0:
        warn.append("pivot: columns/values not found — skipped")
        return fr
    index = [c for c in cfg.get("index") or [] if _index(cols, c) >= 0]
    ii = [_index(cols, c) for c in index]
    heads, cells = {}, {}
    seen_cols: dict = {}
    for r in rows:
        key = tuple(_hkey(_cell(r, k)) for k in ii)
        heads.setdefault(key, [_cell(r, k) for k in ii])
        cv = _cell(r, ci)
        label = "" if _is_null(cv) else str(cv)
        seen_cols.setdefault(label, cv)
        cells.setdefault((key, label), []).append(_cell(r, vi))
    lut = _category_lut(seen_cols.values())
    labels = sorted(seen_cols, key=lambda s: _sortable(seen_cols[s], lut))
    cap = min(int(cfg.get("max_columns") or MAX_PIVOT_COLUMNS), MAX_PIVOT_COLUMNS,
              max(1, MAX_OUTPUT_COLS - len(index)))
    if len(labels) > cap:
        warn.append(f"pivot: {len(labels)} columns capped to {cap}")
        labels = labels[:cap]
    agg, fill = cfg.get("agg") or "sum", cfg.get("fill")
    out_rows = []
    for key, head in heads.items():
        row = list(head)
        for label in labels:
            vals = cells.get((key, label))
            row.append(_aggregate(vals, agg) if vals else fill)
        out_rows.append(row)
    fr["columns"], fr["rows"], fr["other"] = index + labels, out_rows, set()
    return fr


def _st_limit(fr, cfg, warn):
    n = _to_num(cfg)
    if n and n > 0:
        fr["rows"] = fr["rows"][:int(n)]
    return fr


_STAGES = {"derive": _st_derive, "bin": _st_bin, "filters": _st_filters,
           "unpivot": _st_unpivot, "group": _st_group, "having": _st_having,
           "table_calc": _st_table_calc, "top_n": _st_top_n, "sort": _st_sort,
           "pivot": _st_pivot, "limit": _st_limit}


def apply_stage(name: str, columns: list, rows: list, cfg: Any) -> tuple:
    """Run ONE named stage. Exposed for tests and ad-hoc filter injection."""
    fn = _STAGES.get(name)
    if fn is None or cfg in (None, [], {}):
        return _cols(columns), _rows(rows)
    fr, warn = _frame(columns, rows), []
    try:
        norm = normalize_transform({name: cfg}).get(name)
        if norm in (None, [], {}):
            return fr["columns"], fr["rows"]
        fr = fn(fr, norm, warn)
    except Exception:
        return _cols(columns), _rows(rows)
    return fr["columns"], fr["rows"]


# ══════════════════════════════════════════════════════════════════════════
# The engine
# ══════════════════════════════════════════════════════════════════════════

def _split_calcs(calcs: list) -> tuple:
    pre = [c for c in calcs or [] if c.get("stage") != "post_top_n"]
    post = [c for c in calcs or [] if c.get("stage") == "post_top_n"]
    return pre, post


def run_transform(columns: list, rows: list, transform: Optional[dict] = None, *,
                  injected: Optional[list] = None, max_rows: Optional[int] = None,
                  strict: bool = False) -> dict:
    """Rich path used by /canvas/edit and dashboards.py. Never raises unless strict."""
    warn: list = []
    base_cols = _cols(columns)
    fr = _frame(base_cols, rows)
    in_rows = len(fr["rows"])
    t = normalize_transform(transform)
    applied: list = []
    placed: list = []
    skipped: list = []

    # ── Injected filters land at the EARLIEST stage where their column exists ──
    pre_inject, post_inject = [], []
    early = set(base_cols) | {d["as"] for d in t.get("derive", [])} | {b["as"] for b in t.get("bin", [])}
    final = output_columns(base_cols, t, fr["rows"])
    for f in (injected or []):
        if not isinstance(f, dict) or (f.get("op") or "eq") not in OPS:
            skipped.append({"col": (f or {}).get("col") if isinstance(f, dict) else None,
                            "reason": "unknown filter op"})
            continue
        col = f.get("col")
        if _index(list(early), col) >= 0:
            pre_inject.append(f)
            placed.append({**f, "stage": "pre_group"})
        elif _index(final, col) >= 0:
            post_inject.append(f)
            placed.append({**f, "stage": "post_calc"})
        else:
            skipped.append({"col": col, "reason": "column not present in this view"})

    pre_calcs, post_calcs = _split_calcs(t.get("table_calc"))

    def run(name, cfg):
        nonlocal fr
        if cfg in (None, [], {}):
            return
        try:
            fr = _STAGES[name](fr, cfg, warn)
        except Exception as e:                    # a broken stage must not blank the chart
            warn.append(f"{name}: {type(e).__name__} — stage skipped")
            skipped.append({"stage": name, "reason": str(e)[:200]})
            return
        applied.append(name)

    run("derive", t.get("derive"))
    run("bin", t.get("bin"))
    run("filters", (t.get("filters") or []) + pre_inject)
    run("unpivot", t.get("unpivot"))
    run("group", t.get("group"))
    run("having", t.get("having"))
    run("table_calc", pre_calcs)
    run("top_n", t.get("top_n"))
    run("table_calc", post_calcs)
    if post_inject:
        run("filters", post_inject)
    run("sort", t.get("sort"))
    run("pivot", t.get("pivot"))
    run("limit", t.get("limit"))

    cap = int(max_rows) if max_rows else MAX_OUTPUT_ROWS
    truncated = len(fr["rows"]) > cap
    if truncated:
        fr["rows"] = fr["rows"][:cap]
        warn.append(f"result truncated to {cap} rows")
    if len(fr["columns"]) > MAX_OUTPUT_COLS:
        fr["columns"] = fr["columns"][:MAX_OUTPUT_COLS]
        fr["rows"] = [r[:MAX_OUTPUT_COLS] for r in fr["rows"]]
        warn.append(f"result truncated to {MAX_OUTPUT_COLS} columns")

    if strict and (warn or skipped):
        raise VizError("; ".join(warn + [str(s) for s in skipped]))

    return {"columns": fr["columns"], "rows": fr["rows"],
            "applied": [a for i, a in enumerate(applied) if a not in applied[:i]],
            "injected": placed, "skipped": skipped, "warnings": warn,
            "in_rows": in_rows, "out_rows": len(fr["rows"]), "truncated": truncated}


def apply_transform(columns: list, rows: list, transform: Optional[dict] = None) -> tuple:
    """Drop-in replacement for agent._apply_transform. Deterministic; never mutates."""
    out = run_transform(columns, rows, transform)
    return out["columns"], out["rows"]


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


def output_columns(columns: list, transform: Optional[dict] = None,
                   rows: Optional[list] = None) -> list:
    """FINAL column names, in order, WITHOUT touching rows. `rows` is needed only
    when transform.pivot is present (pivot columns are data)."""
    cols = _cols(columns)
    t = normalize_transform(transform)
    if not t:
        return cols

    def add(name):
        if _index(cols, name) < 0:
            cols.append(name)

    for d in t.get("derive", []):
        add(d["as"])
    for b in t.get("bin", []):
        add(b["as"])
    up = t.get("unpivot")
    if up:
        keep = [c for c in up["keep"] if _index(cols, c) >= 0]
        cols = keep + [up["name_as"], up["value_as"]]
    g = t.get("group")
    if g:
        # Same validity test _st_group applies, so introspection and execution cannot
        # disagree about which measures exist.
        by = [c for c in g["by"] if _index(cols, c) >= 0]
        measures = [m for m in g["measures"]
                    if m["agg"] == "count" or _index(cols, m["col"]) >= 0]
        if by or measures:
            others = [c for c in cols if c not in by] if g.get("keep_other_cols") else []
            cols = by + [m["as"] for m in measures] + others
    pre, post = _split_calcs(t.get("table_calc"))
    for c in pre + post:
        add(c["as"])
    p = t.get("pivot")
    if p:
        index = [c for c in p["index"] if _index(cols, c) >= 0]
        ci = _index(cols, p["columns"])
        labels = []
        if rows is not None and ci >= 0:
            # Pivot column names are data — they need a pass over the pre-pivot frame.
            try:
                fr_cols, fr_rows = apply_transform(columns, rows,
                                                   {k: v for k, v in t.items() if k != "pivot"})
                j = _index(fr_cols, p["columns"])
                if j >= 0:
                    seen = {}
                    for r in fr_rows:
                        v = _cell(r, j)
                        seen.setdefault("" if _is_null(v) else str(v), v)
                    lut = _category_lut(seen.values())
                    labels = sorted(seen, key=lambda s: _sortable(seen[s], lut))
                    cap = min(int(p.get("max_columns") or MAX_PIVOT_COLUMNS), MAX_PIVOT_COLUMNS)
                    labels = labels[:cap]
            except Exception:
                labels = []
        cols = index + labels
    return cols[:MAX_OUTPUT_COLS]


def describe_transform(transform: Optional[dict]) -> str:
    """One sentence: 'sum(revenue) by region · % of total · top 5 desc'."""
    t = normalize_transform(transform)
    if not t:
        return ""
    parts = []
    g = t.get("group")
    if g and g.get("measures"):
        ms = ", ".join(f"{m['agg']}({m['col']})" if m["agg"] != "count" else "count"
                       for m in g["measures"])
        parts.append(f"{ms} by {', '.join(g['by'])}" if g.get("by") else ms)
    elif g:
        parts.append(f"grouped by {', '.join(g['by'])}")
    for b in t.get("bin", []):
        parts.append(f"by {b['date_part']}" if b.get("date_part") else f"{b['col']} binned")
    for d in t.get("derive", []):
        parts.append(f"{d['as']} = {d['expr']}")
    if t.get("filters"):
        parts.append(f"{len(t['filters'])} filter" + ("s" if len(t["filters"]) > 1 else ""))
    if t.get("having"):
        parts.append(f"{len(t['having'])} having")
    _LABEL = {"percent_of_total": "% of total", "running_total": "running total",
              "cumulative_percent": "cumulative %", "percent_difference": "% difference",
              "moving_average": "moving average", "percent_of_max": "% of max",
              "z_score": "z-score", "dense_rank": "dense rank"}
    for c in t.get("table_calc", []):
        parts.append(_LABEL.get(c["calc"], c["calc"].replace("_", " ")))
    if t.get("unpivot"):
        parts.append("unpivoted")
    tn = t.get("top_n")
    if tn:
        parts.append(f"{tn['dir']} {tn['n']}" + (" +Other" if tn["other"]["enabled"] else ""))
    for s in t.get("sort", []):
        parts.append(f"sorted by {s['col']} {s['dir']}")
    if t.get("pivot"):
        parts.append(f"pivoted on {t['pivot']['columns']}")
    if t.get("limit"):
        parts.append(f"limit {t['limit']}")
    return " · ".join(parts)


def suggest_transform(columns: list, rows: list, spec: dict) -> dict:
    """Deterministic auto-aggregate when x repeats and every y is numeric."""
    if not isinstance(spec, dict):
        return {}
    cols = _cols(columns)
    rws = _seq(rows)
    x = spec.get("x")
    ys = [y for y in _as_str_list(spec.get("y")) if _index(cols, y) >= 0]
    xi = _index(cols, x)
    if xi < 0 or not ys or not rws:
        return {}
    seen = {_hkey(_cell(r, xi)) for r in rws}
    if len(seen) >= len(rws):
        return {}
    if any(dtype_of(rws, _index(cols, y)) != "number" for y in ys):
        return {}
    return {"group": {"by": [cols[xi]],
                      "measures": [{"col": y, "agg": "sum", "as": y} for y in ys]}}


# ══════════════════════════════════════════════════════════════════════════
# Spec lifecycle
# ══════════════════════════════════════════════════════════════════════════

def _normalize_fields(fields: Any) -> dict:
    if not isinstance(fields, dict):
        return {}
    dims, meas = [], []
    for d in fields.get("dimensions") or []:
        if isinstance(d, dict) and d.get("col"):
            dims.append({"col": str(d["col"]), "label": str(d.get("label") or d["col"]),
                         "dtype": d.get("dtype") if d.get("dtype") in
                                  ("string", "number", "date", "bool") else "string",
                         "role": d.get("role") if d.get("role") in
                                 ("category", "time", "geo", "id") else "category"})
    for m in fields.get("measures") or []:
        if isinstance(m, dict) and m.get("col"):
            meas.append({"col": str(m["col"]), "label": str(m.get("label") or m["col"]),
                         "agg": m.get("agg") if m.get("agg") in AGGS else "sum",
                         "format": str(m.get("format") or "number")})
    out = {}
    if dims:
        out["dimensions"] = dims
    if meas:
        out["measures"] = meas
    return out


def _normalize_interaction(it: Any) -> dict:
    out = copy.deepcopy(_INTERACTION_DEFAULTS)
    if not isinstance(it, dict):
        return out
    if it.get("cross_filter") in _CROSS_FILTER_MODES:
        out["cross_filter"] = it["cross_filter"]
    ab = it.get("affected_by")
    if ab == "none" or ab == "auto":
        out["affected_by"] = ab
    elif isinstance(ab, (list, tuple)):
        out["affected_by"] = _as_str_list(ab)
    if "self_highlight" in it:
        out["self_highlight"] = bool(it["self_highlight"])
    if "multi_select" in it:
        out["multi_select"] = bool(it["multi_select"])
    dr = it.get("drill")
    if isinstance(dr, dict):
        lvl = _to_num(dr.get("level"))
        out["drill"] = {"hierarchy": _as_str_list(dr.get("hierarchy")),
                        "level": max(0, int(lvl)) if lvl else 0}
    return out


def normalize_spec(spec: Optional[dict]) -> dict:
    """v1 → v2. Never raises. None or {} → a valid v2 skeleton."""
    s = spec if isinstance(spec, dict) else {}
    t = s.get("type")
    ctype = t if t in CHART_TYPES else "bar"
    x = s.get("x")
    out = {
        "v": VERSION,
        "type": ctype,
        "title": str(s.get("title") or ""),
        "x": x if isinstance(x, str) and x else (str(x) if x not in (None, "") else None),
        "y": _as_str_list(s.get("y")),
        "fields": _normalize_fields(s.get("fields")),
        "transform": normalize_transform(s.get("transform")),
        "format": {k: v for k, v in (s.get("format") or {}).items()
                   if k in FORMAT_KEYS} if isinstance(s.get("format"), dict) else {},
        "interaction": _normalize_interaction(s.get("interaction")),
    }
    return out


def _merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386: dicts merge recursively, null deletes, lists replace wholesale."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict):
            out[k] = _merge_patch(out.get(k), v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def merge_spec(current: Optional[dict], patch: Optional[dict]) -> dict:
    """LLM output is a merge patch onto the current spec — never a replacement."""
    base = current if isinstance(current, dict) else {}
    if not isinstance(patch, dict) or not patch:
        return normalize_spec(base)
    try:
        return normalize_spec(_merge_patch(base, patch))
    except Exception:
        return normalize_spec(base)


def _stage_columns(columns: list, t: dict, upto: str, rows: Optional[list] = None) -> list:
    """Columns available when `upto` is about to run."""
    partial = {k: v for k, v in t.items()
               if k in TRANSFORM_ORDER and TRANSFORM_ORDER.index(k) < TRANSFORM_ORDER.index(upto)}
    return output_columns(columns, partial, rows)


def validate_spec(spec: dict, columns: list, rows: Optional[list] = None) -> list:
    """Human-readable problems. [] means valid."""
    problems: list = []
    raw = spec if isinstance(spec, dict) else {}
    s = normalize_spec(raw)
    cols = _cols(columns)
    if raw.get("type") not in CHART_TYPES:
        problems.append(f"chart type '{raw.get('type')}' is not one of {', '.join(CHART_TYPES)}")
    t = s["transform"]

    for d in t.get("derive", []):
        avail = _stage_columns(cols, t, "derive", rows)
        try:
            compile_expr(d["expr"], avail + [x["as"] for x in t["derive"]])
        except VizError as e:
            problems.append(f"derive '{d['as']}': {e}")
    for b in t.get("bin", []):
        if _index(_stage_columns(cols, t, "bin", rows), b["col"]) < 0:
            problems.append(f"bin column '{b['col']}' does not exist")
    for key in ("filters", "having"):
        avail = _stage_columns(cols, t, key, rows)
        for p in t.get(key, []):
            if _index(avail, p["col"]) < 0:
                problems.append(f"{key} column '{p['col']}' does not exist")
    g = t.get("group")
    if g:
        avail = _stage_columns(cols, t, "group", rows)
        for c in g["by"]:
            if _index(avail, c) < 0:
                problems.append(f"group by column '{c}' does not exist")
        for m in g["measures"]:
            if m["agg"] != "count" and _index(avail, m["col"]) < 0:
                problems.append(f"group measure column '{m['col']}' does not exist")
    avail = _stage_columns(cols, t, "table_calc", rows)
    for c in t.get("table_calc", []):
        if _index(avail, c["col"]) < 0:
            problems.append(f"table_calc column '{c['col']}' does not exist")
    tn = t.get("top_n")
    if tn and tn.get("by") and _index(_stage_columns(cols, t, "top_n", rows), tn["by"]) < 0:
        problems.append(f"top_n column '{tn['by']}' does not exist")

    final = output_columns(cols, t, rows)
    if s["x"] and _index(final, s["x"]) < 0:
        problems.append(f'x column "{s["x"]}" is not produced by this transform')
    for y in s["y"]:
        if _index(final, y) < 0:
            problems.append(f'y column "{y}" is not produced by this transform')
    for c in (s["format"].get("number") or {}):
        if c != "*" and _index(final, c) < 0:
            problems.append(f'format.number references unknown column "{c}"')
    raw_format = raw.get("format")
    for k in (raw_format if isinstance(raw_format, dict) else {}):
        if k not in FORMAT_KEYS:
            problems.append(f"format.{k} is not a known key")
    return problems


def _sanitize_transform(t: dict, columns: list, rows: Optional[list], warn: list) -> dict:
    """Drop every stage/key that references a column unavailable at that stage."""
    out: dict = {}
    avail = _cols(columns)

    keep = []
    for d in t.get("derive", []):
        try:
            compile_expr(d["expr"], avail)
        except VizError as e:
            warn.append(f"derive '{d['as']}': {e} — dropped")
            continue
        keep.append(d)
        if _index(avail, d["as"]) < 0:
            avail.append(d["as"])
    if keep:
        out["derive"] = keep

    keep = []
    for b in t.get("bin", []):
        if _index(avail, b["col"]) < 0:
            warn.append(f"bin: column '{b['col']}' does not exist — dropped")
            continue
        keep.append(b)
        if _index(avail, b["as"]) < 0:
            avail.append(b["as"])
    if keep:
        out["bin"] = keep

    preds = _valid_predicates(avail, t.get("filters"), warn, "filters")
    if preds:
        out["filters"] = preds

    up = t.get("unpivot")
    if up:
        keep_cols = [c for c in up["keep"] if _index(avail, c) >= 0]
        cols_sel = None if up["cols"] is None else [c for c in up["cols"] if _index(avail, c) >= 0]
        if cols_sel is not None and not cols_sel:
            warn.append("unpivot: no valid value columns — dropped")
        else:
            out["unpivot"] = {**up, "keep": keep_cols, "cols": cols_sel}
            avail = keep_cols + [up["name_as"], up["value_as"]]

    g = t.get("group")
    if g:
        by = [c for c in g["by"] if _index(avail, c) >= 0]
        for c in g["by"]:
            if _index(avail, c) < 0:
                warn.append(f"group: column '{c}' does not exist — dropped")
        measures = []
        for m in g["measures"]:
            if m["agg"] != "count" and _index(avail, m["col"]) < 0:
                warn.append(f"group: measure '{m['col']}' does not exist — dropped")
                continue
            measures.append(m)
        if g["by"] and not by:
            # Every requested dimension is missing — collapsing to one grand-total row
            # would be a far bigger change than keeping the current grain.
            warn.append("group: no valid group-by column — stage dropped")
        elif by or measures:
            others = [c for c in avail if c not in by] if g.get("keep_other_cols") else []
            out["group"] = {"by": by, "measures": measures,
                            "keep_other_cols": bool(g.get("keep_other_cols"))}
            avail = by + [m["as"] for m in measures] + others

    preds = _valid_predicates(avail, t.get("having"), warn, "having")
    if preds:
        out["having"] = preds

    keep = []
    for c in t.get("table_calc", []):
        if _index(avail, c["col"]) < 0:
            warn.append(f"table_calc: column '{c['col']}' does not exist — dropped")
            continue
        part = [p for p in c["partition_by"] if _index(avail, p) >= 0]
        order = c["order_by"] if (c["order_by"] and _index(avail, c["order_by"]) >= 0) else None
        keep.append({**c, "partition_by": part, "order_by": order})
        if _index(avail, c["as"]) < 0:
            avail.append(c["as"])
    if keep:
        out["table_calc"] = keep

    tn = t.get("top_n")
    if tn:
        by = tn["by"] if (tn["by"] and _index(avail, tn["by"]) >= 0) else None
        if tn["by"] and by is None:
            warn.append(f"top_n: column '{tn['by']}' does not exist — ranked on the first measure")
        within = [c for c in tn["within"] if _index(avail, c) >= 0]
        other = dict(tn["other"])
        if other.get("dim") and _index(avail, other["dim"]) < 0:
            other["dim"] = None
        out["top_n"] = {**tn, "by": by, "within": within, "other": other}

    keep = []
    for s in t.get("sort", []):
        if _index(avail, s["col"]) < 0:
            warn.append(f"sort: column '{s['col']}' does not exist — dropped")
            continue
        keep.append(s)
    if keep:
        out["sort"] = keep

    p = t.get("pivot")
    if p:
        if _index(avail, p["columns"]) < 0 or _index(avail, p["values"]) < 0:
            warn.append("pivot: columns/values do not exist — dropped")
        else:
            out["pivot"] = {**p, "index": [c for c in p["index"] if _index(avail, c) >= 0]}

    if t.get("limit"):
        out["limit"] = t["limit"]
    return out


def _prune_format(fmt: dict, final: list, warn: list) -> dict:
    out = {k: v for k, v in fmt.items() if k in FORMAT_KEYS}
    for k in fmt:
        if k not in FORMAT_KEYS:
            warn.append(f"format.{k} is not a known key — dropped")
    for key in ("number", "date", "series"):
        block = out.get(key)
        if isinstance(block, dict):
            kept = {c: v for c, v in block.items() if c == "*" or _index(final, c) >= 0}
            for c in block:
                if c not in kept:
                    warn.append(f"format.{key}['{c}'] references a column this chart "
                                f"does not produce — dropped")
            out[key] = kept
    lab = out.get("labels")
    if isinstance(lab, dict) and lab.get("from_col") and _index(final, lab["from_col"]) < 0:
        warn.append(f"format.labels.from_col '{lab['from_col']}' does not exist — dropped")
        out["labels"] = {**lab, "from_col": None}
    cc = out.get("conditional_colors")
    if isinstance(cc, dict):
        if cc.get("target") and _index(final, cc["target"]) < 0:
            warn.append(f"format.conditional_colors.target '{cc['target']}' does not exist — dropped")
            cc = None
        elif cc.get("by") and _index(final, cc["by"]) < 0:
            cc = {**cc, "by": None}
        if cc is None:
            out.pop("conditional_colors", None)
        else:
            out["conditional_colors"] = cc
    return out


def sanitize_spec(spec: Optional[dict], columns: list, rows: Optional[list] = None) -> tuple:
    """normalize_spec, then drop everything that cannot resolve, and backfill x/y.
    NEVER raises — a hallucinating LLM must degrade, not 500."""
    warn: list = []
    try:
        raw = spec if isinstance(spec, dict) else {}
        for k in raw:
            if k not in SPEC_KEYS:
                warn.append(f"'{k}' is not a chart spec key — dropped")
        s = normalize_spec(raw)
        cols = _cols(columns)
        s["transform"] = _sanitize_transform(s["transform"], cols, rows, warn)

        if raw.get("type") not in CHART_TYPES and raw.get("type") is not None:
            warn.append(f"unknown chart type '{raw.get('type')}' — using bar")

        final = output_columns(cols, s["transform"], rows)
        frame_rows = rows
        if rows is not None and s["transform"]:
            try:
                _, frame_rows = apply_transform(cols, rows, s["transform"])
            except Exception:
                frame_rows = rows

        numeric = [c for c in final
                   if dtype_of(frame_rows, _index(final, c)) == "number"] if frame_rows else []
        # Backfill prefers real measures over bare numerics, so an id column never
        # becomes the plotted value.
        inferred = infer_fields(final, frame_rows)
        measures = [m["col"] for m in inferred["measures"]]
        dims = [d["col"] for d in inferred["dimensions"]]
        # A numeric dimension (order_id, zip) is a poor category axis — try labels first.
        non_numeric = [c for c in dims if c not in numeric] or dims or \
            [c for c in final if c not in numeric]

        if s["x"] and _index(final, s["x"]) < 0:
            warn.append(f"x column '{s['x']}' is not produced by this transform — replaced")
            s["x"] = None
        ys = [y for y in s["y"] if _index(final, y) >= 0]
        if len(ys) != len(s["y"]):
            for y in s["y"]:
                if _index(final, y) < 0:
                    warn.append(f"y column '{y}' is not produced by this transform — dropped")
        s["y"] = ys
        if not s["x"] and final:
            s["x"] = (non_numeric or final)[0]
        if not s["y"]:
            pick = [c for c in (measures or numeric or final) if c != s["x"]]
            s["y"] = pick[:1]
        if frame_rows and not numeric and s["type"] not in ("table", "kpi"):
            warn.append("no numeric column to plot — falling back to a table")
            s["type"] = "table"
        # ChartView self-sorts funnels (ChartView.jsx:385); a server sort would fight it.
        if s["type"] == "funnel":
            s["transform"].pop("sort", None)

        # Re-point the top_n ranking at the first measure when the LLM omitted `by`.
        tn = s["transform"].get("top_n")
        if tn and not tn.get("by"):
            tn["by"] = (s["y"] or numeric or [None])[0]

        s["fields"] = {k: [f for f in v if _index(cols, f["col"]) >= 0 or _index(final, f["col"]) >= 0]
                       for k, v in s["fields"].items()}
        s["fields"] = {k: v for k, v in s["fields"].items() if v}
        s["format"] = _prune_format(s["format"], final, warn) if s["format"] else {}
        return s, warn
    except Exception as e:                       # totality is the contract
        return normalize_spec(spec), warn + [f"spec could not be fully validated: {type(e).__name__}"]


# ══════════════════════════════════════════════════════════════════════════
# Prompt fragments — interpolated verbatim by the lead so schema/prompt cannot drift
# ══════════════════════════════════════════════════════════════════════════

SPEC_PROMPT: str = """\
THE SPEC
{"type","title","x","y",                      — what to draw
 "fields":{"dimensions":[],"measures":[]},    — WHAT THINGS ARE (rarely needs editing)
 "transform":{...},                           — WHAT THE NUMBERS ARE (changes the data)
 "format":{...},                              — HOW IT LOOKS (never changes the data)
 "interaction":{...}}                         — clicking behaviour

type ∈ """ + " ".join(CHART_TYPES) + """
x  = the dimension on the category axis.   y = the measure column(s), always a list.

DIMENSIONS vs MEASURES. A dimension slices ("by region", "over time"); a measure aggregates
("revenue", "count of orders"). x is always a dimension, y is always measures. If asked to
plot a dimension, you are being asked to COUNT it — emit transform.group with agg "count".

TRANSFORM — data. Stages ALWAYS run in this fixed order, so never worry about ordering:
  """ + " → ".join(TRANSFORM_ORDER) + """

  derive     [{"as","expr","kind":"measure|dimension","agg"}]
             expr: + - * / % ** , comparisons, and/or/not, `A if C else B`, and the functions
             """ + " ".join(sorted(EXPR_FUNCS)) + """.
             Column names only — no attributes, no subscripts, no lambdas.
  bin        [{"col","as","date_part":"hour|day|week|month|quarter|year|weekday"}] for dates,
             or [{"col","as","size"|"count"}] for numbers.
  filters    [{"col","op","value"|"values"|"lo"+"hi"}]  op ∈ """ + " ".join(OPS) + """.
             AND-combined. For OR within one column use "in". ALWAYS pre-aggregate.
  unpivot    {"keep":[],"cols":[],"name_as","value_as"}   wide → long.
  group      {"by":[dims],"measures":[{"col","agg","as"}]}  agg ∈ """ + " ".join(AGGS) + """.
             Emit group when the rows are finer-grained than the chart needs — duplicate x
             values plus a numeric y almost always means you want a group.
  having     same predicate shape as filters, applied AFTER group. To filter on an aggregated
             measure ("regions over 1M") use having, never filters.
  table_calc [{"col","calc","as","partition_by":[],"order_by":null,"dir","window","stage"}]
             calc ∈ """ + " ".join(TABLE_CALCS) + """
             These APPEND a column; put its name in "y" or in format.labels.from_col to use it.
             percent_* emit a FRACTION (0.42) — always pair with a format.number entry of
             style "percent". "stage" is "pre_top_n" (default — percentages are of the GRAND
             total) or "post_top_n" (of the visible rows only).
  top_n      {"by","n","dir":"top|bottom","within":[],
              "other":{"enabled":bool,"label":"Other","dim":null,"agg":"sum"}}
             Set other.enabled = true when the tail matters ("share of total", "breakdown");
             leave it false for a pure ranking ("top 5 by revenue").
  sort       [{"col","dir","nulls"}] — the ONE sort. This is what "sorted descending" means.
  pivot      {"index":[],"columns","values","agg"} — long → wide. Use sparingly.
  limit      <int>

FORMAT — appearance. Never changes numbers.
  number {"<col>|*":{"style":"number|currency|percent|compact|scientific|bytes","currency",
                     "decimals","compact","prefix","suffix","scale","negative"}}
         style "percent" MULTIPLIES BY 100 and appends "%".
  date   {"<col>":{"preset":"year|quarter|month|day|datetime|time","pattern"}}
  labels {"show","position":"auto|top|inside|outside|end","series":null,"from_col":null,
          "format":null,"max_points":30}
         ← "from_col" labels a mark with a DIFFERENT column, e.g. bars of revenue labelled
           with their % of total. This is how you show two numbers on one bar.
  axes   {"x":{"title","label_angle","grid","min","max","reverse","type"},
          "y":{"title","grid","min","max","type","zero"},"y2":{...}}
  legend {"show":"auto|true|false","position":"top|bottom|left|right"}
  palette{"mode":"theme|custom|by_key","colors":[],"by_key":{},"other_color"}
  series {"<measure>":{"type":"bar|line|area","axis":"y|y2","color","dash","width","stack"}}
  reference_lines [{"axis":"y","mode":"value|average|median|min|max|percentile","value","of",
                    "to","label","label_position","dash","color"}]
  conditional_colors {"target","by","mode":"rules|scale",
                      "rules":[{"op","value","color","label"}],"default","scale":{...}}
  annotations [{"x","y","text","arrow"}]

TEN RULES YOU MUST FOLLOW
 1. Only reference columns from "columns", from "current_columns", or names YOU create earlier
    in the fixed order (derive.as, bin.as, group measures .as, table_calc.as). Never invent a
    column. x and y must name columns in the FINAL frame, after the transform.
 2. "sorted descending" ⇒ transform.sort. There is exactly one sort. Never emit format.sort.
 3. A reference line's value is in the UNITS OF THE AXIS IT SITS ON. If the target is in
    currency, the measure plotted on that axis must be that currency measure. If the user also
    wants a percentage, plot the currency measure on y and put the percentage in
    format.labels.from_col. NEVER put a currency target on a percent axis.
 4. NEVER emit format.series[*].axis = "y2" unless the user literally asks for a second /
    secondary / dual axis. Two measures of different scale ⇒ type "combo" on one axis, or say
    in the note that two panels would read better.
 5. Percent columns from table_calc are fractions. Always pair them with a format.number entry
    of style "percent".
 6. transform when the ask changes the NUMBERS ("top 5", "% of total", "running total",
    "exclude returns", "by month", "average"). format when it changes the LOOK ("labels",
    "currency", "target line", "red when negative", "legend on the right", "axis title").
    Never fake a data change with format.
 7. Keep the title in sync with what is actually plotted. Retitle when the measure changes.
 8. More than 8 categories ⇒ top_n with other.enabled true, or switch to hbar. Never ask for
    a ninth colour.
 9. Do not emit "fields" unless the user is redefining what a column MEANS ("treat order_id
    as a dimension", "average instead of sum", "region drills into state then city").
10. Edits are LOCAL to this visual. They never modify source tables and never rewrite the SQL.
    If you cannot satisfy part of the instruction, do the rest and say what you skipped in
    "note". Never invent keys, ops or functions.

WORKED EXAMPLE — the full sentence
instruction: "show top 5 regions by revenue as % of total with data labels and a target line
              at 100k, sorted descending"
columns: ["order_id","region","revenue","cost","order_date"]
{"chart":{
  "type":"bar","title":"Top 5 regions by revenue","x":"region","y":["revenue"],
  "transform":{
    "group":{"by":["region"],"measures":[{"col":"revenue","agg":"sum","as":"revenue"}]},
    "table_calc":[{"col":"revenue","calc":"percent_of_total",
                   "as":"revenue_pct_of_total","stage":"pre_top_n"}],
    "top_n":{"by":"revenue","n":5,"dir":"top","other":{"enabled":false}},
    "sort":[{"col":"revenue","dir":"desc"}]},
  "format":{
    "number":{"revenue":{"style":"currency","currency":"USD","compact":true},
              "revenue_pct_of_total":{"style":"percent","decimals":1}},
    "labels":{"show":true,"position":"outside","from_col":"revenue_pct_of_total"},
    "reference_lines":[{"axis":"y","mode":"value","value":100000,"label":"Target",
                        "label_position":"end","dash":"dashed"}],
    "axes":{"y":{"title":"Revenue"}}}},
 "note":"Top 5 regions by revenue, each bar labelled with its share of total, against a $100K target, sorted descending."}

Why this shape: the bars carry revenue, so the 100k target line is unit-coherent on the same
axis; the % of total rides along as the data label via from_col; percent_of_total is computed
pre_top_n, so each share is of the grand total, not of the visible five.

SECOND EXAMPLE — retitle only (the merge patch doing its job)
instruction: "call it Q3 performance"
{"chart":{"title":"Q3 performance"},"note":"Renamed the chart."}

THIRD EXAMPLE — breakdown with a tail
instruction: "break revenue down by product, roll the small ones into Other, as a donut"
{"chart":{"type":"donut","title":"Revenue by product","x":"product","y":["revenue"],
  "transform":{"group":{"by":["product"],"measures":[{"col":"revenue","agg":"sum","as":"revenue"}]},
               "top_n":{"by":"revenue","n":7,"dir":"top",
                        "other":{"enabled":true,"label":"Other","dim":"product","agg":"sum"}},
               "sort":[{"col":"revenue","dir":"desc"}]},
  "format":{"number":{"revenue":{"style":"currency","compact":true}},
            "labels":{"show":true,"position":"outside"},
            "legend":{"show":true,"position":"right"}}},
 "note":"Top 7 products by revenue as a donut, remainder grouped into Other."}

FOURTH EXAMPLE — relative time + bucketing
instruction: "last 90 days, revenue by month, running total"
{"chart":{"type":"line","title":"Cumulative revenue by month","x":"month","y":["revenue_running_total"],
  "transform":{"bin":[{"col":"order_date","as":"month","date_part":"month"}],
               "filters":[{"col":"order_date","op":"last_n","value":90,"unit":"day"}],
               "group":{"by":["month"],"measures":[{"col":"revenue","agg":"sum","as":"revenue"}]},
               "table_calc":[{"col":"revenue","calc":"running_total","as":"revenue_running_total",
                              "order_by":"month","dir":"asc"}],
               "sort":[{"col":"month","dir":"asc"}]},
  "format":{"number":{"*":{"style":"currency","compact":true}},"axes":{"y":{"title":"Revenue"}}}},
 "note":"Last 90 days bucketed by month, showing cumulative revenue."}
"""


def _enum(values):
    return {"type": "string", "enum": list(values)}


_PREDICATE_SCHEMA = {
    "type": "object",
    "required": ["col"],
    "properties": {"col": {"type": "string"}, "op": _enum(OPS), "value": {},
                   "values": {"type": "array"}, "lo": {}, "hi": {},
                   "unit": _enum(("day", "week", "month", "quarter", "year")),
                   "origin": _enum(("spec", "slicer", "cross")),
                   "source_tile": {"type": ["string", "null"]}},
}

# JSON-Schema (draft-07 subset) of the whole chart spec — every v2 key is optional.
SPEC_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Studio chart spec v2",
    "type": "object",
    "required": ["type"],
    "additionalProperties": False,
    "properties": {
        "v": {"type": "integer"},
        "type": _enum(CHART_TYPES),
        "title": {"type": "string"},
        "x": {"type": ["string", "null"]},
        "y": {"type": "array", "items": {"type": "string"}},
        "fields": {
            "type": "object",
            "properties": {
                "dimensions": {"type": "array", "items": {
                    "type": "object", "required": ["col"],
                    "properties": {"col": {"type": "string"}, "label": {"type": "string"},
                                   "dtype": _enum(("string", "number", "date", "bool")),
                                   "role": _enum(("category", "time", "geo", "id"))}}},
                "measures": {"type": "array", "items": {
                    "type": "object", "required": ["col"],
                    "properties": {"col": {"type": "string"}, "label": {"type": "string"},
                                   "agg": _enum(AGGS), "format": {"type": "string"}}}},
            },
        },
        "transform": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "derive": {"type": "array", "items": {
                    "type": "object", "required": ["as", "expr"],
                    "properties": {"as": {"type": "string"}, "expr": {"type": "string"},
                                   "kind": _enum(("measure", "dimension")), "agg": _enum(AGGS)}}},
                "bin": {"type": "array", "items": {
                    "type": "object", "required": ["col", "as"],
                    "properties": {"col": {"type": "string"}, "as": {"type": "string"},
                                   "date_part": _enum(DATE_PARTS),
                                   "size": {"type": ["number", "null"]},
                                   "count": {"type": ["integer", "null"]},
                                   "labels": _enum(("range", "lower", "mid"))}}},
                "filters": {"type": "array", "items": _PREDICATE_SCHEMA},
                "unpivot": {"type": "object", "properties": {
                    "keep": {"type": "array", "items": {"type": "string"}},
                    "cols": {"type": ["array", "null"], "items": {"type": "string"}},
                    "name_as": {"type": "string"}, "value_as": {"type": "string"},
                    "drop_nulls": {"type": "boolean"}}},
                "group": {"type": "object", "properties": {
                    "by": {"type": "array", "items": {"type": "string"}},
                    "measures": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"col": {"type": "string"}, "agg": _enum(AGGS),
                                       "as": {"type": "string"}}}},
                    "keep_other_cols": {"type": "boolean"}}},
                "having": {"type": "array", "items": _PREDICATE_SCHEMA},
                "table_calc": {"type": "array", "items": {
                    "type": "object", "required": ["col", "calc"],
                    "properties": {"col": {"type": "string"}, "calc": _enum(TABLE_CALCS),
                                   "as": {"type": "string"},
                                   "partition_by": {"type": "array", "items": {"type": "string"}},
                                   "order_by": {"type": ["string", "null"]},
                                   "dir": _enum(("asc", "desc")), "window": {"type": "integer"},
                                   "stage": _enum(("pre_top_n", "post_top_n"))}}},
                "top_n": {"type": ["object", "integer", "null"], "properties": {
                    "by": {"type": ["string", "null"]}, "n": {"type": "integer"},
                    "dir": _enum(("top", "bottom")),
                    "within": {"type": "array", "items": {"type": "string"}},
                    "other": {"type": "object", "properties": {
                        "enabled": {"type": "boolean"}, "label": {"type": "string"},
                        "dim": {"type": ["string", "null"]},
                        "agg": _enum(("sum", "avg", "min", "max"))}}}},
                "sort": {"type": "array", "items": {
                    "type": "object", "required": ["col"],
                    "properties": {"col": {"type": "string"}, "dir": _enum(("asc", "desc")),
                                   "nulls": _enum(("first", "last"))}}},
                "pivot": {"type": "object", "properties": {
                    "index": {"type": "array", "items": {"type": "string"}},
                    "columns": {"type": "string"}, "values": {"type": "string"},
                    "agg": _enum(AGGS), "fill": {},
                    "max_columns": {"type": "integer"}}},
                "limit": {"type": ["integer", "null"]},
            },
        },
        "format": {"type": "object", "additionalProperties": False,
                   "properties": {k: {} for k in FORMAT_KEYS}},
        "interaction": {"type": "object", "properties": {
            "cross_filter": _enum(_CROSS_FILTER_MODES),
            "affected_by": {}, "self_highlight": {"type": "boolean"},
            "multi_select": {"type": "boolean"},
            "drill": {"type": "object", "properties": {
                "hierarchy": {"type": "array", "items": {"type": "string"}},
                "level": {"type": "integer"}}}}},
    },
}


def llm_contract() -> dict:
    """The LLM-facing contract, generated from the vocabulary above: the prompt text
    agent.py interpolates into its system prompts and the JSON-Schema it binds
    structured output to. The schema is a copy — a caller may edit its own view of it
    without redefining the spec for everyone else."""
    return {"version": VERSION, "prompt": SPEC_PROMPT, "schema": copy.deepcopy(SPEC_SCHEMA)}


__all__ = [
    # vocabulary
    "VERSION", "VizError", "CHART_TYPES", "TRANSFORM_ORDER", "OPS", "AGGS",
    "TABLE_CALCS", "DATE_PARTS", "CATEGORY_ORDERS", "SPEC_KEYS", "FORMAT_KEYS",
    "EXPR_FUNCS", "MAX_OUTPUT_ROWS", "MAX_OUTPUT_COLS", "MAX_PIVOT_COLUMNS",
    "MAX_GROUPS", "MAX_EXPR_CHARS", "MAX_EXPR_NODES", "MAX_POW_EXPONENT",
    "MAX_EXPR_STR_CHARS", "MAX_DERIVE_SECONDS",
    # engine
    "compile_expr", "coerce_compare", "match_row", "apply_filters",
    "normalize_transform", "apply_stage", "run_transform", "apply_transform",
    # introspection
    "dtype_of", "infer_fields", "distinct_values", "field_catalog",
    "output_columns", "describe_transform", "suggest_transform",
    # spec
    "normalize_spec", "merge_spec", "validate_spec", "sanitize_spec",
    # LLM contract
    "SPEC_PROMPT", "SPEC_SCHEMA", "llm_contract",
]
