"""Explicit chat commands for supervised orchestration-platform runs.

This module only extracts identifiers and JSON supplied by the user. It does
not generate DAGs, notebook paths, container images, or execution approvals.
"""
import copy
import json
import re
import uuid

from fastapi import HTTPException

from . import platforms, supervisor


_TARGETS = {
    "airflow": "airflow", "apache airflow": "airflow",
    "databricks": "databricks_jobs", "databricks jobs": "databricks_jobs",
    "databricks_jobs": "databricks_jobs", "dbt": "dbt_cloud",
    "dbt cloud": "dbt_cloud", "dbt_cloud": "dbt_cloud",
    "kubernetes": "k8s_spark", "k8s": "k8s_spark",
    "kubernetes spark": "k8s_spark", "k8s spark": "k8s_spark",
    "spark on kubernetes": "k8s_spark", "spark on k8s": "k8s_spark",
    "k8s_spark": "k8s_spark",
}
_PLATFORM = re.compile(r"\b(" + "|".join(
    re.escape(k) for k in sorted(_TARGETS, key=len, reverse=True)) + r")\b", re.I)
_IDENTIFIER = r'''(?:"([^"\n]+)"|'([^'\n]+)'|`([^`\n]+)`|([\w.-]+))'''
_NEGATION = re.compile(r"\b(?:don't|don’t|do not|never|without|not yet|instead of|except|unless)\b", re.I)
_COMMAND = re.compile(r"^(?:trigger|run|rerun|execute|start|submit|deploy)\b\s*", re.I)
_STATUS = re.compile(
    r"^(?:(?:check|show|refresh|poll)(?: me)?(?: the)?(?: (?:pipeline|job|run))? status\b|"
    r"status\b|(?:what(?:'s| is)|how is)(?: the)?(?: pipeline|job|run|it|that|this)\b|"
    r"is (?:it|the (?:pipeline|job|run)) (?:done|finished|running|successful)\b)", re.I)


def _target(value):
    return _TARGETS.get(str(value or "").strip().lower())


def _prior(previous):
    return previous if isinstance(previous, dict) and previous.get("execution_mode") == "platform_run" else None


def _object(value):
    """Copy only JSON objects so callers cannot mutate saved chat history."""
    if not isinstance(value, dict):
        return {}, ["payload must be a JSON object"]
    try:
        return json.loads(json.dumps(value, allow_nan=False)), []
    except (TypeError, ValueError):
        return {}, ["payload must contain valid JSON values"]


def _extract_json(text):
    start = text.find("{")
    if start < 0:
        return text, None, []
    before = re.sub(r"```(?:json)?\s*$", "", text[:start], flags=re.I).strip()
    try:
        value, length = json.JSONDecoder().raw_decode(text[start:])
        after = text[start + length:].strip().removesuffix("```").strip().strip(".!?")
        if after:
            return before, None, ["put all run parameters in one JSON object; remove trailing instructions"]
        value, errors = _object(value)
        return before, value, errors
    except (ValueError, TypeError):
        return before, None, ["provide a valid JSON object for the run parameters"]


def _positive_id(value):
    return (not isinstance(value, bool) and bool(re.fullmatch(r"[0-9]{1,20}", str(value or "")))
            and int(value) > 0)


