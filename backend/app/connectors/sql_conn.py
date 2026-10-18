"""Any SQL database, through one connector: SQLAlchemy and an installed driver.

Studio's other connectors each speak one vendor. This one speaks to whatever a
SQLAlchemy URL names — `mysql+pymysql://`, `mssql+pymssql://`,
`oracle+oracledb://`, `postgresql+psycopg://`, or any dialect a driver package
registers (Trino, ClickHouse, Redshift, DB2, Teradata, …). Nothing here knows a
vendor. Everything vendor-shaped is LEARNED, from SQLAlchemy or from the engine
itself:

- METADATA comes from SQLAlchemy's inspector — tables, views, columns, keys,
  view definitions, schemas — which every dialect implements. Names are shown
  as the engine STORES them (the dialect's denormalize_name), so a model writes
  identifiers the engine will resolve.
- IDENTIFIER CASE is asked of the engine, once per connection: `SELECT 1 AS
  Abc` comes back as `abc` (bare names fold down), `ABC` (fold up) or `Abc`
  (kept as written). The query guard enforces that rule for this connection
  (EngineDialect below); if the engine cannot be asked, the guard accepts only
  names it cannot misread — quoted ones — rather than guess.
- THE ROW CAP is placed the engine's way: where the dialect compiles a limit as
  LIMIT, the gateway appends one as for every source; where it does not (TOP,
  FETCH FIRST) the guard leaves the text alone and the rows are capped as they
  are fetched, from a streaming cursor.
- THE NAMESPACE. Bare names resolve against the login's default schema, so a
  source pinned to that schema lists bare names; one pinned to any OTHER schema
  lists `schema.table` and every reference must be written in full — a bare
  name would otherwise reach the default schema's table of the same name.

Read-only on every path: a query runs inside a transaction that is never
committed (closing the connection rolls it back), after the gateway's guard has
accepted it as a single SELECT. Use a read-only database login all the same.

A URL with no host — a database FILE on Studio's own server (SQLite, DuckDB) —
is refused unless STUDIO_SQL_ALLOW_LOCAL_FILES=1: a file path would let a
connection read Studio's own state.
"""
import hashlib
import logging
import os
import threading
import time

from ..queryguard import FULL_NAMES
from .base import Connector, jsonify_rows

log = logging.getLogger("studio.connectors")

#: How the engine reads a BARE identifier, as learned from it.
RULES = ("lower", "upper", "exact", "quoted_only")

#: The question that teaches the rule: the engine's own spelling of a bare
#: mixed-case label. Compiled per dialect (Oracle gets its FROM DUAL).
PROBE = None


def limit_syntax(dialect):
    """Does this dialect spell a row limit as LIMIT? Asked of the dialect's
    own compiler — TOP and FETCH FIRST engines answer no."""
    from sqlalchemy import literal_column, select
    return "LIMIT" in str(select(literal_column("1")).limit(5).compile(
        dialect=dialect, compile_kwargs={"literal_binds": True})).upper()


def _probe_sql(dialect):
    if PROBE is not None:
        return PROBE
    from sqlalchemy import literal_column, select
    return str(select(literal_column("1 AS Abc")).compile(dialect=dialect))


class EngineDialect(str):
    """The engine's name — what the model is told to write SQL for — carrying
    the identifier rule this connection's engine was found to follow, which the
    query guard reads (queryguard._canon). A plain string everywhere else."""

    def __new__(cls, name, identifier_rule):
        out = super().__new__(cls, name)
        out.identifier_rule = identifier_rule
        return out


def local_files_allowed():
    return os.getenv("STUDIO_SQL_ALLOW_LOCAL_FILES", "").lower() in ("1", "true", "yes")


def installed_drivers():
    """Every `dialect+driver` this process can open, best first per dialect —
    SQLAlchemy's own dialects and any a driver package registers."""
    from importlib.metadata import entry_points

    from sqlalchemy.dialects import registry
    names = {"postgresql", "mysql", "mariadb", "mssql", "oracle", "sqlite"}
    names |= {ep.name for ep in entry_points(group="sqlalchemy.dialects")}
    out = []
    for name in sorted(names):
        for driver in _drivers_of(name):
            spec = name if driver is None else f"{name}.{driver}"
            try:
                cls = registry.load(spec)
                if getattr(cls, "is_async", False):
                    continue                    # an asyncio driver: not for this engine
                cls.import_dbapi()
            except Exception:
                continue
            listed = f"{name}+{cls.driver}"
            if listed not in out:
                out.append(listed)
    return out


