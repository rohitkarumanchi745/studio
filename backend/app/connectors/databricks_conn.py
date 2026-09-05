"""Databricks SQL warehouse connector. Configure via env (see .env.example);
requires `pip install databricks-sql-connector` (commented in requirements.txt).
"""
import os
import threading

from .base import Connector, jsonify_rows


class DatabricksConnector(Connector):
    name = "databricks"
    dialect = "databricks"

    def __init__(self):
        self._pool_conn = None
        self._pool_lock = threading.Lock()

    def _cfg(self):
        return {
            "server_hostname": os.getenv("DATABRICKS_SERVER_HOSTNAME", ""),
            "http_path": os.getenv("DATABRICKS_HTTP_PATH", ""),
            "access_token": os.getenv("DATABRICKS_TOKEN", ""),
            "catalog": os.getenv("DATABRICKS_CATALOG", ""),
            "schema": os.getenv("DATABRICKS_SCHEMA", "default"),
        }

    def qualifiers(self):
        """The configured catalog/schema — the only namespace RBAC describes.

        Unity Catalog gives a warehouse token visibility over many catalogs, so
        `other_catalog.default.sales` is outside what list_tables() (and hence
        the allowlist) was built from and the query guard refuses it. Env only,
        no connection: this runs per query.
        """
        cfg = self._cfg()
        schema = (cfg["schema"] or "default").strip().lower()
        catalog = (cfg["catalog"] or "").strip().lower()
        out = {schema}
        if catalog:
            out |= {catalog, f"{catalog}.{schema}"}
        return frozenset(out)

    def configured(self):
        cfg = self._cfg()
        if not (cfg["server_hostname"] and cfg["http_path"] and cfg["access_token"]):
            return False
        try:
            from databricks import sql  # noqa: F401
            return True
        except ImportError:
            return False

    def _conn(self):
        from databricks import sql
        cfg = self._cfg()
        kwargs = {
            "server_hostname": cfg["server_hostname"],
            "http_path": cfg["http_path"],
            "access_token": cfg["access_token"],
        }
        if cfg["catalog"]:
            kwargs["catalog"] = cfg["catalog"]
        if cfg["schema"]:
            kwargs["schema"] = cfg["schema"]
        return sql.connect(**kwargs)


    # ── Connection pool (single persistent connection, reconnect on error) ─

    def _execute(self, fn):
        """Run `fn(connection)` on the pooled connection; reconnect once on
        failure (expired/killed sessions). Serialized by a lock — good enough
        for a prototype; swap for a real pool under heavy concurrency."""
        with self._pool_lock:
            for attempt in (1, 2):
                if self._pool_conn is None:
                    self._pool_conn = self._conn()
                try:
                    return fn(self._pool_conn)
                except Exception:
                    try:
                        self._pool_conn.close()
                    except Exception:
                        pass
                    self._pool_conn = None
                    if attempt == 2:
                        raise

    def close(self):
        with self._pool_lock:
            if self._pool_conn is not None:
                try:
                    self._pool_conn.close()
                except Exception:
                    pass
                self._pool_conn = None

    def list_tables(self):
        def go(con):
            cur = con.cursor()
            cur.execute("SHOW TABLES")
            # SHOW TABLES → (database, tableName, isTemporary)
            return [r[1] for r in cur.fetchall()]
        return self._execute(go)

    def get_schema(self, table):
        def go(con):
            cur = con.cursor()
            cur.execute(f"DESCRIBE TABLE {table}")
            out = []
            for r in cur.fetchall():
                col, dtype = r[0], r[1]
                if not col or col.startswith("#"):
                    break  # partition/metadata section
                out.append({"name": col, "type": dtype})
            return out
        return self._execute(go)

    def run_query(self, sql_text):
        def go(con):
            cur = con.cursor()
            cur.execute(sql_text)
            columns = [d[0] for d in cur.description]
            # Databricks returns date/datetime/Decimal — coerce to JSON-safe so
            # results survive the tile cache and the API response.
            rows = jsonify_rows(cur.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000"))))
            return columns, rows
        return self._execute(go)

    def run_script(self, sql_text):
        """Execute a write/DDL statement (supervisor + human-approved only)."""
        def go(con):
            cur = con.cursor()
            cur.execute(sql_text)
            return {"rowcount": getattr(cur, "rowcount", None)}
        return self._execute(go)

    def submit_spark_job(self, config):
        """Submit a Databricks Jobs run (Spark). `config` is a Jobs 2.1
        run-submit body; returns {run_id}. Requires DATABRICKS_TOKEN + host."""
        import json
        import urllib.request

        cfg = self._cfg()
        host = cfg["server_hostname"]
        url = f"https://{host}/api/2.1/jobs/runs/submit"
        body = json.dumps(config).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {cfg['access_token']}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
