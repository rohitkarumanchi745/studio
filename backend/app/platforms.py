"""Pipeline Control Service: one thin adapter per orchestration platform so the
supervisor can trigger, watch, and audit external pipeline runs through a single
contract instead of four bespoke integrations. Adapters speak stdlib
urllib.request (no new HTTP deps), read credentials from env at call time, and
normalize every platform's run states to the canonical set:

    queued | running | succeeded | failed | canceled | unknown

Contract (consumed by supervisor.py; run refs live inside supervised_jobs.result):
    Platform.configured() -> bool                    never raises
    Platform.trigger(payload) -> {"run_ref", "url"}  raises RuntimeError on failure
    Platform.status(run_ref) -> {"state", "detail", "url", "metrics"}
    Platform.logs(run_ref) -> str                    best-effort, "" if unavailable
    Platform.quality(run_ref) -> [{"name","status","detail"}]  best-effort []

Payload shapes (the UI renders these):
    airflow:         {"dag_id": "etl_daily", "conf": {...}}   # conf optional
    databricks_jobs: a Jobs 2.1 runs/submit body, e.g.
                     {"run_name": "...", "tasks": [{"task_key": "t1", ...}]}
    dbt_cloud:       {"job_id": 123, "cause": "why"}          # job_id falls back
                     # to DBT_CLOUD_JOB_ID; cause optional; extra keys (e.g.
                     # steps_override, git_branch) pass through to dbt
    k8s_spark:       a full SparkApplication manifest (apiVersion/kind/spec), or
                     shorthand: {"main_file": "local:///...", "type": "Python"|
                     "Scala", "main_class": ..., "image": ..., "spark_version":
                     ..., "arguments": [...], "name": ..., "service_account":
                     ..., "driver": {...}, "executor": {...}}
"""
import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.request
import uuid
from urllib.parse import quote


def _http(method, url, headers=None, body=None, timeout=30, context=None):
    """One HTTP round-trip; returns the response body as text. On HTTPError the
    RuntimeError carries the response body so a 403 from Airflow says WHY, not
    just '403'."""
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode(errors="replace")[:1000]
        except Exception:
            detail = ""
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} -> {e.reason}")


def _json(method, url, headers=None, body=None, timeout=30, context=None):
    return json.loads(_http(method, url, headers, body, timeout, context) or "{}")


class Platform:
    """Adapter contract; subclasses own auth, payload shape, and state maps."""
    name = ""
    label = ""

    def configured(self):
        """True when env credentials are present. Never raises."""
        return False

    def trigger(self, payload):
        """Start a run -> {"run_ref": str, "url": str|None}. RuntimeError on failure."""
        raise NotImplementedError

    def status(self, run_ref):
        """-> {"state": canonical, "detail": str|None, "url": str|None, "metrics": dict}."""
        raise NotImplementedError

    def logs(self, run_ref):
        """Best-effort run logs; "" when unavailable."""
        return ""

    def quality(self, run_ref):
        """Best-effort data-quality checks [{"name","status","detail"}]; [] when none."""
        return []


# ── Apache Airflow ──────────────────────────────────────────────────────

