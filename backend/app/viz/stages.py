"""The eleven transform stages, each `fn(frame, cfg, warn) -> frame`.

A frame is {"columns": [...], "rows": [[...]], "other": set()} — `other`
tracks the row positions of any top_n "Other" rows so that later stages
(filters injected post-calc, sort) can keep them pinned last. Every stage
receives cfg ALREADY canonicalised by transform.normalize_transform, so it
reads keys without guards; what it still has to tolerate is data: missing
columns (warn + skip), non-numeric cells, ragged rows.

Stages mutate the frame they are given and return it. The engine in
transform.py owns the fixed order (vocab.TRANSFORM_ORDER) and the error
boundary; nothing here is public.
"""
from __future__ import annotations

import math
import time

from .expr import compile_expr
from .frame import (_aggregate, _category_lut, _cell, _cols, _fmt_num, _hkey, _index, _is_null,
                    _parse_date, _rows, _sortable, _stddev, _to_num, dtype_of)
from .predicates import _resolve_last_n, match_row
from .vocab import (CATEGORY_ORDERS, MAX_DERIVE_SECONDS, MAX_GROUPS, MAX_OUTPUT_COLS,
                    MAX_PIVOT_COLUMNS, OPS, VizError)

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
