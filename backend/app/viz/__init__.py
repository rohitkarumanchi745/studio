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

Layout (each module imports only from the ones above it, so the graph is
acyclic):
  vocab       enums, caps, key sets, VizError — the source the prompt is built from
  frame       cell coercion, sort keys, aggregation, column typing
  expr        the sandboxed AST expression evaluator behind `derive`
  predicates  match_row / apply_filters — the one filter implementation
  stages      the eleven stage implementations over a {columns, rows, other} frame
  transform   normalize_transform, run_transform, output_columns and friends
  spec        normalize/merge/validate/sanitize the spec, plus SPEC_PROMPT/SPEC_SCHEMA

Callers use `from . import viz` and the names below; the submodules are an
implementation detail and nothing outside this package imports them.
"""
from __future__ import annotations

from .expr import EXPR_FUNCS, compile_expr
from .frame import coerce_compare, distinct_values, dtype_of, field_catalog, infer_fields
from .predicates import apply_filters, match_row
from .spec import (SPEC_PROMPT, SPEC_SCHEMA, llm_contract, merge_spec, normalize_spec,
                   sanitize_spec, validate_spec)
from .transform import (apply_stage, apply_transform, describe_transform, normalize_transform,
                        output_columns, run_transform, suggest_transform)
from .vocab import (AGGS, CATEGORY_ORDERS, CHART_TYPES, DATE_PARTS, FORMAT_KEYS,
                    MAX_DERIVE_SECONDS, MAX_EXPR_CHARS, MAX_EXPR_NODES, MAX_EXPR_STR_CHARS,
                    MAX_GROUPS, MAX_OUTPUT_COLS, MAX_OUTPUT_ROWS, MAX_PIVOT_COLUMNS,
                    MAX_POW_EXPONENT, OPS, SPEC_KEYS, TABLE_CALCS, TRANSFORM_ORDER, VERSION,
                    VizError)

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
