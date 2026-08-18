"""The auth abstraction: one GraphAuth interface, two interchangeable impls,
both yielding the SAME GraphClient so every line of sync.py is impl-agnostic.

- AppGraphAuth  (application permissions): client_credentials grant, '.default'
  scope, admin-consented and tenant-wide. Nothing per-user is persisted; the app
  token is cached in process memory with its expiry and re-minted on expiry. It
  targets a specific user's data via /users/{graph_user_id}/... .
- DelegatedGraphAuth (per-user OAuth): reads the user's encrypted refresh token
  from store.get_tokens(); access_token() checks the stored expiry and, when
  stale, does a grant_type=refresh_token exchange, re-encrypts + persists the new
  tokens/expiry via store.set_tokens(), and targets /me/... .

Both reuse tenant/client_id/secret from auth._azure_cfg() and mirror auth.py's
raw-requests token exchange — no msal dependency is added, and requests is
imported lazily inside the exchange so the wheel is never required at import.

Tokens are NEVER logged, never placed in a GraphError message (see client.py),
and never returned by any route. If the stored token can't be decrypted (rotated
STUDIO_SECRET), get_tokens() returns None → the account is marked revoked and the
run fails closed until the user reconnects.
"""
import os
import time
from abc import ABC, abstractmethod

from . import configured
from . import store
from .client import GraphClient, GraphError

_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_APP_SCOPE = "https://graph.microsoft.com/.default"
# Kept in sync with routes.connect's authorize scope (offline_access → refresh).
_DELEGATED_SCOPE = "openid offline_access User.Read Files.Read Sites.Read.All Mail.Read"
_REFRESH_LEEWAY = 120.0     # refresh this many seconds BEFORE the token expires

# Process-memory app-token cache: (tenant, client_id) -> (access_token, expiry).
_app_token_cache = {}


def _cfg():
    from ..auth import _azure_cfg
    return _azure_cfg()


class GraphAuth(ABC):
    """access_token() always returns a currently-valid bearer (refreshing
    transparently); client() binds a GraphClient to THIS auth so every REST call
    re-pulls the token; principal_id() is the AAD object id used for ACL
    resolution; root() is the resource base ('/me' or '/users/{id}') so sync is
    impl-agnostic; revoke() clears stored credentials."""

    @abstractmethod
    def access_token(self) -> str:
        ...

    @abstractmethod
    def principal_id(self) -> str:
        ...

    @abstractmethod
    def root(self) -> str:
        ...

    def client(self) -> GraphClient:
        return GraphClient(self)

    def revoke(self) -> None:
        store.revoke(self.user_id)


class AppGraphAuth(GraphAuth):
    """Application permissions — tenant-wide, admin-consented service identity."""

    def __init__(self, user_id, account):
        self.user_id = user_id
        self.account = account or {}
        self.graph_user_id = self.account.get("graph_user_id")

    def root(self):
        return f"/users/{self.graph_user_id}"

    def principal_id(self):
        return self.graph_user_id

    def access_token(self):
        cfg = _cfg()
        if not cfg:
            raise GraphError("Azure is not configured")
        key = (cfg["tenant"], cfg["client_id"])
        cached = _app_token_cache.get(key)
        now = time.time()
        if cached and cached[1] - _REFRESH_LEEWAY > now:
            return cached[0]
        import requests
        try:
            r = requests.post(
                _TOKEN_URL.format(tenant=cfg["tenant"]),
                data={"client_id": cfg["client_id"], "client_secret": cfg["secret"],
                      "grant_type": "client_credentials", "scope": _APP_SCOPE},
                timeout=30)
        except Exception as e:
            raise GraphError(f"app token request failed: {type(e).__name__}")
        if not r.ok:
            raise GraphError(f"app token exchange failed: {r.status_code}")
        data = r.json()
        access = data.get("access_token")
        if not access:
            raise GraphError("app token exchange returned no token")
        _app_token_cache[key] = (access, now + float(data.get("expires_in", 3600)))
        return access


class DelegatedGraphAuth(GraphAuth):
    """Per-user OAuth — the connected user's own delegated access."""

    def __init__(self, user_id, account):
        self.user_id = user_id
        self.account = account or {}
        self.graph_user_id = self.account.get("graph_user_id")

    def root(self):
        return "/me"

    def principal_id(self):
        return self.graph_user_id

    def access_token(self):
        toks = store.get_tokens(self.user_id)
        if not toks:
            # Rotated secret / wiped tokens → fail closed and force a reconnect.
            store.update_fields(self.user_id, {"status": "revoked"})
            raise GraphError("no valid Microsoft credentials; reconnect required")
        now = time.time()
        exp = toks.get("expires_at") or 0
        if toks.get("access") and exp - _REFRESH_LEEWAY > now:
            return toks["access"]
        return self._refresh(toks["refresh"])

    def _refresh(self, refresh_token):
        cfg = _cfg()
        if not cfg:
            raise GraphError("Azure is not configured")
        import requests
        try:
            r = requests.post(
                _TOKEN_URL.format(tenant=cfg["tenant"]),
                data={"client_id": cfg["client_id"], "client_secret": cfg["secret"],
                      "grant_type": "refresh_token", "refresh_token": refresh_token,
                      "scope": _DELEGATED_SCOPE},
                timeout=30)
        except Exception as e:
            raise GraphError(f"token refresh failed: {type(e).__name__}")
        if not r.ok:
            store.update_fields(self.user_id,
                                {"status": "error", "last_error": "token refresh rejected"})
            raise GraphError(f"token refresh failed: {r.status_code}")
        data = r.json()
        access = data.get("access_token")
        if not access:
            raise GraphError("token refresh returned no token")
        new_refresh = data.get("refresh_token") or refresh_token
        expires_at = time.time() + float(data.get("expires_in", 3600))
        store.set_tokens(self.user_id, access, new_refresh, expires_at)
        return access


def for_user(user_id):
    """The GraphAuth for a connected user, or None when dormant / not connected.
    Picks the impl from graph_accounts.auth_mode, falling back to the env
    STUDIO_GRAPH_AUTH_MODE (default 'delegated')."""
    if not configured():
        return None
    acct = store.get_account(user_id)
    if not acct:
        return None
    mode = (acct.get("auth_mode") or os.getenv("STUDIO_GRAPH_AUTH_MODE", "delegated")).lower()
    if mode == "app":
        return AppGraphAuth(user_id, acct)
    return DelegatedGraphAuth(user_id, acct)
