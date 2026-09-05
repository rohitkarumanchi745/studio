"""The env-driven shared login — one credential a public deploy hands out.

Why it exists: production has no way to MAKE a login. Self-registration is off
by default, and with no SMTP the 6-digit verification code only ever lands in
backend/outbox/ inside the container, so a tester who registered could never
sign in. STUDIO_SHARED_LOGIN_EMAIL/_PASSWORD create ONE pre-verified account at
boot instead — without re-opening signup.

Proves: the account is created pre-verified and logs in immediately in
PRODUCTION mode with no SMTP; the role defaults to analyst, an explicit role is
honoured and an unknown one refuses the boot naming the valid roles; a missing
or short password refuses the boot; the ENVIRONMENT owns the credential, so
rotating STUDIO_SHARED_LOGIN_PASSWORD kills the old password and installs the
new one and changing STUDIO_SHARED_LOGIN_ROLE re-roles the account (this is the
deliberate difference from ensure_bootstrap_admin, which never touches a
password); an admin-role shared login in production logs a loud warning but
still boots; GET /auth/sso hides the address by default and reveals the EMAIL
ONLY when STUDIO_SHARED_LOGIN_SHOW is on; the account is an ordinary account
for RBAC (the gateway refuses it a table its role is denied, and grants it one
its role allows); and an unset email is a no-op.

Run from the backend directory:
    python -m pytest tests/test_shared_login.py -q
"""
import os
import sqlite3
import tempfile

