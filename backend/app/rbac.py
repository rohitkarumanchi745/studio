"""Role-based access control over data sources and tables.

Three roles for the prototype. "*" means every table in the source.
Enforced in three places: the catalog (what you can see), the query guard
(what SQL may touch), and the agent's schema context (what the model is told
exists). Swap this dict for a policy table / Azure AD group mapping later.
"""

_MARKETING = ["ga4", "braze", "powerbi_sap", "dynamic_yield", "qualtrics",
              "google_ads", "microsoft_ads", "sprinklr", "algolia"]

# Object stores are one Studio source each; their "tables" are the datasets an
# admin registers (connectors/objectstore.py), so "*" here means "every
# registered dataset" — the registry, not the bucket, is the boundary.
_OBJECT_STORES = ["s3", "azure_blob", "gcs"]

POLICIES = {
    "admin": {
        "demo": "*", "snowflake": "*", "databricks": "*", "bigquery": "*",
        "neo4j": "*",
        **{o: "*" for o in _OBJECT_STORES},
        **{m: "*" for m in _MARKETING},
    },
    "analyst": {
        "demo": "*", "snowflake": "*", "databricks": "*", "bigquery": "*",
        "neo4j": "*",
        **{o: "*" for o in _OBJECT_STORES},
        **{m: "*" for m in _MARKETING},
    },
    "viewer": {
        # Viewers get aggregate-friendly tables only — no customer PII. The
        # object stores and BigQuery are deliberately absent: their tables are
        # whatever an admin registers or lands in a dataset, so no fixed
        # allowlist can promise a viewer never sees PII. Fail closed until
        # someone names the tables (or governance grants them).
        "demo": {"sales", "web_traffic"},
    },
}


def _policies():
    """The governance document's roles when one is loaded, else the built-in
    POLICIES. This is the single switch that makes RBAC governance-driven."""
    from . import governance
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


def kag_scopes_for(role):
    """Which KAG collection access_scopes a role may retrieve from.

    Mirrors allowed_sources()'s fail-closed shape: a role whose whole policy is
    "*" (admin) reaches every scope, so this returns the "*" sentinel; any other
    role reaches only its own scope. KAG collections are governed exactly like
    data sources — a role's agent (and /kag/search) can only ever surface chunks
    from a collection whose scope is in this set, enforced server-side in SQL, so
    grounding text can never widen data access. Additive: it reads _policies()
    but never touches POLICIES, and adds no new authority a role didn't have.
    """
    pol = _policies().get(role, {})
    if pol == "*" or role == "admin":
        return "*"                       # sentinel: every scope
    return {role}                        # v1: a role reaches its own scope


def can_access(role, source, table):
    policy = _role_policy(role, source)
    if policy is None:
        return False
    if table == "*":  # whole-source chat: allowed if the role can see anything here
        return True
    if policy == "*":
        return True
    return table.lower() in {p.lower() for p in policy}
