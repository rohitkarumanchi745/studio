"""A Databricks source that spans every schema — "show all tables" for an admin.

Schema `*` (of one catalog, or of every catalog when none is set) makes one
source of every table the token can see. There is no single namespace to pin,
so its tables are named in full (catalog.schema.table) and the guard matches
references WHOLE. Locked in here:
- the guard's full-name mode: a full name on the list passes (any case); a
  short name, a two-part name, another catalog, a dotted-quoted part and
  `system.*` are refused; CTEs still work; the mode is explicit — a dotted
  allowlist name never switches it on for another source;
- the listing: one information_schema query (system's for every catalog),
  `system` and information_schema left out, only plain names, capped, with a
  SHOW walk when information_schema cannot be read;
- the connector: no `*` sent as a session schema, DESCRIBE with each part
  quoted, the picker offering "all schemas" per catalog and everything;
- through the gateway as an admin: full names run, short names are refused
  with a hint, and the agent's skill file says to write names in full.
"""
import sys
import types

import pytest

from app import connections, db, gateway, queryguard, skills
from app.connectors import databricks_conn
from app.queryguard import FULL_NAMES, QueryRejected

ALLOWED = ["main.sales.orders", "main.sales.customers", "finance.ledger.entries"]


def _ok(sql, allowed=ALLOWED, quals=FULL_NAMES):
    return queryguard.validate(sql, allowed, qualifiers=quals, dialect="databricks")


def _refused(sql, allowed=ALLOWED, quals=FULL_NAMES):
    with pytest.raises(QueryRejected) as e:
        _ok(sql, allowed, quals)
    return str(e.value)


# ── The guard ───────────────────────────────────────────────────────────

def test_full_names_on_the_list_pass_in_any_case_or_quoting():
    _ok("SELECT * FROM main.sales.orders")
    _ok("SELECT * FROM MAIN.Sales.ORDERS")
    _ok("SELECT * FROM `main`.`sales`.`orders` o JOIN finance.ledger.entries e ON o.id = e.id")
    _ok("WITH t AS (SELECT * FROM main.sales.orders) SELECT * FROM t")


@pytest.mark.parametrize("sql,says", [
    ("SELECT * FROM orders", "Name the table in full"),
    ("SELECT * FROM sales.orders", "not permitted"),
    ("SELECT * FROM other.sales.orders", "not permitted"),
    ("SELECT * FROM system.access.audit", "not permitted"),
    ("SELECT * FROM main.sales.secrets", "not permitted"),
    ("SELECT * FROM `main`.`sales.orders`", "not a name this source lists"),
    ("SELECT * FROM main.sales.orders, payroll", "Name the table in full"),
])
def test_anything_short_or_off_the_list_is_refused(sql, says):
    assert says in _refused(sql)


def test_full_name_mode_is_explicit_never_inferred():
    # A source whose names merely contain dots (file-style) keeps the old rule:
    # `sales.csv` written bare is schema.table there, outside its namespace.
    for quals in (frozenset(), frozenset({"public"})):
        _ok('SELECT * FROM "sales.csv"', allowed=["sales.csv"], quals=quals)
        assert "outside the configured namespace" in _refused(
            "SELECT * FROM sales.csv", allowed=["sales.csv"], quals=quals)


# ── The listing ─────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, script):
        self.script, self.rows, self.sql = script, [], []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        for match, rows in self.script:
            if match in sql:
                if isinstance(rows, Exception):
                    raise rows
                self.rows = rows
                return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return self.rows


