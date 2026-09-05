"""Auth: normal email/password login with JWT for the prototype.

Azure SSO seam: the /auth/azure/* routes are stubbed with the exact flow
documented so wiring MSAL later is mechanical — the rest of the app only
ever sees a JWT with {sub, role}, so swapping the identity provider does
not touch any other module.

The SSO redirect back to the SPA never carries the token: a URL query string is
copied into browser history, Referer headers and every proxy/access log on the
path, and a bearer token in any of those is a session anyone who reads them can
replay. The callback parks the token under a single-use, 60-second code and the
SPA trades it at POST /auth/sso/exchange (see _SSO_HANDOFF).

Account creation is NOT open by default: /auth/register is gated on
bootstrap.open_registration() (demo yes, production no) and, when it is
allowed, the account is created UNVERIFIED and carries no token — the 6-digit
email code is the gate that turns it into a usable login, not decoration.

SECRET is resolved once at import via bootstrap.jwt_secret(): STUDIO_SECRET,
else a per-process random value in demo mode, else None. None means a
production deploy booted without a secret (bootstrap.enforce() should have
refused, but if it was bypassed) — signing and verifying then fail closed
with a 500 rather than using any fallback string.
"""
import json
import os
import random
import secrets
import time
import urllib.parse
from typing import Optional

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from . import bootstrap, db, email_service

SECRET = bootstrap.jwt_secret()
ALGO = "HS256"
TOKEN_TTL = 60 * 60 * 12  # 12h

router = APIRouter(prefix="/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=False)


class Credentials(BaseModel):
    email: str
    password: str


class Register(BaseModel):
    email: str
    password: str
    name: str


def _require_secret():
    if not SECRET:
        raise HTTPException(500, "Server misconfigured: STUDIO_SECRET is not set")
    return SECRET


def make_token(user):
    return jwt.encode(
        {"sub": user["id"], "role": user["role"], "name": user["name"], "exp": int(time.time()) + TOKEN_TTL},
        _require_secret(),
        algorithm=ALGO,
    )


def _require_verified(user):
    """A password account is unusable until its emailed code is entered. SSO
    and Entra users are provisioned verified=1, so this never touches them —
    and rows that predate the column read 1 as well."""
    if not user.get("verified", 1):
        raise HTTPException(403, "Verify your email before signing in")
    return user


def current_user(cred: Optional[HTTPAuthorizationCredentials] = Depends(bearer)):
    """Accepts either a Studio-issued JWT (local login / redirect SSO) or a
    Microsoft Entra access token validated against Microsoft's JWKS keys
    (MSAL.js SPA path). Both converge on the same user record and RBAC.

    Verification is re-checked on every request, not just at login, so a token
    minted before this gate existed cannot outlive it."""
    if cred is None:
        raise HTTPException(401, "Not authenticated")
    token = cred.credentials
    try:
        payload = jwt.decode(token, _require_secret(), algorithms=[ALGO])
        user = db.get_user(payload["sub"])
        if not user:
            raise HTTPException(401, "User no longer exists")
        return _require_verified(user)
    except jwt.PyJWTError:
        pass
    # Not a Studio token — try direct Entra validation (production SPA path)
    from .entra import validate_entra_token
    info = validate_entra_token(token)
    if not info:
        raise HTTPException(401, "Invalid or expired token")
    return db.upsert_sso_user(info["email"], info["name"], info["role"])


@router.post("/login")
def login(body: Credentials):
    user = db.get_user_by_email(body.email.strip().lower())
    if not user or not db.verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    _require_verified(user)
    return {
        "access_token": make_token(user),
        "user": {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]},
    }


