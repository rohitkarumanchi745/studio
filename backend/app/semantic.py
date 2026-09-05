"""Semantic layer — one definition of truth for the business metrics.

Without this, the agent writes SQL from scratch for every question, so "revenue"
can compile differently each time and two phrasings of the same question can
return two different numbers. Dashboards, pipelines and chat each re-derive the
same measure independently. That is the classic self-serve-analytics failure:
nobody can trust a number because nobody can say how it was computed.

The semantic layer fixes that by letting an admin DEFINE the metrics and
dimensions once, in YAML — `revenue = SUM(sales.revenue)`, `region`, `month`
(a time dimension) — and then COMPILING every question against those
definitions. Ask "revenue by region", "sales across geographies", or
"how much did each region make" and all three resolve to the same metric and
dimension, so all three compile to byte-identical SQL and return the same number.

It plugs in ahead of the agent in a chat turn (chat._run_turn): when the loaded
model confidently resolves a prompt to defined metrics, the layer compiles the
canonical SQL deterministically — no model needed, so it works with no LLM key
and can't drift. Anything the layer can't resolve falls through to the agent
untouched. The compiled SQL still goes through queries.verify_sql, so RBAC, the
query guard and governance masking all apply exactly as they do to any query —
the semantic layer decides WHAT to compute, never who may see it.

Storage mirrors governance: a single active YAML document, admin-applied and
hot-reloaded, cached in process — including the freshness check that makes
that cache safe in a multi-process deployment. `PUT /api/semantic` reloads
only the replica that served it, so the accessors that decide what a prompt
compiles to (models_for, catalog) first call _refresh_if_stale(), which at
most once per STUDIO_SEMANTIC_REFRESH_S (default 5s) per process reads the
newest (id, applied_at) from semantic_models and reloads when it differs.
Without it, two replicas answer the same question with two different metric
definitions indefinitely — exactly the "nobody can trust a number" failure
this module exists to remove.
"""
import os
import re
import time
import uuid

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, queries, rbac, roster
from .auth import current_user

router = APIRouter(prefix="/semantic", tags=["semantic"])

_STATE = {"doc": None, "yaml": "", "source": None}
# See _refresh_if_stale(): "at" is this process's last probe of the store,
# "ident" the (id, applied_at) of the newest applied model as of that probe.
_FRESH = {"at": 0.0, "ident": None}
_UNKNOWN = object()          # store unreadable: keep the doc already in hand
DEFAULT_REFRESH_S = 5.0

