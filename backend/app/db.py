"""App state store (users, conversations, messages) — SQLite or Postgres.

Schema policy: the CREATE TABLE IF NOT EXISTS baseline here (and in every
module's init_tables()) is the COMPLETE current schema, so a fresh database
needs no migration. Columns added after a table first shipped are ALSO listed
in app/migrations.py, which brings an old database up to this baseline once
and records it in schema_migrations; startup runs it after the last
init_tables(). Never add a guarded ALTER TABLE here again — add the column to
the CREATE and a numbered migration.

Connections: _conn() hands out one connection per call and the caller
close()s it. On SQLite that is a file handle; on Postgres it is a connection
BORROWED from a process-wide psycopg_pool (see _PgConn), so close() is a
return, not a disconnect — and close() ends the open transaction first, so the
connection goes back IDLE (a read-only caller commits nothing, and psycopg
starts a transaction on its first execute).
"""
import json
import logging
import os
import sqlite3
import time
import uuid

import bcrypt

from . import bootstrap

log = logging.getLogger("studio.db")

# Postgres when DATABASE_URL is set, else SQLite. Container filesystems are
# ephemeral, so file-backed SQLite needs STUDIO_DB_PATH on a mounted volume
# or every deploy discards accounts, chats and dashboards.
DATABASE_URL = os.getenv("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = os.getenv("STUDIO_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "..", "studio.db")

# Postgres pool singleton — created lazily on the first _conn() so importing
# this module (tests, the CLI, SQLite deployments) never dials a database.
# _POOL_DISABLED latches when psycopg_pool is not installed: every _conn()
# then opens a direct connection exactly as before, and _POOL_WARNED makes
# sure that degradation is logged once per process rather than per query.
_POOL = None
_POOL_DISABLED = False
_POOL_WARNED = False


def _pg_sql(sql):
    """SQLite dialect -> Postgres. Placeholders are ?; psycopg wants %s.

    REAL must become DOUBLE PRECISION: Postgres REAL is float4, which holds
    ~7 significant digits and would round a time.time() epoch to whole
    seconds, breaking every ORDER BY created_at.
    """
    return sql.replace("?", "%s").replace(" REAL", " DOUBLE PRECISION")


def _pool():
    """The process-wide psycopg_pool.ConnectionPool, or None when the pool
    package is unavailable (direct-connect fallback). Sized by
    STUDIO_PG_POOL_SIZE (max, default 10) and STUDIO_PG_POOL_TIMEOUT (seconds
    to wait for a free connection, default 30); min_size=1 keeps one warm
    connection so the first request after idle does not pay a handshake."""
    global _POOL, _POOL_DISABLED, _POOL_WARNED
    if _POOL is not None:
        return _POOL
    if _POOL_DISABLED:
        return None
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError:
        _POOL_DISABLED = True
        if not _POOL_WARNED:
            _POOL_WARNED = True
            log.warning(
                "db: psycopg_pool is not installed — opening a direct Postgres "
                "connection per call. Install psycopg[binary,pool] to enable pooling.")
        return None
    _POOL = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=int(os.getenv("STUDIO_PG_POOL_SIZE") or 10),
        timeout=float(os.getenv("STUDIO_PG_POOL_TIMEOUT") or 30),
        kwargs={"row_factory": dict_row},
        open=True,
    )
    return _POOL


def pool_stats():
    """Pool gauges for /health, or None: SQLite, direct-connect fallback, or
    no Postgres connection borrowed yet (the pool is lazy)."""
    if _POOL is None:
        return None
    stats = {"min_size": getattr(_POOL, "min_size", None),
             "max_size": getattr(_POOL, "max_size", None)}
    get_stats = getattr(_POOL, "get_stats", None)
    if callable(get_stats):
        try:
            stats.update(get_stats())
        except Exception:
            pass
    return stats


def close_pool():
    """Close the Postgres pool at shutdown so the process exits without
    leaving server-side sessions to time out. Safe to call on SQLite or
    before the pool was ever created."""
    global _POOL
    pool, _POOL = _POOL, None
    if pool is None:
        return
    try:
        pool.close()
    except Exception:
        pass


