"""Boot-time safety checks: no deployment starts with public credentials.

Two things used to make a fresh Studio deploy exploitable by anyone who had
read the README: the JWT secret defaulted to a known string, and init_db()
always seeded admin@studio.local/admin123 (+ analyst, viewer). This module
draws the line between the two modes:

  * demo mode (STUDIO_DEMO_MODE=1): seed accounts exist, and if STUDIO_SECRET
    is unset a random per-process secret is used — tokens die with the
    process, which is fine for a laptop demo and for the test suite.
  * production mode (the default): STUDIO_SECRET is REQUIRED and must not be
    weak, no seed accounts are created, and any seed account still carrying
    its documented default password gets that password revoked on boot.

Self-service signup follows the same line: open_registration() is ON in demo
mode and OFF in production unless STUDIO_OPEN_REGISTRATION says otherwise —
removing the seeds is pointless if anyone can mint a fresh account instead.

The tool runner follows it too: the default 'process' runner launches approved,
model-generated MCP servers as the app's own uid (see sandbox.py), so a
production boot refuses it rather than letting a deploy discover that the first
time an agent loads a built tool.

Invariants:
  - enforce() runs once at startup, AFTER db.init_db() (tables must exist).
  - It raises RuntimeError rather than warn: a misconfigured production
    container must refuse to start, not serve with a guessable secret.
  - Only stdlib at module level; db is imported lazily inside functions so
    db.py can import this module for demo_mode() without a cycle.
"""
import logging
import os
import secrets

log = logging.getLogger("studio.bootstrap")

MIN_SECRET_LEN = 32
MIN_ADMIN_PASSWORD_LEN = 12

# Well-known placeholder values seen in READMEs, .env.examples and templates.
# Membership is case-insensitive; length is checked separately.
WEAK_SECRETS = {
    "dev-secret-change-me",
    "change-me-in-production",
    "change-me",
    "changeme",
    "secret",
    "password",
    "studio",
}

# (email, documented default password, display name, role) — mirrors the demo
# seeds in db.init_db(). Kept here so revocation and seeding can never drift.
SEED_USERS = [
    ("admin@studio.local", "admin123", "Admin", "admin"),
    ("analyst@studio.local", "analyst123", "Analyst", "analyst"),
    ("viewer@studio.local", "viewer123", "Viewer", "viewer"),
]

_DEMO_SECRET = None  # per-process random secret, demo mode only


TRUTHY = {"1", "true", "yes"}


def demo_mode():
    return os.getenv("STUDIO_DEMO_MODE", "").strip().lower() in TRUTHY


def open_registration():
    """Whether an unauthenticated POST /auth/register may create an account.

    Default follows the mode: ON for a laptop demo, OFF in production, because
    a production Studio reaches real warehouses and its accounts are handed out
    by an administrator or by SSO. STUDIO_OPEN_REGISTRATION overrides either
    way; anything not truthy (including an explicit "0") turns it off.
    """
    raw = os.getenv("STUDIO_OPEN_REGISTRATION")
    if raw is None or not raw.strip():
        return demo_mode()
    return raw.strip().lower() in TRUTHY


# The tool runner is an operator setting read in TWO places: here at boot, and
# in sandbox.launch_spec() at load time. Both names and both predicates live in
# this module — the one that already answers "what mode is this deployment in"
# — so the gates cannot drift; sandbox.py reads them through a lazy import,
# never the other way round (bootstrap is a leaf module, tests/test_layering).
TOOL_RUNNER_ENV = "STUDIO_TOOL_RUNNER"
ALLOW_PROCESS_ENV = "STUDIO_TOOL_RUNNER_ALLOW_PROCESS"


def tool_runner():
    """The runner sandbox.launch_spec() dispatches on; 'process' by default."""
    return (os.getenv(TOOL_RUNNER_ENV, "process") or "process").strip().lower()


