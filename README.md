# Studio

An agentic analytics platform. Ask your data anything in plain English — a crew
of named agents writes the SQL, runs it (read-only, RBAC-enforced), and renders
Power BI / Tableau-class visualizations you can pin, cross-filter, and share.
Beyond chat it builds **verified SQL**, **prompt-driven pipelines** with data
lineage, a **safe-production pipeline flow** (generate → validate → approve →
deploy → run) with a human in the loop, **governance-as-code**, and a learning
loop (**Agent Lightning**) that records every run as a rewarded rollout.

**Live demo:** https://studio-production-ac35.up.railway.app

---

## Architecture

```mermaid
flowchart TB
    subgraph client["Browser — React + Vite"]
        chat["Chat + Canvas<br/>ask · charts · pin · cross-filter"]
        views["Pipelines · Flow · Jobs · Governance<br/>Agents · Skills · Sessions · Dashboards"]
    end

    subgraph api["FastAPI — one origin, API at /api/*"]
        routers["Routers · auth · chat · pipelines · flow<br/>queries · governance · supervisor · sessions<br/>mcp · repos · dashboards · catalog"]

        subgraph agents["Agent layer"]
            orch["orchestrator.py<br/>fan-out + aggregator"]
            agent["agent.py<br/>LangGraph ReAct"]
            roster["roster.py<br/>named agents"]
            flowm["flow.py<br/>staged typed pipeline"]
            sup["supervisor.py<br/>risk + human-in-loop"]
        end

        subgraph gate["Governance + guard"]
            rbacm["rbac.py"]
            guard["queryguard.py"]
            gov["governance.py<br/>YAML policy + compliance"]
            verify["verify_sql · the execution gate"]
        end

        subgraph learn["Agent Lightning + caching"]
            light["lightning.py<br/>rollouts + reward"]
            qc["qcache.py<br/>semantic cache"]
            sess["sessions.py<br/>serialize + prompt cache"]
        end
    end

    subgraph state["State + cache"]
        pg[("PostgreSQL")]
        redis[("Redis<br/>tile cache")]
    end

    subgraph ext["External"]
        llm{{"LLM · Claude / GPT (BYOK)"}}
        mcp{{"MCP servers"}}
        gh{{"GitHub repos"}}
        src[("Connectors · demo · Snowflake<br/>Databricks · Neo4j · marketing APIs")]
    end

    client -->|JWT| routers
    routers --> agents
    agent <--> llm
    agents --> gate
    gate --> src
    agents --> learn
    agent -.tools.-> mcp
    flowm -.scripts.-> gh
    routers --> pg
    routers --> redis

    classDef store fill:#1f6f4a,stroke:#2e9e6b,color:#fff
    classDef extc fill:#2b4a7d,stroke:#3d6bb3,color:#fff
    class pg,redis store
    class llm,mcp,gh,src extc
```

**One gate for data.** Every path that returns rows — the agent's `run_sql`
tool, the verified-SQL library, a pipeline step, the flow's Validator, a cache
hit — funnels through the same `verify_sql`: RBAC check → query guard → real
execution → governance compliance filter. There is no second way to reach the
warehouse, so a permission can never be skipped by taking a different route.

### How a question becomes a chart

```mermaid
sequenceDiagram
    autonumber
    participant U as User
    participant API as FastAPI
    participant A as Agent (LangGraph)
    participant G as queryguard
    participant W as Warehouse
    participant L as Agent Lightning

    U->>API: "top 5 products by revenue in 2025"
    API->>API: RBAC — role may see this source/table?
    API->>API: semantic cache — seen a like question?
    API->>A: prompt + skill file (RBAC-scoped schemas)
    A->>G: run_sql(SELECT …)
    G->>G: single stmt · SELECT-only · allowlist · LIMIT
    G->>W: execute
    W-->>A: columns + rows
    A->>A: render_chart(type, x, y)
    A-->>API: text · sql · rows · chart · usage
    API->>L: record rollout (prompt, sql, reward)
    API-->>U: answer + chart on the canvas
```

