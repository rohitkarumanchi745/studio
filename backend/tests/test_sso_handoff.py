"""SSO hands the session over out of band — never in a URL.

A query string is copied into browser history, Referer headers and every
proxy/access log between the user and the app, so a JWT in the SSO redirect is
a session anyone who can read those can replay. The callback now redirects with
only `?sso_code=<random>` and POST /auth/sso/exchange trades that code for
{access_token, user} exactly once.

Both halves finish only in the browser that started the login: /azure/login
sets an HttpOnly cookie whose hash binds the state and then the handoff code,
so a forwarded sign-in link fails at the callback (before any token exchange)
and a forwarded `/?sso_code=` link fails at the exchange — no login CSRF.

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
import types
import urllib.parse

# Throwaway SQLite BEFORE app modules compute their paths (each test repoints
# db.DB_PATH anyway; this only matters if this module imports app.db first).
_TMP = tempfile.mkdtemp(prefix="studio-sso-test-")
os.environ.setdefault("STUDIO_DB_PATH", os.path.join(_TMP, "studio.db"))

import jwt
import pytest
from fastapi import HTTPException, Response

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

    def __init__(self):
        self.token_exchanges = 0

    def post(self, url, **kw):
        self.token_exchanges += 1
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


class _Req:
    """The parts of a request the SSO handlers read: one browser's cookies."""

    def __init__(self, cookies=None, scheme="https"):
        self.cookies = dict(cookies or {})
        self.url = types.SimpleNamespace(scheme=scheme)
        self.headers = {}


def _login(scheme="https"):
    """Start a login as a browser does: (state, that browser's cookies, response)."""
    resp = auth.azure_login(_Req(scheme=scheme))
    q = urllib.parse.parse_qs(urllib.parse.urlparse(resp.headers["location"]).query)
    cookie = resp.headers["set-cookie"]
    nonce = cookie.split(";")[0].split("=", 1)[1]
    return q["state"][0], {auth._SSO_COOKIE: nonce}, resp


def _callback(finish_in=None):
    """Run an Entra login started in one browser and finished in `finish_in`
    (default: the same one); return (the redirect's Location, the starter's cookies)."""
    state, cookies, _ = _login()
    resp = auth.azure_callback(_Req(cookies if finish_in is None else finish_in),
                               code="microsoft-auth-code", state=state)
    return resp.headers["location"], cookies


def _handoff_code():
    """(code, the cookies of the browser that may spend it)."""
    location, cookies = _callback()
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["sso_code"][0], cookies


def _exchange(code, cookies):
    return auth.sso_exchange(auth.SsoCode(code=code), _Req(cookies), Response())


# ── the redirect ─────────────────────────────────────────────────────────

def test_callback_redirect_carries_no_token_and_no_user(sso):
    location, _ = _callback()
    parts = urllib.parse.urlparse(location)
    q = urllib.parse.parse_qs(parts.query)

    assert location.startswith(FRONTEND)
    assert list(q) == ["sso_code"]                    # nothing else in the URL
    code = q["sso_code"][0]
    assert len(code) >= 32                            # random, not guessable

    # The token exists — it is parked server-side — and no part of it, nor of
    # the user's identity, appears anywhere in the URL that gets logged.
    expiry, token, user, _binding = auth._SSO_HANDOFF[code]
    assert token and token not in location
    assert EMAIL not in location and user["email"] == EMAIL
    assert 0 < expiry - time.time() <= auth.SSO_HANDOFF_TTL == 60


def test_error_redirect_is_unchanged(sso):
    resp = auth.azure_callback(_Req(), error="access_denied", error_description="nope")
    location = resp.headers["location"]
    assert "sso_error=nope" in location and "sso_code" not in location
    assert auth._SSO_HANDOFF == {}


# ── the exchange ─────────────────────────────────────────────────────────

def test_exchange_returns_the_session_exactly_once(sso):
    code, cookies = _handoff_code()
    out = _exchange(code, cookies)

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
        _exchange(code, cookies)
    assert ei.value.status_code == 400


def test_expired_code_is_400_and_is_dropped(sso):
    code, cookies = _handoff_code()
    _exp, token, user, binding = auth._SSO_HANDOFF[code]
    auth._SSO_HANDOFF[code] = (time.time() - 1, token, user, binding)   # 60s went by
    with pytest.raises(HTTPException) as ei:
        _exchange(code, cookies)
    assert ei.value.status_code == 400
    assert code not in auth._SSO_HANDOFF


