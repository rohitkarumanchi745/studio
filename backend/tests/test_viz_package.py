"""Equivalence test for the `viz` package.

viz.py was one 2,600-line module; it is now a package. Nothing in this file
imports a submodule — every call goes through `app.viz`, exactly the way
agent.py and dashboards.py use it, so the test pins the PUBLIC surface and the
observable behaviour, not the internal layout.

Every expected value below is a literal captured by running the pre-split
module once (see the scratch generator in the split PR). They are compared
with `==`, floats included: the operations are deterministic, so a changed
float means a changed evaluation order, which is a behaviour change.

If you change viz behaviour ON PURPOSE, regenerate the affected literal and say
so in the commit; never loosen the comparison.
"""
import hashlib
import json
import types

import pytest

import app.viz as viz

# ── The public surface: every non-underscore name the old module exported ──
PUBLIC = ['AGGS', 'CATEGORY_ORDERS', 'CHART_TYPES', 'DATE_PARTS', 'EXPR_FUNCS', 'FORMAT_KEYS',
          'MAX_DERIVE_SECONDS', 'MAX_EXPR_CHARS', 'MAX_EXPR_NODES', 'MAX_EXPR_STR_CHARS',
          'MAX_GROUPS', 'MAX_OUTPUT_COLS', 'MAX_OUTPUT_ROWS', 'MAX_PIVOT_COLUMNS',
          'MAX_POW_EXPONENT', 'OPS', 'SPEC_KEYS', 'SPEC_PROMPT', 'SPEC_SCHEMA', 'TABLE_CALCS',
          'TRANSFORM_ORDER', 'VERSION', 'VizError', 'apply_filters', 'apply_stage',
          'apply_transform', 'coerce_compare', 'compile_expr', 'describe_transform',
          'distinct_values', 'dtype_of', 'field_catalog', 'infer_fields', 'llm_contract',
          'match_row', 'merge_spec', 'normalize_spec', 'normalize_transform', 'output_columns',
          'run_transform', 'sanitize_spec', 'suggest_transform', 'validate_spec']

# Names agent.py / dashboards.py reach through `viz.` — must never disappear.
USED_BY_CALLERS = ("SPEC_PROMPT", "apply_transform", "field_catalog", "merge_spec",
                   "normalize_spec", "run_transform", "sanitize_spec")


# ══════════════════════════════════════════════════════════════════════════
# Fixture frame: a date, a category, two measures, one null — 8 rows
# ══════════════════════════════════════════════════════════════════════════
COLUMNS = ["order_date", "region", "revenue", "units"]
ROWS = [
    ["2024-01-05", "North", 120.5, 3],
    ["2024-01-20", "South", 80, 2],
    ["2024-02-03", "North", 200, 5],
    ["2024-02-14", "East", 50, 1],
    ["2024-02-28", "South", 130, 4],
    ["2024-03-10", "West", 90, 2],
    ["2024-03-22", "North", 60, 1],
    ["2024-03-30", "East", 40, None],
]

T_FULL = {
    "derive": [{"as": "margin", "expr": "`revenue` - nz(`units`, 0) * 10"}],
    "bin": [{"col": "order_date", "as": "month", "date_part": "month"}],
    "filters": [{"col": "region", "op": "ne", "value": "West"}],
    "group": {"by": ["region"], "measures": [
        {"col": "revenue", "agg": "sum", "as": "revenue"},
        {"col": "margin", "agg": "avg", "as": "avg_margin"},
        {"col": "units", "agg": "count"}]},
    "table_calc": [
        {"col": "revenue", "calc": "percent_of_total", "as": "revenue_pct"},
        {"col": "revenue", "calc": "running_total", "as": "revenue_rt",
         "order_by": "revenue", "dir": "desc"}],
    "top_n": {"by": "revenue", "n": 2, "dir": "top",
              "other": {"enabled": True, "dim": "region"}},
    "sort": [{"col": "revenue", "dir": "desc"}],
}
T_TIME = {
    "bin": [{"col": "order_date", "as": "month", "date_part": "month"}],
    "filters": [{"col": "order_date", "op": "last_n", "value": 60, "unit": "day"}],
    "group": {"by": ["month"], "measures": [{"col": "revenue", "agg": "sum", "as": "revenue"}]},
    "table_calc": [{"col": "revenue", "calc": "running_total", "as": "revenue_rt",
                    "order_by": "month", "dir": "asc"}],
    "sort": [{"col": "month", "dir": "asc"}],
}
T_V1 = {"filter": {"column": "region", "op": "eq", "value": "North"},
        "sort": {"column": "revenue", "dir": "desc"}, "top_n": 2,
        "replace": {"column": "region", "find": "North", "replace": "N"}}
T_V1B = {"filter": {"column": "region", "op": "ne", "value": "West"},
         "sort": {"column": "units", "dir": "desc"}, "top_n": 3}
T_PIVOT = {
    "bin": [{"col": "order_date", "as": "month", "date_part": "month"},
            {"col": "revenue", "as": "rev_bin", "size": 100}],
    "group": {"by": ["region", "month"], "measures": [{"col": "units", "agg": "sum", "as": "units"}]},
    "having": [{"col": "units", "op": "gte", "value": 2}],
    "pivot": {"index": ["region"], "columns": "month", "values": "units", "fill": 0},
    "limit": 2,
}
T_UNPIVOT = {
    "unpivot": {"keep": ["region"], "cols": ["revenue", "units"]},
    "group": {"by": ["region", "series"], "measures": [{"col": "value", "agg": "max", "as": "value"}]},
    "table_calc": [{"col": "value", "calc": "rank", "as": "value_rank", "partition_by": ["series"], "dir": "desc"}],
    "sort": [{"col": "series"}, {"col": "value_rank"}],
}
T_BAD = {"derive": [{"as": "x", "expr": "__import__('os')"}],
         "filters": [{"col": "nope", "op": "eq", "value": 1}, {"col": "region", "op": "zzz"}],
         "group": {"by": ["ghost"], "measures": [{"col": "revenue", "agg": "sum"}]},
         "sort": [{"col": "revenue", "dir": "desc"}], "bogus": 1}

