"""Encrypted-at-rest persistence for OAuth tokens, delta cursors, the webhook
subscriptions, and the item idempotency/tombstone ledger.

Secret handling mirrors keys.py exactly: a Fernet key derived from STUDIO_SECRET
via PBKDF2, but with a DISTINCT salt (b"studio-graph-oauth-v1") so this domain is
cryptographically separated from user_api_keys — a leak of one derivation can
never decrypt the other. OAuth access/refresh tokens and the webhook clientState
are stored ONLY as ciphertext; nothing token-related is ever logged or returned
by a route. A rotated STUDIO_SECRET makes stored tokens undecryptable, and
get_tokens() then returns None (fail closed → the account is marked revoked and
the user must reconnect; no plaintext is recoverable from a stolen DB alone).

Delta links are NOT secret (they are opaque Graph cursor URLs, though we still
strip them from any error message at the client layer) and are stored as plain
TEXT columns on the account row.

Dialect discipline (identical to autopilot.init_tables): TEXT uuid PKs, every
timestamp column declared with a leading-space ' REAL' so db._pg_sql rewrites it
to DOUBLE PRECISION (Postgres REAL would round a time.time() epoch to whole
seconds and break ORDER BY next_run_at); '?' placeholders everywhere; one
db._conn() opened/committed/closed per call. All three tables ship every column
in the initial CREATE — no ALTER anywhere — so the Postgres "ADD COLUMN IF NOT
EXISTS" transaction-poison gotcha is sidestepped entirely.
"""
import base64
import os
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .. import db

# Domain-separated from keys.py's b"studio-user-api-keys-v1".
_SALT = b"studio-graph-oauth-v1"

# A claim older than this (seconds) is treated as dead so a crashed run's row is
# reclaimed — mirrors autopilot._CLAIM_STALE.
_CLAIM_STALE = 300

_DELTA_COLS = {"drive": "drive_delta_link", "mail": "mail_delta_link"}


def _fernet():
    secret = os.getenv("STUDIO_SECRET", "dev-secret-change-me").encode()
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret)))


