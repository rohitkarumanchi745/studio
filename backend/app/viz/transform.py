"""The transform engine: normalize → run the stages in fixed order → introspect.

normalize_transform turns any v1 or v2 shape into the canonical v2 dict the
stages consume (defaults filled, unknown keys dropped). run_transform drives
the stages in vocab.TRANSFORM_ORDER inside one error boundary — a broken stage
warns and is skipped; the chart is never blanked and nothing raises unless
strict=True. Injected filters (slicers, cross-filters) are placed at the
earliest stage where their column exists.

output_columns is the authority on the FINAL column names — `x` and `y` in a
spec name columns of the post-transform frame — and it must stay in this
module: it needs apply_transform to learn pivot column names (they are data),
and run_transform needs it to place injected filters.
"""
from __future__ import annotations

from typing import Any, Optional

from .frame import (_category_lut, _cell, _cols, _hkey, _index, _is_null, _rows, _seq,
                    _sortable, _to_num, dtype_of)
from .stages import _STAGES, _frame
from .vocab import (AGGS, DATE_PARTS, MAX_OUTPUT_COLS, MAX_OUTPUT_ROWS, MAX_PIVOT_COLUMNS, OPS,
                    TABLE_CALCS, VizError)

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
# Introspection over a transform
# ══════════════════════════════════════════════════════════════════════════

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
