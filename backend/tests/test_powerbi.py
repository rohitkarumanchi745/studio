"""Power BI semantic models as a source — through the SAME gate as every other.

A fake Power BI stands in for the service at the connector's one HTTP call
site (powerbi_conn._request). It speaks the real wire formats — Entra's token
endpoint, the REST listing endpoints, executeDaxQueries as LZ4-compressed
Apache Arrow IPC streams (built with pyarrow) and executeQueries as JSON — and
interprets exactly the DAX shapes the connector emits, recording every query
so the tests can assert what was ASKED of Power BI, not just what came back.

Locked in here:
- the model's tables/columns arrive under SQL-friendly names with SQL types;
  auto date tables, calculation groups, RowNumber and binary columns do not;
- a query copies only the tables (queryguard.base_tables) and columns (its
  identifiers) it names, runs through gateway.execute with RBAC, and answers
  correctly; `*`, NATURAL joins and COUNT(*) still see every row they need;
- a copy is whole or refused: over POWERBI_MAX_ROWS, over the JSON API's
  per-query cap, or silently short (15 MB cut) all raise — never a partial
  aggregate — while a bare LIMIT preview asks for only n rows (TOPN);
- the JSON transport works without pyarrow, and auto mode falls back to it
  when executeDaxQueries is unavailable;
- errors name the fix and never carry the secret; a misquoted column fails
  instead of becoming a string; names resolve to ids, ambiguous ones refuse;
- the Data connections screen signs in with the credential alone (browse),
  lists workspaces/models, saves only a source pinned to a model, and shows
  Power BI's setup errors instead of the generic redaction — without the secret.

Run from the backend directory:
    python -m pytest tests/test_powerbi.py -q
"""
import datetime as dt
import importlib
import json
import re
import sqlite3
from decimal import Decimal

import pytest

from app import db, gateway, governance
from app.connectors import powerbi_conn
from app.connectors.powerbi_conn import PowerBIConnector, PowerBIError, TooLarge
from app.queryguard import QueryRejected

pa = pytest.importorskip("pyarrow")

ANALYST = {"id": "u-pbi", "email": "pbi@studio.test", "role": "analyst"}
VIEWER = {"id": "u-pbi-v", "email": "pbiv@studio.test", "role": "viewer"}

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SECRET = "pbi-s3cret-value"
WS = "f089354e-8366-4e18-aea3-4cb4a3a50b48"
WS2 = "0f0f0f0f-0000-4000-8000-000000000002"
DS = "cfafbeb1-8037-4d0c-896e-a46fb27ff229"
DUP1 = "d0000000-0000-4000-8000-000000000001"
DUP2 = "d0000000-0000-4000-8000-000000000002"
FIN = "f1000000-0000-4000-8000-000000000001"
ROWNUM = "RowNumber-2662979B-1795-4F74-8F37-6A1BA8059B61"
AUTODATE = "LocalDateTable_0b6a1e1a-5c2f-4f61-9a43-5e3b0d1c2a10"


def _when(*a):
    return dt.datetime(*a)


# The semantic model: {table: {"columns": [(name, DataType, FormatString, Type)], "rows": [...]}}
MODEL = {
    "Sales Order Lines": {
        "columns": [("Order Date", "DateTime", "Long Date", "Data"),
                    ("Region", "String", "", "Data"),
                    ("Amount", "Decimal", "\\$#,0.00", "Data"),
                    ("Qty", "Int64", "0", "Data"),
                    ("Is Return", "Boolean", "", "Data"),
                    ("Shipped At", "DateTime", "General Date", "Data"),
                    ("Photo", "Binary", "", "Data"),
                    (ROWNUM, "Int64", "", "RowNumber")],
        "rows": [
            (_when(2024, 1, 5), "West", Decimal("100.50"), 2, False, _when(2024, 1, 6, 14, 30), None, 1),
            (_when(2024, 1, 20), "East", Decimal("200.00"), 1, False, _when(2024, 1, 21, 9, 0), None, 2),
            (_when(2024, 2, 3), "West", Decimal("50.25"), 5, True, None, None, 3),
            (_when(2024, 2, 14), "East", Decimal("75.00"), 3, False, _when(2024, 2, 15, 8, 15), None, 4),
            (_when(2024, 2, 28), "West", Decimal("125.00"), 1, False, _when(2024, 3, 1, 10, 0), None, 5),
        ],
    },
    "DimCustomer": {
        "columns": [("CustomerKey", "Int64", "0", "Data"), ("Name", "String", "", "Data"),
                    ("Order", "String", "", "Data"), (ROWNUM, "Int64", "", "RowNumber")],
        "rows": [(1, "Ada", "A", 1), (2, "Bo", "B", 2)],
    },
    AUTODATE: {"columns": [("Date", "DateTime", "", "Data")], "rows": [(_when(2024, 1, 1),)]},
    "Time Intelligence": {"columns": [("Name", "String", "", "Data")], "rows": [("YTD",)],
                          "calc_group": 0},
}


