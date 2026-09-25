"""Equivalence-preserving SQL rewrites, driven by what the plan said.

app/plans.py asks the engine what it WOULD do with a query (EXPLAIN), reads the
plan, and when the plan says the engine will scan more than it needs, this
module rewrites the SQL so it does not. Three rules, each one a transformation
that is true for every row of every table — not a heuristic, not a model:

  sargable_prefix     substr(dt,1,7) = '2023-03'  ->  dt >= '2023-03' AND dt < '2023-04'
  sargable_date_part  year(order_ts) = 2023       ->  order_ts >= TIMESTAMP '2023-01-01 00:00:00'
                                                      AND order_ts < TIMESTAMP '2024-01-01 00:00:00'
  push_filter_into_cte  WITH c AS (SELECT ... FROM big) SELECT ... FROM c WHERE c.dt = '2023-03'
                        -> the same predicate also lands inside c

The first two exist because a column wrapped in a function is invisible to
every pruning mechanism a columnar engine has: Spark will not turn
`substr(dt,1,7)` into a partition filter, and neither Spark nor Trino can use
Parquet/ORC min-max statistics to skip a row group when the predicate is on
`year(ts)` rather than on `ts`. The rewritten form is a plain range on the
column itself, which is exactly the shape partition pruning and predicate
pushdown look for. The third is textbook predicate pushdown, for the engines
and shapes where the optimizer does not do it on its own.

WHY RULES AND NOT A MODEL. A model that rewrites SQL can silently change an
answer, and a wrong number that arrives fast is worse than a slow right one.
Every rule here is a syntactic transformation whose equivalence argument is
written down next to it, each rule REFUSES when it cannot prove its own
precondition (unknown column type, unknown collation, a window function in the
way), and plans.py re-validates, re-explains and re-runs the result before
anything is offered to a user. The model-written rewrite lives in plans.py and
passes through the SAME verification; it is the fallback, not the mechanism.

Edits are spliced at TOKEN boundaries using queryguard.lex(), the one lexer
that also decides what the guard sees. A second tokenizer here would be a
second reading of the same string, which is the bug class queryguard exists to
prevent — so there is only ever one.
"""
import collections
import datetime
import re

from . import queryguard
from .queryguard import QueryRejected

#: A rewrite that was applied: which rule, the SQL after it, and the sentence
#: the UI shows ("dt is compared through substr(), which hides it from
#: partition pruning").
Rewrite = collections.namedtuple("Rewrite", "rule sql why")

#: Dialects whose string comparison is BYTE ORDER, which is what makes
#: `substr(x,1,n) = 'lit'` equivalent to `x >= 'lit' AND x < succ('lit')`.
#: PostgreSQL and Snowflake are deliberately absent: a libc/ICU collation
#: orders 'a' < 'B' and the range would not be the same set of rows (Postgres
#: only makes this same rewrite itself for text_pattern_ops indexes). An
#: unknown dialect is absent too — the rule fails closed.
BINARY_COLLATION = frozenset({"databricks", "spark", "trino", "hive", "duckdb",
                              "sqlite", "demo", "bigquery"})

#: Column types the date rules may touch, read from the connector's own
#: get_schema(). A type carrying a time zone is excluded: `year(ts)` is then
#: evaluated in the session zone while a bare TIMESTAMP literal may not be, and
#: "probably the same zone" is not an equivalence proof.
_DATE_TYPE = re.compile(r"^\s*date\s*$", re.IGNORECASE)
_TIMESTAMP_TYPE = re.compile(r"^\s*(timestamp|timestamp_ntz|datetime)\s*(\(\d+\))?\s*$",
                             re.IGNORECASE)

#: Set operators / clauses that make a block's output rows something other than
#: "its FROM, filtered and projected" — so a predicate on the output is not the
#: same predicate on the input. push_filter_into_cte refuses on any of them.
_NOT_PUSHABLE = frozenset({
    "group", "having", "distinct", "limit", "offset", "fetch", "qualify",
    "union", "intersect", "except", "over", "sample", "tablesample",
})