def _missing(target, payload):
    if target not in platforms.PLATFORMS:
        return ["choose Airflow, Databricks Jobs, dbt Cloud, or Spark on Kubernetes"]
    errors = []
    if target == "airflow":
        dag = payload.get("dag_id")
        if not isinstance(dag, str) or not re.fullmatch(r"[\w.-]{1,250}", dag, flags=re.ASCII):
            errors.append("dag_id: provide the exact Airflow DAG identifier")
        if "conf" in payload and not isinstance(payload["conf"], dict):
            errors.append("conf must be a JSON object")
    elif target in ("databricks_jobs", "dbt_cloud"):
        if target == "databricks_jobs" and "tasks" in payload:
            tasks = payload["tasks"]
            if "job_id" in payload:
                errors.append("choose an existing job_id or a tasks payload, not both")
            if (not isinstance(tasks, list) or not tasks or any(
                    not isinstance(t, dict) or not isinstance(t.get("task_key"), str)
                    or not t["task_key"].strip()
                    or not any(k.endswith("_task") and isinstance(v, dict) and v for k, v in t.items())
                    for t in tasks)):
                errors.append("tasks must contain task_key and an explicit task definition")
        elif not _positive_id(payload.get("job_id")):
            errors.append("job_id: provide the existing numeric job identifier")
        if target == "databricks_jobs" and "job_parameters" in payload and not isinstance(payload["job_parameters"], dict):
            errors.append("job_parameters must be a JSON object")
        if target == "dbt_cloud":
            if "cause" in payload and not isinstance(payload["cause"], str):
                errors.append("cause must be a string")
            if "steps_override" in payload and (not isinstance(payload["steps_override"], list)
                    or not all(isinstance(s, str) for s in payload["steps_override"])):
                errors.append("steps_override must be a list of strings")
    else:
        full = "spec" in payload or "kind" in payload
        spec = payload.get("spec") if full else payload
        if full and (payload.get("kind") != "SparkApplication"
                     or payload.get("apiVersion") != "sparkoperator.k8s.io/v1beta2"):
            errors.append("provide a SparkApplication manifest with apiVersion sparkoperator.k8s.io/v1beta2")
        if not isinstance(spec, dict):
            return errors + ["spec must be a JSON object"]
        main = spec.get("mainApplicationFile") if full else spec.get("main_file") or spec.get("mainApplicationFile")
        if not isinstance(main, str) or not main.strip():
            errors.append("mainApplicationFile" if full else "main_file: provide the deployed Spark application path")
        if not isinstance(spec.get("image"), str) or not spec["image"].strip():
            errors.append("image: provide the Spark container image")
        if spec.get("type") == "Scala" and not spec.get("mainClass" if full else "main_class"):
            errors.append("mainClass" if full else "main_class: provide the Scala entry point")
        if "arguments" in spec and (not isinstance(spec["arguments"], list)
                                    or not all(isinstance(a, str) for a in spec["arguments"])):
            errors.append("arguments must be a list of strings")
        for field in ("driver", "executor"):
            if field in spec and not isinstance(spec[field], dict):
                errors.append(f"{field} must be a JSON object")
        if full and "metadata" in payload and not isinstance(payload["metadata"], dict):
            errors.append("metadata must be a JSON object")
    return errors


def _same_resource(request, previous, references_previous=False):
    if not previous or previous.get("target") != request.get("target"):
        return False
    old, new = previous.get("payload") or {}, request.get("payload") or {}
    target = request.get("target")
    key = "dag_id" if target == "airflow" else "job_id"
    if target in ("airflow", "dbt_cloud") or (target == "databricks_jobs" and (key in old or key in new)):
        return bool(old.get(key)) and str(old[key]) == str(new.get(key))
    if target == "k8s_spark":
        def main(payload):
            spec = payload.get("spec") if isinstance(payload.get("spec"), dict) else payload
            return spec.get("main_file") or spec.get("mainApplicationFile")
        return references_previous or bool(main(old) and main(old) == main(new))
    return references_previous or bool(old and old == new)


def _repair_id(job, job_id):
    result = job.get("result") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = {}
    if job.get("status") == "failed" and isinstance(result, dict) and result.get("run_ref"):
        return f"{job_id}:{result['run_ref']}"
    return None


def _context(request, prompt, previous, *, references_previous=False):
    request["prompt"] = str(prompt or "")
    if (request.get("action") == "submit" and _same_resource(request, previous, references_previous)
            and previous.get("job_id")):
        request["previous_artifact"] = previous
        repair_id = _repair_id({**(previous.get("job") or {}), "status": previous.get("status")}, previous["job_id"])
        if repair_id:
            request["repairs_run_id"] = repair_id
    return request


