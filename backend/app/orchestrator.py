"""Cross-database orchestration.

Every configured data source the user's role can access gets its own agent,
briefed by that source's skill file (skills.py — RBAC-scoped tables/schemas,
auto-refreshed on schema or access changes). The orchestrator is an agent
whose tools are those per-database agents: it routes each question to the
right database(s), lets each source agent write and run its own SQL, and
synthesizes the answers. Sub-agent charts accumulate as panels so a
cross-database question renders one dashboard.

Without an LLM key the orchestrator degrades to deterministic routing: the
source whose tables best match the prompt (catalog.match_tables) handles it
in fallback mode.
"""
import json
import re

from . import agent, rbac, skills
from .catalog import match_tables
from .connectors import all_sources, get_connector


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
    if not agent.llm_available(spec):
        return _route_fallback(prompt, sources, history, user, model)

    ctx = {"panels": [], "sub": [], "errors": []}
    tools = [_source_tool(s, ctx, user, model) for s in sources]
    system = _orchestrator_prompt(sources)

    from langchain.chat_models import init_chat_model

    try:
        llm = init_chat_model(spec)
        graph = agent._build_graph(llm, tools, system)
        messages = [("user" if h["role"] == "user" else "assistant", h["text"]) for h in history]
        messages.append(("user", prompt))
        result = graph.invoke({"messages": messages}, config={"recursion_limit": 20})
        text = agent._final_text(result) or "Done."
    except Exception as e:
        out = _route_fallback(prompt, sources, history, user, model)
        out["text"] = f"(Orchestrator error: {e}) — routed to one database instead.\n\n" + out["text"]
        return out

    last = next((r for r in reversed(ctx["sub"]) if r.get("sql")), None)
    return {
        "text": text,
        "sql": last["sql"] if last else None,
        "columns": last["columns"] if last else [],
        "rows": last["rows"] if last else [],
        "chart": last["chart"] if last else None,
        "panels": ctx["panels"],
        "email": next((r["email"] for r in ctx["sub"] if r.get("email")), None),
        "errors": ctx["errors"],
        "mode": "orchestrated",
        "model": spec,
        "source": last["_source"] if last else sources[0]["connector"].name,
        "agents_used": [r["_source"] for r in ctx["sub"]],
    }


def _tool_name(source_name):
    return "ask_" + re.sub(r"\W", "_", source_name)


def _skill_description(skill_md):
    m = re.search(r"^description:\s*(.+)$", skill_md, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _source_tool(s, ctx, user, model):
    """Wrap one database's agent as an orchestrator tool. The sub-agent gets
    the source's skill file and only that source's RBAC-allowed tables."""
    conn = s["connector"]

    def _ask(question: str) -> str:
        sub = agent.run_agent(question, conn, "*", s["allowed"], s["schemas"],
                              [], user, model, skill_md=s["skill"])
        sub["_source"] = conn.name
        ctx["sub"].append(sub)
        for p in sub.get("panels") or []:
            ctx["panels"].append({**p, "source": conn.name})
        ctx["errors"].extend(sub.get("errors") or [])
        return json.dumps({
            "source": conn.name,
            "answer": sub.get("text"),
            "sql": sub.get("sql"),
            "columns": sub.get("columns"),
            "rows_preview": (sub.get("rows") or [])[:20],
            "total_rows": len(sub.get("rows") or []),
            "charts": len(sub.get("panels") or []),
        }, default=str)

    _ask.__name__ = _tool_name(conn.name)
    _ask.__doc__ = f"""{_skill_description(s['skill']) or f'Ask the {conn.name} database agent.'}

    Args:
        question: A plain-English analytics question answerable from the
            {conn.name} source alone. The agent writes and runs the SQL and
            may render charts; you get back its answer plus a result preview.
    """
    from langchain_core.tools import tool as lc_tool
    return lc_tool(_ask)


def _orchestrator_prompt(sources):
    briefs = "\n".join(
        f"- {_tool_name(s['connector'].name)} — {s['connector'].name} "
        f"({s['connector'].dialect}); tables: {', '.join(s['allowed'][:12])}"
        for s in sources
    )
    return f"""You are Studio's orchestrator. You answer analytics questions by delegating to per-database agents — one per data source, each seeing only the tables this user may access:

{briefs}

Rules:
- Route to the single best database when one source can answer. Call several
  agents only when the question genuinely spans databases; then compare or
  combine their answers yourself.
- Phrase each agent's question in that database's own terms — an agent knows
  nothing about the other sources.
- Never invent numbers: report only what the agents returned. Their charts
  and tables are already shown to the user as panels.
- If an agent errors or lacks the data, say so or try a better-suited source.
- Final answer: a short, direct synthesis (2-4 sentences) with concrete
  numbers, noting which database(s) it came from."""


def _route_fallback(prompt, sources, history, user, model):
    """No LLM key: deterministic routing — the source whose table/column names
    best match the prompt answers in fallback mode."""
    best, best_score = sources[0], -1
    for s in sources:
        score = sum(m["score"] for m in match_tables(prompt, s["schemas"]))
        if score > best_score:
            best, best_score = s, score
    result = agent.run_agent(prompt, best["connector"], "*", best["allowed"], best["schemas"],
                             history, user, model, skill_md=best["skill"])
    result["source"] = best["connector"].name
    result["agents_used"] = [best["connector"].name]
    return result
