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

That leaves a public deploy with no way to make a login for testers at all, so
ensure_shared_login() adds one deliberate exception: STUDIO_SHARED_LOGIN_EMAIL
+ _PASSWORD create ONE pre-verified account (role from STUDIO_SHARED_LOGIN_ROLE,
default analyst) that the operator hands out. Pre-verified because with no SMTP
the emailed code only reaches backend/outbox/ inside the container; a shared
credential rather than open signup because it is one known identity the
operator can rotate or delete, not an unbounded set of self-minted ones.

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
        ensure_shared_login()
        return
    log.info("bootstrap: production mode")
    _require_strong_secret()
    _require_isolated_tool_runner()
    revoke_default_passwords()
    ensure_bootstrap_admin()
    ensure_shared_login()


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


# ── Shared login ─────────────────────────────────────────────────────────
# One credential the operator hands to every tester of a public deploy. It
# exists because production has no other way to MAKE a login: registration is
# closed by default and, with no SMTP configured, the 6-digit verification code
# only ever reaches backend/outbox/ on the container — so a tester who did
# register could never sign in. This account is created pre-verified, so it
# needs no mail at all, and it does NOT re-open self-registration.

SHARED_LOGIN_EMAIL_ENV = "STUDIO_SHARED_LOGIN_EMAIL"
SHARED_LOGIN_PASSWORD_ENV = "STUDIO_SHARED_LOGIN_PASSWORD"
SHARED_LOGIN_ROLE_ENV = "STUDIO_SHARED_LOGIN_ROLE"
SHARED_LOGIN_SHOW_ENV = "STUDIO_SHARED_LOGIN_SHOW"
DEFAULT_SHARED_LOGIN_ROLE = "analyst"


def shared_login_email():
    """The shared account's address, lowercased, or "" when the feature is off.
    Empty is the default: no env var, no account, nothing changes."""
    return (os.getenv(SHARED_LOGIN_EMAIL_ENV) or "").strip().lower()


def _studio_roles():
    """The role names this deployment actually has, read from the built-in
    RBAC table rather than duplicated here — a role added to policies.py is
    accepted by STUDIO_SHARED_LOGIN_ROLE the same day, and one removed from it
    stops being accepted. policies is a pure leaf (it imports nothing from
    app), so this edge adds no cycle; it is imported inside the function to
    keep this module importable from db at module level."""
    from .policies import POLICIES
    return sorted(POLICIES)


def shared_login_role():
    """STUDIO_SHARED_LOGIN_ROLE, defaulting to 'analyst' — enough to query and
    build without holding the admin powers listed in _warn_shared_admin().
    Validated against the real role table; an unknown value refuses the boot
    rather than silently creating an account whose role resolves to no policy
    at all (rbac fails closed, so the testers would just see empty catalogs)."""
    raw = (os.getenv(SHARED_LOGIN_ROLE_ENV) or "").strip().lower()
    role = raw or DEFAULT_SHARED_LOGIN_ROLE
    valid = _studio_roles()
    if role not in valid:
        raise RuntimeError(
            f"{SHARED_LOGIN_ROLE_ENV}={raw!r} is not a Studio role. Valid roles "
            f"are: {', '.join(valid)} (see app/policies.py). Leave it unset for "
            f"the default '{DEFAULT_SHARED_LOGIN_ROLE}'."
        )
    return role


def shared_login_show():
    """Whether GET /auth/sso may disclose the shared address. OFF by default:
    creating the account and ADVERTISING it are separate decisions."""
    return (os.getenv(SHARED_LOGIN_SHOW_ENV, "") or "").strip().lower() in TRUTHY


def shared_login_public():
    """What the login page may be told about the shared account: {"email",
    "role"} or None. NEVER the password — the operator distributes that
    themselves, by whatever channel they chose.

    Turning STUDIO_SHARED_LOGIN_SHOW on advertises the address to EVERYONE who
    loads the login page, including bots: it is published on an unauthenticated
    endpoint. That is the point for a public demo (testers stop guessing which
    address to type), and the wrong default everywhere else, which is why it is
    a second env var rather than a consequence of the first.

    Returns None for a role the env no longer accepts (changed under a running
    process): enforce() validated it at boot, so this endpoint reports nothing
    rather than raising a 500 on the login page.
    """
    email = shared_login_email()
    if not email or not shared_login_show():
        return None
    try:
        role = shared_login_role()
    except RuntimeError:
        return None
    return {"email": email, "role": role}


