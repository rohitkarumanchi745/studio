"""Snowflake connector. Configure via env (see .env.example); requires
`pip install snowflake-connector-python` (commented in requirements.txt).
"""
import os
import threading

from .base import Connector, jsonify_rows


class SnowflakeConnector(Connector):
    name = "snowflake"
    dialect = "snowflake"

    def __init__(self):
        self._pool_conn = None
        self._pool_lock = threading.Lock()

    def _cfg(self):
        return {
            "account": os.getenv("SNOWFLAKE_ACCOUNT", ""),
            "user": os.getenv("SNOWFLAKE_USER", ""),
            "password": os.getenv("SNOWFLAKE_PASSWORD", ""),
            "warehouse": os.getenv("SNOWFLAKE_WAREHOUSE", ""),
            "database": os.getenv("SNOWFLAKE_DATABASE", ""),
            "schema": os.getenv("SNOWFLAKE_SCHEMA", "PUBLIC"),
        }

    def qualifiers(self):
        """The configured schema and `database.schema` — the only namespace
        RBAC describes.

        A Snowflake login routinely sees several databases and schemas, so a
        reference qualified with anything else (`other_db.public.sales`) is
        outside what the catalog and the allowlist were built from and the
        query guard refuses it. Env only, no connection: this runs per query.

        ARITY carries the meaning, and Snowflake's rule is:
          one part  `x.sales`   -> x is a SCHEMA in the CURRENT database
          two parts `a.b.sales` -> a is the DATABASE, b the SCHEMA
        So the DATABASE name must NOT be declared as a one-part qualifier: it
        was, and `ANALYTICS.sales` was therefore accepted while Snowflake reads
        it as the schema ANALYTICS — a namespace the catalog never described
        and the allowlist never covered.

        UPPER-cased, because that is the name Snowflake STORES: the env value
        is an unquoted identifier, and Snowflake folds an unquoted identifier
        up. list_tables() already reads it that way (`cfg["schema"].upper()`),
        so the catalog, the allowlist and this declaration now agree on one
        spelling. The guard canonicalizes a written reference the same way, so
        a bare `public.sales` still matches PUBLIC while a quoted
        `"public".sales` — a schema that does not exist — does not.
        """
        cfg = self._cfg()
        schema = (cfg["schema"] or "PUBLIC").strip().upper()
        database = (cfg["database"] or "").strip().upper()
        out = {schema}
        if database:
            out.add(f"{database}.{schema}")
        return frozenset(out)

    def configured(self):
        cfg = self._cfg()
        if not (cfg["account"] and cfg["user"] and cfg["password"] and cfg["database"]):
            return False
        try:
            import snowflake.connector  # noqa: F401
            return True
        except ImportError:
            return False

    def _conn(self):
        import snowflake.connector
        return snowflake.connector.connect(**self._cfg())


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
        cfg = self._cfg()

        def go(con):
            cur = con.cursor()
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name",
                (cfg["schema"].upper(),),
            )
            return [r[0] for r in cur.fetchall()]
        return self._execute(go)

    def get_schema(self, table):
        cfg = self._cfg()

        def go(con):
            cur = con.cursor()
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                (cfg["schema"].upper(), table.upper()),
            )
            return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        return self._execute(go)

    def run_query(self, sql):
        def go(con):
            cur = con.cursor()
            cur.execute(sql)
            columns = [d[0] for d in cur.description]
            rows = jsonify_rows(cur.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000"))))
            return columns, rows
        return self._execute(go)

    def run_script(self, sql):
        """Execute a write/DDL statement. Reached only after supervisor +
        human approval (supervisor.py). Returns rowcount when available."""
        def go(con):
            cur = con.cursor()
            cur.execute(sql)
            return {"rowcount": cur.rowcount}
        return self._execute(go)
