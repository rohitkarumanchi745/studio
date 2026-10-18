"""Bulk relation metadata (Connector.describe_relations) on the real connector
code, against a scripted Databricks warehouse shaped like studio_dev.retail_qa:
seven tables and a view, as SHOW TABLES lists them (views included)."""
import importlib
import types

import pytest

from app import connections

TABLES = ["retail_customers", "retail_inventory", "retail_order_items", "retail_orders",
          "retail_products", "retail_returns", "retail_stores"]
VIEW = "retail_sales_detail"
COLUMNS = {"retail_orders": [("order_id", "bigint"), ("customer_id", "bigint"),
                             ("store_id", "bigint"), ("order_date", "date"),
                             ("status", "string")],
           VIEW: [("order_id", "bigint"), ("net_sales", "decimal(12,2)")]}


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


def _connector(script, catalog="studio_dev", schema="retail_qa"):
    conn = connections._DynDatabricks("databricks-3", {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", "catalog": catalog, "schema": schema})
    cur = _Cur(script)
    conn._conn = lambda: types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)
    return conn, cur


def _tables_rows(cat="studio_dev", sch="retail_qa"):
    return ([(cat, sch, t, "MANAGED") for t in TABLES] + [(cat, sch, VIEW, "VIEW")])


def _column_rows(cat="studio_dev", sch="retail_qa"):
    return [(cat, sch, t, c, ty) for t, cols in COLUMNS.items() for c, ty in cols]


def _pinned_script(columns=None):
    return [("SHOW TABLES", [("retail_qa", t, False) for t in TABLES + [VIEW]]),
            ("information_schema.tables", _tables_rows()),
            ("full_data_type", columns if columns is not None else _column_rows())]


def test_pinned_schema_describes_kinds_and_columns_in_two_queries():
    conn, cur = _connector(_pinned_script())
    out = conn.describe_relations()
    assert {n for n, v in out.items() if v["kind"] == "table"} == set(TABLES)
    assert out[VIEW]["kind"] == "view"
    assert out["retail_orders"]["namespace"] == "studio_dev.retail_qa"
    assert out["retail_orders"]["columns"][0] == {"name": "order_id", "type": "bigint"}
    assert out[VIEW]["columns"][1] == {"name": "net_sales", "type": "decimal(12,2)"}
    assert len(cur.sql) == 2
    # The pinned namespace comes from the session, not from interpolated text.
    assert all("current_catalog()" in s and "current_schema()" in s for s in cur.sql)
    assert not any("retail_qa" in s for s in cur.sql)


def test_all_schemas_names_relations_by_full_name():
    conn, cur = _connector([
        ("`studio_dev`.information_schema.tables", _tables_rows()),
        ("`studio_dev`.information_schema.columns", _column_rows())], schema="*")
    out = conn.describe_relations()
    assert "studio_dev.retail_qa.retail_orders" in out
    assert out[f"studio_dev.retail_qa.{VIEW}"]["kind"] == "view"
    assert "table_catalog NOT IN ('system')" in cur.sql[0]


def test_older_runtimes_fall_back_to_data_type():
    script = [("information_schema.tables", _tables_rows()),
              ("full_data_type", RuntimeError("UNRESOLVED_COLUMN full_data_type")),
              ("data_type", _column_rows())]
    conn, _ = _connector(script)
    assert conn.describe_relations()["retail_orders"]["columns"][1]["name"] == "customer_id"


def test_no_information_schema_means_not_supported():
    conn, _ = _connector([("information_schema", RuntimeError("Hive metastore"))])
    assert conn.describe_relations() is None


@pytest.mark.parametrize("ttype,kind", [("EXTERNAL", "table"), ("MATERIALIZED_VIEW", "view"),
                                        ("STREAMING_TABLE", "table"), ("SOMETHING_NEW", "other")])
def test_relation_types_map_to_kinds(ttype, kind):
    conn, _ = _connector([("information_schema.tables", [("c", "s", "t", ttype)]),
                          ("full_data_type", [])])
    assert conn.describe_relations()["t"]["kind"] == kind


class _SignalCur(_Cur):
    def fetchone(self):
        return self.rows[0]


def test_the_change_signal_is_one_cheap_metadata_query():
    cur = _SignalCur([("MAX(last_altered)", [(8, "2026-10-17 12:00:00")])])
    conn = connections._DynDatabricks("databricks-3", {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", "catalog": "studio_dev", "schema": "retail_qa"})
    conn._conn = lambda: types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)
    assert conn.change_signal() == "8|2026-10-17 12:00:00"
    assert len(cur.sql) == 1 and "current_schema()" in cur.sql[0]
    cur.script = [("MAX(last_altered)", RuntimeError("no last_altered here"))]
    assert conn.change_signal() is None              # no signal: full-sync cadence instead


def test_discovering_the_retail_schema_end_to_end(tmp_path, monkeypatch):
    """Connect studio_dev.retail_qa → discovery finds seven tables plus the view."""
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "e2e.db"))
    from app import db as _db
    importlib.reload(_db)
    from app import connectors, governance, jobs, metagraph
    _db.init_db()
    jobs.init_tables()
    metagraph.init_tables()
    conn, _ = _connector(_pinned_script())
    monkeypatch.setattr(conn, "configured", lambda: True)
    monkeypatch.setitem(connectors._REGISTRY, "databricks-3", conn)
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n', "test")
    try:
        out = jobs.handlers()["metagraph_discover"]({"source": "databricks-3",
                                                      "trigger": "connection_saved"}, None)
        assert out["objects"] == 8 and out["complete"]
        detail = metagraph.get_source("databricks-3", user={"id": "a", "role": "admin"})
        assert detail["kinds"] == {"table": 7, "view": 1}
        assert {r["name"] for r in detail["relations"]} == set(TABLES) | {VIEW}
    finally:
        governance._STATE.update(doc=None, yaml="", source=None)