#: Aggregates in a block's select list mean the block aggregates even without a
#: GROUP BY (`SELECT count(*) AS n FROM t`), and a predicate on `n` is a HAVING,
#: not a WHERE. Listed rather than inferred: a wrong guess here is a wrong answer.
_AGGREGATES = frozenset({
    "sum", "count", "avg", "min", "max", "stddev", "stddev_pop", "stddev_samp",
    "variance", "var_pop", "var_samp", "median", "percentile", "percentile_cont",
    "percentile_disc", "approx_count_distinct", "approx_percentile",
    "collect_list", "collect_set", "array_agg", "string_agg", "listagg",
    "group_concat", "corr", "covar_pop", "covar_samp", "any_value", "bool_and",
    "bool_or", "first", "last", "first_value", "last_value",
})

_CLAUSE_END = frozenset({"group", "order", "having", "limit", "offset", "fetch",
                         "window", "qualify", "union", "intersect", "except"})

#: Bare words inside a predicate that are SQL, not column names. Without this
#: list the `AND` in `(dt >= 'a' AND dt < 'b')` was read as a column, failed to
#: map, and the whole conjunct was refused. Anything NOT listed here is still
#: treated as a column and must map, so an unknown word fails the rule closed
#: rather than pushing a predicate the block cannot evaluate.
_NON_COLUMN_WORDS = frozenset({
    "and", "or", "not", "is", "null", "true", "false", "unknown", "between",
    "in", "like", "ilike", "rlike", "similar", "escape", "case", "when",
    "then", "else", "end", "cast", "as", "distinct", "exists", "any", "all",
    "some", "collate", "interval", "asc", "desc", "from", "for",
})


# ── Splicing ────────────────────────────────────────────────────────────

def _splice(sql, edits):
    """Apply [(start, end, text), ...] to `sql`. Edits may arrive in any order
    and must not overlap — an overlap means two rules claimed the same tokens,
    which would silently drop one of them, so it raises instead."""
    edits = sorted(edits)
    for (a, b), (c, _d, _t) in zip([(a, b) for a, b, _ in edits], edits[1:]):
        if c < b:
            raise ValueError("overlapping rewrite edits")
    out, at = [], 0
    for start, end, text in edits:
        out.append(sql[at:start])
        out.append(text)
        at = end
    out.append(sql[at:])
    return "".join(out)


def _sql_str(value):
    return "'" + str(value).replace("'", "''") + "'"


def _str_value(tok):
    """The text inside a ('str', "'a''b'") token, with doubled quotes undone."""
    return tok[1][1:-1].replace("''", "'")


# ── Reading small shapes out of the token stream ────────────────────────

def _is_word(tok, *words):
    return tok[0] == "word" and tok[1].lower() in words


def _col_ref(toks, i):
    """`a.b.c` starting at `i` -> (["a","b","c"], index after), or (None, i).

    Only word/ident parts separated by dots: anything else (a function call, a
    literal, an expression) is not a column and the caller must refuse.
    """
    n, parts = len(toks), []
    while i < n and toks[i][0] in ("word", "ident"):
        parts.append(toks[i][1])
        i += 1
        if i + 1 < n and toks[i] == ("punct", ".") and toks[i + 1][0] in ("word", "ident"):
            i += 1
            continue
        break
    if not parts:
        return None, i
    # A column reference is never immediately followed by '(' — that is a call.
    if i < len(toks) and toks[i] == ("punct", "("):
        return None, i
    return parts, i