SPEC_RAW = {"type": "bar", "title": "Rev", "x": "region", "y": "revenue",
            "transform": T_FULL, "format": {"number": {"revenue": {"style": "currency"},
                                                       "ghost": {"style": "percent"}},
                                            "sort": "desc", "labels": {"show": True, "from_col": "revenue_pct"}},
            "interaction": {"cross_filter": "highlight", "drill": {"hierarchy": ["region"], "level": "1"}},
            "extra_key": True}
PATCH = {"title": "Rev v2", "y": ["revenue_rt"], "format": {"number": {"revenue": None},
         "legend": {"show": True}}, "transform": {"sort": [{"col": "revenue", "dir": "asc"}]}}
SPEC_BAD = {"type": "rocket", "x": "ghost", "y": ["nope", "revenue"], "transform": T_BAD,
            "format": {"number": {"ghost": {}}, "weird": 1}}

EXPRS = ["`revenue` * 2 + units", "round(revenue / 7, 2)", "'r:' + region", "upper(region) if revenue > 100 else lower(region)",
         "month(order_date)", "weekday(order_date)", "year(order_date) == 2024 and units in [1, 2]",
         "safe_div(revenue, units)", "2 ** 3", "-units", "not units", "coalesce(units, 'none')",
         "clamp(revenue, 60, 150)", "contains(region, 'or')", "len(region)", "to_date(order_date)",
         "revenue > units > 0", "max(revenue, units)", "log(revenue, 10)", "trim('  x  ')", "1 / 0"]
BAD_EXPRS = ["", "x" * 2001, "import os", "__import__('os')", "revenue.real", "revenue[0]", "lambda: 1",
             "ghost + 1", "2 ** 99", "foo(1)", "round(x=1)", "round(*[1])", "{1: 2}", "1 if 1 else (yield)"]
PREDS = [{"col": "region", "op": "eq", "value": "north"}, {"col": "revenue", "op": "gt", "value": "100"},
         {"col": "units", "op": "isnull"}, {"col": "units", "op": "in", "values": [1, "3"]},
         {"col": "units", "op": "nin", "values": [1]}, {"col": "units", "op": "ne", "value": 1},
         {"col": "revenue", "op": "between", "lo": 60, "hi": 120.5}, {"col": "region", "op": "contains", "value": "OU"},
         {"col": "region", "op": "startswith", "value": "n"}, {"col": "region", "op": "endswith", "value": "T"},
         {"col": "region", "op": "ncontains", "value": "s"}, {"col": "region", "op": "notnull"},
         {"col": "order_date", "op": "last_n", "value": 30, "unit": "day"}, {"col": "ghost", "op": "eq", "value": 1},
         {"col": "region", "op": "zzz"}, {"col": "region"}, "junk"]

NAMED_TRANSFORMS = [("full", T_FULL), ("time", T_TIME), ("v1", T_V1), ("v1b", T_V1B),
                    ("pivot", T_PIVOT), ("unpivot", T_UNPIVOT), ("bad", T_BAD), ("none", None)]
INJECTED = [{"col": "month", "op": "in", "values": ["2024-02", "2024-03"]},
            {"col": "revenue_pct", "op": "gt", "value": 0.1},
            {"col": "ghost", "op": "eq", "value": 1},
            {"col": "region", "op": "zzz"}]
SUGGEST_SPECS = [{"x": "region", "y": ["revenue", "units"]}, {"x": "order_date", "y": "revenue"},
                 {"x": "revenue", "y": ["region"]}]
CATALOG_SPEC = {"fields": {"measures": [{"col": "units", "label": "Units sold"}]}}
COERCE_PAIRS = [(1, 2), ("2", 1), ("b", "a"), ("a", "a"), (None, 1), ("x", 1), (1, "x"), ("1,000", 999)]
FILTER_LIST = [PREDS[12], {"col": "revenue", "op": "gt", "value": 50}, {"col": "ghost"},
               {"col": "revenue", "op": "last_n", "value": 100}, "junk"]


# ══════════════════════════════════════════════════════════════════════════
# Golden literals (captured from the pre-split module)
# ══════════════════════════════════════════════════════════════════════════
NORM_FULL = {'derive': [{'as': 'margin',
             'expr': '`revenue` - nz(`units`, 0) * 10',
             'kind': 'measure',
             'agg': 'sum'}],
 'bin': [{'col': 'order_date',
          'as': 'month',
          'date_part': 'month',
          'size': None,
          'count': None,
          'labels': 'range'}],
 'filters': [{'col': 'region', 'op': 'ne', 'value': 'West', 'origin': 'spec'}],
 'group': {'by': ['region'],
           'measures': [{'col': 'revenue', 'agg': 'sum', 'as': 'revenue'},
                        {'col': 'margin', 'agg': 'avg', 'as': 'avg_margin'},
                        {'col': 'units', 'agg': 'count', 'as': 'count'}],
           'keep_other_cols': False},
 'table_calc': [{'col': 'revenue',
                 'calc': 'percent_of_total',
                 'as': 'revenue_pct',
                 'partition_by': [],
                 'order_by': None,
                 'dir': 'asc',
                 'window': 3,
                 'stage': 'pre_top_n'},
                {'col': 'revenue',
                 'calc': 'running_total',
                 'as': 'revenue_rt',
                 'partition_by': [],
                 'order_by': 'revenue',
                 'dir': 'desc',
                 'window': 3,
                 'stage': 'pre_top_n'}],
 'top_n': {'by': 'revenue',
           'n': 2,
           'dir': 'top',
           'within': [],
           'other': {'enabled': True, 'label': 'Other', 'dim': 'region', 'agg': 'sum'}},
 'sort': [{'col': 'revenue', 'dir': 'desc', 'nulls': 'last'}]}

