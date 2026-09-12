"""KAG on a graph — entity/relation extraction + RBAC-scoped graph retrieval.

Locked in:
- deterministic extraction (no LLM, no network) yields entities + co-occurrence
  edges from prose;
- indexing is scoped by the chunk's access_scope; re-indexing a source clears and
  rebuilds without duplicating;
- graph_search seeds on query terms, walks scoped edges, and returns the
  connected subgraph + source provenance;
- RBAC fail-closed: a private 'u:' scope is reachable ONLY by its owner — not
  another user and not even an admin (same rule as chunk retrieval);
- no query uses a literal % (which would break psycopg on Postgres).
"""
import importlib
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "graph.db"))
    monkeypatch.setenv("STUDIO_SECRET", "graph-secret")
    monkeypatch.setenv("STUDIO_KAG_GRAPH", "1")
    monkeypatch.delenv("STUDIO_KAG_GRAPH_LLM", raising=False)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import kag, kag_graph, rbac
    _db.init_db()
    kag.init_tables()
    kag_graph.init_tables()
    import types
    return types.SimpleNamespace(db=_db, kag=kag, kg=kag_graph, rbac=rbac)


TEXT = ("Acme uses Snowflake for the Warehouse. "
        "The Finance Team owns Revenue reporting and depends on Snowflake.")


def _index(env, scope="analyst", collection="handbook", source="policy.docx", cid="c1"):
    env.kg.index_chunk(collection, chunk_id=cid, source_name=source,
                       text=TEXT, access_scope=scope)


# ── Extraction ────────────────────────────────────────────────────────────

def test_deterministic_extraction_finds_entities_and_edges(env):
    ents, edges = env.kg._extract_deterministic(TEXT)
    assert "snowflake" in ents and "warehouse" in ents and "revenue" in ents
    assert "finance team" in ents          # leading "The" stripped
    assert "acme" not in ents              # stopword-filtered
    # co-occurrence edge inside a sentence (order-normalized)
    assert any({e[0], e[1]} == {"snowflake", "warehouse"} for e in edges)


# ── Indexing + scope + re-index dedup ──────────────────────────────────────

def test_index_persists_scoped_entities(env):
    _index(env, scope="analyst")
    c = env.db._conn()
    n = c.execute("SELECT COUNT(*) n FROM kag_entities WHERE access_scope='analyst'").fetchone()["n"]
    scopes = {r["access_scope"] for r in c.execute("SELECT DISTINCT access_scope FROM kag_edges").fetchall()}
    c.close()
    assert n >= 4 and scopes == {"analyst"}


def test_reindex_clears_and_does_not_duplicate(env):
    _index(env)
    c = env.db._conn(); before = c.execute("SELECT COUNT(*) n FROM kag_entities").fetchone()["n"]; c.close()
    env.kg.clear_source("handbook", "policy.docx")
    _index(env)                            # same source again
    c = env.db._conn(); after = c.execute("SELECT COUNT(*) n FROM kag_entities").fetchone()["n"]; c.close()
    assert after == before                 # rebuilt, not duplicated


# ── Retrieval + traversal ──────────────────────────────────────────────────

def test_graph_search_seeds_and_traverses(env):
    _index(env, scope="analyst")
    g = env.kg.graph_search("snowflake warehouse", role="analyst", user_id="u1")
    names = {e["norm"] for e in g["entities"]}
    assert "snowflake" in names and "warehouse" in names        # seed + neighbor
    assert any({r["source"], r["target"]} == {"snowflake", "warehouse"} for r in g["relations"])
    assert g["sources"] and g["sources"][0]["source"] == "policy.docx"


def test_graph_search_empty_when_no_match(env):
    _index(env, scope="analyst")
    assert env.kg.graph_search("unrelated xyzzy", role="analyst", user_id="u1") == {}


# ── RBAC fail-closed on private 'u:' scopes ────────────────────────────────

def test_private_scope_is_owner_only(env):
    _index(env, scope="u:owner1")
    q = "snowflake warehouse revenue"
    assert env.kg.graph_search(q, role="viewer", user_id="owner1")["entities"]      # owner sees
    assert env.kg.graph_search(q, role="viewer", user_id="other") == {}             # other user: no
    assert env.kg.graph_search(q, role="admin", user_id="adminX") == {}             # admin != owner: no
    assert env.kg.graph_search(q, role="admin", user_id="owner1")["entities"]       # admin who IS owner


def test_role_scope_reachability(env):
    _index(env, scope="analyst")
    q = "snowflake"
    assert env.kg.graph_search(q, role="analyst", user_id="u1")["entities"]         # matching role
    assert env.kg.graph_search(q, role="viewer", user_id="u1") == {}               # different role
    assert env.kg.graph_search(q, role="admin", user_id="a1")["entities"]           # admin reaches org scope


# ── Postgres-safety: no literal % in any query ─────────────────────────────

def test_no_like_percent_in_module(env):
    import app.kag_graph as kg
    src = open(kg.__file__).read()
    # a literal % or a LIKE pattern breaks psycopg (db._pg_sql turns ? into %s)
    assert "LIKE" not in src and "%" not in src, "kag_graph must stay wildcard-free"
    pred, params = env.kag._scope_sql("admin", "u1")
    assert "%" not in pred