def _close_paren(toks, i):
    """Index of the ')' matching the '(' at `i`, or None."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
            if depth == 0:
                return j
    return None


def _split_args(toks, open_i, close_i):
    """Argument token ranges of a call whose parens are at (open_i, close_i)."""
    args, depth, start = [], 0, open_i + 1
    for j in range(open_i + 1, close_i):
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
        elif toks[j] == ("punct", ",") and depth == 0:
            args.append((start, j))
            start = j + 1
    args.append((start, close_i))
    return [a for a in args if a[0] < a[1]]


def _comparison(toks, i):
    """A comparison operator at `i` -> (op, index after). `>=` arrives as two
    punct tokens, so it is reassembled here rather than by every caller."""
    if i >= len(toks) or toks[i][0] != "punct":
        return None, i
    first = toks[i][1]
    if first in ("<", ">") and i + 1 < len(toks) and toks[i + 1] == ("punct", "="):
        return first + "=", i + 2
    if first in ("=", "<", ">"):
        return first, i + 1
    return None, i


# ── Rule 1: a prefix comparison is a range ──────────────────────────────

def _upper_bound(prefix):
    """The smallest string greater than every string starting with `prefix`.

    Increment the last character: every string with this prefix is >= prefix
    and < prefix-with-last-character-incremented, under BYTE ordering (hence
    BINARY_COLLATION). Returns None when the result would not be plain
    printable ASCII — the rewrite has to stay a readable literal a person can
    check, and a non-ASCII bound is exactly where a collation surprise hides.
    """
    if not prefix or any(not (" " <= ch <= "~") for ch in prefix):
        return None
    nxt = chr(ord(prefix[-1]) + 1)
    if not (" " <= nxt <= "~"):
        return None
    return prefix[:-1] + nxt


def _prefix_call(toks, i):
    """`substr(col,1,N)` / `substring(col,1,N)` / `left(col,N)` at `i`
    -> (column parts, N, index after the call), or None."""
    if not _is_word(toks[i], "substr", "substring", "left"):
        return None
    if i + 1 >= len(toks) or toks[i + 1] != ("punct", "("):
        return None
    close = _close_paren(toks, i + 1)
    if close is None:
        return None
    args = _split_args(toks, i + 1, close)
    fn = toks[i][1].lower()
    if fn == "left":
        if len(args) != 2:
            return None
        col_range, len_range = args
    else:
        if len(args) != 3:
            return None
        col_range, start_range, len_range = args
        # 1-indexed in every dialect here; any other start is not a prefix.
        if start_range[1] - start_range[0] != 1 or toks[start_range[0]] != ("num", "1"):
            return None
    parts, after = _col_ref(toks, col_range[0])
    if parts is None or after != col_range[1]:
        return None
    if len_range[1] - len_range[0] != 1 or toks[len_range[0]][0] != "num":
        return None
    try:
        width = int(toks[len_range[0]][1])
    except ValueError:
        return None
    return parts, width, close + 1


def _rule_sargable_prefix(toks, spans, ctx):
    """substr(dt,1,7) = '2023-03'  /  dt LIKE '2023-03%'  ->  a range on dt.

    Equivalence (byte-ordered strings only, see BINARY_COLLATION): for any x,
    `substr(x,1,n) = p` with len(p) = n holds exactly when x starts with p,
    which holds exactly when `p <= x < succ(p)`. NULL propagates identically on
    both sides, and the replacement is always parenthesised so a surrounding
    NOT/OR cannot re-associate it.
    """
    if ctx.dialect not in BINARY_COLLATION:
        return []
    edits, n = [], len(toks)
    for i in range(n):
        call = _prefix_call(toks, i)
        if call:
            parts, width, after = call
            op, after = _comparison(toks, after)
            if op != "=" or after >= n or toks[after][0] != "str":
                continue
            literal, end = _str_value(toks[after]), after
            if len(literal) != width:
                continue            # not a prefix test: substr(x,1,7)='abc'
            start = i
        elif toks[i][0] in ("word", "ident"):
            parts, after = _col_ref(toks, i)
            if parts is None or not _is_word(toks[after] if after < n else ("", ""), "like"):
                continue
            if after + 1 >= n or toks[after + 1][0] != "str":
                continue
            raw = _str_value(toks[after + 1])
            # Only a pure prefix pattern: one trailing %, nothing else special.
            if not raw.endswith("%") or any(c in raw[:-1] for c in "%_\\"):
                continue
            literal, end, start = raw[:-1], after + 1, i
            if not literal:
                continue
        else:
            continue
        bound = _upper_bound(literal)
        if bound is None:
            continue
        col = ".".join(parts)
        edits.append((spans[start][0], spans[end][1],
                      f"({col} >= {_sql_str(literal)} AND {col} < {_sql_str(bound)})"))
    return edits


# ── Rule 2: a date function is a half-open range ────────────────────────

def _month_start(year, month):
    return datetime.date(year, month, 1)


def _bucket(fn, unit, value):
    """The half-open [lo, hi) interval of the dates f() maps onto `value`.

    year(x) = 2023 is true for exactly the dates in [2023-01-01, 2024-01-01);
    date_trunc('month', x) = DATE '2023-03-01' for exactly [2023-03-01,
    2023-04-01). Returns None when the value cannot name a whole bucket (a
    date_trunc compared to a mid-month date matches nothing, and "nothing" is
    not a range we should invent)."""
    if fn == "year":
        return datetime.date(value, 1, 1), datetime.date(value + 1, 1, 1)
    # value is a date here
    if unit in ("year", "years", "yyyy", "yy"):
        if (value.month, value.day) != (1, 1):
            return None
        return value, datetime.date(value.year + 1, 1, 1)
    if unit in ("month", "months", "mm", "mon"):
        if value.day != 1:
            return None
        nxt = (_month_start(value.year + 1, 1) if value.month == 12
               else _month_start(value.year, value.month + 1))
        return value, nxt
    if unit in ("day", "days", "dd", "date"):
        return value, value + datetime.timedelta(days=1)
    return None


_DATE_LIT = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]00:00:00(?:\.0+)?)?$")


def _date_literal(toks, i):
    """`DATE '2023-03-01'` / `TIMESTAMP '2023-03-01 00:00:00'` / `'2023-03-01'`
    at `i` -> (date, index after), or (None, i). A timestamp literal with a
    non-midnight time is refused: it cannot be the start of a date bucket."""
    n = len(toks)
    j = i
    if j < n and _is_word(toks[j], "date", "timestamp"):
        j += 1
    if j >= n or toks[j][0] != "str":
        return None, i
    m = _DATE_LIT.match(_str_value(toks[j]))
    if not m:
        return None, i
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), j + 1
    except ValueError:
        return None, i


def _date_call(toks, i):
    """A date-bucketing call at `i` -> (fn, unit, column parts, index after).

    Recognized: year(c), extract(YEAR FROM c), date_trunc('unit', c),
    cast(c AS DATE), c::DATE, date(c). Each maps a date/timestamp onto the
    first instant of a bucket, which is what makes _bucket() exact.
    """
    n = len(toks)
    if toks[i][0] in ("word", "ident"):
        parts, after = _col_ref(toks, i)
        # c::DATE — the lexer emits '::' as two punct tokens.
        if (parts and after + 2 < n and toks[after] == ("punct", ":")
                and toks[after + 1] == ("punct", ":")
                and _is_word(toks[after + 2], "date")):
            return "cast_date", "day", parts, after + 3
    if not _is_word(toks[i], "year", "extract", "date_trunc", "cast", "date"):
        return None
    if i + 1 >= n or toks[i + 1] != ("punct", "("):
        return None
    close = _close_paren(toks, i + 1)
    if close is None:
        return None
    fn = toks[i][1].lower()
    args = _split_args(toks, i + 1, close)

    if fn in ("year", "date"):
        if len(args) != 1:
            return None
        parts, after = _col_ref(toks, args[0][0])
        if parts is None or after != args[0][1]:
            return None
        return ("year" if fn == "year" else "cast_date",
                "year" if fn == "year" else "day", parts, close + 1)

    if fn == "extract":
        # extract(YEAR FROM c) is a single "argument" holding `YEAR FROM c`.
        if len(args) != 1:
            return None
        a, b = args[0]
        if not (_is_word(toks[a], "year") and a + 1 < b and _is_word(toks[a + 1], "from")):
            return None
        parts, after = _col_ref(toks, a + 2)
        if parts is None or after != b:
            return None
        return "year", "year", parts, close + 1

    if fn == "date_trunc":
        if len(args) != 2 or toks[args[0][0]][0] != "str" or args[0][1] - args[0][0] != 1:
            return None
        unit = _str_value(toks[args[0][0]]).lower()
        parts, after = _col_ref(toks, args[1][0])
        if parts is None or after != args[1][1]:
            return None
        return "trunc", unit, parts, close + 1

    if fn == "cast":
        if len(args) != 1:
            return None
        a, b = args[0]
        parts, after = _col_ref(toks, a)
        if parts is None or after + 2 != b:
            return None
        if not (_is_word(toks[after], "as") and _is_word(toks[after + 1], "date")):
            return None
        return "cast_date", "day", parts, close + 1
    return None


def _range_predicate(col, kind, lo, hi, op):
    """The predicate on the raw column that `f(col) <op> value` is equal to.

    f is monotone non-decreasing and constant on [lo, hi), so:
      =   ->  lo <= col < hi        >   ->  col >= hi
      >=  ->  col >= lo             <   ->  col < lo
      <=  ->  col < hi
    """
    def lit(d):
        return (f"DATE '{d.isoformat()}'" if kind == "date"
                else f"TIMESTAMP '{d.isoformat()} 00:00:00'")
    if op == "=":
        return f"({col} >= {lit(lo)} AND {col} < {lit(hi)})"
    if op == ">":
        return f"({col} >= {lit(hi)})"
    if op == ">=":
        return f"({col} >= {lit(lo)})"
    if op == "<":
        return f"({col} < {lit(lo)})"
    if op == "<=":
        return f"({col} < {lit(hi)})"
    return None


def _rule_sargable_date_part(toks, spans, ctx):
    """year(order_ts) = 2023 -> order_ts >= ... AND order_ts < ...

    Refuses unless the column's DECLARED type (from the connector's own
    get_schema) is a plain DATE or a TIMESTAMP without a time zone. Without the
    type this is a guess: `year('2023-01-05')` on a string column means
    something different on every engine, and on a zoned timestamp the function
    and the literal need not be read in the same zone.
    """
    edits, n = [], len(toks)
    for i in range(n):
        call = _date_call(toks, i)
        if not call:
            continue
        fn, unit, parts, after = call
        kind = ctx.column_kind(parts)
        if kind is None:
            continue
        op, after = _comparison(toks, after)
        if op is None:
            continue
        if fn == "year":
            if after >= n or toks[after][0] != "num" or not toks[after][1].isdigit():
                continue
            year = int(toks[after][1])
            if not 1 <= year <= 9998:
                continue
            bucket, end = _bucket("year", unit, year), after
        else:
            value, nxt = _date_literal(toks, after)
            if value is None:
                continue
            bucket, end = _bucket("trunc", unit, value), nxt - 1
        if bucket is None:
            continue
        text = _range_predicate(".".join(parts), kind, bucket[0], bucket[1], op)
        if text:
            edits.append((spans[i][0], spans[end][1], text))
    return edits


# ── Rule 3: copy an outer filter into the CTE it filters ────────────────

_Block = collections.namedtuple("_Block", "name body_start body_end")


def _with_blocks(toks):
    """[(name, first token of the body, token after the body)] for a leading
    WITH clause. Only the plain `name AS ( ... )` form; a column list or
    RECURSIVE returns nothing, because those are shapes this rule has not
    proved anything about."""
    n = len(toks)
    if not (n and _is_word(toks[0], "with")):
        return []
    if n > 1 and _is_word(toks[1], "recursive"):
        return []
    blocks, i = [], 1
    while i < n:
        if toks[i][0] not in ("word", "ident"):
            return []
        name = toks[i][1]
        if not (i + 2 < n and _is_word(toks[i + 1], "as") and toks[i + 2] == ("punct", "(")):
            return []
        close = _close_paren(toks, i + 2)
        if close is None:
            return []
        blocks.append(_Block(name, i + 3, close))
        i = close + 1
        if i < n and toks[i] == ("punct", ","):
            i += 1
            continue
        return blocks
    return []


def _top_level(toks, start, end, word):
    """Index of `word` at paren depth 0 between start and end, or None."""
    depth = 0
    for j in range(start, end):
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
        elif depth == 0 and _is_word(toks[j], word):
            return j
    return None


def _depth0_words(toks, start, end):
    depth, out = 0, set()
    for j in range(start, end):
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
        elif depth == 0 and toks[j][0] == "word":
            out.add(toks[j][1].lower())
    return out


def _select_items(toks, start, end):
    """Top-level comma-separated ranges of a block's select list."""
    items, depth, at = [], 0, start
    for j in range(start, end):
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
        elif toks[j] == ("punct", ",") and depth == 0:
            items.append((at, j))
            at = j + 1
    if at < end:
        items.append((at, end))
    return items