NORM_V1 = {'derive': [{'as': 'region',
             'expr': "replace(`region`, 'North', 'N')",
             'kind': 'dimension',
             'agg': 'sum'}],
 'filters': [{'col': 'region', 'op': 'eq', 'value': 'North', 'origin': 'spec'}],
 'top_n': {'by': None,
           'n': 2,
           'dir': 'top',
           'within': [],
           'other': {'enabled': False, 'label': 'Other', 'dim': None, 'agg': 'sum'}},
 'sort': [{'col': 'revenue', 'dir': 'desc', 'nulls': 'last'}]}

NORM_BAD = {'derive': [{'as': 'x', 'expr': "__import__('os')", 'kind': 'measure', 'agg': 'sum'}],
 'filters': [{'col': 'nope', 'op': 'eq', 'value': 1, 'origin': 'spec'},
             {'col': 'region', 'op': 'eq', 'origin': 'spec'}],
 'group': {'by': ['ghost'],
           'measures': [{'col': 'revenue', 'agg': 'sum', 'as': 'revenue'}],
           'keep_other_cols': False},
 'sort': [{'col': 'revenue', 'dir': 'desc', 'nulls': 'last'}]}

RUN_FULL = {'columns': ['region', 'revenue', 'avg_margin', 'count', 'revenue_pct', 'revenue_rt'],
 'rows': [['North', 380.5, 96.83333333333333, 3, 0.559147685525349, 380.5],
          ['South', 210.0, 75.0, 2, 0.3085966201322557, 590.5],
          ['Other', 90.0, 40.0, 2.0, 0.13225569434239529, 680.5]],
 'applied': ['derive', 'bin', 'filters', 'group', 'table_calc', 'top_n', 'sort'],
 'injected': [],
 'skipped': [],
 'warnings': [],
 'in_rows': 8,
 'out_rows': 3,
 'truncated': False}

APPLY_TIME = (['month', 'revenue', 'revenue_rt'], [['2024-02', 380.0, 380.0], ['2024-03', 190.0, 570.0]])

APPLY_V1 = (['order_date', 'region', 'revenue', 'units'], [])

APPLY_V1B = (['order_date', 'region', 'revenue', 'units'],
 [['2024-02-03', 'North', 200, 5],
  ['2024-02-28', 'South', 130, 4],
  ['2024-01-05', 'North', 120.5, 3]])

NORM_V1B = {'filters': [{'col': 'region', 'op': 'ne', 'value': 'West', 'origin': 'spec'}],
 'top_n': {'by': None,
           'n': 3,
           'dir': 'top',
           'within': [],
           'other': {'enabled': False, 'label': 'Other', 'dim': None, 'agg': 'sum'}},
 'sort': [{'col': 'units', 'dir': 'desc', 'nulls': 'last'}]}

APPLY_PIVOT = (['region', '2024-01', '2024-02', '2024-03'], [['North', 3.0, 5.0, 0], ['South', 2.0, 4.0, 0]])

APPLY_UNPIVOT = (['region', 'series', 'value', 'value_rank'],
 [['North', 'revenue', 200, 1],
  ['South', 'revenue', 130, 2],
  ['West', 'revenue', 90, 3],
  ['East', 'revenue', 50, 4],
  ['North', 'units', 5, 1],
  ['South', 'units', 4, 2],
  ['West', 'units', 2, 3],
  ['East', 'units', 1, 4]])

RUN_BAD = {'columns': ['revenue'],
 'rows': [],
 'applied': ['derive', 'filters', 'group', 'sort'],
 'injected': [],
 'skipped': [],
 'warnings': ["derive 'x': unknown function '__import__' — skipped",
              "filters: 'nope' is not filterable here — dropped",
              "group: column 'ghost' not found — ignored"],
 'in_rows': 8,
 'out_rows': 0,
 'truncated': False}

RUN_INJECTED = {'columns': ['region', 'revenue', 'avg_margin', 'count', 'revenue_pct', 'revenue_rt'],
 'rows': [['North', 260.0, 100.0, 2, 0.5416666666666666, 260.0],
          ['South', 130.0, 90.0, 1, 0.2708333333333333, 390.0]],
 'applied': ['derive', 'bin', 'filters', 'group', 'table_calc', 'top_n', 'sort'],
 'injected': [{'col': 'month',
               'op': 'in',
               'values': ['2024-02', '2024-03'],
               'stage': 'pre_group'},
              {'col': 'revenue_pct', 'op': 'gt', 'value': 0.1, 'stage': 'post_calc'}],
 'skipped': [{'col': 'ghost', 'reason': 'column not present in this view'},
             {'col': 'region', 'reason': 'unknown filter op'}],
 'warnings': ['result truncated to 2 rows'],
 'in_rows': 8,
 'out_rows': 2,
 'truncated': True}

STAGE_BIN = (['order_date', 'region', 'revenue', 'units', 'b'],
 [['2024-01-05', 'North', 120.5, 3, 120.00000000000001],
  ['2024-01-20', 'South', 80, 2, 66.66666666666667],
  ['2024-02-03', 'North', 200, 5, 173.33333333333337],
  ['2024-02-14', 'East', 50, 1, 66.66666666666667],
  ['2024-02-28', 'South', 130, 4, 120.00000000000001],
  ['2024-03-10', 'West', 90, 2, 66.66666666666667],
  ['2024-03-22', 'North', 60, 1, 66.66666666666667],
  ['2024-03-30', 'East', 40, None, 66.66666666666667]])

