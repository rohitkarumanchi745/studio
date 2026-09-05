"""Cypher goes through the SAME gate as SQL — and only read-only Cypher does.

The gateway's guard was SQL-only, so `MATCH (n:Person) RETURN n` was refused
for not being a SELECT and the neo4j source could not be used at all. The fix
forks the GUARD, never the execution path: gateway.execute dispatches on
connector.dialect and everything around it (RBAC, the row cap, governance,
audit) is untouched. These tests pin both halves — the Cypher rules, and the
fact that the graph source runs through gateway.execute exactly like demo.

Run from the backend directory:
    python -m pytest tests/test_cypher_guard.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-cypher-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app import cypherguard, db, gateway, governance
from app.connectors.graph_conn import GraphConnector
from app.cypherguard import enforce_limit, validate
from app.queryguard import QueryRejected

ANALYST = {"id": "u-cypher", "email": "cy@studio.test", "role": "analyst"}
VIEWER = {"id": "u-cypher-v", "email": "cyv@studio.test", "role": "viewer"}

LABELS = ["Person", "Company"]

GOV_YAML = """
version: 1
roles:
  analyst: { sources: { neo4j: "*" } }
  viewer: { sources: { neo4j: [Company] } }
compliance:
  neo4j:
    person:
      deny_columns: [email]
      mask_columns: [salary]
