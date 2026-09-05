"""Suggested questions for a selected table.

Grounded in the table's real schema, so the prompts a user is offered can
actually be answered by the data in front of them. The LLM writes them when a
key is configured; otherwise column types drive a deterministic set, so the
picker is never empty.

Results are cached per (source, table, schema fingerprint): selecting a table
must not cost an LLM round trip every time, and the cache invalidates itself
when the schema changes.
"""
import json
import re

from . import agent, skills

_CACHE = {}          # (source, table, fingerprint) -> [questions]
_CACHE_MAX = 256
MAX_SUGGESTIONS = 5

# Hints match whole name TOKENS, never substrings: "lifetime_value" is a
# measure, not a date, and only a substring match would call it one.
_DATE_TOKENS = {"date", "day", "time", "timestamp", "ts", "month", "year",
                "week", "quarter", "created", "updated", "at", "on"}
_ID_HINT = re.compile(r"(^|_)id$|uuid|guid", re.I)
_NUM_TYPE = re.compile(r"int|numeric|decimal|real|double|float|money", re.I)
_DATE_TYPE = re.compile(r"date|time", re.I)
# Ranked worst-last. Additive money beats counts, and both beat per-unit rates:
# "revenue by region" is a real question, "unit_price by region" rarely is.
_MEASURE_TIERS = [
    re.compile(r"revenue|sales|amount|total|profit|spend|value|balance|gmv", re.I),
    re.compile(r"units|count|qty|quantity|orders|visits|clicks|sessions|views|"
               r"impressions|conversions|defects|minutes", re.I),
    re.compile(r"price|rate|ratio|percent|avg|average|score|margin|per_", re.I),
]
# Dimensions that are identities in disguise — grouping by them yields one row
# per record, which is a table, not an insight.
_DIM_PENALTY = re.compile(r"name|title|email|phone|address|description|url|comment|note", re.I)


def _tokens(name):
    return {t for t in re.split(r"[^a-z0-9]+", str(name).lower()) if t}


def _measure_rank(name):
    for tier, pat in enumerate(_MEASURE_TIERS):
        if pat.search(name):
            return tier
    return len(_MEASURE_TIERS)


def _classify(columns, rows=None):
    """Split columns into (dates, measures, dimensions) using type + name."""
    dates, measures, dims = [], [], []
    sample = (rows or [None])[0] if rows else None
    for i, c in enumerate(columns):
        name = str(c.get("name", ""))
        ctype = str(c.get("type", "")).lower()
        val = sample[i] if sample is not None and i < len(sample) else None
        is_num = bool(_NUM_TYPE.search(ctype)) or (
            isinstance(val, (int, float)) and not isinstance(val, bool))
        # A declared date/time type wins; otherwise require a whole-token hint.
        if _DATE_TYPE.search(ctype) or (_tokens(name) & _DATE_TOKENS):
            dates.append(name)
        elif _ID_HINT.search(name):
            continue  # ids aggregate to nonsense
        elif is_num:
            measures.append(name)
        else:
            dims.append(name)
    measures.sort(key=_measure_rank)
    dims.sort(key=lambda n: 1 if _DIM_PENALTY.search(n) else 0)
    return dates, measures, dims


def _deterministic(table, columns, rows=None):
    """Schema-shaped questions that work with no LLM key."""
    dates, measures, dims = _classify(columns, rows)
    m = measures[0] if measures else None
    m2 = measures[1] if len(measures) > 1 else None
    d = dims[0] if dims else None
    d2 = dims[1] if len(dims) > 1 else None
    t = dates[0] if dates else None

    out = []
    if m and t:
        out.append(f"How has {m} trended by month?")
    if m and d:
        out.append(f"Top 10 {d} by {m} — bar chart")
    if m and t and d:
        out.append(f"Compare {m} across {d} over time")
    if m and d2:
        out.append(f"Which {d2} has the highest {m}?")
    if m and m2:
        out.append(f"How do {m} and {m2} relate?")
    if m and t:
        out.append(f"{m} year over year, with a running total")
    if not out:
        # No measure found — offer shape questions instead of nothing.
        first = dims[0] if dims else (columns[0]["name"] if columns else "rows")
        out = [f"How many rows are in {table}?", f"Break down {table} by {first}"]
    return out[:MAX_SUGGESTIONS]


_LLM_SYS = """You write starter questions for a business analyst who just opened a table.

Return ONLY a JSON array of strings — no prose, no code fences.
Rules:
- Exactly {n} questions, each answerable by SQL over the columns given.
- Reference real column names naturally; never invent columns or tables.
- Vary the shape: a trend, a ranking, a comparison, a distribution, a share.
- Keep each under 12 words, phrased the way a person would ask.
- Prefer the columns that look like business measures over ids."""


def _from_llm(table, columns, rows=None, n=MAX_SUGGESTIONS):
    from langchain.chat_models import init_chat_model

    llm = init_chat_model(agent.llm_spec())
    payload = json.dumps({
        "table": table,
        "columns": [{"name": c.get("name"), "type": c.get("type")} for c in columns][:40],
        "sample_rows": (rows or [])[:3],
    }, default=str)
    reply = llm.invoke([("system", _LLM_SYS.format(n=n)), ("user", payload)])
    text = reply.content if isinstance(reply.content, str) else "".join(
        b.get("text", "") for b in reply.content if isinstance(b, dict))
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    qs = [str(q).strip() for q in data if str(q).strip()]
    if not qs:
        raise ValueError("no questions returned")
    return qs[:n]


def suggestions_for(connector, table, columns, rows=None):
    """Questions for one table. Never raises — falls back to the schema rules.

    `columns` and `rows` are what the CALLER may see: the catalog passes the
    governed schema (denied columns removed) and a gateway.execute sample
    (denied columns dropped, masked ones as ***), so nothing below — least of
    all the payload _from_llm sends to an external model — ever holds a value
    governance would withhold. This function never reads the connector."""
    fp = skills.fingerprint(connector.dialect, [table], {table: columns})
    key = (connector.name, table, fp)
    if key in _CACHE:
        return _CACHE[key]

    fallback = _deterministic(table, columns, rows)
    out = fallback
    if agent.llm_available():
        try:
            out = _from_llm(table, columns, rows) or fallback
        except Exception:
            out = fallback

    _CACHE[key] = out
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    return out
