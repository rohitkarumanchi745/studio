"""Connector registry.

Connectors are looked up here but never executed here: run_query on every
registered connector is reachable only through app/gateway.py (see base.py).
"""
from .bigquery_conn import BigQueryConnector
from .databricks_conn import DatabricksConnector
from .demo import DemoConnector
from .graph_conn import GraphConnector
from .marketing import MARKETING_CONNECTORS
# objectstore pulls the app DB + auth (its admin registry lives beside the
# connector) — keep auth.py free of module-level connector imports or this
# becomes a cycle.
from .objectstore import OBJECT_STORE_CONNECTORS
from .postgres_conn import PostgresConnector
from .powerbi_conn import PowerBIConnector
from .snowflake_conn import SnowflakeConnector

_REGISTRY = {
    "demo": DemoConnector(),
    "snowflake": SnowflakeConnector(),
    "postgres": PostgresConnector(),
    "databricks": DatabricksConnector(),
    "bigquery": BigQueryConnector(),
    "neo4j": GraphConnector(),
    "powerbi": PowerBIConnector(),
}
# One source per object store (s3 / azure_blob / gcs), all DuckDB-backed.
for _oc in OBJECT_STORE_CONNECTORS:
    _REGISTRY[_oc.name] = _oc
for _mc in MARKETING_CONNECTORS:
    _REGISTRY[_mc.name] = _mc


def _managed(names):
    """{name: connector} for the env-configured sources an admin has changed or
    disconnected on the Data connections screen (connections.managed_sources).
    That setting wins over the environment, for every caller: this registry is
    how the gateway, RBAC, the catalog and the agents all reach a source.
    Lazy import, like resolve() below: connections is built on these classes.
    A failure to read the setting leaves the environment's config in place —
    a read with no audit row returns nothing anyway."""
    try:
        from .. import connections
        return connections.managed_sources(names)
    except Exception:
        return {}


def get_connector(name):
    conn = _REGISTRY.get(name)
    if conn is not None:
        conn = _managed([name]).get(name, conn)
    if conn is None:
        # User-connected sources (added from the UI, stored encrypted) resolve
        # here so every existing chokepoint — gateway, RBAC, catalog — treats
        # them exactly like an env-configured source. Lazy import: connections
        # imports the connector classes above.
        try:
            from .. import connections
            conn = connections.resolve(name)
        except Exception:
            conn = None
    if conn is None:
        raise KeyError(f"Unknown source '{name}'")
    return conn


def all_sources():
    managed = _managed(list(_REGISTRY))
    out = [
        {"name": c.name, "dialect": c.dialect, "configured": c.configured()}
        for c in (managed.get(n, base) for n, base in _REGISTRY.items())
    ]
    try:
        from .. import connections
        out += connections.source_entries()
    except Exception:
        pass
    return out