_AGGS = {"sum", "avg", "min", "max", "count", "count_distinct", "median"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GRAINS = ("hour", "day", "week", "month", "quarter", "year")
# Words in a prompt that ask for a time breakdown, mapped to a grain.
_GRAIN_WORDS = {
    "hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month",
    "quarterly": "quarter", "yearly": "year", "annually": "year", "annual": "year",
    "by hour": "hour", "by day": "day", "by week": "week", "by month": "month",
    "by quarter": "quarter", "by year": "year",
}
# Words that imply a time breakdown without naming a grain — use the model default.
_TREND_WORDS = ("trend", "over time", "time series", "timeline", "trended", "trending")


# ── Store (mirrors governance) ──────────────────────────────────────────

def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS semantic_models (
            id TEXT PRIMARY KEY,
            yaml TEXT NOT NULL,
            applied_by TEXT,
            applied_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS semantic_models_applied_at
            ON semantic_models (applied_at DESC, id DESC);
        """
    )
    c.commit()
    c.close()


def _refresh_ttl():
    try:
        return max(0.0, float(os.getenv("STUDIO_SEMANTIC_REFRESH_S", DEFAULT_REFRESH_S)))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_S


def _newest_ident():
    """(id, applied_at) of the newest applied model — its whole identity, as a
    row is never edited in place. None when the table is empty, _UNKNOWN when
    the store cannot be read (which must not be read as "no model")."""
    try:
        c = db._conn()
        try:
            row = c.execute("SELECT id, applied_at FROM semantic_models "
                            "ORDER BY applied_at DESC, id DESC LIMIT 1").fetchone()
        finally:
            c.close()
    except Exception:
        return _UNKNOWN
    return (row["id"], row["applied_at"]) if row else None


def _refresh_if_stale():
    """Adopt a model another process applied; a no-op inside the TTL.

    Same problem and same shape as governance._refresh_if_stale(): _STATE is
    process-local and PUT /api/semantic reloads only the process that served
    it, so without this a second replica keeps compiling "revenue" from the
    previous definition indefinitely. Cost: one single-row indexed read of
    semantic_models per process per STUDIO_SEMANTIC_REFRESH_S (default 5s).
    The convergence window is one TTL, not zero. An unreadable store keeps
    the model already loaded; a malformed stored document still falls back to
    "no semantic layer" (the agent), never a crash."""
    now = time.monotonic()
    if now - _FRESH["at"] < _refresh_ttl():
        return
    _FRESH["at"] = now                   # stamp first: one probe per TTL, not per thread
    ident = _newest_ident()
    if ident is _UNKNOWN or ident == _FRESH["ident"]:
        return
    load()


def load():
    """Active document: DB (admin-applied) first, else the STUDIO_SEMANTIC file,
    else none (chat just uses the agent). Cached in _STATE; accessors re-check
    the store every STUDIO_SEMANTIC_REFRESH_S; reload() forces it now."""
    c = db._conn()
    row = c.execute("SELECT id, yaml, applied_at FROM semantic_models "
                    "ORDER BY applied_at DESC, id DESC LIMIT 1").fetchone()
    c.close()
    # Record the identity even when the YAML is unusable, or every refresh
    # would see a "change" and reload the same broken document forever.
    _FRESH.update(at=time.monotonic(),
                  ident=(row["id"], row["applied_at"]) if row else None)
    if row and row["yaml"].strip():
        _set(row["yaml"], "database")
        return
    path = os.getenv("STUDIO_SEMANTIC")
    if path and os.path.isfile(path):
        with open(path) as f:
            _set(f.read(), f"file:{path}")
        return
    _STATE.update(doc=None, yaml="", source=None)


def _set(text, source):
    """Install a document in this process. Callers other than load() leave
    _FRESH["ident"] alone: what they installed stands until the STORE's newest
    document differs from the one this process last observed."""
    ok, errors, doc = validate(text)
    if ok:
        _STATE.update(doc=doc, yaml=text, source=source)
    else:
        # A malformed stored doc fails closed to "no semantic layer", not a crash.
        _STATE.update(doc=None, yaml=text, source=f"{source} (invalid: {errors[0] if errors else '?'})")
    _FRESH["at"] = time.monotonic()      # just installed is just checked


def reload():
    load()


def loaded():
    return _STATE["doc"] is not None


def current_yaml():
    return _STATE["yaml"]


def active_source():
    return _STATE["source"]


# ── Validation / parse ──────────────────────────────────────────────────

def _norm_terms(*values):
    """Lowercased synonym set for matching, from names/synonyms."""
    out = set()
    for v in values:
        if not v:
            continue
        if isinstance(v, (list, tuple)):
            for x in v:
                out.add(str(x).strip().lower())
        else:
            out.add(str(v).strip().lower())
    return {t for t in out if t}


def validate(text):
    """(ok, errors, doc). Parse and structurally check a semantic YAML document.

    Shape:
      models:
        - source: demo
          table: sales
          filter: "status <> 'test'"          # optional, applied as WHERE
          default_time: month                  # optional grain for the time dim
          metrics:
            - name: revenue
              agg: sum                          # sum|avg|min|max|count|count_distinct|median
              expr: revenue                     # column or SQL expression ('*' for count)
              filter: "returned = 0"            # optional; CASE-wrapped for sum/count
              synonyms: [sales, turnover]
              format: currency                  # optional display hint
              description: Total sales revenue
          dimensions:
            - name: region
              expr: region
              synonyms: [geography, area]
            - name: month
              expr: order_date                  # a time dimension …
              grain: month                      # … truncated to this grain
              synonyms: [over time, monthly]
    """
    errors = []
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return False, [f"YAML parse error: {e}"], None
    if not isinstance(raw, dict):
        return False, ["Top level must be a mapping with a `models` list"], None

    models_raw = raw.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        return False, ["`models` must be a non-empty list"], None

    models = []
    for i, m in enumerate(models_raw):
        where = f"models[{i}]"
        if not isinstance(m, dict):
            errors.append(f"{where} must be a mapping")
            continue
        source = m.get("source")
        table = m.get("table")
        if not source or not table:
            errors.append(f"{where} needs `source` and `table`")
            continue
        metrics, dims = {}, {}
        for j, met in enumerate(m.get("metrics") or []):
            if not isinstance(met, dict) or not met.get("name"):
                errors.append(f"{where}.metrics[{j}] needs a `name`")
                continue
            name = str(met["name"]).strip()
            if not _IDENT.match(name):
                errors.append(f"{where}.metric '{name}': name must be a simple identifier")
                continue
            agg = str(met.get("agg", "sum")).strip().lower()
            if agg not in _AGGS:
                errors.append(f"{where}.metric '{name}': agg must be one of {sorted(_AGGS)}")
                continue
            expr = str(met.get("expr", "")).strip()
            if not expr:
                errors.append(f"{where}.metric '{name}' needs an `expr`")
                continue
            if name.lower() in metrics:
                errors.append(f"{where}.metric '{name}': duplicate metric name")
                continue
            metrics[name.lower()] = {
                "name": name, "agg": agg, "expr": expr,
                "filter": (met.get("filter") or "").strip() or None,
                "format": met.get("format"), "description": met.get("description", ""),
                "terms": _norm_terms(name, name.replace("_", " "), met.get("synonyms")),
            }
        for j, dim in enumerate(m.get("dimensions") or []):
            if not isinstance(dim, dict) or not dim.get("name"):
                errors.append(f"{where}.dimensions[{j}] needs a `name`")
                continue
            name = str(dim["name"]).strip()
            if not _IDENT.match(name):
                errors.append(f"{where}.dimension '{name}': name must be a simple identifier")
                continue
            expr = str(dim.get("expr", "")).strip()
            if not expr:
                errors.append(f"{where}.dimension '{name}' needs an `expr`")
                continue
            grain = dim.get("grain")
            if grain and str(grain).lower() not in _GRAINS:
                errors.append(f"{where}.dimension '{name}': grain must be one of {list(_GRAINS)}")
                continue
            if name.lower() in dims:
                errors.append(f"{where}.dimension '{name}': duplicate dimension name")
                continue
            dims[name.lower()] = {
                "name": name, "expr": expr,
                "grain": str(grain).lower() if grain else None,
                "description": dim.get("description", ""),
                "terms": _norm_terms(name, name.replace("_", " "), dim.get("synonyms")),
            }
        if not metrics:
            errors.append(f"{where} ({source}.{table}) defines no valid metrics")
            continue
        default_time = m.get("default_time")
        dt = str(default_time).lower() if default_time else None
        if dt:
            # default_time must name a real time dimension (by grain or by name),
            # else time-intent prompts would silently fall back to an arbitrary one.
            time_keys = ({d["grain"] for d in dims.values() if d["grain"]}
                         | {k for k, d in dims.items() if d["grain"]})
            if dt not in time_keys:
                errors.append(f"{where}: default_time '{dt}' matches no time dimension")
                continue
        models.append({
            "source": str(source), "table": str(table),
            "filter": (m.get("filter") or "").strip() or None,
            "default_time": dt,
            "metrics": metrics, "dimensions": dims,
        })

    if errors:
        return False, errors, None
    if not models:
        return False, ["No valid models"], None
    return True, [], {"models": models}


# ── Introspection ───────────────────────────────────────────────────────

def models_for(source, table=None):
    """The models defined on a source. Refreshes first: every compile decision
    goes through here, and must not use a definition another process replaced."""
    _refresh_if_stale()
    doc = _STATE["doc"]
    if not doc:
        return []
    out = [m for m in doc["models"] if m["source"] == source]
    if table and table != "*":
        out = [m for m in out if m["table"] == table]
    return out


def catalog(user):
    """The metrics/dimensions defined on sources THIS user's role can reach —
    what the UI shows and what a prompt can resolve against."""
    _refresh_if_stale()
    doc = _STATE["doc"]
    if not doc:
        return {"loaded": False, "models": []}
    out = []
    for m in doc["models"]:
        if not rbac.can_access(user["role"], m["source"], m["table"]):
            continue   # 404-not-403: an inaccessible model is simply invisible
        out.append({
            "source": m["source"], "table": m["table"],
            "metrics": [{"name": v["name"], "agg": v["agg"], "expr": v["expr"],
                         "format": v.get("format"), "description": v.get("description", ""),
                         "synonyms": sorted(v["terms"])} for v in m["metrics"].values()],
            "dimensions": [{"name": v["name"], "grain": v.get("grain"),
                            "description": v.get("description", ""),
                            "synonyms": sorted(v["terms"])} for v in m["dimensions"].values()],
        })
    return {"loaded": True, "source": _STATE["source"], "models": out}


# ── Resolve a prompt to metrics + dimensions ────────────────────────────

def _stem(w):
    """A crude singularizer so 'regions'/'geographies'/'markets' match
    'region'/'geography'/'market'. Not linguistically perfect — just enough to
    absorb the everyday plurals that would otherwise miss a defined term."""
    w = w.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w


def _stem_tokens(text):
    return {_stem(t) for t in re.findall(r"[a-z0-9_]+", (text or "").lower())}


def _matches(prompt_stems, terms, neg_stems=frozenset()):
    """A term matches when every word of it (stemmed) is present in the prompt
    (also stemmed) and none of its words was negated ("ignore revenue"). Multi-
    word synonyms like 'total sales' need all their words; single words like
    'region' match 'regions' via the stem."""
    for t in terms:
        words = [_stem(w) for w in re.findall(r"[a-z0-9_]+", (t or "").lower())]
        if words and all(w in prompt_stems for w in words) and not any(w in neg_stems for w in words):
            return True
    return False


# Words that negate/exclude the term after them, so "revenue, not by region"
# doesn't match `region`. A short window covers "not by region", "ignore revenue".
_NEG_CUES = {"not", "no", "without", "excluding", "exclude", "except", "ignore",
             "ignoring", "omit", "omitting", "drop", "minus", "rather"}


def _matched_words(prompt_stems, terms):
    """The widest fully-present (stemmed) synonym of a term set — used to tell a
    strong multi-word match ('average order value') from an incidental one-word
    overlap ('order'), so the broader match can shadow the narrower."""
    best = set()
    for t in terms:
        w = {_stem(x) for x in re.findall(r"[a-z0-9_]+", (t or "").lower())}
        if w and w <= prompt_stems and len(w) > len(best):
            best = w
    return best


def _negated_stems(prompt_l):
    """Stems appearing in a short window after a negation/exclusion cue."""
    neg, window = set(), 0
    for tok in re.findall(r"[a-z0-9_]+", prompt_l):
        if tok in _NEG_CUES:
            window = 3
            continue
        if window:
            neg.add(_stem(tok))
            window -= 1
    return neg


def resolve(source, prompt, table=None):
    """Map a natural-language prompt to a model + its matched metrics and
    dimensions, deterministically. Returns None when nothing confidently
    matches (so chat falls through to the agent) — we never force-fit a prompt
    to a metric, because a wrong-but-consistent number is worse than none."""
    prompt_l = (prompt or "").lower()
    prompt_stems = _stem_tokens(prompt_l)
    prompt_tokens = set(re.findall(r"[a-z0-9_]+", prompt_l))
    neg = _negated_stems(prompt_l)
    best = None
    for m in models_for(source, table):
        metrics = [v for v in m["metrics"].values() if _matches(prompt_stems, v["terms"], neg)]
        if not metrics:
            continue
        # Shadowing: drop a metric whose only match is a strict subset of a wider
        # metric's match — "average order value" must not also fire `orders` via
        # its lone word "order", which would silently change the ORDER BY.
        mw = [_matched_words(prompt_stems, v["terms"]) for v in metrics]
        metrics = [v for i, v in enumerate(metrics)
                   if not any(j != i and mw[i] < mw[j] for j in range(len(metrics)))]

        dims = [v for v in m["dimensions"].values() if _matches(prompt_stems, v["terms"], neg)]

        # Time intent: an explicit grain word, else a trend word → the model's
        # default time dimension (or the sole time dimension if unambiguous).
        grain = next((g for w, g in _GRAIN_WORDS.items() if w in prompt_l), None)
        wants_time = grain is not None or any(w in prompt_l for w in _TREND_WORDS)
        time_dims = [v for v in m["dimensions"].values() if v.get("grain")]

        # Confidence floor: a single metric with no dimension and no time intent
        # is the classic false-positive shape (an incidental word like "order" in
        # "in order to"). Require a metric term to appear as a whole UNSTEMMED
        # token — "orders" resolves, the filler "order" does not.
        if len(metrics) == 1 and not dims and not wants_time:
            exact = any(
                all(w in prompt_tokens for w in re.findall(r"[a-z0-9_]+", t))
                for t in metrics[0]["terms"])
            if not exact:
                continue

        if wants_time and not any(d.get("grain") for d in dims):
            pick = None
            if m.get("default_time"):
                pick = next((v for v in time_dims if v["name"].lower() == m["default_time"]
                             or v.get("grain") == m["default_time"]), None)
            if pick is None and len(time_dims) == 1:
                pick = time_dims[0]
            if pick is None and time_dims:
                pick = time_dims[0]
            if pick:
                dims = dims + [pick]
        # An explicit grain word overrides the dimension's declared grain.
        if grain:
            dims = [dict(d, grain=grain) if d.get("grain") else d for d in dims]

        score = len(metrics) * 2 + len(dims)
        cand = {"model": m, "metrics": metrics, "dimensions": dims, "score": score}
        if best is None or score > best["score"]:
            best = cand
    return best


# ── Compile to dialect SQL ──────────────────────────────────────────────

def _q(ident):
    return '"' + str(ident).replace('"', '""') + '"'


def _time_trunc(dialect, expr, grain):
    """Truncate a date/timestamp expression to a grain, per dialect. The demo is
    SQLite with TEXT ISO dates; warehouses have real date types."""
    g = grain
    if dialect == "sqlite":
        fmt = {"hour": "%Y-%m-%dT%H:00", "day": "%Y-%m-%d", "week": "%Y-W%W",
               "month": "%Y-%m-01", "year": "%Y-01-01"}.get(g)
        if g == "quarter":
            # SQLite has no quarter; bucket to the quarter's first month.
            return (f"strftime('%Y-', {expr}) || "
                    f"printf('%02d', ((cast(strftime('%m', {expr}) as int)-1)/3)*3+1) || '-01'")
        return f"strftime('{fmt}', {expr})" if fmt else expr
    if dialect == "bigquery":
        unit = {"hour": "HOUR", "day": "DAY", "week": "WEEK", "month": "MONTH",
                "quarter": "QUARTER", "year": "YEAR"}.get(g, "MONTH")
        return f"TIMESTAMP_TRUNC(CAST({expr} AS TIMESTAMP), {unit})"
    # duckdb, ansi, databricks/spark, snowflake, postgres all accept date_trunc('unit', x)
    return f"date_trunc('{g}', CAST({expr} AS TIMESTAMP))"


# median needs an ordered-set aggregate; these dialects have PERCENTILE_CONT.
# Stock SQLite and BigQuery have no portable single-pass median, so a median
# metric declines there (uniformly, dev and prod) rather than emitting SQL that
# works only on a MEDIAN-enabled build.
_MEDIAN_DIALECTS = {"ansi", "postgres", "duckdb", "snowflake", "databricks"}


def _agg_sql(metric, dialect="ansi"):
    """The aggregate SQL for a metric, dialect-aware, with an optional CASE
    filter. Raises ValueError when the aggregate can't be expressed for the
    dialect (median on sqlite/bigquery) so the caller declines consistently."""
    agg, expr, filt = metric["agg"], metric["expr"], metric.get("filter")
    if agg == "count" and expr == "*":
        base = "*" if not filt else f"CASE WHEN {filt} THEN 1 END"
        return f"COUNT({base})"
    if filt:
        # A per-metric filter only composes cleanly for additive/count aggregates;
        # for others fall back to an unfiltered agg (validated at author time is
        # future work — for now the common revenue/count cases are covered).
        if agg == "sum":
            return f"SUM(CASE WHEN {filt} THEN {expr} ELSE 0 END)"
        if agg == "count":
            return f"COUNT(CASE WHEN {filt} THEN {expr} END)"
        if agg == "count_distinct":
            return f"COUNT(DISTINCT CASE WHEN {filt} THEN {expr} END)"
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {expr})"
    if agg == "median":
        if dialect not in _MEDIAN_DIALECTS:
            raise ValueError(f"median is not supported on the {dialect} dialect")
        return f"PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY {expr})"
    fn = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "count": "COUNT"}[agg]
    return f"{fn}({expr})"


def compile_sql(model, metrics, dimensions, dialect, limit=5000):
    """The canonical SQL for these metrics grouped by these dimensions.

    Deterministic: same inputs → same SQL string, which is the whole point.
    Ordering: a time dimension → chronological; otherwise rank by the first
    metric descending (the natural "top regions by revenue" shape)."""
    dim_exprs, group_terms, has_time = [], [], False
    for d in dimensions:
        if d.get("grain"):
            e = _time_trunc(dialect, d["expr"], d["grain"])
            has_time = True
        else:
            e = d["expr"]
        dim_exprs.append(f"{e} AS {_q(d['name'])}")
        group_terms.append(e)

    met_exprs = [f"{_agg_sql(mt, dialect)} AS {_q(mt['name'])}" for mt in metrics]
    select = ", ".join(dim_exprs + met_exprs)
    sql = f"SELECT {select} FROM {model['table']}"
    if model.get("filter"):
        sql += f" WHERE {model['filter']}"
    if group_terms:
        sql += " GROUP BY " + ", ".join(group_terms)
        if has_time:
            time_e = next(_time_trunc(dialect, d["expr"], d["grain"])
                          for d in dimensions if d.get("grain"))
            sql += f" ORDER BY {time_e}"
        else:
            sql += f" ORDER BY {_agg_sql(metrics[0], dialect)} DESC"
    sql += f" LIMIT {int(limit)}"
    return sql


def explain(model, metrics, dimensions, dialect="ansi"):
    """A plain-language note of exactly how each number was computed — shown on
    the answer so 'revenue' is never a black box."""
    defs = [f"{m['name']} = {_agg_sql(m, dialect)}" for m in metrics]
    by = ", ".join(d["name"] + (f" ({d['grain']})" if d.get("grain") else "") for d in dimensions)
    note = f"From the semantic layer · {', '.join(defs)}"
    if by:
        note += f" · by {by}"
    note += f" · source {model['source']}.{model['table']}"
    return note


# ── Answer a chat turn from the semantic layer ──────────────────────────

def answer(user, source, prompt, table=None):
    """If the prompt resolves to defined metrics, compile + run the canonical
    query and return a chat-shaped result. Returns None to fall through to the
    agent. Never raises — a resolve/compile problem just declines the turn."""
    if not loaded():
        return None
    try:
        match = resolve(source, prompt, table)
    except Exception:
        return None
    if not match:
        return None

    model = match["model"]
    from .connectors import get_connector
    try:
        dialect = get_connector(source).dialect
    except Exception:
        dialect = "ansi"

    try:
        sql = compile_sql(model, match["metrics"], match["dimensions"], dialect)
    except Exception:
        return None

    # Through the SAME gate as any query: RBAC + guard + execution + governance.
    res = queries.verify_sql(user, source, model["table"], sql, full_rows=True)
    if not res.get("ok"):
        # The layer resolved but the query didn't run (permissions, a bad expr):
        # decline rather than surface a broken answer — the agent may still cope.
        return None

    from .agent import _auto_chart
    columns, rows = res["columns"], res["rows"]
    chart = _auto_chart(columns, rows, model["table"]) or {
        "type": "table", "title": model["table"],
        "x": columns[0] if columns else None, "y": columns[1:2]}
    note = explain(model, match["metrics"], match["dimensions"], dialect)
    panel = {"sql": res["sql"], "columns": columns, "rows": rows, "chart": chart}
    return {
        "text": note,
        "sql": res["sql"], "columns": columns, "rows": rows, "chart": chart,
        "panels": [panel], "mode": "semantic", "model": None,
        "served_by": "semantic", "email": None,
        "agents": [roster.SEMANTIC],
        "semantic": {
            "source": model["source"], "table": model["table"],
            "metrics": [{"name": m["name"], "sql": _agg_sql(m, dialect)} for m in match["metrics"]],
            "dimensions": [{"name": d["name"], "grain": d.get("grain")} for d in match["dimensions"]],
        },
    }


# ── Admin API ───────────────────────────────────────────────────────────

def _admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")


TEMPLATE = """\
# Semantic layer — define each business metric ONCE so every question that means
# the same thing compiles to the same SQL and returns the same number.
models:
  - source: demo
    table: sales
    metrics:
      - name: revenue
        agg: sum
        expr: revenue
        format: currency
        synonyms: [sales, turnover, income, earnings]
        description: Total sales revenue
      - name: orders
        agg: count
        expr: "*"
        synonyms: [purchases, transactions, order count]
      - name: units
        agg: sum
        expr: units
        synonyms: [quantity, volume, units sold]
      - name: avg_order_value
        agg: avg
        expr: revenue
        synonyms: [aov, average order value]
    dimensions:
      - name: region
        expr: region
        synonyms: [geography, area, market, location]
      - name: product
        expr: product
        synonyms: [item, sku]
      - name: category
        expr: category
        synonyms: [categories, segment]
      - name: month
        expr: order_date
        grain: month
        synonyms: [over time, monthly, trend]
    default_time: month
"""


@router.get("")
def get_config(user=Depends(current_user)):
    """The active document + a summary. Admins get the raw YAML to edit."""
    # Whichever replica answers, show the document the fleet is converging on.
    _refresh_if_stale()
    doc = _STATE["doc"]
    summary = None
    if doc:
        summary = [{"source": m["source"], "table": m["table"],
                    "metrics": [v["name"] for v in m["metrics"].values()],
                    "dimensions": [v["name"] for v in m["dimensions"].values()]}
                   for m in doc["models"]]
    return {
        "loaded": loaded(),
        "source": active_source(),
        "yaml": current_yaml() if user["role"] == "admin" else None,
        "models": summary,
    }


@router.get("/template")
def get_template(user=Depends(current_user)):
    _admin(user)
    return {"yaml": TEMPLATE}


@router.get("/metrics")
def get_metrics(user=Depends(current_user)):
    """Metrics/dimensions the caller's role can actually use — for the chat UI."""
    return catalog(user)


class YamlIn(BaseModel):
    yaml: str


@router.post("/validate")
def post_validate(body: YamlIn, user=Depends(current_user)):
    _admin(user)
    ok, errors, doc = validate(body.yaml)
    n_metrics = sum(len(m["metrics"]) for m in (doc or {}).get("models", [])) if doc else 0
    return {"ok": ok, "errors": errors,
            "models": len((doc or {}).get("models", [])) if doc else 0,
            "metrics": n_metrics}


@router.put("")
def apply_yaml(body: YamlIn, user=Depends(current_user)):
    _admin(user)
    ok, errors, _ = validate(body.yaml)
    if not ok:
        raise HTTPException(400, {"errors": errors})
    c = db._conn()
    c.execute("INSERT INTO semantic_models (id, yaml, applied_by, applied_at) VALUES (?,?,?,?)",
              (uuid.uuid4().hex, body.yaml, user["email"], time.time()))
    c.commit()
    c.close()
    reload()
    db.log_activity(user, "semantic_apply", ok=loaded())
    return get_config(user)


@router.delete("")
def clear(user=Depends(current_user)):
    _admin(user)
    c = db._conn()
    c.execute("DELETE FROM semantic_models")
    c.commit()
    c.close()
    reload()
    return {"loaded": loaded()}


class CompileIn(BaseModel):
    source: str
    prompt: str
    table: str | None = None


@router.post("/compile")
def post_compile(body: CompileIn, user=Depends(current_user)):
    """Preview: what SQL would this prompt compile to? (No execution.) Lets a
    user see the exact, consistent SQL behind a metric before running it."""
    if not rbac.can_access(user["role"], body.source, body.table or "*"):
        raise HTTPException(404, "No such source")   # no existence oracle
    match = resolve(body.source, body.prompt, body.table)
    if not match:
        return {"resolved": False}
    # Per-table RBAC: a metric may live on a table the caller can't reach even
    # though the source is generally accessible. Treat a denied model as
    # unresolved so /compile can't disclose that table's existence or columns.
    if not rbac.can_access(user["role"], body.source, match["model"]["table"]):
        return {"resolved": False}
    from .connectors import get_connector
    try:
        dialect = get_connector(body.source).dialect
    except Exception:
        dialect = "ansi"
    try:
        sql = compile_sql(match["model"], match["metrics"], match["dimensions"], dialect)
    except ValueError as e:
        return {"resolved": False, "reason": str(e)}
    return {
        "resolved": True, "sql": sql,
        "explain": explain(match["model"], match["metrics"], match["dimensions"], dialect),
        "metrics": [m["name"] for m in match["metrics"]],
        "dimensions": [{"name": d["name"], "grain": d.get("grain")} for d in match["dimensions"]],
    }
