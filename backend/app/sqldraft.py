"""The deterministic SQL drafter: one table, its columns and a question →
an aggregate SELECT, with no model.

It outlived the read-only SQL pipelines it was written for: the BitNet
bootstrap (scripts/bootstrap_rollouts.py) pairs suggested questions with these
drafts and verifies every pair through the real gateway, so a new deployment
has reward-labeled rollouts before it has traffic.
"""
from . import grains, suggest
from .matching import _tokens


def prompt_match(names, terms):
    """The first column in `names` whose own tokens overlap the prompt's, or
    None. Order is the caller's ranking (suggest._classify already ranks
    measures best-first), so this is "the best column the user asked for"."""
    for n in names:
        if _tokens(str(n).replace("_", " ")) & terms:
            return n
    return None


def draft_sql(connector, table, columns, prompt=None):
    """A sensible query for one table, STEERED BY THE PROMPT.

    When the request names a time grain ("monthly revenue by region"), the
    query buckets the date column to that grain in the connector's own dialect
    (grains.bucket_expr), aggregates the measure the prompt named, and groups
    by the dimension it named. With no grain, the first measure over the first
    date or dimension.
    """
    dates, measures, dims = suggest._classify(columns)
    if not (measures and (dates or dims)):
        return f"SELECT * FROM {table} LIMIT 200"
    terms = _tokens(prompt or "")
    grain = grains.detect(prompt or "")
    bucket = grains.bucket_expr(connector.dialect, dates[0], grain) if (grain and dates) else None
    if not bucket:
        m = measures[0]
        g = dates[0] if dates else dims[0]
        return f"SELECT {g}, SUM({m}) AS total_{m} FROM {table} GROUP BY {g} ORDER BY 1 LIMIT 500"
    m = prompt_match(measures, terms) or measures[0]
    dim = prompt_match(dims, terms)
    select = [f"{bucket} AS {grain}"] + ([dim] if dim else [])
    group = [bucket] + ([dim] if dim else [])
    return (f"SELECT {', '.join(select)}, SUM({m}) AS total_{m} FROM {table} "
            f"GROUP BY {', '.join(group)} ORDER BY 1 LIMIT 500")