@pytest.mark.parametrize("code", ["never-issued", "", "   "])
def test_unknown_code_is_400(sso, code):
    with pytest.raises(HTTPException) as ei:
        _exchange(code, {})
    assert ei.value.status_code == 400


def test_parking_a_code_sweeps_expired_ones(sso):
    """Otherwise a deployment where users abandon the redirect keeps a growing
    map of live tokens in memory."""
    stale, _ = _handoff_code()
    _exp, token, user, binding = auth._SSO_HANDOFF[stale]
    auth._SSO_HANDOFF[stale] = (time.time() - 1, token, user, binding)
    fresh, _ = _handoff_code()
    assert stale not in auth._SSO_HANDOFF and fresh in auth._SSO_HANDOFF


def test_exchange_is_a_post_route_under_auth(sso):
    """The SPA posts the code; a GET would put it back in a URL."""
    route = [r for r in auth.router.routes if r.path == "/auth/sso/exchange"]
    assert route and route[0].methods == {"POST"}


# ── bound to the browser that started the login ─────────────────────────

def test_login_sets_an_httponly_cookie_and_keeps_only_its_hash(sso):
    state, cookies, resp = _login()
    cookie = resp.headers["set-cookie"]
    assert cookie.startswith(auth._SSO_COOKIE + "=")
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "Max-Age=600" in cookie and "Secure" in cookie
    nonce = cookies[auth._SSO_COOKIE]
    assert nonce not in resp.headers["location"]                  # not sent to Microsoft
    _exp, binding = auth._SSO_STATES[state]
    assert binding != nonce and binding == auth._binding(nonce)  # only its hash is kept
    # Plain-HTTP local development (the default http:// redirect) still works:
    # a Secure cookie would never come back over http.
    assert "Secure" not in _login(scheme="http")[2].headers["set-cookie"]


def test_a_forwarded_sign_in_link_is_refused_before_any_token_exchange(sso):
    fake = auth.requests
    for browser in ({}, {auth._SSO_COOKIE: "another-browsers-own-nonce"}):
        location, _ = _callback(finish_in=browser)
        assert "sso_error=" in location and "sso_code" not in location
        assert "not%20started%20in%20this%20browser" in location
    assert fake.token_exchanges == 0                  # Microsoft was never asked
    assert auth._SSO_HANDOFF == {} and db.get_user_by_email(EMAIL) is None


def test_a_forwarded_handoff_code_logs_nobody_else_in(sso):
    """The attacker finishes their own login and sends `/?sso_code=…` on: the
    victim's browser cannot spend it, and the code is gone afterwards."""
    code, _attackers = _handoff_code()
    for victim in ({}, {auth._SSO_COOKIE: "the-victims-own-nonce"}):
        with pytest.raises(HTTPException) as ei:
            _exchange(code, victim)
        assert ei.value.status_code == 400
    assert code not in auth._SSO_HANDOFF


def test_a_successful_exchange_clears_the_login_cookie(sso):
    code, cookies = _handoff_code()
    resp = Response()
    auth.sso_exchange(auth.SsoCode(code=code), _Req(cookies), resp)
    cleared = resp.headers["set-cookie"]
    assert cleared.startswith(auth._SSO_COOKIE + "=") and "Max-Age=0" in cleared


def test_the_whole_login_round_trips_through_one_browsers_cookie_jar(sso):
    """Through FastAPI itself: login → Microsoft → callback → the SPA's exchange,
    with the cookie carried by the client's jar — and a second browser handed
    the same sso_code gets nothing."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    browser, stranger = TestClient(app), TestClient(app)
    r = browser.get("/api/auth/azure/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["state"][0]
    r = browser.get("/api/auth/azure/callback", params={"code": "c", "state": state},
                    follow_redirects=False)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["sso_code"][0]
    assert stranger.post("/api/auth/sso/exchange", json={"code": code}).status_code == 400

    # A fresh login in the right browser works end to end.
    r = browser.get("/api/auth/azure/login", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["state"][0]
    r = browser.get("/api/auth/azure/callback", params={"code": "c", "state": state},
                    follow_redirects=False)
    code = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)["sso_code"][0]
    out = browser.post("/api/auth/sso/exchange", json={"code": code})
    assert out.status_code == 200 and out.json()["user"]["email"] == EMAIL
    assert auth._SSO_COOKIE not in browser.cookies              # cleared after use