def _output_map(toks, start, end):
    """{output column -> the expression text it comes from} for a select list,
    plus whether the list has a `*`.

    Only plain column references are mapped. `sum(x) AS total` is deliberately
    left out: substituting an aggregate into a WHERE is not predicate
    pushdown, it is a wrong query.
    """
    mapping, star = {}, False
    for a, b in _select_items(toks, start, end):
        if b - a == 1 and toks[a] == ("punct", "*"):
            star = True
            continue
        if b - a >= 3 and toks[b - 1] == ("punct", "*") and toks[b - 2] == ("punct", "."):
            star = True                          # t.*
            continue
        alias = None
        expr_end = b
        if b - a >= 2 and toks[b - 1][0] in ("word", "ident"):
            if b - a >= 3 and _is_word(toks[b - 2], "as"):
                alias, expr_end = toks[b - 1][1], b - 2
            elif toks[b - 2][0] == "punct" and toks[b - 2][1] == ")":
                alias, expr_end = toks[b - 1][1], b - 1
        parts, after = _col_ref(toks, a)
        if parts is None or after != expr_end:
            continue                              # an expression, not a column
        mapping[(alias or parts[-1]).lower()] = ".".join(parts)
    return mapping, star


def _from_relations(toks, start, end):
    """[(relation name, alias)] for the top-level FROM/JOIN targets of a block."""
    out, depth, j = [], 0, start
    while j < end:
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
        elif depth == 0 and _is_word(toks[j], "from", "join"):
            parts, after = _col_ref(toks, j + 1)
            if parts is None:
                return []                          # a subquery or a function
            alias = None
            if after < end and _is_word(toks[after], "as") and after + 1 < end:
                alias = toks[after + 1][1]
            elif (after < end and toks[after][0] in ("word", "ident")
                  and not _is_word(toks[after], *_CLAUSE_END)
                  and not _is_word(toks[after], "on", "using", "where", "join",
                                   "inner", "left", "right", "full", "cross",
                                   "natural", "anti", "semi")):
                alias = toks[after][1]
            out.append((parts[-1], alias or parts[-1]))
        j += 1
    return out