@router.post("/register")
def register(body: Register):
    # Checked per request, not at import: an operator flipping the env var
    # takes effect on restart without the module being reloaded in a test.
    if not bootstrap.open_registration():
        raise HTTPException(
            403, "Self-registration is disabled; ask an administrator for an "
                 "account or sign in with SSO")
    email = body.email.strip().lower()
    if db.get_user_by_email(email):
        raise HTTPException(409, "Email already registered")
    if len(body.password) < 10:
        raise HTTPException(400, "Password must be at least 10 characters")
    # verified=0 and no token in the response: the account exists but cannot
    # sign in or carry a request until /auth/verify-email accepts the code.
    db.create_user(email, body.password, body.name.strip() or email.split("@")[0],
                   role="viewer", verified=0)

    # Email verification: send a 6-digit code. Without SMTP config the email
    # lands in backend/outbox/ so the flow is testable locally.
    code = f"{random.randint(0, 999999):06d}"
    db.set_email_code(email, code)
    try:
        delivery = email_service.send(
            email,
            "Verify your Studio account",
            f"<p>Welcome to Studio! Your verification code is:</p>"
            f"<h2 style='letter-spacing:4px'>{code}</h2>"
            f"<p>Enter it in the app to verify your email.</p>",
        )
    except Exception as e:
        delivery = {"mode": "error", "detail": str(e)}

    return {"registered": True, "email": email, "verification": delivery}


class Verify(BaseModel):
    email: str
    code: str


@router.post("/verify-email")
def verify_email(body: Verify):
    email = body.email.strip().lower()
    if not db.check_email_code(email, body.code.strip()):
        raise HTTPException(400, "Invalid or expired verification code")
    db.mark_verified(email)
    try:
        email_service.send(
            email,
            "Studio email verified",
            "<p>Your email is verified. Agent alerts and reports will be delivered here.</p>",
        )
    except Exception:
        pass
    return {"verified": True}


@router.get("/me")
def me(user=Depends(current_user)):
    return {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}


# ── Azure / Entra ID SSO ────────────────────────────────────────────────
# Employees sign in with their Microsoft account; Entra group memberships map
# to Studio roles (AZURE_GROUP_ROLE_MAP), so nobody needs warehouse or AI
# credentials — those stay server-side as service accounts.
#
# Setup (Azure portal → App registrations):
#   1. New registration; redirect URI (Web): http://localhost:8000/api/auth/azure/callback
#      (the backend is mounted under a single /api prefix; override with
#      AZURE_REDIRECT_URI for other hosts)
#   2. Certificates & secrets → new client secret
#   3. API permissions: Microsoft Graph → User.Read (+ GroupMember.Read.All to
#      read groups; grant admin consent)
#   4. Fill AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET in .env and
#      AZURE_GROUP_ROLE_MAP, e.g. {"Studio-Admins": "admin", "Analysts": "analyst"}

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
_SSO_STATES = {}  # state -> expiry (in-memory; fine for a single backend)
_ROLE_RANK = {"admin": 3, "analyst": 2, "viewer": 1}

# code -> (expiry, access_token, user dict). The token the SPA will use, parked
# for at most SSO_HANDOFF_TTL seconds and handed over exactly once, so the
# redirect URL carries only an opaque code that is worthless the moment it is
# spent. PER PROCESS, like _SSO_STATES: behind several replicas the exchange
# must land on the replica that handled the callback (sticky sessions), else
# both maps belong in the app DB — the code stays random, single-use and
# short-lived either way.
_SSO_HANDOFF = {}
SSO_HANDOFF_TTL = 60


def _azure_cfg():
    cfg = {
        "tenant": os.getenv("AZURE_TENANT_ID", ""),
        "client_id": os.getenv("AZURE_CLIENT_ID", ""),
        "secret": os.getenv("AZURE_CLIENT_SECRET", ""),
        "redirect": os.getenv("AZURE_REDIRECT_URI", "http://localhost:8000/api/auth/azure/callback"),
    }
    return cfg if cfg["tenant"] and cfg["client_id"] and cfg["secret"] else None


def _group_role_map():
    try:
        return json.loads(os.getenv("AZURE_GROUP_ROLE_MAP", "{}"))
    except json.JSONDecodeError:
        return {}


@router.get("/sso")
def sso_status():
    """Which SSO providers are configured, and whether signup is open at all —
    drives the login button state and lets the UI hide the Register link
    instead of offering a form that can only answer 403."""
    return {"azure": _azure_cfg() is not None,
            "open_registration": bootstrap.open_registration()}