class AirflowPlatform(Platform):
    """Airflow DAG runs over the stable REST API.

    Env: AIRFLOW_URL (base URL, no /api suffix) plus either AIRFLOW_TOKEN
    (sent as Bearer — pre-issued Airflow 3 JWT or managed deployments like
    Astronomer) or AIRFLOW_USERNAME + AIRFLOW_PASSWORD (Basic on v1;
    exchanged at /auth/token for a short-lived JWT on v2, fetched per call).
    AIRFLOW_API_VERSION: "v1" (default, Airflow 2) | "v2" (Airflow 3).
    """
    name = "airflow"
    label = "Apache Airflow"

    _STATES = {"queued": "queued", "running": "running",
               "success": "succeeded", "failed": "failed"}
    # Airflow has no canceled DAG-run state; a run marked failed by a user
    # surfaces as "failed".

    def _cfg(self):
        return {
            "url": os.getenv("AIRFLOW_URL", "").rstrip("/"),
            "token": os.getenv("AIRFLOW_TOKEN", ""),
            "user": os.getenv("AIRFLOW_USERNAME", ""),
            "password": os.getenv("AIRFLOW_PASSWORD", ""),
            "version": os.getenv("AIRFLOW_API_VERSION", "v1"),
        }

    def configured(self):
        cfg = self._cfg()
        return bool(cfg["url"] and (cfg["token"] or (cfg["user"] and cfg["password"])))

    def _headers(self, cfg):
        if cfg["token"]:
            return {"Authorization": f"Bearer {cfg['token']}"}
        if cfg["version"] == "v2":
            # Airflow 3 JWTs are short-lived (minutes) — fetch per operation.
            tok = _json("POST", f"{cfg['url']}/auth/token", {},
                        {"username": cfg["user"], "password": cfg["password"]})
            return {"Authorization": f"Bearer {tok.get('access_token', '')}"}
        cred = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
        return {"Authorization": f"Basic {cred}"}

    def _api(self, cfg, path):
        return f"{cfg['url']}/api/{cfg['version']}{path}"

    def _grid_url(self, cfg, dag_id, run_id):
        return (f"{cfg['url']}/dags/{quote(dag_id, safe='')}/grid"
                f"?dag_run_id={quote(run_id, safe='')}")

    @staticmethod
    def _split(run_ref):
        dag_id, _, run_id = run_ref.partition(":")
        return dag_id, run_id

    def trigger(self, payload):
        dag_id = (payload or {}).get("dag_id")
        if not dag_id:
            raise RuntimeError('airflow payload needs "dag_id" (optional "conf" dict)')
        conf = (payload or {}).get("conf") or {}
        if not isinstance(conf, dict):
            raise RuntimeError('airflow "conf" must be a JSON object')
        cfg = self._cfg()
        # Supply our own run id: URL-safe characters sidestep the +/: percent-
        # encoding trap of Airflow's auto-generated timestamp ids.
        run_id = f"studio__{uuid.uuid4().hex}"
        body = {"dag_run_id": run_id, "conf": conf}
        if cfg["version"] == "v2":
            body["logical_date"] = None  # required-but-nullable in v2
        d = _json("POST", self._api(cfg, f"/dags/{quote(dag_id, safe='')}/dagRuns"),
                  self._headers(cfg), body)
        run_id = d.get("dag_run_id") or run_id
        return {"run_ref": f"{dag_id}:{run_id}",
                "url": self._grid_url(cfg, dag_id, run_id)}

    def status(self, run_ref):
        cfg = self._cfg()
        try:
            dag_id, run_id = self._split(run_ref)
            d = _json("GET", self._api(
                cfg, f"/dags/{quote(dag_id, safe='')}/dagRuns/{quote(run_id, safe='')}"),
                self._headers(cfg))
        except Exception as e:  # includes 404 on a vanished run
            return {"state": "unknown", "detail": str(e), "url": None, "metrics": {}}
        metrics = {k: d.get(k) for k in ("start_date", "end_date", "run_type")
                   if d.get(k) is not None}
        return {"state": self._STATES.get(d.get("state"), "unknown"),
                "detail": d.get("note"),
                "url": self._grid_url(cfg, dag_id, run_id),
                "metrics": metrics}

    def _task_instances(self, run_ref):
        cfg = self._cfg()
        dag_id, run_id = self._split(run_ref)
        d = _json("GET", self._api(
            cfg, f"/dags/{quote(dag_id, safe='')}/dagRuns/{quote(run_id, safe='')}"
                 f"/taskInstances?limit=100"),
            self._headers(cfg))
        return d.get("task_instances", [])

    def logs(self, run_ref):
        # Per-task summary from ONE taskInstances call: whole-run log endpoints
        # don't exist (logs are per task per try_number), and this stays
        # version-agnostic and always available.
        try:
            return "\n".join(
                f"{ti.get('task_id')}  {ti.get('state')}  "
                f"duration={ti.get('duration')}  tries={ti.get('try_number')}"
                for ti in self._task_instances(run_ref))
        except Exception:
            return ""

    def quality(self, run_ref):
        """Task instances as checks: success->pass, failed/upstream_failed->fail,
        anything else (running/queued/skipped/...) -> pending."""
        try:
            out = []
            for ti in self._task_instances(run_ref):
                st = ti.get("state")
                status = ("pass" if st == "success"
                          else "fail" if st in ("failed", "upstream_failed")
                          else "pending")
                out.append({"name": ti.get("task_id"), "status": status,
                            "detail": f"state={st} duration={ti.get('duration')}"})
            return out
        except Exception:
            return []


# ── Databricks Jobs ─────────────────────────────────────────────────────

