"""The metadata graph works for any schema, not the one it was built against.

Four schemas with conventions the retail tables never used — PascalCase ERP
keys (SalesOrderID, a header/detail pair), a legacy abbreviated schema
(cust_no, prod_cd, ord_ln), a star schema with surrogate keys (dim_customer,
customer_sk) and a SaaS schema with fk_ prefixes and short forms (fk_account,
acct_id, subscription_ref) — each with its known joins. Discovery, relationship
inference, question grounding and cross-source entities must recover every
known join, invent none, and find the right tables for a plain question.
"""
import importlib
import sqlite3
import types

import pytest

from app import identifiers as ids
from app.connectors.base import Connector

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}

I, T, D, N = "INTEGER", "TEXT", "DATE", "DECIMAL(12,2)"

SCHEMAS = {
    "erp": {
        "tables": {
            "Customer": [("CustomerID", I), ("CustomerName", T), ("Segment", T)],
            "SalesOrderHeader": [("SalesOrderID", I), ("CustomerID", I), ("TerritoryID", I),
                                 ("OrderDate", D)],
            "SalesOrderDetail": [("SalesOrderDetailID", I), ("SalesOrderID", I),
                                 ("ProductID", I), ("OrderQty", I), ("UnitPrice", N)],
            "Product": [("ProductID", I), ("Name", T), ("ListPrice", N)],
            "SalesTerritory": [("TerritoryID", I), ("Name", T), ("Region", T)],
        },
        "joins": {("SalesOrderHeader", "CustomerID", "Customer", "CustomerID"),
                  ("SalesOrderHeader", "TerritoryID", "SalesTerritory", "TerritoryID"),
                  ("SalesOrderDetail", "SalesOrderID", "SalesOrderHeader", "SalesOrderID"),
                  ("SalesOrderDetail", "ProductID", "Product", "ProductID")},
        "question": "total order quantity by sales territory region",
        "needs": {"SalesOrderDetail", "SalesOrderHeader", "SalesTerritory"},
    },
    "legacy": {
        "tables": {
            "cust": [("cust_no", I), ("cust_nm", T)],
            "ord": [("ord_no", I), ("cust_no", I), ("ord_dt", D)],
            "ord_ln": [("ord_no", I), ("ln_no", I), ("prod_cd", T), ("qty", I)],
            "prod": [("prod_cd", T), ("prod_desc", T)],
        },
        "joins": {("ord", "cust_no", "cust", "cust_no"), ("ord_ln", "ord_no", "ord", "ord_no"),
                  ("ord_ln", "prod_cd", "prod", "prod_cd")},
        "question": "quantity ordered by product description",
        "needs": {"ord_ln", "prod"},
    },
    "star": {
        "tables": {
            "dim_customer": [("customer_sk", I), ("customer_name", T), ("segment", T)],
            "dim_product": [("product_sk", I), ("product_name", T), ("category", T)],
            "dim_date": [("date_sk", I), ("calendar_date", D), ("fiscal_quarter", T)],
            "fact_sales": [("date_sk", I), ("customer_sk", I), ("product_sk", I),
                           ("amount", N)],
        },
        "joins": {("fact_sales", "customer_sk", "dim_customer", "customer_sk"),
                  ("fact_sales", "product_sk", "dim_product", "product_sk"),
                  ("fact_sales", "date_sk", "dim_date", "date_sk")},
        "question": "sales amount by customer segment and fiscal quarter",
        "needs": {"fact_sales", "dim_customer", "dim_date"},
    },
    "saas": {
        "tables": {
            "accounts": [("id", I), ("name", T), ("plan", T)],
            "users": [("id", I), ("fk_account", I), ("email", T)],
            "subscriptions": [("id", I), ("acct_id", I), ("mrr", N), ("started_on", D)],
            "invoices": [("id", I), ("subscription_ref", I), ("amount_due", N)],
        },
        "joins": {("users", "fk_account", "accounts", "id"),
                  ("subscriptions", "acct_id", "accounts", "id"),
                  ("invoices", "subscription_ref", "subscriptions", "id")},
        "question": "invoice amount due by account plan",
        "needs": {"invoices", "subscriptions", "accounts"},
    },
}


class SqliteSource(Connector):
    dialect = "sqlite"

    def __init__(self, name, path):
        self.name, self.path = name, path

    def configured(self):
        return True

    def list_tables(self):
        with sqlite3.connect(self.path) as c:
            return [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def get_schema(self, table):
        with sqlite3.connect(self.path) as c:
            return [{"name": r[1], "type": r[2]} for r in c.execute(f'PRAGMA table_info("{table}")')]

    def describe_relations(self):
        return {t: {"kind": "table", "namespace": None, "columns": self.get_schema(t)}
                for t in self.list_tables()}

    def run_query(self, sql):
        with sqlite3.connect(self.path) as c:
            cur = c.execute(sql)
            return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]


@pytest.fixture()
def graph(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "anyschema.db"))
    monkeypatch.setenv("STUDIO_SECRET", "anyschema-secret")
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, governance, jobs, metaentities, metagraph
    from app.connectors import demo
    _db.init_db()
    for module in (jobs, metagraph, connections):
        module.init_tables()
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo_warehouse.db"))
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n', "test")
    for name, schema in SCHEMAS.items():
        path = tmp_path / f"{name}.db"
        with sqlite3.connect(path) as c:
            for table, cols in schema["tables"].items():
                c.execute(f'CREATE TABLE "{table}" ('
                          + ", ".join(f'"{n}" {t}' for n, t in cols) + ")")
        monkeypatch.setitem(connectors._REGISTRY, name, SqliteSource(name, str(path)))
        jobs.handlers()["metagraph_discover"]({"source": name, "trigger": "test"}, None)
    yield types.SimpleNamespace(mg=metagraph, me=metaentities, db=_db, mp=monkeypatch,
                                tmp=tmp_path, connectors=connectors, jobs=jobs)
    governance._STATE.update(doc=None, yaml="", source=None)


