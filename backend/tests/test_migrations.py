"""Versioned schema migrations (app/migrations.py).

Proves: an OLD database (tables created without the late-added columns)
gains every column from apply_pending() and schema_migrations records every
version;
a second apply_pending() is a no-op; a FRESH baseline (init_db + init_tables)
already carries the columns and apply_pending() only records the versions;
pending() is empty afterwards; migration 7 gives an old database the
messages.reply_to column AND its UNIQUE partial index (a second answer to the
same turn cannot be stored, while messages with no reply_to stay
unconstrained) and leaves a fresh baseline's index alone; MIGRATIONS versions
are strictly increasing and unique; the startup policy applies under STUDIO_AUTO_MIGRATE=1 and refuses to
boot (naming the pending work) under STUDIO_AUTO_MIGRATE=0; and two replicas
booting at the same instant against one database both return without raising,
with every migration applied exactly once.

Run from the backend directory:
    python -m pytest tests/test_migrations.py -q
"""
import os
import sqlite3
import tempfile
import threading

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-migrations-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app import chat, db, mcp, migrations, qcache

# Table -> columns a migration must add. Mirrors MIGRATIONS.
EXPECTED = {
    "users": ["verified"],
    "conversations": ["folder_id"],
    "chat_tasks": ["steps", "user_message_id"],
    "mcp_servers": ["owner_id"],
    "query_cache": ["seen", "avg_reward", "embedding"],
    "messages": ["reply_to"],
}

# Derived from the list itself, so appending a migration does not mean editing
# every assertion here — only EXPECTED and OLD_SCHEMA describe the schema.
ALL_VERSIONS = [v for v, _n, _fn in migrations.MIGRATIONS]
LATEST = ALL_VERSIONS[-1]

# The schema as it shipped BEFORE those columns existed.
OLD_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
    name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'viewer', created_at REAL NOT NULL);
