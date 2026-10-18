"""PostgreSQL data-source connector — a second SQL warehouse next to demo.

Configure with POSTGRES_DSN (e.g. postgresql://user:pass@host:5432/db) and
optionally POSTGRES_SCHEMA (default "public"). Dormant until the DSN is set.
Reads through the same gate as every source (queryguard → RBAC → governance);
run_script exists only for the supervised, human-approved write path.
"""
import logging
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

    def close(self):
        """Close the connector-owned session, if any.

        Dynamic connection probes are deliberately short-lived and saved
        connectors are evicted on delete/replacement.  Exposing the same close
        contract as Snowflake and Databricks lets both paths release the
        PostgreSQL session instead of leaving it open until process exit.
        """
        with self._pool_lock:
            if self._pool_conn is not None:
                try:
                    self._pool_conn.close()
                except Exception:
                    pass
                self._pool_conn = None

    def list_tables(self):
        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name", (self._schema(),))
                return [r[0] for r in cur.fetchall()]
        return self._execute(go)

    def list_namespaces(self):
        """Schemas this login can see in the connected database, for the
        connect screen's picker. PostgreSQL cannot cross databases in a query,
        so the database is always the connected one."""
        database = self._database().strip()

        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name NOT IN ('information_schema', 'pg_catalog', "
                    "'pg_toast') AND schema_name NOT LIKE 'pg\\_temp\\_%' "
                    "AND schema_name NOT LIKE 'pg\\_toast\\_temp\\_%' "
                    "ORDER BY schema_name")
                return [r[0] for r in cur.fetchall()]

        return [{"database": database, "schema": n} for n in self._execute(go)]

    def get_schema(self, table):
        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    (self._schema(), table))
                return [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
        return self._execute(go)

    @staticmethod
    def _comments(cur, schema, name=None):
        """({relation: description}, {(relation, column): description}) — the
        COMMENTs in the pinned schema (or on one relation), from pg_catalog.
        Read last and fail soft: a relation is described without them rather
        than not at all."""
        try:
            cur.execute(
                "SELECT c.relname::text, a.attname::text, d.description "
                "FROM pg_catalog.pg_description d "
                "JOIN pg_catalog.pg_class c ON c.oid = d.objoid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
                "AND a.attnum = d.objsubid "
                "WHERE d.classoid = 'pg_catalog.pg_class'::regclass AND n.nspname = %s"
                + (" AND c.relname = %s" if name is not None else ""),
                (schema,) if name is None else (schema, name))
            rows = cur.fetchall()
        except Exception:
            logging.getLogger("studio.connectors").warning(
                "Postgres comments could not be read", exc_info=True)
            return {}, {}
        tables, columns = {}, {}
        for rel, col, text in rows:
            if col is None:
                tables[rel] = text
            else:
                columns[(rel, col)] = text
        return tables, columns

    def describe_relations(self):
        """Kind, columns and descriptions (COMMENTs) for every relation in the
        pinned schema, in three catalog queries."""
        schema = self._schema()

        def go(con):
            with con.cursor() as cur:
                cur.execute("SELECT table_name, table_type FROM information_schema.tables "
                            "WHERE table_schema = %s", (schema,))
                out = {name: {"kind": "table" if ttype == "BASE TABLE"
                              else "view" if ttype == "VIEW" else "other",
                              "namespace": schema, "columns": []}
                       for name, ttype in cur.fetchall()}
                cur.execute("SELECT table_name, column_name, data_type "
                            "FROM information_schema.columns WHERE table_schema = %s "
                            "ORDER BY table_name, ordinal_position", (schema,))
                for name, col, ctype in cur.fetchall():
                    if name in out:
                        out[name]["columns"].append({"name": col, "type": ctype})
                tables, columns = self._comments(cur, schema)
                for name, entry in out.items():
                    if tables.get(name):
                        entry["description"] = tables[name]
                    for col in entry["columns"]:
                        if columns.get((name, col["name"])):
                            col["description"] = columns[(name, col["name"])]
                return out
        try:
            return self._execute(go)
        except Exception:
            return None

    def describe_relation(self, name):
        """Kind and columns of one relation in the pinned schema."""
        schema = self._schema()

        def go(con):
            with con.cursor() as cur:
                cur.execute("SELECT table_type FROM information_schema.tables "
                            "WHERE table_schema = %s AND table_name = %s", (schema, name))
                row = cur.fetchone()
                if row is None:
                    return None
                cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_schema = %s AND table_name = %s "
                            "ORDER BY ordinal_position", (schema, name))
                cols = [{"name": r[0], "type": r[1]} for r in cur.fetchall()]
                tables, columns = self._comments(cur, schema, name)
                for col in cols:
                    if columns.get((name, col["name"])):
                        col["description"] = columns[(name, col["name"])]
                out = {"kind": "table" if row[0] == "BASE TABLE"
                       else "view" if row[0] == "VIEW" else "other",
                       "namespace": schema, "columns": cols}
                if tables.get(name):
                    out["description"] = tables[name]
                return out
        return self._execute(go)

    def describe_constraints(self):
        """Primary and foreign keys in the pinned schema, from pg_constraint,
        with columns in key order. A foreign key into another schema names a
        table outside this source and is left out."""
        schema = self._schema()

        def cols(rel, keys):
            return ("ARRAY(SELECT a.attname::text FROM unnest(" + keys + ") WITH ORDINALITY "
                    "k(num, pos) JOIN pg_catalog.pg_attribute a ON a.attrelid = " + rel +
                    " AND a.attnum = k.num ORDER BY k.pos)")

        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    "SELECT con.contype::text, con.conname::text, rel.relname::text, "
                    + cols("con.conrelid", "con.conkey") + ", frel.relname::text, "
                    "fns.nspname::text, " + cols("con.confrelid", "con.confkey") + " "
                    "FROM pg_catalog.pg_constraint con "
                    "JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid "
                    "JOIN pg_catalog.pg_namespace ns ON ns.oid = rel.relnamespace "
                    "LEFT JOIN pg_catalog.pg_class frel ON frel.oid = con.confrelid "
                    "LEFT JOIN pg_catalog.pg_namespace fns ON fns.oid = frel.relnamespace "
                    "WHERE ns.nspname = %s AND con.contype IN ('p', 'f')", (schema,))
                out = {"primary_keys": {}, "foreign_keys": []}
                for ctype, name, table, columns, ftable, fschema, fcolumns in cur.fetchall():
                    if ctype == "p":
                        out["primary_keys"][table] = list(columns)
                    elif ftable and fschema == schema:
                        out["foreign_keys"].append({
                            "name": name, "from": table, "from_columns": list(columns),
                            "to": ftable, "to_columns": list(fcolumns)})
                return out
        try:
            return self._execute(go)
        except Exception:
            logging.getLogger("studio.connectors").warning(
                "Postgres describe_constraints failed", exc_info=True)
            return None

    def view_definitions(self):
        schema = self._schema()

        def go(con):
            with con.cursor() as cur:
                cur.execute("SELECT table_name, view_definition FROM information_schema.views "
                            "WHERE table_schema = %s", (schema,))
                return {name: sql for name, sql in cur.fetchall() if sql}
        try:
            return self._execute(go)
        except Exception:
            return None

    def table_signals(self):
        """{relation: md5 of its kind and its columns' names and types}, from
        pg_catalog in one query — over the same relations and attributes as
        change_signal(), so whatever moves that moves one of these. Views are
        included: list_tables() leaves them out, but their changes still
        matter to relationships and lineage."""
        schema = self._schema()

        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    # Kind, columns and every COMMENT: a changed description
                    # is a change, as the metadata graph's fingerprint has it.
                    "SELECT c.relname::text, md5(c.relkind::text || '|' "
                    "|| coalesce(obj_description(c.oid, 'pg_class'), '') || '|' "
                    "|| coalesce(string_agg("
                    "coalesce(a.attname::text, '') || ':' "
                    "|| coalesce(format_type(a.atttypid, a.atttypmod), '') || ':' "
                    "|| coalesce(col_description(c.oid, a.attnum), ''), ',' "
                    "ORDER BY a.attnum), '')) "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
                    "AND a.attnum > 0 AND NOT a.attisdropped "
                    "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
                    "GROUP BY c.oid, c.relname, c.relkind", (schema,))
                return {name: token for name, token in cur.fetchall()}
        try:
            return self._execute(go)
        except Exception:
            logging.getLogger("studio.connectors").warning(
                "Postgres table_signals failed; falling back to the source signal",
                exc_info=True)
            return None

    def change_signal(self):
        """A hash of every relation in the pinned schema with its kind and its
        columns' names and types, from pg_catalog in one query — any DDL that
        changes what discovery would read moves it."""
        schema = self._schema()

        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    # relkind is the internal "char" type: text || "char" is
                    # ambiguous, so every part is cast to text explicitly.
                    "SELECT md5(coalesce(string_agg(c.relname::text || ':' "
                    "|| c.relkind::text || ':' || coalesce(a.attname::text, '') || ':' "
                    "|| coalesce(format_type(a.atttypid, a.atttypmod), ''), ',' "
                    "ORDER BY c.relname, a.attnum), '')) "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                    "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
                    "AND a.attnum > 0 AND NOT a.attisdropped "
                    "WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f')",
                    (schema,))
                return cur.fetchone()[0]
        try:
            return self._execute(go)
        except Exception:
            # No signal means "rediscover on the full-sync cadence" — say why,
            # or a broken query would look like an engine without a signal.
            logging.getLogger("studio.connectors").warning(
                "Postgres change_signal failed; falling back to the full-sync cadence",
                exc_info=True)
            return None

    def relation_kind(self, namespace, table):
        """Inspect pg_catalog without selecting any output-table data."""
        if not isinstance(namespace, str) or not isinstance(table, str) \
                or not namespace or not table or len(namespace) > 128 \
                or len(table) > 128 or "\x00" in namespace or "\x00" in table:
            raise ValueError("Invalid relation identity")

        def go(con):
            with con.cursor() as cur:
                cur.execute(
                    "SELECT c.relkind FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relname=%s LIMIT 1",
                    (namespace, table),
                )
                row = cur.fetchone()
                if row is None:
                    return "missing"
                return "table" if row[0] in ("r", "p") else "other"
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
