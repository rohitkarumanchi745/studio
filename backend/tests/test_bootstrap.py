"""Boot-time credential gate — a fresh deploy never runs with public creds.

Proves: production mode refuses to boot without a strong STUDIO_SECRET; demo
mode gets a stable per-process secret; init_db() seeds *@studio.local ONLY in
demo mode; a pre-existing seed account on its default password is revoked on
production boot; STUDIO_ADMIN_EMAIL creates the first admin and promotes an
existing owner of that address ONLY with proof of control (SSO-provisioned, or
a matching STUDIO_ADMIN_PASSWORD); that production REFUSES the 'process' tool
runner, which would run approved generated code with the app's own filesystem,
network and credentials, unless the operator disables built tools or opts in
explicitly; and auth fails closed (500) when no secret is available.
Self-registration lives in test_registration.py.

Run from the backend directory:
    python -m pytest tests/test_bootstrap.py -q
"""
import os
import sqlite3
import tempfile
import time
import uuid

# Throwaway SQLite BEFORE app modules compute their paths (only matters when
# this module is the first to import app.db; each test repoints DB_PATH anyway).
_TMP = tempfile.mkdtemp(prefix="studio-bootstrap-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import pytest
from fastapi import HTTPException

from app import auth, bootstrap, db

STRONG = "x" * 20 + "-" + "y" * 25  # 46 chars, not a placeholder


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """Isolated DB per test, env scrubbed of everything bootstrap reads.
    Repoints db.DB_PATH directly (no reload) so other modules' state and the
    seeded suite-wide DB are untouched."""
    path = str(tmp_path / "boot.db")
    monkeypatch.setenv("STUDIO_DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    for k in ("STUDIO_SECRET", "STUDIO_DEMO_MODE", "STUDIO_ADMIN_EMAIL",
              "STUDIO_ADMIN_PASSWORD", "STUDIO_OPEN_REGISTRATION",
              "STUDIO_TOOL_RUNNER", "STUDIO_TOOL_RUNNER_ALLOW_PROCESS",
              "STUDIO_TOOLBUILDER"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _prod(mp):
    mp.delenv("STUDIO_DEMO_MODE", raising=False)
    # A production boot must also satisfy the tool-runner gate; the tests that
    # are ABOUT that gate override this line.
    mp.setenv("STUDIO_TOOL_RUNNER", "docker")


def _demo(mp):
    mp.setenv("STUDIO_DEMO_MODE", "1")


def _insert_seed_admin():
    """Simulate a DB seeded by an older init_db() that always created admin."""
    c = db._conn()
    c.execute(
        "INSERT INTO users (id, email, password_hash, name, role, created_at) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), "admin@studio.local", db.hash_password("admin123"),
         "Admin", "admin", time.time()))
    c.commit()
    c.close()


# ── Secret gate ──────────────────────────────────────────────────────────

def test_production_without_secret_refuses_boot(fresh):
    _prod(fresh)
    db.init_db()
    with pytest.raises(RuntimeError) as ei:
        bootstrap.enforce()
    msg = str(ei.value)
    assert "STUDIO_SECRET" in msg and "32" in msg and "STUDIO_DEMO_MODE=1" in msg


@pytest.mark.parametrize("weak", ["dev-secret-change-me", "changeme", "SECRET", "short-but-random-19"])
def test_production_with_weak_secret_refuses_boot(fresh, weak):
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", weak)
    db.init_db()
    with pytest.raises(RuntimeError):
        bootstrap.enforce()


def test_production_with_strong_secret_boots(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    bootstrap.enforce()
    assert bootstrap.jwt_secret() == STRONG


def test_demo_mode_without_secret_gets_stable_ephemeral_secret(fresh):
    _demo(fresh)
    fresh.setattr(bootstrap, "_DEMO_SECRET", None)
    first = bootstrap.jwt_secret()
    assert first and len(first) >= 32
    assert bootstrap.jwt_secret() == first
    db.init_db()
    bootstrap.enforce()  # no secret required in demo mode


def test_production_jwt_secret_is_none_when_unset(fresh):
    _prod(fresh)
    assert bootstrap.jwt_secret() is None


# ── Seeding ──────────────────────────────────────────────────────────────

def test_init_db_production_creates_no_seed_users(fresh):
    _prod(fresh)
    db.init_db()
    c = db._conn()
    rows = c.execute("SELECT email FROM users WHERE email LIKE '%@studio.local'").fetchall()
    c.close()
    assert rows == []


def test_init_db_demo_seeds_all_three(fresh):
    _demo(fresh)
    db.init_db()
    for email, pw, _n, role in bootstrap.SEED_USERS:
        u = db.get_user_by_email(email)
        assert u and u["role"] == role and db.verify_password(pw, u["password_hash"])


def test_production_boot_revokes_default_seed_password(fresh, caplog):
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    _insert_seed_admin()
    assert db.verify_password("admin123", db.get_user_by_email("admin@studio.local")["password_hash"])
    with caplog.at_level("WARNING", logger="studio.bootstrap"):
        bootstrap.enforce()
    user = db.get_user_by_email("admin@studio.local")
    assert user is not None                      # account kept, password gone
    assert not db.verify_password("admin123", user["password_hash"])
    assert any("admin@studio.local" in r.getMessage() for r in caplog.records)


def test_revocation_leaves_changed_seed_password_alone(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    db.create_user("admin@studio.local", "operator-chose-this-1", "Admin", role="admin")
    bootstrap.enforce()
    assert db.verify_password("operator-chose-this-1",
                              db.get_user_by_email("admin@studio.local")["password_hash"])


# ── Bootstrap admin ──────────────────────────────────────────────────────

def test_admin_env_creates_verified_admin(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    fresh.setenv("STUDIO_ADMIN_EMAIL", "Ops@Example.com")
    fresh.setenv("STUDIO_ADMIN_PASSWORD", "twelve-chars")
    db.init_db()
    bootstrap.enforce()
    u = db.get_user_by_email("ops@example.com")
    assert u and u["role"] == "admin" and u["verified"] == 1
    assert db.verify_password("twelve-chars", u["password_hash"])


def test_admin_env_short_password_refuses_boot(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    fresh.setenv("STUDIO_ADMIN_EMAIL", "ops@example.com")
    fresh.setenv("STUDIO_ADMIN_PASSWORD", "elevenchars")
    db.init_db()
    with pytest.raises(RuntimeError) as ei:
        bootstrap.enforce()
    assert "STUDIO_ADMIN_PASSWORD" in str(ei.value)
    assert db.get_user_by_email("ops@example.com") is None


def test_admin_env_promotes_existing_user_when_password_matches(fresh):
    """The operator proves control of the account by supplying its password;
    the stored hash is still never rewritten."""
    _demo(fresh)  # (iii) also runs in demo mode
    fresh.setenv("STUDIO_ADMIN_EMAIL", "ops@example.com")
    fresh.setenv("STUDIO_ADMIN_PASSWORD", "her-own-password-1")
    db.init_db()
    db.create_user("ops@example.com", "her-own-password-1", "Ops", role="viewer")
    bootstrap.enforce()
    u = db.get_user_by_email("ops@example.com")
    assert u["role"] == "admin"
    assert db.verify_password("her-own-password-1", u["password_hash"])


def test_admin_env_refuses_to_promote_a_password_account_it_cannot_prove(fresh):
    """The attack this closes: a self-registered viewer squats the address in
    STUDIO_ADMIN_EMAIL and gets admin handed to them on the next boot."""
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    fresh.setenv("STUDIO_ADMIN_EMAIL", "ops@example.com")
    db.init_db()
    db.create_user("ops@example.com", "squatter-password-1", "Squatter", role="viewer")
    with pytest.raises(RuntimeError) as ei:
        bootstrap.enforce()
    msg = str(ei.value)
    assert "ops@example.com" in msg and "STUDIO_ADMIN_EMAIL" in msg
    assert db.get_user_by_email("ops@example.com")["role"] == "viewer"


def test_admin_env_refuses_when_password_is_wrong(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    fresh.setenv("STUDIO_ADMIN_EMAIL", "ops@example.com")
    fresh.setenv("STUDIO_ADMIN_PASSWORD", "not-her-password")
    db.init_db()
    db.create_user("ops@example.com", "her-own-password-1", "Ops", role="viewer")
    with pytest.raises(RuntimeError):
        bootstrap.enforce()
    assert db.get_user_by_email("ops@example.com")["role"] == "viewer"


def test_admin_env_promotes_sso_provisioned_account_without_password(fresh):
    """An SSO account has no local password anyone could be holding, so the
    identity provider is the proof of control."""
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    fresh.setenv("STUDIO_ADMIN_EMAIL", "ops@example.com")
    db.init_db()
    db.upsert_sso_user("ops@example.com", "Ops", "viewer")
    bootstrap.enforce()
    u = db.get_user_by_email("ops@example.com")
    assert u["role"] == "admin" and u["verified"] == 1
    assert not db.has_usable_password(u)


def test_admin_creation_tolerates_a_concurrent_duplicate(fresh):
    """Two replicas booting at once: the loser's INSERT raises a unique
    violation and must re-read instead of crashing the container."""
    _prod(fresh)
    fresh.setenv("STUDIO_SECRET", STRONG)
    fresh.setenv("STUDIO_ADMIN_EMAIL", "ops@example.com")
    fresh.setenv("STUDIO_ADMIN_PASSWORD", "twelve-chars")
    db.init_db()
    real_create = db.create_user

    def racing(email, password, name, role="viewer", verified=1):
        real_create(email, password, name, role="viewer", verified=verified)  # other replica
        raise sqlite3.IntegrityError("UNIQUE constraint failed: users.email")

    fresh.setattr(db, "create_user", racing)
    bootstrap.enforce()  # must not raise
    u = db.get_user_by_email("ops@example.com")
    assert u["role"] == "admin"
    assert db.verify_password("twelve-chars", u["password_hash"])


# ── Tool runner gate ─────────────────────────────────────────────────────
# The process runner launches approved, model-generated MCP servers as the
# app's own uid: it can read backend/.env and studio.db and reach the network.
# Fine on a laptop, never in production without the operator saying so.

def test_production_with_unset_tool_runner_refuses_boot(fresh):
    _prod(fresh)
    fresh.delenv("STUDIO_TOOL_RUNNER", raising=False)     # the real-world case
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    with pytest.raises(RuntimeError) as ei:
        bootstrap.enforce()
    msg = str(ei.value)
    assert "STUDIO_TOOL_RUNNER=docker" in msg
    assert "STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1" in msg


def test_production_with_explicit_process_runner_refuses_boot(fresh):
    _prod(fresh)
    fresh.setenv("STUDIO_TOOL_RUNNER", "process")
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    with pytest.raises(RuntimeError):
        bootstrap.enforce()


def test_production_process_runner_boots_with_explicit_opt_in(fresh, caplog):
    """The documented escape hatch — and it must be loud in the log."""
    _prod(fresh)
    fresh.delenv("STUDIO_TOOL_RUNNER", raising=False)
    fresh.setenv("STUDIO_TOOL_RUNNER_ALLOW_PROCESS", "1")
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    with caplog.at_level("WARNING", logger="studio.bootstrap"):
        bootstrap.enforce()
    assert any("STUDIO_TOOL_RUNNER_ALLOW_PROCESS" in r.getMessage() for r in caplog.records)


def test_production_process_runner_boots_when_tool_builder_is_off(fresh):
    """With built tools switched off nothing generated can be registered or
    launched, so the runner's privileges no longer matter."""
    _prod(fresh)
    fresh.delenv("STUDIO_TOOL_RUNNER", raising=False)
    fresh.setenv("STUDIO_TOOLBUILDER", "0")
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    bootstrap.enforce()
    assert bootstrap.tool_builder_enabled() is False


def test_docker_runner_boots_in_production(fresh):
    _prod(fresh)                       # sets STUDIO_TOOL_RUNNER=docker
    fresh.setenv("STUDIO_SECRET", STRONG)
    db.init_db()
    bootstrap.enforce()


def test_demo_mode_keeps_the_process_runner(fresh):
    """The dev default is untouched: a laptop demo boots with no tool-runner
    configuration at all."""
    _demo(fresh)
    fresh.delenv("STUDIO_TOOL_RUNNER", raising=False)
    db.init_db()
    bootstrap.enforce()


# ── auth fails closed ────────────────────────────────────────────────────

def test_make_token_without_secret_is_500(fresh):
    fresh.setattr(auth, "SECRET", None)
    user = {"id": "u1", "role": "admin", "name": "A"}
    with pytest.raises(HTTPException) as ei:
        auth.make_token(user)
    assert ei.value.status_code == 500
    assert "STUDIO_SECRET" in ei.value.detail


def test_current_user_without_secret_is_500(fresh):
    fresh.setattr(auth, "SECRET", None)
    cred = type("Cred", (), {"credentials": "any.token.here"})()
    with pytest.raises(HTTPException) as ei:
        auth.current_user(cred)
    assert ei.value.status_code == 500


def test_register_requires_ten_char_password(fresh):
    _demo(fresh)
    db.init_db()
    with pytest.raises(HTTPException) as ei:
        auth.register(auth.Register(email="new@example.com", password="nine-char", name="N"))
    assert ei.value.status_code == 400