STAGE_UNKNOWN = (['order_date', 'region', 'revenue', 'units'],
 [['2024-01-05', 'North', 120.5, 3],
  ['2024-01-20', 'South', 80, 2],
  ['2024-02-03', 'North', 200, 5],
  ['2024-02-14', 'East', 50, 1],
  ['2024-02-28', 'South', 130, 4],
  ['2024-03-10', 'West', 90, 2],
  ['2024-03-22', 'North', 60, 1],
  ['2024-03-30', 'East', 40, None]])

OUTPUT_COLS = {'full': ['region', 'revenue', 'avg_margin', 'count', 'revenue_pct', 'revenue_rt'],
 'time': ['month', 'revenue', 'revenue_rt'],
 'v1': ['order_date', 'region', 'revenue', 'units'],
 'v1b': ['order_date', 'region', 'revenue', 'units'],
 'pivot': ['region', '2024-01'],
 'unpivot': ['region', 'series', 'value', 'value_rank'],
 'bad': ['revenue'],
 'none': ['order_date', 'region', 'revenue', 'units']}

DESCRIBE = {'full': 'sum(revenue), avg(margin), count by region · by month · margin = `revenue` - '
         'nz(`units`, 0) * 10 · 1 filter · % of total · running total · top 2 +Other · sorted '
         'by revenue desc',
 'time': 'sum(revenue) by month · by month · 1 filter · running total · sorted by month asc',
 'v1': "region = replace(`region`, 'North', 'N') · 1 filter · top 2 · sorted by revenue desc",
 'v1b': '1 filter · top 3 · sorted by units desc',
 'pivot': 'sum(units) by region, month · by month · revenue binned · 1 having · pivoted on '
          'month · limit 2',
 'unpivot': 'max(value) by region, series · rank · unpivoted · sorted by series asc · sorted '
            'by value_rank asc',
 'none': ''}

SUGGEST = [{'group': {'by': ['region'],
            'measures': [{'col': 'revenue', 'agg': 'sum', 'as': 'revenue'},
                         {'col': 'units', 'agg': 'sum', 'as': 'units'}]}},
 {},
 {}]

NORM_SPEC = {'v': 2,
 'type': 'bar',
 'title': 'Rev',
 'x': 'region',
 'y': ['revenue'],
 'fields': {},
 'transform': {'derive': [{'as': 'margin',
                           'expr': '`revenue` - nz(`units`, 0) * 10',
                           'kind': 'measure',
                           'agg': 'sum'}],
               'bin': [{'col': 'order_date',
                        'as': 'month',
                        'date_part': 'month',
                        'size': None,
                        'count': None,
                        'labels': 'range'}],
               'filters': [{'col': 'region', 'op': 'ne', 'value': 'West', 'origin': 'spec'}],
               'group': {'by': ['region'],
                         'measures': [{'col': 'revenue', 'agg': 'sum', 'as': 'revenue'},
                                      {'col': 'margin', 'agg': 'avg', 'as': 'avg_margin'},
                                      {'col': 'units', 'agg': 'count', 'as': 'count'}],
                         'keep_other_cols': False},
               'table_calc': [{'col': 'revenue',
                               'calc': 'percent_of_total',
                               'as': 'revenue_pct',
                               'partition_by': [],
                               'order_by': None,
                               'dir': 'asc',
                               'window': 3,
                               'stage': 'pre_top_n'},
                              {'col': 'revenue',
                               'calc': 'running_total',
                               'as': 'revenue_rt',
                               'partition_by': [],
                               'order_by': 'revenue',
                               'dir': 'desc',
                               'window': 3,
                               'stage': 'pre_top_n'}],
               'top_n': {'by': 'revenue',
                         'n': 2,
                         'dir': 'top',
                         'within': [],
                         'other': {'enabled': True,
                                   'label': 'Other',
                                   'dim': 'region',
                                   'agg': 'sum'}},
               'sort': [{'col': 'revenue', 'dir': 'desc', 'nulls': 'last'}]},
 'format': {'number': {'revenue': {'style': 'currency'}, 'ghost': {'style': 'percent'}},
            'labels': {'show': True, 'from_col': 'revenue_pct'}},
 'interaction': {'cross_filter': 'highlight',
                 'affected_by': 'auto',
                 'self_highlight': True,
                 'multi_select': True,
                 'drill': {'hierarchy': ['region'], 'level': 1}}}

NORM_SPEC_EMPTY = {'v': 2,
 'type': 'bar',
 'title': '',
 'x': None,
 'y': [],
 'fields': {},
 'transform': {},
 'format': {},
 'interaction': {'cross_filter': 'filter',
                 'affected_by': 'auto',
                 'self_highlight': True,
                 'multi_select': True,
                 'drill': {'hierarchy': [], 'level': 0}}}

