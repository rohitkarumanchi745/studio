"""Databricks SQL warehouse connector. Configure via env (see .env.example);
requires `pip install databricks-sql-connector` (commented in requirements.txt).
"""
import os
import threading

from .base import Connector


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
            rows = [list(r) for r in cur.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000")))]
            return columns, rows
        return self._execute(go)
