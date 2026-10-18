"""ServiceNow and n8n as sources — through the real gateway.

ServiceNow: incidents, changes, problems and requested items read from the
Table API with display values, paged by offset, capped, cached; Basic or
Bearer auth; only a connection test signs in. n8n: workflows and executions
read from the public API (X-N8N-API-KEY), paged by cursor, never execution
data, durations computed; the same probe-only sign-in. Both answer SQL.

The REST APIs are stand-ins shaped like the real ones.
"""
import importlib
import json
import types
import urllib.error
import urllib.parse

import pytest

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}


class Resp:
    def __init__(self, body, headers=None):
        self.body, self.headers = body, dict(headers or {})

    def read(self):
        return json.dumps(self.body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_SECRET", "snow-n8n-secret")
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, gateway, governance, jobs, metagraph
    from app.connectors import demo, n8n_conn, servicenow_conn
    _db.init_db()
    jobs.init_tables()
    metagraph.init_tables()
    connections.init_tables()
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n', "test")
    calls = []

    def urlopen(req, timeout=None):
        url = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(url.query))
        calls.append((url.path, q, dict(req.header_items())))
        if env_ns.status != 200:
            raise urllib.error.HTTPError(req.full_url, env_ns.status, "Unauthorized", {}, None)
        out = env_ns.route(url.path, q)
        return Resp(*out) if isinstance(out, tuple) else Resp(out)
    monkeypatch.setattr(servicenow_conn.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(n8n_conn.urllib.request, "urlopen", urlopen)
    env_ns = types.SimpleNamespace(db=_db, gw=gateway, connections=connections,
                                   connectors=connectors, snow=servicenow_conn, n8n=n8n_conn,
                                   calls=calls, mp=monkeypatch, status=200, route=None)
    yield env_ns
    governance._STATE.update(doc=None, yaml="", source=None)


# ── ServiceNow ───────────────────────────────────────────────────────────

def incident(n, state="In Progress", priority="2 - High", group="Service Desk"):
    return {"number": f"INC{n:07d}", "short_description": f"Issue {n}", "state": state,
            "priority": priority, "assignment_group": group, "assigned_to": "Ana",
            "opened_at": f"2026-10-0{1 + n % 9} 09:00:00", "closed_at": "",
            "sys_created_on": "2026-10-01 09:00:00", "sys_updated_on": "2026-10-02 09:00:00",
            "urgency": "2 - Medium", "impact": "2 - Medium", "category": "network",
            "caller_id": {"display_value": "Ben", "value": "abc"}, "resolved_at": "",
            "close_code": ""}


def snow_route(path, q, total=True):
    # Seven incidents; the instance caps a page at 3 whatever is asked for.
    if path.startswith("/api/now/table/incident"):
        offset, limit = int(q.get("sysparm_offset", 0)), int(q.get("sysparm_limit", 1000))
        everything = [incident(i, "Closed" if i % 3 == 0 else "In Progress") for i in range(1, 8)]
        body = {"result": everything[offset:offset + min(limit, 3)]}
        return (body, {"X-Total-Count": str(len(everything))}) if total else body
    return {"result": []}


def _snow(env, **cfg):
    conn = env.snow.ServiceNowConnector("snow", {"instance": "https://acme.service-now.com",
                                                 "username": "svc-studio", "password": "pw-secret",
                                                 **cfg})
    env.mp.setitem(env.connectors._REGISTRY, "snow", conn)
    return conn


def test_incidents_are_read_page_by_page_and_queried_with_sql(env):
    env.route = snow_route
    _snow(env)
    out = env.gw.execute(ADMIN, "snow", "SELECT state, COUNT(*) AS n FROM incident "
                                        "GROUP BY state ORDER BY state", "test")
    assert out.rows == [["Closed", 2], ["In Progress", 5]]
    pages = [c for c in env.calls if c[0] == "/api/now/table/incident"]
    assert [p[1]["sysparm_offset"] for p in pages] == ["0", "3", "6"]
    assert pages[0][1]["sysparm_display_value"] == "true"
    assert pages[0][2]["Authorization"].startswith("Basic ")
    caller = env.gw.execute(ADMIN, "snow", "SELECT caller_id FROM incident LIMIT 1",
                            "test").rows
    assert caller == [["Ben"]]                                     # a reference's display value


def test_without_a_total_count_only_an_empty_page_ends_the_read(env):
    env.route = lambda path, q: snow_route(path, q, total=False)
    conn = _snow(env)
    assert len(conn.fetch_report("incident")[1]) == 7
    assert [c[1]["sysparm_offset"] for c in env.calls] == ["0", "3", "6", "7"]


def test_the_record_limit_and_the_cache_bound_the_calls(env):
    env.route = snow_route
    conn = _snow(env, max_rows="4")
    assert len(conn.fetch_report("incident")[1]) == 4
    conn.fetch_report("incident")
    assert len(env.calls) == 2                                     # 3 + 1, then the cache


def test_an_oauth_token_is_sent_as_bearer(env):
    env.route = snow_route
    conn = env.snow.ServiceNowConnector("snow2", {"instance": "https://acme.service-now.com",
                                                  "token": "oauth-secret"})
    conn.fetch_report("incident")
    assert env.calls[0][2]["Authorization"] == "Bearer oauth-secret"


def test_servicenow_signs_in_only_for_a_connection_test(env):
    env.route = snow_route
    assert _snow(env).list_tables() == ["change_request", "incident", "problem", "sc_req_item"]
    assert env.calls == []
    out = env.connections._probe("servicenow", {"instance": "acme.service-now.com/now/nav/ui",
                                                "username": "u", "password": "p"})
    assert out["ok"] and out["tables"] == 4
    env.status = 401
    out = env.connections._probe("servicenow", {"instance": "https://acme.service-now.com",
                                                "username": "u", "password": "pw-secret"})
    assert not out["ok"] and "pw-secret" not in json.dumps(out)


def test_the_servicenow_form_wants_https_and_a_credential(env):
    cfg, problems = env.connections._check_servicenow({"instance": "acme.service-now.com",
                                                       "token": "t"})
    assert cfg["instance"] == "https://acme.service-now.com" and not problems
    _cfg, problems = env.connections._check_servicenow({"instance": "https://acme.service-now.com"})
    assert "username" in problems
    _cfg, problems = env.connections._check_servicenow({"instance": "http://x.example.com",
                                                        "token": "t"})
    assert "https" in problems["instance"]


# ── n8n ──────────────────────────────────────────────────────────────────

def n8n_route(path, q):
    if path == "/api/v1/workflows":
        if q.get("cursor") == "w2":
            return {"data": [{"id": "3", "name": "Nightly export", "active": False,
                              "createdAt": "2026-09-01T00:00:00.000Z",
                              "updatedAt": "2026-09-02T00:00:00.000Z", "tags": [],
                              "nodes": [{}, {}]}], "nextCursor": None}
        return {"data": [{"id": "1", "name": "Sync CRM", "active": True,
                          "createdAt": "2026-08-01T00:00:00.000Z",
                          "updatedAt": "2026-10-01T00:00:00.000Z",
                          "tags": [{"name": "crm"}, {"name": "prod"}], "nodes": [{}, {}, {}]},
                         {"id": "2", "name": "Alert on errors", "active": True,
                          "createdAt": "2026-08-02T00:00:00.000Z",
                          "updatedAt": "2026-10-01T00:00:00.000Z", "tags": [], "nodes": [{}]}],
                "nextCursor": "w2"}
    if path == "/api/v1/executions":
        assert q.get("includeData") == "false"                     # never the items a run saw
        return {"data": [
            {"id": "100", "workflowId": "1", "status": "success", "mode": "trigger",
             "finished": True, "startedAt": "2026-10-17T10:00:00.000Z",
             "stoppedAt": "2026-10-17T10:00:12.500Z"},
            {"id": "101", "workflowId": "1", "status": "error", "mode": "trigger",
             "finished": True, "startedAt": "2026-10-17T11:00:00.000Z",
             "stoppedAt": "2026-10-17T11:00:03.000Z"},
            {"id": "102", "workflowId": "2", "status": "error", "mode": "webhook",
             "finished": True, "startedAt": "2026-10-18T09:00:00.000Z",
             "stoppedAt": "2026-10-18T09:00:01.000Z"}], "nextCursor": None}
    raise AssertionError(path)


def _n8n(env, **cfg):
    conn = env.n8n.N8nConnector("automation", {"url": "https://n8n.acme.com",
                                               "api_key": "n8n-key-secret", **cfg})
    env.mp.setitem(env.connectors._REGISTRY, "automation", conn)
    return conn


def test_workflows_and_executions_answer_sql(env):
    env.route = n8n_route
    _n8n(env)
    out = env.gw.execute(ADMIN, "automation",
                         "SELECT w.name, COUNT(*) AS failures FROM executions e JOIN workflows w "
                         "ON w.id = e.workflow_id WHERE e.status = 'error' GROUP BY w.name "
                         "ORDER BY w.name", "test")
    assert out.rows == [["Alert on errors", 1], ["Sync CRM", 1]]
    wf = env.gw.execute(ADMIN, "automation", "SELECT name, active, tags, node_count FROM "
                                             "workflows ORDER BY id", "test").rows
    assert wf == [["Sync CRM", 1, "crm,prod", 3], ["Alert on errors", 1, "", 1],
                  ["Nightly export", 0, "", 2]]                    # across the cursor
    dur = env.gw.execute(ADMIN, "automation", "SELECT duration_seconds FROM executions "
                                              "WHERE id = '100'", "test").rows
    assert dur == [[12.5]]
    assert {c[2]["X-n8n-api-key"] for c in env.calls} == {"n8n-key-secret"}


def test_n8n_signs_in_only_for_a_connection_test(env):
    env.route = n8n_route
    assert _n8n(env).list_tables() == ["executions", "workflows"]
    assert env.calls == []
    out = env.connections._probe("n8n", {"url": "n8n.acme.com:5678/home/workflows",
                                         "api_key": "n8n-key-secret"})
    assert out["ok"]
    assert env.calls[0][0] == "/api/v1/workflows"
    env.status = 401
    out = env.connections._probe("n8n", {"url": "https://n8n.acme.com", "api_key": "bad-secret"})
    assert not out["ok"] and "bad-secret" not in json.dumps(out)


def test_the_n8n_form_keeps_a_self_hosted_port(env):
    cfg, problems = env.connections._check_n8n({"url": "n8n.acme.com:5678/home"})
    assert cfg["url"] == "https://n8n.acme.com:5678" and not problems


def test_all_three_new_types_are_offered(env):
    kinds = {t["ctype"] for t in env.connections.types(user=ADMIN)}
    assert {"servicenow", "n8n", "jira", "databricks", "powerbi"} <= kinds