"""


def ok(cypher, allowed=LABELS):
    return validate(cypher, allowed)


def rejected(cypher, allowed=LABELS):
    with pytest.raises(QueryRejected):
        validate(cypher, allowed)


# ── The read shape the SQL guard could not express ──────────────────────

@pytest.mark.parametrize("cypher", [
    "MATCH (n:Person) RETURN n",
    "OPTIONAL MATCH (n:Person) RETURN n",
    "MATCH (p:Person)-[:WORKS_AT]->(c:Company) WHERE p.age > 30 RETURN p.name, c.name",
    "MATCH (n:Person) RETURN n.name ORDER BY n.name SKIP 10",
    "MATCH (n:Person) RETURN count(*)",
    "MATCH (n:`Person`) RETURN n",
    "MATCH (n:Person|Company) RETURN n",
    "UNWIND [1, 2] AS x MATCH (n:Person) WHERE n.id = x RETURN n",
    "WITH 1 AS x MATCH (n:Person) WHERE n.id = x RETURN n",
    "MATCH (:Person)-[:KNOWS]->(m:Person) RETURN m",
    "MATCH p = shortestPath((a:Person)-[*]-(b:Company)) RETURN p",
    # A variable already pinned to an allowed label may be re-matched.
    "MATCH (n:Person) WITH n LIMIT 5 MATCH (n)-[:R]->(m:Company) RETURN m",
    # A read-only CALL subquery, and the catalog procedures.
    "MATCH (n:Person) CALL { MATCH (m:Company) RETURN m } RETURN n, m",
    "CALL db.labels() YIELD label RETURN label",
    # A pattern inside an EXISTS block ends where the block does, so the
    # projection after it is read as a projection and not as node patterns.
    "MATCH (n:Person) WHERE EXISTS { MATCH (n)-[:R]->(c:Company) } RETURN count(*)",
])
def test_read_only_cypher_is_accepted(cypher):
    assert ok(cypher)


def test_a_limit_is_appended_when_the_tail_has_none():
    assert enforce_limit(ok("MATCH (n:Person) RETURN n"), 25) == \
        "MATCH (n:Person) RETURN n LIMIT 25"
    # An explicit LIMIT on the final RETURN is left alone...
    assert enforce_limit(ok("MATCH (n:Person) RETURN n LIMIT 3"), 25) == \
        "MATCH (n:Person) RETURN n LIMIT 3"
    # ...but a LIMIT on an intermediate WITH bounds nothing that is returned.
    capped = enforce_limit(ok("MATCH (n:Person) WITH n LIMIT 5 "
                              "MATCH (n)-[:R]->(m:Company) RETURN m"), 25)
    assert capped.endswith("RETURN m LIMIT 25")
    # A trailing comment cannot swallow the appended clause.
    assert enforce_limit(ok("MATCH (n:Person) RETURN n // all of them"), 25) == \
        "MATCH (n:Person) RETURN n LIMIT 25"


# ── Labels are this source's tables ─────────────────────────────────────

@pytest.mark.parametrize("cypher", [
    "MATCH (n:Secret) RETURN n",
    "MATCH (p:Person)-[:KNOWS]->(s:Secret) RETURN s",
    "MATCH (n:Person|Secret) RETURN n",
    "MATCH (n:Person) MATCH (s:Secret) RETURN n, s",
    "MATCH (n:Person) WHERE n:Secret RETURN n",
])
def test_a_label_outside_the_allowlist_is_rejected(cypher):
    with pytest.raises(QueryRejected, match="not permitted for your role"):
        validate(cypher, LABELS)


@pytest.mark.parametrize("cypher", [
    "MATCH (n) RETURN n",                          # the whole graph
    "MATCH (p:Person)-[]->(x) RETURN x",           # an anonymous neighbour
    "MATCH (a:Person), (b) RETURN a, b",
    "MATCH p = shortestPath((a:Person)-[*]-(b)) RETURN p",
    "MATCH (n:Person) WITH n AS m MATCH (m)-[:R]->(c) RETURN c",   # a fresh alias
    "MATCH (n:Person) WHERE EXISTS { MATCH (x) } RETURN count(*)",  # inside a block
])
def test_an_unlabelled_node_is_rejected(cypher):
    with pytest.raises(QueryRejected, match="must carry a label"):
        validate(cypher, LABELS)


def test_a_query_with_no_label_at_all_fails_closed():
    with pytest.raises(QueryRejected):
        validate("RETURN 1", LABELS)


# ── Writes, administration and multiple statements ──────────────────────

@pytest.mark.parametrize("cypher", [
    "CREATE (n:Person {name: 'x'}) RETURN n",
    "MERGE (n:Person {id: 1}) RETURN n",
    "MATCH (n:Person) DELETE n RETURN 1",
    "MATCH (n:Person) DETACH DELETE n RETURN 1",
    "MATCH (n:Person) SET n.name = 'x' RETURN n",
    "MATCH (n:Person) REMOVE n.name RETURN n",
    "MATCH (n:Person) FOREACH (x IN [1] | SET n.a = x) RETURN n",
    "LOAD CSV FROM 'http://evil.example/x.csv' AS row RETURN row",
    "USING PERIODIC COMMIT LOAD CSV FROM 'http://x/y.csv' AS r RETURN r",
    "DROP INDEX person_name RETURN 1",
    "USE other MATCH (n:Person) RETURN n",
    "SHOW DATABASES",
    "MATCH (n:Person) CALL { CREATE (m:Company) RETURN m } RETURN n, m",
])
def test_every_mutating_or_administrative_clause_is_rejected(cypher):
    rejected(cypher)


@pytest.mark.parametrize("cypher", [
    "CALL apoc.export.csv.all('/tmp/out.csv', {}) YIELD file RETURN file",
    "CALL apoc.cypher.doIt('CREATE (n:X)', {}) YIELD value RETURN value",
    "CALL dbms.security.listUsers() YIELD username RETURN username",
    "CALL db.index.fulltext.createNodeIndex('i', ['Person'], ['name']) RETURN 1",
    "MATCH (n:Person) RETURN apoc.text.join([n.name], ',')",
    "MATCH (n:Person) RETURN gds.util.asNode(0)",
])
def test_only_allowlisted_read_procedures_are_reachable(cypher):
    rejected(cypher)


def test_multiple_statements_are_rejected():
    with pytest.raises(QueryRejected, match="single statement"):
        validate("MATCH (n:Person) RETURN n; MATCH (s:Secret) RETURN s", LABELS)
    # A trailing semicolon is not a second statement.
    assert ok("MATCH (n:Person) RETURN n;")


@pytest.mark.parametrize("cypher", [
    "MATCH (n:Person) //\nCREATE (s:Secret) RETURN n",
    "MATCH (n:Person) /* x */ DETACH DELETE n RETURN 1",
    "MATCH (n:Person) RETURN n /* unterminated",
    "MATCH (n:Person) WHERE n.name = 'unterminated RETURN n",
])
def test_a_comment_or_literal_cannot_hide_a_write(cypher):
    rejected(cypher)


def test_a_write_word_inside_a_literal_or_a_property_is_not_a_write():
    """The mirror image: tokens, not regexes. A regex guard rejected these."""
    assert ok("MATCH (n:Person {name: 'create'}) RETURN n")
    assert ok("MATCH (n:Person) RETURN n.created_at")
    assert ok("MATCH (n:Person) WHERE n.deleted_at IS NULL RETURN n")


def test_validate_returns_the_comment_free_text_the_caller_must_run():
    cleaned = ok("MATCH (n:Person) // pick everyone\nRETURN n")
    assert "//" not in cleaned and "pick everyone" not in cleaned
    assert cleaned.startswith("MATCH (n:Person)") and cleaned.endswith("RETURN n")


def test_only_a_read_clause_may_start_a_statement():
    for head in ("EXPLAIN MATCH (n:Person) RETURN n", "PROFILE MATCH (n:Person) RETURN n",
                 "START n=node(*) RETURN n", ""):
        rejected(head)


def test_the_qualifiers_argument_is_accepted_for_parity_and_ignored():
    """The gateway dispatches without special-casing, so validate() takes the
    same keyword. An empty namespace from the connector must not read as
    'reject everything'."""
    assert validate("MATCH (n:Person) RETURN n", LABELS, qualifiers=frozenset())
    assert validate("MATCH (n:Person) RETURN n", LABELS, qualifiers=None)


# ── Through the gateway: same order, same audit, same governance ────────

class _StubGraph(GraphConnector):
    """The neo4j connector with its HTTP transport replaced by a fixed result.
    run_query is DEFINED here, so __init_subclass__ wraps it and the gateway
    guard applies exactly as it does to the real connector."""

    def __init__(self):
        self.last = None

    def configured(self):
        return True

    def list_tables(self):
        return list(LABELS)

    def run_query(self, cypher):
        self.last = cypher
        return ["name", "email", "salary"], [["Ada", "ada@x.test", 10], ["Bo", "bo@x.test", 20]]


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _graph(monkeypatch):
    """Register the stub as the neo4j source and start from built-in RBAC."""
    from app import connectors
    stub = _StubGraph()
    monkeypatch.setitem(connectors._REGISTRY, "neo4j", stub)
    governance._STATE.update(doc=None, yaml="", source=None)
    yield stub
    governance._STATE.update(doc=None, yaml="", source=None)


def _audit(user, action):
    return [r for r in db.list_activity(user["id"]) if r["action"] == action]


def test_a_graph_read_runs_through_gateway_execute(_graph):
    before = len(_audit(ANALYST, "graph_probe"))
    r = gateway.execute(ANALYST, "neo4j", "MATCH (n:Person) RETURN n.name AS name",
                        "graph_probe", max_rows=5)
    # The guard's cleaned, LIMIT-bearing text is what the connector ran.
    assert _graph.last == "MATCH (n:Person) RETURN n.name AS name LIMIT 5"
    assert r.sql == _graph.last and r.columns == ["name", "email", "salary"]
    assert r.row_count == 2
    rows = _audit(ANALYST, "graph_probe")
    assert len(rows) == before + 1 and rows[0]["ok"] == 1 and rows[0]["sql"] == r.sql


def test_a_rejected_graph_query_never_reaches_the_connector_and_is_audited(_graph):
    before = len(_audit(ANALYST, "graph_bad"))
    with pytest.raises(QueryRejected):
        gateway.execute(ANALYST, "neo4j", "MATCH (n:Person) DETACH DELETE n RETURN 1",
                        "graph_bad")
    assert _graph.last is None
    rows = _audit(ANALYST, "graph_bad")
    assert len(rows) == before + 1 and rows[0]["ok"] == 0


def test_rbac_still_filters_labels_through_the_gateway(_graph):
    governance._set(GOV_YAML, "test")
    with pytest.raises(QueryRejected, match="not permitted for your role"):
        gateway.execute(VIEWER, "neo4j", "MATCH (n:Person) RETURN n", "graph_rbac")
    assert _graph.last is None
    r = gateway.execute(VIEWER, "neo4j", "MATCH (n:Company) RETURN n", "graph_rbac")
    assert r.row_count == 2


def test_governance_filters_the_result_by_column_name_on_the_cypher_path(_graph):
    """filter_result keys on COLUMN names, which is dialect-neutral: no SQL is
    parsed to drop `email` and mask `salary` out of a Cypher result."""
    governance._set(GOV_YAML, "test")
    r = gateway.execute(ANALYST, "neo4j", "MATCH (n:Person) RETURN n", "graph_gov")
    assert "email" not in r.columns and "name" in r.columns
    assert all(row[r.columns.index("salary")] == "***" for row in r.rows)


def test_sql_is_not_accepted_on_a_cypher_source(_graph):
    with pytest.raises(QueryRejected):
        gateway.execute(ANALYST, "neo4j", "SELECT * FROM Person", "graph_sql")
    assert _graph.last is None


def test_the_gateway_picks_the_guard_by_dialect(_graph):
    from app.connectors.demo import DemoConnector
    assert gateway._guard(_graph) is cypherguard
    assert gateway._guard(DemoConnector()).__name__.endswith("queryguard")