Without an API key the agent degrades to a deterministic preview
(`SELECT * … LIMIT` + auto chart), so the whole flow stays demoable.

---

## The agent platform

### A named crew, not one model

Every connected source gets its own **worker agent**, briefed by an
auto-generated *skill file* (`skills.py`) that lists only the tables the current
user's role may touch. A cross-source question fans out to every accessible
worker and an **Aggregator** synthesizes their answers; a staged pipeline adds a
**Pipeline planner**, **Code generator**, **Validator**, **Approval agent**, and
a **Deployment executor**; the verified-SQL path is served by a **SQL verifier**.
`roster.py` gives each a stable name, so every answer reports which agent(s) ran
and each rollout is attributed to them.

```mermaid
flowchart LR
    q["Question (source = all)"] --> orch["Orchestrator"]
    orch --> w1["Snowflake agent"]
    orch --> w2["Databricks agent"]
    orch --> w3["Demo agent"]
    w1 --> agg["Aggregator"]
    w2 --> agg
    w3 --> agg
    agg --> ans["one synthesized answer<br/>+ per-agent chart panels"]
```

The skill file carries a fingerprint of `(dialect, allowed tables, schemas)` and
rebuilds itself the moment a schema or a role's access changes — the **Skill
files** view shows exactly what each agent knows.

### Verified SQL library

The agent writes SQL; a user can edit it, **verify** it (RBAC + guard + real
execution), and only then save it with the requirement prompt that produced it.
Verification is a hard gate — a query that doesn't run is never stored, and
re-running a saved query re-validates against the *runner's* role, so the
library can never become an RBAC bypass.

### Prompt-built pipelines + data lineage

Describe a job; the **Pipeline planner** routes it to the source whose tables
best match, drafts an ordered set of **verified** steps, and can pick the right
**GitHub repo** (`repos.py`) whose scripts fit the prompt. A pipeline renders a
**source → table → step lineage diagram** so a multi-source request shows
exactly where each table comes from; a failed step emails the requester naming
the failing source/table, and every run is traced through Agent Lightning.

### The staged flow — safe production behavior

`flow.py` turns one business request into a chain where **each stage is a named
agent that emits a typed JSON contract**, and each is recorded as a rollout:

```mermaid
flowchart TB
    req["Business request"] --> plan["Pipeline planner → PipelineSpec"]
    plan --> gen["Code generator → GeneratedArtifact"]
    gen --> val{"Validator → ValidationResult"}
    val -- fail --> rep["repair ×N"]
    rep --> val
    rep -- still failing --> hr["human review · STOP before approval"]
    val -- ok --> appr["Approval agent / human → DeploymentRequest"]
    appr -- reject --> pb["policy block (no bypass)"]
    appr -- ok --> dep{"Deploy"}
    dep -- fail --> de["report error · do NOT bypass policy"]
    dep -- ok --> run{"Run"}
    run -- fail --> rt["platform retries"]
    rt -- still failing --> ro["stop · alert · rollback"]
    run -- ok --> ex["ExecutionResult"]
```

Read-only deployments run through; writes and Spark jobs hit the human-approval
gate. Deploy and Run are distinct phases — a deploy error is reported and never
bypasses policy; a run that keeps failing after the platform's retries is
stopped, the requester is alerted, and the deployment is rolled back
(best-effort, via an opt-in connector `rollback` hook).

### Supervised execution + human-in-the-loop

Studio is read-only by default. Running a script or Spark job against a real
environment passes a **supervisor agent** (`supervisor.py`): read-only
statements auto-approve; writes, DDL, and jobs require a human (admin) to
approve before anything runs. Execution failures retry, then escalate — the
requester is emailed and an admin approves a retry or aborts. Studio *generates*
Python but never executes arbitrary code itself; running always goes through
this gate.

### Governance-as-code

One YAML document (`governance.py`) owns per-role source/table access **and**
per-table compliance (deny columns, mask columns, row caps). Validate, then
apply — it hot-reloads with no redeploy and is enforced at every `run_query`
exit, so even `SELECT *` can't leak a denied column. Clearing it reverts to
built-in RBAC.