MERGED = {'v': 2,
 'type': 'bar',
 'title': 'Rev v2',
 'x': 'region',
 'y': ['revenue_rt'],
 'fields': {},
 'transform': {'derive': [{'as': 'margin',
                           'expr': '`revenue` - nz(`units`, 0) * 10',
                           'kind': 'measure',
                           'agg': 'sum'}],
               'bin': [{'col': 'order_date',
                        'as': 'month',
                        'date_part': 'month',
                        'size': None,
                        'count': None,
                        'labels': 'range'}],
               'filters': [{'col': 'region', 'op': 'ne', 'value': 'West', 'origin': 'spec'}],
               'group': {'by': ['region'],
                         'measures': [{'col': 'revenue', 'agg': 'sum', 'as': 'revenue'},
                                      {'col': 'margin', 'agg': 'avg', 'as': 'avg_margin'},
                                      {'col': 'units', 'agg': 'count', 'as': 'count'}],
                         'keep_other_cols': False},
               'table_calc': [{'col': 'revenue',
                               'calc': 'percent_of_total',
                               'as': 'revenue_pct',
                               'partition_by': [],
                               'order_by': None,
                               'dir': 'asc',
                               'window': 3,
                               'stage': 'pre_top_n'},
                              {'col': 'revenue',
                               'calc': 'running_total',
                               'as': 'revenue_rt',
                               'partition_by': [],
                               'order_by': 'revenue',
                               'dir': 'desc',
                               'window': 3,
                               'stage': 'pre_top_n'}],
               'top_n': {'by': 'revenue',
                         'n': 2,
                         'dir': 'top',
                         'within': [],
                         'other': {'enabled': True,
                                   'label': 'Other',
                                   'dim': 'region',
                                   'agg': 'sum'}},
               'sort': [{'col': 'revenue', 'dir': 'asc', 'nulls': 'last'}]},
 'format': {'number': {'ghost': {'style': 'percent'}},
            'labels': {'show': True, 'from_col': 'revenue_pct'},
            'legend': {'show': True}},
 'interaction': {'cross_filter': 'highlight',
                 'affected_by': 'auto',
                 'self_highlight': True,
                 'multi_select': True,
                 'drill': {'hierarchy': ['region'], 'level': 1}}}

SANITIZED = ({'v': 2,
  'type': 'bar',
  'title': 'Rev',
  'x': 'region',
  'y': ['revenue'],
  'fields': {},
  'transform': {'derive': [{'as': 'margin',
                            'expr': '`revenue` - nz(`units`, 0) * 10',
                            'kind': 'measure',
                            'agg': 'sum'}],
                'bin': [{'col': 'order_date',
                         'as': 'month',
                         'date_part': 'month',
                         'size': None,
                         'count': None,
                         'labels': 'range'}],
                'filters': [{'col': 'region', 'op': 'ne', 'value': 'West', 'origin': 'spec'}],
                'group': {'by': ['region'],
                          'measures': [{'col': 'revenue', 'agg': 'sum', 'as': 'revenue'},
                                       {'col': 'margin', 'agg': 'avg', 'as': 'avg_margin'},
                                       {'col': 'units', 'agg': 'count', 'as': 'count'}],
                          'keep_other_cols': False},
                'table_calc': [{'col': 'revenue',
                                'calc': 'percent_of_total',
                                'as': 'revenue_pct',
                                'partition_by': [],
                                'order_by': None,
                                'dir': 'asc',
                                'window': 3,
                                'stage': 'pre_top_n'},
                               {'col': 'revenue',
                                'calc': 'running_total',
                                'as': 'revenue_rt',
                                'partition_by': [],
                                'order_by': 'revenue',
                                'dir': 'desc',
                                'window': 3,
                                'stage': 'pre_top_n'}],
                'top_n': {'by': 'revenue',
                          'n': 2,
                          'dir': 'top',
                          'within': [],
                          'other': {'enabled': True,
                                    'label': 'Other',
                                    'dim': 'region',
                                    'agg': 'sum'}},
                'sort': [{'col': 'revenue', 'dir': 'desc', 'nulls': 'last'}]},
  'format': {'number': {'revenue': {'style': 'currency'}},
             'labels': {'show': True, 'from_col': 'revenue_pct'}},
  'interaction': {'cross_filter': 'highlight',
                  'affected_by': 'auto',
                  'self_highlight': True,
                  'multi_select': True,
                  'drill': {'hierarchy': ['region'], 'level': 1}}},
 ["'extra_key' is not a chart spec key — dropped",
  "format.number['ghost'] references a column this chart does not produce — dropped"])

SANITIZED_BAD = ({'v': 2,
  'type': 'bar',
  'title': '',
  'x': 'order_date',
  'y': ['revenue'],
  'fields': {},
  'transform': {'filters': [{'col': 'region', 'op': 'eq', 'origin': 'spec'}],
                'sort': [{'col': 'revenue', 'dir': 'desc', 'nulls': 'last'}]},
  'format': {'number': {}},
  'interaction': {'cross_filter': 'filter',
                  'affected_by': 'auto',
                  'self_highlight': True,
                  'multi_select': True,
                  'drill': {'hierarchy': [], 'level': 0}}},
 ["derive 'x': unknown function '__import__' — dropped",
  "filters: column 'nope' is not available — predicate dropped",
  "group: column 'ghost' does not exist — dropped",
  'group: no valid group-by column — stage dropped',
  "unknown chart type 'rocket' — using bar",
  "x column 'ghost' is not produced by this transform — replaced",
  "y column 'nope' is not produced by this transform — dropped",
  "format.number['ghost'] references a column this chart does not produce — dropped"])

VALIDATE_BAD = ["chart type 'rocket' is not one of bar, hbar, stacked_bar, line, area, stacked_area, combo, "
 'pie, donut, scatter, bubble, heatmap, treemap, funnel, radar, gauge, kpi, histogram, '
 'boxplot, waterfall, sankey, table',
 "derive 'x': unknown function '__import__'",
 "filters column 'nope' does not exist",
 "group by column 'ghost' does not exist",
 'x column "ghost" is not produced by this transform',
 'y column "nope" is not produced by this transform',
 'format.number references unknown column "ghost"',
 'format.weird is not a known key']

VALIDATE_OK = ['format.number references unknown column "ghost"', 'format.sort is not a known key']

