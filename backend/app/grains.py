"""Time grains — prompt word → grain → the dialect's bucket expression.

A "monthly revenue by region" request must produce MONTHLY buckets, not a
daily aggregate that happens to mention revenue. Two callers need the same
vocabulary to make that true:

  * agent.py's keyless fallback, which already owned the SQLite strftime
    table (``_GRAN_FMT``) and now imports it from here;
  * pipelines.py's deterministic drafter, which buckets a drafted step by the
    grain named in the prompt and warns when an LLM-drafted step does not.

Invariants:
  - Imports nothing from ``app`` — a pure leaf, so anyone may import it at
    module level (agent, pipelines and anything below them).
  - Deterministic: no model call, no I/O, same answer for the same input.
  - ``bucket_expr`` emits READ-ONLY scalar SQL. It never interpolates user
    text — only a caller-supplied column name and a grain from ``GRAINS`` —
    so it cannot widen what the query guard later sees.
  - ``has_bucket`` is a HINT used to warn, never to drop a step: a false
    negative must only ever add a ⚠ badge.
"""
import re

#: Coarse-to-fine is irrelevant here; this is just the closed vocabulary.
GRAINS = ("hour", "day", "week", "month", "quarter", "year")

#: SQLite strftime formats per grain. No "quarter": strftime has no quarter
#: format, so bucket_expr composes one from the month number instead.
SQLITE_FMT = {
    "hour": "%Y-%m-%d %H:00",
    "day": "%Y-%m-%d",
    "week": "%Y-%W",
    "month": "%Y-%m",
    "year": "%Y",
}

# Prompt words → grain. Both the adjective ("monthly") and the noun ("month")
# appear, because users write either; "annual"/"annually" are the one pair
# whose noun is not its own stem.
_WORDS = {
    "hourly": "hour", "hour": "hour", "hours": "hour",
    "daily": "day", "day": "day", "days": "day",
    "weekly": "week", "week": "week", "weeks": "week",
    "monthly": "month", "month": "month", "months": "month",
    "quarterly": "quarter", "quarter": "quarter", "quarters": "quarter",
    "yearly": "year", "year": "year", "years": "year",
    "annual": "year", "annually": "year",
}

#: Separator in the composed SQLite quarter label ('2024-Q3'). Shared by
#: bucket_expr and has_bucket so the one form we emit is the one we recognise.
QUARTER_MARK = "-Q"

# Every non-SQLite dialect we target (postgres, snowflake, databricks,
# bigquery, duckdb) buckets with DATE_TRUNC('<grain>', col), so bucket_expr
# emits that as the default. A dialect that disagrees surfaces as a
# verification failure — never as a silently wrong number.


def detect(prompt):
    """The time grain a request names, or None. First word wins, so
    "monthly revenue" is monthly even if the sentence later says "per day"
    about something else."""
    for w in re.findall(r"[a-z]+", (prompt or "").lower()):
        g = _WORDS.get(w)
        if g:
            return g
    return None


def bucket_expr(dialect, column, grain):
    """SQL that buckets `column` to `grain` in `dialect`.

    SQLite gets strftime (the demo warehouse's dialect); quarter is composed,
    because strftime cannot express it. Everything else gets DATE_TRUNC.
    Returns None for a grain outside GRAINS, so callers fall back to their
    un-bucketed shape rather than emitting nonsense.
    """
    if grain not in GRAINS:
        return None
    if dialect == "sqlite":
        if grain == "quarter":
            # '2024-Q3' — integer division of the month number, no CASE needed.
            # QUARTER_MARK is what makes this composed form recognisable again
            # by has_bucket, which otherwise only knows format strings.
            return (f"strftime('%Y', {column}) || '{QUARTER_MARK}' || "
                    f"((CAST(strftime('%m', {column}) AS INTEGER) + 2) / 3)")
        return f"strftime('{SQLITE_FMT[grain]}', {column})"
    return f"DATE_TRUNC('{grain}', {column})"


def has_bucket(sql, grain):
    """Does this SQL bucket by `grain`? Recognises the spellings the model
    actually emits across dialects — strftime/DATE_FORMAT with the grain's
    format string, DATE_TRUNC in either argument order, and EXTRACT.

    Deliberately conservative about the format string: the quoted literal is
    matched WHOLE, so `strftime('%Y-%m-%d', …)` (daily) is not mistaken for
    the monthly `'%Y-%m'`. That is the whole point of the check.
    """
    if grain not in GRAINS:
        return False
    s = (sql or "").lower()
    if re.search(rf"date_trunc\s*\(\s*'{grain}'", s):
        return True
    if re.search(rf"date_trunc\s*\(\s*[^,()]+,\s*'?{grain}'?\s*\)", s):
        return True          # BigQuery's DATE_TRUNC(col, MONTH)
    if re.search(rf"extract\s*\(\s*{grain}\b", s):
        return True
    fmt = SQLITE_FMT.get(grain)
    if fmt:
        lit = re.escape(fmt.lower())
        if re.search(rf"strftime\s*\(\s*'{lit}'", s):
            return True
        if re.search(rf"date_format\s*\([^,()]+,\s*'{lit}'", s):
            return True
    if grain == "quarter" and ("quarter" in s or f"'{QUARTER_MARK.lower()}'" in s):
        return True          # the composed SQLite form, or any spelling that names it
    return False
