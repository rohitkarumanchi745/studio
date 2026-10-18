"""Analytical correctness: a metric's filter is never dropped, and a cached plan
is never reused for the opposite question.

- Every aggregate honours its metric filter — AVG, MIN, MAX and MEDIAN used to
  fall back to an unfiltered aggregate, so "average value of completed orders"
  silently averaged every order.
- Ranking words are part of a cached plan's identity: "highest" and "lowest"
  never share a plan, whatever the similarity score says, including entries
  stored before directions were kept.
- A company called Acme is an entity, not a stopword.
"""
import importlib
import json
import sqlite3
import time
import uuid

import pytest

ROWS = [(1, 10.0, "done"), (2, 20.0, "done"), (3, 30.0, "done"),
        (4, 1.0, "cancelled"), (5, 1000.0, "cancelled"), (6, 2000.0, "cancelled")]

# Filtered (status = 'done') vs every row — each differs, so a dropped filter shows.
EXPECTED = {"sum": (60.0, 3061.0), "count": (3, 6), "count_distinct": (3, 6),
            "avg": (20.0, 3061.0 / 6), "min": (10.0, 1.0), "max": (30.0, 2000.0)}


def _metric(agg, filtered=True):
    return {"name": "m", "agg": agg, "expr": "amount",
            "filter": "status = 'done'" if filtered else None}


@pytest.mark.parametrize("agg", sorted(EXPECTED))
def test_every_aggregate_honours_its_metric_filter(agg):
    from app import semantic
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE orders (id INTEGER, amount REAL, status TEXT)")
    c.executemany("INSERT INTO orders VALUES (?,?,?)", ROWS)
    filtered, everything = EXPECTED[agg]
    got = c.execute(f"SELECT {semantic._agg_sql(_metric(agg), 'sqlite')} FROM orders").fetchone()[0]
    assert got == pytest.approx(filtered)
    got = c.execute(f"SELECT {semantic._agg_sql(_metric(agg, False), 'sqlite')} "
                    f"FROM orders").fetchone()[0]
    assert got == pytest.approx(everything)


def test_a_median_honours_its_filter_too():
    duckdb = pytest.importorskip("duckdb")
    from app import semantic
    c = duckdb.connect()
    c.execute("CREATE TABLE orders (id INTEGER, amount DOUBLE, status VARCHAR)")
    c.executemany("INSERT INTO orders VALUES (?,?,?)", ROWS)
    sql = semantic._agg_sql({**_metric("median")}, "duckdb")
    assert c.execute(f"SELECT {sql} FROM orders").fetchone()[0] == pytest.approx(20.0)
    sql = semantic._agg_sql(_metric("median", False), "duckdb")
    assert c.execute(f"SELECT {sql} FROM orders").fetchone()[0] == pytest.approx(25.0)
    with pytest.raises(ValueError):
        semantic._agg_sql(_metric("median"), "sqlite")           # still declines there


# ── The query cache never reuses a plan for the opposite question ────────

@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "cache.db"))
    monkeypatch.delenv("HARRIER_EMBED_URL", raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import qcache
    _db.init_db()
    qcache.init_tables()
    monkeypatch.setattr(qcache, "_exec_full", lambda user, source, sql: (["x"], [[1]]))
    return qcache


USER = {"id": "u1", "role": "analyst", "email": "a@x.test"}


def _store(qc, prompt, sql):
    return qc.store(USER, "demo", "sales", prompt, {"sql": sql, "rows": [[1]], "text": "ok"},
                    reward=1.0)


def test_highest_and_lowest_never_share_a_plan(cache):
    _store(cache, "Show the highest-revenue products", "SELECT ... ORDER BY revenue DESC")
    assert cache.lookup(USER, "demo", "sales", "Show the lowest-revenue products") is None
    hit = cache.lookup(USER, "demo", "sales", "Show the top revenue products")
    assert hit and hit["sql"].endswith("DESC")                   # same direction: reused
    assert cache._sig("best products") != cache._sig("worst products")
    assert cache._sig("top products") == cache._sig("highest products")


def test_the_direction_is_checked_whatever_the_similarity_says(cache, monkeypatch):
    _store(cache, "products with the most revenue", "SELECT ... DESC")
    monkeypatch.setattr(cache, "_sim", lambda *a: 1.0)          # an embedding calls them twins
    assert cache.lookup(USER, "demo", "sales", "products with the least revenue") is None
    assert cache.lookup(USER, "demo", "sales", "products with the most revenue")


def test_an_entry_stored_before_directions_were_kept_is_judged_by_its_prompt(cache,
                                                                          monkeypatch):
    old_sig = ["product", "revenue"]                            # how the old signature looked
    now = time.time()
    with cache.db.connect() as c:
        c.execute("INSERT INTO query_cache (id, role, source, table_scope, prompt, signature, "
                  "sql, chart, text, hits, seen, avg_reward, embedding, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (str(uuid.uuid4()), "analyst", "demo", "sales",
                   "Show the highest-revenue products", json.dumps(old_sig),
                   "SELECT ... DESC", None, "ok", 0, 3, 1.0, None, now, now))
        c.commit()
    assert cache.learned("demo", "sales", "lowest revenue products") is None   # in the band
    monkeypatch.setattr(cache, "_sim", lambda *a: 1.0)          # only the guard can stop it
    assert cache.lookup(USER, "demo", "sales", "lowest revenue product") is None
    assert cache.lookup(USER, "demo", "sales", "highest revenue product")   # its own direction


# ── KAG: a company called Acme is an entity ──────────────────────────────

def test_a_company_called_acme_keeps_its_name():
    from app import kag_graph
    ents, _edges = kag_graph._extract_deterministic(
        "Acme Markets renewed with Snowflake. ACME Brick depends on Acme.")
    assert {"acme markets", "acme brick", "acme"} <= set(ents)