### Extending the agent — MCP, Build Python, graph DBs

Register **MCP servers** (`mcp.py`) exposing your existing scripts or internal
tools and agents pick them up automatically. **Build Python** (`pybuild.py`)
drafts a module in the style of your existing scripts using that MCP context.
A **Neo4j / Cypher** connector (`graph_conn.py`) sits behind the same interface.

---

## Agent Lightning — the learning loop

Modeled on Microsoft's [Agent Lightning](https://github.com/microsoft/agent-lightning):
every run becomes a **rollout** (prompt → actions → outcome) with a **reward**,
persisted to the `agent_traces` table.

```mermaid
flowchart LR
    run["Any run<br/>chat · pipeline · flow · SQL-verify"] --> roll[("agent_traces<br/>prompt · sql · reward · agents")]
    roll --> fb["👍 / 👎 overwrites the heuristic reward"]
    roll --> fail["recent failures → system prompt<br/>(immediate, in-context)"]
    roll --> apo["APO distills low-reward traces<br/>→ prompts/system_learned.txt"]
    roll --> exp["export_rollouts() → JSONL<br/>(ready for VERL / GRPO)"]
    fb --> apo
```

**Is it reinforcement learning?** Yes in structure, no in the usual sense. The
rollout + reward machinery *is* RL. But Studio runs on hosted models (Claude /
GPT) whose weights are frozen, so it can't do gradient/weight RL. Instead it
optimizes the **prompt** — recent failures injected in-context (immediate) and
APO distilling low-reward traces offline (RLAIF-style). The traces export in the
exact shape a real RL trainer consumes, so the day a self-hosted open-weight
model is added, the same rewarded data drives true weight RL — nothing about
collection changes.

**Train our own model.** `GET /training` reports prompts collected vs. a
threshold, reward-labeled and human-rated counts, and readiness; the admin
**Agents** panel shows the progress bar. `POST /training/export` writes the
rollout JSONL.

---

## Performance

```mermaid
flowchart TB
    q["User query"] --> sem{"Semantic cache<br/>role · source · table · signature"}
    sem -- hit --> re["re-execute SQL<br/>RBAC + guard + governance → fresh rows"]
    sem -- miss --> ag["Agent turn"]
    ag --> pc["Prompt cache · system + skill prefix<br/>(Anthropic cache_control)"]
    ag --> kv["KV reuse across turns<br/>history cache breakpoint"]
    ag --> par["parallel schema fetch · util.pmap"]
    ag --> st["cache the plan (SQL + chart)"]
```

- **Independent I/O runs in parallel** (`util.pmap`) — per-source `list_tables`
  + schema fetches in the orchestrator, pipelines, and whole-source chat load
  concurrently, so latency is the slowest source, not their sum. (Agent tools
  stay sequential — they share the turn's context by design; the orchestrator
  fans agents out in parallel instead.)
- **Prompt caching** marks the large stable system/skill prefix with an
  Anthropic cache breakpoint — the provider keeps its KV cache warm and bills
  reads at ~10%.
- **KV reuse across conversation turns** marks the last prior-turn message with
  a second breakpoint, so each turn reuses the whole system + history prefix.
- **Semantic query cache** (`qcache.py`) caches a successful run's plan under a
  normalized token signature; a semantically-similar prompt reuses it but
  **always re-executes the SQL** through RBAC + guard + governance — fresh rows,
  access re-checked, never stale and never a bypass.

`usage` on each answer (input/output + cache-read/write tokens) makes the reuse
measurable.

---

## Sessions & concurrent tasks

- **Serialized agent sessions** (`sessions.py`) — every conversation is
  snapshotted with its model, scope, full transcript, and a hashed cacheable
  prefix, so a run can be paused, **resumed**, or **forked**; token + cache-read
  meters show the reuse.
- **Background tasks + blue dot** — `POST /chat/background` runs a turn in a
  worker pool and returns immediately, so you can start a question in one chat,
  switch to another and start a different one, and a **blue dot** lights up each
  conversation when its task finishes (cleared when opened).