CREATE TABLE conversations (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE chat_tasks (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, user_id TEXT NOT NULL, prompt TEXT,
    status TEXT NOT NULL DEFAULT 'running', error TEXT, seen INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, finished_at REAL);
CREATE TABLE mcp_servers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, transport TEXT NOT NULL, url TEXT, command TEXT,
    args TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE messages (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE query_cache (
    id TEXT PRIMARY KEY, role TEXT NOT NULL, source TEXT NOT NULL, table_scope TEXT NOT NULL,
    prompt TEXT NOT NULL, signature TEXT NOT NULL, sql TEXT NOT NULL, chart TEXT, text TEXT,
    hits INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""


@pytest.fixture()
def fresh_path(tmp_path, monkeypatch):
    """Isolated DB file per test; db.DB_PATH is repointed directly (no reload)
    so the module-level suite DB is never touched."""
    path = str(tmp_path / "mig.db")
    monkeypatch.setenv("STUDIO_DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.delenv("STUDIO_AUTO_MIGRATE", raising=False)
    return path


@pytest.fixture()
def old_db(fresh_path):
    raw = sqlite3.connect(fresh_path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()
    return fresh_path


def _columns(path, table):
    raw = sqlite3.connect(path)
    try:
        return [r[1] for r in raw.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        raw.close()


def _recorded(path):
    raw = sqlite3.connect(path)
    try:
        return [r[0] for r in raw.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
    finally:
        raw.close()


def _assert_complete(path):
    for table, cols in EXPECTED.items():
        have = _columns(path, table)
        for col in cols:
            assert col in have, f"{table}.{col} missing"


# ── Ordering invariant ───────────────────────────────────────────────────

def test_versions_strictly_increasing_and_unique():
    versions = [v for v, _n, _fn in migrations.MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert all(b > a for a, b in zip(versions, versions[1:]))
    assert versions[:5] == [1, 2, 3, 4, 5]
    assert all(callable(fn) and isinstance(n, str) and n for _v, n, fn in migrations.MIGRATIONS)


# ── Old database gains every column ──────────────────────────────────────

def test_old_schema_gains_columns_and_records_versions(old_db):
    for table, cols in EXPECTED.items():
        for col in cols:
            assert col not in _columns(old_db, table)      # really old
    assert [v for v, _ in migrations.pending()] == ALL_VERSIONS

    applied = migrations.apply_pending()
    assert [v for v, _ in applied] == ALL_VERSIONS
    assert applied[0][1] == "users.verified"
    _assert_complete(old_db)
    assert _recorded(old_db) == ALL_VERSIONS
    assert migrations.pending() == []
    assert migrations.status() == {"current": LATEST, "pending": []}


def test_apply_twice_is_noop(old_db):
    assert len(migrations.apply_pending()) == len(ALL_VERSIONS)
    assert migrations.apply_pending() == []
    assert _recorded(old_db) == ALL_VERSIONS
    # Columns exist exactly once — an ALTER re-run would have raised.
    assert _columns(old_db, "query_cache").count("seen") == 1


def test_old_columns_get_working_defaults(old_db):
    """The added columns are usable by today's code paths: a pre-migration
    user reads as verified, a task carries steps, cache rows have seen=0."""
    migrations.apply_pending()
    raw = sqlite3.connect(old_db)
    raw.execute("INSERT INTO users (id,email,password_hash,name,created_at) VALUES ('u','a@b','h','A',1)")
    raw.execute("INSERT INTO query_cache (id,role,source,table_scope,prompt,signature,sql,created_at,updated_at) "
                "VALUES ('q','r','s','t','p','sig','select 1',1,1)")
    assert raw.execute("SELECT verified FROM users").fetchone()[0] == 1
    assert raw.execute("SELECT seen, avg_reward, embedding FROM query_cache").fetchone() == (0, None, None)
    raw.close()


# ── Fresh baseline: nothing to alter, versions still recorded ────────────

def test_fresh_baseline_records_without_altering(fresh_path):
    db.init_db()
    chat.init_tables()
    mcp.init_tables()
    qcache.init_tables()
    _assert_complete(fresh_path)                          # baseline is complete
    before = {t: _columns(fresh_path, t) for t in EXPECTED}

    applied = migrations.apply_pending()
    assert [v for v, _ in applied] == ALL_VERSIONS
    assert migrations.pending() == []
    assert _recorded(fresh_path) == ALL_VERSIONS
    assert {t: _columns(fresh_path, t) for t in EXPECTED} == before   # untouched


def _indexes(path, table):
    raw = sqlite3.connect(path)
    try:
        return {r[1] for r in raw.execute(f"PRAGMA index_list({table})").fetchall()}
    finally:
        raw.close()


def test_reply_to_index_is_created_and_enforced_on_an_old_database(old_db):
    """Migration 7 is the one that carries an INDEX, not just a column: the
    UNIQUE partial index on messages.reply_to is what makes a background turn
    answerable exactly once, so an upgraded database must get it too."""
    assert "idx_messages_reply_to" not in _indexes(old_db, "messages")
    migrations.apply_pending()
    assert "reply_to" in _columns(old_db, "messages")
    assert "idx_messages_reply_to" in _indexes(old_db, "messages")

    raw = sqlite3.connect(old_db)
    raw.execute("INSERT INTO messages (id, conversation_id, role, content, created_at, reply_to) "
                "VALUES ('a','c','assistant','{}',1,'u1')")
    # Two answers to the same user turn: the second cannot be stored.
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT INTO messages (id, conversation_id, role, content, created_at, reply_to) "
                    "VALUES ('b','c','assistant','{}',2,'u1')")
    # The index is PARTIAL: rows with no reply_to are unconstrained, so
    # ordinary user turns and synchronous answers still pile up freely.
    raw.execute("INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES ('c1','c','user','{}',3)")
    raw.execute("INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES ('c2','c','user','{}',4)")
    raw.commit()
    assert raw.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 3
    raw.close()


def test_fresh_baseline_already_has_the_reply_to_index(fresh_path):
    """The baseline is complete on its own: init_db() creates the index, and
    migration 7's CREATE ... IF NOT EXISTS leaves it alone."""
    db.init_db()
    assert "idx_messages_reply_to" in _indexes(fresh_path, "messages")
    migrations.apply_pending()
    assert "idx_messages_reply_to" in _indexes(fresh_path, "messages")


def test_missing_table_is_skipped_not_created(fresh_path):
    """A migration never creates its table — that is the baseline's job — so
    running the CLI against an empty database records versions harmlessly."""
    applied = migrations.apply_pending()
    assert len(applied) == len(ALL_VERSIONS)
    raw = sqlite3.connect(fresh_path)
    names = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    raw.close()
    assert names == {"schema_migrations"}


def test_failed_migration_rolls_back_its_ddl_and_record(old_db, monkeypatch):
    """One transaction per migration: a step that ALTERs and then fails leaves
    neither the column nor a schema_migrations row, so the next boot retries."""
    def bad(c, is_pg):
        migrations._add_column(c, "users", "verified", "INTEGER NOT NULL DEFAULT 1", is_pg)
        raise RuntimeError("boom after DDL")

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, "users.verified", bad)])
    with pytest.raises(RuntimeError, match="boom"):
        migrations.apply_pending()
    assert "verified" not in _columns(old_db, "users")
    assert _recorded(old_db) == []
    assert migrations.status() == {"current": 0, "pending": [(1, "users.verified")]}


# ── Startup policy ───────────────────────────────────────────────────────

def test_startup_auto_migrate_default_applies(old_db, monkeypatch):
    monkeypatch.delenv("STUDIO_AUTO_MIGRATE", raising=False)
    assert [v for v, _ in migrations.run_startup()] == ALL_VERSIONS
    _assert_complete(old_db)
    assert migrations.run_startup() == []                 # current: no-op


def test_startup_auto_migrate_off_refuses_with_pending(old_db, monkeypatch):
    monkeypatch.setenv("STUDIO_AUTO_MIGRATE", "0")
    with pytest.raises(RuntimeError) as ei:
        migrations.run_startup()
    msg = str(ei.value)
    assert "users.verified" in msg and "query_cache" in msg
    assert "app.migrate up" in msg
    # Refusing to boot must not have altered anything.
    assert "verified" not in _columns(old_db, "users")
    assert _recorded(old_db) == []


def test_startup_auto_migrate_off_passes_when_current(old_db, monkeypatch):
    migrations.apply_pending()
    monkeypatch.setenv("STUDIO_AUTO_MIGRATE", "0")
    assert migrations.run_startup() == []


# ── Concurrent replicas ──────────────────────────────────────────────────

def _apply_concurrently(fn, n=2):
    """Run fn() in n threads released together; return (results, errors)."""
    go = threading.Event()
    results, errors, lock = [], [], threading.Lock()

    def _one():
        go.wait(5)
        try:
            out = fn()
        except BaseException as e:      # recorded, then asserted on by the caller
            with lock:
                errors.append(e)
        else:
            with lock:
                results.append(out)

    threads = [threading.Thread(target=_one) for _ in range(n)]
    for t in threads:
        t.start()
    go.set()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "apply_pending deadlocked"
    return results, errors


def test_two_replicas_applying_at_once_both_succeed_exactly_once(old_db):
    """Web and worker boot together on the first start after an upgrade and
    both call apply_pending(). Unguarded, one of them died on "database is
    locked"; under the migration lock the loser re-reads schema_migrations,
    finds nothing pending and returns [] — and no version is recorded twice."""
    results, errors = _apply_concurrently(migrations.apply_pending, n=2)
    assert errors == [], f"a replica crashed on boot: {errors}"
    assert len(results) == 2
    # Exactly one of them did the work; the other exited cleanly with nothing.
    assert sorted(len(r) for r in results) == [0, len(ALL_VERSIONS)]
    # One row per version — no duplicates, nothing skipped.
    assert _recorded(old_db) == ALL_VERSIONS
    _assert_complete(old_db)
    assert migrations.pending() == []


def test_two_processes_racing_the_applied_set_do_not_crash(old_db, monkeypatch):
    """The cross-PROCESS half of the guard, forced open. Two replicas are made
    to read schema_migrations at the SAME instant (a barrier inside
    _applied_versions), then both walk the migration list — the exact
    interleaving two separate processes hit on the first boot after an
    upgrade, and the one a process-local mutex cannot prevent.

    Before the fix the loser died on "database is locked" here every time.
    Now SQLite's IMMEDIATE transaction plus busy_timeout makes it WAIT, and a
    version the winner committed in the gap is recognised as already applied
    rather than re-raised, so the loser applies nothing and returns cleanly.
    """
    barrier = threading.Barrier(2, timeout=30)
    real = migrations._applied_versions

    def synced(c):
        out = real(c)
        barrier.wait()          # neither replica moves until both have read
        return out

    monkeypatch.setattr(migrations, "_applied_versions", synced)
    # _apply_pending_locked, not apply_pending: the in-process mutex is not
    # what is under test here, and holding it would serialise the two threads
    # into two separate processes' worth of behaviour we already cover above.
    results, errors = _apply_concurrently(migrations._apply_pending_locked, n=2)
    assert errors == [], f"a replica crashed on boot: {errors}"
    # One replica did all the work; the other applied nothing and returned.
    assert sorted(len(r) for r in results) == [0, len(ALL_VERSIONS)]
    # Read the outcome straight from the file (the barrier is still armed, so
    # nothing here may call back into migrations._applied_versions).
    assert _recorded(old_db) == ALL_VERSIONS       # one row per version
    _assert_complete(old_db)
    assert _columns(old_db, "query_cache").count("seen") == 1


def test_apply_pending_holds_no_lock_after_returning(old_db):
    """The lock is released on the way out (and on the way out of a FAILURE),
    so a second call in the same process is never blocked by the first."""
    migrations.apply_pending()
    assert migrations.apply_pending() == []
    assert not migrations._PROCESS_LOCK.locked()


def test_failed_migration_releases_the_lock(old_db, monkeypatch):
    """A migration that raises must not leave the lock held — otherwise the
    next boot of this process (or the next replica, on Postgres) would hang
    instead of retrying the step."""
    real = migrations.MIGRATIONS

    def bad(c, is_pg):
        raise RuntimeError("boom")

    monkeypatch.setattr(migrations, "MIGRATIONS", [(1, "users.verified", bad)])
    with pytest.raises(RuntimeError, match="boom"):
        migrations.apply_pending()
    assert not migrations._PROCESS_LOCK.locked()
    # Restore only MIGRATIONS (monkeypatch.undo() would also revert the
    # fixture's db.DB_PATH and point us at the suite-wide database).
    migrations.MIGRATIONS = real
    assert [v for v, _ in migrations.apply_pending()] == ALL_VERSIONS


# ── CLI ──────────────────────────────────────────────────────────────────

def test_cli_status_and_up(old_db, capsys):
    from app import migrate
    assert migrate.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "current: 0" in out and "users.verified" in out
    assert migrate.main(["up"]) == 0
    out = capsys.readouterr().out
    assert "applied 1  users.verified" in out and f"current: {LATEST}" in out
    assert migrate.main(["status"]) == 0
    assert "pending: none" in capsys.readouterr().out
    assert migrate.main(["bogus"]) == 2
