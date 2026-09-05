"""Per-user provider API keys — bring your own key.

Keys are encrypted at rest with a Fernet key derived from STUDIO_SECRET, and
are never returned to a client: the API exposes only the provider, the last
four characters, and when it was added. A user's key is used in place of the
server's for that user's own requests; nothing else changes.

Rotating STUDIO_SECRET makes stored keys undecryptable. That fails closed —
the user is asked to re-enter, and no plaintext is recoverable from a stolen
database alone.
"""
import base64
import os
import time
import uuid

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import db

PROVIDERS = ("anthropic", "openai")
_SALT = b"studio-user-api-keys-v1"


def _fernet():
    # Same secret as the JWT signer (bootstrap.jwt_secret): STUDIO_SECRET, an
    # ephemeral per-process value in demo mode, never a known placeholder. A
    # missing secret fails closed rather than encrypting with a public string.
    from . import bootstrap
    secret = (bootstrap.jwt_secret() or "").encode()
    if not secret:
        raise RuntimeError("STUDIO_SECRET is not set; cannot derive the encryption key")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret)))


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            ciphertext TEXT NOT NULL,
            last4 TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_user_api_keys_one
            ON user_api_keys(user_id, provider);
        """
    )
    c.commit()
    c.close()


def set_key(user_id, provider, api_key):
    token = _fernet().encrypt(api_key.encode()).decode()
    c = db._conn()
    c.execute("DELETE FROM user_api_keys WHERE user_id=? AND provider=?", (user_id, provider))
    c.execute(
        "INSERT INTO user_api_keys (id, user_id, provider, ciphertext, last4, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), user_id, provider, token, api_key[-4:], time.time()),
    )
    c.commit()
    c.close()


def get_key(user_id, provider):
    """Decrypted key, or None. Never log or return this to a client."""
    c = db._conn()
    row = c.execute(
        "SELECT ciphertext FROM user_api_keys WHERE user_id=? AND provider=?",
        (user_id, provider)).fetchone()
    c.close()
    if not row:
        return None
    try:
        return _fernet().decrypt(row["ciphertext"].encode()).decode()
    except InvalidToken:
        return None  # STUDIO_SECRET rotated — treat as absent


def list_keys(user_id):
    c = db._conn()
    rows = c.execute(
        "SELECT provider, last4, created_at FROM user_api_keys WHERE user_id=? ORDER BY provider",
        (user_id,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def delete_key(user_id, provider):
    c = db._conn()
    c.execute("DELETE FROM user_api_keys WHERE user_id=? AND provider=?", (user_id, provider))
    c.commit()
    c.close()


def verify(provider, api_key):
    """Cheapest real call that proves the key works. (ok, detail).

    Checked before storing, so a typo is rejected at the point of entry rather
    than surfacing later as a broken agent.
    """
    try:
        if provider == "anthropic":
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "hi"}]},
                timeout=20)
        elif provider == "openai":
            r = requests.get("https://api.openai.com/v1/models",
                             headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
        else:
            return False, f"Unknown provider '{provider}'"
    except Exception as e:
        return False, f"Could not reach {provider}: {e}"

    if r.status_code in (200, 201):
        return True, "ok"
    if r.status_code in (401, 403):
        return False, "That key was rejected by the provider."
    if r.status_code == 429:
        # Authentication succeeded; the account simply has no balance. Storing
        # it would strand the user in fallback, so refuse with the real reason.
        return False, "Key is valid but the account has no credit or is rate limited."
    return False, f"{provider} returned {r.status_code}: {r.text[:120]}"
