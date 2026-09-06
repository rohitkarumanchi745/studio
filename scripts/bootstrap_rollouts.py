#!/usr/bin/env python3
"""Bootstrap the FIRST rollout corpus, so a new deployment can train at all.

THE CIRCULARITY
    app/router.py:bitnet_ready() has two gates — a serving endpoint
    (STUDIO_LLM_BASE_URL) and a published `tool_call` adapter. The adapter is
    built by scripts/train_online.py from reward-labeled rollouts pulled from
    GET /training/rollouts. Those rollouts come from real traffic. On a new
    deployment there is no traffic, so there are no rollouts, so there is no
    adapter, so BitNet never serves, so no BitNet traffic is ever produced.
    Nothing breaks the loop on its own.

WHAT THIS DOES
    Produces a first corpus from machinery Studio already has, with no LLM key
    and no organic usage:

      app/suggest.py      questions grounded in a table's REAL schema
      app/pipelines.py    _draft_sql: table + columns + prompt -> aggregate SQL
      app/grains.py       the prompt's time grain -> the dialect's bucket
      app/gateway.py      the ONE data gate — RBAC, guard, governance, audit

    Both halves of every pair come from the SAME schema and dialect the live
    agent sees, and every pair is EXECUTED through the real gateway as a real
    user before it is kept. A rollout whose SQL does not run is worse than no
    rollout: it teaches the policy to emit broken SQL. So is a rollout whose
    SQL runs but does not answer its question — that teaches the policy to
    answer "top region by revenue" with a time series — so pairs are also
    dropped when the SQL visibly does not do what the question asked
    (see is_faithful).

    Rows land in agent_traces exactly as app/trainer.py's /training/rollouts
    endpoint serves them, labeled reward_source="bootstrap" and mode="bootstrap"
    so a human can tell them from organic rollouts at a glance and delete them:

        DELETE FROM agent_traces WHERE reward_source='bootstrap';

WHAT IT CANNOT DO
    The questions are template-generated and the SQL is a deterministic
    drafter, so the adapter learns "given this schema and this question SHAPE,
    emit this SQL" — not human phrasing variety and not shapes the drafter
    cannot express (ranking, window functions, joins). The run prints the
    distinct-SQL-SHAPE count, which is what actually bounds how much a LoRA
    can learn from the corpus; read it before trusting the sample count.

    Every rollout carries the SAME reward, so there is no preference signal in
    it: scripts/train_online.py's SFT mode consumes the whole corpus, and its
    DPO mode mines ZERO pairs from it (a pair needs two outcomes for one prompt
    with a reward gap). Bootstrap gets you an SFT adapter; DPO still waits for
    real traffic.

    It also does not populate app/qcache.py's `query_cache`. router.choose()
    routes to BitNet only for a prompt qcache has LEARNED (repeated + well
    scored), so this unblocks TRAINING, not routing — organic repetition still
    decides what BitNet is allowed to answer.

MEASURED, on the seeded demo warehouse as admin (15 tables, sqlite)
    486 candidates -> 162 dropped as unfaithful -> 324 verified rollouts kept
    (66.7%), 0 of which failed to execute; 162 distinct statements but only
    SIX distinct SQL shapes (1.9%): {month, quarter, year} x {with, without a
    grouping dimension}. Read that as: the corpus teaches six lessons, in 27
    schema variations each, two phrasings apiece. It is enough to make an
    adapter exist and to prove the loop end to end; it is not enough to make
    one good.

USAGE (run from anywhere; reads the same DB env the API does)
    python scripts/bootstrap_rollouts.py --dry-run
    python scripts/bootstrap_rollouts.py --source demo --limit 100
    python scripts/bootstrap_rollouts.py --user admin@studio.local --json

Environment: STUDIO_DB_PATH / DATABASE_URL (the app state store) plus whatever
each connector needs. Pure stdlib + the app package — no ML deps, so it runs
in the lean API image beside the backend it writes to.
"""
import argparse
import json
import os
import re
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from app import (agent, catalog, db, gateway, governance, grains,  # noqa: E402
                 pipelines, rbac, suggest)
