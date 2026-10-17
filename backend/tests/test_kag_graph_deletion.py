"""KAG graph deletion and provenance: deleting a document deletes what it said.

The graph used to outlive its documents. No delete path touched it, a
relationship summed its weight across documents under ONE source_name (so
deleting the document named there dropped a relationship another document still
supported), and counts only ever went up (so re-ingesting inflated them).

Locked in here:
- deleting document A removes the entities and relationships only A supported,
  and keeps those document B also supports, with counts recomputed;
- deleting a collection, an M365 purge, and an M365 tombstone (via
  delete_source) remove the graph with the chunks, in one transaction;
- re-ingesting a document never inflates a count;
- a deleted document never surfaces in graph search or its source references,
  even if a stray row outlived it;
- scope and per-user isolation are unchanged by deletion;
- the repair migration plus rebuild turn a legacy graph into one derived from
  the surviving chunks only.
"""
import importlib
import types

import pytest

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "graph-deletion.db"))
    monkeypatch.setenv("STUDIO_SECRET", "graph-deletion-secret")
    monkeypatch.setenv("STUDIO_KAG_GRAPH", "1")
    monkeypatch.delenv("STUDIO_KAG_GRAPH_LLM", raising=False)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HARRIER_EMBED_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import jobs, kag, kag_graph, migrations
    from app.extraction import store, sync
    _db.init_db()
    kag.init_tables()            # creates the graph tables too
    jobs.init_tables()
    store.init_tables()
    # Start empty even on a shared database (DATABASE_URL pointing at Postgres).
    with _db.connect() as c:
        for table in ("kag_edge_mentions", "kag_mentions", "kag_edges", "kag_entities",
                      "kag_chunks", "kag_collections", "background_jobs"):
            c.execute(f"DELETE FROM {table}")
        c.commit()
    return types.SimpleNamespace(db=_db, kag=kag, kg=kag_graph, jobs=jobs,
                                 migrations=migrations, sync=sync, mp=monkeypatch)


def ingest(env, name, *lines, collection="deals", scope="analyst"):
    if env.kag._collection(collection) is None:
        env.kag._create_collection(collection, scope, None, ADMIN)
    body = "note\n" + "\n".join(lines) + "\n"
    return env.kag.ingest_bytes(ADMIN, collection, name, body.encode())


def graph(env, collection="deals"):
    """{entity name: mentions}, {(src, dst): weight} — exactly what is stored."""
    with env.db.connect() as c:
        ents = {r["norm"]: r["mentions"] for r in c.execute(
            "SELECT norm, mentions FROM kag_entities WHERE collection=?", (collection,)).fetchall()}
        edges = {(r["src"], r["dst"]): r["weight"] for r in c.execute(
            "SELECT src, dst, weight FROM kag_edges WHERE collection=?", (collection,)).fetchall()}
    return ents, edges


def evidence_counts(env, collection="deals"):
    with env.db.connect() as c:
        return {t: c.execute(f"SELECT COUNT(*) n FROM {t} WHERE collection=?",
                             (collection,)).fetchone()["n"]
                for t in ("kag_chunks", "kag_entities", "kag_edges", "kag_mentions",
                          "kag_edge_mentions")}


def consistent(env, collection="deals"):
    """Every stored count equals the evidence behind it; nothing unsupported."""
    with env.db.connect() as c:
        bad_e = c.execute(
            "SELECT COUNT(*) n FROM kag_entities e WHERE collection=? AND mentions <> "
            "(SELECT COUNT(*) FROM kag_mentions m WHERE m.collection=e.collection "
            "AND m.norm=e.norm AND m.access_scope=e.access_scope)", (collection,)).fetchone()["n"]
        bad_r = c.execute(
            "SELECT COUNT(*) n FROM kag_edges e WHERE collection=? AND weight <> "
            "(SELECT COUNT(*) FROM kag_edge_mentions m WHERE m.collection=e.collection "
            "AND m.src=e.src AND m.dst=e.dst AND m.relation=e.relation "
            "AND m.access_scope=e.access_scope)", (collection,)).fetchone()["n"]
        dead = c.execute(
            "SELECT COUNT(*) n FROM kag_mentions m WHERE collection=? AND NOT EXISTS "
            "(SELECT 1 FROM kag_chunks k WHERE k.id=m.chunk_id)", (collection,)).fetchone()["n"]
    return bad_e == 0 and bad_r == 0 and dead == 0