CATALOG = [{'col': 'order_date',
  'label': 'order_date',
  'kind': 'dimension',
  'dtype': 'date',
  'role': 'time',
  'values': ['2024-01-05',
             '2024-01-20',
             '2024-02-03',
             '2024-02-14',
             '2024-02-28',
             '2024-03-10',
             '2024-03-22',
             '2024-03-30'],
  'value_count': 8,
  'min': '2024-01-05',
  'max': '2024-03-30'},
 {'col': 'region',
  'label': 'region',
  'kind': 'dimension',
  'dtype': 'string',
  'role': 'category',
  'values': ['East', 'North', 'South', 'West'],
  'value_count': 4,
  'min': None,
  'max': None},
 {'col': 'revenue',
  'label': 'revenue',
  'kind': 'measure',
  'dtype': 'number',
  'role': 'measure',
  'values': None,
  'value_count': 8,
  'min': 40,
  'max': 200},
 {'col': 'units',
  'label': 'Units sold',
  'kind': 'measure',
  'dtype': 'number',
  'role': 'measure',
  'values': None,
  'value_count': 5,
  'min': 1,
  'max': 5}]

INFER = {'dimensions': [{'col': 'order_date', 'label': 'order_date', 'dtype': 'date', 'role': 'time'},
                {'col': 'region', 'label': 'region', 'dtype': 'string', 'role': 'category'}],
 'measures': [{'col': 'revenue',
               'label': 'revenue',
               'dtype': 'number',
               'agg': 'sum',
               'format': 'number'},
              {'col': 'units',
               'label': 'units',
               'dtype': 'number',
               'agg': 'sum',
               'format': 'number'}]}

DISTINCT = [['East', 'North', 'South', 'West'], [1, 2, 3, 4, 5], []]

DTYPES = ['empty', 'date', 'string', 'number', 'number', 'empty']

EXPR_VALUES = {'`revenue` * 2 + units': [244.0, 162.0, 405.0, 101.0, 264.0, 182.0, 121.0, None],
 'round(revenue / 7, 2)': [17.21, 11.43, 28.57, 7.14, 18.57, 12.86, 8.57, 5.71],
 "'r:' + region": ['r:North',
                   'r:South',
                   'r:North',
                   'r:East',
                   'r:South',
                   'r:West',
                   'r:North',
                   'r:East'],
 'upper(region) if revenue > 100 else lower(region)': ['NORTH',
                                                       'south',
                                                       'NORTH',
                                                       'east',
                                                       'SOUTH',
                                                       'west',
                                                       'north',
                                                       'east'],
 'month(order_date)': [1, 1, 2, 2, 2, 3, 3, 3],
 'weekday(order_date)': ['Fri', 'Sat', 'Sat', 'Wed', 'Wed', 'Sun', 'Fri', 'Sat'],
 'year(order_date) == 2024 and units in [1, 2]': [False,
                                                  True,
                                                  False,
                                                  True,
                                                  False,
                                                  True,
                                                  True,
                                                  False],
 'safe_div(revenue, units)': [40.166666666666664, 40.0, 40.0, 50.0, 32.5, 45.0, 60.0, None],
 '2 ** 3': [8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0, 8.0],
 '-units': [-3.0, -2.0, -5.0, -1.0, -4.0, -2.0, -1.0, None],
 'not units': [False, False, False, False, False, False, False, True],
 "coalesce(units, 'none')": [3, 2, 5, 1, 4, 2, 1, 'none'],
 'clamp(revenue, 60, 150)': [120.5, 80.0, 150.0, 60.0, 130.0, 90.0, 60.0, 60.0],
 "contains(region, 'or')": [True, False, True, False, False, False, True, False],
 'len(region)': [5, 5, 5, 4, 5, 4, 5, 4],
 'to_date(order_date)': ['2024-01-05',
                         '2024-01-20',
                         '2024-02-03',
                         '2024-02-14',
                         '2024-02-28',
                         '2024-03-10',
                         '2024-03-22',
                         '2024-03-30'],
 'revenue > units > 0': [True, True, True, True, True, True, True, False],
 'max(revenue, units)': [120.5, 80.0, 200.0, 50.0, 130.0, 90.0, 60.0, 40.0],
 'log(revenue, 10)': [2.0809870469108867,
                      1.9030899869919433,
                      2.301029995663981,
                      1.6989700043360185,
                      2.1139433523068365,
                      1.9542425094393248,
                      1.7781512503836434,
                      1.6020599913279623],
 "trim('  x  ')": ['x', 'x', 'x', 'x', 'x', 'x', 'x', 'x'],
 '1 / 0': [None, None, None, None, None, None, None, None]}

EXPR_ERRORS = {'': 'expression is empty',
 'xxxxxxxxxxxxxxxxxxxx': 'expression exceeds 2000 characters',
 'import os': 'cannot parse expression: invalid syntax (<unknown>, line 1)',
 "__import__('os')": "unknown function '__import__'",
 'revenue.real': 'Attribute is not allowed in an expression',
 'revenue[0]': 'Subscript is not allowed in an expression',
 'lambda: 1': 'Lambda is not allowed in an expression',
 'ghost + 1': "unknown column 'ghost' in expression",
 '2 ** 99': 'exponent must be a number ≤ 8',
 'foo(1)': "unknown function 'foo'",
 'round(x=1)': 'keyword arguments are not allowed',
 'round(*[1])': 'argument unpacking is not allowed',
 '{1: 2}': 'Dict is not allowed in an expression',
 '1 if 1 else (yield)': 'Yield is not allowed in an expression'}

COERCE = [-1, 1, 1, 0, None, None, None, 1]