def ensure_shared_login():
    """Create/refresh the env-driven shared login. Runs in BOTH modes.

    WHY THIS BREAKS ensure_bootstrap_admin's never-touch-the-password RULE:
    the bootstrap admin belongs to a PERSON — rotating STUDIO_ADMIN_PASSWORD
    must not overwrite the password that person later set for themselves, so
    that path only ever proves control and promotes. This account belongs to
    the DEPLOYMENT. Its password is not a person's secret but a configuration
    value the operator hands out, so the environment is its source of truth:
    every boot re-asserts the password and the role, and changing
    STUDIO_SHARED_LOGIN_PASSWORD rotates the credential for everyone on the
    next restart. The corollary is that STUDIO_SHARED_LOGIN_EMAIL must not
    name a real person's account: if it does, this takes it over (password,
    role and verified flag) on the next boot.

    The account is created verified=1, which is what makes it usable with no
    SMTP: /auth/login and current_user both refuse an unverified password
    account, and a code that only reaches backend/outbox/ inside a container is
    not a login path. It does NOT open self-registration — that stays off.

    In EVERY other respect this is an ordinary account. No bypasses: RBAC
    (rbac.can_access on its role), governance, the gateway and the audit log
    treat it exactly like any other user. The cost of a shared credential is
    attribution — the audit log records every tester's prompts, SQL and
    approvals against this ONE identity, so "who ran that" stops being a
    question the log can answer. Hand out per-person accounts when that
    matters.
    """
    from . import db
    email = shared_login_email()
    if not email:
        return
    role = shared_login_role()          # refuses an unknown role
    password = os.getenv(SHARED_LOGIN_PASSWORD_ENV) or ""
    if len(password) < MIN_ADMIN_PASSWORD_LEN:
        state = "is not set" if not password else \
            f"is shorter than {MIN_ADMIN_PASSWORD_LEN} characters"
        raise RuntimeError(
            f"{SHARED_LOGIN_EMAIL_ENV}={email} is set, so "
            f"{SHARED_LOGIN_PASSWORD_ENV} (at least {MIN_ADMIN_PASSWORD_LEN} "
            f"characters) is required: it {state}. This one password is handed "
            f"to every tester — make it long and random, and unset "
            f"{SHARED_LOGIN_EMAIL_ENV} to turn the shared login off."
        )
    _warn_shared_admin(email, role)

    user = db.get_user_by_email(email)
    if not user:
        try:
            db.create_user(email, password, email.split("@")[0],
                           role=role, verified=1)
        except Exception:
            # Two replicas booting at once race on the same email; the loser
            # sees a unique violation. Re-read and fall through to the same
            # sync rule rather than crashing the container.
            user = db.get_user_by_email(email)
            if not user:
                raise
        else:
            log.info("bootstrap: created shared login %s (role %s, pre-verified) "
                     "from %s", email, role, SHARED_LOGIN_EMAIL_ENV)
            return
    _sync_shared_login(user, email, role, password)


def _sync_shared_login(user, email, role, password):
    """Re-assert the environment on an account that already exists: password,
    role, verified. Each is a no-op when it already matches, so a boot that
    changed nothing logs nothing."""
    from . import db
    if not db.verify_password(password, user["password_hash"]):
        db.set_user_password(user["id"], db.hash_password(password))
        log.info("bootstrap: shared login %s — password reset to match %s "
                 "(the old one no longer works)", email, SHARED_LOGIN_PASSWORD_ENV)
    if user["role"] != role:
        db.set_user_role(email, role)
        log.info("bootstrap: shared login %s — role %s → %s (%s)",
                 email, user["role"], role, SHARED_LOGIN_ROLE_ENV)
    if not user.get("verified", 1):
        db.mark_verified(email)
        log.info("bootstrap: shared login %s — marked verified (no SMTP needed)", email)


def _warn_shared_admin(email, role):
    """A shared ADMIN credential in production is a foot-gun, not an error.

    The operator may have chosen it deliberately (a demo where testers need the
    admin screens), so the boot is NOT refused — but the log says exactly what
    the credential grants, because 'admin' reads like a convenience and is not.
    """
    if role != "admin" or demo_mode():
        return
    for line in (
        "=" * 72,
        f"bootstrap: SHARED ADMIN LOGIN — {email} is an admin account whose "
        f"password is handed to every tester.",
        "bootstrap: anyone holding it can: APPROVE MODEL-GENERATED CODE FOR "
        "EXECUTION (tool builder), EDIT GOVERNANCE (widen what every role may "
        "read), REGISTER MCP SERVERS, and READ EVERY USER'S ACTIVITY (prompts, "
        "SQL, results in the audit log).",
        f"bootstrap: keep it only if you meant it — set STUDIO_TOOLBUILDER=0 so "
        f"no generated code can be registered or launched, or set "
        f"{SHARED_LOGIN_ROLE_ENV}=analyst instead.",
        "=" * 72,
    ):
        log.warning("%s", line)
