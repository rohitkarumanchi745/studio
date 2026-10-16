"""APIRouter(prefix='/m365') — connect / oauth-callback / webhook / sync /
status / disconnect.

Everything is current_user-gated EXCEPT the two Graph-driven endpoints: the OAuth
callback (identified by a signed `state`, like auth.azure_callback) and the
webhook (validated by clientState). Every endpoint returns {"configured": false}
with 200 when the feature is dormant — never a 500 — and NO endpoint ever returns
a token in its body.

The signed state is a short-lived JWT over STUDIO_SECRET carrying the initiating
user's id, so the stateless callback can recover the user without a bearer and a
forged/expired state is rejected. A signature alone did not say WHICH BROWSER may
finish the sign-in: anyone could start a link for their own Studio account, send
the Microsoft authorize URL to someone else, and have that person's mailbox bound
to the sender's account. So /connect also sets a short-lived HttpOnly cookie
holding a random nonce, the state carries the nonce's hash, and the callback
finishes only in the browser that has the matching cookie — before any token
exchange. Registered in main.py both unprefixed and under the '/api' for-loop
(done by the wiring task, not here).
"""
import hashlib
import hmac
import os
import secrets
import time
import urllib.parse

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from .. import auth as _auth
from ..auth import current_user
from . import configured, graphauth, store, sync

router = APIRouter(prefix="/m365", tags=["m365"])

_STATE_TTL = 600            # signed-state lifetime (seconds)
_STATE_AUD = "m365-oauth"
_LINK_COOKIE = "studio_m365_link"   # binds a sign-in to the browser that started it


def _redirect_uri():
    """The registered OAuth redirect for THIS connector. Prefers an explicit env,
    else derives from STUDIO_PUBLIC_URL, else the local dev default."""
    explicit = os.getenv("STUDIO_GRAPH_REDIRECT_URI")
    if explicit:
        return explicit
    public = os.getenv("STUDIO_PUBLIC_URL", "").rstrip("/")
    if public:
        return public + "/api/m365/oauth/callback"
    return "http://localhost:8000/api/m365/oauth/callback"


def _binding(nonce):
    return hashlib.sha256(nonce.encode()).hexdigest()


def _make_state(user_id, nonce):
    """The state carries the HASH of this browser's link cookie, never the
    nonce itself: the authorize URL can be seen (and forwarded) by anyone."""
    return jwt.encode({"uid": user_id, "aud": _STATE_AUD, "bnd": _binding(nonce),
                       "exp": int(time.time()) + _STATE_TTL},
                      _auth.SECRET, algorithm=_auth.ALGO)


def _read_state(state, nonce):
    """(uid, problem): the initiating user's id when the state is genuine, live
    AND was started in this browser (`nonce` is its link cookie); otherwise
    None and why, for the message."""
    if not state:
        return None, "expired"
    try:
        payload = jwt.decode(state, _auth.SECRET, algorithms=[_auth.ALGO],
                             audience=_STATE_AUD)
    except jwt.PyJWTError:
        return None, "expired"
    bound = str(payload.get("bnd") or "")
    if not nonce or not bound or not hmac.compare_digest(bound, _binding(nonce)):
        return None, "browser"
    return payload.get("uid"), None


def _https(request):
    """Mark the cookie Secure when Studio is served over HTTPS (directly, behind
    a proxy that says so, or per its configured public redirect)."""
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
            or _redirect_uri().startswith("https://"))


# ── Status ───────────────────────────────────────────────────────────────

@router.get("/status")
def status(user=Depends(current_user)):
    if not configured():
        return {"configured": False}
    return {"configured": True, **store.public_status(user["id"])}


# ── Connect ──────────────────────────────────────────────────────────────

@router.post("/connect")
def connect(request: Request, response: Response, user=Depends(current_user)):
    if not configured():
        return {"configured": False}
    mode = os.getenv("STUDIO_GRAPH_AUTH_MODE", "delegated").lower()
    if mode == "app":
        # Application permissions: no user OAuth — provision the row (targeting
        # the user's UPN/email) and let the ticker do the pull.
        store.save_account(user["id"], "app", user.get("email"))
        sync.enqueue_onboard(user["id"], "app")
        return {"connected": True, "mode": "app"}
    cfg = _auth._azure_cfg()
    nonce = secrets.token_urlsafe(32)
    # SameSite=Lax: sent on the top-level GET redirect back from Microsoft,
    # never on a cross-site subrequest. Same lifetime as the state.
    response.set_cookie(_LINK_COOKIE, nonce, max_age=_STATE_TTL, httponly=True,
                        secure=_https(request), samesite="lax", path="/")
    params = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": _redirect_uri(),
        "response_mode": "query",
        "scope": graphauth._DELEGATED_SCOPE,
        "state": _make_state(user["id"], nonce),
    })
    return {"authorize_url":
            f"https://login.microsoftonline.com/{cfg['tenant']}/oauth2/v2.0/authorize?{params}"}


