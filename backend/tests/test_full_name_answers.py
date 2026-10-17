"""An answer on a Databricks "all schemas" source stays visible on reload.

Such a source names its tables in full (catalog.schema.table) and the query
guard matches references whole. The read-time re-check of a stored answer
reduced every reference to its bare name (`retail_products`), so the guard
refused the answer's own SQL and every answer on the source came back as
"🔒 Hidden — this message used data your role cannot access." — even to the
administrator who asked it. Locked in here:
- the re-check compares full names, so the owner sees their answer again, and
  a role granted exactly that table sees it too;
- it still refuses what the live gateway would: a role without the grant, a
  bare name, a table outside the source's catalog, `system` and
  information_schema — even under a policy of "*".
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-full-name-answers-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

import app.connectors as connectors
from app import chat, connections, db, governance, pipelines, rbac

SOURCE = "databricks-3"
TABLE = "studio_dev.retail_qa.retail_products"
SQL = f"SELECT category, SUM(price) AS total FROM {TABLE} GROUP BY 1"
ADMIN = {"id": "u-fn-admin", "email": "fn-admin@studio.test", "role": "admin", "name": "Ad"}
ANA = {"id": "u-fn-ana", "email": "fn-ana@studio.test", "role": "analyst", "name": "Ana"}
POLICY = f"""
version: 1
roles:
  admin: {{ sources: "*" }}
  analyst: {{ sources: {{ {SOURCE}: [{TABLE}] }} }}
  viewer: {{ sources: {{ demo: [sales] }} }}
"""


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    yield


@pytest.fixture(autouse=True)
def _source(monkeypatch):
    conn = connections._DynDatabricks(SOURCE, {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", "catalog": "studio_dev", "schema": "*"})
    monkeypatch.setattr(connectors, "get_connector", lambda name: conn)
    monkeypatch.setattr(rbac, "_user_connection", lambda name: name == SOURCE)
    governance._set(POLICY, "test")
    yield conn
    governance._STATE.update(doc=None, yaml="", source=None)


def _answer(owner, sql=SQL, table=TABLE, author="analyst"):
    cid = db.create_conversation(owner["id"], "retail products")
    db.add_message(cid, "assistant", {
        "text": "Toys lead.", "source": SOURCE, "table": table, "sql": sql,
        "columns": ["category", "total"], "rows": [["toys", 120]], "chart": {"type": "bar"},
        "author_role": author,
        "panels": [{"sql": sql, "columns": ["category", "total"], "rows": [["toys", 120]],
                    "chart": {"type": "bar"}}]})
    return cid


def _shown(cid, reader, access="owner"):
    return [m["content"] for m in chat._visible_messages(cid, reader, access)
            if m["role"] == "assistant"][0]


def test_the_owner_sees_their_answer_again(_source):
    assert _source.full_names
    shown = _shown(_answer(ADMIN, author="admin"), ADMIN)
    assert not shown.get("redacted")
    assert shown["rows"] == [["toys", 120]] and shown["sql"] == SQL


def test_a_role_granted_exactly_that_table_sees_it_and_others_do_not():
    cid = _answer(ANA)
    assert not _shown(cid, ANA).get("redacted")
    viewer = {"id": "u-fn-view", "email": "v@studio.test", "role": "viewer", "name": "V"}
    assert _shown(cid, viewer, "view")["redacted"] is True       # no grant on this source


@pytest.mark.parametrize("sql", [
    "SELECT category FROM retail_products",                     # a bare name
    "SELECT * FROM prod.retail_qa.retail_products",             # another catalog
    "SELECT * FROM system.access.audit",                        # never listed
    "SELECT * FROM studio_dev.information_schema.tables",       # never listed
])
def test_what_the_gateway_would_refuse_stays_hidden_even_under_star(sql):
    assert not pipelines.stored_step_allowed("admin", {"source": SOURCE, "sql": sql}, SOURCE)
    assert _shown(_answer(ADMIN, sql=sql, author="admin"), ADMIN)["redacted"] is True


def test_the_scope_follows_the_connector(_source):
    assert _source.full_name_in_scope(TABLE)
    assert not _source.full_name_in_scope("prod.retail_qa.retail_products")
    assert not _source.full_name_in_scope("retail_qa.retail_products")
    unpinned = connections._DynDatabricks("dbx", {
        "server_hostname": "h", "http_path": "/p", "access_token": "t", "catalog": "", "schema": "*"})
    assert unpinned.full_name_in_scope("prod.retail_qa.retail_products")   # any catalog…
    assert not unpinned.full_name_in_scope("system.access.audit")          # …but system
