"""SSO hands the session over out of band — never in a URL.

A query string is copied into browser history, Referer headers and every
proxy/access log between the user and the app, so a JWT in the SSO redirect is
a session anyone who can read those can replay. The callback now redirects with
only `?sso_code=<random>` and POST /auth/sso/exchange trades that code for
{access_token, user} exactly once.

Proves: the callback's redirect carries no token and no user payload; a valid
code exchanges once and the replay is a 400; an expired code and an unknown
code are both 400 (and the expired entry is dropped); parking a new code sweeps
expired ones, so a process that only mints codes never accumulates live tokens;
the error redirect is untouched; and the token the exchange returns is a normal
Studio JWT that current_user accepts, i.e. the bearer path is unchanged.

Run from the backend directory:
    python -m pytest tests/test_sso_handoff.py -q
"""
import json
import os
import tempfile
import time
import urllib.parse

# Throwaway SQLite BEFORE app modules compute their paths (each test repoints
# db.DB_PATH anyway; this only matters if this module imports app.db first).
_TMP = tempfile.mkdtemp(prefix="studio-sso-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import jwt
import pytest
from fastapi import HTTPException

from app import auth, db

STRONG = "x" * 20 + "-" + "y" * 25  # 46 chars, not a placeholder
FRONTEND = "https://studio.example.com"
EMAIL = "ops@example.com"


class _Response:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeRequests:
    """Microsoft's half of the callback: token endpoint, /me, /me/memberOf.
    Substituted for the `requests` module so the flow runs offline."""

    def post(self, url, **kw):
        return _Response({"access_token": "graph-access-token"})

    def get(self, url, **kw):
        if "memberOf" in url:
            return _Response({"value": []})
        return _Response({"mail": EMAIL, "displayName": "Ops"})


@pytest.fixture()
def sso(tmp_path, monkeypatch):
    """Isolated DB, a usable signing secret, a configured Azure app and a fake
    Microsoft. The handoff map starts empty so codes cannot leak between tests."""
    path = str(tmp_path / "sso.db")
    monkeypatch.setenv("STUDIO_DB_PATH", path)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    monkeypatch.setattr(auth, "SECRET", STRONG)
    monkeypatch.setattr(auth, "FRONTEND_URL", FRONTEND)
    monkeypatch.setattr(auth, "requests", _FakeRequests())
    for k, v in (("AZURE_TENANT_ID", "tenant"), ("AZURE_CLIENT_ID", "client"),
                 ("AZURE_CLIENT_SECRET", "secret")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AZURE_GROUP_ROLE_MAP", raising=False)
    auth._SSO_HANDOFF.clear()
    auth._SSO_STATES.clear()
    return monkeypatch


def _bearer(token):
    return type("Cred", (), {"credentials": token})()


def _callback():
    """Run a successful Entra callback; return the redirect's Location."""
    state = "state-for-this-test"
    auth._SSO_STATES[state] = time.time() + 600
    resp = auth.azure_callback(code="microsoft-auth-code", state=state)
    return resp.headers["location"]


def _handoff_code():
    q = urllib.parse.parse_qs(urllib.parse.urlparse(_callback()).query)
    return q["sso_code"][0]


# ── the redirect ─────────────────────────────────────────────────────────

def test_callback_redirect_carries_no_token_and_no_user(sso):
    location = _callback()
    parts = urllib.parse.urlparse(location)
    q = urllib.parse.parse_qs(parts.query)

    assert location.startswith(FRONTEND)
    assert list(q) == ["sso_code"]                    # nothing else in the URL
    code = q["sso_code"][0]
    assert len(code) >= 32                            # random, not guessable

    # The token exists — it is parked server-side — and no part of it, nor of
    # the user's identity, appears anywhere in the URL that gets logged.
    expiry, token, user = auth._SSO_HANDOFF[code]
    assert token and token not in location
    assert EMAIL not in location and user["email"] == EMAIL
    assert 0 < expiry - time.time() <= auth.SSO_HANDOFF_TTL == 60


def test_error_redirect_is_unchanged(sso):
    resp = auth.azure_callback(error="access_denied", error_description="nope")
    location = resp.headers["location"]
    assert "sso_error=nope" in location and "sso_code" not in location
    assert auth._SSO_HANDOFF == {}


# ── the exchange ─────────────────────────────────────────────────────────

def test_exchange_returns_the_session_exactly_once(sso):
    code = _handoff_code()
    out = auth.sso_exchange(auth.SsoCode(code=code))

    user = db.get_user_by_email(EMAIL)
    assert out["user"] == {"id": user["id"], "email": EMAIL,
                           "name": "Ops", "role": user["role"]}
    payload = jwt.decode(out["access_token"], STRONG, algorithms=[auth.ALGO])
    assert payload["sub"] == user["id"] and payload["role"] == user["role"]

    # The bearer path is untouched: this is an ordinary Studio JWT.
    assert auth.current_user(_bearer(out["access_token"]))["email"] == EMAIL

    # Spent. A replay (from a history entry, a log, a shoulder) gets nothing.
    assert code not in auth._SSO_HANDOFF
    with pytest.raises(HTTPException) as ei:
        auth.sso_exchange(auth.SsoCode(code=code))
    assert ei.value.status_code == 400


def test_expired_code_is_400_and_is_dropped(sso):
    code = _handoff_code()
    _exp, token, user = auth._SSO_HANDOFF[code]
    auth._SSO_HANDOFF[code] = (time.time() - 1, token, user)   # 60s went by
    with pytest.raises(HTTPException) as ei:
        auth.sso_exchange(auth.SsoCode(code=code))
    assert ei.value.status_code == 400
    assert code not in auth._SSO_HANDOFF


@pytest.mark.parametrize("code", ["never-issued", "", "   "])
def test_unknown_code_is_400(sso, code):
    with pytest.raises(HTTPException) as ei:
        auth.sso_exchange(auth.SsoCode(code=code))
    assert ei.value.status_code == 400


def test_parking_a_code_sweeps_expired_ones(sso):
    """Otherwise a deployment where users abandon the redirect keeps a growing
    map of live tokens in memory."""
    stale = _handoff_code()
    _exp, token, user = auth._SSO_HANDOFF[stale]
    auth._SSO_HANDOFF[stale] = (time.time() - 1, token, user)
    fresh = _handoff_code()
    assert stale not in auth._SSO_HANDOFF and fresh in auth._SSO_HANDOFF


def test_exchange_is_a_post_route_under_auth(sso):
    """The SPA posts the code; a GET would put it back in a URL."""
    route = [r for r in auth.router.routes if r.path == "/auth/sso/exchange"]
    assert route and route[0].methods == {"POST"}