def _drivers_of(name):
    """The drivers a SQLAlchemy dialect module declares, default first."""
    try:
        import importlib
        mod = importlib.import_module(f"sqlalchemy.dialects.{name.split('.')[0]}")
    except Exception:
        return [None]
    drivers = [None]
    for attr in dir(mod):
        sub = getattr(mod, attr, None)
        driver = getattr(getattr(sub, "dialect", None), "driver", None)
        if isinstance(driver, str) and driver not in drivers:
            drivers.append(driver)
    return drivers


def check_url(raw):
    """(url, problem): the parsed SQLAlchemy URL, or why it cannot be used —
    unparseable, no driver installed for it, or a local file."""
    from sqlalchemy.engine import make_url
    try:
        url = make_url((raw or "").strip())
    except Exception:
        return None, ("That is not a connection URL — it looks like "
                      "dialect+driver://user:password@host:port/database")
    try:
        url.get_dialect().import_dbapi()
    except Exception:
        have = ", ".join(installed_drivers()) or "none"
        return None, (f"No driver for '{url.drivername}' is installed here. "
                      f"Installed: {have}.")
    if not url.host and not local_files_allowed():
        return None, ("The URL names no host. A database file on Studio's own server "
                      "is not allowed (set STUDIO_SQL_ALLOW_LOCAL_FILES=1 to permit it).")
    return url, None


# What was learned about each engine, per connection URL (cached across the
# connector instances a URL is built into; the URL never leaves this dict).
_LEARNED = {}
_LEARNED_LOCK = threading.Lock()
RETRY_SECONDS = 60


