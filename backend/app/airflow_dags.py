"""Compile declarative SQL graphs and publish approved, immutable Airflow DAGs.

The Airflow environment needs ``apache-airflow-providers-common-sql`` plus the
provider for each configured database. ``STUDIO_AIRFLOW_CONNECTIONS_JSON`` maps
Studio sources to *existing Airflow connection IDs*, never credentials. A DAG
uses the same source/namespace as the Studio connector; administrators must
configure that correspondence. Airflow 2.4+ and 3.x imports are supported.

``STUDIO_AIRFLOW_DAGS_DIR`` must be an absolute, existing, symlink-free directory
shared with the Airflow DAG processor. Publication does not register, unpause,
or trigger a DAG: the supervisor handles those separately after human approval.
Studio never imports or executes generated Python. Schedules are intentionally
disabled, and SQL/parameters are not evaluated as Airflow/Jinja templates.

SQL authorization belongs to pipeline_dags.validate; validate_plan below only
checks the declarative structure. Both must pass before deployment.
"""
import errno
import hashlib
import hmac
import json
import math
import os
import re
import stat
import uuid
from pathlib import Path


_COMPILER_VERSION = 1
_TASK_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_DAG_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z")
_SOURCE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}\Z")
_CONNECTION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,249}\Z")
_TABLE_PART = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^"\x00]|"")+"|`[^`\x00]+`|\[[^\]\x00]+\])'
_TABLE = re.compile(rf"{_TABLE_PART}(?:\s*\.\s*{_TABLE_PART}){{0,2}}\Z")
_MAX_TASKS = 64
_MAX_SQL = 64 * 1024
_MAX_ARTIFACT = 2 * 1024 * 1024


class AirflowPlanError(ValueError):
    """The proposed graph cannot be safely represented by this compiler."""


class AirflowConfigurationError(RuntimeError):
    """A required server-side Airflow deployment setting is missing/invalid."""


class AirflowDeploymentError(RuntimeError):
    """The immutable DAG publication could not be completed safely."""


def _text(value, field, maximum=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AirflowPlanError(f"{field} must be a nonempty string (max {maximum} characters)")
    if "\x00" in value or any(marker in value for marker in ("{{", "{%", "{#")):
        raise AirflowPlanError(f"{field} may not contain NUL or Airflow/Jinja templates")
    return value


def _identifier(value, field, pattern):
    _text(value, field, 250)
    if not pattern.fullmatch(value):
        raise AirflowPlanError(f"Invalid {field}")
    return value


def validate_plan(plan):
    """Return a canonical structural plan; does not authorize or execute SQL.

    Metadata from the planner (read_sql, kind, status, prompt, etc.) is ignored,
    not compiled as executable Python. Parameters are audit metadata; their
    values must already be compiled into validated SQL constants by the planner.
    Runtime ``dag_run.conf`` cannot change the reviewed graph or those values.
    """
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != 1:
        raise AirflowPlanError("DAG plan version must be 1")
    if plan.get("schedule") is not None:
        raise AirflowPlanError("Generated DAGs are manual-only; schedule must be null")
    if plan.get("status") not in (None, "ready") or plan.get("errors") or plan.get("missing"):
        raise AirflowPlanError("DAG plan is not ready for compilation")
    source = _identifier(plan.get("source"), "source", _SOURCE)
    result = {"version": 1,
              "name": _text(plan.get("name"), "name", 500),
              "dag_id": _identifier(plan.get("dag_id"), "dag_id", _DAG_ID),
              "source": source, "schedule": None, "tasks": [], "parameters": {}}
    parameters = plan.get("parameters", {})
    if not isinstance(parameters, dict) or len(parameters) > 100:
        raise AirflowPlanError("parameters must be an object of at most 100 scalar values")
    for key, value in sorted(parameters.items(), key=lambda item: str(item[0])):
        _identifier(key, "parameter name", _TASK_ID)
        if value is not None and type(value) not in (str, bool, int, float):
            raise AirflowPlanError("parameters must contain only JSON scalar values")
        if isinstance(value, float) and not math.isfinite(value):
            raise AirflowPlanError("parameters may not contain NaN or infinity")
        if isinstance(value, str):
            if len(value) > 4096 or "\x00" in value or any(
                    marker in value for marker in ("{{", "{%", "{#")):
                raise AirflowPlanError("Invalid parameter value or Airflow/Jinja template")
        result["parameters"][key] = value
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= _MAX_TASKS:
        raise AirflowPlanError(f"tasks must contain 1 to {_MAX_TASKS} SQL tasks")
    seen = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise AirflowPlanError("Every task must be an object")
        tid = _identifier(task.get("id"), "task id", _TASK_ID)
        if tid in seen:
            raise AirflowPlanError(f"Duplicate task id: {tid}")
        seen.add(tid)
        if task.get("source", source) != source:
            raise AirflowPlanError("All DAG tasks must use the reviewed source")
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or len(deps) > _MAX_TASKS:
            raise AirflowPlanError(f"Task {tid} depends_on must be a list of task IDs")
        for dep in deps:
            _identifier(dep, "dependency id", _TASK_ID)
        if len(set(deps)) != len(deps) or tid in deps:
            raise AirflowPlanError(f"Task {tid} has duplicate or self dependencies")
        normalized = {"id": tid, "name": _text(task.get("name", tid), "task name", 500),
                      "source": source, "sql": _text(task.get("sql"), "SQL", _MAX_SQL),
                      "depends_on": sorted(deps)}
        if task.get("produces") is not None:
            normalized["produces"] = _identifier(task["produces"], "produced table", _TABLE)
        result["tasks"].append(normalized)
    graph = {task["id"]: set(task["depends_on"]) for task in result["tasks"]}
    for tid, deps in graph.items():
        unknown = deps - seen
        if unknown:
            raise AirflowPlanError(f"Task {tid} depends on unknown task {sorted(unknown)[0]}")
    completed = set()
    while len(completed) < len(graph):
        ready = {tid for tid, deps in graph.items() if tid not in completed and deps <= completed}
        if not ready:
            raise AirflowPlanError("DAG task dependencies contain a cycle")
        completed.update(ready)
    result["tasks"].sort(key=lambda task: task["id"])
    try:
        size = len(json.dumps(result, allow_nan=False).encode())
    except (ValueError, OverflowError) as exc:
        raise AirflowPlanError("DAG parameters are not bounded JSON values") from exc
    if size > _MAX_ARTIFACT // 2:
        raise AirflowPlanError("DAG plan exceeds the size limit")
    return result


def _connection(source, original):
    raw = os.getenv("STUDIO_AIRFLOW_CONNECTIONS_JSON", "")
    try:
        mapping = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as exc:
        raise AirflowConfigurationError("STUDIO_AIRFLOW_CONNECTIONS_JSON must be a JSON object") from exc
    if not isinstance(mapping, dict):
        raise AirflowConfigurationError("STUDIO_AIRFLOW_CONNECTIONS_JSON must be a JSON object")
    conn_id = mapping.get(source)
    if not isinstance(conn_id, str) or not _CONNECTION.fullmatch(conn_id):
        raise AirflowConfigurationError(f"No valid Airflow connection ID configured for source {source}")
    requested = [original.get("connection_id")]
    requested.extend(task.get("conn_id") for task in original["tasks"])
    if any(value is not None and value != conn_id for value in requested):
        raise AirflowPlanError("Airflow connection mapping changed; validate and approve the plan again")
    return conn_id


def artifact(plan):
    """Return source, immutable DAG ID, filename, and approval fingerprint.

    ``digest`` hashes the canonical graph, resolved connection, and compiler
    version (not the self-referencing source bytes). ``source_sha256`` separately
    hashes the actual Python download. A connection change changes the DAG ID.
    """
    normalized = validate_plan(plan)
    conn_id = _connection(normalized["source"], plan)
    canonical = json.dumps({"compiler_version": _COMPILER_VERSION, "plan": normalized,
                            "connection_id": conn_id}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    dag_id = f"studio_{normalized['dag_id']}__{digest}"
    lines = [
        "# Generated by Studio's declarative SQL DAG compiler; do not edit.",
        f"# Studio approval fingerprint: {digest}",
        "from datetime import datetime, timezone",
        "try:",
        "    from airflow.sdk import DAG  # Airflow 3",
        "except ImportError:",
        "    from airflow import DAG  # Airflow 2.4+",
        "from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator",
        "", "# Audit metadata only; parameter values are already compiled into reviewed SQL.",
        f"STUDIO_PARAMETERS = {normalized['parameters']!r}",
        "", "with DAG(",
        f"    dag_id={dag_id!r},",
        f"    description={normalized['name']!r},",
        "    start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),",
        "    schedule=None,",
        "    catchup=False,",
        "    is_paused_upon_creation=False,",
        "    max_active_runs=1,",
        "    default_args={'retries': 0},",
        "    tags=['studio', 'approved-sql-pipeline'],",
        ") as dag:",
        "    tasks = {}",
    ]
    for task in normalized["tasks"]:
        ref = f"tasks[{task['id']!r}]"
        lines.extend([
            f"    {ref} = SQLExecuteQueryOperator(",
            f"        task_id={task['id']!r},",
            f"        conn_id={conn_id!r},",
            f"        sql={task['sql']!r},",
            "        parameters=None,",
            "        autocommit=False,",
            "        split_statements=False,",
            "        do_xcom_push=False,",
            "        show_return_value_in_logs=False,",
            "        trigger_rule='all_success',",
            "    )",
            "    # Reviewed SQL and values are constants, never Jinja or file templates.",
            f"    {ref}.template_fields = ()",
            f"    {ref}.template_ext = ()",
        ])
    for task in normalized["tasks"]:
        for dep in task["depends_on"]:
            lines.append(f"    tasks[{dep!r}] >> tasks[{task['id']!r}]")
    source = "\n".join(lines) + "\n"
    if len(source.encode()) > _MAX_ARTIFACT:
        raise AirflowPlanError("Compiled DAG exceeds the size limit")
    return {"dag_id": dag_id, "digest": digest, "source": source, "parameters_mode": "compiled_sql",
            "filename": f"{dag_id}.py", "source_sha256": hashlib.sha256(source.encode()).hexdigest()}


def compile_dag(plan):
    """Compile to Python text without importing Airflow or executing the result."""
    return artifact(plan)["source"]


def _open_directory():
    raw = os.getenv("STUDIO_AIRFLOW_DAGS_DIR", "")
    if not raw:
        raise AirflowConfigurationError(
            "Airflow DAG deployment is not configured: set STUDIO_AIRFLOW_DAGS_DIR to a shared DAG directory")
    directory = Path(raw)
    if not directory.is_absolute() or directory == Path("/") or any(
            part in (".", "..") for part in raw.split("/")):
        raise AirflowConfigurationError("STUDIO_AIRFLOW_DAGS_DIR must be an absolute, non-root directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        # Pin every component by descriptor, refusing symlink traversal even
        # when another process swaps a directory during publication.
        for component in directory.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return directory, fd
    except OSError as exc:
        os.close(fd)
        raise AirflowConfigurationError(
            "STUDIO_AIRFLOW_DAGS_DIR must exist and contain no symlink components") from exc


def _same_file(directory_fd, filename, expected):
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except OSError as exc:
        raise AirflowDeploymentError("Existing DAG path is not a readable regular file") from exc
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != len(expected):
            return False
        return stream.read(len(expected) + 1) == expected


def deploy(plan, *, approver, expected_digest=None):
    """Publish a policy-validated graph after an administrator's explicit approval.

    Supervisor callers must *also* validate using the original requester's
    current permissions, and compare the fingerprint reviewed by the approver.
    Existing files are never overwritten. A fully written, fsynced temporary
    inode is linked to its final name atomically (no rename-overwrite race).
    No request can choose a destination directory or arbitrary filename.
    """
    if (not isinstance(approver, dict) or approver.get("role") != "admin"
            or not (approver.get("id") or approver.get("email"))
            or approver.get("verified") in (False, 0)):
        raise AirflowDeploymentError("An authenticated administrator must approve DAG deployment")
    from . import pipeline_dags
    validated = pipeline_dags.validate(approver, plan)
    compiled = artifact(validated)
    if expected_digest is not None and (not isinstance(expected_digest, str)
            or not hmac.compare_digest(expected_digest, compiled["digest"])):
        raise AirflowDeploymentError("DAG changed since review; approve the new fingerprint before deploying")
    directory, directory_fd = _open_directory()
    temporary = f".studio-{uuid.uuid4().hex}.tmp"
    temporary_created = False
    source = compiled["source"].encode()
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o640, dir_fd=directory_fd)
        temporary_created = True
        with os.fdopen(fd, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, compiled["filename"], src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd, follow_symlinks=False)
        except FileExistsError:
            if not _same_file(directory_fd, compiled["filename"], source):
                raise AirflowDeploymentError("Refusing to overwrite a different existing DAG file")
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    except OSError as exc:
        raise AirflowDeploymentError("Could not atomically publish the approved DAG in the shared directory") from exc
    finally:
        if temporary_created:
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)
    return {"dag_id": compiled["dag_id"], "digest": compiled["digest"],
            "source_sha256": compiled["source_sha256"],
            "path": str(directory / compiled["filename"]), "deployed": True}
