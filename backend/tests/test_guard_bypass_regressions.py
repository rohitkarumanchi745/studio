"""Bypasses found by re-probing the guards AFTER the third review's fixes.

Each test here reproduces something that VALIDATED (or crashed) on the tree as
the fixers left it. They are grouped by the invariant they defend:

  * a PATTERN COMPREHENSION is a graph traversal, so its node patterns are
    subject to the same label rules as a MATCH — twice over, because its
    brackets also hid every `:Label` inside them behind the
    "relationship types are not allowlisted" exemption;
  * a connector's declared namespace is a CATALOG spelling, so the guard must
    admit exactly the spelling the engine STORES — admitting a case-folded
    copy as well vouched for a different schema than the one list_tables()
    and the search_path pin reach;
  * the CREATE-TABLE baseline runs BEFORE migrations, on old databases too, so
    it must never name a column an old table has yet to be given.

Run from the backend directory:
    python -m pytest tests/test_guard_bypass_regressions.py -q
"""
import os
import sqlite3
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-bypass-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app import cypherguard, queryguard
from app.connectors.postgres_conn import PostgresConnector
from app.connectors.snowflake_conn import SnowflakeConnector
from app.queryguard import QueryRejected

GRAPH_ALLOWED = ["Person", "Company"]


def cypher_rejected(text, match):
    with pytest.raises(QueryRejected, match=match):
        cypherguard.validate(text, GRAPH_ALLOWED)


# ── Pattern comprehensions are patterns ─────────────────────────────────

@pytest.mark.parametrize("text", [
    # The projection is the one place a traversal can hide outside a MATCH.
    "MATCH (n:Person) RETURN [ (n)-->(x:Secret) | x ] AS ns",
    "MATCH (n:Person) RETURN [ (n)-[r:KNOWS]->(x:Secret) | x ] AS ns",
    "MATCH (n:Person) WITH [ (a:Secret)-->(b:Secret) | b ] AS s RETURN s",
    "MATCH (n:Person) WHERE size([ (a:Secret)-->(b:Company) | b ]) > 0 RETURN n",
    "MATCH (n:Person) WHERE EXISTS { RETURN [ (a:Secret)-->(b:Company) | b ] AS s } RETURN n",
])
def test_a_denied_label_cannot_hide_in_a_pattern_comprehension(text):
    """Every `:Label` inside a `[…]` was read as a RELATIONSHIP type, and
    relationship types are deliberately not allowlisted — so a denied label was
    reachable from any projection. Only a bracket that follows a `-` opens a
    relationship; every other one is a list, and a list can hold a pattern."""
    cypher_rejected(text, "label 'secret' is not permitted")


@pytest.mark.parametrize("text", [
    "MATCH (n:Person) RETURN [ (a)-->(b) | b ] AS everything",
    "MATCH (n:Person) RETURN [ (n)-->(x) | x ] AS ns",
    "MATCH (n:Person) WHERE [ (a)-->(b) | b ] <> [] RETURN n",
    "MATCH (n:Person) UNWIND [ (a)-->(b) | b ] AS z RETURN z",
    "MATCH (n:Person) RETURN n ORDER BY size([ (a)-->(b) | b ])",
])
def test_an_unlabelled_node_in_a_comprehension_reads_the_whole_graph(text):
    """`[ (a)-->(b) | b ]` binds two fresh, unlabelled nodes and streams the
    graph, exactly as `MATCH (a)-->(b)` would. The bracket was stepped over, so
    those node groups were never visited by the label rule."""
    cypher_rejected(text, "must carry a label")


@pytest.mark.parametrize("text", [
    # A comprehension anchored on an in-scope variable, reaching an allowed
    # label, is the ordinary shape and must keep working.
    "MATCH (n:Person) RETURN [ (n)-->(c:Company) | c.name ] AS cs",
    "MATCH (n:Person) RETURN [ (n)-->(c:Company) WHERE c.size > 5 | c.name ] AS cs",
    "MATCH (n:Person) WITH [ (n)-->(c:Company) | c ] AS cs RETURN cs",
    # …and nothing that merely LOOKS like one may be dragged into the rule:
    "MATCH (n:Person) RETURN [1, 2] AS l",                    # list literal
    "MATCH (n:Person) RETURN [{a: 1}, {b: 2}] AS l",          # list of maps
    "MATCH (n:Person) RETURN [[1, 2], [3]] AS l",             # nested lists
    "MATCH (n:Person) RETURN [x IN n.tags WHERE x > 1 | x] AS l",  # list comprehension
    "MATCH (n:Person) RETURN n.tags[0] AS t",                 # index
    "MATCH (n:Person) RETURN n.tags[1..2] AS t",              # slice
    "MATCH (n:Person) RETURN [ (n.a) - (n.b) ] AS d",         # subtraction, not an arrow
    "MATCH (a:Person)-[r:KNOWS]->(b:Company) WHERE r.x IN [1, 2] RETURN a",
    "MATCH (a:Person)-[r:KNOWS {since: 2020}]->(b:Company) RETURN a",
])
def test_a_list_that_is_not_a_traversal_is_left_alone(text):
    assert cypherguard.validate(text, GRAPH_ALLOWED)