---

## Visualization

22 chart types across three engines (ECharts default, Plotly, Vega-Lite), with a
data-shape fit check that hides types the current result can't support. A single
prompt drives the full **chart spec v2** — one merge patch over data *and* pixels:

| Layer | Where | What |
|---|---|---|
| `transform` | `viz.py`, server-side | calculated fields (sandboxed AST — no `eval`), table calcs (% of total, running total, rank, period-over-period, moving average), binning, date truncation, top-N with an "Other" bucket, filters, grouping |
| `format` | `format.js`, client-side | number/date/currency/percent formats, data labels, axis titles, legend, palettes, reference/target lines, conditional colours |

Stages run in a fixed order — `derive → bin → filters → unpivot → group → having
→ table_calc → top_n → sort → pivot → limit` — so the model emits *what*, never
*when*. **Multiple charts from one sentence**: each panel may carry its own
`SELECT`, so a finer grain the current result aggregated away is re-queried
through the same guard. **Cross-filtering** is a server-side predicate applied to
every tile — no JS mirror, so filtering means the same thing everywhere.

---

## Data architecture

State lives in **PostgreSQL** when `DATABASE_URL` is set, otherwise SQLite.
`db.py` wraps psycopg in a SQLite-shaped facade, so every statement is written
once and runs on both.

```mermaid
erDiagram
    users ||--o{ conversations : owns
    users ||--o{ dashboards : owns
    users ||--o{ agent_traces : scores
    users ||--o{ agent_sessions : serializes
    users ||--o{ chat_tasks : "runs in background"
    conversations ||--o{ messages : contains
    conversations ||--o{ conversation_shares : "shared via"
    dashboards ||--o{ dashboard_tiles : contains
    users ||--o{ pipelines : builds
    pipelines ||--o{ pipeline_runs : "traced by"
    users ||--o{ saved_queries : verifies
    users ||--o{ flow_runs : "runs staged flow"
    users ||--o{ supervised_jobs : submits
    users ||--o{ query_cache : "semantic cache"

    users {
        text id PK
        text email UK
        text role
    }
    messages {
        text id PK
        text content "JSON: text, sql, rows, chart, author_role"
    }
    dashboard_tiles {
        text id PK
        text sql
        text spec "chart spec v2"
    }
    agent_traces {
        text id PK
        double reward
        text reward_source
        text meta "agents, artifact"
    }
    agent_sessions {
        text id PK
        text messages
        text prefix_hash
        int cache_read_tokens
    }
    flow_runs {
        text id PK
        text spec
        text validation
        text deployment
        text execution
    }
```

Alongside these: `governance_docs`, `mcp_servers`, `github_repos` — each a small
table with a TEXT-uuid primary key (Postgres has no implicit autoincrement, and
a `REAL` epoch would round; the facade maps `REAL → DOUBLE PRECISION`).

**Two deliberate choices:** dashboards and saved queries store the **recipe**
(SQL + spec), never rows, so RBAC is evaluated at *view* time; messages **do**
store rows (that is what makes a chat replayable), which is exactly why sharing
is an RBAC boundary enforced at read time.

### Tile cache

```mermaid
flowchart LR
    req["POST /dashboards/{id}/data"] --> l1{"L1 · in-process LRU"}
    l1 -->|hit| out["rows → transform → chart"]
    l1 -->|miss| l2{"L2 · Redis<br/>studio:tile:role:source:sha1(sql)"}
    l2 -->|hit| out
    l2 -->|miss| wh[("Warehouse")]
    wh --> w["write L2 (TTL) + L1"] --> out
    classDef c fill:#1f6f4a,stroke:#2e9e6b,color:#fff
    class l1,l2 c
```

The **role is part of the cache key**, so two roles never share rows, and
`queryguard` still runs on every request — the cache only skips the warehouse
round-trip, never a permission check. Redis is optional and falls back to the
in-process cache silently.

---

## Security model

