"""Sandboxed expression evaluator for `derive` — ast whitelist, never eval/exec.

An expression is parsed with `ast.parse(mode="eval")`, walked once against a
whitelist of node types (constants, column names, arithmetic, comparisons,
boolean logic, the ternary, list literals and bare calls to EXPR_FUNCS), and
compiled into a closure over a row. Anything not on the list — attributes,
subscripts, lambdas, comprehensions, keyword arguments, starred args — raises
VizError at compile time, so a hostile expression fails before it touches data.

Three resource caps defend the process, not just correctness: the source
length and node budget bound the INPUT, MAX_POW_EXPONENT keeps ** from building
huge ints, and MAX_EXPR_STR_CHARS bounds every string an expression PRODUCES
(nesting `replace`/`+` can amplify a tiny source into gigabytes). The string
cap raises rather than degrading to null: the memory has already been asked
for, so silently continuing would be the wrong kind of totality.

Backticked names (`Net Sales`) let a column with spaces or punctuation be
referenced; they are swapped for placeholders before parsing.
"""
from __future__ import annotations

import ast
import math
import re
from typing import Any, Callable

from .frame import _cell, _cols, _is_null, _parse_date, _to_num, coerce_compare
from .vocab import (CATEGORY_ORDERS, MAX_EXPR_CHARS, MAX_EXPR_NODES, MAX_EXPR_STR_CHARS,
                    MAX_POW_EXPONENT, VizError)

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
