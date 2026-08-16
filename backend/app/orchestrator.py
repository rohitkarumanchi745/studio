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

from . import agent, rbac, skills
from .connectors import all_sources, get_connector

MAX_PARALLEL = 6


def accessible_sources(user, max_schema_tables=10):
    """[{connector, allowed, schemas, skill}] for every configured source the
    user's role may query — the roster of database agents."""
    out = []
    role = user["role"]
    for meta in all_sources():
        name = meta["name"]
        if name not in rbac.allowed_sources(role) or not meta["configured"]:
            continue
        conn = get_connector(name)
        try:
            all_tables = conn.list_tables()
        except Exception:
            continue  # unreachable source — leave it off the roster
        allowed = rbac.allowed_tables(role, name, all_tables)
        if not allowed:
            continue
        schemas = {}
        for t in allowed[:max_schema_tables]:
            try:
                schemas[t] = conn.get_schema(t)
            except Exception:
                schemas[t] = []
        out.append({
            "connector": conn,
            "allowed": allowed,
            "schemas": schemas,
            "skill": skills.get_skill(conn, role, allowed, schemas),
        })
    return out


def run_orchestrated(prompt, user, history, model=None):
    """One multi-database turn. Same result shape as agent.run_agent, plus
    agents_used; mode is "orchestrated"."""
    sources = accessible_sources(user)
    if not sources:
        return {"text": "No accessible data sources.", "sql": None, "columns": [],
                "rows": [], "chart": None, "panels": [], "email": None,
                "errors": [], "mode": "orchestrated", "model": None,
                "source": "*", "agents_used": []}

    if len(sources) == 1:
        s = sources[0]
        result = agent.run_agent(prompt, s["connector"], "*", s["allowed"], s["schemas"],
                                 history, user, model, skill_md=s["skill"])
        result["source"] = s["connector"].name
        result["agents_used"] = [s["connector"].name]
        return result

    spec = model or agent.llm_spec()
    subs = _fanout(prompt, sources, user, model)

    panels, errors = [], []
    for sub in subs:
        for p in sub.get("panels") or []:
            panels.append({**p, "source": sub["_source"]})
        errors.extend(sub.get("errors") or [])

    text = _aggregate(prompt, subs, user, spec)
    last = next((r for r in subs if r.get("sql")), None)
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
    }


def _fanout(prompt, sources, user, model):
    """Scatter: run every source's agent concurrently and independently. One
    thread per source (each has its own connector), so the agents never touch
    each other's state. Returns each agent's result tagged with its source."""
    def _ask(s):
        conn = s["connector"]
        try:
            sub = agent.run_agent(prompt, conn, "*", s["allowed"], s["schemas"],
                                  [], user, model, skill_md=s["skill"])
        except Exception as e:
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