from app.connectors import all_sources  # noqa: E402
from app.matching import _tokens  # noqa: E402

# How these rows are labeled. Both are load-bearing:
#   reward_source  the ONLY thing that distinguishes synthetic from organic —
#                  the refusal check, the idempotency check and the cleanup
#                  DELETE all key on it.
#   mode           carried into the training sample; scripts/train_online.py
#                  drops modes starting with "fallback"/"error", so this must
#                  not start with either.
REWARD_SOURCE = "bootstrap"
MODE = "bootstrap"
GENERATOR = "scripts/bootstrap_rollouts.py"

# Deliberately modest, and deliberately above scripts/train_online.py's
# STUDIO_TRAIN_MIN_REWARD default (0.6) so the corpus is trainable — but below
# what a good organic run scores (lightning.heuristic_reward reaches 1.0), so a
# real preference signal always outranks a synthetic one in DPO pair mining.
DEFAULT_REWARD = 0.7
DEFAULT_LIMIT = 200
DEFAULT_TABLES = 20
# Verification only has to prove the statement RUNS; enforce_limit never
# rewrites the drafter's own LIMIT 500, so capping rows here changes what we
# fetch, never the SQL we store.
DEFAULT_MAX_ROWS = 200

# Time grains the templates ask for. Kept to the ones a business question
# actually names, and each produces a DIFFERENT bucket expression (SQLite's
# quarter is a composed CASE-free expression, not a strftime format), so this
# is also the main source of distinct SQL shapes.
TEMPLATE_GRAINS = ("month", "quarter", "year")

# Question constructs app/pipelines.py's _draft_sql provably cannot express: it
# emits one bucketed/grouped SUM ordered by the first column. A question asking
# for a ranking, a window function or a relationship is dropped rather than
# paired with SQL that does not answer it.
_UNSUPPORTED = (
    "running total", "year over year", "year-over-year", "yoy", "cumulative",
    "moving average", "rolling", "relate", "correlat", "versus", " vs ",
    "compare", "share of", "percent of total", "forecast", "median",
    "distribution", "histogram", "top ", "highest", "lowest", "rank", "best",
    "worst",
)

# Aggregate calls that make a statement a real aggregation rather than a dump.
_AGG = re.compile(r"\b(sum|count|avg|min|max)\s*\(", re.IGNORECASE)

# SQL vocabulary preserved by sql_shape(); everything else alphanumeric is an
# identifier and collapses to X, so `sales.revenue by region` and
# `downtime_events.minutes by plant` are recognised as the SAME shape.
#
# Deliberately NOT in this set: type names (INTEGER, TEXT, DATE, REAL). A
# column really can be called `date` — the demo warehouse has several — and
# keeping type words would report one shape per column name, which is exactly
# the inflation this function exists to defeat. `CAST(x AS INTEGER)` still
# collapses consistently to `cast(X as X)`.
_SQL_WORDS = {
    "select", "from", "where", "group", "by", "order", "limit", "as", "and",
    "or", "not", "in", "is", "null", "on", "join", "left", "right", "inner",
    "outer", "full", "cross", "having", "distinct", "asc", "desc", "case",
    "when", "then", "else", "end", "with", "union", "all", "between", "like",
    "over", "partition",
}
# Function names are only vocabulary when CALLED — a column named `count` or
# `max` is an identifier like any other.
_SQL_FUNCS = {
    "sum", "count", "avg", "min", "max", "cast", "coalesce", "round", "abs",
    "strftime", "date_trunc", "date_format", "datetime", "extract",
    "row_number", "rank", "dense_rank", "lag", "lead",
}


# ── The user this runs as ───────────────────────────────────────────────

def resolve_user(email=None):
    """The user every generated query is executed AS. Everything below runs
    through gateway.execute with this identity, so the corpus can only ever
    contain SQL this user's ROLE was allowed to run — a bootstrap corpus must
    not be a way around RBAC. Defaults to the oldest admin account."""
    if email:
        user = db.get_user_by_email(email)
        if not user:
            raise SystemExit(f"[bootstrap] no such user: {email}")
        return user
    with db.connect() as c:
        row = c.execute("SELECT * FROM users WHERE role='admin' "
                        "ORDER BY created_at LIMIT 1").fetchone()
    if not row:
        raise SystemExit("[bootstrap] no admin account found — pass --user EMAIL")
    return dict(row)