def allow_process_tool_runner():
    """Has the operator explicitly accepted that approved generated code runs
    with the app's own privileges (STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1)? Read
    per call, never cached, so boot and load time cannot disagree."""
    return (os.getenv(ALLOW_PROCESS_ENV, "") or "").strip().lower() in TRUTHY


def process_tool_runner_refused():
    """True when a process-runner launch must be refused: production mode
    without the opt-in. This is the single predicate behind both gates."""
    return not demo_mode() and not allow_process_tool_runner()


def tool_builder_enabled():
    """Whether an approved tool-builder artifact may still become a runnable
    MCP server — i.e. whether mcp.register_stdio() accepts an owner-scoped row
    and registered() will launch one. ON unless STUDIO_TOOLBUILDER says
    otherwise, which is the third way to satisfy the production runner gate:
    with the feature off, no generated code can be registered or launched at
    all, so the runner's privileges no longer matter.
    """
    raw = os.getenv("STUDIO_TOOLBUILDER")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in TRUTHY


def is_weak_secret(secret):
    if not secret:
        return True
    return len(secret) < MIN_SECRET_LEN or secret.strip().lower() in WEAK_SECRETS


def jwt_secret():
    """The signing secret, or None. STUDIO_SECRET always wins (even if weak —
    enforce() decides whether that refuses boot). Demo mode falls back to a
    random per-process value; production never falls back to anything."""
    global _DEMO_SECRET
    explicit = os.getenv("STUDIO_SECRET")
    if explicit:
        return explicit
    if demo_mode():
        if _DEMO_SECRET is None:
            _DEMO_SECRET = secrets.token_urlsafe(48)
        return _DEMO_SECRET
    return None


def enforce():
    """Startup gate. Call once, after db.init_db()."""
    if demo_mode():
        log.info("bootstrap: demo mode: seed accounts enabled, ephemeral JWT secret")
        ensure_bootstrap_admin()
        return
    log.info("bootstrap: production mode")
    _require_strong_secret()
    _require_isolated_tool_runner()
    revoke_default_passwords()
    ensure_bootstrap_admin()


def _require_strong_secret():
    secret = os.getenv("STUDIO_SECRET")
    if is_weak_secret(secret):
        state = "is not set" if not secret else "is a weak/placeholder value"
        raise RuntimeError(
            f"STUDIO_SECRET {state}. Production boot requires STUDIO_SECRET of at "
            f"least {MIN_SECRET_LEN} random characters (e.g. `python -c "
            f"\"import secrets; print(secrets.token_urlsafe(48))\"`). For a local "
            f"demo set STUDIO_DEMO_MODE=1 instead."
        )


def _require_isolated_tool_runner():
    """Production refuses the process tool runner (production mode only).

    A built tool is model-written code that an admin approved; under the
    'process' runner it runs as the app's own uid, so it can read backend/.env
    and studio.db and reach the network (sandbox.py documents this honestly).
    The runner is chosen at LOAD time from the environment, so a deploy that
    simply never set STUDIO_TOOL_RUNNER would find out only when an agent first
    loads a built tool — hence a boot refusal, not a warning.

    Three ways out, in order of preference: STUDIO_TOOL_RUNNER=docker (real
    isolation), STUDIO_TOOLBUILDER=0 (nothing generated can be registered or
    launched), or STUDIO_TOOL_RUNNER_ALLOW_PROCESS=1 (the operator accepts that
    approved generated code runs with the app's privileges).
    """
    if tool_runner() != "process" or not tool_builder_enabled():
        return
    if allow_process_tool_runner():
        log.warning(
            "bootstrap: %s=1 — approved generated tools run with the app's own "
            "filesystem, network and credentials", ALLOW_PROCESS_ENV)
        return
    state = ("is unset (defaulting to 'process')"
             if not (os.getenv(TOOL_RUNNER_ENV) or "").strip() else "is 'process'")
    raise RuntimeError(
        f"STUDIO_TOOL_RUNNER {state}, so an approved tool-builder server would "
        f"run with the app's own filesystem, network and credentials. Set "
        f"STUDIO_TOOL_RUNNER=docker (build scripts/Dockerfile.toolrunner), or "
        f"STUDIO_TOOLBUILDER=0 to disable built tools entirely, or "
        f"{ALLOW_PROCESS_ENV}=1 to accept that approved generated code "
        f"runs with the app's privileges. For a local demo set "
        f"STUDIO_DEMO_MODE=1 instead."
    )