MATCH = [[True, False, True, False, False, False, True, False],
 [True, False, True, False, True, False, False, False],
 [False, False, False, False, False, False, False, True],
 [True, False, False, True, False, False, True, False],
 [True, True, True, False, True, True, False, True],
 [True, True, True, False, True, True, False, True],
 [True, True, False, False, False, True, True, False],
 [False, True, False, False, True, False, False, False],
 [True, False, True, False, False, False, True, False],
 [False, False, False, True, False, True, False, True],
 [True, False, True, False, False, False, True, False],
 [True, True, True, True, True, True, True, True],
 [True, True, True, True, True, True, True, True],
 [False, False, False, False, False, False, False, False],
 [False, False, False, False, False, False, False, False],
 [False, False, False, False, False, False, False, False],
 [False, False, False, False, False, False, False, False]]

FILTERED = (['order_date', 'region', 'revenue', 'units'],
 [['2024-03-10', 'West', 90, 2], ['2024-03-22', 'North', 60, 1]])

PROMPT_LEN = 10264
PROMPT_FIRST = 'THE SPEC'
PROMPT_SHA = '8f0d2ab6ac2500bb4f477746bf441c7c03e2dbbfe5e57c53ab24cd19f6688ff3'
SCHEMA_SHA = '77a4af3fe74fd3cffd1a4b0aabd00d2683336fd3225ca775b0a8683454e53d9f'
VERSION = 2
TRANSFORM_ORDER = ('derive', 'bin', 'filters', 'unpivot', 'group', 'having', 'table_calc', 'top_n', 'sort', 'pivot', 'limit')
CHART_TYPES = ('bar', 'hbar', 'stacked_bar', 'line', 'area', 'stacked_area', 'combo', 'pie', 'donut', 'scatter', 'bubble', 'heatmap', 'treemap', 'funnel', 'radar', 'gauge', 'kpi', 'histogram', 'boxplot', 'waterfall', 'sankey', 'table')
OPS = ('eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'nin', 'between', 'contains', 'ncontains', 'startswith', 'endswith', 'isnull', 'notnull', 'last_n')
AGGS = ('sum', 'avg', 'min', 'max', 'count', 'count_distinct', 'median', 'p25', 'p75', 'p90', 'stddev', 'first', 'last')
TABLE_CALCS = ('percent_of_total', 'running_total', 'cumulative_percent', 'difference', 'percent_difference', 'rank', 'dense_rank', 'percentile', 'moving_average', 'index', 'percent_of_max', 'z_score')
DATE_PARTS = ('hour', 'day', 'week', 'month', 'quarter', 'year', 'weekday')
CATEGORY_ORDERS = {'weekday': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'], 'month': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 'quarter': ['Q1', 'Q2', 'Q3', 'Q4']}
SPEC_KEYS = ('v', 'type', 'title', 'x', 'y', 'fields', 'transform', 'format', 'interaction')
FORMAT_KEYS = ('number', 'date', 'labels', 'axes', 'legend', 'palette', 'series', 'reference_lines', 'conditional_colors', 'annotations', 'title', 'tooltip', 'data_zoom', 'totals', 'empty_text')
MAX_OUTPUT_ROWS = 50000
MAX_OUTPUT_COLS = 512
MAX_PIVOT_COLUMNS = 50
MAX_GROUPS = 5000
MAX_EXPR_CHARS = 2000
MAX_EXPR_NODES = 500
MAX_POW_EXPONENT = 8
MAX_EXPR_STR_CHARS = 64000
MAX_DERIVE_SECONDS = 2.0
EXPR_FUNC_NAMES = ['abs', 'ceil', 'clamp', 'coalesce', 'contains', 'day', 'endswith', 'floor', 'iif', 'len', 'log', 'lower', 'max', 'min', 'month', 'nz', 'replace', 'round', 'safe_div', 'sqrt', 'startswith', 'to_date', 'to_number', 'trim', 'upper', 'weekday', 'year']


# ══════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════

def test_public_surface_is_unchanged():
    assert sorted(viz.__all__) == PUBLIC
    for name in PUBLIC:
        assert hasattr(viz, name), name
    for name in USED_BY_CALLERS:
        assert name in PUBLIC
    # Nothing new leaks: every viz-defined callable without an underscore is in PUBLIC.
    leaked = sorted(n for n in dir(viz)
                    if not n.startswith("_") and n not in PUBLIC
                    and not isinstance(getattr(viz, n), types.ModuleType)
                    and str(getattr(getattr(viz, n), "__module__", "")).startswith("app.viz"))
    assert leaked == []
    assert issubclass(viz.VizError, ValueError)


def test_vocabulary_tables():
    assert viz.VERSION == VERSION
    assert viz.TRANSFORM_ORDER == TRANSFORM_ORDER
    assert viz.CHART_TYPES == CHART_TYPES
    assert viz.OPS == OPS
    assert viz.AGGS == AGGS
    assert viz.TABLE_CALCS == TABLE_CALCS
    assert viz.DATE_PARTS == DATE_PARTS
    assert viz.CATEGORY_ORDERS == CATEGORY_ORDERS
    assert viz.SPEC_KEYS == SPEC_KEYS
    assert viz.FORMAT_KEYS == FORMAT_KEYS
    assert sorted(viz.EXPR_FUNCS) == EXPR_FUNC_NAMES
    assert (viz.MAX_OUTPUT_ROWS, viz.MAX_OUTPUT_COLS, viz.MAX_PIVOT_COLUMNS, viz.MAX_GROUPS) == \
        (MAX_OUTPUT_ROWS, MAX_OUTPUT_COLS, MAX_PIVOT_COLUMNS, MAX_GROUPS)
    assert (viz.MAX_EXPR_CHARS, viz.MAX_EXPR_NODES, viz.MAX_POW_EXPONENT,
            viz.MAX_EXPR_STR_CHARS, viz.MAX_DERIVE_SECONDS) == \
        (MAX_EXPR_CHARS, MAX_EXPR_NODES, MAX_POW_EXPONENT, MAX_EXPR_STR_CHARS, MAX_DERIVE_SECONDS)


def test_llm_contract_prompt_and_schema():
    assert len(viz.SPEC_PROMPT) == PROMPT_LEN
    assert viz.SPEC_PROMPT.splitlines()[0] == PROMPT_FIRST
    assert hashlib.sha256(viz.SPEC_PROMPT.encode()).hexdigest() == PROMPT_SHA
    assert hashlib.sha256(json.dumps(viz.SPEC_SCHEMA, sort_keys=True).encode()).hexdigest() == SCHEMA_SHA
    c = viz.llm_contract()
    assert sorted(c) == ["prompt", "schema", "version"]
    assert c["version"] == VERSION and c["prompt"] == viz.SPEC_PROMPT
    assert c["schema"] == viz.SPEC_SCHEMA and c["schema"] is not viz.SPEC_SCHEMA


def test_normalize_transform():
    assert viz.normalize_transform(T_FULL) == NORM_FULL
    assert viz.normalize_transform(T_V1) == NORM_V1
    assert viz.normalize_transform(T_V1B) == NORM_V1B
    assert viz.normalize_transform(T_BAD) == NORM_BAD
    assert viz.normalize_transform(None) == {} and viz.normalize_transform("junk") == {}


def test_run_transform_full_pipeline():
    rows_before = [list(r) for r in ROWS]
    assert viz.run_transform(COLUMNS, ROWS, T_FULL) == RUN_FULL
    assert ROWS == rows_before, "run_transform must never mutate its input"


def test_apply_transform_variants():
    assert viz.apply_transform(COLUMNS, ROWS, T_TIME) == APPLY_TIME
    assert viz.apply_transform(COLUMNS, ROWS, T_V1) == APPLY_V1
    assert viz.apply_transform(COLUMNS, ROWS, T_V1B) == APPLY_V1B
    assert viz.apply_transform(COLUMNS, ROWS, T_PIVOT) == APPLY_PIVOT
    assert viz.apply_transform(COLUMNS, ROWS, T_UNPIVOT) == APPLY_UNPIVOT
    assert viz.apply_transform(COLUMNS, ROWS, None) == (COLUMNS, ROWS)


def test_run_transform_degrades_and_injects():
    assert viz.run_transform(COLUMNS, ROWS, T_BAD) == RUN_BAD
    assert viz.run_transform(COLUMNS, ROWS, T_FULL, injected=INJECTED, max_rows=2) == RUN_INJECTED
    with pytest.raises(viz.VizError):
        viz.run_transform(COLUMNS, ROWS, T_BAD, strict=True)


def test_apply_stage():
    assert viz.apply_stage("bin", COLUMNS, ROWS,
                           [{"col": "revenue", "as": "b", "count": 3, "labels": "mid"}]) == STAGE_BIN
    assert viz.apply_stage("nope", COLUMNS, ROWS, {"a": 1}) == STAGE_UNKNOWN
    assert viz.apply_stage("sort", COLUMNS, ROWS, None) == STAGE_UNKNOWN


def test_introspection():
    assert {k: viz.output_columns(COLUMNS, t, ROWS) for k, t in NAMED_TRANSFORMS} == OUTPUT_COLS
    assert {k: viz.describe_transform(t) for k, t in NAMED_TRANSFORMS if k != "bad"} == DESCRIBE
    assert [viz.suggest_transform(COLUMNS, ROWS, s) for s in SUGGEST_SPECS] == SUGGEST
    assert viz.field_catalog(COLUMNS, ROWS, CATALOG_SPEC) == CATALOG
    assert viz.infer_fields(COLUMNS, ROWS) == INFER
    assert [viz.distinct_values(COLUMNS, ROWS, c) for c in ("region", "units", "nope")] == DISTINCT
    assert [viz.dtype_of(ROWS, i) for i in range(-1, 5)] == DTYPES


def test_spec_lifecycle():
    assert viz.normalize_spec(SPEC_RAW) == NORM_SPEC
    assert viz.normalize_spec(None) == NORM_SPEC_EMPTY
    assert viz.normalize_spec("junk") == NORM_SPEC_EMPTY
    assert viz.merge_spec(SPEC_RAW, PATCH) == MERGED
    assert viz.merge_spec(SPEC_RAW, None) == NORM_SPEC
    assert viz.sanitize_spec(SPEC_RAW, COLUMNS, ROWS) == SANITIZED
    assert viz.sanitize_spec(SPEC_BAD, COLUMNS, ROWS) == SANITIZED_BAD
    assert viz.validate_spec(SPEC_BAD, COLUMNS, ROWS) == VALIDATE_BAD
    assert viz.validate_spec(SPEC_RAW, COLUMNS, ROWS) == VALIDATE_OK


def test_expressions():
    got = {e: [viz.compile_expr(e, COLUMNS)(r) for r in ROWS] for e in EXPRS}
    assert got == EXPR_VALUES
    errs = {}
    for e in BAD_EXPRS:
        with pytest.raises(viz.VizError) as ex:
            viz.compile_expr(e, COLUMNS)
        errs[e[:20]] = str(ex.value)
    assert errs == EXPR_ERRORS


def test_predicates():
    assert [viz.coerce_compare(a, b) for a, b in COERCE_PAIRS] == COERCE
    assert [[viz.match_row(r, COLUMNS, p) for r in ROWS] for p in PREDS] == MATCH
    assert viz.apply_filters(COLUMNS, ROWS, FILTER_LIST) == FILTERED
    assert viz.apply_filters(COLUMNS, ROWS, None) == (COLUMNS, ROWS)
