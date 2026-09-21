"""Versioned schema migrations for the state store (SQLite or Postgres).

Two layers keep the schema honest:

  * The BASELINE — db.init_db() plus every module's init_tables() — is the
    complete, current schema expressed as CREATE TABLE IF NOT EXISTS. A fresh
    database is whole after the baseline alone; no migration is needed.
  * MIGRATIONS bring an OLD database up to the baseline: columns that were
    added after a table first shipped. Each step is recorded in
    schema_migrations so it runs exactly once per database, and each fn is
    idempotent anyway (it checks the catalog before ALTER), so a table that
    already carries the column — created by today's baseline — is left alone
    and the version is simply recorded as applied.

Invariants:
  - apply_pending() runs AFTER the baseline (main.startup(), or the CLI on a
    database the app has already booted against at least once). A migration
    never creates a table: if its table does not exist yet, it is a no-op,
    because the baseline will create the table complete. It MAY create an
    index on a column it just added (CREATE ... IF NOT EXISTS, so the fresh
    baseline that already has it is untouched) — an index is not part of a
    CREATE TABLE, so there is nowhere else for an old database to get one.
  - Versions are strictly increasing and unique (tests/test_migrations.py
    pins this); a new column gets a new version appended at the end.
  - One transaction per migration. Both SQLite and Postgres have
    transactional DDL, so a failed step rolls back its ALTER together with
    its schema_migrations row and the next boot retries it.
  - apply_pending() is SERIALISED across processes. Web and worker replicas
    boot at the same moment after an upgrade and both call it; unguarded, one
    of them met "database is locked" (SQLite) or a colliding transaction
    (Postgres) and crashed on its first boot. Whoever holds the lock applies;
    whoever gets in second RE-READS schema_migrations under it, finds nothing
    pending and returns [] — a clean exit, not an error.
  - Callers go through db's facade so placeholders stay '?' and REAL stays
    the SQLite spelling (db._pg_sql rewrites both for psycopg).
  - STUDIO_AUTO_MIGRATE (default "1") decides what startup does with pending
    work: apply it, or refuse to boot and list it — production discipline is
    to run `python -m app.migrate up` as an explicit release step.
"""
import hashlib
import json
import logging
import os
import threading
import time

from . import db

log = logging.getLogger("studio.migrations")


# ── Catalog helpers (dialect-aware) ─────────────────────────────────────

def _table_exists(c, table, is_pg):
    if is_pg:
        row = c.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,)).fetchone()
    else:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)).fetchone()
    return row is not None


def _column_exists(c, table, column, is_pg):
    if is_pg:
        row = c.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
            (table, column)).fetchone()
        return row is not None
    # Table names here are trusted module constants; PRAGMA takes no bind
    # parameters, hence the f-string.
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _add_column(c, table, column, ddl, is_pg):
    """ALTER TABLE ... ADD COLUMN, only when the table exists and lacks the
    column. `ddl` is the SQLite spelling of the column type/constraints; the
    facade translates it for Postgres."""
    if not _table_exists(c, table, is_pg):
        return False
    if _column_exists(c, table, column, is_pg):
        return False
    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


# ── The migrations ──────────────────────────────────────────────────────
# Each fn(conn, is_pg) is idempotent. Append new versions at the END; never
# renumber or edit an applied one — deployed databases already recorded it.

def _m1_users_verified(c, is_pg):
    # Email-verification flag; pre-existing users are treated as verified.
    _add_column(c, "users", "verified", "INTEGER NOT NULL DEFAULT 1", is_pg)


def _m2_conversations_folder_id(c, is_pg):
    # Sidebar folders (personal organisation of chats).
    _add_column(c, "conversations", "folder_id", "TEXT", is_pg)


def _m3_chat_tasks_steps(c, is_pg):
    # Live activity feed for a background chat turn (progress.py).
    _add_column(c, "chat_tasks", "steps", "TEXT", is_pg)


def _m4_mcp_servers_owner_id(c, is_pg):
    # NULL = admin-registered global server; set = a toolbuilder-built tool
    # that loads only for its owner.
    _add_column(c, "mcp_servers", "owner_id", "TEXT", is_pg)


def _m5_query_cache_routing_columns(c, is_pg):
    # Routing / embedding columns added when the cache learned to rank hits.
    _add_column(c, "query_cache", "seen", "INTEGER NOT NULL DEFAULT 0", is_pg)
    _add_column(c, "query_cache", "avg_reward", "REAL", is_pg)
    _add_column(c, "query_cache", "embedding", "TEXT", is_pg)


