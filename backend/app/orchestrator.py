"""Cross-database orchestration — parallel fan-out + aggregator.

                    ┌→ Snowflake agent ─┐
   User question ──┼→ Databricks agent ┼→ Aggregator → one synthesized answer
                    └→ SAP agent ───────┘

Every configured source the user's role can access gets its own agent,
briefed by that source's skill file (skills.py — RBAC-scoped tables/schemas).
The agents are INDEPENDENT: the question is fanned out to all of them
concurrently — each writes and runs its own SQL against its own source, with
no knowledge of the others — and an aggregator then synthesizes their answers
into one. Every agent's charts accumulate as panels, so a cross-database
question renders one dashboard.

Fan-out is thread-safe: one thread per source, each with its own connector.
It works with or without an LLM key — a keyless agent answers in its
deterministic fallback, and the aggregator falls back to a per-source summary.
"""
import concurrent.futures
import json
import re

from . import agent, lightning, progress, rbac, roster, skills, util
from .connectors import all_sources, get_connector

MAX_PARALLEL = 6


def accessible_sources(user, max_schema_tables=10):
    """[{connector, allowed, schemas, skill}] for every configured source the
    user's role may query — the roster of database agents. Sources are probed
    concurrently (list_tables + schema fetch are independent per source), so the
    roster's latency is the slowest single source, not their sum."""
    role = user["role"]

    def build(meta):
        name = meta["name"]
        if name not in rbac.allowed_sources(role) or not meta["configured"]:
            return None
        conn = get_connector(name)
        try:
            all_tables = conn.list_tables()
        except Exception:
            return None  # unreachable source — leave it off the roster
        allowed = rbac.allowed_tables(role, name, all_tables)
        if not allowed:
            return None
        tabs = allowed[:max_schema_tables]
        cols = util.pmap(conn.get_schema, tabs, default=[])
        schemas = {t: (c or []) for t, c in zip(tabs, cols)}
        return {"connector": conn, "allowed": allowed, "schemas": schemas,
                "skill": skills.get_skill(conn, role, allowed, schemas)}

    return [e for e in util.pmap(build, all_sources(), workers=MAX_PARALLEL) if e]


def _norm(name):
    """Lowercase, every non-alphanumeric run → one space (same rule for prompt
    and table names, so `web-traffic`, `analytics.events`, `ecommerce_orders`
    all compare as plain words)."""
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def ambiguous_tables(prompt, sources):
    """Tables the prompt NAMES that exist in MORE THAN ONE accessible source →
    [{table, sources}], sorted by table. Studio never guesses which database a
    same-named table means; the caller asks the user instead.

    A table counts as named only when its normalized name appears as a whole
    phrase in the normalized prompt ("sales", "ecommerce orders", "web traffic").
    Deliberately no stemming: "in order of region" must not trip `orders`, nor
    "the status of the migration" trip `status` — if the user didn't name the
    table, the safe default is the ordinary fan-out (every source answers and
    the Aggregator cites each)."""
    text = " " + _norm(prompt) + " "
    owners = {}
    for s in sources:
        for t in s.get("allowed") or []:
            owners.setdefault(t.lower(), set()).add(s["connector"].name)
    out = []
    for t, names in sorted(owners.items()):
        phrase = _norm(t)
        if len(names) >= 2 and phrase and f" {phrase} " in text:
            out.append({"table": t, "sources": sorted(names)})
    return out