- **RBAC** — roles (admin / analyst / viewer) map to sources and tables in
  `rbac.py` (or the governance YAML), enforced in the catalog, the query guard,
  and the agent's schema context. A viewer cannot see `customers` (PII) at all.
- **Query guard** — single statement, SELECT-only, forbidden-keyword scan,
  per-role table allowlist, enforced `LIMIT`.
- **Read-only by default** — writes / DDL / Spark jobs only run through the
  supervisor + human approval; Studio never executes arbitrary Python.
- **Compliance filter** — governance strips denied columns, masks masked ones,
  and caps rows at every result exit — even `SELECT *` can't leak a denied field.
- **Sharing is an RBAC boundary** — messages carry rows, so enforcement happens
  when messages are **read**, keyed off the *reader's* role (an owner gets no
  bypass). Messages are stamped server-side with the author's role and released
  only to a role at least as privileged; hidden ones return a 🔒 placeholder.
- **No existence oracle** — a resource you cannot see returns **404, never 403**
  (`_own_or_404` everywhere), so the id space can't be probed.
- **Admin-only surfaces** — governance, the MCP + GitHub-repo registries, job
  approval, learning stats, and training readiness are all admin-gated.
- **Auth** — email/password JWT, plus an Entra ID seam (redirect flow + bearer
  validation against Microsoft's JWKS) converging on the same user + RBAC.

---

## Tradeoffs — why we chose this

| Decision | Why | Tradeoff accepted |
|---|---|---|
| **Hosted LLMs (Claude / GPT) via BYOK**, not self-hosted weights | No GPU fleet; users bring their own key; always the latest models | Can't do gradient/weight RL — learning is prompt-level |
| **Agent Lightning optimizes the prompt, not weights** (APO / RLAIF) | Frozen hosted weights make prompt-opt the reachable lever | No true policy-gradient learning until a self-hosted model is added — rollouts export ready for it |
| **Prompt/KV caching via provider `cache_control`** | Hosted APIs don't expose the raw attention KV cache | You cache the *prefix* (server-side, TTL-bound), not tensors |
| **Semantic cache re-executes the SQL** (never returns stored rows) | Fresh data + RBAC/guard/governance re-checked on every hit | A hit still pays the warehouse round-trip (but skips the LLM) |
| **Lexical-normalized signature**, not embeddings, for that cache | Zero model dependency; deterministic; works offline | Misses paraphrases with entirely different content words — embeddings are a pluggable upgrade |
| **Read-only by default; writes go through supervisor + a human** | An analytics tool must never silently mutate production | Every write is gated — latency and a human in the loop, by design |
| **Studio generates Python but never runs it** | Arbitrary code execution is the blast radius to avoid | Running requires the supervised Jobs path (an extra, deliberate step) |
| **Dashboards/queries store the recipe, not rows** | RBAC evaluated at view time, not frozen at pin time | Every view re-runs SQL (mitigated by the tile cache) |
| **Messages store rows** (replayable chat) | A chat has to render its past answers | Sharing becomes an RBAC boundary enforced at read time |
| **404, never 403**, for unauthorized resources | The id space must not be an existence oracle | Slightly less "helpful" errors — intentional |
| **Typed JSON contracts between flow stages** (Pydantic) | Explicit agent boundaries; every stage serializable + traceable | More ceremony than passing raw dicts |
| **Fail-fast flow + repair loop → human review** | A bad artifact must never reach approval or deploy | A borderline case a smarter model could fix still goes to a human |
| **Parallelize independent I/O, keep agent tools sequential** | Tools share the turn's context (`run_sql → render_chart` order matters) | No intra-turn tool parallelism — the orchestrator fans out across agents instead |
| **One SQLite-shaped facade over Postgres** | Write each statement once; runs on dev and prod | The facade must patch dialect gaps (`REAL → DOUBLE`, uuid PKs) |
| **Per-database agent + auto skill file** | RBAC-scoped context — an agent only ever sees tables the role may touch | The skill file must rebuild on any schema/access change (fingerprinted) |
| **LangGraph provider-neutral ReAct** | Swap Claude ↔ GPT without touching the graph or tools | Bound to LangChain's abstractions |
| **Background tasks on a thread pool**, not a broker/queue | One service, no extra infra; survives navigation | No cross-process durability — a restart drops in-flight tasks |
| **Single Railway service** (API serves the built frontend) | One origin, no CORS, one deploy, one healthcheck | Frontend and backend scale together |

---

## Deployment

```mermaid
flowchart LR
    gh["GitHub · main"] -->|push| build["Railway build · Dockerfile"]
    build --> svc["studio service · uvicorn :$PORT<br/>API + built frontend"]
    svc --- pg[("Postgres · DATABASE_URL")]
    svc --- rd[("Redis · REDIS_URL")]
    svc --> hc["/health · healthcheck gate"]
    classDef s fill:#1f6f4a,stroke:#2e9e6b,color:#fff
    class pg,rd s
```

A two-stage Dockerfile builds the frontend with Node, then serves it from
FastAPI alongside the API — one service, one origin, no CORS in production.
`railway.json` gates each deploy on `/health`, which reports which backends are
live: `{ "store": "postgres", "tile_cache": "redis", "agent": "ready" }`.

---

## Run it

### Backend
```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add ANTHROPIC_API_KEY or OPENAI_API_KEY
uvicorn app.main:app --reload --port 8000
```

### Frontend
```bash
cd frontend
npm install
npm run dev                      # http://localhost:5173 (proxies /api → :8000)
```

Or run it exactly as production does — `npm run build`, then start the backend
alone; FastAPI serves the built frontend on one origin.

### Demo logins
| Email | Password | Role | Sees |
|---|---|---|---|
| admin@studio.local | admin123 | admin | everything + governance, jobs, training |
| analyst@studio.local | analyst123 | analyst | everything (no admin surfaces) |
| viewer@studio.local | viewer123 | viewer | demo: sales, web_traffic only |

---

## Configuration

| Variable | Purpose |
|---|---|
| `STUDIO_LLM` | LangChain `init_chat_model` string — `anthropic:claude-sonnet-5`, `openai:gpt-4o`, … |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | Key for whichever provider `STUDIO_LLM` names (users may also BYOK) |
| `STUDIO_SECRET` | JWT signing secret — **must** be set in production |
| `DATABASE_URL` | Postgres; unset falls back to SQLite |
| `STUDIO_DB_PATH` | SQLite path — point at a mounted volume, or deploys wipe it |
| `REDIS_URL` | Tile cache; unset falls back to in-process |
| `STUDIO_PROMPT_CACHE` | Toggle Anthropic prompt/KV caching (default on) |
| `STUDIO_QCACHE_THRESHOLD` | Semantic-cache similarity threshold (default 0.82) |
| `STUDIO_TRAIN_THRESHOLD` | Prompts to collect before "ready to train" (default 500) |
| `STUDIO_MODELS` | The model menu offered in the composer |
| `STUDIO_MCP_SERVERS` | JSON map of MCP servers exposed to the agent as extra tools |
| `GITHUB_TOKEN` | Read private repos in the GitHub repo registry |
| `AZURE_TENANT_ID` / `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_GROUP_ROLE_MAP` | Entra SSO + group→role mapping |

Warehouse credentials (`SNOWFLAKE_*`, `DATABRICKS_*`, `NEO4J_*`) and the
marketing connectors are listed in `backend/.env.example`; sources appear in the
picker automatically once configured.

---

## Roadmap
- Streaming agent steps to the UI (LangGraph `stream`)
- Embedding-backed semantic cache (pluggable behind the current lexical signature)
- Self-hosted BitNet → true weight training from the rollouts. Studio is the
  producer + adapter server (`trainer.py`); a **CPU worker**
  (`scripts/train_online.py`, no GPU — BitNet's 1-bit base + small LoRA train on
  CPU) consumes the rollout stream and publishes a global tool-calling adapter +
  per-user style adapters that serving hot-swaps in. Simultaneous by design.
- Drill-through from a dashboard tile back into chat
- Scheduled dashboard email digests
