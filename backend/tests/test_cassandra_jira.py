"""Apache Cassandra and Atlassian Jira as sources — through the real gateway.

Cassandra: a keyspace's tables and views from system_schema (key columns
first, primary keys as constraints), CQL SELECTs only, the keyspace pinned, a
quoted look-alike refused, rows paged and serialized (UUIDs, sets, maps,
timestamps). Jira: issues and projects as tables, fetched across Jira's pages
(Cloud's nextPageToken, Data Center's startAt), capped, cached, signed in only
by a connection test, and queried with ordinary SQL.

The driver and the REST API are stand-ins shaped exactly like the real ones;
there is no cluster or Jira site in the test environment.
"""
import collections
import datetime
import importlib
import json
import types
import uuid

import pytest

from app.queryguard import QueryRejected

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}

Row = collections.namedtuple("Row", "table_name column_name type kind position")
Name = collections.namedtuple("Name", "table_name")
View = collections.namedtuple("View", "view_name")
Comment = collections.namedtuple("Comment", "table_name comment")

COLUMNS = [
    Row("orders", "customer_id", "uuid", "partition_key", 0),
    Row("orders", "placed_at", "timestamp", "clustering", 0),
    Row("orders", "amount", "decimal", "regular", -1),
    Row("orders", "tags", "set<text>", "regular", -1),
    Row("orders", "attrs", "map<text, text>", "regular", -1),
    Row("orders_by_day", "day", "date", "partition_key", 0),
    Row("orders_by_day", "customer_id", "uuid", "clustering", 0),
]


class FakeResult(list):
    def __init__(self, rows, column_names=None):
        super().__init__(rows)
        self.column_names = column_names


class FakeSession:
    def __init__(self):
        self.statements, self.keyspace, self.many = [], None, 0

    def set_keyspace(self, ks):
        self.keyspace = ks

    def execute(self, query, params=()):
        text = getattr(query, "query_string", query)
        self.statements.append(text)
        if "system_schema.tables" in text and "comment" in text:
            return FakeResult([Comment("orders", "One row per order"),
                               Comment("orders_by_day", "")])
        if "system_schema.tables" in text:
            return FakeResult([Name("orders")])
        if "system_schema.views" in text:
            return FakeResult([View("orders_by_day")])
        if "system_schema.columns" in text:
            rows = COLUMNS if len(params) == 1 else [r for r in COLUMNS if r.table_name == params[1]]
            return FakeResult(rows)
        if self.many:                                             # a big partition, paged
            return FakeResult([(uuid.UUID(int=i), None, set(), {}) for i in range(self.many)],
                              ["customer_id", "placed_at", "tags", "attrs"])
        return FakeResult([(uuid.UUID(int=7), datetime.datetime(2026, 10, 1, 12, 0),
                            {"b", "a"}, {"k": "v"})],
                          ["customer_id", "placed_at", "tags", "attrs"])


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_SECRET", "cass-jira-secret")
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, gateway, governance, jobs, metagraph
    from app.connectors import cassandra_conn, demo, jira_conn
    _db.init_db()
    jobs.init_tables()
    metagraph.init_tables()
    connections.init_tables()
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    session = FakeSession()
    cass = cassandra_conn.CassandraConnector("cass", {"contact_points": "c1,c2",
                                                      "keyspace": "shop"})
    monkeypatch.setattr(cass, "_session_", lambda: session)
    monkeypatch.setitem(connectors._REGISTRY, "cass", cass)
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n', "test")
    yield types.SimpleNamespace(db=_db, gw=gateway, mg=metagraph, cass=cass, session=session,
                                connections=connections, jira_conn=jira_conn, mp=monkeypatch,
                                connectors=connectors)
    governance._STATE.update(doc=None, yaml="", source=None)


# ── Cassandra ────────────────────────────────────────────────────────────

def test_a_keyspaces_tables_come_from_system_schema(env):
    c = env.cass
    assert c.configured()
    assert c.list_tables() == ["orders", "orders_by_day"]
    assert [x["name"] for x in c.get_schema("orders")] == [
        "customer_id", "placed_at", "amount", "attrs", "tags"]     # keys first, then by name
    rel = c.describe_relations()
    assert rel["orders"]["kind"] == "table" and rel["orders_by_day"]["kind"] == "view"
    assert rel["orders"]["description"] == "One row per order"
    assert c.describe_constraints()["primary_keys"]["orders"] == ["customer_id", "placed_at"]
    assert c.qualifiers() == frozenset({"shop"})
    assert c.dialect == "cql" and c.dialect.identifier_rule == "lower"