def _m6_chat_tasks_user_message_id(c, is_pg):
    # The id of the user message a background turn is answering. The
    # re-entrancy guard matches on it (chat._already_answered), so two turns
    # running at once in one conversation cannot be mistaken for each other.
    # NULL on rows written before this column: those keep the old temporal
    # test.
    _add_column(c, "chat_tasks", "user_message_id", "TEXT", is_pg)


def _m7_messages_reply_to(c, is_pg):
    # The id of the user message an assistant message answers, lifted out of
    # the JSON content into a real column so it can carry a UNIQUE index.
    #
    # WHY a constraint and not a check: a background chat turn's
    # "did someone already answer this?" guard is check-then-write, and two
    # reclaimed attempts of the same chat_turn job can be running at the same
    # instant — the queue's claim token fences the job ROW, but nothing can
    # stop a Python thread mid-flight. The index makes the second answer
    # impossible to store, which is the only guarantee that survives the race.
    #
    # The index is PARTIAL (reply_to IS NOT NULL) so user turns and
    # synchronous answers, which have no reply_to, are unaffected — and it is
    # safe to add to a live database for the same reason: every existing row
    # gets NULL, so nothing can already violate it.
    if not _table_exists(c, "messages", is_pg):
        return
    _add_column(c, "messages", "reply_to", "TEXT", is_pg)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_reply_to "
              "ON messages(reply_to) WHERE reply_to IS NOT NULL")


def _m8_redteam_benchmarks_model_revision(c, is_pg):
    # Exact benchmark-cache reuse must bind to a deployment/model revision,
    # not merely a provider alias that can move underneath an experiment.
    # "unversioned" preserves already-created benchmarks; new cached runs are
    # rejected at the API boundary unless the operator supplies a fingerprint.
    _add_column(
        c,
        "redteam_benchmarks",
        "model_revision",
        "TEXT NOT NULL DEFAULT 'unversioned'",
        is_pg,
    )


def _m9_training_adapters_sha256(c, is_pg):
    # A mutable URI and a human release number do not identify model bytes.
    # New strict BitNet deployments pin this digest end to end; NULL preserves
    # existing non-strict adapter records.
    _add_column(c, "training_adapters", "sha256", "TEXT", is_pg)


def _m10_agent_traces_updated_at(c, is_pg):
    # The trainer must observe a reward revision made after its creation-time
    # cursor passed a trace. A durable integer clock also prevents equal wall
    # clock timestamps at a page boundary from skipping rollouts.
    if not _table_exists(c, "agent_traces", is_pg):
        return
    _add_column(c, "agent_traces", "updated_at", "REAL", is_pg)
    _add_column(c, "agent_traces", "training_revision", "INTEGER", is_pg)
    c.execute("CREATE TABLE IF NOT EXISTS training_event_clock "
              "(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)")
    c.execute("INSERT INTO training_event_clock (id, revision) VALUES (1, 0) "
              "ON CONFLICT(id) DO NOTHING")
    c.execute("UPDATE agent_traces SET updated_at=created_at WHERE updated_at IS NULL")
    current = int(c.execute(
        "SELECT revision FROM training_event_clock WHERE id=1").fetchone()["revision"])
    rows = c.execute(
        "SELECT id FROM agent_traces WHERE training_revision IS NULL "
        "ORDER BY created_at, id").fetchall()
    for row in rows:
        current += 1
        c.execute("UPDATE agent_traces SET training_revision=? WHERE id=?",
                  (current, row["id"]))
    c.execute("UPDATE training_event_clock SET revision=? WHERE id=1", (current,))
    c.execute("CREATE INDEX IF NOT EXISTS idx_traces_training_revision "
              "ON agent_traces(training_revision)")


def _m11_training_adapters_one_active(c, is_pg):
    """Repair pre-fix publication races, then enforce one active identity."""
    if not _table_exists(c, "training_adapters", is_pg):
        return
    rows = c.execute(
        "SELECT id,scope,kind,version,created_at FROM training_adapters "
        "WHERE status='active' "
        "ORDER BY scope,kind,version DESC,created_at DESC,id DESC"
    ).fetchall()
    seen = set()
    for row in rows:
        key = (row["scope"], row["kind"])
        if key in seen:
            c.execute("UPDATE training_adapters SET status='superseded' WHERE id=?",
                      (row["id"],))
        else:
            seen.add(key)
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_adapters_one_active "
        "ON training_adapters(scope,kind) WHERE status='active'")


