"""Prompt → table relevance ranking — pure, deterministic, a leaf module.

match_tables() scores each table by the overlap between a request's terms and
the table's name / column names. It used to live in catalog.py, but catalog
is a router that pulls in gateway, governance, suggest (→ agent) and more, so
callers that only wanted the ranking (chat) had to import the whole
catalog. Here it has
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


# Warehouse naming prefixes: two tables sharing "fact" or "dim" says nothing
# about either one's subject.
_NAME_STOP = {"fact", "dim", "stg", "raw", "tbl", "vw", "src", "int", "tmp", "agg"}


def _named(name, text):
    """True when `text` names the table outright (`fact_payments` and
    `studio_qa.fact_payments` both name studio_dev.studio_qa.fact_payments)."""
    return re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text) is not None


def focus_tables(prompt, tables, context="", limit=8):
    """The tables a request most likely needs, best first — from their names
    alone, since a source can hold hundreds and reading each one's columns is
    a warehouse query. Evidence, strongest first:
      - the request names the table outright;
      - `context` (this conversation's earlier SQL) names it, so a follow-up
        keeps its tables;
      - the request shares terms with the table's own name (its last part:
        "payments" → studio_qa.fact_payments).
    Only the request's terms count, never the SQL's: ORDER BY must not pull
    in an orders table. Tables with no evidence are left out; ties keep the
    order given."""
    text, earlier = prompt.lower(), (context or "").lower()
    words = _tokens(prompt)
    scored = []
    for i, table in enumerate(tables):
        name = table.lower()
        parts = name.split(".")
        tails = {".".join(parts[k:]) for k in range(len(parts))}   # full … last part
        score = 0
        if any(_named(t, text) for t in tails):
            score += 10
        if earlier and any(_named(t, earlier) for t in tails):
            score += 6
        score += 2 * len(words & (_tokens(parts[-1].replace("_", " ")) - _NAME_STOP))
        if score:
            scored.append((-score, i, table))
    return [t for _, _, t in sorted(scored)[:limit]]
