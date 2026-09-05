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
import os
import re
import time
import uuid

from . import db, embed

# Two distinct bands so the tiers don't overlap:
#   >= CACHE_THRESHOLD           → near-identical, reuse the exact plan (cache)
#   [LEARN_THRESHOLD, CACHE)     → a learned *variation*, route to BitNet
#   <  LEARN_THRESHOLD           → novel, the frontier LLM
CACHE_THRESHOLD = float(os.getenv("STUDIO_QCACHE_THRESHOLD", "0.9"))
LEARN_THRESHOLD = float(os.getenv("STUDIO_BITNET_MATCH", "0.6"))
# A pattern is "learned" (routable to BitNet) once it has been asked at least
# this many times with at least this average reward — repeated AND successful.
MIN_SEEN = int(os.getenv("STUDIO_BITNET_MIN_SEEN", "2"))
MIN_REWARD = float(os.getenv("STUDIO_BITNET_MIN_REWARD", "0.6"))

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
    with db.connect() as c:
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
                seen INTEGER NOT NULL DEFAULT 0,
                avg_reward REAL,
                embedding TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_qcache_scope
                ON query_cache(role, source, table_scope);
            """
        )
        # seen / avg_reward / embedding arrived with routing and embeddings; a
        # table created before them gets them from migration 5 (app/migrations.py).
        c.commit()


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


def _sig_text(sig):
    return " ".join(sig)


def _query_vec(sig):
    """Harrier embedding of an INCOMING prompt (query side — instruction-prefixed),
    or None (→ lexical fallback)."""
    return embed.embed(_sig_text(sig), kind="query") if embed.available() else None


def _doc_vec(sig):
    """Harrier embedding of a STORED pattern (document side — plain)."""
    return embed.embed(_sig_text(sig), kind="document") if embed.available() else None


def _sim(q_vec, q_sig, e_vec, e_sig):
    """Similarity between a query and a stored entry: Harrier cosine when both
    sides have a vector, else lexical Jaccard. One consistent scale for the
    cache / learn bands either way (paraphrases score high on both, just wider
    with embeddings)."""
    if q_vec and e_vec:
        return embed.cosine(q_vec, e_vec)
    return _jaccard(q_sig, e_sig)


def _exec_full(user, source, sql):
    """Re-run cached SQL with full rows through the gateway (RBAC + guard +
    governance + audit, purpose "cache_replay"). Returns (columns, rows) or
    None on ANY failure — a denied table, an unconfigured source, a connector
    error — which the caller treats as a cache miss and runs the agent."""
    from . import gateway
    try:
        res = gateway.execute(user, source, sql, "cache_replay")
        return res.columns, res.rows
    except Exception:
        return None


def lookup(user, source, table_scope, prompt):
    """Best semantic hit for this prompt in scope, re-executed fresh. Returns a
    run-result dict (mode='cached') or None."""
    sig = _sig(prompt)
    if not sig:
        return None
    q_vec = _query_vec(sig)
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM query_cache WHERE role=? AND source=? AND table_scope=?",
            (user["role"], source, table_scope)).fetchall()
    best, best_score = None, 0.0
    for r in rows:
        e_vec = json.loads(r["embedding"]) if r["embedding"] else None
        score = _sim(q_vec, sig, e_vec, json.loads(r["signature"]))
        if score > best_score:
            best, best_score = r, score
    if not best or best_score < CACHE_THRESHOLD:
        return None

    executed = _exec_full(user, source, best["sql"])
    if executed is None:
        return None
    cols, data = executed
    chart = json.loads(best["chart"]) if best["chart"] else None
    panel = {"sql": best["sql"], "columns": cols, "rows": data, "chart": chart}
    with db.connect() as c:
        c.execute("UPDATE query_cache SET hits=hits+1, updated_at=? WHERE id=?",
                  (time.time(), best["id"]))
        c.commit()
    return {
        "text": best["text"] or "Reused a cached query plan.",
        "sql": best["sql"], "columns": cols, "rows": data, "chart": chart,
        "panels": [panel] if (chart or data) else [],
        "email": None, "errors": [], "mode": "cached", "model": None, "served_by": "cache",
        "agents": [{"name": "Query cache", "source": source, "role": "cache"}],
        "cached": {"similarity": round(best_score, 3), "from_prompt": best["prompt"],
                   "hits": best["hits"] + 1},
    }


def scope_stats():
    """BitNet's scope: how many use cases it covers (patterns that have crossed
    'repeated + successful'), plus how many are still being learned (handled by
    the frontier until they cross over). Centralized across roles."""
    with db.connect() as c:
        total = c.execute("SELECT COUNT(DISTINCT signature) n FROM query_cache").fetchone()["n"]
        row = c.execute(
            "SELECT COUNT(*) n FROM (SELECT signature FROM query_cache GROUP BY signature "
            "HAVING SUM(seen) >= ? AND AVG(avg_reward) >= ?) sub", (MIN_SEEN, MIN_REWARD)).fetchone()
    in_scope = row["n"]
    return {"in_scope": in_scope, "total_patterns": total, "learning": max(0, total - in_scope)}


def store(user, source, table_scope, prompt, result, reward=None):
    """Cache a successful single-SQL run's plan, and track how often this pattern
    recurs and how well it scores — so `learned()` can tell a repeated, reliable
    pattern (routable to BitNet) from a one-off. Skips multi-panel/orchestrated
    and error results."""
    sql = result.get("sql")
    text = result.get("text") or ""
    if not sql or text.startswith("(Agent error") or len(result.get("panels") or []) > 1:
        return
    sig = _sig(prompt)
    if not sig:
        return
    if reward is None:
        reward = 1.0 if result.get("rows") else 0.5
    chart = json.dumps(result.get("chart")) if result.get("chart") else None
    emb = _doc_vec(sig)                            # stored pattern = document side
    emb_json = json.dumps(emb) if emb else None
    now = time.time()
    with db.connect() as c:
        row = c.execute("SELECT id, seen, avg_reward, embedding FROM query_cache WHERE role=? "
                        "AND source=? AND table_scope=? AND signature=?",
                        (user["role"], source, table_scope, json.dumps(sig))).fetchone()
        if row:
            seen = (row["seen"] or 0) + 1
            prev = row["avg_reward"] if row["avg_reward"] is not None else reward
            avg = (prev * (seen - 1) + reward) / seen        # running average
            c.execute("UPDATE query_cache SET sql=?, chart=?, text=?, seen=?, avg_reward=?, "
                      "embedding=?, updated_at=? WHERE id=?",
                      (sql, chart, text[:2000], seen, avg,
                       emb_json or row["embedding"], now, row["id"]))
        else:
            c.execute(
                "INSERT INTO query_cache (id, role, source, table_scope, prompt, signature, sql, "
                "chart, text, hits, seen, avg_reward, embedding, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), user["role"], source, table_scope, prompt[:500],
                 json.dumps(sig), sql, chart, text[:2000], 0, 1, reward, emb_json, now, now))
        c.commit()


def learned(source, table_scope, prompt):
    """A repeated + successful pattern that ANY user has established for this
    source + scope — CENTRALIZED across roles. Aggregates seen count and reward
    over every role that has asked it (SUM(seen) >= MIN_SEEN, AVG(reward) >=
    MIN_REWARD), so one user's learning benefits everyone. Returns the pattern,
    or None. It is deliberately role-agnostic; the router still checks the
    *requester's* access before actually routing to BitNet ("same access")."""
    sig = _sig(prompt)
    if not sig:
        return None
    q_vec = _query_vec(sig)
    with db.connect() as c:
        rows = c.execute(
            "SELECT signature, SUM(seen) seen, AVG(avg_reward) avg_reward, MAX(sql) sql, "
            "MAX(prompt) prompt, MAX(embedding) embedding FROM query_cache "
            "WHERE source=? AND table_scope=? GROUP BY signature "
            "HAVING SUM(seen) >= ? AND AVG(avg_reward) >= ?",
            (source, table_scope, MIN_SEEN, MIN_REWARD)).fetchall()
    best, score = None, 0.0
    for r in rows:
        e_vec = json.loads(r["embedding"]) if r["embedding"] else None
        s = _sim(q_vec, sig, e_vec, json.loads(r["signature"]))
        if s > score:
            best, score = r, s
    # The learned band sits below the cache threshold — a variation the exact
    # cached plan may not fit, but within a family BitNet has been trained on.
    if best and LEARN_THRESHOLD <= score < CACHE_THRESHOLD:
        return {"prompt": best["prompt"], "sql": best["sql"], "seen": best["seen"],
                "avg_reward": round(best["avg_reward"], 3), "similarity": round(score, 3)}
    return None