def _conjuncts(toks, start, end):
    """Top-level AND-separated ranges. An OR anywhere at depth 0 means the
    whole WHERE is one conjunct and nothing can be pushed from part of it.

    The AND of a BETWEEN is not a conjunction: splitting `dt BETWEEN 'a' AND
    'b'` on it produced the predicate `dt BETWEEN 'a'`, which is both invalid
    and — had the engine accepted it — a different filter. Each depth-0 BETWEEN
    therefore consumes the next depth-0 AND.
    """
    depth, parts, at, pending_between = 0, [], start, 0
    for j in range(start, end):
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
        elif depth == 0 and _is_word(toks[j], "between"):
            pending_between += 1
        elif depth == 0 and _is_word(toks[j], "or"):
            return [(start, end)]
        elif depth == 0 and _is_word(toks[j], "and"):
            if pending_between:
                pending_between -= 1
                continue
            parts.append((at, j))
            at = j + 1
    if at < end:
        parts.append((at, end))
    return parts


def _clause_bounds(toks, start, end, clause):
    """(first token after `clause`, first token of the next clause) at depth 0."""
    at = _top_level(toks, start, end, clause)
    if at is None:
        return None
    depth, j = 0, at + 1
    while j < end:
        if toks[j] == ("punct", "("):
            depth += 1
        elif toks[j] == ("punct", ")"):
            depth -= 1
        elif depth == 0 and toks[j][0] == "word" and toks[j][1].lower() in _CLAUSE_END:
            return at + 1, j
        j += 1
    return at + 1, end


