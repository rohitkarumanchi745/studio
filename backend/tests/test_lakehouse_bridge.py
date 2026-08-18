"""Lakehouse write→read bridge — objectstore.register_output.

The agent loop ends by querying a Spark job's Parquet output in chat, which
means the output URI has to become an objectstore dataset. That last hop is an
exfil/priv surface (a job that names its own output could register
s3://someone-elses-bucket/* or smuggle credentials with an admin's authority),
so these lock the guarantees down: an in-prefix output registers and is wired
into the same view machinery every dataset uses; an out-of-prefix URI and a
credential-smuggling URI are refused with NO row written; re-registering
updates in place; and the new dataset is RBAC-scoped exactly like any other —
a role without the object store cannot see it. No live S3/Spark: registration
is app-DB + validation logic, verified against a throwaway SQLite file, and the
"queryable as a view" claim is proven against the vetted view-DDL template. Run
from the backend directory:

    python -m pytest tests/test_lakehouse_bridge.py -q
"""
import os
import tempfile

# Point the app at a throwaway SQLite file BEFORE app.db computes DB_PATH.
os.environ["STUDIO_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="studio-lakehouse-test-"), "studio.db")

import pytest
from fastapi import HTTPException

from app import db, rbac
from app.connectors import objectstore

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst", "name": "Ana"}

# A registered SOURCE the Spark job "read" — its bucket/prefix is the default
# confinement boundary when no explicit prefix / env is set.
SRC = ("s3", "orders", "s3://studio-lake/orders/*.parquet", "parquet")


@pytest.fixture(autouse=True)
def _tables():
    db.init_db()
    objectstore.init_tables()
    c = db._conn()
    c.execute("DELETE FROM objectstore_datasets")
    c.commit()
    c.close()
    objectstore._upsert_dataset(*SRC)  # the job's source dataset


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("STUDIO_SPARK_OUTPUT_PREFIX", raising=False)
    monkeypatch.delenv("STUDIO_OBJECT_DATASETS", raising=False)


def _names(source="s3"):
    return {d["name"] for d in objectstore.datasets(source)}


def _row_count(source, name):
    c = db._conn()
    n = c.execute("SELECT COUNT(*) AS n FROM objectstore_datasets WHERE source=? AND name=?",
                  (source, name)).fetchone()["n"]
    c.close()
    return n


# ── valid in-prefix output registers + is queryable as a view ────────────

def test_in_prefix_output_registers_and_is_queryable():
    uri = "s3://studio-lake/orders/enriched/*.parquet"
    row = objectstore.register_output(ADMIN, "s3", "orders_enriched", uri,
                                      from_dataset="orders")
    assert row["name"] == "orders_enriched" and row["uri"] == uri
    assert row["source"] == "s3" and row["format"] == "parquet" and row["id"]

    # In the vetted registry, and exposed as a queryable view name by the
    # connector — the same list agents' SQL is allowlisted against.
    d = next(d for d in objectstore.datasets("s3") if d["name"] == "orders_enriched")
    assert d["uri"] == uri
    assert "orders_enriched" in objectstore.ObjectStoreConnector("s3").list_tables()

    # The view DDL the registry hands DuckDB is executable — prove it against a
    # local parquet (live S3 is dormant here) so "queryable as a view" is real,
    # not merely "present in a list".
    import duckdb
    tmp = tempfile.mkdtemp(prefix="lakehouse-parquet-")
    path = os.path.join(tmp, "part.parquet")
    con = duckdb.connect(":memory:")
    con.execute(f"COPY (SELECT 1 AS id, 'a' AS grp) TO '{path}' (FORMAT parquet)")
    ddl = objectstore._view_sql({"name": d["name"], "format": "parquet", "uri": path})
    con.execute(ddl)
    assert con.execute('SELECT id FROM "orders_enriched"').fetchone()[0] == 1
    con.close()


def test_env_prefix_allows_output(monkeypatch):
    monkeypatch.setenv("STUDIO_SPARK_OUTPUT_PREFIX", "s3://studio-lake/exports/")
    uri = "s3://studio-lake/exports/daily/*.parquet"
    row = objectstore.register_output(ADMIN, "s3", "daily_export", uri)
    assert row["uri"] == uri and "daily_export" in _names()


def test_explicit_allowed_prefix_list(monkeypatch):
    monkeypatch.setenv("STUDIO_SPARK_OUTPUT_PREFIX", "s3://ignored/")  # explicit arg wins
    uri = "s3://studio-lake/orders/out/*.parquet"
    row = objectstore.register_output(ADMIN, "s3", "orders_out", uri,
                                      allowed_prefix=["s3://studio-lake/orders/"])
    assert row["uri"] == uri


# ── out-of-prefix is REJECTED, nothing written ───────────────────────────

def test_out_of_prefix_rejected():
    uri = "s3://someone-elses-bucket/loot/*.parquet"
    with pytest.raises(objectstore.RegistrationError):
        objectstore.register_output(ADMIN, "s3", "exfil", uri, from_dataset="orders")
    assert "exfil" not in _names()
    assert _row_count("s3", "exfil") == 0


def test_no_boundary_configured_rejects():
    # No allowed_prefix, no env, and from_dataset names an unregistered source
    # → empty prefix set → every URI refused (fail-safe default).
    uri = "s3://studio-lake/orders/enriched/*.parquet"
    with pytest.raises(objectstore.RegistrationError):
        objectstore.register_output(ADMIN, "s3", "orphan", uri, from_dataset="nope")
    assert "orphan" not in _names()


# ── credential-smuggling URI is REJECTED ─────────────────────────────────

def test_credential_smuggling_rejected():
    uri = ("s3://studio-lake/orders/enriched/*.parquet"
           "?s3_access_key_id=AKIA&s3_secret_access_key=shh")
    with pytest.raises(objectstore.RegistrationError):
        objectstore.register_output(ADMIN, "s3", "smuggled", uri, from_dataset="orders")
    assert "smuggled" not in _names()
    assert _row_count("s3", "smuggled") == 0


def test_wrong_scheme_rejected():
    with pytest.raises(objectstore.RegistrationError):
        objectstore.register_output(ADMIN, "s3", "wrong", "gs://studio-lake/orders/x/*.parquet",
                                    allowed_prefix="s3://studio-lake/orders/")


# ── re-registering updates (no duplicate) ────────────────────────────────

def test_reregister_updates_in_place():
    uri = "s3://studio-lake/orders/enriched/*.parquet"
    first = objectstore.register_output(ADMIN, "s3", "orders_enriched", uri,
                                        from_dataset="orders")
    second = objectstore.register_output(ADMIN, "s3", "orders_enriched", uri,
                                         from_dataset="orders")
    assert _row_count("s3", "orders_enriched") == 1        # no dup under the unique index
    assert first["id"] != second["id"]                     # DELETE-then-INSERT: fresh row
    assert [d for d in objectstore.datasets("s3")
            if d["name"] == "orders_enriched"][0]["uri"] == uri


# ── RBAC unchanged: a role without the object store cannot see it ─────────

def test_new_dataset_is_rbac_scoped():
    objectstore.register_output(ADMIN, "s3", "orders_enriched",
                                "s3://studio-lake/orders/enriched/*.parquet",
                                from_dataset="orders")
    names = list(_names())
    assert "orders_enriched" in names
    # viewer has no "s3" policy at all → the registered dataset is invisible,
    # exactly like every other objectstore dataset. No policy was broadened.
    assert rbac.allowed_tables("viewer", "s3", names) == []
    assert rbac.can_access("viewer", "s3", "orders_enriched") is False
    # admin (and analyst) see it via the ordinary _OBJECT_STORES "*" policy.
    assert "orders_enriched" in rbac.allowed_tables("admin", "s3", names)
    assert rbac.can_access("analyst", "s3", "orders_enriched") is True


# ── admin authority is the same gate as manual registration ──────────────

def test_non_admin_caller_rejected():
    with pytest.raises(HTTPException) as e:
        objectstore.register_output(ANALYST, "s3", "orders_enriched",
                                    "s3://studio-lake/orders/enriched/*.parquet",
                                    from_dataset="orders")
    assert e.value.status_code == 403
    assert "orders_enriched" not in _names()


# ── prefix helper ────────────────────────────────────────────────────────

def test_dataset_prefix_helper():
    assert objectstore.dataset_prefix("s3", "orders") == "s3://studio-lake/orders/"
    assert objectstore.dataset_prefix("s3", "missing") is None
    assert objectstore._prefix_of("s3://b/x/y/z.parquet") == "s3://b/x/y/"
    assert objectstore._prefix_of("s3://b/orders/*.parquet") == "s3://b/orders/"
    assert objectstore._prefix_of("s3://bucket") == "s3://bucket"  # bare root intact


# ── register_spark_output wrapper: fail-safe, admin-approval gated ───────

def test_spark_output_wrapper_requires_admin_approval():
    out = {"source": "s3", "name": "orders_enriched",
           "uri": "s3://studio-lake/orders/enriched/*.parquet", "from_dataset": "orders"}
    # No human_by → not admin-approved → refused, nothing written.
    res = objectstore.register_spark_output(out, {"id": "j1"}, ADMIN)
    assert "error" in res and "orders_enriched" not in _names()
    # human_by set (an admin approved the run) → registers, returns the row.
    res = objectstore.register_spark_output(out, {"id": "j1", "human_by": "admin@studio.test"}, ADMIN)
    assert res.get("registered") == "orders_enriched"
    assert res["dataset"]["uri"] == out["uri"] and "orders_enriched" in _names()


def test_spark_output_wrapper_surfaces_out_of_prefix():
    out = {"source": "s3", "name": "exfil",
           "uri": "s3://someone-elses-bucket/loot/*.parquet", "from_dataset": "orders"}
    res = objectstore.register_spark_output(out, {"id": "j2", "human_by": "admin@studio.test"}, ADMIN)
    assert "error" in res and "exfil" not in _names()


def test_spark_output_wrapper_never_raises_on_bad_shape():
    assert "error" in objectstore.register_spark_output(None, {"human_by": "x"}, ADMIN)


# ── DEFECT #2 regressions: sibling-dir + payload-supplied boundary ───────

def test_sibling_dir_prefix_bypass_rejected():
    """A look-alike sibling directory must NOT satisfy the boundary. With the
    allowed prefix s3://studio-lake/orders/, the sibling s3://studio-lake/
    orders-EVIL/... shares the raw-startswith stem but not the path segment, so
    a separator-aware match refuses it. Regression for the bare-startswith hole."""
    uri = "s3://studio-lake/orders-EVIL/x/*.parquet"
    with pytest.raises(objectstore.RegistrationError):
        objectstore.register_output(ADMIN, "s3", "sibling", uri,
                                    allowed_prefix="s3://studio-lake/orders")
    assert "sibling" not in _names()
    assert _row_count("s3", "sibling") == 0
    # The legitimate in-segment child still registers under the same boundary.
    ok = objectstore.register_output(ADMIN, "s3", "child",
                                     "s3://studio-lake/orders/enriched/*.parquet",
                                     allowed_prefix="s3://studio-lake/orders")
    assert ok["name"] == "child" and "child" in _names()


def test_spark_output_wrapper_ignores_payload_allowed_prefix():
    """The Spark path must derive the boundary SERVER-SIDE only. A payload that
    smuggles its own allowed_prefix (and a from_dataset) pointing at a foreign
    bucket is refused: the wrapper ignores output['allowed_prefix'] and confines
    to the server-resolved source dataset's prefix (s3://studio-lake/orders/)."""
    out = {"source": "s3", "name": "exfil2",
           "uri": "s3://someone-elses-bucket/out/*.parquet",
           "allowed_prefix": "s3://someone-elses-bucket/",   # attacker-set boundary
           "from_dataset": "orders"}
    res = objectstore.register_spark_output(
        out, {"id": "j3", "human_by": "admin@studio.test"}, ADMIN)
    assert "error" in res and "exfil2" not in _names()
    assert _row_count("s3", "exfil2") == 0


def test_spark_output_wrapper_ignores_payload_prefix_even_with_no_source():
    """Even absent any real source dataset, a payload allowed_prefix cannot
    manufacture a boundary: from_dataset names an unregistered dataset and env
    is unset, so the server-side allowlist is empty → refuse (fail-safe),
    regardless of the foreign allowed_prefix the payload carries."""
    out = {"source": "s3", "name": "exfil3",
           "uri": "s3://someone-elses-bucket/out/*.parquet",
           "allowed_prefix": "s3://someone-elses-bucket/",
           "from_dataset": "does-not-exist"}
    res = objectstore.register_spark_output(
        out, {"id": "j4", "human_by": "admin@studio.test"}, ADMIN)
    assert "error" in res and "exfil3" not in _names()