# ── OAuth callback (no bearer; state-matched) ────────────────────────────

@router.get("/oauth/callback")
def oauth_callback(request: Request, code: str = None, state: str = None, error: str = None,
                   error_description: str = None):
    def done(qs):
        out = RedirectResponse(f"{_auth.FRONTEND_URL}/?{qs}")
        out.delete_cookie(_LINK_COOKIE, path="/")      # one sign-in per cookie
        return out

    if not configured():
        return done("m365_error=" + urllib.parse.quote("Microsoft 365 is not configured"))
    if error:
        return done("m365_error=" + urllib.parse.quote((error_description or error)[:300]))
    uid, problem = _read_state(state, request.cookies.get(_LINK_COOKIE))
    if problem == "browser":
        # A genuine state finishing in a browser that did not start it: a
        # forwarded authorize link. Refused BEFORE the code is exchanged, so
        # no token is ever obtained, let alone bound to the link's owner.
        return done("m365_error=" + urllib.parse.quote(
            "This Microsoft 365 sign-in was not started in this browser. Connect Microsoft 365 "
            "from Studio's Data connections, in the browser you are signed in to."))
    if not uid or not code:
        return done("m365_error=" + urllib.parse.quote("Sign-in state expired — try again"))
    cfg = _auth._azure_cfg()

    import requests
    try:
        tok = requests.post(
            f"https://login.microsoftonline.com/{cfg['tenant']}/oauth2/v2.0/token",
            data={"client_id": cfg["client_id"], "client_secret": cfg["secret"],
                  "grant_type": "authorization_code", "code": code,
                  "redirect_uri": _redirect_uri(), "scope": graphauth._DELEGATED_SCOPE},
            timeout=30)
    except Exception:
        return done("m365_error=" + urllib.parse.quote("Token exchange failed"))
    if not tok.ok:
        return done("m365_error=" + urllib.parse.quote("Token exchange rejected"))
    data = tok.json()
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    if not access or not refresh:
        return done("m365_error=" + urllib.parse.quote("Microsoft did not return a refresh token"))

    # Resolve the AAD object id for ACL principal resolution (best-effort).
    graph_user_id = None
    try:
        me = requests.get("https://graph.microsoft.com/v1.0/me?$select=id",
                          headers={"Authorization": f"Bearer {access}"}, timeout=30)
        if me.ok:
            graph_user_id = me.json().get("id")
    except Exception:
        pass

    store.save_account(uid, "delegated", graph_user_id)
    store.set_tokens(uid, access, refresh, time.time() + float(data.get("expires_in", 3600)))
    sync.enqueue_onboard(uid, "delegated")
    return done("m365_connected=1")


# ── Webhook (no bearer; clientState-validated) ───────────────────────────

@router.get("/webhook")
def webhook_validate(validationToken: str = None):
    if validationToken is not None:
        return PlainTextResponse(validationToken)
    return Response(status_code=202)


@router.post("/webhook")
async def webhook(request: Request):
    validation = request.query_params.get("validationToken")
    if validation is not None:
        # Graph's 10s subscription handshake — echo the token as text/plain.
        return PlainTextResponse(validation)
    if not configured():
        return Response(status_code=202)
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=202)
    for note in (body.get("value") or [])[:512]:   # bound synchronous DB reads
        sub_id = note.get("subscriptionId")
        if sub_id:
            try:
                sync.process_notification(sub_id, note.get("clientState"))
            except Exception:
                pass
    return Response(status_code=202)          # never fetches Graph inline


# ── Manual sync ──────────────────────────────────────────────────────────

@router.post("/sync")
def sync_now(user=Depends(current_user), user_id: str = None):
    if not configured():
        return {"configured": False}
    target = user["id"]
    if user_id and user_id != user["id"]:
        if user["role"] != "admin":
            raise HTTPException(403, "Only an admin may resync another user")
        target = user_id
    if not store.get_account(target):
        raise HTTPException(404, "Not connected")
    store.update_fields(target, {"next_run_at": time.time(), "claimed_at": None})
    return {"queued": True}


# ── Disconnect ───────────────────────────────────────────────────────────

@router.delete("/connect")
def disconnect(user=Depends(current_user), purge: bool = False):
    if not configured():
        return {"configured": False}
    sync.disconnect(user["id"], purge=purge)
    return {"revoked": True}
