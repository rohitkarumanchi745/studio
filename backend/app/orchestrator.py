"""Cross-database orchestration — dependency graph + terminal combiner.

                    ┌→ Snowflake agent ─┐
   User question ──┼→ Databricks agent ┼→ Aggregator → one synthesized answer
                    └→ SAP agent ───────┘

Independent planners propose only from sources the user's role can access; a
deterministic gate selects one validated whole graph, which may make one agent
depend on another's bounded result. Independent nodes still run in parallel.
The terminal either synthesizes prose or, for ``combine=table``,
uses the guarded in-memory blender to return one federated table. An unusable
plan falls back to the original all-source fan-out.

Fan-out is thread-safe: one thread per source, each with its own connector.
It works with or without an LLM key — a keyless agent answers in its
deterministic fallback, and the aggregator falls back to a per-source summary.
"""
import concurrent.futures
import json
import os
import re

from . import agent, agent_graph, jobs, lightning, progress, rbac, roster, skills, util
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

    graph_enabled = os.getenv("STUDIO_AGENT_GRAPH", "1").lower() \
        not in ("0", "false", "no")

    if len(sources) == 1 and not graph_enabled:
        # One accessible source → its worker answers directly, no aggregator.
        s = sources[0]
        result = agent.run_agent(prompt, s["connector"], "*", s["allowed"], s["schemas"],
                                 history, user, model, skill_md=s["skill"])
        result["source"] = s["connector"].name
        result["agents_used"] = [s["connector"].name]
        result.setdefault("agents", [roster.worker(s["connector"].name)])
        return result

    spec = model or agent.llm_spec()

    # Plan the shape of this turn, then run it. A planned graph lets one
    # source's rows feed another's question ("the accounts from Postgres, then
    # their spend in Snowflake"), which the blind fan-out below cannot express.
    # With no LLM key, a single source, or an unusable plan, plan_graph returns
    # the FLAT graph — every source, no dependencies — which executes as the
    # same parallel fan-out this function always did. So the fallback path is
    # the old behavior rather than an approximation of it, and _fanout stays
    # for the kill switch.
    graph = None
    plan = None
    run = None
    if graph_enabled:
        try:
            plan = agent_graph.plan_graph(prompt, sources, user, model)
        except agent_graph.GraphLimitExceeded as exc:
            # A planner may safely narrow a large roster. If planning is
            # unavailable or unusable, however, the fallback would fan out to
            # every source. Refuse that turn cleanly instead of silently
            # dropping sources or bypassing the hard graph budget.
            detail = str(exc)
            return {
                "text": detail,
                "sql": None, "columns": [], "rows": [], "chart": None,
                "panels": [], "email": None, "errors": [detail],
                "mode": "orchestrated", "model": spec, "source": "*",
                "agents_used": [], "agents": [roster.ORCHESTRATOR],
                "graph": {
                    "nodes": [
                        {"id": "__supervisor__", "source": "*",
                         "kind": "supervisor", "agent": roster.ORCHESTRATOR["name"],
                         "task": "select a bounded source roster", "depends_on": [],
                         "status": "failed", "rows": 0, "depth": 0,
                         "dynamic": False, "spawned_by": None},
                        {"id": "__reason__", "source": "*", "kind": "reasoner",
                         "agent": roster.AGGREGATOR["name"],
                         "task": "synthesize one answer", "depends_on": [],
                         "status": "skipped", "rows": 0, "depth": None,
                         "dynamic": False, "spawned_by": None},
                    ],
                    "edges": [], "combine": "reason", "why": detail,
                    "planned": False, "dynamic": False,
                    "spawn_rejections": [],
                },
            }
        if plan.get("planned"):
            formation = plan.get("formation") or {}
            council = formation.get("planners") or []
            if council:
                progress.emit(
                    f"{len(council)} independent planner(s) formed a "
                    f"{len(plan['nodes'])}-agent graph; the server selected "
                    f"{formation.get('selected') or 'a validated proposal'}")
            else:
                progress.emit(f"planned {len(plan['nodes'])} agent(s): "
                              + (plan.get("why") or "").strip())
        else:
            progress.emit("fanning out to " + ", ".join(
                roster.name_for(s["connector"].name) for s in sources))
        run = agent_graph.execute(plan, sources, prompt, user, model, conversation_id,
                                  history=history)
        jobs.check_claim()
        # Workers may have requested children while executing. Every terminal
        # operation must use the server-materialized runtime plan, not the seed.
        plan = run.get("runtime_plan") or plan
        graph = run["graph"]
        # execute() already recorded each node's rollout, so the per-worker
        # loop below is skipped for this path — scoring a worker twice would
        # double-weight it in its own policy.
        subs = [run["results"][nid] for nid in run["order"]]
    else:
        progress.emit("fanning out to " + ", ".join(
            roster.name_for(s["connector"].name) for s in sources))
        subs = _fanout(prompt, sources, user, model)
        for sub in subs:
            lightning.record_agent_rollout(
                user, conversation_id, prompt, roster.name_for(sub["_source"]),
                "worker", sub, conditioning_prompt=prompt)

    panels, errors = [], []
    for sub in subs:
        for p in sub.get("panels") or []:
            # Stamp every panel with the named agent that produced it.
            panels.append({**p, "source": sub["_source"],
                           "agent": roster.name_for(sub["_source"])})
        errors.extend(sub.get("errors") or [])

    table_requested = bool(plan and plan.get("combine") == "table")
    blended, blend_error = None, None
    if table_requested:
        jobs.check_claim()
        progress.emit("Aggregator: blending the agents' results into one table")
        try:
            blended = agent_graph.blend_parts(plan, run["results"], user)
            if blended is None:
                blend_error = ("every planned node must complete with verified SQL, "
                               "and at least two usable parts are required")
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc) or type(exc).__name__
            blend_error = str(detail)[:300]
        if blend_error:
            errors.append(f"Table combine failed: {blend_error}")

    jobs.check_claim()
    progress.emit("Aggregator: synthesizing one answer from "
                  f"{len(subs)} agents' results")
    reasoner_spec = agent_graph.reasoning_model_spec(spec)
    text = _aggregate(prompt, subs, user, reasoner_spec)
    # The provider can block while the durable background claim is reclaimed.
    # A stale owner may not publish a terminal answer or a training trace.
    jobs.check_claim()
    if blend_error:
        text = f"Could not produce the requested combined table: {blend_error}.\n\n{text}"
    last = next((r for r in subs if r.get("sql")), None)

    # A table plan returns the federated table itself, never the last worker's
    # unrelated result.  The single panel also makes the canvas display that
    # table rather than the pre-blend worker panels.  The combine statement is
    # valid ONLY inside blend.py's throwaway DuckDB, so keep it as blend_sql;
    # ordinary `sql` fields mean "replayable against source" and must stay null.
    # If the blend failed, keep the worker panels for diagnosis but leave the
    # top-level table empty.
    if blended is not None:
        table_chart = {"type": "table", "title": "Blended table"}
        columns, rows, sql, chart, source = (
            blended["columns"], blended["rows"], None, table_chart, "*")
        panels = [{"sql": None, "columns": blended["columns"],
                   "rows": blended["rows"], "chart": table_chart,
                   "source": "*", "agent": roster.AGGREGATOR["name"]}]
    elif table_requested:
        columns, rows, sql, chart, source = [], [], None, None, "*"
    else:
        columns = last["columns"] if last else []
        rows = last["rows"] if last else []
        sql = last["sql"] if last else None
        chart = last["chart"] if last else None
        source = last["_source"] if last else subs[0]["_source"]

    if graph is not None:
        # The reasoner can still return a useful partial synthesis/table, but
        # the requested graph did not complete when any planned worker failed
        # or was skipped.  Preserve the partial artifact while marking the
        # terminal outcome honestly for UI and learning.
        terminal_status = "failed" if errors else "ok"
        graph = agent_graph.describe(plan, run["results"], terminal_status,
                                     len(rows))

    # Per-agent reward shaping: each worker is scored on ITS own answer (done
    # per node in agent_graph.execute, or in the kill-switch branch above), the
    # aggregator on ITS synthesis — separate rollouts, separate policies.
    reward_result = {"text": text, "sql": sql, "columns": columns, "rows": rows,
                     "chart": chart, "panels": panels, "errors": errors,
                     "model": reasoner_spec, "source": source}
    if blend_error:
        # agent_reward treats this prefix as a terminal failure.  Do not award
        # synthesis credit when the requested artifact was not produced.
        reward_result["text"] = f"(Orchestrator error: {blend_error})"
    jobs.check_claim()
    lightning.record_agent_rollout(
        user, conversation_id, prompt, roster.AGGREGATOR["name"], "aggregator",
        reward_result, conditioning_prompt=_aggregate_prompt(prompt, subs),
        graph_meta={"aggregate": True})

    executed_subs = [r for r in subs if r.get("_status") != "skipped"]
    planner_agents = [
        {"name": p.get("agent") or p.get("role") or "Graph planner",
         "source": "*", "role": "planner", "planner_role": p.get("role"),
         "selected": bool(p.get("selected"))}
        for p in (((plan or {}).get("formation") or {}).get("planners") or [])
    ]

    return {
        "text": text,
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "chart": chart,
        "panels": panels,
        "email": next((r["email"] for r in subs if r.get("email")), None),
        "errors": errors,
        "mode": "orchestrated",
        "model": reasoner_spec,
        "source": source,
        "agents_used": [r["_source"] for r in executed_subs],
        # The full named crew for this turn: independent formation planners,
        # every worker that ran, and the Aggregator that synthesized them.
        "agents": planner_agents + [
            {**roster.worker(r["_source"]),
             **({"node_id": r["_node"], "spawned_by": r.get("_spawned_by"),
                 "depth": r.get("_depth", 0)} if r.get("_node") else {})}
            for r in executed_subs
        ] + [roster.AGGREGATOR],
        # The topology this turn actually ran, for audit and observability.
        # This is serialized execution state, not the mechanism that drives
        # execution. None means the classic fan-out kill-switch path ran.
        "graph": graph,
        **({"row_count": blended["row_count"], "blend_sql": blended["sql"],
            "parts": blended["parts"],
            "lineage": blended["lineage"],
            "blend_provenance": blended["blend_provenance"]}
           if blended is not None else {}),
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


_AGG_SYS = """You are the terminal reasoner over a runtime graph of data agents. Some agents ran independently; others were spawned as specialists or consumed bounded rows from an upstream agent. Synthesize ONE answer.

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

    # The deployed self-hosted adapter emits guarded tool actions, not prose
    # synthesis. Without a separately configured frontier, deterministic
    # aggregation is honest; rendering action JSON as an answer is not.
    try:
        if agent.self_hosted(agent.concrete_model_spec(spec)):
            heads = ", ".join(s["_source"] for s in subs)
            return f"Combined results from {heads}:\n\n{summary}" if summary else \
                f"Queried {heads}; see the panels for each database's result."
    except Exception:
        pass

    if not agent.llm_available(spec, user):
        heads = ", ".join(s["_source"] for s in subs)
        return f"Combined results from {heads}:\n\n{summary}" if summary else \
            f"Queried {heads}; see the panels for each database's result."

    try:
        llm = agent.make_llm(spec, user)
        reply = llm.invoke([("system", _AGG_SYS),
                            ("user", _aggregate_prompt(prompt, subs))])
        text = reply.content if isinstance(reply.content, str) else "".join(
            b.get("text", "") for b in reply.content if isinstance(b, dict))
        return text.strip() or summary
    except Exception:
        return summary or "Combined results — see the panels for each database."


def _aggregate_prompt(prompt, subs):
    """Exact user message supplied to the synthesis model and its trainer."""
    payload = json.dumps([{
        "node": s.get("_node"),
        "source": s["_source"],
        "spawned_by": s.get("_spawned_by"),
        "answer": s.get("text"),
        "sql": s.get("sql"),
        "columns": s.get("columns"),
        "total_rows": len(s.get("rows") or []),
    } for s in subs], default=str)
    return f"Question: {prompt}\n\nPer-database answers:\n{payload}"