class DatabricksJobsPlatform(Platform):
    """Databricks one-time job runs via Jobs API 2.1 runs/submit.

    Env: DATABRICKS_SERVER_HOSTNAME + DATABRICKS_TOKEN — the same pair
    connectors/databricks_conn.submit_spark_job already uses. Hostname may
    include a scheme; https:// is assumed when absent.

    quality() returns [] — the Jobs API exposes no test/data-quality artifact
    (expectations live in DLT pipelines, a different API).
    """
    name = "databricks_jobs"
    label = "Databricks Jobs"

    # If result_state is present map on it; else map on life_cycle_state.
    _RESULT = {"SUCCESS": "succeeded", "SUCCESS_WITH_FAILURES": "succeeded",
               "CANCELED": "canceled", "UPSTREAM_CANCELED": "canceled",
               "FAILED": "failed", "TIMEDOUT": "failed", "UPSTREAM_FAILED": "failed",
               "MAXIMUM_CONCURRENT_RUNS_REACHED": "failed", "EXCLUDED": "failed",
               "DISABLED": "failed"}
    _LIFE = {"QUEUED": "queued", "PENDING": "queued", "BLOCKED": "queued",
             "WAITING_FOR_RETRY": "queued",
             "RUNNING": "running", "TERMINATING": "running",
             "INTERNAL_ERROR": "failed",
             "SKIPPED": "canceled"}  # aborted because a previous run was active

    def _cfg(self):
        host = os.getenv("DATABRICKS_SERVER_HOSTNAME", "")
        base = host if host.startswith(("http://", "https://")) else f"https://{host}"
        return {"base": base.rstrip("/"), "host": host,
                "token": os.getenv("DATABRICKS_TOKEN", "")}

    def configured(self):
        cfg = self._cfg()
        return bool(cfg["host"] and cfg["token"])

    def _headers(self, cfg):
        return {"Authorization": f"Bearer {cfg['token']}"}

    def trigger(self, payload):
        if not isinstance(payload, dict) or not payload.get("tasks"):
            raise RuntimeError('databricks_jobs payload must be a Jobs 2.1 '
                               'runs/submit body with a non-empty "tasks" list')
        cfg = self._cfg()
        d = _json("POST", f"{cfg['base']}/api/2.1/jobs/runs/submit",
                  self._headers(cfg), payload)
        run_id = d.get("run_id")
        if run_id is None:
            raise RuntimeError(f"databricks runs/submit returned no run_id: {d}")
        # run_page_url only appears on runs/get; status() supplies the url.
        return {"run_ref": str(run_id), "url": None}

    def _get_run(self, cfg, run_ref):
        return _json("GET", f"{cfg['base']}/api/2.1/jobs/runs/get?run_id={run_ref}",
                     self._headers(cfg))

    def status(self, run_ref):
        cfg = self._cfg()
        try:
            d = self._get_run(cfg, run_ref)
        except Exception as e:
            return {"state": "unknown", "detail": str(e), "url": None, "metrics": {}}
        st = d.get("state") or {}
        result, life = st.get("result_state"), st.get("life_cycle_state")
        state = (self._RESULT.get(result, "unknown") if result
                 else self._LIFE.get(life, "unknown"))
        # run_duration is only set for multitask runs; else sum the task parts.
        dur = d.get("run_duration") or sum(
            d.get(k) or 0 for k in ("setup_duration", "execution_duration",
                                    "cleanup_duration"))
        metrics = {k: d.get(k) for k in ("start_time", "end_time", "queue_duration")
                   if d.get(k) is not None}
        metrics["duration_ms"] = dur
        metrics["task_count"] = len(d.get("tasks") or [])
        return {"state": state, "detail": st.get("state_message"),
                "url": d.get("run_page_url"), "metrics": metrics}

    def logs(self, run_ref):
        # get-output errors on a parent run — read tasks[].run_id from runs/get
        # and fetch per task, preferring stdout logs, then notebook exit value,
        # always appending error + trace.
        cfg = self._cfg()
        try:
            d = self._get_run(cfg, run_ref)
            tasks = [(t.get("task_key") or "task", t.get("run_id"))
                     for t in (d.get("tasks") or []) if t.get("run_id")]
            if not tasks:  # single-task submit fallback
                tasks = [("run", run_ref)]
            parts = []
            for key, rid in tasks:
                try:
                    o = _json("GET",
                              f"{cfg['base']}/api/2.1/jobs/runs/get-output?run_id={rid}",
                              self._headers(cfg))
                except RuntimeError:
                    continue
                text = o.get("logs") or (o.get("notebook_output") or {}).get("result") or ""
                if o.get("error"):
                    text += ("\n" if text else "") + f"ERROR: {o['error']}"
                if o.get("error_trace"):
                    text += "\n" + o["error_trace"]
                if text:
                    parts.append(f"== {key} ==\n{text}")
            return "\n\n".join(parts)
        except Exception:
            return ""


