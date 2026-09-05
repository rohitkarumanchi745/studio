"""PostgreSQL data-source connector — a second SQL warehouse next to demo.

Configure with POSTGRES_DSN (e.g. postgresql://user:pass@host:5432/db) and
optionally POSTGRES_SCHEMA (default "public"). Dormant until the DSN is set.
Reads through the same gate as every source (queryguard → RBAC → governance);
run_script exists only for the supervised, human-approved write path.
"""
import os
import re
import threading
from urllib.parse import urlsplit

from .base import Connector, jsonify_rows

# A schema name that needs no escaping inside libpq's `options` string. A
# configured schema outside this shape is refused rather than pasted in and
# hoped for: search_path is a security control here, not a convenience.
_PLAIN_SCHEMA = re.compile(r"[A-Za-z0-9_$]+")


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
        schema the login can also see) is refused by the query guard.

        ARITY carries the meaning, and PostgreSQL's rule is:
          one part  `x.sales`      -> x is a SCHEMA in the current database
          two parts `a.b.sales`    -> a is the DATABASE (only ever the connected
                                      one; PostgreSQL cannot cross databases in
                                      a query) and b the SCHEMA
        So the schema is declared at one part and `database.schema` at two, and
        neither spelling is accepted at the other arity — a database name in the
        one-part position would name a schema the catalog never described.
        CASE is the stored spelling, verbatim — not lower-cased. This
        connector already treats POSTGRES_SCHEMA as the EXACT name of the
        schema: list_tables() matches information_schema.table_schema against
        it as written, and _search_path_option() DOUBLE-QUOTES it so the server
        does not fold it. Declaring a lower-cased copy contradicted both, and
        the contradiction was a hole: with POSTGRES_SCHEMA=Analytics the guard
        admitted `analytics.sales`, which PostgreSQL resolves to a different
        schema than the one the catalog and the allowlist were built from.
        Env only, no connection: this runs on every query.
        """
        schema = self._schema()
        out = {schema}
        database = self._database().strip()
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

    def _search_path_option(self):
        """The libpq `options` string that PINS search_path to the configured
        schema.

        Invariant: an UNQUALIFIED reference can only ever mean the CONFIGURED
        schema — the one list_tables() built the catalog from and qualifiers()
        reports. Without this the server's default search_path applies, so an
        allowlisted bare name (`sales`) resolves to whichever schema comes
        FIRST on that path, which can be another schema the login happens to
        see. The query guard cannot catch that: a bare name carries no
        namespace to check, so the pin has to happen on the connection.

        Double-quoted so PostgreSQL does not case-fold the value: search_path
        items are read as identifiers, and an unquoted `Analytics` would silently
        become `analytics`. The schema is validated first (see _PLAIN_SCHEMA) —
        a name that would need escaping is a misconfiguration and fails closed.
        """
        schema = self._schema()
        if not _PLAIN_SCHEMA.fullmatch(schema):
            raise ValueError(
                f"POSTGRES_SCHEMA={schema!r} is not a plain identifier; "
                "search_path cannot be pinned safely")
        return f'-c search_path="{schema}"'

    def _conn(self):
        import psycopg
        # `options` as a keyword WINS over anything in the DSN, so a DSN that
        # carries its own search_path cannot loosen the pin.
        return psycopg.connect(self._dsn(), autocommit=True,
                               options=self._search_path_option())

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
