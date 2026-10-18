"""Apache Cassandra (and CQL-compatible ScyllaDB, Amazon Keyspaces) as a source.

One keyspace per source, read with CQL through the DataStax driver.

- METADATA from system_schema, the engine's own catalog: a keyspace's tables
  and views, their columns and types, and their primary keys (partition and
  clustering columns) — in a query or two, never a row.
- QUERIES are CQL SELECTs, through the same gateway guard as every source:
  read-only, single statement, only granted tables, only this keyspace. CQL
  folds unquoted identifiers to lower case and keeps quoted ones exact, which
  the guard is told (EngineDialect). There are no joins in CQL: the briefing
  says so, and an answer needing two tables is the orchestrator's job.
- ROWS are paged from the cluster and capped at STUDIO_MAX_ROWS; the gateway
  appends a LIMIT as for any source (CQL accepts LIMIT).

Driver: `pip install cassandra-driver`. Without it the source reports
unconfigured, like any connector whose library is missing.
"""
import logging
import os
import threading

from ..queryguard import QueryRejected
from .base import Connector, jsonify_rows
from .sql_conn import EngineDialect

log = logging.getLogger("studio.connectors")


def driver_available():
    try:
        import cassandra.cluster  # noqa: F401
        return True
    except Exception:
        return False