def accessible_sources(user, only=None):
    """Configured sources this user's role may query, in a stable order.
    `only` is a comma-separated filter (--source)."""
    wanted = {s.strip() for s in (only or "").split(",") if s.strip()}
    allowed = rbac.allowed_sources(user["role"])
    names = [s["name"] for s in all_sources()
             if s["configured"] and s["name"] in allowed]
    if wanted:
        names = [n for n in names if n in wanted]
    return sorted(names)


# ── Question / SQL pair generation ──────────────────────────────────────

def _grain_word(grain):
    """'month' -> 'Monthly'. app/grains.py maps the adjective back."""
    return {"month": "Monthly", "quarter": "Quarterly", "year": "Yearly",
            "week": "Weekly", "day": "Daily", "hour": "Hourly"}.get(grain, grain)


def question_set(connector, table, columns, rows=None, use_llm=False):
    """The questions to try for one table, each tagged with its origin.

    Two families, both grounded in the SAME schema classification the live
    picker uses (suggest._classify):

    'suggest'  app/suggest.py's own starter questions — exactly what a user is
               offered in the UI. Most are dropped downstream (the drafter
               cannot answer a ranking question), which is itself the finding:
               only the trend family survives.
    'template' grain x measure x dimension phrasings the drafter CAN answer
               faithfully, so the corpus contains more than one SQL shape and
               several phrasings map to the same statement.

    use_llm routes the first family through suggest.suggestions_for, which asks
    the configured model to phrase them (and silently falls back to the same
    deterministic set with no key). Off by default: it costs calls and makes a
    re-run generate DIFFERENT questions, which is at odds with idempotency.
    """
    qs = []
    seen = set()

    def add(prompt, origin):
        p = " ".join(str(prompt).split())
        if p and p.lower() not in seen:
            seen.add(p.lower())
            qs.append({"prompt": p, "origin": origin})

    for q in (suggest.suggestions_for(connector, table, columns, rows)
              if use_llm else suggest._deterministic(table, columns, rows)):
        add(q, "suggest")

    dates, measures, dims = suggest._classify(columns, rows)
    if not measures:
        return qs
    if not dates:
        # No date column: _draft_sql's un-bucketed shape groups by the FIRST
        # dimension, so only a question naming that dimension is answered
        # faithfully. Rare in the demo warehouse, ordinary in a real one.
        for m in measures[:2]:
            for d in dims[:1]:
                add(f"Total {m} by {d}", "template")
                add(f"{m} by {d}", "template")
        return qs
    for m in measures[:2]:
        for g in TEMPLATE_GRAINS:
            add(f"{_grain_word(g)} {m}", "template")
            add(f"How has {m} trended by {g}?", "template")
            for d in dims[:2]:
                add(f"{_grain_word(g)} {m} by {d}", "template")
                add(f"Show {m} by {d} for each {g}", "template")
    return qs


def is_faithful(prompt, sql, columns, rows=None):
    """Does this SQL visibly answer THIS question? Returns (ok, reason).

    The drafter is deterministic and prompt-steered, but a prompt it cannot
    express still gets an answer: 'Top 10 region by revenue' comes back as
    revenue grouped by ORDER DATE. Executable, and wrong. Training on it
    teaches the policy that any question is a time series, which is a worse
    corpus than a smaller one. Four checks, all lexical and all using the same
    modules the drafter used:

      1. the question asks for nothing the drafter cannot express
      2. it is an aggregate at all (not a `SELECT *` dump)
      3. a time grain the question named is actually bucketed (grains)
      4. every measure and dimension the question named appears in the SQL
    """
    low = (prompt or "").lower()
    for phrase in _UNSUPPORTED:
        if phrase in low:
            return False, f"unsupported construct: {phrase.strip()}"
    if "group by" not in (sql or "").lower() or not _AGG.search(sql or ""):
        return False, "not an aggregate"
    grain = grains.detect(prompt)
    if grain and not grains.has_bucket(sql, grain):
        return False, f"does not bucket by {grain}"
    terms = _tokens(prompt or "")
    _dates, measures, dims = suggest._classify(columns, rows)
    named = [c for c in list(measures) + list(dims)
             if _tokens(str(c).replace("_", " ")) & terms]
    for col in named:
        if not re.search(rf"\b{re.escape(col)}\b", sql or "", re.IGNORECASE):
            return False, f"question names {col}, SQL does not use it"
    return True, ""


