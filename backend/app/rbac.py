"""Role-based access control over data sources and tables.

Three roles for the prototype. "*" means every table in the source.
Enforced in three places: the catalog (what you can see), the query guard
(what SQL may touch), and the agent's schema context (what the model is told
exists). Swap this dict for a policy table / Azure AD group mapping later.
"""

_MARKETING = ["ga4", "braze", "powerbi_sap", "dynamic_yield", "qualtrics",
              "google_ads", "microsoft_ads", "sprinklr", "algolia"]

POLICIES = {
    "admin": {
        "demo": "*", "snowflake": "*", "databricks": "*",
        **{m: "*" for m in _MARKETING},
    },
    "analyst": {
        "demo": "*", "snowflake": "*", "databricks": "*",
        **{m: "*" for m in _MARKETING},
    },
    "viewer": {
        # Viewers get aggregate-friendly tables only — no customer PII.
        "demo": {"sales", "web_traffic"},
    },
}


def allowed_sources(role):
    return set(POLICIES.get(role, {}).keys())


def allowed_tables(role, source, all_tables):
    """Filter a source's table list down to what this role may access."""
    policy = POLICIES.get(role, {}).get(source)
    if policy is None:
        return []
    if policy == "*":
        return list(all_tables)
    return [t for t in all_tables if t.lower() in {p.lower() for p in policy}]


def can_access(role, source, table):
    policy = POLICIES.get(role, {}).get(source)
    if policy is None:
        return False
    if table == "*":  # whole-source chat: allowed if the role can see anything here
        return True
    if policy == "*":
        return True
    return table.lower() in {p.lower() for p in policy}
