"""Atlassian Jira (Cloud, and Jira Data Center / Server) as a source.

Jira's issues and projects are SQL tables: when the agent runs SQL, the
issues and projects it names are fetched from Jira's REST API into an
in-memory SQLite database and the SQL runs there (ApiReportConnector), so the
guard, RBAC, governance and charts work unchanged over Jira data.

- CLOUD (https://<site>.atlassian.net): an account email and an API token
  (id.atlassian.com → Security → API tokens), Basic auth, and the paginated
  /rest/api/3/search/jql endpoint.
- DATA CENTER / SERVER (any other https URL): a personal access token
  (Bearer) and /rest/api/2/search.
- SCOPE is a JQL filter (default: every issue the account can see, newest
  first), at most JIRA_MAX_ISSUES (5000) issues per refresh; fetched data is
  kept JIRA_CACHE_SECONDS (300) per source so a conversation is not a fetch
  per question. Only what the account can see in Jira is ever read.
"""
import base64
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from .marketing import ApiReportConnector

ISSUE_FIELDS = ("summary,status,issuetype,priority,assignee,reporter,created,updated,"
                "resolutiondate,duedate,labels,parent,project,resolution")


def _when(value):
    return value or None


def _name(obj, key="name"):
    return (obj or {}).get(key) if isinstance(obj, dict) else None


def _issue_row(issue):
    f = issue.get("fields") or {}
    status = f.get("status") or {}
    return [issue.get("key"), issue.get("id"), _name(f.get("project"), "key"),
            f.get("summary"), status.get("name"),
            _name(status.get("statusCategory")), _name(f.get("issuetype")),
            _name(f.get("priority")), _name(f.get("assignee"), "displayName"),
            _name(f.get("reporter"), "displayName"), _name(f.get("resolution")),
            _when(f.get("created")), _when(f.get("updated")), _when(f.get("resolutiondate")),
            _when(f.get("duedate")), ",".join(f.get("labels") or []),
            _name(f.get("parent"), "key")]


class JiraConnector(ApiReportConnector):
    name = "jira"
    REPORTS = {
        "issues": [("key", "TEXT"), ("id", "TEXT"), ("project_key", "TEXT"),
                   ("summary", "TEXT"), ("status", "TEXT"), ("status_category", "TEXT"),
                   ("issue_type", "TEXT"), ("priority", "TEXT"), ("assignee", "TEXT"),
                   ("reporter", "TEXT"), ("resolution", "TEXT"), ("created", "TEXT"),
                   ("updated", "TEXT"), ("resolved", "TEXT"), ("due_date", "TEXT"),
                   ("labels", "TEXT"), ("parent_key", "TEXT")],
        "projects": [("key", "TEXT"), ("id", "TEXT"), ("name", "TEXT"),
                     ("project_type", "TEXT"), ("lead", "TEXT")],
    }

    def __init__(self, name="jira", cfg=None):
        self.name = name
        self._dyn = dict(cfg or {})
        self._cache = {}
        self._lock = threading.Lock()

    # ── Configuration ────────────────────────────────────────────────────

    def _cfg(self):
        base = {"site": os.getenv("JIRA_SITE", ""), "email": os.getenv("JIRA_EMAIL", ""),
                "api_token": os.getenv("JIRA_API_TOKEN", ""), "jql": os.getenv("JIRA_JQL", ""),
                "max_issues": os.getenv("JIRA_MAX_ISSUES", "5000")}
        base.update({k: v for k, v in self._dyn.items() if v not in (None, "")})
        base["site"] = str(base["site"]).strip().rstrip("/")
        return base

    def configured(self):
        cfg = self._cfg()
        return bool(cfg["site"].startswith("https://") and cfg["api_token"])

    def _cloud(self):
        host = urllib.parse.urlsplit(self._cfg()["site"]).hostname or ""
        return host.endswith(".atlassian.net")

    def _headers(self):
        cfg = self._cfg()
        if cfg["email"]:
            auth = base64.b64encode(f"{cfg['email']}:{cfg['api_token']}".encode()).decode()
            value = f"Basic {auth}"
        else:
            value = f"Bearer {cfg['api_token']}"          # a Data Center personal token
        return {"Authorization": value, "Accept": "application/json"}

    def _json(self, path, params):
        url = f"{self._cfg()['site']}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def _max_issues(self):
        try:
            return max(1, min(50000, int(self._cfg()["max_issues"])))
        except ValueError:
            return 5000

    # ── Fetching ─────────────────────────────────────────────────────────

    def _issues(self):
        jql = self._cfg()["jql"].strip() or "order by updated DESC"
        cap, rows = self._max_issues(), []
        if self._cloud():
            token = None
            while len(rows) < cap:
                params = {"jql": jql, "fields": ISSUE_FIELDS,
                          "maxResults": min(100, cap - len(rows))}
                if token:
                    params["nextPageToken"] = token
                page = self._json("/rest/api/3/search/jql", params)
                rows += [_issue_row(i) for i in page.get("issues") or []]
                token = page.get("nextPageToken")
                if page.get("isLast", not token) or not token or not page.get("issues"):
                    break
        else:
            start = 0
            while len(rows) < cap:
                page = self._json("/rest/api/2/search", {
                    "jql": jql, "fields": ISSUE_FIELDS, "startAt": start,
                    "maxResults": min(100, cap - len(rows))})
                issues = page.get("issues") or []
                rows += [_issue_row(i) for i in issues]
                start += len(issues)
                if not issues or start >= int(page.get("total") or 0):
                    break
        return rows[:cap]

    def _projects(self):
        rows = []
        if self._cloud():
            start = 0
            while True:
                page = self._json("/rest/api/3/project/search",
                                  {"startAt": start, "maxResults": 50, "expand": "lead"})
                values = page.get("values") or []
                rows += [[p.get("key"), p.get("id"), p.get("name"), p.get("projectTypeKey"),
                          _name(p.get("lead"), "displayName")] for p in values]
                start += len(values)
                if page.get("isLast", True) or not values:
                    break
        else:
            for p in self._json("/rest/api/2/project", {"expand": "lead"}) or []:
                rows.append([p.get("key"), p.get("id"), p.get("name"), p.get("projectTypeKey"),
                             _name(p.get("lead"), "displayName")])
        return rows

    def fetch_report(self, table):
        if table not in self.REPORTS:
            raise ValueError(f"Unknown report {table}")
        ttl = int(os.getenv("JIRA_CACHE_SECONDS", "300"))
        with self._lock:
            hit = self._cache.get(table)
            if hit and time.time() - hit[0] < ttl:
                return [c for c, _t in self.REPORTS[table]], hit[1]
        rows = self._issues() if table == "issues" else self._projects()
        with self._lock:
            self._cache[table] = (time.time(), rows)
        return [c for c, _t in self.REPORTS[table]], rows

    def list_tables(self):
        # A connection test signs in for real, so a wrong site or token fails
        # on the form rather than at the first question. Everywhere else the
        # tables are just the two reports — no call to Jira.
        if self.name in ("__probe__", "__browse__"):
            self._json("/rest/api/3/myself" if self._cloud() else "/rest/api/2/myself", {})
        return super().list_tables()

    def query_guidance(self):
        return ["Write SQLite SQL over the tables issues and projects (join on "
                "issues.project_key = projects.key).",
                "Dates are ISO-8601 text: use date(created), strftime('%Y-%m', created).",
                "status_category is To Do, In Progress or Done; resolved is empty for "
                "unresolved issues."]