def intent(prompt, previous=None, *, target=None, payload=None, action=None):
    """Return an explicit submit/status request, or None for ordinary chat.

An explicit UI action uses the same validation as a natural-language command.
Only references such as 'rerun it' inherit the previous run's parameters. A
new named DAG/job starts with an empty payload, even on the same platform.
"""
    previous = _prior(previous)
    if target is not None or payload is not None or action is not None:
        selected = _target(target) if target is not None else (previous or {}).get("target")
        selected_action = action or "submit"
        if selected_action not in ("submit", "status"):
            raise HTTPException(400, "Platform action must be submit or status")
        body, errors = _object(payload if payload is not None else (
            (previous or {}).get("payload", {}) if target is None else {}))
        request = {"action": selected_action, "target": selected, "payload": body,
                   "missing": errors + (_missing(selected, body) if selected_action == "submit" else [])}
        if selected_action == "status":
            request["artifact"] = previous if previous and previous.get("target") == selected else None
        return _context(request, prompt, previous, references_previous=target is None)

    text = str(prompt or "").strip()
    text = re.sub(r"^(?:(?:can|could|would) you\s+)?(?:please\s+)?", "", text, flags=re.I)
    text, supplied, errors = _extract_json(text)
    if _NEGATION.search(text):
        return None
    named = _PLATFORM.search(text)
    selected = _target(named.group()) if named else (previous or {}).get("target")
    if _STATUS.match(re.sub(r"\s+", " ", _PLATFORM.sub("", text))):
        if not previous and not named:
            return None
        return _context({"action": "status", "target": selected, "payload": {}, "missing": [],
                         "artifact": previous if previous and previous.get("target") == selected else None}, prompt, previous)
    command = _COMMAND.match(text)
    # Answering a missing-input card continues that explicit request. A plain
    # new question, quoted instruction, or descriptive prose never does.
    filling = bool(previous and previous.get("status") == "needs_input" and (
        re.match(r"^(?:dag|job)(?:_id)?\b", text, re.I) or (supplied is not None and not text)))
    if not command and not filling:
        return None
    tail = text[command.end():].strip() if command else text
    if not selected:
        # Recognize external-platform commands even when no platform was
        # supplied; leave generic local SQL pipeline commands to chat_pipelines.
        if not re.search(r"\b(?:dag|databricks|airflow|dbt|kubernetes|k8s)\b", tail, re.I):
            return None
    reference = bool(re.match(r"^(?:(?:this|that|the) (?:pipeline|job|run|dag)|it|this|that)\b", tail, re.I))
    same = previous and selected == previous.get("target")
    body = copy.deepcopy(previous.get("payload") or {}) if same and (reference or filling) else {}
    if named:
        tail = _PLATFORM.sub("", tail, count=1).strip()
    # Parameters supplied as 'with conf {...}' are Airflow conf, whereas an
    # unqualified JSON body is the complete platform API payload.
    conf = bool(re.search(r"\b(?:with\s+)?conf\s*:?\s*$", tail, re.I))
    tail = re.sub(r"\s*(?:(?:with\s+)?(?:conf|payload|parameters|json)|with)\s*:?\s*$", "", tail, flags=re.I).strip()
    if supplied is not None:
        if conf:
            if selected == "airflow":
                body["conf"] = supplied
            else:
                errors.append("conf is an Airflow parameter; provide a platform JSON payload")
        else:
            body.update(supplied)
    noun = "dag" if selected == "airflow" else "job"
    identifier = re.search(r"\b" + noun + r"(?:_id|\s+id)?(?:\s*(?:=|:)\s*|\s+)" + _IDENTIFIER, tail, re.I)
    if identifier:
        value = next(v for v in identifier.groups() if v is not None)
        if value.lower() not in ("with", "on", "for", "now", "again", "please"):
            key = "dag_id" if selected == "airflow" else "job_id"
            if key in body and str(body[key]) != value and supplied and key in supplied:
                errors.append(f"conflicting {key} in the command and JSON payload")
            body[key] = int(value) if key == "job_id" and _positive_id(value) else value
            tail = tail[:identifier.start()] + tail[identifier.end():]
    # No inferred deployment/schedule/filter semantics. Any unresolved prose
    # is surfaced for clarification instead of being silently dropped.
    remainder = re.sub(
        r"\b(?:this|that|the|a|an|my|it|pipeline|job|run|dag|on|now|again|please|spark|application)\b",
        "", tail, flags=re.I).strip(" \t\r\n.!?:")
    if remainder:
        errors.append("express additional run parameters as an explicit JSON payload")
    return _context({"action": "submit", "target": selected, "payload": body,
                     "missing": list(dict.fromkeys(errors + _missing(selected, body)))}, prompt, previous,
                    references_previous=reference)


