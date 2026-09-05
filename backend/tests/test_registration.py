"""Self-registration is a gate, not a formality.

Proves: production mode refuses an unauthenticated /auth/register (403, and no
row is left behind); STUDIO_OPEN_REGISTRATION re-opens it either way; an
account created that way is UNVERIFIED and carries no token, so /auth/login and
current_user both refuse it until /auth/verify-email accepts the emailed code;
and SSO provisioning is untouched by all of it (verified=1, no local password).

Run from the backend directory:
    python -m pytest tests/test_registration.py -q
"""
import os
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths (each test repoints
# db.DB_PATH anyway; this only matters if this module imports app.db first).
_TMP = tempfile.mkdtemp(prefix="studio-registration-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest
from fastapi import HTTPException

from app import auth, bootstrap, db

STRONG = "x" * 20 + "-" + "y" * 25  # 46 chars, not a placeholder
PASSWORD = "long-enough-password"


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Isolated DB per test, env scrubbed of every flag registration reads, and
    a usable signing secret so make_token() works in production mode too."""
    path = str(tmp_path / "reg.db")
    monkeypatch.setenv("STUDIO_DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    for k in ("STUDIO_SECRET", "STUDIO_DEMO_MODE", "STUDIO_OPEN_REGISTRATION",
              "STUDIO_ADMIN_EMAIL", "STUDIO_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(auth, "SECRET", STRONG)
    return monkeypatch


def _prod(mp):
    mp.delenv("STUDIO_DEMO_MODE", raising=False)
    mp.setenv("STUDIO_SECRET", STRONG)


def _register(email="new@example.com", password=PASSWORD, name="New"):
    return auth.register(auth.Register(email=email, password=password, name=name))


def _bearer(token):
    return type("Cred", (), {"credentials": token})()


def _code_for(email):
    """The 6-digit code register() actually stored — read from the DB rather
    than from the outbox so the test does not depend on mail delivery."""
    c = db._conn()
    row = c.execute("SELECT code FROM email_codes WHERE email=?", (email,)).fetchone()
    c.close()
    return row["code"]


# ── The gate ─────────────────────────────────────────────────────────────

def test_production_registration_is_closed_and_leaves_no_user(fresh):
    _prod(fresh)
    db.init_db()
    with pytest.raises(HTTPException) as ei:
        _register()
    assert ei.value.status_code == 403
    assert "Self-registration is disabled" in ei.value.detail
    assert db.get_user_by_email("new@example.com") is None


def test_demo_mode_still_allows_registration(fresh):
    fresh.setenv("STUDIO_DEMO_MODE", "1")
    db.init_db()
    assert bootstrap.open_registration() is True
    assert _register()["registered"] is True
    assert db.get_user_by_email("new@example.com") is not None


@pytest.mark.parametrize("value", ["1", "true", "YES"])
def test_env_opens_registration_in_production(fresh, value):
    _prod(fresh)
    fresh.setenv("STUDIO_OPEN_REGISTRATION", value)
    assert bootstrap.open_registration() is True


@pytest.mark.parametrize("value", ["0", "false", "off", ""])
def test_env_closes_registration_in_demo_mode(fresh, value):
    fresh.setenv("STUDIO_DEMO_MODE", "1")
    fresh.setenv("STUDIO_OPEN_REGISTRATION", value)
    # An empty/whitespace value is "unset" — it falls back to the mode default.
    assert bootstrap.open_registration() is (value == "")


def test_sso_status_advertises_whether_signup_is_open(fresh):
    """The login screen asks once and hides the Register link in production."""
    _prod(fresh)
    assert auth.sso_status()["open_registration"] is False
    fresh.setenv("STUDIO_OPEN_REGISTRATION", "1")
    assert auth.sso_status()["open_registration"] is True


# ── What registration hands back ─────────────────────────────────────────

def test_registration_creates_an_unverified_account_with_no_token(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_OPEN_REGISTRATION", "1")
    db.init_db()
    out = _register()
    assert out["registered"] is True and out["email"] == "new@example.com"
    assert "access_token" not in out and "user" not in out
    assert "verification" in out
    assert db.get_user_by_email("new@example.com")["verified"] == 0


def test_unverified_account_cannot_log_in_until_verified(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_OPEN_REGISTRATION", "1")
    db.init_db()
    _register()

    with pytest.raises(HTTPException) as ei:
        auth.login(auth.Credentials(email="new@example.com", password=PASSWORD))
    assert ei.value.status_code == 403 and "Verify your email" in ei.value.detail

    auth.verify_email(auth.Verify(email="new@example.com", code=_code_for("new@example.com")))
    out = auth.login(auth.Credentials(email="new@example.com", password=PASSWORD))
    assert out["access_token"] and out["user"]["role"] == "viewer"


def test_token_for_an_unverified_account_is_rejected_by_current_user(fresh):
    """Verification is re-checked per request, so a token minted any other way
    (an older build, a still-valid token) cannot outlive the gate."""
    _prod(fresh)
    fresh.setenv("STUDIO_OPEN_REGISTRATION", "1")
    db.init_db()
    _register()
    user = db.get_user_by_email("new@example.com")
    token = auth.make_token(user)

    with pytest.raises(HTTPException) as ei:
        auth.current_user(_bearer(token))
    assert ei.value.status_code == 403 and "Verify your email" in ei.value.detail

    db.mark_verified("new@example.com")
    assert auth.current_user(_bearer(token))["email"] == "new@example.com"


def test_expired_code_does_not_verify(fresh):
    """The code TTL still applies — verify_email is the only way in."""
    _prod(fresh)
    fresh.setenv("STUDIO_OPEN_REGISTRATION", "1")
    db.init_db()
    db.create_user("new@example.com", PASSWORD, "New", role="viewer", verified=0)
    db.set_email_code("new@example.com", "123456")
    assert db.check_email_code("new@example.com", "123456", max_age=0) is False
    with pytest.raises(HTTPException) as ei:
        auth.verify_email(auth.Verify(email="new@example.com", code="000000"))
    assert ei.value.status_code == 400
    assert db.get_user_by_email("new@example.com")["verified"] == 0


# ── SSO is unaffected ────────────────────────────────────────────────────

def test_sso_upsert_stays_verified_and_has_no_local_password(fresh):
    _prod(fresh)  # registration closed; SSO provisioning still works
    db.init_db()
    user = db.upsert_sso_user("emp@corp.com", "Emp", "analyst")
    assert user["verified"] == 1 and user["role"] == "analyst"
    assert not db.has_usable_password(user)
    # An SSO account is a usable session immediately.
    assert auth.current_user(_bearer(auth.make_token(user)))["email"] == "emp@corp.com"


def test_create_user_defaults_to_verified(fresh):
    """Every pre-existing caller keeps creating usable accounts."""
    _prod(fresh)
    db.init_db()
    db.create_user("ops@example.com", PASSWORD, "Ops", role="admin")
    assert db.get_user_by_email("ops@example.com")["verified"] == 1