def run_orchestrated(prompt, user, history, model=None, conversation_id=None,
                     sources=None):
    """One multi-database turn. Same result shape as agent.run_agent, plus
    agents_used; mode is "orchestrated". Each worker and the aggregator is
    scored as its own rollout (per-agent reward shaping), so their policies
    improve independently. `sources`: an already-built roster (the caller
    probed it for the ambiguity check) — else built here."""
    sources = sources if sources is not None else accessible_sources(user)
    if not sources:
        return {"text": "No accessible data sources.", "sql": None, "columns": [],
                "rows": [], "chart": None, "panels": [], "email": None,
                "errors": [], "mode": "orchestrated", "model": None,
                "source": "*", "agents_used": []}

    if len(sources) == 1:
        # One accessible source → its worker answers directly, no aggregator.
        s = sources[0]
        result = agent.run_agent(prompt, s["connector"], "*", s["allowed"], s["schemas"],
                                 history, user, model, skill_md=s["skill"])
        result["source"] = s["connector"].name
        result["agents_used"] = [s["connector"].name]
        result.setdefault("agents", [roster.worker(s["connector"].name)])
        return result

    progress.emit("fanning out to " + ", ".join(
        roster.name_for(s["connector"].name) for s in sources))
    spec = model or agent.llm_spec()
    subs = _fanout(prompt, sources, user, model)

    panels, errors = [], []
    for sub in subs:
        for p in sub.get("panels") or []:
            # Stamp every panel with the named agent that produced it.
            panels.append({**p, "source": sub["_source"],
                           "agent": roster.name_for(sub["_source"])})
        errors.extend(sub.get("errors") or [])

    progress.emit("Aggregator: synthesizing one answer from "
                  f"{len(subs)} agents' results")
    text = _aggregate(prompt, subs, user, spec)
    last = next((r for r in subs if r.get("sql")), None)

    # Per-agent reward shaping: each worker scored on ITS own answer, the
    # aggregator on ITS synthesis — separate rollouts, separate policies.
    for sub in subs:
        lightning.record_agent_rollout(
            user, conversation_id, prompt, roster.name_for(sub["_source"]), "worker", sub)
    lightning.record_agent_rollout(
        user, conversation_id, prompt, roster.AGGREGATOR["name"], "aggregator",
        {"text": text, "panels": panels, "model": spec})

    return {
        "text": text,
        "sql": last["sql"] if last else None,
        "columns": last["columns"] if last else [],
        "rows": last["rows"] if last else [],
        "chart": last["chart"] if last else None,
        "panels": panels,
        "email": next((r["email"] for r in subs if r.get("email")), None),
        "errors": errors,
        "mode": "orchestrated",
        "model": spec,
        "source": last["_source"] if last else subs[0]["_source"],
        "agents_used": [r["_source"] for r in subs],
        # The full named crew for this turn: every worker that ran + the
        # Aggregator that synthesized them.
        "agents": [roster.worker(r["_source"]) for r in subs] + [roster.AGGREGATOR],
    }


def _fanout(prompt, sources, user, model):
    """Scatter: run every source's agent concurrently and independently. One
    thread per source (each has its own connector), so the agents never touch
    each other's state. Returns each agent's result tagged with its source."""
    # Pool threads don't inherit the live-activity contextvar — capture the
    # task id here and rebind inside each worker so its emits still land.
    tid = progress.current()

    def _ask(s):
        conn = s["connector"]
        progress.bind(tid)
        try:
            sub = agent.run_agent(prompt, conn, "*", s["allowed"], s["schemas"],
                                  [], user, model, skill_md=s["skill"])
            progress.emit_for(tid, f"{roster.name_for(conn.name)}: finished "
                                   f"({len(sub.get('rows') or [])} rows)")
        except Exception as e:
            progress.emit_for(tid, f"{roster.name_for(conn.name)}: failed ({str(e)[:80]})")
            sub = {"text": f"(agent error: {e})", "sql": None, "columns": [],
                   "rows": [], "chart": None, "panels": [], "errors": [str(e)]}
        sub["_source"] = conn.name
        return sub

    subs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(sources), MAX_PARALLEL)) as ex:
        for sub in ex.map(_ask, sources):
            subs.append(sub)
    # Deterministic order regardless of which agent finished first.
    subs.sort(key=lambda r: r["_source"])
    return subs


_AGG_SYS = """You are the aggregator over independent per-database agents. Each agent answered the user's question from its own data source, in isolation. Synthesize ONE answer.

Rules:
- Use only what the agents returned — never invent numbers.
- Compare and combine across sources where they relate; note which database each figure came from.
- Their charts and tables are already shown to the user as panels; don't paste raw tables.
- 2–5 sentences, direct, with concrete numbers."""


def _aggregate(prompt, subs, user, spec):
    """Reduce: synthesize the independent answers into one. LLM if available,
    else a deterministic per-source summary."""
    named = [s for s in subs if (s.get("text") or "").strip()]
    summary = "\n\n".join(f"**{s['_source']}** — {s['text'].strip()}" for s in named)

    if not agent.llm_available(spec, user):
        heads = ", ".join(s["_source"] for s in subs)
        return f"Combined results from {heads}:\n\n{summary}" if summary else \
            f"Queried {heads}; see the panels for each database's result."

    try:
        llm = agent.make_llm(spec, user)
        payload = json.dumps([{
            "source": s["_source"],
            "answer": s.get("text"),
            "sql": s.get("sql"),
            "columns": s.get("columns"),
            "total_rows": len(s.get("rows") or []),
        } for s in subs], default=str)
        reply = llm.invoke([("system", _AGG_SYS),
                            ("user", f"Question: {prompt}\n\nPer-database answers:\n{payload}")])
        text = reply.content if isinstance(reply.content, str) else "".join(
            b.get("text", "") for b in reply.content if isinstance(b, dict))
        return text.strip() or summary
    except Exception:
        return summary or "Combined results — see the panels for each database."
