"""Prompt → table relevance ranking — pure, deterministic, a leaf module.

match_tables() scores each table by the overlap between a request's terms and
the table's name / column names. It used to live in catalog.py, but catalog
is a router that pulls in gateway, governance, suggest (→ agent) and more, so
callers that only wanted the ranking (chat, pipelines) had to import the whole
catalog — pipelines did so lazily to stay off that import chain. Here it has
no dependencies at all, so anyone can import it at module level.

Invariants:
  - Imports nothing from ``app`` (enforced by tests/test_layering.py).
  - Works with or without an LLM: no model call, no I/O, same result for the
    same input.
  - catalog re-exports match_tables so ``from .catalog import match_tables``
    keeps working; new code should import from here.
"""
import re

_STOP = {"the", "and", "for", "with", "show", "what", "which", "how", "many",
         "much", "per", "over", "last", "this", "that", "from", "table",
         "tables", "data", "chart", "level", "give", "get", "top", "all"}


def _tokens(text):
    return {w.rstrip("s") for w in re.findall(r"[a-z0-9]+", text.lower())
            if len(w) >= 3 and w not in _STOP}


def match_tables(prompt, table_schemas):
    """Rank tables by relevance to a business request: overlap between the
    prompt's terms and each table's name and column names (name hits weigh
    double). Deterministic — works with or without an LLM."""
    words = _tokens(prompt)
    out = []
    for table, cols in table_schemas.items():
        name_terms = _tokens(table.replace("_", " "))
        col_terms = {}
        for c in cols:
            for t in _tokens(str(c.get("name", "")).replace("_", " ")):
                col_terms.setdefault(t, str(c["name"]))
        name_hits = words & name_terms
        col_hits = words & set(col_terms)
        score = 2 * len(name_hits) + len(col_hits)
        if score > 0:
            out.append({
                "table": table,
                "score": score,
                "why": sorted(name_hits | {col_terms[h] for h in col_hits})[:6],
                "columns": [str(c.get("name", "")) for c in cols][:12],
            })
    out.sort(key=lambda m: (-m["score"], m["table"]))
    return out[:5]
