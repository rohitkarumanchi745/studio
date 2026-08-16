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

from .base import Connector


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
        url = f"{cfg['uri'].rstrip('/')}/db/{cfg['database']}/tx/commit"
        body = json.dumps({"statements": [{"statement": cypher,
                                           "parameters": params or {}}]}).encode()
        auth = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json",
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def list_tables(self):
        """Node labels present as tables."""
        res = self._post("CALL db.labels() YIELD label RETURN label ORDER BY label")
        data = res.get("results", [{}])[0].get("data", [])
        return [row["row"][0] for row in data]

    def get_schema(self, table):
        """Property keys on a label, as columns (types unknown over HTTP)."""
        res = self._post(
            f"MATCH (n:`{table}`) WITH n LIMIT 1 RETURN keys(n) AS ks")
        data = res.get("results", [{}])[0].get("data", [])
        keys = data[0]["row"][0] if data else []
        return [{"name": k, "type": "property"} for k in keys]

    def run_query(self, cypher):
        """Execute Cypher; flatten rows to (columns, rows)."""
        res = self._post(cypher)
        result = res.get("results", [{}])[0]
        columns = result.get("columns", [])
        rows = [row["row"] for row in result.get("data", [])]
        return columns, rows
