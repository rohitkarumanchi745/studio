"""Natural requests -> reusable declarative DAGs -> supervised publication.

Plans contain data, never model-authored Python. Similar historical successes
are examples for adaptation; only an exact intent may reuse its recipe as-is.
"""
import copy
import json
import os
import re
import uuid

from fastapi import HTTPException

from . import jobs, pipeline_memory, platforms, supervisor


def is_plan(value):
    return isinstance(value, dict) and value.get("execution_mode") == "airflow_dag"


def _clarification(prompt, previous):
    """Recognize explicit answers only while a DAG is asking for details."""
    if not is_plan(previous) or previous.get("status") != "needs_input":
        return False
    text = re.sub(r"\s+", " ", str(prompt or "").strip().lower()).rstrip(".!? ")
    text = re.sub(r"^(?:(?:can|could|would) you )?(?:please )?", "", text)
    return bool(re.search(
        r"^use\s+.+\b(?:as|for)\s+(?:the\s+)?(?:output|destination|table|keys?|source|database|deduplication key)\b|"
        r"^use\s+(?:full[- ]row\s+)?distinct\b|"
        r"^(?:keep|retain)\s+(?:the\s+)?(?:latest|earliest|first|last|highest|lowest|newest|oldest)\b|"
        r"^(?:dedup|deduplicate)\b.*\b(?:by|on|using)\s+\S+|"
        r"^append\s+(?:(?:rows?|data)\s+)?(?:to|into)\s+\S+|"
        r"^create\s+(?:(?:an?|the|new)\s+)*(?:output(?:\s+table)?|destination(?:\s+table)?|table)\s+\S+|"
        r"^create\s+\S+\s+as\s+(?:the\s+)?(?:output|destination)\b", text))


def intent(prompt, previous=None):
    text = re.sub(r"\s+", " ", str(prompt or "").strip().lower()).rstrip(".!? ")
    text = re.sub(r"^(?:(?:can|could|would) you )?(?:please )?", "", text)
    if re.search(r"\b(?:don't|do not|never)\s+(?:build|create|generate|draft|make|deploy|run|execute|trigger|start|submit)\b|\b(?:not yet|without running)\b", text):
        return None
    if is_plan(previous):
        if re.fullmatch(r"(?:check|show|refresh)(?: the)?(?: pipeline|job|run)? status|status", text):
            return "status"
        if re.fullmatch(r"(?:run|rerun|execute|deploy|submit)(?: the| this| that| my)?(?: pipeline| dag| it)(?: now| again)?", text):
            return "submit"
        if re.match(r"^(?:fix|repair|revise|change|update|adapt|make|add|remove|include|exclude|filter)\b", text):
            return "build"
        if _clarification(prompt, previous):
            return "build"
    pipeline = bool(re.search(r"\b(?:pipeline|dag|workflow|etl)\b", text))
    if pipeline and re.search(r"\bairflow\b", text) and re.match(r"^(?:build|create|generate|draft|make|deploy|run)\b", text):
        return "build_submit" if re.match(r"^(?:deploy|run)\b|^(?:build|create) and run\b", text) else "build"
    writes = bool(re.search(r"\b(?:load|ingest|deduplicat\w*|write|insert|publish|upsert|clean)\b|remove duplicates|update .*table", text))
    if pipeline and writes and re.match(r"^(?:build|create|generate|draft|make|run)\b", text):
        return "build_submit" if re.match(r"^(?:run|build and run|create and run)\b", text) else "build"
    if re.match(r"^(?:load|ingest|extract)\b", text) and re.search(r"\b(?:then|deduplicat\w*|publish|write|update|clean)\b|remove duplicates", text):
        return "build"
    return None


def _repair_id(previous, user):
    if not is_plan(previous):
        return None
    if previous.get("job_id"):
        old = supervisor.get_job(previous["job_id"], user)
        result = old.get("result") or {}
        if old["status"] == "failed" and result.get("run_ref"):
            return f"{old['id']}:{result['run_ref']}"
        return None
    return previous.get("repairs_run_id")


def _sql_recipe(tasks):
    from . import queryguard
    recipe = []
    for task in tasks or []:
        tokens, _ = queryguard._tokens(task.get("sql") or "")
        canonical = tuple((kind, value.casefold() if kind == "word" else value)
                          for kind, value in tokens if (kind, value) != ("punct", ";"))
        recipe.append((task.get("source") or "", canonical))
    return sorted(recipe)


