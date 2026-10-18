"""LanceDB as a source: its tables, minus their embeddings, as SQL.

LanceDB keeps vectors next to ordinary columns — ids, text, labels, sources,
dates — in Lance tables, on object storage (open-source LanceDB) or in
LanceDB Cloud / Enterprise. Questions about those columns ("how many chunks
per source?", "what was added this week?") are SQL questions; similarity
search is not, so:

- TABLES are the database's own (list_tables, paged). A column holding
  embeddings — a fixed-size list, or a list of them — is left out of SQL;
  every other column keeps its Arrow type, named as DuckDB names it.
- QUERIES run in DuckDB over the tables the SQL names (queryguard.base_tables),
  each read with only its SQL columns and kept LANCEDB_CACHE_SECONDS (300).
  DuckDB runs with external access off — it reads the Arrow it is handed and
  no file, URL or extension — and its configuration locked.
- SIZE: at most LANCEDB_MAX_ROWS (1,000,000) rows per table are read. A table
  with more is named in the agent's briefing with its full row count, so an
  answer over it is not taken for the whole table.
- WHERE: s3://, gs:// or az:// (open-source LanceDB; storage credentials as
  LanceDB storage options, JSON), or db://<database> with an API key (LanceDB
  Cloud / Enterprise). A path on Studio's own disk needs
  STUDIO_SQL_ALLOW_LOCAL_FILES=1, as a database file does for Any SQL database.

Driver: `pip install lancedb`. Without it the source reports unconfigured.
"""
import json
import logging
import os
import threading
import time

from .base import Connector, current_cancel, jsonify_rows

log = logging.getLogger("studio.connectors")

#: URI schemes LanceDB reads remotely; anything else is a local path.
REMOTE_SCHEMES = ("s3", "s3+ddb", "gs", "az", "abfss", "db")


def driver_available():
    try:
        import lancedb  # noqa: F401
        return True
    except Exception:
        return False


def is_embedding(t):
    """An Arrow type that holds vectors: fixed-size list, or a list of them."""
    import pyarrow as pa
    if pa.types.is_fixed_size_list(t):
        return True
    return (pa.types.is_list(t) or pa.types.is_large_list(t)) \
        and pa.types.is_fixed_size_list(t.value_type)


def _lockdown(con):
    """DuckDB reads only the Arrow registered on `con`: no files, URLs or
    extensions, a memory cap, then the configuration frozen (last)."""
    for stmt in ("SET enable_external_access=false",
                 "SET autoinstall_known_extensions=false",
                 "SET autoload_known_extensions=false",
                 "SET allow_community_extensions=false",
                 "SET memory_limit='%s'" % os.getenv("STUDIO_DUCKDB_MEMORY", "2GB")
                 .replace("'", "''"),
                 "SET lock_configuration=true"):
        con.execute(stmt)