class SqlConnector(Connector):
    name = "sql"

    def __init__(self, name="sql", url="", schema=""):
        self.name = name
        self._url_text = (url or "").strip()
        self._schema_cfg = (schema or "").strip()
        self._engine = None
        self._lock = threading.Lock()

    # ── Configuration ────────────────────────────────────────────────────

    def configured(self):
        if not self._url_text:
            return False
        url, problem = check_url(self._url_text)
        return problem is None

    def _key(self):
        return hashlib.sha256(self._url_text.encode()).hexdigest()

    def engine(self):
        with self._lock:
            if self._engine is None:
                from sqlalchemy import create_engine
                url, problem = check_url(self._url_text)
                if problem:
                    raise RuntimeError(problem)
                try:
                    self._engine = create_engine(url, pool_pre_ping=True, pool_size=2,
                                                 max_overflow=2, pool_recycle=1800)
                except TypeError:            # a pool class without those settings
                    self._engine = create_engine(url, pool_pre_ping=True)
            return self._engine

    def close(self):
        with self._lock:
            engine, self._engine = self._engine, None
        if engine is not None:
            engine.dispose()

    def _learned(self):
        """{default_schema, rule, limit}: what the engine told us, asked once.
        An engine that could not be asked is asked again only after
        RETRY_SECONDS — meanwhile the fail-closed answer stands, so an engine
        that is down costs one connection attempt a minute, not one per use."""
        key = self._key()
        with _LEARNED_LOCK:
            hit = _LEARNED.get(key)
        if hit is not None and (not hit.get("failed_at")
                                or time.time() - hit["failed_at"] < RETRY_SECONDS):
            return hit
        from sqlalchemy import inspect
        engine = self.engine()
        dialect = engine.dialect
        limit = limit_syntax(dialect)
        rule, default_schema = "quoted_only", None
        probe = _probe_sql(dialect)
        try:
            with engine.connect() as c:
                # Through the driver, so the label is the engine's own spelling.
                result = c.execution_options(no_parameters=True).exec_driver_sql(probe)
                label = list(result.keys())[0]
                result.close()
                rule = {"abc": "lower", "ABC": "upper", "Abc": "exact"}.get(label, "quoted_only")
                default_schema = inspect(c).default_schema_name
                c.rollback()
        except Exception:
            log.warning("sql connector %s: could not learn identifier case", self.name,
                        exc_info=True)
            out = {"default_schema": None, "rule": "quoted_only", "limit": limit,
                   "failed_at": time.time()}
            with _LEARNED_LOCK:
                _LEARNED[key] = out
            return out
        out = {"default_schema": default_schema, "rule": rule, "limit": limit}
        with _LEARNED_LOCK:
            _LEARNED[key] = out
        return out

    @property
    def dialect(self):
        try:
            name = self.engine().dialect.name
            rule = self._learned()["rule"]
        except Exception:
            return EngineDialect("sql", "quoted_only")
        return EngineDialect(name, rule)

    @property
    def sql_row_limit(self):
        """Whether the gateway may append LIMIT (the engine's limit syntax)."""
        try:
            return self._learned()["limit"]
        except Exception:
            return False

    @property
    def full_names(self):
        try:
            return not self._schema()[1]
        except Exception:
            return False

    def query_guidance(self):
        """What the model should know about this engine, from what was learned
        of it — never from a list of vendors."""
        try:
            name, learned = self.engine().dialect.name, self._learned()
        except Exception:
            return ["Write standard SQL. Quote every table name exactly as listed."]
        out = [f"Write SQL for {name}, in its own dialect (functions, date math, quoting)."]
        out.append({
            "lower": "Unquoted identifiers fold to lowercase: quote any name that has capitals.",
            "upper": "Unquoted identifiers fold to UPPERCASE: quote any name that is not all "
                     "capitals.",
            "exact": "Write every identifier exactly as listed (same case).",
        }.get(learned["rule"], "Quote every table name exactly as listed — an unquoted name "
                               "with capitals is refused."))
        if not learned["limit"]:
            out.append(f"{name} does not use LIMIT; rows are capped by the platform, so add a "
                       f"row limit only when the question asks for a top N, in {name}'s own "
                       f"syntax.")
        return out

    def _schema(self):
        """(the schema this source reads — as the inspector names it, or None
        for the default — and whether its tables are listed bare)."""
        learned = self._learned()
        default = learned["default_schema"]
        if not self._schema_cfg:
            return None, True
        dialect = self.engine().dialect
        wanted = dialect.normalize_name(self._schema_cfg) if dialect.requires_name_normalize \
            else self._schema_cfg
        if default is not None and wanted == default:
            return None, True
        return wanted, False

    # ── Names: stored spellings out, inspector spellings in ─────────────

    def _out(self, name):
        dialect = self.engine().dialect
        return dialect.denormalize_name(name) if dialect.requires_name_normalize else name

    def _in(self, name):
        dialect = self.engine().dialect
        return dialect.normalize_name(name) if dialect.requires_name_normalize else name

    def _listed(self, name):
        schema, bare = self._schema()
        return self._out(name) if bare else f"{self._out(schema)}.{self._out(name)}"

    def _unlisted(self, listed):
        """The inspector's name for a listed one."""
        schema, bare = self._schema()
        text = str(listed)
        if not bare:
            prefix = f"{self._out(schema)}."
            if not text.startswith(prefix):
                raise ValueError(f"'{listed}' is not in this source's schema")
            text = text[len(prefix):]
        return self._in(text)

    def _relations(self, inspector):
        """{inspector name: kind} for the tables and views in scope."""
        schema, _bare = self._schema()
        out = {n: "table" for n in inspector.get_table_names(schema=schema)}
        for method in ("get_view_names", "get_materialized_view_names"):
            fn = getattr(inspector, method, None)
            if fn is None:
                continue
            try:
                out.update({n: "view" for n in fn(schema=schema)})
            except NotImplementedError:
                pass
        return out

    def _inspect(self, fn):
        from sqlalchemy import inspect
        with self.engine().connect() as c:
            try:
                return fn(inspect(c))
            finally:
                c.rollback()

    @staticmethod
    def _type(col):
        try:
            return str(col["type"])
        except Exception:
            return type(col["type"]).__name__

    # ── The Connector contract ───────────────────────────────────────────

    def list_tables(self):
        return self._inspect(lambda i: sorted(self._listed(n) for n in self._relations(i)))

    def get_schema(self, table):
        schema, _bare = self._schema()
        name = self._unlisted(table)
        return self._inspect(lambda i: [{"name": self._out(c["name"]), "type": self._type(c)}
                                        for c in i.get_columns(name, schema=schema)])

    def describe_relations(self):
        """Kinds and columns of everything in scope, in the inspector's bulk
        read where the dialect has one."""
        schema, _bare = self._schema()

        def go(i):
            kinds = self._relations(i)
            from sqlalchemy.engine.reflection import ObjectKind
            try:
                cols = {key[1]: v for key, v in
                        i.get_multi_columns(schema=schema, kind=ObjectKind.ANY).items()}
            except Exception:
                cols = {n: i.get_columns(n, schema=schema) for n in kinds}
            for n in kinds:
                if n not in cols:
                    try:
                        cols[n] = i.get_columns(n, schema=schema)
                    except Exception:
                        continue
            ns = self._out(schema) if schema else None
            return {self._listed(n): {"kind": kinds[n], "namespace": ns,
                                      "columns": [{"name": self._out(c["name"]),
                                                   "type": self._type(c)} for c in cols[n]]}
                    for n in kinds if n in cols}
        return self._inspect(go)

    def describe_relation(self, name):
        schema, _bare = self._schema()
        inner = self._unlisted(name)

        def go(i):
            kind = self._relations(i).get(inner)
            if kind is None:
                return None
            return {"kind": kind, "namespace": self._out(schema) if schema else None,
                    "columns": [{"name": self._out(c["name"]), "type": self._type(c)}
                                for c in i.get_columns(inner, schema=schema)]}
        return self._inspect(go)

    def describe_constraints(self):
        schema, _bare = self._schema()

        def go(i):
            kinds = self._relations(i)
            tables = [n for n, k in kinds.items() if k == "table"]
            out = {"primary_keys": {}, "foreign_keys": []}
            for n in tables:
                pk = (i.get_pk_constraint(n, schema=schema) or {}).get("constrained_columns")
                if pk:
                    out["primary_keys"][self._listed(n)] = [self._out(c) for c in pk]
                for fk in i.get_foreign_keys(n, schema=schema) or []:
                    if fk.get("referred_schema") not in (None, schema) \
                            or fk.get("referred_table") not in kinds:
                        continue
                    out["foreign_keys"].append({
                        "name": fk.get("name") or f"{n}_fk",
                        "from": self._listed(n),
                        "from_columns": [self._out(c) for c in fk["constrained_columns"]],
                        "to": self._listed(fk["referred_table"]),
                        "to_columns": [self._out(c) for c in fk["referred_columns"]]})
            return out
        try:
            return self._inspect(go)
        except Exception:
            log.warning("sql connector %s: describe_constraints failed", self.name,
                        exc_info=True)
            return None

    def view_definitions(self):
        schema, _bare = self._schema()

        def go(i):
            out = {}
            for n, kind in self._relations(i).items():
                if kind != "view":
                    continue
                try:
                    sql = i.get_view_definition(n, schema=schema)
                except Exception:
                    continue
                if sql:
                    out[self._listed(n)] = str(sql)
            return out
        try:
            return self._inspect(go)
        except Exception:
            return None

    def list_namespaces(self):
        from sqlalchemy.engine import make_url
        database = make_url(self._url_text).database or ""
        return self._inspect(lambda i: [{"database": database, "schema": self._out(s)}
                                        for s in i.get_schema_names()])

    def qualifiers(self):
        """The default schema, and `database.schema`, for a source listed with
        bare names; full names (every reference written schema.table) for one
        pinned to another schema. Learned once per connection, then cached."""
        try:
            schema, bare = self._schema()
            default = self._learned()["default_schema"]
        except Exception:
            return frozenset()
        if not bare:
            return FULL_NAMES
        if not default:
            return frozenset()
        from sqlalchemy.engine import make_url
        out = {self._out(default)}
        url = make_url(self._url_text)
        database = url.database if url.host else None      # a file's path is no name
        if database and database != default:
            out.add(f"{database}.{self._out(default)}")
        return frozenset(out)

    def run_query(self, sql):
        max_rows = int(os.getenv("STUDIO_MAX_ROWS", "50000"))
        started = time.time()
        with self.engine().connect() as c:
            try:
                result = c.execution_options(stream_results=True, no_parameters=True) \
                    .exec_driver_sql(sql)
                columns = list(result.keys())
                rows = jsonify_rows([tuple(r) for r in result.fetchmany(max_rows)])
                result.close()
                return columns, rows
            finally:
                c.rollback()                   # never commit: read-only by construction
                log.debug("sql connector %s: query took %.2fs", self.name,
                          time.time() - started)
