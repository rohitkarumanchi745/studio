"""Security contract for persisted cross-source blend results."""

import pytest
from fastapi import HTTPException

from app import blend


USER = {"id": "blend-user", "email": "blend@studio.test", "role": "analyst"}
PARTS = [
    {"name": "left", "source": "postgres", "table": "orders",
     "sql": "SELECT id FROM orders"},
    {"name": "right", "source": "snowflake", "table": "sales",
     "sql": "SELECT id FROM sales"},
]


def _verified(_user, source, _table, _sql, full_rows=False):
    assert full_rows is True
    return {
        "ok": True,
        # Deliberately differs from the submitted SQL: provenance must contain
        # the gateway-cleaned statement that actually ran.
        "sql": f"SELECT id FROM cleaned_{source} LIMIT 50000",
        "columns": ["id"],
        "rows": [[1]],
        "row_count": 1,
    }


def test_blend_mints_verified_provenance_under_one_policy_identity(monkeypatch):
    identities = iter(["policy-a", "policy-a"])
    monkeypatch.setattr(blend.governance, "identity", lambda: next(identities))
    monkeypatch.setattr(blend.queries, "verify_sql", _verified)

    result = blend.blend(USER, PARTS)

    assert result["rows"] == [[1], [1]]
    assert result["blend_provenance"] == {
        "kind": "blend_v1",
        "governance_identity": "policy-a",
        "inputs": [
            {"source": "postgres", "table": "orders",
             "sql": "SELECT id FROM cleaned_postgres LIMIT 50000"},
            {"source": "snowflake", "table": "sales",
             "sql": "SELECT id FROM cleaned_snowflake LIMIT 50000"},
        ],
    }


def test_blend_discards_rows_if_governance_changes_mid_blend(monkeypatch):
    identities = iter(["policy-a", "policy-b"])
    monkeypatch.setattr(blend.governance, "identity", lambda: next(identities))
    monkeypatch.setattr(blend.queries, "verify_sql", _verified)

    with pytest.raises(HTTPException) as caught:
        blend.blend(USER, PARTS)

    assert caught.value.status_code == 409
    assert "Governance changed" in caught.value.detail