def _rule_push_filter_into_cte(toks, spans, ctx):
    """Copy an outer WHERE conjunct into the CTE whose rows it filters.

    Equivalence: the predicate is COPIED, never moved, so the outer query keeps
    filtering exactly as before and the only question is whether the extra
    inner predicate can remove a row the outer one would have kept. It cannot,
    when the block is a plain projection over its FROM — no GROUP BY, HAVING,
    DISTINCT, window function, LIMIT or set operator (_NOT_PUSHABLE), and no
    aggregate in the select list — because then each output row comes from one
    input row and the predicate holds on the output row exactly when it holds
    on the input row it was projected from. Any of those clauses breaks the
    one-row-to-one-row correspondence, so the rule refuses instead of guessing.
    """
    blocks = _with_blocks(toks)
    if not blocks:
        return []
    n = len(toks)
    outer_start = blocks[-1].body_end + 1
    where = _clause_bounds(toks, outer_start, n, "where")
    if not where:
        return []
    from_bounds = _clause_bounds(toks, outer_start, n, "from")
    if not from_bounds:
        return []
    outer_rels = _from_relations(toks, from_bounds[0] - 1, where[0] - 1)
    by_alias = {a.lower(): r.lower() for r, a in outer_rels}
    by_name = {b.name.lower(): b for b in blocks}

    # Predicates are gathered PER BLOCK and spliced once. Emitting one edit per
    # conjunct inserted a second ` WHERE ` into a block that had none, because
    # both anchors were computed against the same unedited text.
    pushed = collections.OrderedDict()
    for c_start, c_end in _conjuncts(toks, *where):
        # Substitute the column references INSIDE the conjunct's own text
        # rather than reassembling it from tokens: `>=` is two punct tokens and
        # a token-by-token rebuild spelled it `> =`.
        target, subs, ok = None, [], True
        j = c_start
        while j < c_end:
            if toks[j][0] not in ("word", "ident"):
                j += 1
                continue
            if toks[j][0] == "word" and j + 1 < c_end and toks[j + 1][0] == "str":
                # A bare word directly in front of a string literal is a TYPE,
                # not a column: `TIMESTAMP '2023-01-01 00:00:00'`. Checked
                # before the keyword list so every typed literal is covered,
                # including the ones (DATE, TIMESTAMP) that are also plausible
                # column names elsewhere in the query.
                j += 2
                continue
            if toks[j][0] == "word" and toks[j][1].lower() in _NON_COLUMN_WORDS:
                j += 1
                continue
            parts, after = _col_ref(toks, j)
            if parts is None:
                j += 1
                continue
            if len(parts) == 1:
                # Unqualified: only unambiguous when the outer query reads
                # exactly one relation.
                if len(outer_rels) != 1:
                    ok = False
                    break
                alias, column = outer_rels[0][1], parts[0]
            elif len(parts) == 2:
                alias, column = parts[0], parts[1]
            else:
                ok = False
                break
            block = by_name.get(by_alias.get(alias.lower(), ""))
            if block is None or (target and block.name != target.name):
                ok = False
                break
            target = block
            inner = ctx.inner_expression(block, column)
            if inner is None:
                ok = False
                break
            subs.append((spans[j][0], spans[after - 1][1], inner))
            j = after
        if not ok or target is None or not ctx.pushable(target):
            continue
        start, end = spans[c_start][0], spans[c_end - 1][1]
        predicate = _splice(ctx.sql[start:end],
                            [(a - start, b - start, t) for a, b, t in subs]).strip()
        # Idempotence: optimize() runs the rules to a fixpoint, so a predicate
        # already sitting in the block's WHERE must not be appended again —
        # without this the same filter accumulated once per pass.
        if not predicate or ctx.already_filters(target, predicate):
            continue
        pushed.setdefault(target.name, (target, []))[1].append(predicate)

    edits = []
    for target, predicates in pushed.values():
        anchor = ctx.push_anchor(target)
        if anchor is None:
            continue
        at, joiner = anchor
        edits.append((at, at, joiner + " AND ".join(f"({p})" for p in predicates)))
    return edits