def pairs_for_table(connector, table, columns, rows=None, use_llm=False):
    """Every candidate (prompt, sql) for one table, with its faithfulness
    verdict attached. Pure — no execution, no writes — so the generation half
    is testable without a warehouse."""
    out = []
    for q in question_set(connector, table, columns, rows, use_llm=use_llm):
        sql = pipelines._draft_sql(connector, table, columns, q["prompt"])
        ok, reason = is_faithful(q["prompt"], sql, columns, rows)
        out.append({"prompt": q["prompt"], "sql": sql, "origin": q["origin"],
                    "table": table, "faithful": ok, "reason": reason})
    return out


# ── Shape accounting (what the corpus can actually teach) ───────────────

def sql_shape(sql):
    """A statement's SHAPE: identifiers collapsed, SQL vocabulary and string
    literals kept. Two queries share a shape when they differ only in which
    table and columns they name.

    This is the honest denominator for "how much is there to learn here". Fifty
    rollouts over ten tables that all read
    `SELECT strftime('lit', X) AS X, SUM(X) FROM X GROUP BY ... `
    are ONE lesson repeated fifty times, and a LoRA will learn exactly that one
    lesson. String literals are preserved because a grain's format string is
    part of the construction the model must produce ('%Y-%m' vs '%Y').
    """
    s = re.sub(r"--[^\n]*", " ", sql or "")
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.DOTALL)
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "'":                                  # keep literals verbatim
            j = s.find("'", i + 1)
            j = len(s) if j < 0 else j
            out.append(s[i:j + 1])
            i = j + 1
            continue
        m = re.match(r"[A-Za-z_][A-Za-z_0-9$]*", s[i:])
        if m:
            w = m.group(0)
            called = re.match(r"\s*\(", s[i + len(w):]) is not None
            keep = w.lower() in (_SQL_FUNCS if called else _SQL_WORDS)
            out.append(w.lower() if keep else "X")
            i += len(w)
            continue
        m = re.match(r"\d+(\.\d+)?", s[i:])
        if m:
            out.append("N")
            i += len(m.group(0))
            continue
        out.append(" " if ch.isspace() else ch)
        i += 1
    return re.sub(r"\s+", " ", "".join(out)).strip()


def shape_report(rollouts):
    """Distinct SQL, distinct SHAPES, and the fraction of rollouts that carry a
    shape nothing else in the corpus carries."""
    n = len(rollouts)
    sqls = {r["sql"] for r in rollouts}
    shapes = {}
    for r in rollouts:
        shapes.setdefault(sql_shape(r["sql"]), []).append(r)
    return {
        "rollouts": n,
        "distinct_sql": len(sqls),
        "distinct_shapes": len(shapes),
        "shape_fraction": round(len(shapes) / n, 4) if n else 0.0,
        "shapes": sorted(
            ({"shape": k, "count": len(v)} for k, v in shapes.items()),
            key=lambda s: -s["count"]),
    }


# ── The existing corpus: refuse to dilute a real preference signal ──────

def organic_count():
    """Reward-labeled rollouts that did NOT come from this script."""
    with db.connect() as c:
        row = c.execute(
            "SELECT COUNT(*) n FROM agent_traces WHERE reward IS NOT NULL "
            "AND (reward_source IS NULL OR reward_source <> ?)",
            (REWARD_SOURCE,)).fetchone()
    return row["n"]