def init_tables():
    """CREATE TABLE / INDEX IF NOT EXISTS only — no ALTER. Registered in
    main.py startup() next to kag.init_tables()."""
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS graph_accounts (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            auth_mode TEXT NOT NULL,
            graph_user_id TEXT,
            access_ct TEXT,
            refresh_ct TEXT,
            token_expires_at REAL,
            drive_delta_link TEXT,
            mail_delta_link TEXT,
            next_run_at REAL,
            claimed_at REAL,
            status TEXT,
            last_error TEXT,
            created_at REAL,
            updated_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_accounts_user
            ON graph_accounts(user_id);

        CREATE TABLE IF NOT EXISTS graph_subscriptions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            resource TEXT NOT NULL,
            client_state_ct TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_graph_subscriptions_user
            ON graph_subscriptions(user_id);

        CREATE TABLE IF NOT EXISTS graph_items (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            graph_id TEXT NOT NULL,
            kind TEXT,
            etag TEXT,
            collection TEXT,
            source_name TEXT,
            access_scope TEXT,
            ingested_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_items_key
            ON graph_items(user_id, graph_id);
        """
    )
    c.commit()
    c.close()


# ── Account row (one per connected Studio user) ──────────────────────────

def save_account(user_id, auth_mode, graph_user_id=None):
    """Ensure the account row exists (status='onboarding' on first insert) and
    refresh its auth_mode / graph_user_id. Never touches token ciphertext."""
    now = time.time()
    c = db._conn()
    r = c.execute("SELECT id FROM graph_accounts WHERE user_id=?", (user_id,)).fetchone()
    if r:
        c.execute(
            "UPDATE graph_accounts SET auth_mode=?, "
            "graph_user_id=COALESCE(?, graph_user_id), updated_at=? WHERE user_id=?",
            (auth_mode, graph_user_id, now, user_id))
    else:
        c.execute(
            "INSERT INTO graph_accounts (id, user_id, auth_mode, graph_user_id, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), user_id, auth_mode, graph_user_id, "onboarding", now, now))
    c.commit()
    c.close()


def get_account(user_id):
    c = db._conn()
    r = c.execute("SELECT * FROM graph_accounts WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def update_fields(user_id, fields):
    """Set arbitrary account columns (keys are our own column names, never
    user input). Deliberately does NOT auto-bump updated_at so a scheduler-only
    write doesn't masquerade as a successful sync (last_sync = updated_at)."""
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    c = db._conn()
    c.execute(f"UPDATE graph_accounts SET {sets} WHERE user_id=?",
              list(fields.values()) + [user_id])
    c.commit()
    c.close()


# ── OAuth tokens (encrypted at rest) ─────────────────────────────────────

def set_tokens(user_id, access, refresh, expires_at):
    """Encrypt + persist the access/refresh pair and its expiry. Creates a bare
    account row if none exists yet (delegated OAuth callback path)."""
    f = _fernet()
    ac = f.encrypt((access or "").encode()).decode()
    rc = f.encrypt((refresh or "").encode()).decode()
    now = time.time()
    c = db._conn()
    r = c.execute("SELECT id FROM graph_accounts WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        c.execute(
            "INSERT INTO graph_accounts (id, user_id, auth_mode, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), user_id,
             os.getenv("STUDIO_GRAPH_AUTH_MODE", "delegated"), "onboarding", now, now))
    c.execute(
        "UPDATE graph_accounts SET access_ct=?, refresh_ct=?, token_expires_at=?, "
        "updated_at=? WHERE user_id=?", (ac, rc, expires_at, now, user_id))
    c.commit()
    c.close()


def get_tokens(user_id):
    """{access, refresh, expires_at} or None. Returns None on InvalidToken (a
    rotated STUDIO_SECRET) so the caller fails closed and forces a reconnect —
    never a plaintext leak. Never log this."""
    c = db._conn()
    r = c.execute(
        "SELECT access_ct, refresh_ct, token_expires_at FROM graph_accounts "
        "WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    if not r or not r["access_ct"] or not r["refresh_ct"]:
        return None
    f = _fernet()
    try:
        return {"access": f.decrypt(r["access_ct"].encode()).decode(),
                "refresh": f.decrypt(r["refresh_ct"].encode()).decode(),
                "expires_at": r["token_expires_at"]}
    except InvalidToken:
        return None


# ── Delta cursors (opaque, non-secret) ───────────────────────────────────

def set_delta(user_id, kind, delta_link):
    col = _DELTA_COLS.get(kind)
    if not col:
        return
    c = db._conn()
    c.execute(f"UPDATE graph_accounts SET {col}=? WHERE user_id=?", (delta_link, user_id))
    c.commit()
    c.close()


def get_delta(user_id, kind):
    col = _DELTA_COLS.get(kind)
    if not col:
        return None
    c = db._conn()
    r = c.execute(f"SELECT {col} AS v FROM graph_accounts WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return (r["v"] if r else None)


# ── Item ledger (idempotency + tombstones) ───────────────────────────────

def get_item(user_id, graph_id):
    c = db._conn()
    r = c.execute("SELECT * FROM graph_items WHERE user_id=? AND graph_id=?",
                  (user_id, graph_id)).fetchone()
    c.close()
    return dict(r) if r else None


def upsert_item(user_id, graph_id, kind, etag, collection, source_name, access_scope):
    c = db._conn()
    c.execute("DELETE FROM graph_items WHERE user_id=? AND graph_id=?", (user_id, graph_id))
    c.execute(
        "INSERT INTO graph_items (id, user_id, graph_id, kind, etag, collection, "
        "source_name, access_scope, ingested_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), user_id, graph_id, kind, etag, collection,
         source_name, access_scope, time.time()))
    c.commit()
    c.close()


def delete_item(user_id, graph_id):
    c = db._conn()
    c.execute("DELETE FROM graph_items WHERE user_id=? AND graph_id=?", (user_id, graph_id))
    c.commit()
    c.close()


# ── Scheduler claim (autopilot-style race-safe conditional UPDATE) ───────

def claim_due(now):
    """Atomically claim all due, non-revoked, unclaimed(or stale-claimed)
    accounts by stamping claimed_at=now, then read them back. The conditional
    UPDATE is the race guard (SQLite serializes writes; Postgres row-locks), so
    two ticks never both win the same account — identical to autopilot._claim_due."""
    stale = now - _CLAIM_STALE
    c = db._conn()
    c.execute(
        "UPDATE graph_accounts SET claimed_at=? "
        "WHERE next_run_at IS NOT NULL AND next_run_at<=? AND status!=? "
        "AND (claimed_at IS NULL OR claimed_at<?)",
        (now, now, "revoked", stale))
    c.commit()
    rows = c.execute("SELECT * FROM graph_accounts WHERE claimed_at=?", (now,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── Webhook subscriptions (clientState encrypted at rest) ────────────────

def save_subscription(sub_id, user_id, resource, client_state, expires_at):
    ct = _fernet().encrypt((client_state or "").encode()).decode()
    c = db._conn()
    c.execute("DELETE FROM graph_subscriptions WHERE id=?", (sub_id,))
    c.execute(
        "INSERT INTO graph_subscriptions (id, user_id, resource, client_state_ct, "
        "expires_at, created_at) VALUES (?,?,?,?,?,?)",
        (sub_id, user_id, resource, ct, expires_at, time.time()))
    c.commit()
    c.close()


def subscription(sub_id):
    """The subscription with clientState DECRYPTED for a constant-time compare
    in process_notification — internal only, never returned by a route. Returns
    None on InvalidToken (rotated secret → fail closed → ignore the notification)."""
    c = db._conn()
    r = c.execute("SELECT * FROM graph_subscriptions WHERE id=?", (sub_id,)).fetchone()
    c.close()
    if not r:
        return None
    d = dict(r)
    try:
        d["client_state"] = _fernet().decrypt(d["client_state_ct"].encode()).decode()
    except InvalidToken:
        return None
    d.pop("client_state_ct", None)
    return d


def subscriptions_for(user_id):
    """Non-secret subscription metadata for renewal/teardown — no ciphertext."""
    c = db._conn()
    rows = c.execute(
        "SELECT id, user_id, resource, expires_at, created_at "
        "FROM graph_subscriptions WHERE user_id=?", (user_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def delete_subscription(sub_id):
    c = db._conn()
    c.execute("DELETE FROM graph_subscriptions WHERE id=?", (sub_id,))
    c.commit()
    c.close()


def delete_subscriptions(user_id):
    c = db._conn()
    c.execute("DELETE FROM graph_subscriptions WHERE user_id=?", (user_id,))
    c.commit()
    c.close()


# ── Revoke / disconnect ──────────────────────────────────────────────────

def revoke(user_id):
    """Wipe tokens, drop all subscriptions, mark the account revoked. Fail
    closed: no plaintext token survives, and claim_due skips revoked rows until
    the user reconnects."""
    now = time.time()
    c = db._conn()
    c.execute(
        "UPDATE graph_accounts SET access_ct=NULL, refresh_ct=NULL, "
        "token_expires_at=NULL, status=?, next_run_at=NULL, claimed_at=NULL, "
        "updated_at=? WHERE user_id=?", ("revoked", now, user_id))
    c.execute("DELETE FROM graph_subscriptions WHERE user_id=?", (user_id,))
    c.commit()
    c.close()


# ── Public status (non-secret metadata only) ─────────────────────────────

def public_status(user_id):
    """Everything /m365/status needs and NOTHING secret — no ciphertext, no
    token, no delta cursor. last_sync is the account's updated_at (bumped only on
    a successful run + token/save writes)."""
    acct = get_account(user_id)
    if not acct:
        return {"connected": False, "mode": None, "status": None,
                "last_sync": None, "item_count": 0}
    c = db._conn()
    r = c.execute("SELECT COUNT(*) AS n FROM graph_items WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return {
        "connected": acct.get("status") not in (None, "revoked"),
        "mode": acct.get("auth_mode"),
        "status": acct.get("status"),
        "last_sync": acct.get("updated_at"),
        "item_count": (r["n"] if r else 0),
    }