NW, CB, FG = "northwind traders", "contoso bank", "fabrikam group"


# ── Document deletion ────────────────────────────────────────────────────

def test_deleting_a_document_removes_what_only_it_said(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    ingest(env, "b.csv", "Contoso Bank hired Fabrikam Group.")
    env.kag.delete_doc("deals", "a.csv", user=ADMIN)          # the HTTP delete route
    ents, edges = graph(env)
    assert NW not in ents and (CB, NW) not in edges          # A's alone: gone
    assert ents[CB] == 1 and ents[FG] == 1                   # B's: kept, recounted
    assert edges == {(CB, FG): 1}
    assert consistent(env)


@pytest.mark.parametrize("first", ["a.csv", "b.csv"])
def test_deleting_one_supporter_keeps_a_shared_relationship(env, first):
    # The old edge stored ONE source_name (a.csv): deleting A dropped a
    # relationship B still said, and deleting B left A's weight inflated.
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    ingest(env, "b.csv", "Contoso Bank renewed with Northwind Traders.")
    assert graph(env) == ({NW: 2, CB: 2}, {(CB, NW): 2})
    env.kag.delete_source("deals", first)
    assert graph(env) == ({NW: 1, CB: 1}, {(CB, NW): 1})
    other = "b.csv" if first == "a.csv" else "a.csv"
    env.kag.delete_source("deals", other)
    assert graph(env) == ({}, {})
    assert evidence_counts(env) == dict.fromkeys(
        ("kag_chunks", "kag_entities", "kag_edges", "kag_mentions", "kag_edge_mentions"), 0)


def test_reingesting_a_document_never_inflates_counts(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    once = graph(env)
    for _ in range(3):
        ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    assert graph(env) == once == ({NW: 1, CB: 1}, {(CB, NW): 1})
    ingest(env, "a.csv", "Contoso Bank hired Fabrikam Group.")       # a new version
    assert graph(env) == ({CB: 1, FG: 1}, {(CB, FG): 1})            # old version's graph gone
    assert consistent(env)


def test_a_new_version_with_no_text_removes_the_old_one(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    out = env.kag.ingest_bytes(ADMIN, "deals", "a.csv", b"note\n")   # header only: no text
    assert out["chunks"] == 0
    assert evidence_counts(env)["kag_chunks"] == 0
    assert graph(env) == ({}, {})


def test_deletion_still_clears_the_graph_when_the_layer_is_turned_off(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    env.mp.setenv("STUDIO_KAG_GRAPH", "0")
    env.kag.delete_source("deals", "a.csv")
    assert graph(env) == ({}, {})


def test_a_failed_graph_delete_rolls_back_the_chunk_delete(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    before = evidence_counts(env)

    def boom(*a, **k):
        raise RuntimeError("graph write failed")
    env.mp.setattr(env.kg, "remove_source", boom)
    with pytest.raises(RuntimeError):
        env.kag.delete_source("deals", "a.csv")
    assert evidence_counts(env) == before        # neither the chunks nor the graph moved


# ── Collection deletion, M365 purge and tombstone ────────────────────────

def test_deleting_a_collection_removes_its_whole_graph_only(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.", collection="other")
    env.kag.delete_collection("deals", user=ADMIN)                 # the HTTP delete route
    assert set(evidence_counts(env).values()) == {0}
    assert env.kag._collection("deals") is None
    assert graph(env, "other") == ({NW: 1, CB: 1}, {(CB, NW): 1})  # untouched


def test_m365_purge_removes_the_private_graph(env):
    uid = "u-purge"
    name = f"m365-{uid}"
    ingest(env, "mail.eml.csv", "Northwind Traders partnered with Contoso Bank.",
           collection=name, scope=f"u:{uid}")
    env.sync._purge_collection(uid)
    assert set(evidence_counts(env, name).values()) == {0}
    assert env.kag._collection(name) is None


# ── Retrieval never shows a deleted document ─────────────────────────────

def test_deleted_documents_never_surface_in_search_or_sources(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    ingest(env, "b.csv", "Contoso Bank hired Fabrikam Group.")
    env.kag.delete_source("deals", "a.csv")
    assert env.kg.graph_search("Northwind Traders", role="analyst", user_id="u1") == {}
    g = env.kg.graph_search("Contoso Bank", role="analyst", user_id="u1")
    assert {s["source"] for s in g["sources"]} == {"b.csv"}
    assert NW not in {e["norm"] for e in g["entities"]}
    assert all(NW not in (r["source"], r["target"]) for r in g["relations"])


def test_rows_that_outlived_their_chunk_are_ignored_by_retrieval(env):
    # Whatever a legacy write or a crash left behind, retrieval trusts only
    # evidence whose chunk still exists.
    ingest(env, "b.csv", "Contoso Bank hired Fabrikam Group.")
    with env.db.connect() as c:
        c.execute("INSERT INTO kag_entities (id, collection, norm, name, etype, access_scope, "
                  "mentions, created_at) VALUES ('e-ghost','deals',?, 'Northwind Traders','term',"
                  "'analyst',9,0)", (NW,))
        c.execute("INSERT INTO kag_mentions (collection, norm, chunk_id, source_name, page, "
                  "access_scope) VALUES ('deals',?,'gone-chunk','a.csv',NULL,'analyst')", (NW,))
        c.execute("INSERT INTO kag_edges (id, collection, src, dst, relation, access_scope, weight, "
                  "source_name, created_at) VALUES ('r-ghost','deals',?,?,'related_to','analyst',"
                  "9,'a.csv',0)", (CB, NW))
        c.execute("INSERT INTO kag_mentions (collection, norm, chunk_id, source_name, page, "
                  "access_scope) VALUES ('deals',?,'gone-chunk','a.csv',NULL,'analyst')", (CB,))
        c.commit()
    assert env.kg.graph_search("Northwind Traders", role="analyst", user_id="u1") == {}
    g = env.kg.graph_search("Contoso Bank", role="analyst", user_id="u1")
    assert {s["source"] for s in g["sources"]} == {"b.csv"}
    assert all(NW not in (r["source"], r["target"]) for r in g["relations"])


# ── Isolation is unchanged by deletion ───────────────────────────────────

def test_deleting_in_one_scope_never_touches_another(env):
    line = "Northwind Traders partnered with Contoso Bank."
    ingest(env, "a.csv", line, collection="m365-alice", scope="u:alice")
    ingest(env, "a.csv", line, collection="m365-bob", scope="u:bob")
    ingest(env, "a.csv", line, collection="finance", scope="finance")
    env.kag.delete_source("m365-alice", "a.csv")
    assert graph(env, "m365-alice") == ({}, {})
    assert graph(env, "m365-bob") == ({NW: 1, CB: 1}, {(CB, NW): 1})
    assert graph(env, "finance") == ({NW: 1, CB: 1}, {(CB, NW): 1})
    q = "Northwind Traders"
    assert env.kg.graph_search(q, role="viewer", user_id="bob")["entities"]       # bob: his own
    assert env.kg.graph_search(q, role="viewer", user_id="alice") == {}          # alice: deleted
    # An admin reaches the org 'finance' scope, never bob's private one.
    with env.db.connect() as c:
        finance_chunk = c.execute(
            "SELECT id FROM kag_chunks WHERE collection='finance'").fetchone()["id"]
    sources = env.kg.graph_search(q, role="admin", user_id="adm")["sources"]
    assert sources == [{"source": "a.csv", "chunk_id": finance_chunk}]


def test_the_same_entity_in_two_collections_is_counted_separately(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.", collection="sales")
    ingest(env, "b.csv", "Northwind Traders partnered with Contoso Bank.", collection="ops")
    env.kag.delete_source("sales", "a.csv")
    assert graph(env, "ops") == ({NW: 1, CB: 1}, {(CB, NW): 1})


# ── Legacy repair: migration 16 + rebuild ────────────────────────────────

def _make_legacy(env):
    """The pre-provenance shape: A deleted the old way (chunks only), edges with
    one source_name and no per-chunk evidence, counts inflated."""
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    ingest(env, "b.csv", "Contoso Bank renewed with Northwind Traders.",
           "Contoso Bank hired Fabrikam Group.")
    with env.db.connect() as c:
        c.execute("DELETE FROM kag_edge_mentions")
        c.execute("UPDATE kag_edges SET source_name='a.csv', weight=weight+5")
        c.execute("UPDATE kag_entities SET mentions=mentions+5")
        c.execute("DELETE FROM kag_chunks WHERE source_name='a.csv'")   # the old delete
        c.commit()


def _migrate(env):
    with env.db.connect() as c:
        env.migrations._m16_kag_graph_provenance(c, env.db.IS_PG)
        c.commit()


def test_repair_migration_drops_dead_and_unprovable_rows_and_queues_a_rebuild(env):
    _make_legacy(env)
    _migrate(env)
    ents, edges = graph(env)
    assert edges == {}                                  # no legacy edge is provable
    assert ents == {NW: 1, CB: 1, FG: 1}                # recounted from B's chunk only
    assert consistent(env)
    with env.db.connect() as c:
        jobs = c.execute("SELECT kind, status FROM background_jobs "
                         "WHERE id='kag-graph-rebuild-m16'").fetchall()
    assert [(j["kind"], j["status"]) for j in jobs] == [("kag_graph_rebuild", "queued")]
    _migrate(env)                                       # idempotent, one job
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM background_jobs").fetchone()["n"] == 1


def test_rebuild_rederives_the_graph_from_surviving_chunks(env):
    _make_legacy(env)
    _migrate(env)
    result = env.jobs.handlers()["kag_graph_rebuild"]({}, None)
    assert result["collections"] >= 1
    ents, edges = graph(env)
    # B alone (one chunk, two sentences); A's words are gone for good. The
    # default extractor links names within a sentence.
    assert ents == {NW: 1, CB: 1, FG: 1}
    assert edges == {(CB, NW): 1, (CB, FG): 1}
    assert consistent(env)
    assert env.kg.rebuild() and graph(env) == (ents, edges)     # re-running changes nothing


def test_rebuild_keeps_evidence_of_a_chunk_ingested_meanwhile(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    ingest(env, "b.csv", "Contoso Bank hired Fabrikam Group.")
    real = env.kg._chunks_of
    # Rebuild reads the chunk list, then b.csv's chunk arrives before it writes.
    env.mp.setattr(env.kg, "_chunks_of",
                   lambda name: [ch for ch in real(name) if ch["source_name"] != "b.csv"])
    env.kg.rebuild("deals")
    assert graph(env) == ({NW: 1, CB: 2, FG: 1}, {(CB, NW): 1, (CB, FG): 1})
    assert consistent(env)


def test_migration_on_a_store_without_documents_queues_nothing(env):
    _migrate(env)
    with env.db.connect() as c:
        assert c.execute("SELECT COUNT(*) n FROM background_jobs").fetchone()["n"] == 0


def test_rebuild_with_the_layer_off_only_removes_dead_evidence(env):
    _make_legacy(env)
    env.mp.setenv("STUDIO_KAG_GRAPH", "0")
    env.kg.rebuild()
    ents, edges = graph(env)
    assert ents == {NW: 1, CB: 1, FG: 1} and edges == {}     # nothing re-extracted
    assert consistent(env)


def test_payload_names_one_collection(env):
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.")
    ingest(env, "a.csv", "Northwind Traders partnered with Contoso Bank.", collection="other")
    with env.db.connect() as c:
        c.execute("UPDATE kag_entities SET mentions=7 WHERE collection IN ('deals','other')")
        c.commit()
    assert env.kg._rebuild_job({"collection": "other"})["collections"] == 1
    assert graph(env, "other")[0] == {NW: 1, CB: 1}      # repaired
    assert graph(env, "deals")[0] == {NW: 7, CB: 7}      # not named: untouched