class CassandraConnector(Connector):
    name = "cassandra"
    #: The guard reads CQL like Postgres-style SQL: bare names fold down.
    dialect = EngineDialect("cql", "lower")

    def __init__(self, name="cassandra", cfg=None):
        self.name = name
        self._dyn = dict(cfg or {})
        self._cluster = None
        self._session = None
        self._lock = threading.Lock()

    # ── Configuration ────────────────────────────────────────────────────

    def _cfg(self):
        base = {"contact_points": os.getenv("CASSANDRA_CONTACT_POINTS", ""),
                "port": os.getenv("CASSANDRA_PORT", "9042"),
                "keyspace": os.getenv("CASSANDRA_KEYSPACE", ""),
                "username": os.getenv("CASSANDRA_USERNAME", ""),
                "password": os.getenv("CASSANDRA_PASSWORD", ""),
                "datacenter": os.getenv("CASSANDRA_DATACENTER", ""),
                "ssl": os.getenv("CASSANDRA_SSL", "")}
        base.update({k: v for k, v in self._dyn.items() if v not in (None, "")})
        return base

    def configured(self):
        cfg = self._cfg()
        return bool(cfg["contact_points"].strip() and cfg["keyspace"].strip()) \
            and driver_available()

    def _keyspace(self):
        return self._cfg()["keyspace"].strip()

    def _session_(self):
        with self._lock:
            if self._session is None:
                from cassandra.auth import PlainTextAuthProvider
                from cassandra.cluster import Cluster
                cfg = self._cfg()
                kw = {"contact_points": [h.strip() for h in cfg["contact_points"].split(",")
                                         if h.strip()],
                      "port": int(cfg["port"] or 9042),
                      "connect_timeout": 10}
                if cfg["username"]:
                    kw["auth_provider"] = PlainTextAuthProvider(cfg["username"], cfg["password"])
                if cfg["datacenter"]:
                    from cassandra.policies import DCAwareRoundRobinPolicy
                    kw["load_balancing_policy"] = DCAwareRoundRobinPolicy(
                        local_dc=cfg["datacenter"])
                if str(cfg["ssl"]).lower() in ("1", "true", "yes"):
                    import ssl
                    kw["ssl_context"] = ssl.create_default_context()
                self._cluster = Cluster(**kw)
                self._session = self._cluster.connect()
                self._session.default_timeout = 60
            return self._session

    def close(self):
        with self._lock:
            cluster, self._cluster, self._session = self._cluster, None, None
        if cluster is not None:
            try:
                cluster.shutdown()
            except Exception:
                pass

    def _rows(self, cql, params=()):
        return list(self._session_().execute(cql, params))

    # ── Metadata (system_schema) ─────────────────────────────────────────

    def list_tables(self):
        ks = self._keyspace()
        names = [r.table_name for r in self._rows(
            "SELECT table_name FROM system_schema.tables WHERE keyspace_name = %s", (ks,))]
        names += [r.view_name for r in self._rows(
            "SELECT view_name FROM system_schema.views WHERE keyspace_name = %s", (ks,))]
        return sorted(names)

    def _columns(self, table=None):
        ks = self._keyspace()
        if table is None:
            rows = self._rows("SELECT table_name, column_name, type, kind, position "
                              "FROM system_schema.columns WHERE keyspace_name = %s", (ks,))
        else:
            rows = self._rows("SELECT table_name, column_name, type, kind, position "
                              "FROM system_schema.columns WHERE keyspace_name = %s "
                              "AND table_name = %s", (ks, table))
        # Key columns first, in key order, then the rest by name — the order
        # CQL's own DESCRIBE shows.
        rank = {"partition_key": 0, "clustering": 1, "static": 2, "regular": 3}
        out = {}
        for r in sorted(rows, key=lambda r: (rank.get(r.kind, 4), r.position if r.position
                                             is not None and r.position >= 0 else 0,
                                             r.column_name)):
            out.setdefault(r.table_name, []).append(r)
        return out

    def get_schema(self, table):
        cols = self._columns(table).get(table)
        if cols is None:
            raise ValueError(f"Unknown table {table}")
        return [{"name": c.column_name, "type": c.type} for c in cols]

    def describe_relations(self):
        ks = self._keyspace()
        views = {r.view_name for r in self._rows(
            "SELECT view_name FROM system_schema.views WHERE keyspace_name = %s", (ks,))}
        out = {}
        for table, cols in self._columns().items():
            out[table] = {"kind": "view" if table in views else "table", "namespace": ks,
                          "columns": [{"name": c.column_name, "type": c.type} for c in cols]}
        try:
            for r in self._rows("SELECT table_name, comment FROM system_schema.tables "
                                "WHERE keyspace_name = %s", (ks,)):
                if r.comment and r.table_name in out:
                    out[r.table_name]["description"] = r.comment
        except Exception:
            pass
        return out

    def describe_constraints(self):
        """A table's primary key: its partition key then clustering columns."""
        keys = {}
        for table, cols in self._columns().items():
            pk = [c.column_name for c in cols if c.kind in ("partition_key", "clustering")]
            if pk:
                keys[table] = pk
        return {"primary_keys": keys, "foreign_keys": []}

    def qualifiers(self):
        """The keyspace: `keyspace.table` is this source; any other keyspace
        is refused by the guard."""
        ks = self._keyspace()
        return frozenset({ks}) if ks else frozenset()

    def query_guidance(self):
        return ["Write CQL (Cassandra Query Language), not SQL: one table per query — "
                "there are no JOINs, subqueries or GROUP BY on arbitrary columns.",
                "Filter on the partition key (and clustering columns, in order) where you "
                "can; filtering on other columns needs ALLOW FILTERING and may be slow.",
                "Unquoted identifiers fold to lowercase: quote any name with capitals."]

    # ── Queries ──────────────────────────────────────────────────────────

    def run_query(self, cql):
        from cassandra.query import SimpleStatement
        text = (cql or "").strip().rstrip(";")
        if not text.lower().startswith("select"):
            raise QueryRejected("Only SELECT queries are allowed")
        max_rows = int(os.getenv("STUDIO_MAX_ROWS", "50000"))
        session = self._session_()
        # Bare table names read this source's keyspace, whatever the session's.
        session.set_keyspace(self._keyspace())
        result = session.execute(SimpleStatement(text, fetch_size=min(5000, max_rows)))
        columns = list(result.column_names or [])
        rows = []
        for row in result:
            rows.append(list(row))
            if len(rows) >= max_rows:
                break
        return columns, jsonify_rows(rows)
