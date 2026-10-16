"""A source whose schema is empty is not a permission problem.

An admin pointed `databricks` at a catalog/schema with no tables; every
question then answered "Your role has no access to this source", which sent
them looking at roles. Locked in here:
- a role that may use a source but finds no tables on it is told the source is
  EMPTY (and where to fix it) — by the chat, and by the gateway every other
  read goes through; a role filtered down to nothing still hears "no access";
- a warehouse whose schema shows no tables cannot be saved: the probe refuses
  it, marking the schema field and naming the default catalog when none was set.
"""
import pytest
from fastapi import HTTPException

from app import chat, connections, gateway, rbac
from app.queryguard import QueryRejected

ADMIN = {"id": "u-empty-admin", "email": "empty-admin@studio.test", "role": "admin"}


class _Empty:
    name = "databricks"
    dialect = "databricks"

    def __init__(self, tables=()):
        self.tables = list(tables)

    def configured(self):
        return True

    def list_tables(self):
        return self.tables


def test_the_words_say_empty_or_no_access():
    assert "has no tables to read" in rbac.nothing_to_read("databricks", [])
    assert "Data connections" in rbac.nothing_to_read("databricks", [])
    assert rbac.nothing_to_read("databricks", ["orders"]) == \
        "Your role has no access to this source"


def test_the_gateway_says_an_empty_source_is_empty(monkeypatch):
    monkeypatch.setattr(gateway.sources, "connector_or_400", lambda source: _Empty())
    with pytest.raises(QueryRejected) as e:
        gateway.scope(ADMIN, "databricks")
    assert "databricks has no tables to read" in str(e.value)


def test_the_chat_says_an_empty_source_is_empty(monkeypatch):
    monkeypatch.setattr(chat, "connector_or_400", lambda source: _Empty())
    with pytest.raises(HTTPException) as e:
        chat._scope(chat.Ask(prompt="revenue by region", source="databricks", table="*"), ADMIN)
    assert e.value.status_code == 403 and "has no tables to read" in e.value.detail


def test_a_role_filtered_to_nothing_still_hears_no_access(monkeypatch):
    monkeypatch.setattr(gateway.sources, "connector_or_400", lambda source: _Empty(["orders"]))
    monkeypatch.setattr(rbac, "allowed_tables", lambda role, source, tables: [])
    with pytest.raises(QueryRejected) as e:
        gateway.scope(ADMIN, "databricks")
    assert str(e.value) == "Your role has no access to this source"


@pytest.mark.parametrize("cfg,mentions_catalog", [
    ({"catalog": "", "schema": "default"}, True),
    ({"catalog": "main", "schema": "default"}, False),
])
def test_an_empty_warehouse_schema_cannot_be_saved(monkeypatch, cfg, mentions_catalog):
    monkeypatch.setitem(connections.TYPES["databricks"], "build", lambda name, c: _Empty())
    monkeypatch.setattr(connections, "_resolves", lambda host, wait=5.0: True)
    out = connections._probe("databricks", {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", **cfg})
    assert out["ok"] is False and list(out["fields"]) == ["schema"] and out["signed_in"] is True
    assert out["error"].startswith("Signed in, but this schema has no tables")
    assert "Sign in and list schemas" in out["error"]
    assert ("no catalog is set" in out["error"]) is mentions_catalog


def test_a_schema_with_tables_still_saves_and_other_types_may_be_empty(monkeypatch):
    monkeypatch.setitem(connections.TYPES["databricks"], "build",
                        lambda name, c: _Empty(["orders"]))
    monkeypatch.setattr(connections, "_resolves", lambda host, wait=5.0: True)
    out = connections._probe("databricks", {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", "catalog": "main", "schema": "sales"})
    assert out == {"ok": True, "tables": 1, "sample": ["orders"]}
    # A graph or a Power BI model has no schema to pick: no such rule.
    assert not connections.TYPES["neo4j"].get("needs_tables")
    assert not connections.TYPES["powerbi"].get("needs_tables")
    assert all(connections.TYPES[t]["needs_tables"]
               for t in ("postgres", "snowflake", "databricks", "bigquery"))
