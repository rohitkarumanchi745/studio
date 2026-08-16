"""Connector registry."""
from .databricks_conn import DatabricksConnector
from .demo import DemoConnector
from .graph_conn import GraphConnector
from .marketing import MARKETING_CONNECTORS
from .snowflake_conn import SnowflakeConnector

_REGISTRY = {
    "demo": DemoConnector(),
    "snowflake": SnowflakeConnector(),
    "databricks": DatabricksConnector(),
    "neo4j": GraphConnector(),
}
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
