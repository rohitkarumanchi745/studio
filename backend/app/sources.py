"""Resolve a source name to a usable connector, or fail with the right HTTP status.

Moved out of catalog.py so the gateway (and anything else that only needs a
connector) does not import the catalog router and its dependencies.
catalog._connector_or_400 remains as an alias for existing importers.
"""
from fastapi import HTTPException

from .connectors import get_connector


def connector_or_400(source):
    """The connector for `source`: 404 when the name is unknown, 400 when it is
    known but has no credentials/config."""
    try:
        conn = get_connector(source)
    except KeyError:
        raise HTTPException(404, f"Unknown source '{source}'")
    if not conn.configured():
        raise HTTPException(
            400,
            f"Source '{source}' is not configured. Set its credentials in the "
            f"backend environment (see .env.example) and install its connector package.",
        )
    return conn
