# Studio

Ask your data anything. A ChatGPT-style analytics studio: pick a data source
(Snowflake, Databricks, or the built-in demo warehouse), pick a table, ask a
question in plain English — an agent writes the SQL, runs it (read-only,
RBAC-enforced), and renders the answer with ECharts visualizations you can
switch between chart types.

## Architecture

```
frontend/  React + Vite + ECharts     — ChatGPT-style UI, chart-type switcher
backend/   FastAPI                    — auth (JWT), RBAC, catalog, chat
  agent    LangChain + LangGraph      — model-agnostic ReAct agent
             tools: run_sql (SELECT-only, RBAC-guarded) + render_chart
  connectors: demo (seeded SQLite) · Snowflake · Databricks
```

- **Any model**: `STUDIO_LLM` takes a LangChain `init_chat_model` string —
  `anthropic:claude-opus-4-8` (default), `openai:gpt-4o`, etc.
- **RBAC**: roles (admin / analyst / viewer) map to sources and tables in
  `backend/app/rbac.py`; enforced in the catalog, the SQL guard, and the
  agent's schema context. Viewers can't see `customers` (PII) at all.
- **Safety**: agent SQL passes a guard — single statement, SELECT-only,
  forbidden-keyword scan, table allowlist per role, enforced LIMIT.
- **Auth**: normal email/password login (JWT) for the prototype; the Azure
  SSO seam is stubbed in `backend/app/auth.py` with the MSAL flow documented —
  the rest of the app only ever consumes the JWT, so swapping identity
  providers touches nothing else.
- **No key? Still works**: without an LLM API key the agent degrades to a
  deterministic preview (SELECT * + auto chart), so the full flow stays demoable.

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
npm run dev                      # http://localhost:5173
```

### Demo logins
| Email | Password | Role | Sees |
|---|---|---|---|
| admin@studio.local | admin123 | admin | everything |
| analyst@studio.local | analyst123 | analyst | everything |
| viewer@studio.local | viewer123 | viewer | demo: sales, web_traffic only |

## Connecting real warehouses

Uncomment the connector package in `backend/requirements.txt`, `pip install`
again, and fill the env vars (see `.env.example`):

- **Snowflake**: `SNOWFLAKE_ACCOUNT/USER/PASSWORD/WAREHOUSE/DATABASE/SCHEMA`
- **Databricks**: `DATABRICKS_SERVER_HOSTNAME/HTTP_PATH/TOKEN`

They appear in the source picker automatically once configured.

## Roadmap
- Azure SSO (MSAL) — flow documented in `backend/app/auth.py`
- AAD-group → role mapping for RBAC
- Streaming agent steps to the UI (LangGraph `stream`)
- Saved dashboards from chat charts
