"""Row limits shared by every query path.

MAX_ROWS is the server ceiling: no result leaves the backend with more rows,
whatever LIMIT a query carries. PREVIEW_ROWS is how many rows the agent's
model is shown per query. They lived in agent.py, which made every module that
needed a cap (dashboards, qcache, supervisor, the gateway) import the whole
agent runtime for two integers; agent.py re-exports them so `agent.MAX_ROWS`
keeps working while the gateway depends only on this leaf module.
"""
import os

MAX_ROWS = int(os.getenv("STUDIO_MAX_ROWS", "50000"))  # server ceiling (configurable)
PREVIEW_ROWS = 40      # rows shown to the model per query
