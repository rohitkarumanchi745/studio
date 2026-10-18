"""ServiceNow as a source: ITSM records as SQL tables.

Incidents, changes, problems and requested items are tables. When the agent
runs SQL, the records it names are read from ServiceNow's Table API
(/api/now/table/<table>) into an in-memory SQLite database and the SQL runs
there (ApiReportConnector), so the guard, RBAC, governance and charts work
unchanged over ServiceNow data.

- AUTH: a ServiceNow user and password (Basic), or an OAuth access token
  (Bearer). Read only what that account may read — give it the itil (or a
  narrower) read role.
- VALUES are display values (a state reads "In Progress", an assignment group
  its name), which is what questions use.
- SCOPE: an optional encoded query per refresh, at most SERVICENOW_MAX_ROWS
  (5000) records per table, kept SERVICENOW_CACHE_SECONDS (300).
"""
import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from .marketing import ApiReportConnector

_COMMON = [("number", "TEXT"), ("short_description", "TEXT"), ("state", "TEXT"),
           ("priority", "TEXT"), ("assignment_group", "TEXT"), ("assigned_to", "TEXT"),
           ("opened_at", "TEXT"), ("closed_at", "TEXT"), ("sys_created_on", "TEXT"),
           ("sys_updated_on", "TEXT")]


class ServiceNowConnector(ApiReportConnector):
    name = "servicenow"
    REPORTS = {
        "incident": _COMMON + [("urgency", "TEXT"), ("impact", "TEXT"), ("category", "TEXT"),
                               ("caller_id", "TEXT"), ("resolved_at", "TEXT"),
                               ("close_code", "TEXT")],
        "change_request": _COMMON + [("type", "TEXT"), ("risk", "TEXT"),
                                     ("start_date", "TEXT"), ("end_date", "TEXT")],
        "problem": _COMMON + [("category", "TEXT"), ("resolved_at", "TEXT")],
        "sc_req_item": _COMMON + [("cat_item", "TEXT"), ("request", "TEXT"),
                                  ("stage", "TEXT")],
    }

    def __init__(self, name="servicenow", cfg=None):
        self.name = name
        self._dyn = dict(cfg or {})
        self._cache = {}
        self._lock = threading.Lock()

    def _cfg(self):
        base = {"instance": os.getenv("SERVICENOW_INSTANCE", ""),
                "username": os.getenv("SERVICENOW_USERNAME", ""),
                "password": os.getenv("SERVICENOW_PASSWORD", ""),
                "token": os.getenv("SERVICENOW_TOKEN", ""),
                "query": os.getenv("SERVICENOW_QUERY", ""),
                "max_rows": os.getenv("SERVICENOW_MAX_ROWS", "5000")}
        base.update({k: v for k, v in self._dyn.items() if v not in (None, "")})
        base["instance"] = str(base["instance"]).strip().rstrip("/")
        return base

    def configured(self):
        cfg = self._cfg()
        return cfg["instance"].startswith("https://") and bool(
            cfg["token"] or (cfg["username"] and cfg["password"]))

    def _headers(self):
        cfg = self._cfg()
        if cfg["token"]:
            value = f"Bearer {cfg['token']}"
        else:
            raw = f"{cfg['username']}:{cfg['password']}".encode()
            value = f"Basic {base64.b64encode(raw).decode()}"
        return {"Authorization": value, "Accept": "application/json"}

    def _get(self, path, params):
        url = f"{self._cfg()['instance']}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=60) as r:
            total = (getattr(r, "headers", None) or {}).get("X-Total-Count")
            return json.loads(r.read().decode()), total

    def _json(self, path, params):
        return self._get(path, params)[0]

    def _cap(self):
        try:
            return max(1, min(50000, int(self._cfg()["max_rows"])))
        except ValueError:
            return 5000

    @staticmethod
    def _value(v):
        """A display value as text (a reference may come back as an object)."""
        if isinstance(v, dict):
            return v.get("display_value") or v.get("value")
        return v if v != "" else None

    def fetch_report(self, table):
        if table not in self.REPORTS:
            raise ValueError(f"Unknown report {table}")
        columns = [c for c, _t in self.REPORTS[table]]
        ttl = int(os.getenv("SERVICENOW_CACHE_SECONDS", "300"))
        with self._lock:
            hit = self._cache.get(table)
            if hit and time.time() - hit[0] < ttl:
                return columns, hit[1]
        cap, rows, offset = self._cap(), [], 0
        query = self._cfg()["query"].strip() or "ORDERBYDESCsys_updated_on"
        while len(rows) < cap:
            body, total = self._get(f"/api/now/table/{table}", {
                "sysparm_query": query, "sysparm_fields": ",".join(columns),
                "sysparm_display_value": "true", "sysparm_exclude_reference_link": "true",
                "sysparm_limit": min(1000, cap - len(rows)), "sysparm_offset": offset})
            page = body.get("result") or []
            rows += [[self._value(rec.get(c)) for c in columns] for rec in page]
            offset += len(page)
            # An instance may return fewer records than asked for (its own page
            # cap), so a short page is not the end: X-Total-Count says where
            # the end is, and without it only an empty page does.
            if not page or (str(total).isdigit() and offset >= int(total)):
                break
        rows = rows[:cap]
        with self._lock:
            self._cache[table] = (time.time(), rows)
        return columns, rows

    def list_tables(self):
        # A connection test signs in for real; listing the tables otherwise
        # never calls the instance.
        if self.name in ("__probe__", "__browse__"):
            self._json("/api/now/table/incident", {"sysparm_limit": 1, "sysparm_fields": "number"})
        return super().list_tables()

    def query_guidance(self):
        return ["Write SQLite SQL over incident, change_request, problem and sc_req_item.",
                "Values are ServiceNow display values: state reads 'New', 'In Progress', "
                "'Resolved', 'Closed'; priority reads '1 - Critical' … '5 - Planning'.",
                "Dates are text 'YYYY-MM-DD HH:MM:SS': use date(opened_at), "
                "strftime('%Y-%m', opened_at)."]
