"""n8n as a source: workflows and their executions as SQL tables.

n8n's public REST API (/api/v1, an API key in the X-N8N-API-KEY header) is
read into an in-memory SQLite database per query (ApiReportConnector), so a
question like "which workflows failed most this week?" runs through the same
guard, RBAC and charts as any source. Self-hosted n8n and n8n Cloud alike:
the URL is the instance's own.

- workflows: one row per workflow — name, whether it is active, when it was
  created and updated, its tags and how many nodes it has.
- executions: one row per run — workflow, status (success, error, running,
  waiting, canceled…), mode (trigger, webhook, manual…), start, stop and
  duration. Execution DATA (the items a run processed) is never read.

At most N8N_MAX_ROWS (5000) executions per refresh, newest first, kept
N8N_CACHE_SECONDS (300). Read-only: nothing is triggered or changed.
"""
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

from .marketing import ApiReportConnector


def _seconds(start, stop):
    try:
        a = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(stop).replace("Z", "+00:00"))
        return round((b - a).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


class N8nConnector(ApiReportConnector):
    name = "n8n"
    REPORTS = {
        "workflows": [("id", "TEXT"), ("name", "TEXT"), ("active", "INTEGER"),
                      ("created_at", "TEXT"), ("updated_at", "TEXT"), ("tags", "TEXT"),
                      ("node_count", "INTEGER")],
        "executions": [("id", "TEXT"), ("workflow_id", "TEXT"), ("status", "TEXT"),
                       ("mode", "TEXT"), ("finished", "INTEGER"), ("started_at", "TEXT"),
                       ("stopped_at", "TEXT"), ("duration_seconds", "REAL")],
    }

    def __init__(self, name="n8n", cfg=None):
        self.name = name
        self._dyn = dict(cfg or {})
        self._cache = {}
        self._lock = threading.Lock()

    def _cfg(self):
        base = {"url": os.getenv("N8N_URL", ""), "api_key": os.getenv("N8N_API_KEY", ""),
                "max_rows": os.getenv("N8N_MAX_ROWS", "5000")}
        base.update({k: v for k, v in self._dyn.items() if v not in (None, "")})
        base["url"] = str(base["url"]).strip().rstrip("/")
        return base

    def configured(self):
        cfg = self._cfg()
        return cfg["url"].startswith("https://") and bool(cfg["api_key"])

    def _json(self, path, params):
        cfg = self._cfg()
        url = f"{cfg['url']}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"X-N8N-API-KEY": cfg["api_key"],
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def _cap(self):
        try:
            return max(1, min(50000, int(self._cfg()["max_rows"])))
        except ValueError:
            return 5000

    def _pages(self, path, params, cap):
        out, cursor = [], None
        while len(out) < cap:
            page = self._json(path, {**params, "limit": min(250, cap - len(out)),
                                     **({"cursor": cursor} if cursor else {})})
            data = page.get("data") or []
            out += data
            cursor = page.get("nextCursor")
            if not cursor or not data:
                break
        return out[:cap]

    def fetch_report(self, table):
        if table not in self.REPORTS:
            raise ValueError(f"Unknown report {table}")
        columns = [c for c, _t in self.REPORTS[table]]
        ttl = int(os.getenv("N8N_CACHE_SECONDS", "300"))
        with self._lock:
            hit = self._cache.get(table)
            if hit and time.time() - hit[0] < ttl:
                return columns, hit[1]
        if table == "workflows":
            rows = [[str(w.get("id")), w.get("name"), 1 if w.get("active") else 0,
                     w.get("createdAt"), w.get("updatedAt"),
                     ",".join(t.get("name", "") for t in w.get("tags") or []
                              if isinstance(t, dict)),
                     len(w.get("nodes") or [])]
                    for w in self._pages("/api/v1/workflows", {}, 50000)]
        else:
            rows = [[str(e.get("id")), str(e.get("workflowId")), e.get("status"), e.get("mode"),
                     1 if e.get("finished") else 0, e.get("startedAt"), e.get("stoppedAt"),
                     _seconds(e.get("startedAt"), e.get("stoppedAt"))]
                    for e in self._pages("/api/v1/executions", {"includeData": "false"},
                                         self._cap())]
        with self._lock:
            self._cache[table] = (time.time(), rows)
        return columns, rows

    def list_tables(self):
        # A connection test signs in for real; listing otherwise calls nothing.
        if self.name in ("__probe__", "__browse__"):
            self._json("/api/v1/workflows", {"limit": 1})
        return super().list_tables()

    def query_guidance(self):
        return ["Write SQLite SQL over workflows and executions (join on "
                "executions.workflow_id = workflows.id).",
                "status is success, error, crashed, canceled, running, waiting or new; "
                "mode is trigger, webhook, manual, retry, …",
                "Timestamps are ISO-8601 text: date(started_at), strftime('%Y-%m-%d', started_at)."]