def test_cql_runs_through_the_guard_with_rows_serialized(env):
    out = env.gw.execute(ADMIN, "cass", "SELECT customer_id, placed_at, tags, attrs "
                                        "FROM orders WHERE customer_id = 7", "test")
    assert env.session.statements[-1].endswith("LIMIT 50000")      # the gateway's row cap
    assert env.session.keyspace == "shop"
    assert out.rows == [["00000000-0000-0000-0000-000000000007", "2026-10-01T12:00:00",
                         ["a", "b"], {"k": "v"}]]


def test_rows_are_capped_as_they_are_paged(env):
    from app.connectors.base import unguarded
    env.mp.setenv("STUDIO_MAX_ROWS", "3")
    env.session.many = 10
    with unguarded():
        _cols, rows = env.cass.run_query("SELECT * FROM orders")
    assert len(rows) == 3


@pytest.mark.parametrize("bad", [
    "INSERT INTO orders (customer_id) VALUES (1)",
    "DELETE FROM orders WHERE customer_id = 1",
    "SELECT * FROM other_keyspace.orders",
    'SELECT * FROM "Orders"',                                       # not the same table in CQL
])
def test_writes_other_keyspaces_and_lookalikes_are_refused(env, bad):
    with pytest.raises(QueryRejected):
        env.gw.execute(ADMIN, "cass", bad, "test")


def test_the_metadata_graph_reads_a_keyspace(env):
    env.mg.discover("cass", "test")
    detail = env.mg.get_source("cass", user=ADMIN)
    assert detail["kinds"] == {"table": 1, "view": 1}
    orders = next(r for r in detail["relations"] if r["name"] == "orders")
    assert orders["description"] == "One row per order"


def test_the_model_is_told_it_writes_cql(env):
    from app import skills
    text = skills._build(env.cass, "admin", ["orders"], {"orders": []}, "fp")
    assert "Write CQL" in text and "no JOINs" in text


def test_the_cassandra_form_checks_before_signing_in(env):
    cfg, problems = env.connections._check_cassandra({"contact_points": " a , b ,",
                                                       "port": "nine"})
    assert cfg["contact_points"] == "a,b" and "port" in problems
    from app.connectors import cassandra_conn
    env.mp.setattr(cassandra_conn, "driver_available", lambda: False)
    _cfg, problems = env.connections._check_cassandra({"contact_points": "a", "port": "9042"})
    assert "cassandra-driver" in problems["contact_points"]


# ── Jira ─────────────────────────────────────────────────────────────────

def issue(key, status="In Progress", category="In Progress", **f):
    return {"key": key, "id": key.split("-")[1], "fields": {
        "project": {"key": key.split("-")[0]}, "summary": f"Fix {key}",
        "status": {"name": status, "statusCategory": {"name": category}},
        "issuetype": {"name": "Bug"}, "priority": {"name": "High"},
        "assignee": {"displayName": "Ana"}, "reporter": {"displayName": "Ben"},
        "resolution": None, "created": "2026-09-01T10:00:00.000+0000",
        "updated": "2026-10-01T10:00:00.000+0000", "resolutiondate": None,
        "duedate": None, "labels": ["api", "p1"], "parent": {"key": "OPS-1"}, **f}}