def _artifact(target, payload, user, *, status="needs_input", missing=None):
    platform = platforms.PLATFORMS.get(target)
    return {"execution_mode": "platform_run", "target": target,
            "label": platform.label if platform else "Pipeline platform",
            "payload": copy.deepcopy(payload), "requested_by": user["id"],
            "status": status, "missing": list(missing or [])}


def _summary(job):
    """Job metadata only: no requester email, raw script, or full result/logs."""
    out = {k: job[k] for k in ("id", "kind", "target", "status", "risk", "attempts",
                               "supervisor_decision", "supervisor_reasons", "last_error",
                               "human_by", "created_at", "updated_at") if k in job}
    for field in ("supervisor_reasons",):
        if isinstance(out.get(field), str):
            try:
                out[field] = json.loads(out[field])
            except ValueError:
                pass
    result = job.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if isinstance(result, dict):
        out["result"] = {k: result[k] for k in ("run_ref", "state") if k in result}
    return out


def submit(request, user, *, request_id):
    """Queue a supervised request once; an admin must still approve its run."""
    if request.get("action") == "status":
        return refresh(request.get("artifact") or _artifact(request.get("target"), {}, user), user)
    if request.get("action") != "submit":
        raise HTTPException(400, "Platform action must be submit or status")
    if user.get("role") not in ("admin", "analyst"):
        raise HTTPException(403, "Only admins and analysts can submit platform pipelines")
    target = _target(request.get("target"))
    payload, errors = _object(request.get("payload", {}))
    missing = list(dict.fromkeys(list(request.get("missing") or []) + errors + _missing(target, payload)))
    artifact = _artifact(target, payload, user, missing=missing)
    if missing:
        return artifact
    if not platforms.PLATFORMS[target].configured():
        artifact.update(status="needs_configuration", missing=[f"configure {artifact['label']} credentials in Studio"])
        return artifact
    if not request_id:
        raise HTTPException(400, "A request_id is required for a platform submission")
    learning_context = {k: request[k] for k in ("prompt", "conversation_id") if request.get(k) is not None}
    prior = _prior(request.get("previous_artifact"))
    if prior and prior.get("job_id"):
        # The background monitor may have finished this run since its chat
        # card was saved. Read the persisted, ACL-checked job before linking
        # a repair; never poll the external platform as part of a submission.
        old_job = supervisor.get_job(prior["job_id"], user=user)
        if old_job.get("kind") != supervisor.PLATFORM_KIND or old_job.get("target") != target:
            raise HTTPException(404, "Job not found")
        repair_id = _repair_id(old_job, prior["job_id"])
        if repair_id:
            learning_context["repairs_run_id"] = repair_id
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"studio:chat-platform:{user['id']}:{request_id}"))
    job = supervisor.submit(supervisor.PLATFORM_KIND, target,
                            json.dumps(payload, sort_keys=True, separators=(",", ":")), user,
                            job_id=job_id, notify=False,
                            learning_context=learning_context)
    artifact.update(status=job["status"], job_id=job["id"], job=_summary(job))
    return artifact


def refresh(artifact, user):
    """Refresh an owned run; looking at a status never approves or reruns it."""
    artifact = _prior(artifact)
    if not artifact:
        raise HTTPException(400, "Select a platform pipeline first")
    owner = artifact.get("requested_by")
    if owner and owner != user["id"] and user.get("role") != "admin":
        raise HTTPException(404, "Job not found")
    job_id = artifact.get("job_id")
    if not job_id:
        out = copy.deepcopy(artifact)
        out.update(status="needs_input", missing=["submit a platform run before checking its status"])
        return out
    job = supervisor.get_job(job_id, user=user)
    if job.get("kind") != supervisor.PLATFORM_KIND or job.get("target") != artifact.get("target"):
        raise HTTPException(404, "Job not found")
    live = supervisor.live_job(job_id, user=user)
    job = live.get("job") or job
    out = copy.deepcopy(artifact)
    out.update(status=job["status"], job=_summary(job), missing=[],
               state=live.get("state"), detail=live.get("detail"), url=live.get("url"),
               metrics=live.get("metrics") or {}, quality=live.get("quality") or [])
    return out