def _m12_agent_sessions_message_ids(c, is_pg):
    # Exact chat-row provenance for a serialized transcript. It lets reads
    # re-run current governance without replacing a stable fork with later
    # turns from the original conversation. NULL marks a legacy row, which the
    # reader only upgrades when its text is an exact visible subsequence.
    _add_column(c, "agent_sessions", "conversation_message_ids", "TEXT", is_pg)


def _m13_agent_sessions_forks(c, is_pg):
    """Keep stable forks out of the canonical auto-checkpoint lookup.

    Older builds created the canonical row first and each fork later. Mark all
    but the oldest row for each (user, conversation) as a fork while adding
    the explicit discriminator used by new writes.
    """
    _add_column(c, "agent_sessions", "is_fork", "INTEGER NOT NULL DEFAULT 0", is_pg)
    if not _table_exists(c, "agent_sessions", is_pg):
        return
    rows = c.execute(
        "SELECT id,user_id,conversation_id FROM agent_sessions "
        "WHERE conversation_id IS NOT NULL ORDER BY user_id,conversation_id,created_at,id"
    ).fetchall()
    seen = set()
    for row in rows:
        key = (row["user_id"], row["conversation_id"])
        if key in seen:
            c.execute("UPDATE agent_sessions SET is_fork=1 WHERE id=?", (row["id"],))
        else:
            seen.add(key)


def _m14_scrub_private_graph_trace_context(c, is_pg):
    """Remove warehouse reference rows stored by the pre-gate DAG tracer.

    Older dependent-node rollouts persisted their exact conditioning prompt,
    including the bounded ``REFERENCE DATA`` block. New writes redact before
    insertion, but an upgrade also has to clean values already at rest. The
    migration keeps root/task identity and structural lineage while removing
    model-authored request/rejection payloads that may repeat row values.
    """
    if (not _table_exists(c, "agent_traces", is_pg)
            or not _column_exists(c, "agent_traces", "prompt", is_pg)
            or not _column_exists(c, "agent_traces", "meta", is_pg)):
        return

    def _identifier(value):
        return str(value or "")[:160] or None

    def _bounded_int(value, default, maximum):
        try:
            return min(maximum, max(0, int(value)))
        except (TypeError, ValueError):
            return default

    def _safe_graph(graph):
        if not isinstance(graph, dict):
            return None
        spawned_by = _identifier(graph.get("spawned_by"))
        depth = _bounded_int(graph.get("depth"), 0, 100)
        context_mode = str(graph.get("context_mode") or "none").strip().lower()
        if context_mode not in {"none", "parent_rows"}:
            context_mode = "none"
        spawned = graph.get("spawned") if isinstance(graph.get("spawned"), list) else []
        dynamic = bool(graph.get("dynamic")) or (
            depth > 0 and spawned_by not in (None, "__supervisor__"))
        requests = graph.get("spawn_requests")
        rejections = graph.get("spawn_rejections")
        return {
            "node_id": _identifier(graph.get("node_id")),
            "spawned_by": spawned_by,
            "depth": depth,
            "dynamic": dynamic,
            "context_mode": context_mode,
            "spawned": [_identifier(item) for item in spawned[:3] if _identifier(item)],
            "spawn_request_count": _bounded_int(
                graph.get("spawn_request_count",
                          len(requests) if isinstance(requests, list) else 0), 0, 3),
            "spawn_rejection_count": _bounded_int(
                graph.get("spawn_rejection_count",
                          len(rejections) if isinstance(rejections, list) else 0), 0, 20),
        }

    rows = c.execute("SELECT id,prompt,meta FROM agent_traces WHERE meta IS NOT NULL").fetchall()
    for row in rows:
        raw = row["meta"] or ""
        try:
            meta = json.loads(raw)
        except (TypeError, ValueError):
            # A malformed legacy blob cannot be inspected field-by-field. Only
            # replace it when the private-context marker proves it is unsafe.
            if "REFERENCE DATA" not in str(raw):
                continue
            meta = {}
        if not isinstance(meta, dict):
            if "REFERENCE DATA" not in str(raw):
                continue
            meta = {}

        graph = meta.get("graph")
        safe_graph = _safe_graph(graph)
        conditioning = meta.get("conditioning_prompt")
        marker = isinstance(conditioning, str) and "REFERENCE DATA" in conditioning
        graph_private = bool(safe_graph and (
            safe_graph["context_mode"] == "parent_rows" or safe_graph["dynamic"]))
        if not (marker or graph_private or meta.get("global_train_eligible") is False):
            continue

        root = meta.get("root_prompt")
        if not isinstance(root, str):
            root = str(row["prompt"] or "")
        meta["root_prompt"] = root
        meta["conditioning_prompt"] = root
        meta["conditioning_redacted"] = (
            meta.get("conditioning_redacted") or "legacy_graph_context")
        meta["global_train_eligible"] = False
        if safe_graph is not None:
            meta["graph"] = safe_graph
        else:
            meta.pop("graph", None)
        c.execute("UPDATE agent_traces SET meta=? WHERE id=?",
                  (json.dumps(meta, separators=(",", ":")), row["id"]))


