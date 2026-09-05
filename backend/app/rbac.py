"""Role-based access control over data sources and tables.

Three roles for the prototype. "*" means every table in the source.
Enforced in three places: the catalog (what you can see), the query guard
(what SQL may touch), and the agent's schema context (what the model is told
exists). The built-in policy dict lives in policies.py (a leaf module) so that
governance can scaffold from it without importing this module; it is
re-exported here as rbac.POLICIES for existing references.

Layering: rbac sits ABOVE governance — it resolves a role's policy from the
loaded governance document, falling back to the built-in POLICIES — and
governance never imports rbac. That is what lets both imports be module-level
instead of the lazy pair that used to form a governance ↔ rbac cycle.
"""
from . import governance
# Re-exported for one release: rbac.POLICIES / rbac._MARKETING /
# rbac._OBJECT_STORES keep working. New code imports from .policies.
from .policies import POLICIES, _MARKETING, _OBJECT_STORES  # noqa: F401


def _policies():
    """The governance document's roles when one is loaded, else the built-in
    POLICIES. This is the single switch that makes RBAC governance-driven."""
    gov = governance.policies()
    return gov if gov is not None else POLICIES


def _role_policy(role, source):
    """Resolve a role's policy for a source under the active document. A role
    whose whole `sources` is '*' can see every source and table."""
    role_pol = _policies().get(role, {})
    if role_pol == "*":
        return "*"
    return role_pol.get(source)


def allowed_sources(role):
    pol = _policies().get(role, {})
    if pol == "*":
        return {s["name"] for s in _all_source_names()}
    return set(pol.keys())


def _all_source_names():
    from .connectors import all_sources
    return all_sources()


def allowed_tables(role, source, all_tables):
    """Filter a source's table list down to what this role may access."""
    policy = _role_policy(role, source)
    if policy is None:
        return []
    if policy == "*":
        return list(all_tables)
    return [t for t in all_tables if t.lower() in {p.lower() for p in policy}]


def kag_scopes_for(role, user_id=None):
    """Which KAG collection access_scopes a role may retrieve from.

    Mirrors allowed_sources()'s fail-closed shape: a role whose whole policy is
    "*" (admin) reaches every scope, so this returns the "*" sentinel; any other
    role reaches only its own scope. KAG collections are governed exactly like
    data sources — a role's agent (and /kag/search) can only ever surface chunks
    from a collection whose scope is in this set, enforced server-side in SQL, so
    grounding text can never widen data access. Additive: it reads _policies()
    but never touches POLICIES, and adds no new authority a role didn't have.

    Additive user_id (default None reproduces the exact prior output): when a
    caller identity is threaded through, a non-admin also reaches its OWN private
    per-user scope 'u:{user_id}' — the token M365/Graph documents are ingested
    under — so a user retrieves their own connected documents and NOBODY else's.
    Admin/'*' is unchanged (reaches every scope).
    """
    pol = _policies().get(role, {})
    if pol == "*" or role == "admin":
        return "*"                       # sentinel: every scope
    scopes = {role}                      # v1: a role reaches its own scope
    if user_id:
        scopes.add("u:" + user_id)       # plus this user's private M365 scope
    return scopes


def can_access(role, source, table):
    policy = _role_policy(role, source)
    if policy is None:
        return False
    if table == "*":  # whole-source chat: allowed if the role can see anything here
        return True
    if policy == "*":
        return True
    return table.lower() in {p.lower() for p in policy}
