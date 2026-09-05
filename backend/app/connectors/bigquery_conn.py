"""BigQuery connector. Configure via env (see .env.example); requires
`pip install google-cloud-bigquery` (commented in requirements.txt).

Read-only by design, and cost-capped: BigQuery bills per byte scanned, so every
query carries a `maximum_bytes_billed` cap. BigQuery estimates the scan before
running and rejects a query that exceeds the cap at no charge — that is the only
control that actually stops a runaway bill. `LIMIT` does not: you are billed for
every byte of the referenced columns regardless of how many rows come back.
Writes stay unimplemented, so the base class refuses run_script/submit_spark_job.
"""
import base64
import json
import os
import threading

from .base import Connector, jsonify_rows


class BigQueryConnector(Connector):
    name = "bigquery"
    dialect = "bigquery"

    def __init__(self):
        # No client and no SDK import here — _REGISTRY instantiates every
        # connector at import time, before the package is known to exist.
        self._client_obj = None
        self._client_lock = threading.Lock()

    def _cfg(self):
        return {
            "project": os.getenv("BIGQUERY_PROJECT", "") or os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            "dataset": os.getenv("BIGQUERY_DATASET", ""),
            "location": os.getenv("BIGQUERY_LOCATION", ""),   # e.g. US, us-central1
            "credentials_json": os.getenv("BIGQUERY_CREDENTIALS_JSON", ""),
            "key_file": os.getenv("GOOGLE_APPLICATION_CREDENTIALS", ""),
        }

    def _creds_info(self):
        """Parse the inline service-account key. A PaaS like Railway can set env
        vars but not files, so the whole key travels in BIGQUERY_CREDENTIALS_JSON.
        Base64 is accepted as well — some dashboards unescape the `\\n` sequences
        inside `private_key`, after which the raw JSON no longer parses."""
        raw = self._cfg()["credentials_json"].strip()
        if not raw:
            return {}
        try:
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode()
            info = json.loads(raw)
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}  # a malformed key reads as "not configured", never a boot error

    def _project(self):
        """Explicit env wins; otherwise the service-account key carries its own
        project_id. ADC users must set BIGQUERY_PROJECT: resolving the project
        from ADC means calling google.auth.default(), which probes the GCE
        metadata server and blocks for seconds — and configured() below runs on
        every /catalog/sources request."""
        return self._cfg()["project"] or self._creds_info().get("project_id", "")

    def qualifiers(self):
        """The configured dataset, bare and project-qualified — the only
        namespace RBAC describes.

        A service account usually holds bigquery.dataViewer on more than one
        dataset, so `other_dataset.customers` (or another project's) is outside
        what the catalog was built from and the query guard refuses it. Env and
        the inline key only — never a network call, since this runs per query.
        """
        dataset = (self._cfg()["dataset"] or "").strip().lower()
        if not dataset:
            return frozenset()
        project = (self._project() or "").strip().lower()
        out = {dataset}
        if project:
            out.add(f"{project}.{dataset}")
        return frozenset(out)

    def configured(self):
        cfg = self._cfg()
        if not (cfg["dataset"] and self._project()):
            return False
        try:
            from google.cloud import bigquery  # noqa: F401
            return True
        except ImportError:
            return False

    def _dataset_ref(self):
        return f"{self._project()}.{self._cfg()['dataset']}"

    def _credentials(self):
        """Inline JSON → key file → ADC (None lets the client resolve it)."""
        from google.oauth2 import service_account

        scopes = ["https://www.googleapis.com/auth/bigquery"]
        info = self._creds_info()
        if info:
            return service_account.Credentials.from_service_account_info(info, scopes=scopes)
        key_file = self._cfg()["key_file"]
        if key_file:
            return service_account.Credentials.from_service_account_file(key_file, scopes=scopes)
        return None

    def _job_config(self):
        from google.cloud import bigquery

        cfg = self._cfg()
        job = bigquery.QueryJobConfig(
            # The hard cost stop. Floor is 10 MiB (BigQuery's per-query minimum
            # billing) — a cap below that rejects every query.
            maximum_bytes_billed=int(os.getenv("BIGQUERY_MAX_BYTES_BILLED", str(10 ** 9))),
            # The REST default for useLegacySql is true; pin it off so a legacy
            # dialect can never leak in behind a guard that only knows GoogleSQL.
            use_legacy_sql=False,
            use_query_cache=True,
            job_timeout_ms=int(os.getenv("BIGQUERY_TIMEOUT_MS", "60000")),
            labels={"app": "studio"},
        )
        if cfg["dataset"] and self._project():
            # What makes bare `FROM orders` resolve — and bare names are exactly
            # what the query guard's allowlist can check, so the agent never
            # needs a backticked `project.dataset.table` reference.
            job.default_dataset = self._dataset_ref()
        return job


    # ── Client (built once, cached; no reconnect loop needed) ───────────────

    def _client(self):
        """Lazily build and cache the client. Unlike the warehouse connectors
        there is no _execute reconnect wrapper: this is an HTTPS/REST client with
        no session to expire, and google-api-core already retries transient
        failures on query/list_tables/get_table."""
        with self._client_lock:
            if self._client_obj is None:
                from google.cloud import bigquery
                cfg = self._cfg()
                self._client_obj = bigquery.Client(
                    project=self._project() or None,
                    credentials=self._credentials(),
                    location=cfg["location"] or None,
                    # Also set per call, but a client-level default means any
                    # future code path that forgets a job_config stays capped.
                    default_query_job_config=self._job_config(),
                )
            return self._client_obj

    def close(self):
        with self._client_lock:
            if self._client_obj is not None:
                try:
                    self._client_obj.close()
                except Exception:
                    pass
                self._client_obj = None

    def list_tables(self):
        """Bare table ids in the configured dataset. tables.list is a metadata
        call — zero bytes billed — whereas INFORMATION_SCHEMA would bill the
        10 MB minimum on every catalog refresh. Views are included alongside
        tables: they are SELECT-able, so they belong in the catalog and in the
        RBAC allowlist."""
        c = self._client()
        return sorted(t.table_id for t in c.list_tables(self._dataset_ref()))

    def get_schema(self, table):
        c = self._client()
        t = c.get_table(f"{self._dataset_ref()}.{table}")
        # field_type uses the legacy spellings (INTEGER/FLOAT/BOOLEAN/RECORD),
        # and mode=REPEATED is how BigQuery expresses ARRAY<T> — flatten that
        # into the type string, since the catalog wants one string per column.
        return [{"name": f.name,
                 "type": f.field_type + ("[]" if f.mode == "REPEATED" else "")}
                for f in t.schema]

    def run_query(self, sql):
        c = self._client()
        it = c.query_and_wait(
            sql,
            job_config=self._job_config(),
            max_results=int(os.getenv("STUDIO_MAX_ROWS", "50000")),
        )
        columns = [f.name for f in it.schema]
        # BigQuery returns date/datetime/Decimal like the other warehouses, plus
        # STRUCT as dict and ARRAY as list — jsonify_rows recurses into both, so
        # nested values survive the tile cache and the API response. Three edges
        # it inherits from to_jsonable: BIGNUMERIC (38-digit Decimal) rounds
        # through float, BYTES decodes as utf-8 with "replace" so true binary is
        # mangled, and INTERVAL stringifies to a relativedelta repr.
        return columns, jsonify_rows([list(r) for r in it])