MIGRATIONS = [
    (1, "users.verified", _m1_users_verified),
    (2, "conversations.folder_id", _m2_conversations_folder_id),
    (3, "chat_tasks.steps", _m3_chat_tasks_steps),
    (4, "mcp_servers.owner_id", _m4_mcp_servers_owner_id),
    (5, "query_cache.seen+avg_reward+embedding", _m5_query_cache_routing_columns),
    (6, "chat_tasks.user_message_id", _m6_chat_tasks_user_message_id),
    (7, "messages.reply_to + unique index", _m7_messages_reply_to),
    (8, "redteam_benchmarks.model_revision", _m8_redteam_benchmarks_model_revision),
    (9, "training_adapters.sha256", _m9_training_adapters_sha256),
    (10, "agent_traces.training_revision", _m10_agent_traces_updated_at),
    (11, "training_adapters.one_active", _m11_training_adapters_one_active),
    (12, "agent_sessions.conversation_message_ids", _m12_agent_sessions_message_ids),
    (13, "agent_sessions.is_fork", _m13_agent_sessions_forks),
    (14, "agent_traces.scrub_private_graph_context", _m14_scrub_private_graph_trace_context),
]


# ── Bookkeeping ─────────────────────────────────────────────────────────

def _ensure_table(c):
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at REAL NOT NULL
        );
        """
    )


def _applied_versions(c):
    rows = c.execute("SELECT version FROM schema_migrations").fetchall()
    return {r["version"] for r in rows}


def _begin(c, is_pg):
    """Open the per-migration transaction. psycopg begins one implicitly at
    the first statement; Python's sqlite3 only does so before DML, so DDL
    would otherwise autocommit outside any transaction.

    IMMEDIATE, not deferred: it takes SQLite's write lock up front, so a
    second replica booting at the same instant WAITS (see _sqlite_wait_for_
    lock's busy_timeout) instead of reading, then failing to upgrade to a
    write lock with "database is locked" halfway through a migration."""
    if not is_pg:
        c.execute("BEGIN IMMEDIATE")


# ── The cross-process migration lock ────────────────────────────────────
# One fixed key, derived from a constant string, so every replica of every
# version computes the SAME lock and nothing else in the database collides
# with it by accident. Postgres gets a real session-level advisory lock;
# SQLite has no such primitive, so it gets the two things that together give
# the same guarantee: a process-local mutex (threads) plus a busy_timeout and
# an IMMEDIATE transaction (processes).

_LOCK_NAME = "studio.schema_migrations"
_ADVISORY_KEY = int.from_bytes(
    hashlib.sha256(_LOCK_NAME.encode()).digest()[:8], "big", signed=True)
_PROCESS_LOCK = threading.Lock()
_SQLITE_LOCK_WAIT_MS = 30_000


def _acquire_pg_lock(c):
    """Block until this session owns the migration lock. Session-level, so it
    survives the per-migration commits and is only released explicitly."""
    c.execute("SELECT pg_advisory_lock(?)", (_ADVISORY_KEY,))
    c.commit()


def _release_pg_lock(c):
    try:
        c.execute("SELECT pg_advisory_unlock(?)", (_ADVISORY_KEY,))
        c.commit()
    except Exception:
        # The session is going away anyway, which drops the lock; never let
        # cleanup mask the migration's own outcome.
        log.debug("migrations: advisory unlock failed", exc_info=True)


def _sqlite_wait_for_lock(c):
    """Wait rather than fail when another process holds SQLite's write lock."""
    c.execute(f"PRAGMA busy_timeout = {_SQLITE_LOCK_WAIT_MS}")


def _is_applied(c, version):
    try:
        return c.execute("SELECT 1 FROM schema_migrations WHERE version=?",
                         (version,)).fetchone() is not None
    except Exception:
        return False


def pending():
    """[(version, name)] not yet recorded in schema_migrations, in order."""
    with db.connect() as c:
        _ensure_table(c)
        done = _applied_versions(c)
    return [(v, n) for v, n, _fn in MIGRATIONS if v not in done]


def status():
    """{"current": highest applied version (0 = none), "pending": [(v, name)]}."""
    with db.connect() as c:
        _ensure_table(c)
        done = _applied_versions(c)
    return {
        "current": max(done) if done else 0,
        "pending": [(v, n) for v, n, _fn in MIGRATIONS if v not in done],
    }


def apply_pending():
    """Run every unapplied migration in order; returns [(version, name)] applied.
    A no-op (returns []) on a database that is already current.

    Safe to call from several processes at once — web and worker replicas do
    exactly that on the first boot after an upgrade. The work happens under
    the migration lock (see above), and the applied set is re-read once the
    lock is held, so the loser applies nothing and returns [] instead of
    crashing on a locked database or a colliding transaction."""
    # The process-local half of the lock: threads in THIS process queue here,
    # so only one of them ever contends for the database lock below.
    with _PROCESS_LOCK:
        return _apply_pending_locked()


def _apply_pending_locked():
    applied = []
    is_pg = db.IS_PG
    # ONE connection for the whole apply: the advisory lock is session-level,
    # and SQLite's IMMEDIATE transaction lives on this connection too.
    with db.connect() as c:
        holding_pg = False
        try:
            if is_pg:
                _acquire_pg_lock(c)
                holding_pg = True
            else:
                _sqlite_wait_for_lock(c)
            _ensure_table(c)
            # RE-READ under the lock. Another replica may have applied everything
            # while we were waiting for it; a process that now finds nothing
            # pending must exit cleanly rather than re-run anything.
            done = _applied_versions(c)
            for version, name, fn in MIGRATIONS:
                if version in done:
                    continue
                _begin(c, is_pg)
                try:
                    fn(c, is_pg)
                    c.execute(
                        "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
                        (version, name, time.time()))
                    c.commit()
                except Exception:
                    c.rollback()
                    # Last line of defence for the one window the lock cannot
                    # cover on SQLite (another PROCESS committed this exact
                    # version between our re-read and our INSERT): the migration
                    # is applied, just not by us, so carry on instead of crashing
                    # a replica on boot. A genuine failure still raises.
                    if _is_applied(c, version):
                        log.info("migrations: %d %s was applied concurrently by another "
                                 "process", version, name)
                        continue
                    raise
                applied.append((version, name))
                log.info("migrations: applied %d %s", version, name)
        finally:
            if holding_pg:
                _release_pg_lock(c)
    return applied


def auto_migrate():
    return os.getenv("STUDIO_AUTO_MIGRATE", "1").strip().lower() not in {"0", "false", "no", ""}


def run_startup():
    """The startup policy. Call once, AFTER the baseline (last init_tables()).

    STUDIO_AUTO_MIGRATE=1 (default): apply pending migrations and log them.
    STUDIO_AUTO_MIGRATE=0: never alter the schema from a booting app; if
    anything is pending, refuse to boot and name it, so an operator runs
    `python -m app.migrate up` as a deliberate release step first.
    """
    if auto_migrate():
        applied = apply_pending()
        if applied:
            log.info("migrations: applied %s",
                     ", ".join(f"{v} {n}" for v, n in applied))
        else:
            log.info("migrations: schema is current")
        return applied
    todo = pending()
    if todo:
        raise RuntimeError(
            "Schema migrations pending and STUDIO_AUTO_MIGRATE=0: "
            + ", ".join(f"{v} {n}" for v, n in todo)
            + ". Run `python -m app.migrate up` before deploying this version."
        )
    log.info("migrations: schema is current (STUDIO_AUTO_MIGRATE=0)")
    return []