def existing_keys():
    """Identity of every bootstrap rollout already written, so a re-run tops up
    instead of duplicating. Keyed on the content — (source, table, prompt, sql)
    — not on a run id, so the same pair generated by a later version of this
    script is still recognised as already present."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT source, tbl, prompt, sql FROM agent_traces WHERE reward_source=?",
            (REWARD_SOURCE,)).fetchall()
    return {(r["source"], r["tbl"], r["prompt"], r["sql"]) for r in rows}


def bootstrap_count():
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) n FROM agent_traces WHERE reward_source=?",
                         (REWARD_SOURCE,)).fetchone()["n"]


# ── Chart label ─────────────────────────────────────────────────────────

def chart_type_for(prompt, columns, rows, table):
    """The chart half of the tool call, chosen by the SAME deterministic
    chooser the keyless agent uses (agent._auto_chart) over the rows the query
    actually returned — a phrase in the question ("bar chart") wins, exactly as
    agent.canvas_edit resolves it. None when there is nothing to draw."""
    low = (prompt or "").lower()
    for phrase, t in agent._CHART_PHRASES:
        if phrase in low:
            return t
    spec = agent._auto_chart(columns, rows, table)
    return (spec or {}).get("type")


# ── The run ─────────────────────────────────────────────────────────────

def _init_state():
    """Bring up only what this script touches: the state store (agent_traces
    lives there) and the governance document that decides RBAC and column
    denials. Deliberately NOT app.main.init_state() — this must not start a
    web app's worth of tables to write a few hundred rows."""
    db.init_db()
    try:
        governance.init_tables()
        governance.load()
    except Exception as e:                       # governance is optional
        print(f"[bootstrap] governance not loaded ({e}); using built-in policies")


def candidates(user, sources, *, tables_per_source=DEFAULT_TABLES, use_llm=False,
               stats=None, log=print):
    """Every faithful (prompt, sql) candidate, grouped per (source, table).

    Generation is separated from verification so the --limit budget can be
    spread ROUND-ROBIN across tables: taking the first N candidates in table
    order would spend the whole budget on whichever table sorts first and
    produce a corpus about `ads_performance` and nothing else.
    """
    stats = stats if stats is not None else {}
    stats.setdefault("generated", 0)
    stats.setdefault("unfaithful", 0)
    stats.setdefault("by_reason", {})
    stats.setdefault("skipped", {})
    groups = []
    for source in sources:
        try:
            connector, allowed = gateway.scope(user, source)
        except Exception as e:
            stats["skipped"][source] = str(e)[:160]
            log(f"[bootstrap] {source}: skipped — {str(e)[:120]}")
            continue
        for table in allowed[:tables_per_source]:
            try:
                columns = catalog._governed_schema(source, table, connector.get_schema(table))
                # The sample rows go to suggest._classify exactly as the live
                # picker sends them: through the gateway, so a denied column is
                # gone and a masked one is masked before it shapes a question.
                sample = gateway.execute(user, source, f"SELECT * FROM {table} LIMIT 3",
                                         "bootstrap_sample", table_label=table,
                                         max_rows=3).rows
            except Exception as e:
                stats["skipped"][f"{source}.{table}"] = str(e)[:160]
                continue
            keep = []
            for cand in pairs_for_table(connector, table, columns, sample, use_llm=use_llm):
                stats["generated"] += 1
                if cand["faithful"]:
                    keep.append({**cand, "source": source,
                                 "dialect": getattr(connector, "dialect", None)})
                else:
                    stats["unfaithful"] += 1
                    stats["by_reason"][cand["reason"]] = \
                        stats["by_reason"].get(cand["reason"], 0) + 1
            if keep:
                groups.append(keep)
    return groups


def _round_robin(groups):
    """One candidate from each table in turn, so a --limit cut leaves every
    table represented instead of exhausting the first one."""
    i = 0
    while any(len(g) > i for g in groups):
        for g in groups:
            if len(g) > i:
                yield g[i]
        i += 1


