"""The ACL → KAG scope resolver — fail-closed BY CONSTRUCTION.

Each connected account belongs to exactly one Studio user, and their M365
documents land in ONE private collection m365-{user_id} whose access_scope is the
per-user token 'u:{user_id}'. Retrievability is made to mirror the SOURCE ACL by
resolving, per item, whether THIS user may actually read it — items the user
cannot read are never ingested at all (deny, never a broader/open scope).

principal_set(client, graph_user_id) = {the user's AAD object id} ∪ {object ids
of every AAD group the user is memberOf}, fetched once per sync run. If that call
fails (throttle / error / unparseable), it returns the EMPTY set — which makes
every drive item resolve to None below.

resolve_access_scope(user, kind, graph_id, client, principals):
  - mail: the mailbox owner IS the connected user → always granted → 'u:{id}'.
  - drive: GET .../drive/items/{id}/permissions; collect the granted user+group
    ids from grantedToV2 / grantedToIdentitiesV2 (and their non-suffixed
    variants). Accessible IFF principals ∩ granted is non-empty (a direct grant
    to the user, or to a group they belong to). Anonymous / 'anyone' links carry
    no user/group id, so they DON'T widen Studio visibility.
  - EVERY other case → None (SKIP the item): no intersecting grant, empty
    principal set, a permissions call that 4xx/5xx/throttles, or an unparseable
    response. None is never a role scope, 'admin', or any open scope.
"""

SCOPE_PREFIX = "u:"


def principal_set(client, graph_user_id):
    """{object_id} ∪ memberOf group ids, cached per run by the caller. Empty set
    on ANY failure so drive ACL resolution fails closed."""
    principals = set()
    if graph_user_id:
        principals.add(str(graph_user_id))
    try:
        path = f"{client.auth.root()}/memberOf?$select=id&$top=999"
        while path:
            data = client.get(path)
            for g in data.get("value", []):
                gid = g.get("id")
                if gid:
                    principals.add(str(gid))
            path = data.get("@odata.nextLink")
        return principals
    except Exception:
        return set()


def _granted_ids(permissions):
    """The set of user/group object ids a drive item's permissions actually
    grant to a principal. Link/anonymous grants (no user/group id) contribute
    nothing, so a public link never widens Studio visibility."""
    granted = set()
    for perm in (permissions.get("value") or []):
        for key in ("grantedToV2", "grantedTo"):
            ident = perm.get(key) or {}
            for who in ("user", "group", "siteUser", "siteGroup", "application", "device"):
                obj = ident.get(who) or {}
                oid = obj.get("id")
                if oid:
                    granted.add(str(oid))
        for ident in ((perm.get("grantedToIdentitiesV2") or [])
                      + (perm.get("grantedToIdentities") or [])):
            for who in ("user", "group", "siteUser", "siteGroup"):
                obj = ident.get(who) or {}
                oid = obj.get("id")
                if oid:
                    granted.add(str(oid))
    return granted


def resolve_access_scope(user, kind, graph_id, client, principals):
    """'u:{user_id}' when the source ACL grants THIS user read; None otherwise
    (→ skip ingest). Fail-closed on every ambiguity."""
    scope = SCOPE_PREFIX + user["id"]
    if kind == "mail":
        return scope                              # mailbox owner == connected user
    if kind != "drive":
        return None
    if not principals:
        return None                               # couldn't resolve identity → deny
    try:
        data = client.get(f"{client.auth.root()}/drive/items/{graph_id}/permissions")
    except Exception:
        return None                               # throttle / 4xx/5xx → deny
    if principals & _granted_ids(data):
        return scope
    return None
