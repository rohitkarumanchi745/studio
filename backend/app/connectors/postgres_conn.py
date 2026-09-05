"""PostgreSQL data-source connector — a second SQL warehouse next to demo.

Configure with POSTGRES_DSN (e.g. postgresql://user:pass@host:5432/db) and
optionally POSTGRES_SCHEMA (default "public"). Dormant until the DSN is set.
Reads through the same gate as every source (queryguard → RBAC → governance);
run_script exists only for the supervised, human-approved write path.
"""
import os
import threading
from urllib.parse import urlsplit

from .base import Connector, jsonify_rows


class PostgresConnector(Connector):
    name = "postgres"
    dialect = "postgres"

    def __init__(self):
        self._pool_conn = None
        self._pool_lock = threading.Lock()

    def _dsn(self):
        return os.getenv("POSTGRES_DSN", "").strip()

    def _schema(self):
        return os.getenv("POSTGRES_SCHEMA", "public").strip() or "public"

    def _database(self):
        """Database name out of the DSN — URI form (…/dbname) or key=value form
        (dbname=…). Best effort and never a connection: an unparseable DSN just
        means the `db.schema.table` spelling is not accepted."""
        dsn = self._dsn()
        if "://" in dsn:
            try:
                return urlsplit(dsn).path.lstrip("/").split("?")[0]
            except ValueError:
                return ""
        for part in dsn.split():
            if part.startswith("dbname="):
                return part[len("dbname="):].strip("'\"")
        return ""

    def qualifiers(self):
        """The configured schema, plus `database.schema` — the only namespaces
        this connector's credential is meant to reach. Anything else (another
        schema the login can also see) is refused by the query guard."""
        schema = self._schema().lower()
        out = {schema}
        database = self._database().strip().lower()
        if database:
            out.add(f"{database}.{schema}")
        return frozenset(out)

    def configured(self):
        if not self._dsn():
            return False
        try:
            import psycopg  # noqa: F401
            return True
        except ImportError:
            return False

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn(), autocommit=True)

    def _execute(self, fn):
        """Run fn(connection) on one pooled connection; reconnect once on a
        failure (dropped session). Serialized by a lock — same shape as the
        Snowflake connector; swap for a real pool under heavy concurrency."""
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

    def list_tables(self):
        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name", (self._schema(),))
                return [r[0] for r in cur.fetchall()]
        return self._execute(go)

    def get_schema(self, table):
        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    (self._schema(), table))
                return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        return self._execute(go)

    def run_query(self, sql):
        def go(con):
            with con.cursor() as cur:
                cur.execute(sql)
                columns = [d.name for d in cur.description]
                rows = jsonify_rows(cur.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000"))))
                return columns, rows
        return self._execute(go)

    def run_script(self, sql):
        """Write/DDL — reached only after supervisor + human approval."""
        def go(con):
            with con.cursor() as cur:
                cur.execute(sql)
                return {"rowcount": cur.rowcount}
        return self._execute(go)