class LanceDBConnector(Connector):
    name = "lancedb"
    dialect = "duckdb"

    def __init__(self, name="lancedb", cfg=None):
        self.name = name
        self._dyn = dict(cfg or {})
        self._db_ = None
        self._names = None             # (time, [table names]) for run_query
        self._cache = {}               # table -> (time, arrow)
        self._partial = {}             # table -> (rows read, rows in the table)
        self._lock = threading.Lock()

    # ── Configuration ────────────────────────────────────────────────────

    def _cfg(self):
        base = {"uri": "", "api_key": "", "region": "us-east-1", "host_override": "",
                "storage_options": "", "max_rows": os.getenv("LANCEDB_MAX_ROWS", "1000000")}
        base.update({k: v for k, v in self._dyn.items() if v not in (None, "")})
        base["uri"] = str(base["uri"]).strip()
        return base

    def _cloud(self):
        return self._cfg()["uri"].lower().startswith("db://")

    def _local(self):
        uri = self._cfg()["uri"]
        return "://" not in uri or uri.lower().startswith("file://")

    def configured(self):
        from .sql_conn import local_files_allowed
        cfg = self._cfg()
        return bool(cfg["uri"]) and (not self._cloud() or bool(cfg["api_key"])) \
            and (not self._local() or local_files_allowed()) and driver_available()

    def _cap(self):
        try:
            return max(1, int(self._cfg()["max_rows"]))
        except ValueError:
            return 1000000

    def _db(self):
        with self._lock:
            if self._db_ is None:
                import lancedb
                cfg = self._cfg()
                kw = {}
                if self._cloud():
                    kw = {"api_key": cfg["api_key"], "region": cfg["region"] or "us-east-1"}
                    if cfg["host_override"]:
                        kw["host_override"] = cfg["host_override"]
                elif cfg["storage_options"]:
                    kw["storage_options"] = json.loads(cfg["storage_options"])
                self._db_ = lancedb.connect(cfg["uri"], **kw)
            return self._db_

    def close(self):
        with self._lock:
            self._db_, self._names = None, None
            self._cache.clear()

    # ── Metadata ─────────────────────────────────────────────────────────

    def list_tables(self):
        db, names, token = self._db(), [], None
        while True:
            page = db.list_tables(page_token=token, limit=1000)
            names += list(page.tables)
            if not page.page_token or page.page_token == token or not page.tables:
                break
            token = page.page_token
        return sorted(set(names))

    @staticmethod
    def _columns(schema):
        """[{"name", "type"}] for a schema's SQL columns, typed as DuckDB
        types them (BIGINT, VARCHAR, TIMESTAMP…)."""
        import duckdb
        import pyarrow as pa
        keep = pa.schema([f for f in schema if not is_embedding(f.type)])
        if not len(keep):
            return []
        con = duckdb.connect(":memory:")
        try:
            con.register("t", keep.empty_table())
            return [{"name": r[0], "type": r[1]} for r in con.execute("DESCRIBE t").fetchall()]
        finally:
            con.close()

    def get_schema(self, table):
        try:
            tbl = self._db().open_table(table)
        except Exception as e:
            raise ValueError(f"Unknown table {table}") from e
        return self._columns(tbl.schema)

    def describe_relation(self, name):
        try:
            schema = self._db().open_table(name).schema
        except Exception:
            return None
        return {"kind": "table", "namespace": "", "columns": self._columns(schema)}

    def describe_relations(self):
        out = {}
        for name in self.list_tables():
            rel = self.describe_relation(name)
            if rel is not None:
                out[name] = rel
        return out

    def query_guidance(self):
        lines = ["Write DuckDB SQL over the LanceDB tables. Quote a table name with capitals "
                 "or a hyphen: \"my-table\".",
                 "Embedding (vector) columns are left out of SQL; answer from the other "
                 "columns. Similarity search is not available through SQL."]
        for table, (read, total) in sorted(self._partial.items()):
            lines.append(f"Only the first {read:,} of {total:,} rows of {table} are read "
                         "(LANCEDB_MAX_ROWS): totals over it cover those rows, not the table.")
        return lines

    # ── Queries ──────────────────────────────────────────────────────────

    def _ttl(self):
        return int(os.getenv("LANCEDB_CACHE_SECONDS", "300"))

    def _known(self):
        """{lowercase name: name} — SQL names are case-insensitive in DuckDB,
        LanceDB's are not."""
        with self._lock:
            hit = self._names
        if not hit or time.time() - hit[0] >= self._ttl():
            hit = (time.time(), self.list_tables())
            with self._lock:
                self._names = hit
        out = {}
        for name in hit[1]:
            out.setdefault(name.lower(), name)
        return out

    def _load(self, table):
        """The table's SQL columns as Arrow, at most _cap() rows, cached."""
        with self._lock:
            hit = self._cache.get(table)
        if hit and time.time() - hit[0] < self._ttl():
            return hit[1]
        tbl = self._db().open_table(table)
        cols = [f.name for f in tbl.schema if not is_embedding(f.type)]
        total = tbl.count_rows()
        arrow = tbl.search().select(cols).limit(self._cap()).to_arrow()
        with self._lock:
            self._cache[table] = (time.time(), arrow)
            if total > arrow.num_rows:
                self._partial[table] = (arrow.num_rows, total)
                log.warning("lancedb %s: read %d of %d rows of %s", self.name,
                            arrow.num_rows, total, table)
            else:
                self._partial.pop(table, None)
        return arrow

    def run_query(self, sql):
        import duckdb

        from ..queryguard import base_tables
        known = self._known()
        wanted = [known[t] for t in sorted(base_tables(sql)) if t in known]
        con = duckdb.connect(":memory:")
        try:
            for table in wanted:
                con.register(table, self._load(table))
            _lockdown(con)
            token = current_cancel()
            if token is not None:
                token.attach(con.interrupt)
            res = con.execute(sql)
            columns = [d[0] for d in res.description]
            rows = jsonify_rows(res.fetchmany(int(os.getenv("STUDIO_MAX_ROWS", "50000"))))
            return columns, rows
        finally:
            con.close()