def _connector(script, catalog="", schema="*"):
    conn = connections._DynDatabricks("dbx_all", {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", "catalog": catalog, "schema": schema})
    cur = _Cur(script)
    conn._conn = lambda: types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)
    return conn, cur


def test_every_catalog_comes_from_one_information_schema_query():
    conn, cur = _connector([("system.information_schema.tables", [
        ("finance", "ledger", "entries"), ("main", "sales", "orders"),
        ("main", "sales", "weird-name"), ("main", "odd.schema", "t")])])
    assert conn.list_tables() == ["finance.ledger.entries", "main.sales.orders"]
    sql = cur.sql[0]
    assert "table_catalog NOT IN ('system')" in sql and "table_schema <> 'information_schema'" in sql
    assert conn.qualifiers() is FULL_NAMES and conn.full_names


def test_one_catalog_and_a_cap(monkeypatch):
    monkeypatch.setenv("STUDIO_DATABRICKS_MAX_TABLES", "2")
    conn, cur = _connector([("`main`.information_schema.tables", [
        ("main", "a", "t1"), ("main", "a", "t2"), ("main", "b", "t3")])], catalog="main")
    assert conn.list_tables() == ["main.a.t1", "main.a.t2"] and conn.tables_truncated
    assert "LIMIT 3" in cur.sql[0]


def test_without_information_schema_it_walks():
    conn, _ = _connector([
        ("information_schema.tables", RuntimeError("no information_schema")),
        ("SHOW CATALOGS", [("main",), ("system",)]),
        ("SHOW SCHEMAS IN `main`", [("sales",), ("information_schema",)]),
        ("SHOW TABLES IN `main`.`sales`", [("sales", "orders", False)]),
    ])
    assert conn.list_tables() == ["main.sales.orders"]


def test_describe_quotes_each_part_and_no_star_reaches_the_session(monkeypatch):
    conn, cur = _connector([("DESCRIBE TABLE", [("region", "string"), ("revenue", "double")])])
    assert conn.get_schema("main.sales.orders")[0] == {"name": "region", "type": "string"}
    assert cur.sql[-1] == "DESCRIBE TABLE `main`.`sales`.`orders`"
    with pytest.raises(ValueError):
        conn.get_schema("main.sales.orders; DROP")
    seen = {}
    sql = types.ModuleType("databricks.sql")
    sql.connect = lambda **kw: seen.update(kw) or types.SimpleNamespace(close=lambda: None)
    pkg = types.ModuleType("databricks")
    pkg.__path__, pkg.sql = [], sql
    monkeypatch.setitem(sys.modules, "databricks", pkg)
    monkeypatch.setitem(sys.modules, "databricks.sql", sql)
    connections._DynDatabricks("x", {"server_hostname": "h.databricks.com", "http_path": "/sql/x",
                                     "access_token": "dapi", "catalog": "", "schema": "*"})._conn()
    assert "schema" not in seen and "catalog" not in seen


def test_the_picker_offers_all_schemas_and_everything():
    conn, _ = _connector([
        ("SHOW CATALOGS", [("main",), ("finance",), ("system",)]),
        ("SHOW SCHEMAS IN `main`", [("sales",), ("ops",)]),
        ("`main`.information_schema.tables", [("sales", 3), ("ops", 1)]),
        ("SHOW SCHEMAS IN `finance`", [("ledger",)]),
        ("`finance`.information_schema.tables", [("ledger", 2)]),
        ("SHOW SCHEMAS IN `system`", [("access",)]),
        ("`system`.information_schema.tables", [("access", 9)]),
    ], schema="")
    listed = conn.list_namespaces()
    assert listed[0] == {"database": "", "schema": "*", "tables": 6}
    assert {"database": "main", "schema": "*", "tables": 4} in listed
    # finance has one schema: that schema already is the whole catalog.
    assert not any(i["database"] == "finance" and i["schema"] == "*" for i in listed)
    assert {"database": "system", "schema": "*"} not in listed and \
        not any(i["database"] == "system" and i["schema"] == "*" for i in listed)


def test_how_such_a_source_is_labelled():
    assert connections._namespace("databricks", {"catalog": "", "schema": "*"}) == "all catalogs"
    assert connections._namespace("databricks", {"catalog": "main", "schema": "*"}) == \
        "main · all schemas"
    assert connections._namespace("databricks", {"catalog": "main", "schema": "sales"}) == "main.sales"


# ── Through the gateway, as an admin ────────────────────────────────────

ADMIN = {"id": "u-all-admin", "email": "all-admin@studio.test", "role": "admin"}


@pytest.fixture()
def _store():
    db.init_db()            # the gateway writes an audit row per read
    yield


def test_an_admin_reads_any_listed_table_by_its_full_name(monkeypatch, _store):
    class _Source:
        name, dialect = "dbx_all", "databricks"
        full_names = True

        def list_tables(self):
            return ["main.sales.orders", "finance.ledger.entries"]

        def qualifiers(self):
            return FULL_NAMES

        def run_query(self, sql):
            return ["region"], [["EU"]]

    monkeypatch.setattr(gateway.sources, "connector_or_400", lambda source: _Source())
    monkeypatch.setattr(gateway.rbac, "can_access", lambda role, source, table: True)
    monkeypatch.setattr(gateway.rbac, "allowed_tables", lambda role, source, tables: list(tables))
    res = gateway.execute(ADMIN, "dbx_all", "SELECT region FROM main.sales.orders", "test")
    assert res.rows == [["EU"]]
    with pytest.raises(QueryRejected) as e:
        gateway.execute(ADMIN, "dbx_all", "SELECT region FROM orders", "test")
    assert "catalog.schema.table" in str(e.value)

    md = skills._build(_Source(), "admin", ["main.sales.orders"], {}, "fp")
    assert "write EVERY table" in md and "### main.sales.orders" in md
