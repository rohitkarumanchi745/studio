"""Microsoft 365 / Graph extraction — dormant unless Azure is configured.

This package pulls a connected user's OWN Microsoft 365 documents (OneDrive /
SharePoint files, Outlook mail) into a PRIVATE, ACL-scoped KAG collection so the
user's agent can ground on them. It reuses Studio's existing seams end to end:
the KAG ingest/parse/retrieve pipeline (kag.py), the autopilot-style background
ticker, Fernet-at-rest secret storage (keys.py pattern), RBAC scope tokens, and
auth.py's raw-requests OAuth token exchange (no msal added).

The single dormancy gate is `configured()`: with no AZURE_* env vars the whole
feature is inert — routes return {"configured": false} 200, the ticker never
starts, onboarding is a no-op, and no Graph call is ever made. Imports stay
deliberately light (this module imports nothing heavy at module load) so
`import app.extraction` never fails when Azure is unconfigured or an optional
parser wheel (python-docx / python-pptx) is missing.
"""

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def configured() -> bool:
    """Master switch: true only when Azure app-registration env is present.

    Reuses auth._azure_cfg() (AZURE_TENANT_ID / AZURE_CLIENT_ID /
    AZURE_CLIENT_SECRET) so this connector is configured exactly like the SSO
    seam it shares a tenant with. Imported lazily so `import app.extraction`
    stays free of the auth import chain until something actually asks.
    """
    from ..auth import _azure_cfg
    return _azure_cfg() is not None