def _shape(toks):
    """A comparable form of a token run: kinds and case-folded text, with any
    wrapping parentheses removed so `(dt = 'x')` and `dt = 'x'` are one thing."""
    out = [(k, t.lower() if k == "word" else t) for k, t in toks]
    while len(out) >= 2 and out[0] == ("punct", "(") and out[-1] == ("punct", ")"):
        out = out[1:-1]
    return tuple(out)


# ── The context every rule reads ────────────────────────────────────────

class _Context:
    """What the rules need to know that the token stream does not say: the
    dialect (how strings compare), the column types (from the connector's own
    catalog), and the per-block analysis rule 3 shares."""

    def __init__(self, sql, toks, spans, dialect, columns):
        self.sql, self.toks, self.spans = sql, toks, spans
        self.dialect = (dialect or "").lower()
        #: bare lower-case column name -> declared type. Built by plans.py from
        #: connector.get_schema(); a name that means two different types in two
        #: tables is DROPPED by the builder, so a lookup here is never a guess.
        self.columns = {k.lower(): v for k, v in (columns or {}).items()}
        self._blocks = {}

    def column_kind(self, parts):
        """"date" / "timestamp" for a column reference, or None when the type
        is unknown or is anything else (including a zoned timestamp)."""
        declared = self.columns.get(parts[-1].lower())
        if not declared:
            return None
        if _DATE_TYPE.match(declared):
            return "date"
        if _TIMESTAMP_TYPE.match(declared):
            return "timestamp"
        return None

    def _analyze(self, block):
        if block.name in self._blocks:
            return self._blocks[block.name]
        toks, s, e = self.toks, block.body_start, block.body_end
        info = {"pushable": False, "map": {}, "star": False, "relations": []}
        if _is_word(toks[s], "select"):
            select_end = _top_level(toks, s, e, "from")
            if select_end is not None:
                words = _depth0_words(toks, s, e)
                info["relations"] = _from_relations(toks, select_end, e)
                info["map"], info["star"] = _output_map(toks, s + 1, select_end)
                calls = {toks[k][1].lower() for k in range(s, select_end)
                         if toks[k][0] == "word" and k + 1 < e
                         and toks[k + 1] == ("punct", "(")}
                info["pushable"] = (not (words & _NOT_PUSHABLE)
                                    and not (calls & _AGGREGATES)
                                    and bool(info["relations"]))
        self._blocks[block.name] = info
        return info

    def pushable(self, block):
        return self._analyze(block)["pushable"]

    def inner_expression(self, block, column):
        """The expression inside `block` that its output column `column` is.

        `SELECT t.dt AS day FROM t` maps `day` to `t.dt`. A `SELECT *` over a
        SINGLE relation maps any name to itself (the bare name is unambiguous
        there); over a join it does not, because the same bare name may exist
        on both sides and picking one would be a coin flip.
        """
        info = self._analyze(block)
        hit = info["map"].get(column.lower())
        if hit:
            return hit
        if info["star"] and len(info["relations"]) == 1:
            return column
        return None

    def already_filters(self, block, predicate):
        """Is this exact predicate already a conjunct of the block's WHERE?

        Compared on the token stream, not the text, so spacing and case cannot
        make the same filter look new. It is a conservative check: a predicate
        that is present in a DIFFERENT spelling is pushed a second time, which
        is redundant but still correct.
        """
        where = _clause_bounds(self.toks, block.body_start, block.body_end, "where")
        if not where:
            return False
        try:
            want = _shape(queryguard.lex(predicate)[0])
        except QueryRejected:
            return False
        for a, b in _conjuncts(self.toks, *where):
            if _shape(self.toks[a:b]) == want:
                return True
        return False

    def push_anchor(self, block):
        """Where a pushed predicate is spliced into a block, and what joins it
        to what is already there: the end of its WHERE (` AND `), or the start
        of the first clause after its FROM (` WHERE `)."""
        toks, s, e = self.toks, block.body_start, block.body_end
        where = _clause_bounds(toks, s, e, "where")
        if where:
            return self.spans[where[1] - 1][1], " AND "
        after_from = _clause_bounds(toks, s, e, "from")
        if not after_from:
            return None
        return self.spans[after_from[1] - 1][1], " WHERE "