@router.get("/azure/login")
def azure_login():
    cfg = _azure_cfg()
    if not cfg:
        msg = ("Azure SSO is not configured. Set AZURE_TENANT_ID / AZURE_CLIENT_ID / "
               "AZURE_CLIENT_SECRET in backend/.env (see auth.py for the app-registration steps).")
        return RedirectResponse(f"{FRONTEND_URL}/?sso_error={urllib.parse.quote(msg)}")
    state = secrets.token_urlsafe(24)
    now = time.time()
    _SSO_STATES[state] = now + 600
    for k in [k for k, exp in _SSO_STATES.items() if exp < now]:
        _SSO_STATES.pop(k, None)
    params = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect"],
        "response_mode": "query",
        "scope": "openid profile email User.Read",
        "state": state,
    })
    return RedirectResponse(
        f"https://login.microsoftonline.com/{cfg['tenant']}/oauth2/v2.0/authorize?{params}")


@router.get("/azure/callback")
def azure_callback(code: str = None, state: str = None, error: str = None,
                   error_description: str = None):
    def fail(msg):
        return RedirectResponse(f"{FRONTEND_URL}/?sso_error={urllib.parse.quote(msg[:300])}")

    if error:
        return fail(error_description or error)
    cfg = _azure_cfg()
    if not cfg:
        return fail("Azure SSO is not configured.")
    if not code or not state or _SSO_STATES.pop(state, 0) < time.time():
        return fail("SSO state expired or invalid — try signing in again.")

    token_res = requests.post(
        f"https://login.microsoftonline.com/{cfg['tenant']}/oauth2/v2.0/token",
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["secret"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg["redirect"],
            "scope": "openid profile email User.Read",
        }, timeout=30)
    if not token_res.ok:
        return fail(f"Token exchange failed: {token_res.text[:200]}")
    access_token = token_res.json().get("access_token")

    graph = {"Authorization": f"Bearer {access_token}"}
    me = requests.get("https://graph.microsoft.com/v1.0/me", headers=graph, timeout=30).json()
    email = (me.get("mail") or me.get("userPrincipalName") or "").lower()
    if not email:
        return fail("Could not read your profile from Microsoft Graph.")
    name = me.get("displayName") or email.split("@")[0]

    # Entra groups → Studio role (highest matching role wins; default viewer)
    role = "viewer"
    mapping = _group_role_map()
    if mapping:
        try:
            groups_res = requests.get(
                "https://graph.microsoft.com/v1.0/me/memberOf?$select=displayName",
                headers=graph, timeout=30).json()
            names = {g.get("displayName") for g in groups_res.get("value", [])}
            for g, r in mapping.items():
                if g in names and _ROLE_RANK.get(r, 0) > _ROLE_RANK.get(role, 0):
                    role = r
        except Exception:
            pass

    user = db.upsert_sso_user(email, name, role)
    # Neither the token nor the user payload goes in the URL — only a code that
    # POST /auth/sso/exchange trades for the session once.
    return RedirectResponse(f"{FRONTEND_URL}/?sso_code={_park_sso_session(user)}")


def _park_sso_session(user):
    """Mint the user's JWT, park it under a fresh random handoff code, return
    the code. Expired entries are dropped on the way through so a process that
    only ever mints codes cannot grow a map of live tokens."""
    now = time.time()
    for stale in [k for k, (exp, _t, _u) in _SSO_HANDOFF.items() if exp < now]:
        _SSO_HANDOFF.pop(stale, None)
    code = secrets.token_urlsafe(32)
    _SSO_HANDOFF[code] = (now + SSO_HANDOFF_TTL, make_token(user),
                          {"id": user["id"], "email": user["email"],
                           "name": user["name"], "role": user["role"]})
    return code


class SsoCode(BaseModel):
    code: str


@router.post("/sso/exchange")
def sso_exchange(body: SsoCode):
    """Trade a handoff code for {access_token, user} — exactly once.

    The entry is popped BEFORE anything is checked, so a replayed code (or one
    scraped from a history entry, which never held the token) gets the same 400
    as a forged or expired one. 400 rather than 401: nothing is authenticated
    here yet, and the SPA's 401 handler must not reload the page under it.
    """
    entry = _SSO_HANDOFF.pop((body.code or "").strip(), None)
    if not entry or entry[0] < time.time():
        raise HTTPException(400, "SSO code is invalid or expired — sign in again")
    _exp, token, user = entry
    return {"access_token": token, "user": user}
