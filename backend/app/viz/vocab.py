"""Vocabulary of the v2 chart spec — the single source of truth.

Every enum the spec can express (chart types, predicate ops, aggregations,
table calcs, date parts), every resource cap, and the accepted key sets live
here and nowhere else. The LLM prompt and the JSON-Schema (spec.py) are
GENERATED from these tuples, and every stage validates against them, so the
prompt, the schema and the engine cannot drift apart.

This module imports nothing from the rest of the package: it is the root of
the import graph (vocab ← frame ← expr ← predicates ← stages ← transform ←
spec), which is what keeps that graph acyclic.
"""
from __future__ import annotations

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


class VizError(ValueError):
    """Raised only by compile_expr and by strict=True. Every other path warns."""
