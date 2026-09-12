"""Find a caller's proven pipeline recipes without granting execution authority.

This is retrieval, not weight training. A match is historical data: SQL must
still pass the normal live verifier and platform/DAG consumers must validate
and obtain approval again. Similar prompts never license string-rewriting SQL
or silently reusing an old filter.
"""
import json
import logging
import re

from . import db, queryguard, rbac
from .connectors import get_connector


log = logging.getLogger("studio.pipeline_memory")
_SUCCEEDED = {"success", "succeeded", "succeeded_sql_only"}
_SCAN_LIMIT = 250
_MAX_ACTION_BYTES = 100_000
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "to", "by", "from", "in", "on",
    "at", "with", "as", "me", "my", "please", "can", "could", "would", "you",
    "build", "create", "make", "generate", "draft", "pipeline", "pipelines",
    "data", "run", "execute", "start", "then", "it", "this", "that", "now",
})
_TERM_ALIASES = {"regional": "region", "regions": "region", "revenues": "revenue"}


def normalize_prompt(prompt):
    """Normalize presentation only, retaining operators, dates and literals.

    Command boilerplate is interchangeable; business clauses are not. Quoted
    values retain case and whitespace because they can be case-sensitive SQL
    literals. Punctuation inside the requirement is not discarded.
    """
    text = str(prompt or "").strip()
    text = re.sub(r"^(?:(?:can|could|would) you\s+)?(?:please\s+)?", "", text, flags=re.I)
    text = re.sub(
        r"^(?:build|create|make|generate|draft|run|execute)(?:\s+(?:and|then)\s+(?:run|execute))?\s+"
        r"(?:a\s+|the\s+|my\s+)?(?:data\s+)?pipeline\s+(?:for|to)\s+", "", text, flags=re.I)
    # Avoid lowercasing a string literal such as 'North' into a different SQL
    # value. Outside quotes, natural-language casing is not a parameter.
    parts = re.split(r"('(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")", text)
    text = "".join(part if i % 2 else re.sub(r"\s+", " ", part).casefold()
                   for i, part in enumerate(parts))
    return text.strip().rstrip(".!? ")


def _terms(prompt):
    return {_TERM_ALIASES.get(term, term)
            for term in re.findall(r"[\w]+", normalize_prompt(prompt).casefold())
            if term not in _STOPWORDS}


def _similarity(prompt, saved_prompt):
    current, saved = _terms(prompt), _terms(saved_prompt)
    if not current or not saved:
        return None
    if normalize_prompt(prompt) == normalize_prompt(saved_prompt):
        return ("exact", 1.0)
    overlap = current & saved
    # One accidental table/verb hit is not enough to select a recipe. Exact
    # one-word requirements are supported above, but not fuzzy ones.
    if len(overlap) < 2:
        return None
    coverage = len(overlap) / max(len(current), len(saved))
    union = len(overlap) / len(current | saved)
    score = round((coverage + union) / 2, 4)
    return ("similar", score) if score >= 0.5 else None


def _sql_allowed(user, action, source, tables):
    steps = action.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        return False
    selected = [t for t in (tables or []) if t and t != "*"]
    for step in steps:
        if not isinstance(step, dict):
            return False
        src, sql = step.get("source"), step.get("sql")
        if not src or src == "*" or not isinstance(sql, str) or not sql.strip():
            return False
        if source and source != "*" and src != source:
            return False
        if not rbac.can_access(user["role"], src, "*"):
            return False
        connector = get_connector(src)
        dialect = getattr(connector, "dialect", None)
        tokens, _ = queryguard._tokens(sql)
        # The SQL tokenizer retains full qualifiers; a same-named CTE cannot
        # turn secret_schema.sales into an allowed local sales reference.
        allowed = [queryguard._canon(parts[-1], dialect)
                   for parts, _ in queryguard._table_refs(tokens)
                   if rbac.can_access(user["role"], src, parts[-1].text)]
        queryguard.validate(sql, allowed, qualifiers=connector.qualifiers(), dialect=dialect)
        if selected:
            queryguard.validate(sql, selected, qualifiers=connector.qualifiers(), dialect=dialect)
        # A display label is not permission evidence, but an explicit table
        # label should not disclose a newly forbidden object either.
        table = step.get("table")
        if table and table != "*" and not rbac.can_access(user["role"], src, table):
            return False
    return True


def successful_recipes(user, prompt, *, source=None, tables=None,
                       action_types=("sql_pipeline",), limit=3, validate_action=None):
    """Rank bounded, owner-only successful history by requirement similarity.

    A failed run with thumbs-up is still failed. A succeeded run relabelled
    below reward 1 is not a trusted recipe either. Non-SQL consumers MUST
    provide ``validate_action(action) -> bool`` to enforce current scope; a
    missing callback fails closed. All returned actions are data, not plans
    ready to execute. No remote schema lookup or verification read occurs.
    """
    try:
        limit = max(1, min(int(limit), 10))
        types = {action_types} if isinstance(action_types, str) else set(action_types)
        if not types or not _terms(prompt):
            return []
        with db.connect() as c:
            rows = c.execute(
                "SELECT id,prompt,source,ok,reward,meta,created_at FROM agent_traces "
                "WHERE user_id=? AND mode='pipeline' AND ok=1 AND reward>=1 "
                "ORDER BY created_at DESC LIMIT ?", (user["id"], _SCAN_LIMIT)).fetchall()
    except Exception:
        log.warning("Pipeline memory unavailable", exc_info=True)
        return []
    matches = []
    for row in rows:
        try:
            if len(row["meta"] or "") > _MAX_ACTION_BYTES:
                continue
            meta = json.loads(row["meta"] or "{}")
            action = meta.get("action")
            if (not isinstance(action, dict) or action.get("type") not in types
                    or meta.get("status") not in _SUCCEEDED or not meta.get("run_id")
                    or row["reward"] != 1):
                continue
            ranked = _similarity(prompt, row["prompt"])
            if ranked is None:
                continue
            if source and source != "*" and row["source"] not in (source, "*"):
                continue
            if action["type"] == "sql_pipeline":
                if not _sql_allowed(user, action, source, tables):
                    continue
                action = {"type": "sql_pipeline", "steps": [
                    {key: step.get(key) for key in ("name", "source", "table", "sql")}
                    for step in action["steps"]]}
            elif not validate_action or not validate_action(action):
                continue
            matches.append({"trace_id": row["id"], "run_id": meta["run_id"],
                            "prompt": row["prompt"], "source": row["source"],
                            "action": action, "status": meta["status"], "reward": row["reward"],
                            "repairs_run_id": meta.get("repairs_run_id"),
                            "match": ranked[0], "similarity": ranked[1],
                            "created_at": row["created_at"]})
        except Exception:
            # A malformed/stale recipe or unavailable connector is simply
            # not a reusable example; it must not break new generation.
            continue
    # Exact requirements precede fuzzy matches. Among equally relevant
    # recipes, prefer a successful correction, then the most recent run.
    matches.sort(key=lambda m: (m["match"] == "exact", m["similarity"],
                               bool(m["repairs_run_id"]), m["created_at"]), reverse=True)
    return matches[:limit]


def provenance(recipe, reuse_type):
    """Small, row-free metadata safe to attach to a private chat artifact."""
    return {"trace_id": recipe["trace_id"], "run_id": recipe["run_id"],
            "matched_prompt": recipe["prompt"], "similarity": recipe["similarity"],
            "reuse_type": reuse_type, "repairs_run_id": recipe.get("repairs_run_id")}