def generate(user, sources, *, limit=DEFAULT_LIMIT, tables_per_source=DEFAULT_TABLES,
             max_rows=DEFAULT_MAX_ROWS, use_llm=False, skip_keys=frozenset(),
             log=print):
    """Generate, VERIFY through the gateway, de-duplicate. Returns (kept, stats).

    Nothing is written here, so --dry-run is this exact code path minus the
    final insert — what it reports is what a real run would store.
    """
    stats = {"generated": 0, "unfaithful": 0, "failed": 0, "duplicate": 0,
             "not_attempted": 0, "by_reason": {}, "by_source": {},
             "by_origin": {}, "skipped": {}}
    groups = candidates(user, sources, tables_per_source=tables_per_source,
                        use_llm=use_llm, stats=stats, log=log)
    seen, kept = set(skip_keys), []
    for cand in _round_robin(groups):
        if len(kept) >= limit:
            stats["not_attempted"] += 1
            continue
        source, table = cand["source"], cand["table"]
        # VERIFY: the real gate, as the real user — RBAC, query guard, row cap,
        # governance, audit. Only SQL that actually executed is ever kept; an
        # unrunnable rollout teaches the policy to emit broken SQL.
        try:
            res = gateway.execute(user, source, cand["sql"], "bootstrap_verify",
                                  table_label=table, max_rows=max_rows)
        except Exception as e:
            stats["failed"] += 1
            stats["by_reason"][f"execution failed: {type(e).__name__}"] = \
                stats["by_reason"].get(f"execution failed: {type(e).__name__}", 0) + 1
            continue
        key = (source, table, cand["prompt"], res.sql)
        if key in seen:
            stats["duplicate"] += 1
            continue
        seen.add(key)
        kept.append({
            "source": source, "table": table, "prompt": cand["prompt"],
            # the CLEANED sql the gateway actually ran — never the draft
            "sql": res.sql, "row_count": res.row_count, "duration_ms": res.took_ms,
            "origin": cand["origin"], "dialect": cand["dialect"],
            "chart_type": chart_type_for(cand["prompt"], res.columns, res.rows, table),
            "grain": grains.detect(cand["prompt"]),
        })
        stats["by_source"][source] = stats["by_source"].get(source, 0) + 1
        stats["by_origin"][cand["origin"]] = stats["by_origin"].get(cand["origin"], 0) + 1
    stats["kept"] = len(kept)
    considered = stats["generated"] - stats["not_attempted"]
    stats["keep_rate"] = round(len(kept) / considered, 4) if considered else 0.0
    executed = len(kept) + stats["duplicate"] + stats["failed"]
    stats["verify_rate"] = round((executed - stats["failed"]) / executed, 4) if executed else 0.0
    stats["considered"] = considered
    for src in sources:
        stats["by_source"].setdefault(src, 0)
        log(f"[bootstrap] {src}: kept {stats['by_source'][src]} verified rollouts")
    return kept, stats


def write(user, rollouts, reward=DEFAULT_REWARD):
    """Persist as agent_traces rows in exactly the shape trainer.stream() reads:
    prompt, action (sql + chart_type), reward, reward_source, source, tbl, mode,
    and a meta carrying the empty `agents`/`history` the endpoint expects."""
    ids = []
    for r in rollouts:
        ids.append(db.add_trace(
            user, prompt=r["prompt"], model=f"{GENERATOR}:_draft_sql", mode=MODE,
            source=r["source"], table=r["table"], sql=r["sql"], ok=True,
            row_count=r["row_count"], chart_type=r["chart_type"],
            duration_ms=r["duration_ms"], reward=reward, reward_source=REWARD_SOURCE,
            meta={
                "synthetic": True, "generator": GENERATOR, "origin": r["origin"],
                "grain": r["grain"], "dialect": r["dialect"],
                # trainer.stream() reads these two keys off meta; a bootstrap
                # rollout is single-turn and un-orchestrated, so both are empty.
                "agents": [], "history": [],
            }))
    return ids


def build_parser():
    p = argparse.ArgumentParser(
        prog="bootstrap_rollouts.py",
        description="Generate a first, verified rollout corpus so a new deployment "
                    "can train a tool_call adapter before it has organic traffic.")
    p.add_argument("--user", help="email to run as (default: the oldest admin)")
    p.add_argument("--source", help="only this source (comma-separated for several)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"max rollouts to write (default {DEFAULT_LIMIT})")
    p.add_argument("--tables", type=int, default=DEFAULT_TABLES,
                   help=f"max tables per source (default {DEFAULT_TABLES})")
    p.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                   help="row cap for verification reads")
    p.add_argument("--reward", type=float, default=DEFAULT_REWARD,
                   help=f"reward to label each rollout with (default {DEFAULT_REWARD})")
    p.add_argument("--llm-questions", action="store_true",
                   help="let suggest.py phrase the questions with the configured "
                        "LLM (costs calls; questions stop being reproducible)")
    p.add_argument("--dry-run", action="store_true",
                   help="generate and verify, print what would be written, write nothing")
    p.add_argument("--force", action="store_true",
                   help="write even though organic rollouts already exist")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    return p