class _PgConn:
    """SQLite-shaped facade over psycopg, so callers stay dialect-agnostic.

    __init__ borrows a connection from the pool; close() RETURNS it. There
    are ~180 call sites written as `c = _conn(); ...; c.close()`, so the
    borrow/return has to be exactly as cheap and forgiving as the old
    connect/close: close() is idempotent (a second call must not putconn()
    the same connection twice), and __del__ returns a connection a caller
    forgot to close — logging it so the leak gets fixed rather than hidden.
    """

    def __init__(self, dsn):
        # _closed starts True so a failure while borrowing (pool timeout,
        # connect error) leaves nothing for __del__ to return.
        self._closed = True
        self._pool = _pool()
        if self._pool is not None:
            self._c = self._pool.getconn()
        else:
            import psycopg
            from psycopg.rows import dict_row
            self._c = psycopg.connect(dsn, row_factory=dict_row)
        self._closed = False

    def execute(self, sql, params=()):
        return self._c.execute(_pg_sql(sql), tuple(params))

    def executescript(self, script):
        with self._c.cursor() as cur:
            cur.execute(_pg_sql(script))
        self._c.commit()

    def commit(self):
        self._c.commit()

    def rollback(self):
        self._c.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        # End the transaction HERE. psycopg opens one implicitly on the first
        # execute() and never closes it, so a read-only caller — most of them,
        # they have nothing to commit — used to hand the pool a connection in
        # INTRANS. putconn() then rolled it back itself: a "returning
        # connection with transaction in progress" warning and an extra server
        # round trip on EVERY read. Rolling back keeps close()'s contract
        # unchanged (a caller that forgot commit() loses its writes, exactly as
        # a real close() did) and is a no-op after commit(), when the
        # connection is already idle.
        try:
            self._c.rollback()
        except Exception:
            # A broken connection cannot be reset; the pool discards it on
            # return. Never let cleanup raise out of close().
            log.debug("db: rollback before returning the connection failed", exc_info=True)
        if self._pool is not None:
            self._pool.putconn(self._c)
        else:
            self._c.close()

    def __del__(self):
        # Safety net for a leaked connection (caller forgot close(), or an
        # exception skipped it). Guarded for interpreter shutdown, when
        # module globals may already be gone.
        if getattr(self, "_closed", True):
            return
        try:
            log.warning("db: Postgres connection dropped without close() — "
                        "returning it to the pool; the caller leaked it")
            self.close()
        except Exception:
            pass