# Throwaway SQLite BEFORE app modules compute their paths (each test repoints
# db.DB_PATH anyway; this only matters if this module imports app.db first).
_TMP = tempfile.mkdtemp(prefix="studio-shared-login-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest

from app import auth, bootstrap, db, gateway
from app.queryguard import QueryRejected

STRONG = "x" * 20 + "-" + "y" * 25          # 46 chars, not a placeholder
SHARED = "testers@studio.example"
PASSWORD = "shared-tester-password-1"       # >= MIN_ADMIN_PASSWORD_LEN

# Env this feature reads, scrubbed per test so nothing leaks into the rest of
# the suite (conftest runs the whole suite in demo mode).
SHARED_ENV = ("STUDIO_SHARED_LOGIN_EMAIL", "STUDIO_SHARED_LOGIN_PASSWORD",
              "STUDIO_SHARED_LOGIN_ROLE", "STUDIO_SHARED_LOGIN_SHOW")


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Isolated DB per test, env scrubbed of everything bootstrap reads, and a
    usable signing secret in auth so make_token() works in production mode."""
    path = str(tmp_path / "shared.db")
    monkeypatch.setenv("STUDIO_DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    for k in ("STUDIO_SECRET", "STUDIO_DEMO_MODE", "STUDIO_ADMIN_EMAIL",
              "STUDIO_ADMIN_PASSWORD", "STUDIO_OPEN_REGISTRATION",
              "STUDIO_TOOL_RUNNER", "STUDIO_TOOL_RUNNER_ALLOW_PROCESS",
              "STUDIO_TOOLBUILDER") + SHARED_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(auth, "SECRET", STRONG)
    return monkeypatch


def _prod(mp):
    """A production boot that satisfies the OTHER gates (secret, tool runner),
    so anything these tests see refused is refused by the shared login."""
    mp.delenv("STUDIO_DEMO_MODE", raising=False)
    mp.setenv("STUDIO_SECRET", STRONG)
    mp.setenv("STUDIO_TOOL_RUNNER", "docker")


def _shared(mp, *, email=SHARED, password=PASSWORD, role=None, show=None):
    mp.setenv("STUDIO_SHARED_LOGIN_EMAIL", email)
    if password is not None:
        mp.setenv("STUDIO_SHARED_LOGIN_PASSWORD", password)
    if role is not None:
        mp.setenv("STUDIO_SHARED_LOGIN_ROLE", role)
    if show is not None:
        mp.setenv("STUDIO_SHARED_LOGIN_SHOW", show)


def _boot(mp, **kw):
    _prod(mp)
    _shared(mp, **kw)
    db.init_db()
    bootstrap.enforce()
    return db.get_user_by_email(kw.get("email", SHARED))


# ── Created pre-verified, usable with no SMTP ────────────────────────────

def test_shared_login_is_created_preverified_and_can_log_in_in_production(fresh):
    """The whole point: no SMTP, no verification code, no open registration —
    and the tester can still sign in the moment the container is up."""
    user = _boot(fresh)
    assert user is not None
    assert user["verified"] == 1
    assert db.verify_password(PASSWORD, user["password_hash"])
    # Self-registration stays shut: this is not a signup back door.
    assert bootstrap.open_registration() is False

    out = auth.login(auth.Credentials(email=SHARED, password=PASSWORD))
    assert out["access_token"]
    assert out["user"]["email"] == SHARED and out["user"]["role"] == "analyst"


def test_login_email_is_normalised_to_lowercase(fresh):
    user = _boot(fresh, email="Testers@Studio.Example")
    assert db.get_user_by_email("testers@studio.example") is not None
    assert user is None or user["email"] == "testers@studio.example"
    auth.login(auth.Credentials(email="TESTERS@studio.example", password=PASSWORD))


def test_wrong_password_is_still_refused(fresh):
    _boot(fresh)
    with pytest.raises(Exception) as ei:
        auth.login(auth.Credentials(email=SHARED, password="not-the-password"))
    assert getattr(ei.value, "status_code", None) == 401


def test_shared_login_also_runs_in_demo_mode(fresh):
    fresh.setenv("STUDIO_DEMO_MODE", "1")
    _shared(fresh)
    db.init_db()
    bootstrap.enforce()
    assert db.get_user_by_email(SHARED)["verified"] == 1


# ── Role ─────────────────────────────────────────────────────────────────

def test_default_role_is_analyst(fresh):
    assert _boot(fresh)["role"] == "analyst"


@pytest.mark.parametrize("role", ["viewer", "analyst", "admin"])
def test_explicit_role_is_honoured(fresh, role):
    assert _boot(fresh, role=role)["role"] == role


def test_unknown_role_refuses_boot_and_names_the_valid_ones(fresh):
    from app.policies import POLICIES
    _prod(fresh)
    _shared(fresh, role="superuser")
    db.init_db()
    with pytest.raises(RuntimeError) as ei:
        bootstrap.enforce()
    msg = str(ei.value)
    assert "STUDIO_SHARED_LOGIN_ROLE" in msg and "superuser" in msg
    for role in POLICIES:                       # the real table, not a copy
        assert role in msg
    assert db.get_user_by_email(SHARED) is None  # nothing half-created


def test_valid_roles_come_from_the_policy_table_not_a_hardcoded_list(fresh, monkeypatch):
    """Add a role to policies.POLICIES and the env var accepts it."""
    from app import policies
    monkeypatch.setitem(policies.POLICIES, "auditor", {"demo": "*"})
    assert _boot(fresh, role="auditor")["role"] == "auditor"


# ── Password is required, and long ───────────────────────────────────────

def test_missing_password_refuses_boot(fresh):
    _prod(fresh)
    _shared(fresh, password=None)
    db.init_db()
    with pytest.raises(RuntimeError) as ei:
        bootstrap.enforce()
    assert "STUDIO_SHARED_LOGIN_PASSWORD" in str(ei.value)
    assert db.get_user_by_email(SHARED) is None


def test_short_password_refuses_boot(fresh):
    _prod(fresh)
    _shared(fresh, password="x" * (bootstrap.MIN_ADMIN_PASSWORD_LEN - 1))
    db.init_db()
    with pytest.raises(RuntimeError) as ei:
        bootstrap.enforce()
    msg = str(ei.value)
    assert "STUDIO_SHARED_LOGIN_PASSWORD" in msg
    assert str(bootstrap.MIN_ADMIN_PASSWORD_LEN) in msg
    assert db.get_user_by_email(SHARED) is None


# ── The ENVIRONMENT owns this credential (unlike the bootstrap admin) ────

def test_rotating_the_password_rotates_the_credential(fresh, caplog):
    """ensure_bootstrap_admin never rewrites a stored password; this account
    belongs to the deployment, so the env var IS the password."""
    first = _boot(fresh)["password_hash"]
    fresh.setenv("STUDIO_SHARED_LOGIN_PASSWORD", "rotated-tester-password-2")
    with caplog.at_level("INFO", logger="studio.bootstrap"):
        bootstrap.enforce()
    user = db.get_user_by_email(SHARED)
    assert user["password_hash"] != first
    assert not db.verify_password(PASSWORD, user["password_hash"])       # old dead
    assert db.verify_password("rotated-tester-password-2", user["password_hash"])
    assert any("password reset" in r.getMessage() for r in caplog.records)

    with pytest.raises(Exception) as ei:
        auth.login(auth.Credentials(email=SHARED, password=PASSWORD))
    assert getattr(ei.value, "status_code", None) == 401
    assert auth.login(auth.Credentials(
        email=SHARED, password="rotated-tester-password-2"))["access_token"]


def test_unchanged_password_is_not_rewritten_on_every_boot(fresh):
    """A boot that changes nothing writes nothing (and does not invalidate the
    hash tests above by churning bcrypt salts)."""
    first = _boot(fresh)["password_hash"]
    bootstrap.enforce()
    assert db.get_user_by_email(SHARED)["password_hash"] == first


def test_changing_the_role_updates_the_account(fresh, caplog):
    assert _boot(fresh)["role"] == "analyst"
    fresh.setenv("STUDIO_SHARED_LOGIN_ROLE", "viewer")
    with caplog.at_level("INFO", logger="studio.bootstrap"):
        bootstrap.enforce()
    assert db.get_user_by_email(SHARED)["role"] == "viewer"
    assert any("analyst" in r.getMessage() and "viewer" in r.getMessage()
               for r in caplog.records)


def test_an_unverified_account_on_that_address_is_verified(fresh):
    """Someone registered the address first (demo mode, verification pending):
    the deployment owns this identity, so the boot makes it usable."""
    _prod(fresh)
    _shared(fresh)
    db.init_db()
    db.create_user(SHARED, "whatever-they-chose", "Testers", role="viewer", verified=0)
    bootstrap.enforce()
    user = db.get_user_by_email(SHARED)
    assert user["verified"] == 1 and user["role"] == "analyst"
    assert db.verify_password(PASSWORD, user["password_hash"])
    assert auth.login(auth.Credentials(email=SHARED, password=PASSWORD))["access_token"]


def test_creation_tolerates_a_concurrent_duplicate(fresh):
    """Two replicas booting at once: the loser's INSERT raises a unique
    violation and must re-sync instead of crashing the container."""
    _prod(fresh)
    _shared(fresh)
    db.init_db()
    real_create = db.create_user

    def racing(email, password, name, role="viewer", verified=1):
        real_create(email, "other-replica-password", name, role="viewer", verified=0)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.email")

    fresh.setattr(db, "create_user", racing)
    bootstrap.enforce()                     # must not raise
    user = db.get_user_by_email(SHARED)
    assert user["role"] == "analyst" and user["verified"] == 1
    assert db.verify_password(PASSWORD, user["password_hash"])


# ── The admin foot-gun is loud, not fatal ────────────────────────────────

def test_admin_shared_login_in_production_warns_but_boots(fresh, caplog):
    with caplog.at_level("WARNING", logger="studio.bootstrap"):
        user = _boot(fresh, role="admin")
    assert user["role"] == "admin"          # the boot was NOT refused
    warned = " ".join(r.getMessage() for r in caplog.records
                      if r.levelname == "WARNING").lower()
    assert SHARED in warned
    for phrase in ("approve", "governance", "mcp", "activity", "studio_toolbuilder=0"):
        assert phrase in warned


def test_non_admin_shared_login_does_not_warn(fresh, caplog):
    with caplog.at_level("WARNING", logger="studio.bootstrap"):
        _boot(fresh, role="analyst")
    assert not [r for r in caplog.records
                if r.levelname == "WARNING" and "SHARED ADMIN" in r.getMessage()]


def test_admin_shared_login_warns_again_on_a_later_boot(fresh, caplog):
    """The warning is about the credential, not about creating it — a restart
    of a long-lived deploy must still say it."""
    _boot(fresh, role="admin")
    with caplog.at_level("WARNING", logger="studio.bootstrap"):
        bootstrap.enforce()
    assert any("SHARED ADMIN" in r.getMessage() for r in caplog.records)


# ── Discoverability: the EMAIL only, and only when asked for ─────────────

def test_sso_payload_hides_the_shared_login_by_default(fresh):
    _boot(fresh)
    payload = auth.sso_status()
    assert payload["shared_login"] is None
    assert "shared_login" in payload            # the key is always present


def test_sso_payload_reveals_the_email_when_show_is_on(fresh):
    _boot(fresh, show="1")
    payload = auth.sso_status()
    assert payload["shared_login"] == {"email": SHARED, "role": "analyst"}
    # Never the password, under any key, anywhere in the response.
    assert PASSWORD not in repr(payload)
    assert "password" not in repr(payload).lower()


def test_sso_payload_is_null_when_no_shared_login_exists(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SHARED_LOGIN_SHOW", "1")   # on, but no account
    db.init_db()
    bootstrap.enforce()
    assert auth.sso_status()["shared_login"] is None


def test_sso_payload_survives_a_role_that_stopped_being_valid(fresh):
    """The env changed under a running process; the login page reports nothing
    rather than 500-ing."""
    _boot(fresh, show="1")
    fresh.setenv("STUDIO_SHARED_LOGIN_ROLE", "superuser")
    assert auth.sso_status()["shared_login"] is None


# ── An ordinary account: no RBAC bypass ──────────────────────────────────

def test_shared_login_is_subject_to_normal_rbac(fresh):
    """A viewer-role shared login is refused demo.customers exactly like any
    other viewer — the gateway knows nothing about how the account was made."""
    user = _boot(fresh, role="viewer")
    with pytest.raises(QueryRejected):
        gateway.execute(user, "demo", "SELECT * FROM customers", "test",
                        table_label="customers")


def test_shared_login_gets_exactly_what_its_role_allows(fresh):
    """The mirror image: the same account reaches a table its role DOES grant,
    so the test above is proving RBAC and not a broken account."""
    user = _boot(fresh, role="viewer")
    _connector, allowed, _sql = gateway.check(
        user, "demo", "SELECT * FROM sales", table_label="sales")
    assert "sales" in allowed and "customers" not in allowed


def test_promoting_the_shared_login_widens_access_only_via_its_role(fresh):
    user = _boot(fresh, role="analyst")
    _connector, allowed, _sql = gateway.check(
        user, "demo", "SELECT * FROM customers", table_label="customers")
    assert "customers" in allowed


# ── Off by default ───────────────────────────────────────────────────────

def test_unset_email_is_a_no_op(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SHARED_LOGIN_PASSWORD", PASSWORD)   # set but unused
    db.init_db()
    bootstrap.enforce()
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
    assert bootstrap.shared_login_public() is None


def test_blank_email_is_a_no_op(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SHARED_LOGIN_EMAIL", "   ")
    db.init_db()
    bootstrap.enforce()                      # no password required either
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0