def run(argv=None):
    args = build_parser().parse_args(argv)
    quiet = args.json
    log = (lambda *a, **k: None) if quiet else print

    _init_state()
    user = resolve_user(args.user)
    sources = accessible_sources(user, args.source)
    if not sources:
        raise SystemExit("[bootstrap] no configured source this user may query"
                         + (f" matching --source {args.source}" if args.source else ""))

    organic = organic_count()
    if organic and not args.force and not args.dry_run:
        raise SystemExit(
            f"[bootstrap] refusing to write: this database already has {organic} organic "
            f"reward-labeled rollout(s).\n"
            f"  Synthetic rollouts are template-generated and all carry the same reward, so "
            f"mixing them into a corpus that already holds REAL preference signal dilutes "
            f"it — the trainer would imitate the drafter instead of the behaviour your users "
            f"actually rewarded, and DPO would mine pairs against a flat synthetic baseline.\n"
            f"  Bootstrap is for a deployment with NO usage yet. If you understand that and "
            f"still want them, re-run with --force; remove them later with\n"
            f"    DELETE FROM agent_traces WHERE reward_source='{REWARD_SOURCE}';")

    log(f"[bootstrap] user={user['email']} role={user['role']} "
        f"sources={','.join(sources)} limit={args.limit}"
        + (" [DRY RUN]" if args.dry_run else ""))
    if organic:
        log(f"[bootstrap] WARNING: {organic} organic rollout(s) present "
            f"({'--force given' if args.force else 'dry run'})")

    existing = existing_keys()
    kept, stats = generate(user, sources, limit=args.limit,
                           tables_per_source=args.tables, max_rows=args.max_rows,
                           use_llm=args.llm_questions, skip_keys=existing, log=log)

    report = {
        "user": user["email"], "role": user["role"], "sources": sources,
        "dry_run": bool(args.dry_run), "reward": args.reward,
        "reward_source": REWARD_SOURCE, "mode": MODE,
        "already_present": len(existing), "organic_rollouts": organic,
        **stats, "shape": shape_report(kept),
        "written": 0,
    }

    if args.dry_run:
        log(f"[bootstrap] DRY RUN — would write {len(kept)} rollouts, wrote 0")
        for r in kept[:10]:
            log(f"    {r['source']}.{r['table']}  Q: {r['prompt']}")
            log(f"        {r['sql']}")
        if len(kept) > 10:
            log(f"    … and {len(kept) - 10} more")
    else:
        report["written"] = len(write(user, kept, reward=args.reward))
        log(f"[bootstrap] wrote {report['written']} rollouts "
            f"({len(existing)} already present were skipped)")

    log(f"[bootstrap] keep rate: {len(kept)}/{stats['considered']} candidates "
        f"({stats['keep_rate'] * 100:.1f}%) — {stats['unfaithful']} dropped as unfaithful, "
        f"{stats['failed']} dropped for failing to execute, "
        f"{stats['duplicate']} already present. "
        f"Of the SQL actually executed, {stats['verify_rate'] * 100:.1f}% ran clean")
    sh = report["shape"]
    log(f"[bootstrap] SQL shapes: {sh['distinct_shapes']} distinct shapes / "
        f"{sh['distinct_sql']} distinct statements / {sh['rollouts']} rollouts "
        f"({sh['shape_fraction'] * 100:.1f}% of rollouts are a shape of their own) — "
        f"this, not the row count, is what the adapter can learn")
    log(f"[bootstrap] remove later with: "
        f"DELETE FROM agent_traces WHERE reward_source='{REWARD_SOURCE}';")
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    return report


if __name__ == "__main__":
    run()