def test_a_relationship_type_is_still_not_allowlisted():
    """The deliberate exemption stays: RBAC for this source is keyed on node
    labels, the only thing list_tables() can enumerate. Narrowing the bracket
    rule must not turn every edge type into a denied 'label'."""
    assert cypherguard.validate(
        "MATCH (a:Person)-[r:PAYS_SALARY_TO]->(b:Company) RETURN a, b", GRAPH_ALLOWED)


# ── A declared namespace is a catalog spelling ──────────────────────────

def test_postgres_declares_the_schema_the_catalog_actually_uses(monkeypatch):
    """POSTGRES_SCHEMA is the EXACT stored name everywhere else in the
    connector — information_schema is matched against it as written and
    _search_path_option() double-quotes it — so the guard must admit that
    spelling and no other.

    Lower-casing the declaration while ALSO admitting the folded reading was a
    fail-open: with POSTGRES_SCHEMA=Analytics the guard accepted
    `analytics.sales`, which PostgreSQL resolves to the schema `analytics` —
    a namespace the catalog never described and the allowlist never covered —
    and rejected `"Analytics".sales`, the one spelling that reaches the
    configured schema.
    """
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://u:p@host:5432/acme")
    monkeypatch.setenv("POSTGRES_SCHEMA", "Analytics")
    quals = PostgresConnector().qualifiers()
    assert quals == frozenset({"Analytics", "acme.Analytics"})
    for outside in ('SELECT * FROM analytics.sales',
                    'SELECT * FROM "analytics".sales',
                    'SELECT * FROM Analytics.sales'):          # bare folds DOWN
        with pytest.raises(QueryRejected, match="outside the configured namespace"):
            queryguard.validate(outside, ["sales"], quals, "postgres")
    assert queryguard.validate('SELECT * FROM "Analytics".sales', ["sales"],
                               quals, "postgres")
    assert queryguard.validate('SELECT * FROM "acme"."Analytics".sales', ["sales"],
                               quals, "postgres")


def test_snowflake_declares_the_schema_snowflake_stores(monkeypatch):
    """The mirror image: Snowflake FOLDS an unquoted env value UP, and
    list_tables() already reads it that way, so a lower-cased declaration
    admitted `"public".sales` — a quoted, and therefore different, schema."""
    monkeypatch.setenv("SNOWFLAKE_DATABASE", "analytics")
    monkeypatch.setenv("SNOWFLAKE_SCHEMA", "public")
    quals = SnowflakeConnector().qualifiers()
    assert quals == frozenset({"PUBLIC", "ANALYTICS.PUBLIC"})
    assert queryguard.validate("SELECT * FROM public.sales", ["SALES"],
                               quals, "snowflake")
    with pytest.raises(QueryRejected, match="outside the configured namespace"):
        queryguard.validate('SELECT * FROM "public".sales', ["SALES"],
                            quals, "snowflake")


def test_a_declared_prefix_has_exactly_one_identity():
    """One spelling in, one identity out. Two readings of the same declared
    prefix is what let a folded copy of a quote-created schema through."""
    by_arity = queryguard._declared_qualifiers({"Analytics"}, "postgres")
    assert by_arity == {1: {"Analytics"}}


# ── The baseline must survive an OLD database ───────────────────────────

def test_the_baseline_runs_on_a_database_created_before_reply_to(monkeypatch):
    """db.init_db() runs at startup BEFORE migrations.run_startup(), on every
    database — so naming a column an old table lacks does not just skip an
    index, it aborts the whole executescript and the app never boots. That is
    what `CREATE UNIQUE INDEX … ON messages(reply_to)` inside the CREATE TABLE
    script did: every existing deployment failed with "no such column:
    reply_to", and migration 7 (the thing that adds the column) never ran.

    Fresh database: the CREATE TABLE makes the column and init_db builds the
    index. Old database: init_db skips it and migration 7 adds both.
    """
    from app import db, migrations
    old = os.path.join(tempfile.mkdtemp(prefix="studio-oldschema-"), "old.db")
    c = sqlite3.connect(old)
    c.executescript(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """)
    c.commit()
    c.close()
    monkeypatch.setattr(db, "DB_PATH", old)

    db.init_db()                                    # the boot that used to die
    with db.connect() as conn:
        assert not db._has_column(conn, "messages", "reply_to")
        assert not _indexes(conn) & {"idx_messages_reply_to"}

    migrations.apply_pending()
    with db.connect() as conn:
        assert db._has_column(conn, "messages", "reply_to")
        assert "idx_messages_reply_to" in _indexes(conn)


def test_a_fresh_baseline_still_builds_the_unique_index(monkeypatch):
    """Moving the index out of the script must not lose it: the guarantee that
    one chat turn is answered ONCE is that index, not a check-then-insert."""
    from app import db
    fresh = os.path.join(tempfile.mkdtemp(prefix="studio-freshschema-"), "new.db")
    monkeypatch.setattr(db, "DB_PATH", fresh)
    db.init_db()
    with db.connect() as conn:
        assert "idx_messages_reply_to" in _indexes(conn)
        conn.execute("INSERT INTO conversations (id, user_id, title, created_at) "
                     "VALUES ('c1', 'u1', 't', 1)")
        conn.commit()
    assert db.add_message("c1", "assistant", {"a": 1}, reply_to="um1")
    assert db.add_message("c1", "assistant", {"a": 2}, reply_to="um1") is None


def _indexes(conn):
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()}