# ── dbt Cloud ───────────────────────────────────────────────────────────

class DbtCloudPlatform(Platform):
    """dbt Cloud job runs via API v2.

    Env: DBT_CLOUD_ACCOUNT_ID + DBT_CLOUD_API_TOKEN (service token, sent as
    Bearer); DBT_CLOUD_JOB_ID (default job, payload may override);
    DBT_CLOUD_BASE_URL (default https://cloud.getdbt.com — regional/cell-based
    accounts use e.g. https://emea.dbt.com or https://{prefix}.us1.dbt.com).
    Trailing slashes on run paths are deliberate: dbt 301-redirects without
    them and urllib's re-POST across redirects is unreliable.
    """
    name = "dbt_cloud"
    label = "dbt Cloud"

    _STATES = {1: "queued", 2: "running", 3: "running",  # 2 = Starting
               10: "succeeded", 20: "failed", 30: "canceled"}

    def _cfg(self):
        return {
            "account": os.getenv("DBT_CLOUD_ACCOUNT_ID", ""),
            "token": os.getenv("DBT_CLOUD_API_TOKEN", ""),
            "job": os.getenv("DBT_CLOUD_JOB_ID", ""),
            "base": os.getenv("DBT_CLOUD_BASE_URL", "https://cloud.getdbt.com").rstrip("/"),
        }

    def configured(self):
        cfg = self._cfg()
        return bool(cfg["account"] and cfg["token"])

    def _headers(self, cfg):
        return {"Authorization": f"Bearer {cfg['token']}"}

    def trigger(self, payload):
        cfg = self._cfg()
        payload = payload or {}
        job_id = payload.get("job_id") or cfg["job"]
        if not job_id:
            raise RuntimeError('dbt_cloud needs "job_id" in the payload or '
                               'DBT_CLOUD_JOB_ID in the env')
        # job_id goes into the URL path — a numeric id only, so `222/../../evil`
        # can't manipulate the path on the (still same-host) dbt endpoint.
        if not str(job_id).isdigit():
            raise RuntimeError("dbt_cloud job_id must be numeric")
        # "cause" is the only required field; extra keys (steps_override,
        # git_branch, schema_override, ...) pass through as overrides.
        body = {k: v for k, v in payload.items() if k != "job_id"}
        body.setdefault("cause", "Triggered by Studio supervisor")
        d = _json("POST",
                  f"{cfg['base']}/api/v2/accounts/{cfg['account']}/jobs/{job_id}/run/",
                  self._headers(cfg), body)
        run = d.get("data") or {}
        if run.get("id") is None:
            raise RuntimeError(f"dbt Cloud trigger returned no run id: {d}")
        return {"run_ref": str(run["id"]), "url": run.get("href")}

    def _get_run(self, cfg, run_ref, include=None):
        url = f"{cfg['base']}/api/v2/accounts/{cfg['account']}/runs/{run_ref}/"
        if include:
            url += f"?include_related={quote(json.dumps(include), safe='')}"
        return _json("GET", url, self._headers(cfg)).get("data") or {}

    def status(self, run_ref):
        cfg = self._cfg()
        try:
            run = self._get_run(cfg, run_ref)
        except Exception as e:
            return {"state": "unknown", "detail": str(e), "url": None, "metrics": {}}
        metrics = {k: run.get(k) for k in
                   ("duration", "run_duration", "started_at", "finished_at",
                    "artifacts_saved") if run.get(k) is not None}
        return {"state": self._STATES.get(run.get("status"), "unknown"),
                "detail": run.get("status_message") or run.get("status_humanized"),
                "url": run.get("href"), "metrics": metrics}

    def logs(self, run_ref):
        # High-level step logs come inline with ?include_related=["run_steps"];
        # debug_logs are skipped (huge, truncated to 1000 lines anyway).
        cfg = self._cfg()
        try:
            steps = self._get_run(cfg, run_ref, include=["run_steps"]).get("run_steps") or []
            return "\n\n".join(
                f"== {s.get('name')} ==\n{s.get('logs') or ''}"
                for s in sorted(steps, key=lambda s: s.get("index") or 0))
        except Exception:
            return ""

    def quality(self, run_ref):
        """Test results from the run_results.json artifact. Artifacts exist only
        for completed runs (404 before/without them — treated as no data)."""
        cfg = self._cfg()
        try:
            run = self._get_run(cfg, run_ref)
            if not run.get("is_complete"):
                return []
            art = _json("GET",
                        f"{cfg['base']}/api/v2/accounts/{cfg['account']}/runs/"
                        f"{run_ref}/artifacts/run_results.json",
                        self._headers(cfg))
        except Exception:  # 404 = artifact never written (e.g. some failed runs)
            return []
        return [{"name": r.get("unique_id"), "status": r.get("status"),
                 "detail": r.get("message")}
                for r in art.get("results") or []
                if str(r.get("unique_id", "")).startswith("test.")]