class FakeJira:
    """Jira's REST API: Cloud pages with nextPageToken, Data Center with startAt."""

    def __init__(self, monkeypatch, conn_mod, *, status=200):
        self.calls, self.status = [], status

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return json.dumps(self.body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            import urllib.error
            import urllib.parse
            url = urllib.parse.urlsplit(req.full_url)
            q = dict(urllib.parse.parse_qsl(url.query))
            self.calls.append((url.path, q, req.headers.get("Authorization")))
            if self.status != 200:
                raise urllib.error.HTTPError(req.full_url, self.status, "Unauthorized", {}, None)
            if url.path.endswith("/myself"):
                return Resp({"accountId": "x"})
            if url.path == "/rest/api/3/search/jql":
                if q.get("nextPageToken") == "t2":
                    return Resp({"issues": [issue("OPS-3", "Done", "Done")], "isLast": True})
                return Resp({"issues": [issue("OPS-1"), issue("OPS-2")], "nextPageToken": "t2",
                             "isLast": False})
            if url.path == "/rest/api/2/search":
                start = int(q.get("startAt", 0))
                page = [issue("DC-1"), issue("DC-2"), issue("DC-3")][start:start + 2]
                return Resp({"issues": page, "total": 3})
            if url.path == "/rest/api/3/project/search":
                return Resp({"values": [{"key": "OPS", "id": "10", "name": "Operations",
                                         "projectTypeKey": "software",
                                         "lead": {"displayName": "Ana"}}], "isLast": True})
            raise AssertionError(url.path)
        monkeypatch.setattr(conn_mod.urllib.request, "urlopen", urlopen)


def _jira(env, **cfg):
    conn = env.jira_conn.JiraConnector("jira-ops", {"site": "https://acme.atlassian.net",
                                                    "email": "ana@acme.com",
                                                    "api_token": "tok-secret", **cfg})
    env.mp.setitem(env.connectors._REGISTRY, "jira-ops", conn)
    return conn


def test_issues_are_read_across_pages_and_queried_with_sql(env):
    fake = FakeJira(env.mp, env.jira_conn)
    _jira(env)
    out = env.gw.execute(ADMIN, "jira-ops", "SELECT status_category, COUNT(*) AS n FROM issues "
                                            "GROUP BY status_category ORDER BY 1", "test")
    assert out.rows == [["Done", 1], ["In Progress", 2]]
    pages = [c for c in fake.calls if c[0] == "/rest/api/3/search/jql"]
    assert len(pages) == 2 and pages[1][1]["nextPageToken"] == "t2"
    assert pages[0][2].startswith("Basic ")                        # Cloud: email + API token
    row = env.gw.execute(ADMIN, "jira-ops", "SELECT key, assignee, labels, parent_key "
                                            "FROM issues WHERE key = 'OPS-1'", "test").rows
    assert row == [["OPS-1", "Ana", "api,p1", "OPS-1"]]
    joined = env.gw.execute(ADMIN, "jira-ops", "SELECT p.name, COUNT(*) FROM issues i JOIN "
                                               "projects p ON p.key = i.project_key "
                                               "GROUP BY p.name", "test").rows
    assert joined == [["Operations", 3]]


def test_the_issue_limit_and_the_cache_bound_the_calls(env):
    fake = FakeJira(env.mp, env.jira_conn)
    conn = _jira(env, max_issues="2")
    cols, rows = conn.fetch_report("issues")
    assert len(rows) == 2
    conn.fetch_report("issues")                                    # within the cache window
    assert len([c for c in fake.calls if "search" in c[0]]) == 1
    # A page larger than asked for (Jira may round up) is still cut to the limit.
    one = _jira(env, max_issues="1")
    assert len(one.fetch_report("issues")[1]) == 1


def test_data_center_uses_a_personal_token_and_start_at(env):
    fake = FakeJira(env.mp, env.jira_conn)
    conn = env.jira_conn.JiraConnector("jira-dc", {"site": "https://jira.acme.internal",
                                                   "api_token": "pat-secret"})
    _cols, rows = conn.fetch_report("issues")
    assert [r[0] for r in rows] == ["DC-1", "DC-2", "DC-3"]
    assert {c[2] for c in fake.calls} == {"Bearer pat-secret"}
    assert [c[1]["startAt"] for c in fake.calls] == ["0", "2"]


def test_only_a_connection_test_signs_in(env):
    fake = FakeJira(env.mp, env.jira_conn)
    assert _jira(env).list_tables() == ["issues", "projects"]
    assert fake.calls == []                                        # the catalog costs nothing
    out = env.connections._probe("jira", {"site": "acme.atlassian.net/jira/software/projects",
                                          "email": "ana@acme.com", "api_token": "tok-secret"})
    assert out["ok"] and out["tables"] == 2
    assert fake.calls[0][0] == "/rest/api/3/myself"


def test_a_rejected_token_fails_the_test_without_echoing_it(env):
    FakeJira(env.mp, env.jira_conn, status=401)
    out = env.connections._probe("jira", {"site": "https://acme.atlassian.net",
                                          "email": "ana@acme.com", "api_token": "tok-secret"})
    assert not out["ok"] and "tok-secret" not in json.dumps(out)


def test_the_jira_form_wants_an_https_site(env):
    cfg, problems = env.connections._check_jira({"site": "acme.atlassian.net/browse/OPS-1"})
    assert cfg["site"] == "https://acme.atlassian.net" and not problems
    _cfg, problems = env.connections._check_jira({"site": "http://jira.example.com"})
    assert "https" in problems["site"]


def test_both_appear_in_the_connect_form(env):
    kinds = {t["ctype"]: t for t in env.connections.types(user=ADMIN)}
    assert kinds["cassandra"]["available"] is True
    assert [f["key"] for f in kinds["jira"]["fields"]][:3] == ["site", "email", "api_token"]
    assert "mysql+pymysql" in kinds["sql"]["drivers"]
