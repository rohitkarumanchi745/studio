"""Direct Entra ID (Azure AD) bearer-token validation.

Production auth path: the SPA acquires an Entra access token (MSAL.js) and
sends it as `Authorization: Bearer <token>`. This module validates the token
signature against Microsoft's published JWKS keys (RS256), checks issuer /
audience / expiry, and derives the Studio role from the token's claims:

- `roles`  — Entra **app roles** (assign users to app roles named
             "admin" / "analyst" / "viewer" in the app registration). This is
             the recommended production mapping.
- `groups` — group **object IDs** (when the app registration emits the groups
             claim). Map IDs via AZURE_GROUP_ROLE_MAP alongside display names.

The interactive redirect flow in auth.py remains for browsers without MSAL;
both paths converge on the same per-user record, RBAC, and audit.
"""
import json
import os
import threading

import jwt

_RANK = {"admin": 3, "analyst": 2, "viewer": 1}
_jwk_clients = {}
_lock = threading.Lock()


def _jwk_client(tenant):
    url = f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
    with _lock:
        if url not in _jwk_clients:
            _jwk_clients[url] = jwt.PyJWKClient(url, cache_keys=True, lifespan=86400)
        return _jwk_clients[url]


def _role_map():
    try:
        return json.loads(os.getenv("AZURE_GROUP_ROLE_MAP", "{}"))
    except json.JSONDecodeError:
        return {}


def validate_entra_token(token):
    """Return {email, name, role} for a valid Entra token, else None.

    None means "not an Entra token / not configured" — the caller decides
    whether other token types apply. Signature, issuer, audience, and expiry
    are all enforced before any claim is trusted.
    """
    tenant = os.getenv("AZURE_TENANT_ID")
    if not tenant:
        return None
    audience = os.getenv("AZURE_API_AUDIENCE") or os.getenv("AZURE_CLIENT_ID")
    if not audience:
        return None
    try:
        signing_key = _jwk_client(tenant).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=f"https://login.microsoftonline.com/{tenant}/v2.0",
        )
    except Exception:
        return None

    email = (claims.get("preferred_username") or claims.get("email")
             or claims.get("upn") or "").lower()
    if not email:
        return None
    name = claims.get("name") or email.split("@")[0]

    role = "viewer"
    # Entra app roles ("admin"/"analyst"/"viewer") — highest wins
    for r in claims.get("roles") or []:
        r = str(r).lower()
        if _RANK.get(r, 0) > _RANK[role]:
            role = r
    # Group object IDs via AZURE_GROUP_ROLE_MAP
    mapping = _role_map()
    for g in claims.get("groups") or []:
        r = mapping.get(g)
        if r and _RANK.get(r, 0) > _RANK[role]:
            role = r

    return {"email": email, "name": name, "role": role}
