"""Graph database connector (Neo4j / Cypher).

Lets agents run on a graph environment alongside the SQL warehouses. Node
labels present as "tables" and their properties as the schema, so the catalog
and RBAC treat it uniformly; queries are Cypher over the HTTP transaction
endpoint. Configured via env (NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD);
absent, it reports not-configured and is left out of the source list.
"""
import base64
import json
import os
import urllib.request
from urllib.parse import urlsplit

from .base import Connector


class GraphQueryError(RuntimeError):
    """Neo4j refused a statement. Its HTTP API answers 200 with the error in
    the body, so without this a failed query read as an empty result."""


def http_uri_problem(uri):
    """Why `uri` cannot reach Neo4j's HTTP API (this connector's transport),
    or None."""
    scheme = urlsplit((uri or "").strip()).scheme.lower()
    if scheme in ("http", "https"):
        return None
    return ("Studio talks to Neo4j over its HTTP API — use https://host:7473 (or "
            f"http://host:7474), not {scheme or 'a bare host'}://")


def _label(name):
    """A label as a Cypher identifier: backtick-quoted, backticks doubled, so
    a label's own name can never close the quotes and write Cypher."""
    return "`" + str(name).replace("`", "``") + "`"


class GraphConnector(Connector):
    name = "neo4j"
    dialect = "cypher"

    def _cfg(self):
        return {
            "uri": os.getenv("NEO4J_URI", ""),       # e.g. https://host:7473
            "user": os.getenv("NEO4J_USER", ""),
            "password": os.getenv("NEO4J_PASSWORD", ""),
            "database": os.getenv("NEO4J_DATABASE", "neo4j"),
        }

    def configured(self):
        cfg = self._cfg()
        return bool(cfg["uri"] and cfg["user"] and cfg["password"])

    def _post(self, cypher, params=None):
        cfg = self._cfg()
        problem = http_uri_problem(cfg["uri"])
        if problem:
            raise GraphQueryError(problem)
        url = f"{cfg['uri'].rstrip('/')}/db/{cfg['database']}/tx/commit"
        body = json.dumps({"statements": [{"statement": cypher,
                                           "parameters": params or {}}]}).encode()
        auth = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())
        if not isinstance(res, dict):
            raise GraphQueryError("Neo4j returned an unexpected response")
        if res.get("errors"):
            first = res["errors"][0] or {}
            raise GraphQueryError(f"{first.get('code', 'Neo.Error')}: "
                                  f"{first.get('message', '')}"[:300])
        return res

    @staticmethod
    def _result(res):
        results = res.get("results") or []
        if not results:
            raise GraphQueryError("Neo4j returned no result for the statement")
        return results[0]

    def list_tables(self):
        """Node labels present as tables."""
        res = self._post("CALL db.labels() YIELD label RETURN label ORDER BY label")
        return [row["row"][0] for row in self._result(res).get("data", [])]

    def get_schema(self, table):
        """Property keys on a label, as columns (types unknown over HTTP)."""
        res = self._post(f"MATCH (n:{_label(table)}) WITH n LIMIT 1 RETURN keys(n) AS ks")
        data = self._result(res).get("data", [])
        keys = data[0]["row"][0] if data else []
        return [{"name": k, "type": "property"} for k in keys]

    def run_query(self, cypher):
        """Execute Cypher; flatten rows to (columns, rows)."""
        result = self._result(self._post(cypher))
        columns = result.get("columns", [])
        rows = [row["row"] for row in result.get("data", [])]
        return columns, rows