def inferred_joins(env, source):
    rels = env.mg.source_relationships(source, user=ADMIN)["relationships"]
    return {(r["from_object"], r["from_columns"][0], r["to_object"], r["to_columns"][0])
            for r in rels if r["kind"] == "join"}


# ── Every known join, nothing invented ───────────────────────────────────

@pytest.mark.parametrize("source", sorted(SCHEMAS))
def test_every_known_join_is_found_and_none_invented(graph, source):
    assert inferred_joins(graph, source) == SCHEMAS[source]["joins"]


@pytest.mark.parametrize("source", sorted(SCHEMAS))
def test_the_join_path_connects_the_whole_schema(graph, source):
    tables = sorted(SCHEMAS[source]["tables"])
    paths = graph.mg.source_paths(source, tables=",".join(tables), user=ADMIN)
    assert paths["connected"] and len(paths["joins"]) == len(tables) - 1


# ── A plain question finds its tables ────────────────────────────────────

@pytest.mark.parametrize("source", sorted(SCHEMAS))
def test_a_plain_question_grounds_on_the_right_tables(graph, source):
    from app import metaground
    spec = SCHEMAS[source]
    g = metaground.ground(source, spec["question"], list(spec["tables"]))
    assert spec["needs"] <= set(g["tables"]), (g["matched"], g["bridges"])
    linked = {t for j in g["joins"] for t in (j["from_object"], j["to_object"])}
    assert spec["needs"] <= linked                       # and they come joined


# ── Entities across sources with different conventions ───────────────────

def test_one_customer_across_three_conventions(graph):
    graph.mg.tick_once()
    ent = next(e for e in graph.mg.list_entities(user=ADMIN)["entities"]
               if e["name"] == "Customer")
    got = {(m["source"], m["object"], m["column"], m["role"]) for m in ent["members"]}
    assert {("erp", "Customer", "CustomerID", "home"),
            ("erp", "SalesOrderHeader", "CustomerID", "reference"),
            ("legacy", "cust", "cust_no", "home"),
            ("legacy", "ord", "cust_no", "reference"),
            ("star", "dim_customer", "customer_sk", "home"),
            ("star", "fact_sales", "customer_sk", "reference")} <= got
    # a text key never maps to a number key: legacy prod_cd is not a product id
    product = next(e for e in graph.mg.list_entities(user=ADMIN)["entities"]
                   if e["name"] == "Product")
    assert not any(m["column"] == "prod_cd" for m in product["members"])


# ── Reading identifiers ──────────────────────────────────────────────────

@pytest.mark.parametrize("identifier,expected", [
    ("SalesOrderDetailID", ("sale", "order", "detail", "id")),
    ("orderQty", ("order", "quantity")),
    ("HTTPStatusCode", ("http", "status", "code")),
    ("customer_sk", ("customer", "sk")),
    ("prod_desc", ("product", "description")),
])
def test_identifiers_read_as_words(identifier, expected):
    assert ids.words(identifier) == expected


@pytest.mark.parametrize("column,stem", [
    ("customer_id", ("customer",)), ("CustomerID", ("customer",)),
    ("customer_sk", ("customer",)), ("fk_customer", ("customer",)),
    ("cust_no", ("customer",)), ("SalesOrderID", ("sale", "order")),
    ("id", None), ("region", None), ("ord_dt", None), ("amount_due", None),
])
def test_key_columns_name_their_subject(column, stem):
    assert ids.key_stem(column) == stem


def test_a_schema_spelled_word_resolves_its_abbreviation_but_never_guesses():
    assert ids.expand("whs", {"warehouse", "wholesale"}) == "whs"         # two candidates
    assert ids.expand("whs", {"warehouse", "region"}) == "warehouse"
    assert ids.expand("inv") == "inv"                    # invoice or inventory: not assumed
    assert ids.expand("inv", {"invoice", "amount"}) == "invoice"


@pytest.mark.parametrize("table,stem,home", [
    ("SalesOrderHeader", ("sale", "order"), True),
    ("SalesOrderDetail", ("sale", "order"), False),       # a detail table is a child
    ("dim_customer", ("customer",), True),
    ("SalesTerritory", ("territory",), True),            # a domain prefix is fine
    ("customer_notes", ("customer",), False),
    ("ord_ln", ("order",), False),
])
def test_which_table_is_home_to_a_subject(table, stem, home):
    assert ids.names_entity(table, stem) is home


def test_an_even_tie_is_kept_and_labelled_not_guessed():
    from app import metarelations as mr
    objects = {"orders": {"kind": "table", "columns": [{"name": "region_id", "type": "int"}]},
               "sales_region": {"kind": "table", "columns": [{"name": "id", "type": "int"}]},
               "geo_region": {"kind": "table", "columns": [{"name": "id", "type": "int"}]}}
    found = mr.infer(objects, {})
    assert {k[3] for k in found} == {"sales_region", "geo_region"}
    for evidence in found.values():
        assert "one of 2 tables that could hold region" in evidence[0]["label"]
        assert evidence[0]["confidence"] < 0.6          # below the join-path threshold