# ── Spark on Kubernetes (Kubeflow spark-operator) ───────────────────────

class K8sSparkPlatform(Platform):
    """SparkApplication CRs (sparkoperator.k8s.io/v1beta2) over the K8s API.

    Env: K8S_API_URL (e.g. https://1.2.3.4:6443), K8S_TOKEN (ServiceAccount
    bearer token with create/get on sparkapplications and get on pods +
    pods/log), K8S_NAMESPACE (default "default"), K8S_VERIFY_SSL (default
    verify; "false"/"0" skips — cluster CAs aren't in public bundles).

    quality() returns [] — the operator exposes no test/data-quality results;
    checks belong to whatever the Spark app itself writes.
    """
    name = "k8s_spark"
    label = "Spark on Kubernetes"

    _GROUP = "/apis/sparkoperator.k8s.io/v1beta2"
    _STATES = {"": "queued", "SUBMITTED": "queued", "PENDING_RERUN": "queued",
               # SUCCEEDING/FAILING are terminal-transition states, not final
               "RUNNING": "running", "SUCCEEDING": "running", "FAILING": "running",
               "INVALIDATING": "running", "RESUMING": "running",
               "SUSPENDING": "running",
               "COMPLETED": "succeeded",
               "FAILED": "failed", "SUBMISSION_FAILED": "failed",
               "SUSPENDED": "canceled"}

    def _cfg(self):
        return {
            "base": os.getenv("K8S_API_URL", "").rstrip("/"),
            "token": os.getenv("K8S_TOKEN", ""),
            "namespace": os.getenv("K8S_NAMESPACE", "default"),
            "verify": os.getenv("K8S_VERIFY_SSL", "true").lower() not in ("false", "0", "no"),
        }

    def configured(self):
        cfg = self._cfg()
        return bool(cfg["base"] and cfg["token"])

    def _headers(self, cfg):
        return {"Authorization": f"Bearer {cfg['token']}"}

    def _ctx(self, cfg):
        return None if cfg["verify"] else ssl._create_unverified_context()

    @staticmethod
    def _dns_name(s):
        # Operator validates names as DNS-1035 labels (driver service derives
        # from them): lowercase, [-a-z0-9], must start with a letter; keep well
        # under 63 since -driver/-driver-svc suffixes are appended.
        s = re.sub(r"[^a-z0-9-]+", "-", (s or "").lower()).strip("-")
        if not s or not s[0].isalpha():
            s = f"studio-{s}" if s else f"studio-{uuid.uuid4().hex[:12]}"
        return s[:47].rstrip("-")

    def _expand(self, shorthand, cfg):
        main = shorthand.get("main_file") or shorthand.get("mainApplicationFile")
        if not main:
            raise RuntimeError('k8s_spark shorthand needs "main_file" '
                               '(or pass a full SparkApplication manifest)')
        typ = shorthand.get("type", "Python")
        spec = {
            "type": typ,
            "mode": "cluster",
            "image": shorthand.get("image", "docker.io/apache/spark:4.0.4"),
            "imagePullPolicy": "IfNotPresent",
            "mainApplicationFile": main,
            "sparkVersion": shorthand.get("spark_version", "4.0.4"),
            "driver": shorthand.get("driver") or {
                "cores": 1, "memory": "512m",
                # SA must be allowed to create executor pods (helm chart default)
                "serviceAccount": shorthand.get("service_account", "spark-operator-spark"),
            },
            "executor": shorthand.get("executor") or
                {"instances": 1, "cores": 1, "memory": "512m"},
        }
        if typ == "Python":
            spec["pythonVersion"] = "3"
        if shorthand.get("main_class"):
            spec["mainClass"] = shorthand["main_class"]
        elif typ == "Scala":
            raise RuntimeError('k8s_spark Scala jobs need "main_class"')
        if shorthand.get("arguments"):
            spec["arguments"] = list(shorthand["arguments"])
        return {"apiVersion": "sparkoperator.k8s.io/v1beta2",
                "kind": "SparkApplication",
                "metadata": {"name": shorthand.get("name") or ""},
                "spec": spec}

    @staticmethod
    def _split(run_ref, cfg):
        if "/" in run_ref:
            return run_ref.split("/", 1)
        return cfg["namespace"], run_ref

    def trigger(self, payload):
        if not isinstance(payload, dict) or not payload:
            raise RuntimeError("k8s_spark payload must be a SparkApplication "
                               "manifest or a shorthand dict")
        cfg = self._cfg()
        if payload.get("kind") == "SparkApplication" or "spec" in payload:
            manifest = json.loads(json.dumps(payload))  # don't mutate caller's dict
        else:
            manifest = self._expand(payload, cfg)
        meta = manifest.setdefault("metadata", {})
        meta["name"] = self._dns_name(meta.get("name"))
        # Pin the run to the configured namespace — a manifest's own
        # metadata.namespace must NOT silently retarget another namespace
        # (the K8S_TOKEN's cross-namespace RBAC would decide the blast radius).
        ns = meta["namespace"] = cfg["namespace"]
        _http("POST", f"{cfg['base']}{self._GROUP}/namespaces/{ns}/sparkapplications",
              self._headers(cfg), manifest, context=self._ctx(cfg))
        # No externally reachable UI by default (driver web UI is ClusterIP);
        # status() surfaces the ingress address when one exists.
        return {"run_ref": f"{ns}/{meta['name']}", "url": None}

    def _get_app(self, cfg, run_ref):
        ns, name = self._split(run_ref, cfg)
        return ns, name, _json(
            "GET", f"{cfg['base']}{self._GROUP}/namespaces/{ns}/sparkapplications/{name}",
            self._headers(cfg), context=self._ctx(cfg))

    def status(self, run_ref):
        cfg = self._cfg()
        try:
            _, _, d = self._get_app(cfg, run_ref)
        except Exception as e:
            return {"state": "unknown", "detail": str(e), "url": None, "metrics": {}}
        st = d.get("status") or {}
        app_state = st.get("applicationState") or {}
        raw = app_state.get("state") or ""
        metrics = {k: st.get(k) for k in
                   ("sparkApplicationId", "executionAttempts",
                    "submissionAttempts", "terminationTime") if st.get(k) is not None}
        return {"state": self._STATES.get(raw, "unknown"),
                "detail": app_state.get("errorMessage"),
                "url": (st.get("driverInfo") or {}).get("webUIIngressAddress") or None,
                "metrics": metrics}

    def logs(self, run_ref):
        cfg = self._cfg()
        ns, name = self._split(run_ref, cfg)
        try:
            _, _, d = self._get_app(cfg, run_ref)
            pod = ((d.get("status") or {}).get("driverInfo") or {}).get("podName")
        except Exception:
            pod = None
        pod = pod or f"{name}-driver"  # operator's default driver pod name
        try:
            return _http("GET",
                         f"{cfg['base']}/api/v1/namespaces/{ns}/pods/{pod}/log"
                         f"?tailLines=1000",
                         self._headers(cfg), context=self._ctx(cfg))
        except Exception:  # 400 while Pending, 404 after pod GC
            return ""


PLATFORMS = {
    "airflow": AirflowPlatform(),
    "databricks_jobs": DatabricksJobsPlatform(),
    "dbt_cloud": DbtCloudPlatform(),
    "k8s_spark": K8sSparkPlatform(),
}


def get_platform(name):
    """Adapter for `name`; raises KeyError for unknown platforms."""
    return PLATFORMS[name]


def all_platforms():
    """[{"name","label","configured"}] for the UI's platform picker."""
    return [{"name": p.name, "label": p.label, "configured": p.configured()}
            for p in PLATFORMS.values()]
