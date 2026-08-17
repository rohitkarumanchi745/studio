"""Connector registry."""
from .bigquery_conn import BigQueryConnector
from .databricks_conn import DatabricksConnector
from .demo import DemoConnector
from .graph_conn import GraphConnector
from .marketing import MARKETING_CONNECTORS
# objectstore pulls the app DB + auth (its admin registry lives beside the
# connector) — keep auth.py free of module-level connector imports or this
# becomes a cycle.
from .objectstore import OBJECT_STORE_CONNECTORS
from .snowflake_conn import SnowflakeConnector

_REGISTRY = {
    "demo": DemoConnector(),
    "snowflake": SnowflakeConnector(),
    "databricks": DatabricksConnector(),
    "bigquery": BigQueryConnector(),
    "neo4j": GraphConnector(),
}
# One source per object store (s3 / azure_blob / gcs), all DuckDB-backed.
for _oc in OBJECT_STORE_CONNECTORS:
    _REGISTRY[_oc.name] = _oc
for _mc in MARKETING_CONNECTORS:
    _REGISTRY[_mc.name] = _mc


def get_connector(name):
    conn = _REGISTRY.get(name)
    if conn is None:
        raise KeyError(f"Unknown source '{name}'")
    return conn


def all_sources():
    return [
        {"name": c.name, "dialect": c.dialect, "configured": c.configured()}
        for c in _REGISTRY.values()
    ]
