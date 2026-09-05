"""The chart spec: its lifecycle and the contract the LLM writes it against.

Lifecycle — normalize_spec (v1 → v2, never raises), merge_spec (LLM output is
an RFC 7386 merge patch onto the current spec, never a replacement),
validate_spec (human-readable problems) and sanitize_spec (drop everything
that cannot resolve against the real columns, backfill x/y, and NEVER raise —
a hallucinating LLM must degrade, not 500).

Contract — SPEC_PROMPT, the prose agent.py interpolates verbatim into its
system prompts, and SPEC_SCHEMA, the JSON-Schema it binds structured output
to. Both are generated from vocab.py (and EXPR_FUNCS from expr.py) so the
prompt can never advertise an op, agg, calc or function the engine lacks.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from .expr import EXPR_FUNCS, compile_expr
from .frame import _cols, _index, _to_num, dtype_of, infer_fields
from .predicates import _valid_predicates
from .transform import _as_str_list, apply_transform, normalize_transform, output_columns
from .vocab import (AGGS, CHART_TYPES, DATE_PARTS, FORMAT_KEYS, OPS, SPEC_KEYS, TABLE_CALCS,
                    TRANSFORM_ORDER, VERSION, VizError, _CROSS_FILTER_MODES,
                    _INTERACTION_DEFAULTS)

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
    """The LLM-facing contract, generated from vocab.py: the prompt text
    agent.py interpolates into its system prompts and the JSON-Schema it binds
    structured output to. The schema is a copy — a caller may edit its own view of it
    without redefining the spec for everyone else."""
    return {"version": VERSION, "prompt": SPEC_PROMPT, "schema": copy.deepcopy(SPEC_SCHEMA)}
