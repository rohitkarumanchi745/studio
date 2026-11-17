"""Which sources an answer read and, under each source, the tables and the
columns its SQL names.

The chat's side panel draws this as a tree:

    demo · Any SQL database · SQLite
      ▦ sales      region, revenue, day
      ▦ customers  segment

so a reader sees what an answer was computed from without reading its SQL.
It is read once, when the answer is saved (chat._answer), and stored with
it as `uses`.

How it is read: queryguard's lexer finds the tables (CTEs excluded) and their
aliases. A table's columns are the ones its schema has that the SQL names,
either bare or through the table's alias. The schema is the turn's own (what
the agent was briefed with), else the metadata graph's record. No source is
queried. `all_columns` marks a table read with `*`.

Names only, never values: the same names the answer's SQL already shows, so
the tree is withheld exactly when the SQL is (chat._visible_messages drops a
withheld answer whole).
"""
import logging

from . import db, progress, queryguard

log = logging.getLogger(__name__)

MAX_SOURCES, MAX_TABLES, MAX_COLUMNS = 8, 20, 40
_NOT_A_SOURCE = (None, "", "*", "auto")


def _identifiers(sql):
    """(bare, qualified, stars) the SQL names: bare identifiers that are not a
    function or a qualifier, (qualifier, name) pairs, and the qualifiers read
    with `*` ("" for a bare `SELECT *`). All lowercased."""
    try:
        toks, _ = queryguard._tokens((sql or "").strip().rstrip(";").strip())
    except queryguard.QueryRejected:
        return set(), set(), set()
    bare, qualified, stars = set(), set(), set()
    n = len(toks)
    for i, (kind, text) in enumerate(toks):
        prev = toks[i - 1] if i else None
        if (kind, text) == ("punct", "*"):
            if prev == ("punct", ".") and i >= 2 and toks[i - 2][0] in ("word", "ident"):
                stars.add(toks[i - 2][1].lower())
            elif prev == ("punct", ",") or (prev and prev[0] == "word"
                                            and prev[1].lower() in ("select", "distinct", "all")):
                stars.add("")
            continue
        if kind not in ("word", "ident"):
            continue
        nxt = toks[i + 1] if i + 1 < n else None
        if nxt in (("punct", "("), ("punct", ".")):
            continue                     # a function, or the qualifier of what follows
        if prev == ("punct", ".") and i >= 2 and toks[i - 2][0] in ("word", "ident"):
            qualified.add((toks[i - 2][1].lower(), text.lower()))
        else:
            bare.add(text.lower())
    return bare, qualified, stars


def _recorded_columns(source, table):
    """The metadata graph's columns for `table` (matched without case), or []."""
    try:
        with db.connect() as c:
            row = c.execute("SELECT name FROM meta_objects WHERE source=? AND LOWER(name)=? "
                            "AND removed_at IS NULL", (source, table.lower())).fetchone()
        if not row:
            return []
        from . import metaground
        found = metaground.discovered(source, [row["name"]]).get(row["name"]) or {}
        return [str(c.get("name")) for c in found.get("columns") or [] if c.get("name")]
    except Exception:
        return []


def _columns(source, table, schemas):
    for name, cols in (schemas or {}).items():
        if str(name).lower() == table.lower():
            return [str(c.get("name") if isinstance(c, dict) else c) for c in cols or []]
    return _recorded_columns(source, table)


def _queries(result):
    """[(source, sql)] an answer ran: its own SQL, then each view's."""
    own = result.get("source")
    out = []
    if result.get("sql"):
        out.append((own, result["sql"]))
    for panel in result.get("panels") or []:
        if isinstance(panel, dict) and panel.get("sql"):
            out.append((panel.get("source") or own, panel["sql"]))
    return out


def uses(result, *, schemas=None, schema_source=None):
    """[{source, source_type?, engine?, tables: [{name, columns, all_columns?}]}]
    for an answer. `schemas` ({table: columns}) is the turn's own schema for
    `schema_source`; other sources' tables are looked up in the metadata graph."""
    by_source = {}
    for source, sql in _queries(result if isinstance(result, dict) else {}):
        if source in _NOT_A_SOURCE:
            continue
        if source not in by_source:
            if len(by_source) >= MAX_SOURCES:
                continue
            by_source[source] = {**progress.describe(source), "source": source, "tables": []}
        entry = by_source[source]
        bare, qualified, stars = _identifiers(sql)
        for table, alias in queryguard.table_aliases(sql):
            names = {table.lower()} | ({alias.lower()} if alias else set())
            row = next((t for t in entry["tables"] if t["name"].lower() == table.lower()), None)
            if row is None:
                if len(entry["tables"]) >= MAX_TABLES:
                    continue
                row = {"name": table, "columns": []}
                entry["tables"].append(row)
            schema = _columns(source, table, schemas if source == schema_source else None)
            named = [c for c in schema if c.lower() in bare
                     or any((q, c.lower()) in qualified for q in names)]
            row["columns"] = [c for c in schema if c in set(row["columns"]) | set(named)][:MAX_COLUMNS]
            if "" in stars or names & stars:
                row["all_columns"] = True
    return list(by_source.values())


def safe_uses(result, **kw):
    """uses(), or [] — describing an answer must never fail saving it."""
    try:
        return uses(result, **kw)
    except Exception:
        log.warning("could not describe what an answer used", exc_info=True)
        return []