RULES = (
    ("sargable_prefix", _rule_sargable_prefix,
     "a prefix test hides the column from partition pruning and file skipping"),
    ("sargable_date_part", _rule_sargable_date_part,
     "a date function on the column hides it from partition pruning and "
     "min/max statistics"),
    ("push_filter_into_cte", _rule_push_filter_into_cte,
     "the filter only runs after the CTE has been built, so the CTE reads the "
     "whole table"),
)


def optimize(sql, *, dialect=None, columns=None, only=None, max_passes=4):
    """Apply every rule that fires, to a fixpoint. -> (sql, [Rewrite, ...]).

    One rule at a time, re-lexing in between: a rule's own output is the next
    rule's input (a pushed-down predicate is then a candidate for the range
    rules inside the CTE), and re-lexing means every rule always reads spans
    that belong to the text it is editing. `only` restricts the rule set, which
    is how the benchmark measures one rule at a time.

    Returns the input unchanged (and an empty list) when nothing fires or the
    SQL does not lex — an unparseable query is the engine's to reject, not
    this module's to repair.
    """
    applied = []
    for _ in range(max_passes):
        try:
            toks, cleaned, spans = queryguard.lex(sql)
        except QueryRejected:
            break
        if not toks:
            break
        changed = False
        for name, rule, why in RULES:
            if only and name not in only:
                continue
            ctx = _Context(cleaned, toks, spans, dialect, columns)
            try:
                edits = rule(toks, spans, ctx)
            except Exception:
                edits = []            # a rule that cannot read the query does nothing
            if not edits:
                continue
            try:
                sql = _splice(cleaned, edits)
            except ValueError:
                continue              # two edits collided; try again next pass
            applied.append(Rewrite(name, sql, why))
            changed = True
            break
        if not changed:
            break
    return sql, applied