def build(user, prompt, *, source=None, tables=None, model=None, previous=None, context=None):
    from . import airflow_dags, pipeline_dags
    if user["role"] not in ("admin", "analyst"):
        raise HTTPException(403, "Only analysts and administrators can build executable DAGs")
    clarifying = _clarification(prompt, previous)
    if is_plan(previous) and previous.get("job_id"):
        job = supervisor.get_job(previous["job_id"], user)
        previous = copy.deepcopy(previous)
        previous.update(status=job["status"], approved=bool(job.get("human_by")))
        if job["status"] == "failed":
            result = job.get("result") or {}
            previous["failure"] = {"run_id": f"{job['id']}:{result.get('run_ref', '')}",
                                   "error": job.get("last_error") or result.get("detail")}
    source = source if source and source != "*" else (previous or {}).get("source")

    def allowed(action):
        checked = pipeline_dags.validate(user, action.get("plan"), source=source, tables=tables)
        # An existing CTAS output needs adaptation, not loss of its historical
        # recipe. Security-invalid plans have errors/no tasks and stay hidden.
        return bool(checked.get("tasks") and not checked.get("errors"))

    matches = pipeline_memory.successful_recipes(user, prompt, source=source, tables=tables,
        action_types=("airflow_dag",), validate_action=allowed)
    memory = matches[0] if matches else None
    if memory and memory["match"] == "exact" and not previous:
        plan = pipeline_dags.validate(user, copy.deepcopy(memory["action"]["plan"]), source=source, tables=tables)
        plan["memory"] = pipeline_memory.provenance(memory, "exact_revalidated")
    else:
        # The visible conversation is context, not a source of new authority.
        plan = pipeline_dags.build(user, prompt, source=source, tables=tables, model=model,
                                   previous=previous, examples=matches, context=context)
        if memory:
            plan["memory"] = pipeline_memory.provenance(memory, "model_adapted" if plan.get("status") == "ready" else "adaptation_required")
            old = memory["action"]["plan"].get("tasks") or []
            new = plan.get("tasks") or []
            if (memory["match"] != "exact" and plan.get("status") == "ready" and _sql_recipe(old) == _sql_recipe(new)):
                plan.update(status="needs_input", missing=[
                    "The planner returned the previous SQL unchanged for a different request. Clarify the intended change before execution."])
                plan["memory"]["reuse_type"] = "adaptation_unconfirmed"
    if previous and previous.get("failure") and plan.get("status") == "ready" and _sql_recipe(previous.get("tasks")) == _sql_recipe(plan.get("tasks")):
        plan.update(status="needs_input", missing=[
            "The proposed repair did not change the failed SQL. Clarify a correction, or explicitly rerun the existing plan if the failure was transient."])
    objective = (previous or {}).get("prompt") if previous and re.match(r"^(?:fix|repair)\b", prompt.strip(), re.I) else prompt
    if clarifying and (previous or {}).get("prompt"):
        # Keep the actual user objective for later successful-recipe retrieval.
        # This combined text is stored only AFTER planning: the planner still
        # receives the current clarification separately, so an unconfirmed
        # destination in prior model/history text cannot authorize a write.
        objective = f"{previous['prompt']} Clarification: {prompt}"
    plan.update(prompt=objective or prompt, revision_prompt=prompt,
                requested_by=user["id"], execution_mode="airflow_dag")
    repairs = _repair_id(previous, user)
    if repairs:
        plan["repairs_run_id"] = repairs
    if plan.get("status") == "ready":
        try:
            plan["artifact"] = airflow_dags.artifact(plan)
        except Exception as exc:
            plan.update(status="needs_input", missing=[str(exc)])
    return plan


def submit(user, plan, *, request_id, conversation_id=None):
    from . import workflow_runs
    if user["role"] not in ("admin", "analyst"):
        raise HTTPException(403, "Your role cannot deploy pipelines")
    if not is_plan(plan) or plan.get("requested_by") not in (None, user["id"]) and user["role"] != "admin":
        raise HTTPException(403, "This pipeline does not belong to you")
    if not request_id:
        raise HTTPException(400, "A pipeline request identity is required")
    if plan.get("job_id"):
        current = supervisor.get_job(plan["job_id"], user)
        if current.get("kind") != supervisor.DAG_KIND:
            raise HTTPException(400, "The selected job is not an Airflow DAG deployment")
        if (current["status"] in {"awaiting_approval", "approved", "deploying", "launching", "running"}
                or (current.get("recovery") or {}).get("state") in {"pending", "diagnosing", "retrying", "awaiting_approval"}):
            # Repeating a conversational run command observes the already
            # submitted recipe. It is not authority for a second approval or
            # another external run. New/repaired builds have no job_id.
            recorded = json.loads(current["script"])["plan"]
            return {**copy.deepcopy(plan), **recorded, "status": current["status"],
                    "requested_by": plan.get("requested_by") or user["id"], "job_id": current["id"],
                    "job": {"id": current["id"], "status": current["status"]}, "missing": []}
    checked, artifact = workflow_runs.prepare(plan, user)
    out = {**copy.deepcopy(plan), **checked, "requested_by": user["id"], "artifact": artifact}
    if not os.getenv("STUDIO_AIRFLOW_DAGS_DIR") or not platforms.get_platform("airflow").configured():
        out.update(status="needs_configuration", missing=[
            "Configure Airflow credentials and STUDIO_AIRFLOW_DAGS_DIR as a folder shared with its DAG processor. The DAG is available for download; nothing was deployed."])
        return out
    jid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"studio-chat-dag:{user['id']}:{request_id}"))
    # An approved recipe is immutable; config or SQL changes need a new job.
    script = json.dumps({"plan": checked, "digest": artifact["digest"]}, sort_keys=True)
    jobs.check_claim()
    job = supervisor.submit(supervisor.DAG_KIND, "airflow", script, user, job_id=jid, notify=False,
        learning_context={"prompt": plan.get("prompt"), "conversation_id": conversation_id,
                          "repairs_run_id": _repair_id(plan, user),
                          "agent_recovery": {"enabled": True, "max_attempts": 2}})
    out.update(status=job["status"], job_id=job["id"], job={"id": job["id"], "status": job["status"]}, missing=[])
    return out


def recover(user, request_id):
    """A queue replay recovers the approved-request recipe before replanning."""
    jid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"studio-chat-dag:{user['id']}:{request_id}"))
    try:
        job = supervisor.get_job(jid, user)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise
    spec = json.loads(job["script"])
    return {**spec["plan"], "execution_mode": "airflow_dag", "requested_by": user["id"],
            "status": job["status"], "job_id": jid, "job": {"id": jid, "status": job["status"]}}


def refresh(user, plan):
    if not is_plan(plan) or not plan.get("job_id"):
        raise HTTPException(400, "Submit this DAG before asking for its execution status")
    live = supervisor.live_job(plan["job_id"], user)
    out = copy.deepcopy(plan)
    out.update(status=live["job"]["status"], live=live,
               job={"id": live["job"]["id"], "status": live["job"]["status"]})
    return out
