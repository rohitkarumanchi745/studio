"""Semantic cache for user queries.

Two differently-phrased questions that mean the same thing ("top products by
revenue" / "which products made the most revenue") should not both pay for a
full agent run. Each successful run's PLAN (its SQL + chart) is cached under a
normalized token signature of the prompt, scoped by (role, source, table). A new
prompt whose signature is similar enough (Jaccard ≥ threshold) reuses that plan.

Correctness guardrails:
- A hit ALWAYS re-executes the cached SQL through the RBAC + query-guard +
  governance path, so the cache serves fresh rows and can never leak data a
  role may not see or return stale results.
- The signature is role- and source-scoped, so one role's cache can't answer
  for another.
- A high threshold keeps near-duplicates together while different questions miss
  and fall through to the agent.
"""
import json
import re
import time
import uuid

from . import db

THRESHOLD = float(__import__("os").getenv("STUDIO_QCACHE_THRESHOLD", "0.82"))

# Stopwords include query filler AND ranking words (top / most / highest / best):
# those map to the same stored SQL plan (whose ORDER BY already encodes the
# ranking), so dropping them groups "top products by revenue" with "which
# products made the most revenue" onto one entry. Measure words (count, sum,
# total, average) are deliberately NOT stopped — they change the plan.
_STOP = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "by", "with",
         "from", "our", "show", "me", "what", "which", "how", "is", "are", "was",
         "give", "get", "list", "find", "please", "can", "you", "all", "that",
         "this", "do", "does", "did", "my", "we", "their", "over", "per", "each",
         "top", "most", "highest", "lowest", "best", "worst", "many", "much",
         "made", "make", "have", "has", "had", "be", "been", "will", "would"}


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS query_cache (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            source TEXT NOT NULL,
            table_scope TEXT NOT NULL,
            prompt TEXT NOT NULL,
            signature TEXT NOT NULL,
            sql TEXT NOT NULL,
            chart TEXT,
            text TEXT,
            hits INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_qcache_scope
            ON query_cache(role, source, table_scope);
        """
    )
    c.commit()
    c.close()


def _stem(t):
    # Crude singularization so plural/singular map together (products→product,
    # customers→customer). Skips short words and common -ss (address).
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _sig(prompt):
    toks = [_stem(t) for t in re.split(r"[^a-z0-9]+", (prompt or "").lower())
            if t and t not in _STOP and len(t) > 1]
    return sorted(set(t for t in toks if t not in _STOP))


def _jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _exec_full(user, source, sql):
    """Re-run cached SQL with full rows, re-checking RBAC + guard + governance.
    Returns (columns, rows) or None on any failure (→ cache miss, run the agent)."""
    from . import agent, governance, queryguard, rbac
    from .catalog import _connector_or_400
    try:
        conn = _connector_or_400(source)
        allowed = rbac.allowed_tables(user["role"], source, conn.list_tables())
        cleaned = queryguard.enforce_limit(queryguard.validate(sql, allowed), agent.MAX_ROWS)
        cols, rows = conn.run_query(cleaned)
        cols, rows = governance.filter_result(source, cleaned, cols, rows)
        return cols, rows[:agent.MAX_ROWS]
    except Exception:
        return None


def lookup(user, source, table_scope, prompt):
    """Best semantic hit for this prompt in scope, re-executed fresh. Returns a
    run-result dict (mode='cached') or None."""
    sig = _sig(prompt)
    if not sig:
        return None
    c = db._conn()
    rows = c.execute(
        "SELECT * FROM query_cache WHERE role=? AND source=? AND table_scope=?",
        (user["role"], source, table_scope)).fetchall()
    c.close()
    best, best_score = None, 0.0
    for r in rows:
        score = _jaccard(sig, json.loads(r["signature"]))
        if score > best_score:
            best, best_score = r, score
    if not best or best_score < THRESHOLD:
        return None

    executed = _exec_full(user, source, best["sql"])
    if executed is None:
        return None
    cols, data = executed
    chart = json.loads(best["chart"]) if best["chart"] else None
    panel = {"sql": best["sql"], "columns": cols, "rows": data, "chart": chart}
    c = db._conn()
    c.execute("UPDATE query_cache SET hits=hits+1, updated_at=? WHERE id=?",
              (time.time(), best["id"]))
    c.commit()
    c.close()
    return {
        "text": best["text"] or "Reused a cached query plan.",
        "sql": best["sql"], "columns": cols, "rows": data, "chart": chart,
        "panels": [panel] if (chart or data) else [],
        "email": None, "errors": [], "mode": "cached", "model": None,
        "agents": [{"name": "Query cache", "source": source, "role": "cache"}],
        "cached": {"similarity": round(best_score, 3), "from_prompt": best["prompt"],
                   "hits": best["hits"] + 1},
    }


def store(user, source, table_scope, prompt, result):
    """Cache a successful single-SQL run's plan. Skips multi-panel/orchestrated
    and error results — those don't have one reusable plan."""
    sql = result.get("sql")
    text = result.get("text") or ""
    if not sql or text.startswith("(Agent error") or len(result.get("panels") or []) > 1:
        return
    sig = _sig(prompt)
    if not sig:
        return
    now = time.time()
    c = db._conn()
    # One entry per identical signature in scope — refresh it rather than pile up.
    c.execute("DELETE FROM query_cache WHERE role=? AND source=? AND table_scope=? AND signature=?",
              (user["role"], source, table_scope, json.dumps(sig)))
    c.execute(
        "INSERT INTO query_cache (id, role, source, table_scope, prompt, signature, sql, "
        "chart, text, hits, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), user["role"], source, table_scope, prompt[:500],
         json.dumps(sig), sql, json.dumps(result.get("chart")) if result.get("chart") else None,
         text[:2000], 0, now, now),
    )
    c.commit()
    c.close()