class _Resp:
    def __init__(self, status, body=None, content=None, ctype=None):
        self.status_code = status
        self.ok = status < 400
        self._body = body
        self.content = content if content is not None else json.dumps(body or {}).encode()
        self.headers = {"Content-Type": ctype or ("application/vnd.apache.arrow.stream"
                                                  if content is not None else "application/json")}

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


def _stream(columns, rows, metadata=None):
    """One Arrow IPC stream, LZ4-compressed like executeDaxQueries'."""
    data = {c: [r[i] for r in rows] for i, c in enumerate(columns)}
    table = pa.table(data) if rows else pa.table({c: pa.array([], pa.null()) for c in columns})
    if metadata:
        table = table.replace_schema_metadata(metadata)
    sink = pa.BufferOutputStream()
    opts = pa.ipc.IpcWriteOptions(compression="lz4")
    with pa.ipc.new_stream(sink, table.schema, options=opts) as w:
        w.write_table(table)
    return sink.getvalue().to_pybytes()


def _error_stream(message):
    return _stream(["ErrorCode", "ErrorMessage"], [["-1052311437", message]],
                   {"IsError": "true", "FaultCode": "0xC1000000", "FaultString": message})


def _json_value(v):
    if isinstance(v, dt.datetime):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


class FakePowerBI:
    """Power BI's REST surface in-process. `arrow=False` makes executeDaxQueries
    unavailable (404), the way a tenant without it answers."""

    def __init__(self, model=MODEL, arrow=True):
        self.model = model
        self.arrow = arrow
        self.queries = []           # (endpoint, dax, row_limit)
        self.calls = []             # (method, path)
        self.short_json = False     # drop the last row of a JSON read (the 15 MB cut)
        self.error_on = None        # a DAX substring answered with an error rowset
        self.api_status = None      # force every REST call to this status

    def __call__(self, method, url, **kw):
        if url.startswith("https://login.microsoftonline.com/"):
            data = kw["data"]
            assert data["scope"] == "https://analysis.windows.net/powerbi/api/.default"
            if data["client_secret"] != SECRET:
                return _Resp(401, {"error": "invalid_client", "error_description":
                                   "AADSTS7000215: Invalid client secret provided. Ensure the "
                                   "secret being sent is the client secret value.\r\n"
                                   "Trace ID: 1234\r\nCorrelation ID: 5678"})
            return _Resp(200, {"access_token": "tok-123", "expires_in": 3600})
        assert kw["headers"]["Authorization"] == "Bearer tok-123"
        path = url.split("/v1.0/myorg", 1)[1]
        self.calls.append((method, path))
        if self.api_status:
            return _Resp(self.api_status, {"error": {"code": "PowerBINotAuthorizedException",
                                                     "message": "API is not accessible"}})
        if path == "/groups":
            assert kw["params"] == {"$top": 5000}
            return _Resp(200, {"value": [{"id": WS, "name": "Sales"}, {"id": WS2, "name": "Finance"}]})
        m = re.fullmatch(r"/groups/([^/]+)/datasets", path)
        if m:
            sets = {WS: [{"id": DS, "name": "Sales Model"}, {"id": DUP1, "name": "Dup"},
                         {"id": DUP2, "name": "Dup"}],
                    WS2: [{"id": FIN, "name": "Finance Model"}]}
            return _Resp(200, {"value": sets.get(m.group(1), [])})
        m = re.fullmatch(r"/groups/([^/]+)/datasets/([^/]+)/(executeDaxQueries|executeQueries)", path)
        assert m and m.group(1) == WS and m.group(2) == DS, path
        body = kw["json"]
        if m.group(3) == "executeDaxQueries":
            self.queries.append(("arrow", body["query"], body.get("resultSetRowCountLimit")))
            if not self.arrow:
                return _Resp(404, {"error": {"code": "EntityNotFound"}})
            if self.error_on and self.error_on in body["query"]:
                return _Resp(200, content=_error_stream("Query (1, 1) The syntax is incorrect."))
            limit = body.get("resultSetRowCountLimit") or 1_000_000
            return _Resp(200, content=b"".join(
                _stream(cols, rows[:limit]) for cols, rows in self._answer(body["query"], arrow=True)))
        query = body["queries"][0]["query"]
        self.queries.append(("json", query, None))
        assert body["serializerSettings"] == {"includeNulls": True}
        if self.error_on and self.error_on in query:
            return _Resp(200, {"results": [{"error": {"code": "DatasetExecuteQueriesError",
                                                      "message": "DAX query failure"}}]})
        (cols, rows), = self._answer(query, arrow=False)
        error = None
        cap = min(powerbi_conn._JSON_MAX_ROWS, powerbi_conn._JSON_MAX_VALUES // max(1, len(cols)))
        if len(rows) > cap:
            rows, error = rows[:cap], {"code": "DatasetExecuteQueriesError",
                                       "message": f"More than {cap} rows in a query result"}
        if self.short_json and "SELECTCOLUMNS" in query:
            rows = rows[:-1]
        table = {"rows": [{c: _json_value(v) for c, v in zip(cols, r)} for r in rows]}
        if error:
            table["error"] = error
        return _Resp(200, {"results": [{"tables": [table]}]})

    # ── the DAX the connector emits ──
    def _answer(self, q, arrow):
        if "INFO.VIEW.TABLES()" in q:
            tables = [[i, name, False, name.startswith("LocalDateTable"),
                       name.startswith("LocalDateTable"), t.get("calc_group")]
                      for i, (name, t) in enumerate(self.model.items())]
            columns = [[t, c[0], c[1], c[3], "RowNumber" if c[3] == "RowNumber" else "", c[2], False]
                       for t, spec in self.model.items() for c in spec["columns"]]
            return [(["[ID]", "[Name]", "[IsHidden]", "[IsPrivate]", "[ShowAsVariationOnly]",
                      "[CalculationGroupPrecedence]"], tables),
                    (["[Table]", "[Name]", "[DataType]", "[Type]", "[DataCategory]",
                      "[FormatString]", "[IsHidden]"], columns)]
        if "COLUMNSTATISTICS()" in q:
            rows = []
            for t, spec in self.model.items():
                for i, c in enumerate(spec["columns"]):
                    if c[1] == "Binary":
                        continue
                    vals = [r[i] for r in spec["rows"] if r[i] is not None]
                    text = c[1] == "String"
                    rows.append([t, c[0], _json_value(min(vals)) if vals else None,
                                 _json_value(max(vals)) if vals else None, len(set(vals)),
                                 max(len(v) for v in vals) if text and vals else None])
            return [(["[Table Name]", "[Column Name]", "[Min]", "[Max]", "[Cardinality]",
                      "[Max Length]"], rows)]
        m = re.search(r"ROW\(\"n\", COUNTROWS\('((?:[^']|'')*)'\)\)", q)
        if m:
            return [(["[n]"], [[len(self.model[m.group(1).replace("''", "'")]["rows"])]])]
        table = re.search(r"SELECTCOLUMNS\(\s*'((?:[^']|'')*)'", q).group(1).replace("''", "'")
        pairs = re.findall(r'"((?:[^"]|"")*)",\s*\'(?:[^\']|\'\')*\'\[((?:[^\]]|\]\])*)\]', q)
        spec = self.model[table]
        index = {c[0]: i for i, c in enumerate(spec["columns"])}
        rows = [[r[index[col.replace("]]", "]")]] for _, col in pairs] for r in spec["rows"]]
        top = re.search(r"TOPN\((\d+),", q)
        if top:
            rows = rows[:int(top.group(1))]
        return [([f"[{out}]" for out, _ in pairs], rows)]

    def fetches(self):
        """(table, [columns], topn) for every data read the connector made."""
        out = []
        for _, q, _limit in self.queries:
            m = re.search(r"SELECTCOLUMNS\(\s*'((?:[^']|'')*)'", q)
            if m:
                top = re.search(r"TOPN\((\d+),", q)
                out.append((m.group(1), re.findall(r'"([^"]+)",', q),
                            int(top.group(1)) if top else None))
        return out


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    yield


@pytest.fixture()
def fake(monkeypatch):
    """A fresh fake Power BI and a fresh env-configured `powerbi` source."""
    from app import connectors
    f = FakePowerBI()
    monkeypatch.setattr(powerbi_conn, "_request", f)
    powerbi_conn._TOKENS.clear()
    for k, v in {"POWERBI_TENANT_ID": TENANT, "POWERBI_CLIENT_ID": CLIENT,
                 "POWERBI_CLIENT_SECRET": SECRET, "POWERBI_WORKSPACE": WS,
                 "POWERBI_DATASET": DS}.items():
        monkeypatch.setenv(k, v)
    for k in ("POWERBI_API", "POWERBI_MAX_ROWS", "POWERBI_SCHEMA_TTL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setitem(connectors._REGISTRY, "powerbi", PowerBIConnector())
    governance._STATE.update(doc=None, yaml="", source=None)
    yield f
    governance._STATE.update(doc=None, yaml="", source=None)
    powerbi_conn._TOKENS.clear()


def _run(sql, user=ANALYST, **kw):
    return gateway.execute(user, "powerbi", sql, "pbi_test", **kw)


# ── The model, as SQL sees it ───────────────────────────────────────────

def test_discovery_maps_the_model_to_sql_names_and_types(fake):
    conn = PowerBIConnector()
    assert conn.list_tables() == ["dim_customer", "sales_order_lines"]
    assert conn.get_schema("sales_order_lines") == [
        {"name": "order_date", "type": "DATE"},          # 'Long Date' format: no time of day
        {"name": "region", "type": "TEXT"},
        {"name": "amount", "type": "REAL"},
        {"name": "qty", "type": "INTEGER"},
        {"name": "is_return", "type": "BOOLEAN"},
        {"name": "shipped_at", "type": "TIMESTAMP"},     # 'General Date' carries a time
    ]                                                    # Photo (binary) and RowNumber: gone
    # A reserved word gets a suffix rather than a query that cannot parse.
    assert [c["name"] for c in conn.get_schema("dim_customer")] == ["customer_key", "name", "order_"]
    # One metadata round trip, through the Arrow API, cached for the next call.
    conn.list_tables()
    assert [(e, q) for e, q, _ in fake.queries] == [
        ("arrow", "EVALUATE INFO.VIEW.TABLES()\nEVALUATE INFO.VIEW.COLUMNS()")]


def test_names_that_would_collide_both_get_stable_suffixes():
    names = powerbi_conn._sql_names(["Sales Amount", "SalesAmount", "Region"], "c")
    assert names["Region"] == "region"
    assert names["Sales Amount"] != names["SalesAmount"]
    assert all(n.startswith("sales_amount_") for k, n in names.items() if k != "Region")
    # Stable: adding a third spelling later must not hand either one the bare name.
    again = powerbi_conn._sql_names(["Sales-Amount", "Sales Amount", "SalesAmount"], "c")
    assert again["Sales Amount"] == names["Sales Amount"] and "sales_amount" not in again.values()


@pytest.mark.parametrize("raw,sql", [
    ("Sales Order Lines", "sales_order_lines"), ("CustomerKey", "customer_key"),
    ("SKUCode", "sku_code"), ("Order", "order_"), ("Set", "set_"), ("2023 Sales", "c_2023_sales"),
    ("% Margin", "margin"), ("Größe", "grosse"), ("ProductID", "product_id"),
])
def test_sql_names(raw, sql):
    assert powerbi_conn._sql_name(raw, "c") == sql


# ── Reading through the gateway ─────────────────────────────────────────

def test_a_query_copies_only_the_tables_and_columns_it_names(fake):
    r = _run("SELECT region, SUM(amount) AS revenue FROM sales_order_lines "
             "GROUP BY region ORDER BY region")
    assert r.rows == [["East", 275.0], ["West", 275.75]]
    # DimCustomer was never read; of Sales Order Lines, only the two named columns.
    assert fake.fetches() == [("Sales Order Lines", ["region", "amount"], None)]
    data = [q for q in fake.queries if "SELECTCOLUMNS" in q[1]]
    assert data[0][2] == 250_001        # one row over POWERBI_MAX_ROWS, to see "too big"
    assert "'Sales Order Lines'[Order Date]" not in data[0][1]


def test_dates_are_iso_text_sqlite_can_bucket_and_compare(fake):
    r = _run("SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS n "
             "FROM sales_order_lines GROUP BY month ORDER BY month")
    assert r.rows == [["2024-01", 2], ["2024-02", 3]]
    r = _run("SELECT shipped_at FROM sales_order_lines WHERE order_date = '2024-01-05'")
    assert r.rows == [["2024-01-06 14:30:00"]]


def test_a_join_reads_both_tables_and_count_star_keeps_every_row(fake):
    r = _run("SELECT COUNT(*) AS n FROM sales_order_lines")
    assert r.rows == [[5]]
    # COUNT(*) names no column: one narrow column is copied, every row of it.
    assert fake.fetches()[-1] == ("Sales Order Lines", ["order_date"], None)
    r = _run("SELECT c.name, s.qty FROM sales_order_lines s JOIN dim_customer c "
             "ON s.qty = c.customer_key ORDER BY c.name, s.qty")
    assert r.rows == [["Ada", 1], ["Ada", 1], ["Bo", 2]]
    assert sorted(t for t, _, _ in fake.fetches()[-2:]) == ["DimCustomer", "Sales Order Lines"]


def test_star_and_natural_join_copy_every_column(fake):
    _run("SELECT * FROM sales_order_lines WHERE qty > 1")
    assert fake.fetches()[-1][1] == ["order_date", "region", "amount", "qty", "is_return",
                                     "shipped_at"]
    # `price * qty` is arithmetic, not a star.
    _run("SELECT amount * qty AS total FROM sales_order_lines WHERE region = 'West'")
    assert fake.fetches()[-1][1] == ["region", "amount", "qty"]


def test_a_preview_asks_power_bi_for_only_n_rows(fake):
    r = _run("SELECT * FROM sales_order_lines LIMIT 2")
    assert r.row_count == 2
    assert fake.fetches()[-1][2] == 2                     # TOPN(2, …)
    # The gateway appends its own LIMIT to a bare SELECT — still a preview.
    r = _run("SELECT region FROM sales_order_lines", max_rows=3)
    assert r.row_count == 3 and fake.fetches()[-1][2] == 3


def test_a_table_over_the_cap_is_refused_never_truncated(fake, monkeypatch):
    monkeypatch.setenv("POWERBI_MAX_ROWS", "3")
    with pytest.raises(TooLarge, match="won't answer from part of one"):
        _run("SELECT region, SUM(amount) FROM sales_order_lines GROUP BY region")
    # A preview is still fine: any 2 rows answer it.
    assert _run("SELECT region FROM sales_order_lines LIMIT 2").row_count == 2


def test_a_misquoted_column_fails_instead_of_becoming_a_string(fake):
    # SQLite's legacy fallback would return the text 'Order Date' for every row.
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        _run('SELECT "Order Date" FROM sales_order_lines WHERE qty > 0')


def test_rbac_applies_viewer_has_no_power_bi_by_default(fake):
    with pytest.raises(QueryRejected):
        _run("SELECT region FROM sales_order_lines", user=VIEWER)
    assert fake.fetches() == []                          # refused before any copy


def test_an_error_rowset_is_raised_not_read_as_empty(fake):
    fake.error_on = "SELECTCOLUMNS"
    with pytest.raises(PowerBIError, match="syntax is incorrect"):
        _run("SELECT region FROM sales_order_lines WHERE qty > 1")


# ── The JSON transport ──────────────────────────────────────────────────

def test_without_pyarrow_the_json_api_serves_metadata_and_rows(fake, monkeypatch):
    monkeypatch.setattr(powerbi_conn, "_has_arrow", lambda: False)
    conn = PowerBIConnector()
    # COLUMNSTATISTICS cannot tell a calculation group from a table, so on this
    # path one shows up — harmless: its rows are the calculation items' names.
    # Auto date tables and RowNumber columns are recognisable by name and go.
    assert conn.list_tables() == ["dim_customer", "sales_order_lines", "time_intelligence"]
    # COLUMNSTATISTICS reports values, not types — the types are inferred.
    assert {c["name"]: c["type"] for c in conn.get_schema("sales_order_lines")} == {
        "order_date": "DATE", "region": "TEXT", "amount": "REAL", "qty": "INTEGER",
        "is_return": "BOOLEAN", "shipped_at": "TIMESTAMP"}
    r = _run("SELECT region, SUM(qty) AS q FROM sales_order_lines GROUP BY region ORDER BY region")
    assert r.rows == [["East", 4], ["West", 8]]
    assert all(e == "json" for e, _, _ in fake.queries)
    # A whole-table read is counted first, so a short answer can be recognised.
    assert any("COUNTROWS('Sales Order Lines')" in q for _, q, _ in fake.queries)


def test_auto_mode_falls_back_to_json_and_parks_arrow(fake):
    fake.arrow = False
    r = _run("SELECT COUNT(*) FROM dim_customer")
    assert r.rows == [[2]]
    tried = [e for e, _, _ in fake.queries]
    assert tried[0] == "arrow" and "json" in tried
    before = tried.count("arrow")
    _run("SELECT COUNT(*) FROM sales_order_lines")
    assert [e for e, _, _ in fake.queries].count("arrow") == before   # parked: not retried


def test_the_json_apis_per_query_cap_refuses_rather_than_truncates(fake, monkeypatch):
    from app import connectors
    monkeypatch.setattr(powerbi_conn, "_has_arrow", lambda: False)
    connectors.get_connector("powerbi").list_tables()     # metadata first, at the real cap
    monkeypatch.setattr(powerbi_conn, "_JSON_MAX_ROWS", 4)
    with pytest.raises(TooLarge, match="has 5 rows, over the 4 rows .* executeQueries"):
        _run("SELECT SUM(amount) FROM sales_order_lines")
    # Refused on the count, before the rows were ever requested.
    assert not any("SELECTCOLUMNS" in q for _, q, _ in fake.queries)


def test_a_silently_short_json_answer_is_refused(fake, monkeypatch):
    monkeypatch.setattr(powerbi_conn, "_has_arrow", lambda: False)
    fake.short_json = True
    with pytest.raises(PowerBIError, match="returned 4 of 5 rows"):
        _run("SELECT SUM(amount) FROM sales_order_lines")


# ── Credentials, names, browsing ────────────────────────────────────────

def test_a_wrong_secret_names_the_fix_and_never_the_secret(fake, monkeypatch):
    monkeypatch.setenv("POWERBI_CLIENT_SECRET", "wrong-secret-xyz")
    with pytest.raises(PowerBIError) as e:
        PowerBIConnector().list_tables()
    assert "AADSTS7000215" in str(e.value) and "Trace ID" not in str(e.value)
    assert "wrong-secret-xyz" not in str(e.value) and SECRET not in str(e.value)


def test_an_api_refusal_explains_the_tenant_setting(fake):
    fake.api_status = 401
    with pytest.raises(PowerBIError, match="service principals") as e:
        PowerBIConnector().list_tables()
    assert e.value.fatal and "API is not accessible" in str(e.value)


def test_names_resolve_to_ids_and_an_ambiguous_name_is_refused(fake, monkeypatch):
    monkeypatch.setenv("POWERBI_WORKSPACE", "Sales")
    monkeypatch.setenv("POWERBI_DATASET", "sales model")      # case-insensitive fallback
    assert PowerBIConnector().list_tables() == ["dim_customer", "sales_order_lines"]
    monkeypatch.setenv("POWERBI_DATASET", "Dup")
    with pytest.raises(PowerBIError, match="2 semantic models are named 'Dup'"):
        PowerBIConnector().list_tables()
    monkeypatch.setenv("POWERBI_DATASET", "Nope")
    with pytest.raises(PowerBIError, match="No semantic model named 'Nope'"):
        PowerBIConnector().list_tables()


def test_browse_lists_models_by_name_and_duplicates_by_id(fake):
    found = PowerBIConnector().list_namespaces()
    assert found == [
        {"database": "Finance", "schema": "Finance Model"},
        {"database": "Sales", "schema": DUP1},
        {"database": "Sales", "schema": DUP2},
        {"database": "Sales", "schema": "Sales Model"},
    ]


def test_an_unpinned_source_is_unconfigured_but_can_still_browse(fake, monkeypatch):
    # No model pinned: nothing to read, so not a configured source — but the
    # credential alone is enough to list what it could be pinned to.
    monkeypatch.delenv("POWERBI_DATASET")
    conn = PowerBIConnector()
    assert not conn.configured()
    assert {"database": "Sales", "schema": "Sales Model"} in conn.list_namespaces()
    assert conn.namespaces_truncated is False


def test_stale_metadata_is_served_briefly_when_a_refresh_fails(fake, monkeypatch):
    monkeypatch.setenv("POWERBI_SCHEMA_TTL", "0")
    conn = PowerBIConnector()
    assert conn.list_tables() == ["dim_customer", "sales_order_lines"]
    fake.api_status = 429
    monkeypatch.setattr(powerbi_conn.time, "sleep", lambda s: None)
    assert conn.list_tables() == ["dim_customer", "sales_order_lines"]


# ── Data connections screen ─────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch, fake):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "pbi_app.db"))
    monkeypatch.setenv("STUDIO_SECRET", "pbi-conn-secret-delta")
    monkeypatch.setenv("STUDIO_GRAPH_SYNC_TICKER", "0")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import connections
    connections._CACHE.clear()
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        tok = c.post("/api/auth/login", json={"email": "admin@studio.local",
                                              "password": "admin123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c
    connections._CACHE.clear()


CREDS = {"tenant_id": TENANT, "client_id": CLIENT, "client_secret": SECRET}


def test_the_connect_screen_signs_in_lists_models_and_pins_one(client, fake):
    types = {t["ctype"]: t for t in client.get("/api/connections/types").json()}
    assert types["powerbi"]["ns"] == {"schema": "dataset", "database": "workspace"}

    # Signing in IS the browse: the credential alone lists every model.
    browse = client.post("/api/connections/browse", json={"ctype": "powerbi", "config": CREDS})
    assert browse.status_code == 200 and browse.json()["ok"] is True
    assert {"database": "Sales", "schema": "Sales Model"} in browse.json()["namespaces"]

    # The credential alone is not a source…
    r = client.post("/api/connections", json={"name": "pbi", "ctype": "powerbi", "config": CREDS})
    assert r.status_code == 400 and "pick a semantic model" in r.json()["detail"]
    # …a picked model is.
    r = client.post("/api/connections", json={
        "name": "pbi-sales", "ctype": "powerbi",
        "config": {**CREDS, "workspace": "Sales", "dataset": "Sales Model"}})
    assert r.status_code == 201, r.text
    assert r.json()["namespace"] == "Sales.Sales Model" and SECRET not in r.text
    assert SECRET not in client.get("/api/connections").text

    from app import connectors
    assert connectors.get_connector("pbi-sales").list_tables() == ["dim_customer",
                                                                    "sales_order_lines"]


def test_a_failed_sign_in_shows_the_fix_not_a_redaction_and_never_the_secret(client, fake):
    bad = {**CREDS, "client_secret": "wrong-secret-xyz"}
    r = client.post("/api/connections/browse", json={"ctype": "powerbi", "config": bad})
    body = r.json()
    assert body["ok"] is False and "AADSTS7000215" in body["error"]
    assert "redacted" not in body["error"]
    assert "wrong-secret-xyz" not in r.text and SECRET not in r.text
