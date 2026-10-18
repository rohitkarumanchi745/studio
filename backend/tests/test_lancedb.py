"""LanceDB as a source — real LanceDB tables, through the real gateway.

Tables are the database's own; embedding columns are left out of SQL; the SQL
runs in DuckDB over the tables it names, read once per cache window and
capped per table (a capped table is named in the briefing); DuckDB reads
nothing but that Arrow. The form takes object-store URIs or db:// with an API
key, refuses Studio's own disk unless the deployment allows database files,
and normalizes storage options.
"""
import importlib
import json
import types

import pyarrow as pa
import pytest

lancedb = pytest.importorskip("lancedb")

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_SECRET", "lancedb-secret")
    monkeypatch.setenv("STUDIO_SQL_ALLOW_LOCAL_FILES", "1")
    from app import db as _db
    importlib.reload(_db)
    from app import connections, connectors, gateway, governance, jobs, metagraph
    from app.connectors import base, demo, lancedb_conn
    _db.init_db()
    jobs.init_tables()
    metagraph.init_tables()
    connections.init_tables()
    monkeypatch.setattr(demo, "WAREHOUSE_PATH", str(tmp_path / "demo.db"))
    governance._set('version: 1\nroles:\n  admin: { sources: "*" }\n', "test")
    uri = str(tmp_path / "lance")
    store = lancedb.connect(uri)
    n = 25
    store.create_table("docs", pa.table({
        "id": list(range(n)),
        "source": ["wiki" if i % 5 else "slack" for i in range(n)],
        "chunk": [f"chunk {i}" for i in range(n)],
        "vector": pa.array([[float(i), 0.0, 1.0, 0.5] for i in range(n)],
                           type=pa.list_(pa.float32(), 4)),
        "added": pa.array([1_760_000_000_000_000 + i * 86_400_000_000 for i in range(n)],
                          type=pa.timestamp("us"))}))
    store.create_table("Source-Owners", pa.table({
        "source": ["wiki", "slack"], "owner": ["Ana", "Ben"]}))
    yield types.SimpleNamespace(gw=gateway, connections=connections, connectors=connectors,
                                lc=lancedb_conn, base=base, mp=monkeypatch, uri=uri,
                                store=store, tmp=tmp_path)
    governance._STATE.update(doc=None, yaml="", source=None)


def _lance(env, name="vectors", **cfg):
    conn = env.lc.LanceDBConnector(name, {"uri": env.uri, **cfg})
    env.mp.setitem(env.connectors._REGISTRY, name, conn)
    return conn


def sql(env, text, source="vectors"):
    return env.gw.execute(ADMIN, source, text, "test").rows


def test_tables_answer_sql_without_their_embeddings(env):
    conn = _lance(env)
    assert conn.list_tables() == ["Source-Owners", "docs"]
    assert sql(env, "SELECT source, COUNT(*) AS n FROM docs GROUP BY source ORDER BY source") \
        == [["slack", 5], ["wiki", 20]]
    assert sql(env, 'SELECT o.owner, COUNT(*) FROM docs d JOIN "Source-Owners" o '
                    "ON o.source = d.source GROUP BY o.owner ORDER BY o.owner") \
        == [["Ana", 20], ["Ben", 5]]
    assert conn.get_schema("docs") == [{"name": "id", "type": "BIGINT"},
                                       {"name": "source", "type": "VARCHAR"},
                                       {"name": "chunk", "type": "VARCHAR"},
                                       {"name": "added", "type": "TIMESTAMP"}]
    with pytest.raises(Exception, match="(?i)vector"):
        sql(env, "SELECT vector FROM docs")
    rels = conn.describe_relations()
    assert set(rels) == {"docs", "Source-Owners"} and rels["docs"]["kind"] == "table"
    assert [c["name"] for c in rels["docs"]["columns"]] == ["id", "source", "chunk", "added"]


def test_a_multivector_column_is_an_embedding_too(env):
    t = pa.list_(pa.list_(pa.float32(), 8))
    assert env.lc.is_embedding(t) and env.lc.is_embedding(pa.list_(pa.float16(), 3))
    assert not env.lc.is_embedding(pa.list_(pa.string()))
    assert not env.lc.is_embedding(pa.float32())


def test_a_capped_table_is_named_in_the_briefing(env):
    conn = _lance(env, max_rows="10")
    assert sql(env, "SELECT COUNT(*) FROM docs") == [[10]]
    said = " ".join(conn.query_guidance())
    assert "first 10 of 25 rows of docs" in said
    assert "Source-Owners" not in said                         # not referenced, not loaded
    conn = _lance(env, name="whole")
    assert sql(env, "SELECT COUNT(*) FROM docs", source="whole") == [[25]]
    assert "rows of docs" not in " ".join(conn.query_guidance())


def test_a_table_is_read_once_per_cache_window(env):
    conn = _lance(env)
    opened = []
    db = conn._db()
    real = db.open_table
    env.mp.setattr(db, "open_table", lambda name, **kw: opened.append(name) or real(name, **kw))
    sql(env, "SELECT COUNT(*) FROM docs")
    sql(env, "SELECT MAX(id) FROM docs")
    assert opened == ["docs"]
    env.store.open_table("docs").add(pa.table({
        "id": [99], "source": ["wiki"], "chunk": ["late"],
        "vector": pa.array([[0.0, 0.0, 0.0, 0.0]], type=pa.list_(pa.float32(), 4)),
        "added": pa.array([0], type=pa.timestamp("us"))}))
    assert sql(env, "SELECT COUNT(*) FROM docs") == [[25]]      # still the cached read
    env.mp.setenv("LANCEDB_CACHE_SECONDS", "0")
    assert sql(env, "SELECT COUNT(*) FROM docs") == [[26]]