def revoke_default_passwords():
    """Any seed account still on its documented default password gets an
    unrecoverable random one. Existing deployments that seeded before this
    check existed are exactly the ones at risk, so this runs on every boot."""
    from . import db
    for email, default_pw, _name, _role in SEED_USERS:
        user = db.get_user_by_email(email)
        if not user or not db.verify_password(default_pw, user["password_hash"]):
            continue
        db.set_user_password(user["id"], db.hash_password(secrets.token_urlsafe(32)))
        log.warning(
            "bootstrap: %s still had its default password — revoked. Reset it via "
            "STUDIO_ADMIN_EMAIL/STUDIO_ADMIN_PASSWORD or sign in through SSO.", email)


def ensure_bootstrap_admin():
    """STUDIO_ADMIN_EMAIL names the first real admin. Missing → created from
    STUDIO_ADMIN_PASSWORD (verified, role admin). Present → promoted only if
    the operator can prove they control that account (see _promote_admin);
    its password is never touched, so rotating it is a one-time env change
    rather than a permanent override."""
    from . import db
    email = (os.getenv("STUDIO_ADMIN_EMAIL") or "").strip().lower()
    if not email:
        return
    password = os.getenv("STUDIO_ADMIN_PASSWORD") or ""
    user = db.get_user_by_email(email)
    if user:
        _promote_admin(user, email, password)
        return
    if len(password) < MIN_ADMIN_PASSWORD_LEN:
        raise RuntimeError(
            f"STUDIO_ADMIN_EMAIL={email} does not exist yet, so STUDIO_ADMIN_PASSWORD "
            f"(at least {MIN_ADMIN_PASSWORD_LEN} characters) is required to create it."
        )
    try:
        db.create_user(email, password, email.split("@")[0], role="admin", verified=1)
    except Exception:
        # Two replicas booting at once race on the same email; the loser sees a
        # unique violation. Re-read and fall through to the same promotion rule
        # rather than crashing the container.
        user = db.get_user_by_email(email)
        if not user:
            raise
        _promote_admin(user, email, password)
        return
    log.info("bootstrap: created admin %s from STUDIO_ADMIN_EMAIL", email)


def _promote_admin(user, email, password):
    """Grant admin to an account that already owns STUDIO_ADMIN_EMAIL — but
    only when the operator has proved they control it, otherwise setting the
    env var to any address on the instance (e.g. one a self-registered viewer
    took first) would silently hand that person admin.

    Proof is either: the account is SSO-provisioned, so there is no local
    password anyone could be holding and the identity provider owns it; or
    STUDIO_ADMIN_PASSWORD verifies against the stored hash. Anything else
    refuses the boot — a conflict an operator must resolve deliberately.
    """
    from . import db
    if user["role"] == "admin":
        return
    sso = not db.has_usable_password(user)
    if not sso and not (password and db.verify_password(password, user["password_hash"])):
        raise RuntimeError(
            f"STUDIO_ADMIN_EMAIL={email} is already a local account (role "
            f"{user['role']}) that STUDIO_ADMIN_PASSWORD does not match, so it "
            f"will NOT be promoted to admin. Pick a different STUDIO_ADMIN_EMAIL "
            f"or reset that account's password and set STUDIO_ADMIN_PASSWORD to it."
        )
    db.set_user_role(email, "admin")
    log.info("bootstrap: promoted %s to admin (%s)", email,
             "SSO-provisioned account" if sso else "password verified")