def _conn():
    if IS_PG:
        return _PgConn(DATABASE_URL)
    parent = os.path.dirname(os.path.abspath(DB_PATH))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'viewer',
            verified INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            folder_id TEXT,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_folders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_shares (
            conversation_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'edit',
            created_at REAL NOT NULL,
            PRIMARY KEY (conversation_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_memory (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_codes (
            email TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            prompt TEXT,
            source TEXT,
            tbl TEXT,
            sql TEXT,
            mode TEXT,
            row_count INTEGER,
            ok INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            duration_ms INTEGER,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS agent_traces (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            conversation_id TEXT,
            prompt TEXT,
            model TEXT,
            mode TEXT,
            source TEXT,
            tbl TEXT,
            sql TEXT,
            ok INTEGER NOT NULL DEFAULT 1,
            error TEXT,
            row_count INTEGER,
            chart_type TEXT,
            panel_count INTEGER,
            duration_ms INTEGER,
            reward REAL,
            reward_source TEXT,
            meta TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_traces_time ON agent_traces(created_at DESC);
        """
    )
    # users.verified and conversations.folder_id were once added here by
    # guarded ALTERs; they live in the CREATE above and in migrations 1-2 now.
    # Seed demo users (one per role) so RBAC is demonstrable out of the box —
    # ONLY in demo mode. Their passwords are public (README), so a production
    # deploy must never create them; bootstrap.enforce() also revokes any that
    # were seeded before this guard existed.
    seeds = bootstrap.SEED_USERS if bootstrap.demo_mode() else []
    for email, pw, name, role in seeds:
        if not c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            c.execute(
                "INSERT INTO users (id, email, password_hash, name, role, created_at) VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), email, hash_password(pw), name, role, time.time()),
            )
    c.commit()
    c.close()


# Not a bcrypt hash, so verify_password() can never match it: the marker for
# an account that has no local password at all (SSO-provisioned). Kept
# distinguishable because bootstrap.ensure_bootstrap_admin() may only promote
# an existing account in place when the operator can prove control of it, and
# "there is no password to prove" is one of the two ways to prove that.
UNUSABLE_PASSWORD_HASH = "!sso-no-local-password"


def hash_password(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw, pw_hash):
    try:
        return bcrypt.checkpw(pw.encode(), pw_hash.encode())
    except ValueError:
        return False


# ── Users ───────────────────────────────────────────────────────────────

def get_user_by_email(email):
    # Emails are stored lowercased; normalise here too so a share invite or
    # login can never miss a user on casing alone.
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE email=?",
                    ((email or "").strip().lower(),)).fetchone()
    c.close()
    return dict(row) if row else None


def get_user(user_id):
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def create_user(email, password, name, role="viewer", verified=1):
    """verified defaults to 1 so every pre-existing caller (SSO provisioning,
    the bootstrap admin, tests) keeps creating usable accounts. Self-service
    signup is the one caller that passes verified=0: auth.current_user and
    /auth/login refuse a password account until it clears the emailed code."""
    email = (email or "").strip().lower()
    c = _conn()
    uid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO users (id, email, password_hash, name, role, verified, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (uid, email, hash_password(password), name, role, 1 if verified else 0, time.time()),
    )
    c.commit()
    c.close()
    # Auto-onboard the new user's Microsoft 365 documents. Lazy import breaks the
    # db <- extraction import cycle; a no-op when Graph is unconfigured (the
    # common case), and best-effort so a connector hiccup never fails signup.
    try:
        from .extraction import sync as _m365_sync
        _m365_sync.enqueue_onboard(uid)
    except Exception:
        pass
    return uid


def set_user_password(user_id, new_hash):
    """Takes an already-hashed password so callers can never store plaintext."""
    c = _conn()
    c.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, user_id))
    c.commit()
    c.close()


def has_usable_password(user):
    """False when the account can never be reached by /auth/login — i.e. it was
    provisioned by SSO. Legacy SSO rows (created before the marker existed)
    carry a random hash nobody knows and read as True here; that only makes the
    bootstrap-admin promotion refuse, which fails safe."""
    if not user:
        return False
    return bool(user.get("password_hash")) and user["password_hash"] != UNUSABLE_PASSWORD_HASH


def set_user_role(email, role):
    c = _conn()
    c.execute("UPDATE users SET role=? WHERE email=?",
              (role, (email or "").strip().lower()))
    c.commit()
    c.close()


# ── Audit log (per-user activity: prompts, SQL, outcomes) ───────────────

def log_activity(user, action, prompt=None, source=None, table=None, sql=None,
                 mode=None, row_count=None, ok=True, error=None, duration_ms=None):
    c = _conn()
    c.execute(
        "INSERT INTO audit_log (id, user_id, email, role, action, prompt, source, tbl, sql, "
        "mode, row_count, ok, error, duration_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), user["id"], user["email"], user["role"], action,
         prompt, source, table, sql, mode, row_count, 1 if ok else 0,
         error, duration_ms, time.time()),
    )
    c.commit()
    c.close()


def list_activity(user_id=None, limit=200):
    """user_id=None → all users (admin); otherwise that user's own activity."""
    c = _conn()
    if user_id:
        rows = c.execute(
            "SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── Agent traces (Agent Lightning-style rollouts: run + reward) ─────────

def add_trace(user, conversation_id=None, prompt=None, model=None, mode=None,
              source=None, table=None, sql=None, ok=True, error=None,
              row_count=None, chart_type=None, panel_count=None,
              duration_ms=None, reward=None, reward_source=None, meta=None):
    tid = str(uuid.uuid4())
    c = _conn()
    c.execute(
        "INSERT INTO agent_traces (id, user_id, email, role, conversation_id, prompt, "
        "model, mode, source, tbl, sql, ok, error, row_count, chart_type, panel_count, "
        "duration_ms, reward, reward_source, meta, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, user["id"], user["email"], user["role"], conversation_id, prompt,
         model, mode, source, table, sql, 1 if ok else 0, error, row_count,
         chart_type, panel_count, duration_ms, reward, reward_source,
         json.dumps(meta or {}), time.time()),
    )
    c.commit()
    c.close()
    return tid


def set_trace_reward(trace_id, reward, source="user", note=None):
    """Overwrite a trace's reward with explicit feedback (user > heuristic)."""
    c = _conn()
    row = c.execute("SELECT meta FROM agent_traces WHERE id=?", (trace_id,)).fetchone()
    if not row:
        c.close()
        return False
    meta = json.loads(row["meta"] or "{}")
    if note:
        meta["feedback_note"] = note[:500]
    c.execute(
        "UPDATE agent_traces SET reward=?, reward_source=?, meta=? WHERE id=?",
        (reward, source, json.dumps(meta), trace_id),
    )
    c.commit()
    c.close()
    return True


def list_traces(limit=200, max_reward=None):
    c = _conn()
    if max_reward is not None:
        rows = c.execute(
            "SELECT * FROM agent_traces WHERE reward IS NOT NULL AND reward <= ? "
            "ORDER BY created_at DESC LIMIT ?", (max_reward, limit)).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM agent_traces ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def trace_stats():
    """Learning dashboard numbers: volume, reward, and failure clusters."""
    c = _conn()
    total = c.execute("SELECT COUNT(*) n, AVG(reward) avg_reward FROM agent_traces").fetchone()
    by_mode = c.execute(
        "SELECT mode, COUNT(*) n, AVG(reward) avg_reward FROM agent_traces "
        "GROUP BY mode ORDER BY n DESC").fetchall()
    by_model = c.execute(
        "SELECT model, COUNT(*) n, AVG(reward) avg_reward FROM agent_traces "
        "WHERE model IS NOT NULL GROUP BY model ORDER BY n DESC").fetchall()
    feedback = c.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN reward >= 0.5 THEN 1 ELSE 0 END) up "
        "FROM agent_traces WHERE reward_source='user'").fetchone()
    fails = c.execute(
        "SELECT substr(error, 1, 90) cluster, COUNT(*) n FROM agent_traces "
        "WHERE error IS NOT NULL GROUP BY cluster ORDER BY n DESC LIMIT 10").fetchall()
    c.close()
    return {
        "total": total["n"], "avg_reward": total["avg_reward"],
        "by_mode": [dict(r) for r in by_mode],
        "by_model": [dict(r) for r in by_model],
        "feedback": dict(feedback),
        "failure_clusters": [dict(r) for r in fails],
    }


def recent_failures(source, limit=5):
    """Distinct recent failure messages for a source — injected into the
    system prompt so the agent stops repeating known mistakes."""
    c = _conn()
    rows = c.execute(
        "SELECT error, MAX(created_at) ts FROM agent_traces "
        "WHERE source=? AND error IS NOT NULL GROUP BY substr(error, 1, 60) "
        "ORDER BY ts DESC LIMIT ?", (source, limit)).fetchall()
    c.close()
    return [r["error"] for r in rows]


# ── Memory + email verification ─────────────────────────────────────────

def add_memory(user_id, note):
    c = _conn()
    c.execute(
        "INSERT INTO user_memory (id, user_id, note, created_at) VALUES (?,?,?,?)",
        (str(uuid.uuid4()), user_id, note[:500], time.time()),
    )
    c.commit()
    c.close()


def list_memory(user_id, limit=20):
    c = _conn()
    rows = c.execute(
        "SELECT note FROM user_memory WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    c.close()
    return [r["note"] for r in rows]


def set_email_code(email, code):
    c = _conn()
    c.execute(
        "INSERT INTO email_codes (email, code, created_at) VALUES (?,?,?) "
        "ON CONFLICT(email) DO UPDATE SET code=excluded.code, created_at=excluded.created_at",
        (email, code, time.time()),
    )
    c.commit()
    c.close()


def check_email_code(email, code, max_age=3600):
    c = _conn()
    row = c.execute("SELECT code, created_at FROM email_codes WHERE email=?", (email,)).fetchone()
    c.close()
    return bool(row and row["code"] == code and time.time() - row["created_at"] < max_age)


def mark_verified(email):
    c = _conn()
    c.execute("UPDATE users SET verified=1 WHERE email=?", (email,))
    c.execute("DELETE FROM email_codes WHERE email=?", (email,))
    c.commit()
    c.close()


def upsert_sso_user(email, name, role):
    """Create or refresh a user arriving via SSO (no local password use)."""
    existing = get_user_by_email(email)
    if existing:
        c = _conn()
        c.execute("UPDATE users SET name=?, role=?, verified=1 WHERE email=?", (name, role, email))
        c.commit()
        c.close()
    else:
        # SSO users are verified by the identity provider and never sign in
        # with a local password, so the row gets the unusable-password marker
        # instead of a random hash.
        uid = create_user(email, uuid.uuid4().hex, name, role=role, verified=1)
        set_user_password(uid, UNUSABLE_PASSWORD_HASH)
    return get_user_by_email(email)


# ── Conversations / messages ────────────────────────────────────────────

def create_conversation(user_id, title):
    c = _conn()
    cid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO conversations (id, user_id, title, created_at) VALUES (?,?,?,?)",
        (cid, user_id, title[:80], time.time()),
    )
    c.commit()
    c.close()
    return cid


def list_conversations(user_id):
    """Own conversations plus any shared with this user, newest first."""
    c = _conn()
    rows = c.execute(
        "SELECT c.id, c.title, c.created_at, c.folder_id, c.user_id AS owner_id, "
        "       u.email AS owner_email, "
        "       s.permission AS shared_permission "
        "FROM conversations c "
        "LEFT JOIN users u ON u.id = c.user_id "
        "LEFT JOIN conversation_shares s ON s.conversation_id = c.id AND s.user_id = ? "
        "WHERE c.user_id = ? OR s.user_id IS NOT NULL "
        "ORDER BY c.created_at DESC",
        (user_id, user_id),
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        owned = d.pop("owner_id") == user_id
        perm = "owner" if owned else (d.pop("shared_permission", None) or "view")
        d.pop("shared_permission", None)
        out.append({
            "id": d["id"], "title": d["title"], "created_at": d["created_at"],
            "permission": perm, "shared": not owned,
            # Folders are personal organization: a recipient never sees (or
            # inherits) the owner's filing, so shared rows stay unfiled.
            "folder_id": d.get("folder_id") if owned else None,
            "owner_email": d.get("owner_email"),
            "can_edit": perm in ("owner", "edit"),
        })
    return out


def rename_conversation(conversation_id, title):
    c = _conn()
    c.execute("UPDATE conversations SET title=? WHERE id=?",
              (title[:80], conversation_id))
    c.commit()
    c.close()


def conversation_owner(conversation_id):
    c = _conn()
    row = c.execute("SELECT user_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    c.close()
    return row["user_id"] if row else None


def conversation_access(conversation_id, user_id):
    """'owner' | 'edit' | 'view' | None — the single source of truth for who
    may touch a conversation. None means it does not exist for this user."""
    c = _conn()
    row = c.execute("SELECT user_id FROM conversations WHERE id=?",
                    (conversation_id,)).fetchone()
    if row is None:
        c.close()
        return None
    if row["user_id"] == user_id:
        c.close()
        return "owner"
    share = c.execute(
        "SELECT permission FROM conversation_shares WHERE conversation_id=? AND user_id=?",
        (conversation_id, user_id)).fetchone()
    c.close()
    if share is None:
        return None
    return "edit" if share["permission"] == "edit" else "view"


def share_conversation(conversation_id, user_id, permission="edit"):
    permission = "edit" if permission == "edit" else "view"
    c = _conn()
    c.execute("DELETE FROM conversation_shares WHERE conversation_id=? AND user_id=?",
              (conversation_id, user_id))
    c.execute(
        "INSERT INTO conversation_shares (conversation_id, user_id, permission, created_at) "
        "VALUES (?,?,?,?)",
        (conversation_id, user_id, permission, time.time()))
    c.commit()
    c.close()


def list_conversation_shares(conversation_id):
    c = _conn()
    rows = c.execute(
        "SELECT s.user_id, s.permission, u.email, u.name "
        "FROM conversation_shares s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE s.conversation_id=? ORDER BY u.email",
        (conversation_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def unshare_conversation(conversation_id, user_id):
    c = _conn()
    c.execute("DELETE FROM conversation_shares WHERE conversation_id=? AND user_id=?",
              (conversation_id, user_id))
    c.commit()
    c.close()


def delete_conversation(conversation_id):
    c = _conn()
    c.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    c.execute("DELETE FROM conversation_shares WHERE conversation_id=?", (conversation_id,))
    c.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
    c.commit()
    c.close()


def add_message(conversation_id, role, content):
    c = _conn()
    mid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?,?,?,?,?)",
        (mid, conversation_id, role, json.dumps(content), time.time()),
    )
    c.commit()
    c.close()
    return mid


def list_messages(conversation_id):
    c = _conn()
    rows = c.execute(
        "SELECT id, role, content, created_at FROM messages WHERE conversation_id=? ORDER BY created_at",
        (conversation_id,),
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["content"] = json.loads(d["content"])
        out.append(d)
    return out