def test_duckdb_reads_nothing_but_the_tables(env):
    conn = _lance(env)
    with env.base.gateway_scope("test"):                         # past the guard, on purpose
        for bad in ("SELECT * FROM read_text('/etc/hosts')",
                    "SELECT * FROM read_csv('/etc/hosts')",
                    "SELECT * FROM '/etc/hosts'"):
            with pytest.raises(Exception):
                conn.run_query(bad)
        for setting in ("SET enable_external_access=true", "SET memory_limit='64GB'",
                        "SET autoload_known_extensions=true"):
            with pytest.raises(Exception):
                conn.run_query(setting)
        assert conn.run_query("SELECT COUNT(*) FROM docs")[1] == [[25]]


def test_a_cancel_interrupts_duckdb(env):
    conn = _lance(env)
    token = env.base.CancelToken()
    with env.base.gateway_scope("test"), env.base.cancel_scope(token):
        conn.run_query("SELECT COUNT(*) FROM docs")
    assert len(token._cancels) == 1 and token._cancels[0].__name__ == "interrupt"


def test_tables_are_listed_page_by_page(env):
    conn = _lance(env)
    pages = {None: (["a", "b"], "p2"), "p2": (["c"], "p3"), "p3": (["d"], None)}
    seen = []

    def list_tables(page_token=None, limit=None):
        seen.append(page_token)
        tables, nxt = pages[page_token]
        return types.SimpleNamespace(tables=tables, page_token=nxt)
    env.mp.setattr(conn._db(), "list_tables", list_tables)
    assert conn.list_tables() == ["a", "b", "c", "d"] and seen == [None, "p2", "p3"]


def test_cloud_and_object_storage_connect_with_their_own_settings(env):
    calls = []
    env.mp.setattr(lancedb, "connect", lambda uri, **kw: calls.append((uri, kw)) or object())
    env.lc.LanceDBConnector("c", {"uri": "db://prod-db", "api_key": "ldb-secret",
                                  "region": "eu-west-1", "host_override": "https://x.example.com",
                                  "storage_options": '{"a": "b"}'})._db()
    env.lc.LanceDBConnector("s", {"uri": "s3://bucket/lance",
                                  "storage_options": '{"region": "us-east-2"}'})._db()
    assert calls == [("db://prod-db", {"api_key": "ldb-secret", "region": "eu-west-1",
                                       "host_override": "https://x.example.com"}),
                     ("s3://bucket/lance", {"storage_options": {"region": "us-east-2"}})]


def test_the_form_check(env):
    check = env.connections._check_lancedb
    cfg, problems = check({"uri": " s3://bucket/lance ", "storage_options":
                           '{"aws_access_key_id": "k", "allow_http": true, "timeout": 30}'})
    assert not problems and cfg["uri"] == "s3://bucket/lance"
    assert json.loads(cfg["storage_options"]) == {"aws_access_key_id": "k",
                                                  "allow_http": "true", "timeout": "30"}
    assert "api_key" in check({"uri": "db://prod"})[1]
    assert not check({"uri": "db://prod", "api_key": "k"})[1]
    assert "uri" in check({"uri": "https://example.com/lance"})[1]
    assert "storage_options" in check({"uri": "gs://b/p", "storage_options": "[1]"})[1]
    assert "storage_options" in check({"uri": "gs://b/p", "storage_options": "{oops"})[1]
    assert "host_override" in check({"uri": "db://p", "api_key": "k",
                                      "host_override": "http://x"})[1]
    assert "max_rows" in check({"uri": "az://c/p", "max_rows": "lots"})[1]
    assert not check({"uri": env.uri})[1]                       # local files allowed here
    env.mp.delenv("STUDIO_SQL_ALLOW_LOCAL_FILES")
    assert "STUDIO_SQL_ALLOW_LOCAL_FILES" in check({"uri": env.uri})[1]["uri"]
    assert "uri" in check({"uri": "file:///data/lance"})[1]
    assert not env.lc.LanceDBConnector("x", {"uri": env.uri}).configured()
    env.mp.setattr(env.lc, "driver_available", lambda: False)
    assert "pip install lancedb" in check({"uri": "s3://b/p"})[1]["uri"]


def test_a_connection_test_lists_the_tables_and_never_leaks_the_key(env):
    out = env.connections._probe("lancedb", {"uri": env.uri})
    assert out["ok"] and out["tables"] == 2
    empty = env.tmp / "empty"
    lancedb.connect(str(empty))
    out = env.connections._probe("lancedb", {"uri": str(empty)})
    assert not out["ok"] and "no tables" in out["error"]

    def refuse(uri, **kw):
        raise RuntimeError("401 Unauthorized")
    env.mp.setattr(lancedb, "connect", refuse)
    out = env.connections._probe("lancedb", {"uri": "db://prod", "api_key": "ldb-key-secret"})
    assert not out["ok"] and "ldb-key-secret" not in json.dumps(out)


def test_lancedb_is_offered(env):
    assert "lancedb" in {t["ctype"] for t in env.connections.types(user=ADMIN)}
