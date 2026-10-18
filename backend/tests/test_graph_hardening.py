"""Graph execution, hardened: join trust, the Neo4j transport, tenants.

- A join inferred from column names is never preferred over a trusted one
  (declared, verified, observed). It is used only where nothing trusted
  connects the tables, and wherever it reaches the agent or the API it says
  it is unverified.
- Neo4j is spoken to over its HTTP API: a Bolt URI is refused with the right
  one to use, a Cypher error raises instead of reading as an empty result, and
  a label is quoted so its own name cannot write Cypher.
- The metadata mirror never writes to a shared Neo4j under a default tenant.
"""
import json
import types

import pytest

from app import metaground, metarelations
from app.connectors import graph_conn


def rel(a, b, state, confidence, ca="k", cb="k"):
    return {"kind": "join", "from_object": a, "to_object": b, "from_columns": [ca],
            "to_columns": [cb], "state": state, "confidence": confidence}


def test_a_trusted_path_wins_over_a_shorter_inferred_one():
    rels = [rel("orders", "customers", "inferred", 0.85, "cust_no", "cust_no"),
            rel("orders", "accounts", "declared", 1.0),
            rel("accounts", "customers", "observed", 0.9)]
    joins, missing = metarelations.join_path(rels, ["orders", "customers"])
    assert missing == [] and {j["state"] for j in joins} == {"declared", "observed"}


def test_an_inferred_join_is_used_only_where_nothing_trusted_reaches():
    rels = [rel("orders", "customers", "declared", 1.0),
            rel("orders", "returns", "inferred", 0.85, "order_ref", "order_id")]
    joins, missing = metarelations.join_path(rels, ["orders", "customers", "returns"])
    assert missing == [] and [j["state"] for j in joins] == ["declared", "inferred"]
    lines = metaground.join_hint_lines(joins)
    assert "(declared, confidence 1.0)" in lines[0]
    assert "UNVERIFIED — inferred from column names" in lines[1]
    assert "check the columns hold the same values" in lines[1]


def test_a_rejected_or_weak_inference_is_never_used():
    rels = [rel("orders", "returns", "rejected", 0.85), rel("orders", "refunds", "inferred", 0.4)]
    _joins, missing = metarelations.join_path(rels, ["orders", "returns", "refunds"])
    assert set(missing) == {"returns", "refunds"}


def test_the_paths_api_says_which_joins_are_unverified(monkeypatch):
    from app import metagraph
    rels = [rel("orders", "customers", "declared", 1.0),
            rel("orders", "returns", "inferred", 0.85)]
    monkeypatch.setattr(metagraph, "_visible_source", lambda user, source: None)
    monkeypatch.setattr(metagraph, "visible_objects", lambda user, source, with_columns=False:
                        [{"name": n} for n in ("orders", "customers", "returns")])
    monkeypatch.setattr(metarelations, "current", lambda source, visible, **k: rels)
    out = metagraph.source_paths("s", "orders,customers,returns", user={"role": "admin"})
    assert out["connected"] and out["unverified"] == 1
    assert [(j["state"], j["trusted"]) for j in out["joins"]] == [("declared", True),
                                                                   ("inferred", False)]


# ── Neo4j over HTTP ──────────────────────────────────────────────────────

class _Neo:
    """Neo4j's HTTP API: 200 with results, or 200 with errors in the body."""

    def __init__(self, monkeypatch, *, errors=None, results=None):
        self.posted = []

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            self.posted.append(json.loads(req.data.decode()))
            return Resp(json.dumps({"results": results if results is not None else [],
                                    "errors": errors or []}).encode())
        monkeypatch.setattr(graph_conn.urllib.request, "urlopen", urlopen)
        for k, v in (("NEO4J_URI", "https://graph.example:7473"), ("NEO4J_USER", "u"),
                     ("NEO4J_PASSWORD", "p")):
            monkeypatch.setenv(k, v)


def test_a_cypher_error_raises_instead_of_reading_as_empty(monkeypatch):
    _Neo(monkeypatch, errors=[{"code": "Neo.ClientError.Statement.SyntaxError",
                               "message": "Invalid input 'RETRUN'"}])
    conn = graph_conn.GraphConnector()
    with pytest.raises(graph_conn.GraphQueryError, match="SyntaxError"):
        conn.list_tables()
    from app.connectors.base import unguarded
    with unguarded(), pytest.raises(graph_conn.GraphQueryError):
        conn.run_query("MATCH (n) RETRUN n")


def test_an_empty_answer_is_still_an_answer(monkeypatch):
    _Neo(monkeypatch, results=[{"columns": ["label"], "data": []}])
    assert graph_conn.GraphConnector().list_tables() == []


def test_a_label_cannot_write_cypher(monkeypatch):
    neo = _Neo(monkeypatch, results=[{"columns": ["ks"], "data": [{"row": [["name"]]}]}])
    graph_conn.GraphConnector().get_schema("Person`) DETACH DELETE n //")
    statement = neo.posted[-1]["statements"][0]["statement"]
    assert statement.startswith("MATCH (n:`Person``) DETACH DELETE n //`)")


def test_a_bolt_uri_is_refused_with_the_one_to_use(monkeypatch):
    _Neo(monkeypatch)
    monkeypatch.setenv("NEO4J_URI", "bolt://graph.example:7687")
    with pytest.raises(graph_conn.GraphQueryError, match="https://host:7473"):
        graph_conn.GraphConnector().list_tables()
    from app import connections
    out = connections._probe("neo4j", {"uri": "neo4j://graph.example:7687", "user": "u",
                                       "password": "p"})
    assert not out["ok"] and out["stage"] == "check" and "HTTP API" in out["fields"]["uri"]
    form = next(t for t in connections.TYPES["neo4j"]["fields"] if t["key"] == "uri")
    assert "7473" in form["placeholder"] and form["placeholder"].startswith("https://")


# ── The mirror's tenant ──────────────────────────────────────────────────

@pytest.fixture()
def mirror_env(monkeypatch):
    from app import metagraph
    for k, v in (("METAGRAPH_NEO4J_URI", "https://shared:7473"), ("METAGRAPH_NEO4J_USER", "u"),
                 ("METAGRAPH_NEO4J_PASSWORD", "p")):
        monkeypatch.setenv(k, v)
    for k in ("STUDIO_METAGRAPH_TENANT", "NEO4J_URI"):
        monkeypatch.delenv(k, raising=False)
    return metagraph


def test_the_mirror_needs_a_tenant_of_its_own(mirror_env, monkeypatch):
    with pytest.raises(mirror_env.MirrorRefused, match="STUDIO_METAGRAPH_TENANT"):
        mirror_env._mirror_cfg()
    monkeypatch.setenv("STUDIO_METAGRAPH_TENANT", "acme-prod")
    assert mirror_env._mirror_cfg()["uri"] == "https://shared:7473"


def test_the_mirror_refuses_a_bolt_uri(mirror_env, monkeypatch):
    monkeypatch.setenv("STUDIO_METAGRAPH_TENANT", "acme-prod")
    monkeypatch.setenv("METAGRAPH_NEO4J_URI", "bolt://shared:7687")
    with pytest.raises(mirror_env.MirrorRefused, match="HTTP API"):
        mirror_env._mirror_cfg()


def test_no_mirror_configured_needs_nothing(mirror_env, monkeypatch):
    monkeypatch.delenv("METAGRAPH_NEO4J_URI")
    assert mirror_env._mirror_cfg() is None
