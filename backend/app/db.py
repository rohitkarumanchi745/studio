"""App state store (users, conversations, messages) — SQLite for the prototype."""
import json
import os
import sqlite3
import time
import uuid

import bcrypt

# Postgres when DATABASE_URL is set, else SQLite. Container filesystems are
# ephemeral, so file-backed SQLite needs STUDIO_DB_PATH on a mounted volume
# or every deploy discards accounts, chats and dashboards.
DATABASE_URL = os.getenv("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = os.getenv("STUDIO_DB_PATH") or os.path.join(
    os.path.dirname(__file__), "..", "studio.db")


def _pg_sql(sql):
    """SQLite dialect -> Postgres. Placeholders are ?; psycopg wants %s.

    REAL must become DOUBLE PRECISION: Postgres REAL is float4, which holds
    ~7 significant digits and would round a time.time() epoch to whole
    seconds, breaking every ORDER BY created_at.
    """
    return sql.replace("?", "%s").replace(" REAL", " DOUBLE PRECISION")


class _PgConn:
    """SQLite-shaped facade over psycopg, so callers stay dialect-agnostic."""

    def __init__(self, dsn):
        import psycopg
        from psycopg.rows import dict_row
        self._c = psycopg.connect(dsn, row_factory=dict_row)

    def execute(self, sql, params=()):
        return self._c.execute(_pg_sql(sql), tuple(params))

    def executescript(self, script):
        with self._c.cursor() as cur:
            cur.execute(_pg_sql(script))
        self._c.commit()

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()


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
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
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
    # Email-verification flag (guarded ALTER for existing databases). Postgres
    # gets IF NOT EXISTS rather than a caught error: a failed statement there
    # aborts the whole transaction, so swallowing it would poison the seeds
    # that follow — and every restart after the first would fail to boot.
    if IS_PG:
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS verified INTEGER NOT NULL DEFAULT 1")
    else:
        try:
            c.execute("ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass
    c.commit()
    # Seed demo users (one per role) so RBAC is demonstrable out of the box.
    seeds = [
        ("admin@studio.local", "admin123", "Admin", "admin"),
        ("analyst@studio.local", "analyst123", "Analyst", "analyst"),
        ("viewer@studio.local", "viewer123", "Viewer", "viewer"),
    ]
    for email, pw, name, role in seeds:
        if not c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            c.execute(
                "INSERT INTO users (id, email, password_hash, name, role, created_at) VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), email, hash_password(pw), name, role, time.time()),
            )
    c.commit()
    c.close()


def hash_password(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw, pw_hash):
    try:
        return bcrypt.checkpw(pw.encode(), pw_hash.encode())
    except ValueError:
        return False


# ── Users ───────────────────────────────────────────────────────────────

def get_user_by_email(email):
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_user(user_id):
    c = _conn()
    row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def create_user(email, password, name, role="viewer"):
    c = _conn()
    uid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO users (id, email, password_hash, name, role, created_at) VALUES (?,?,?,?,?,?)",
        (uid, email, hash_password(password), name, role, time.time()),
    )
    c.commit()
    c.close()
    return uid


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
        create_user(email, uuid.uuid4().hex, name, role=role)
        mark_verified(email)
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
        "SELECT c.id, c.title, c.created_at, c.user_id AS owner_id, u.email AS owner_email, "
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
