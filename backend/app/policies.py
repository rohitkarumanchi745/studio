"""Built-in RBAC policies — pure data, a leaf module.

This is the fallback access document Studio uses when no governance YAML is
loaded: which sources (and which tables in them) each role may reach. It
lives in its own module so that BOTH rbac.py (which resolves a role's policy)
and governance.py (which scaffolds a YAML template from it) can import it at
module level. Before the split governance lazily imported rbac for POLICIES
and rbac lazily imported governance for the loaded document — a two-module
import cycle that only worked because both imports were deferred.

Invariants:
  - Imports nothing from ``app`` (enforced by tests/test_layering.py). Keep it
    that way: anything imported here is importable by every other module.
  - rbac re-exports POLICIES / _MARKETING / _OBJECT_STORES, so ``rbac.POLICIES``
    keeps working; new code should import from here.
  - "*" means every table in the source. Swap this dict for a policy table /
    Azure AD group mapping later.
"""

_MARKETING = ["ga4", "braze", "powerbi_sap", "dynamic_yield", "qualtrics",
              "google_ads", "microsoft_ads", "sprinklr", "algolia"]

# Object stores are one Studio source each; their "tables" are the datasets an
# admin registers (connectors/objectstore.py), so "*" here means "every
# registered dataset" — the registry, not the bucket, is the boundary.
_OBJECT_STORES = ["s3", "azure_blob", "gcs"]

POLICIES = {
    "admin": {
        "demo": "*", "snowflake": "*", "postgres": "*", "databricks": "*",
        "bigquery": "*", "neo4j": "*",
        **{o: "*" for o in _OBJECT_STORES},
        **{m: "*" for m in _MARKETING},
    },
    "analyst": {
        "demo": "*", "snowflake": "*", "postgres": "*", "databricks": "*",
        "bigquery": "*", "neo4j": "*",
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
