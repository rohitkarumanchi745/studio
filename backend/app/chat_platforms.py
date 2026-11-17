"""Explicit chat commands for supervised orchestration-platform runs.

This module only extracts identifiers and JSON supplied by the user. It does
not generate DAGs, notebook paths, container images, or execution approvals.

Commands are plain English: "kick off the daily sales pipeline on Airflow",
"run /opt/jobs/etl.py on Spark on Kubernetes with image my/spark:3.5 with
args --day 2026-11-01", "run `python etl.py` on Ray". Words are mapped to a
field only when they say it outright — a DAG named in words is looked up in
the DAGs Airflow actually has (one match, or a choice of several), a path is
a path, "image X" is the image — and anything left over is asked about,
never guessed. The one default is the operator's: K8S_SPARK_IMAGE (and
K8S_SPARK_JOBS_DIR for a bare file name), or the image set on the cluster.

Spark runs on a Kubernetes cluster an administrator connected (clusters.py):
"on AKS", "on GKE", "locally" / "on minikube" pick one; a request that names
none runs on the first connected of AKS, GKE, the server's own, local — and
the card queued for approval says which.
"""
import copy
import json
import os
import re
import shlex
import time
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
    "ray": "ray", "ray jobs": "ray", "ray job": "ray", "ray cluster": "ray",
    "kuberay": "ray", "anyscale": "ray",
}
# A cluster named is Spark on that cluster ("run etl.py on AKS"). Short names
# like "gke" are whole words only — never part of a path (s3a://gke-lake/…).
_CLUSTER_TARGETS = {
    "aks": "k8s_spark", "azure kubernetes service": "k8s_spark", "azure kubernetes": "k8s_spark",
    "gke": "k8s_spark", "google kubernetes engine": "k8s_spark", "google kubernetes": "k8s_spark",
    "local kubernetes": "k8s_spark", "local k8s": "k8s_spark", "minikube": "k8s_spark",
    "kind cluster": "k8s_spark", "docker desktop": "k8s_spark", "k3d": "k8s_spark",
}
_WORD_START, _WORD_END = r"(?<![\w/.:-])", r"(?![\w/:-])(?!\.\w)"
_PLATFORM = re.compile(r"\b(?:" + "|".join(
    re.escape(k) for k in sorted(_TARGETS, key=len, reverse=True)) + r")\b|" + _WORD_START + "(?:"
    + "|".join(re.escape(k) for k in sorted(_CLUSTER_TARGETS, key=len, reverse=True)) + ")" + _WORD_END,
    re.I)
_TARGETS.update(_CLUSTER_TARGETS)
_IDENTIFIER = r'''(?:"([^"\n]+)"|'([^'\n]+)'|`([^`\n]+)`|([\w.-]+))'''
_NEGATION = re.compile(r"\b(?:don't|don’t|do not|never|without|not yet|instead of|except|unless)\b", re.I)
_COMMAND = re.compile(r"^(?:trigger|re-?run|run|execute|start|submit|deploy|kick[\s-]+off|launch|"
                      r"fire(?:\s+off)?)\b\s*", re.I)
# How a request may open before its verb: "can you", "please", "I'd like to"…
_LEAD = re.compile(r"^(?:(?:(?:can|could|would|will) you|please|kindly|i(?:['’]d| would) like to|"
                   r"i (?:want|need) to|let['’]?s|go ahead and)\s+)+", re.I)
# Words that carry no run parameter: what is left after them is asked about.
_FILLER = re.compile(r"\b(?:this|that|the|a|an|my|our|it|pipeline|workflow|job|run|dag|on|in|now|"
                     r"again|please|spark|application|app|using|via|with|to|into|onto|for me|"
                     r"right away)\b", re.I)
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
        return ["choose Airflow, Databricks Jobs, dbt Cloud, Spark on Kubernetes, or Ray Jobs"]
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
    elif target == "ray":
        if "script" in payload:
            if "entrypoint" in payload:
                errors.append("choose an entrypoint or a script, not both")
            if not isinstance(payload["script"], str) or not payload["script"].strip():
                errors.append("script: provide the Python source to run")
            args = payload.get("args")
            if args is not None and not (isinstance(args, str) or (
                    isinstance(args, list) and all(isinstance(a, str) for a in args))):
                errors.append("args must be a list of strings")
        elif not isinstance(payload.get("entrypoint"), str) or not payload["entrypoint"].strip():
            errors.append("entrypoint: provide the command the cluster runs, or a script")
        for field in ("runtime_env", "metadata"):
            if field in payload and not isinstance(payload[field], dict):
                errors.append(f"{field} must be a JSON object")
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
        cluster = payload.get("cluster")
        if cluster is not None:
            from . import clusters
            if cluster not in clusters.ORDER:
                errors.append("cluster: choose aks, gke, local or default")
            elif _cluster_for(payload)[0] is None:
                errors.append(f"cluster: {_CLUSTER_NAMES[cluster]} is not connected — an administrator "
                              "connects it in Integrations › Compute for Spark")
        if not isinstance(spec.get("image"), str) or not spec["image"].strip():
            if full or not (_cluster_for(payload)[1] or {}).get("image"):
                errors.append("image: provide the Spark container image")
        if spec.get("type") in ("Scala", "Java") and not spec.get("mainClass" if full else "main_class"):
            errors.append("mainClass" if full else
                          f"main_class: provide the {spec['type']} entry point (class com.example.Main)")
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
    text = _LEAD.sub("", text)
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
    picked = _picked(text, previous)
    if picked:
        text = f"dag {picked}"
    filling = bool(previous and previous.get("status") == "needs_input" and (
        re.match(r"^(?:dag|job)(?:_id)?\b", text, re.I) or (supplied is not None and not text)
        or (previous.get("target") == "k8s_spark" and (_spark_said(text) or _cluster_said(text)))
        or (previous.get("target") == "ray" and _command_said(text))))
    if not command and not filling:
        return None
    tail = text[command.end():].strip() if command else text
    if not selected:
        # Recognize external-platform commands even when no platform was
        # supplied; anything else is an ordinary chat question.
        if not re.search(r"\b(?:dag|databricks|airflow|dbt|kubernetes|k8s|ray)\b", tail, re.I):
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
    # Only DAGs and Databricks / dbt jobs have ids; a Spark or Ray "job" names none.
    identifier = selected in ("airflow", "databricks_jobs", "dbt_cloud") and re.search(
        r"\b" + noun + r"(?:_id|\s+id)?(?:\s*(?:=|:)\s*|\s+)" + _IDENTIFIER, tail, re.I)
    if identifier:
        value = next(v for v in identifier.groups() if v is not None)
        if value.lower() not in ("with", "on", "for", "now", "again", "please", "in", "at", "to",
                                 "from", "of", "and", "using", "via", "the", "this", "that"):
            key = "dag_id" if selected == "airflow" else "job_id"
            if key in body and str(body[key]) != value and supplied and key in supplied:
                errors.append(f"conflicting {key} in the command and JSON payload")
            body[key] = int(value) if key == "job_id" and _positive_id(value) else value
            tail = tail[:identifier.start()] + tail[identifier.end():]
    # key=value pairs said outright: Airflow conf, Databricks job parameters.
    tail, pairs = _pairs(tail)
    if pairs:
        field = {"airflow": "conf", "databricks_jobs": "job_parameters"}.get(selected)
        if field:
            body[field] = {**(body.get(field) if isinstance(body.get(field), dict) else {}), **pairs}
        else:
            errors.append("key=value parameters are Airflow conf or Databricks job parameters; "
                          "give this platform a JSON payload")
    if selected == "k8s_spark":
        tail = _cluster_words(text, tail, body, errors)
        tail = _spark_words(tail, body, errors)
    elif selected == "ray":
        tail = _ray_words(tail, body)
    # No inferred deployment/schedule/filter semantics. Any unresolved prose
    # is surfaced for clarification instead of being silently dropped — but a
    # DAG named in words is looked up among the DAGs Airflow has.
    remainder = _leftover(tail)
    choices = []
    if remainder and selected == "airflow" and "dag_id" not in body:
        dag, choices, problem = _dag_named(remainder)
        if dag:
            body["dag_id"] = dag
        if dag or problem:
            remainder = ""
        if problem:
            errors.append(problem)
    if remainder:
        errors.append(f"I could not use “{remainder[:120]}” — give extra run parameters as "
                      "key=value pairs or a JSON payload")
    return _context({"action": "submit", "target": selected, "payload": body,
                     "missing": _one_per_field(errors + _missing(selected, body)),
                     **({"choices": choices} if choices else {})}, prompt, previous,
                    references_previous=reference)


def _one_per_field(messages):
    """Each message once, and one per field ("dag_id: …"): the first, most
    specific one — "no DAG matches 'payroll'" says more than "provide the
    DAG identifier"."""
    seen, out = set(), []
    for m in dict.fromkeys(messages):
        field = re.match(r"^([a-z_]+(?:_id|_file|_class)?|image|arguments|cluster):", m)
        key = field.group(1) if field else m
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out


# ── Plain-words parameters ──────────────────────────────────────────────

def _leftover(tail):
    return re.sub(r"\s+", " ", _FILLER.sub(" ", tail)).strip(" \t\r\n.,;!?:")


_PAIR = re.compile(r"(?<![\w-])([A-Za-z_][\w.-]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)")


def _pairs(tail):
    """("tail without them", {key: value}) for key=value pairs said outright;
    numbers and true/false become JSON numbers and booleans."""
    found = {}
    for key, raw in _PAIR.findall(tail):
        value = raw[1:-1] if raw[:1] in "\"'" else raw
        if raw[:1] not in "\"'":
            if re.fullmatch(r"-?\d{1,15}", value):
                value = int(value)
            elif re.fullmatch(r"-?\d+\.\d+", value):
                value = float(value)
            elif value.lower() in ("true", "false"):
                value = value.lower() == "true"
        found[key] = value
    return (_PAIR.sub(" ", tail) if found else tail), found


def _words(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    return [w for w in re.split(r"[^a-z0-9]+", spaced.lower()) if w]


_DAG_CACHE = {}


def _known_dags():
    """The DAG ids the configured Airflow has (cached a minute), or None when
    Airflow is not configured or cannot be listed."""
    airflow = platforms.PLATFORMS.get("airflow")
    if airflow is None or not hasattr(airflow, "list_dags"):
        return None
    try:
        if not airflow.configured():
            return None
        key = airflow._cfg().get("url")
        hit = _DAG_CACHE.get(key)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        dags = list(airflow.list_dags())
    except Exception:
        return None
    _DAG_CACHE[key] = (time.time(), dags)
    return dags


def _dag_named(phrase):
    """(dag_id, choices, problem) for a DAG named in words ("daily sales"):
    the one Airflow DAG whose id says the same words, or a choice of the
    several that do, or what went wrong — never a made-up id."""
    words = _words(phrase)
    if not words:
        return None, [], None
    known = _known_dags()
    if known is None:
        # Nothing to look it up in: an exact id is taken as typed.
        if re.fullmatch(r"[\w.-]{1,250}", phrase, flags=re.ASCII):
            return phrase, [], None
        return None, [], (f"dag_id: which Airflow DAG is “{phrase[:80]}”? Give its exact id, "
                          "e.g. “dag daily_sales”")
    key = "_".join(words)
    exact = [d for d in known if d.lower() == phrase.lower() or "_".join(_words(d)) == key]
    hits = exact or [d for d in known if set(words) <= set(_words(d))]
    if len(hits) == 1:
        return hits[0], [], None
    if hits:
        return None, hits[:8], (f"dag_id: more than one Airflow DAG matches “{phrase[:80]}” — "
                                f"pick one: {', '.join(hits[:8])}")
    some = ", ".join(known[:6]) + (" …" if len(known) > 6 else "")
    return None, [], (f"dag_id: no Airflow DAG matches “{phrase[:80]}”"
                      + (f" — its DAGs include {some}" if known else " — Airflow lists no DAGs"))


def _picked(text, previous):
    """A DAG picked from the choices a card offered, said on its own."""
    if not previous or previous.get("status") != "needs_input" or previous.get("target") != "airflow":
        return None
    said = text.strip().strip("`'\"“”.!").lower()
    return next((c for c in previous.get("choices") or [] if c.lower() == said), None)


_SPARK_PATH = re.compile(r"((?:local|s3a?|gs|abfss|wasbs|hdfs|file|https?)://[^\s,;'\"`]+"
                         r"|(?<![\w:])/(?:[\w.-]+/)*[\w.-]+\.(?:py|jar|r)\b)", re.I)
_SPARK_FILE = re.compile(r"(?<![\w/.-])([\w-]+\.(?:py|jar|r))\b", re.I)
_SPARK_IMAGE = re.compile(r"\b(?:(?:using|with|on)\s+)?(?:the\s+)?(?:docker\s+|container\s+)?image"
                          r"\s*[:=]?\s*([A-Za-z0-9][\w./:@-]*)", re.I)
_SPARK_CLASS = re.compile(r"\b(?:main[\s_-]?class|class)\s*[:=]?\s*([A-Za-z_$][\w.$]*)", re.I)
_SPARK_NAME = re.compile(r"\b(?:named|called)\s+([A-Za-z0-9][\w-]*)", re.I)
# Which cluster: said outright. "kind" alone is an English word: only "kind cluster" / "on kind".
_CLUSTER_WORDS = tuple((kind, re.compile(_WORD_START + "(?:" + words + ")" + _WORD_END, re.I)) for kind, words in (
    ("aks", r"aks|azure kubernetes(?: service)?|azure k8s"),
    ("gke", r"gke|google kubernetes(?: engine)?|google k8s"),
    ("local", r"locally|(?:(?:on|in) )?(?:the |my )?local (?:kubernetes|k8s|cluster)|"
              r"minikube|kind cluster|on kind|docker desktop|k3d"),
))
_CLUSTER_NAMES = {"aks": "AKS", "gke": "GKE", "local": "the local cluster",
                  "default": "the server's cluster"}


def _cluster_words(text, tail, body, errors):
    """Set the cluster a Spark request names ("on AKS", "locally"); returns
    the tail without those words."""
    said = [kind for kind, pattern in _CLUSTER_WORDS if pattern.search(text)]
    for _, pattern in _CLUSTER_WORDS:
        tail = pattern.sub(" ", tail)
    if len(said) > 1:
        errors.append("cluster: name one cluster — AKS, GKE or local")
        tail = re.sub(r"\b(?:or|and|either)\b", " ", tail, flags=re.I)
    elif said:
        body["cluster"] = said[0]
    return re.sub(r"\bcluster\b", " ", tail, flags=re.I)


def _cluster_said(text):
    return any(pattern.search(text) for _, pattern in _CLUSTER_WORDS)


def _cluster_for(payload):
    """(name, config) of the cluster a Spark payload would run on, or (None, None)."""
    try:
        return platforms.PLATFORMS["k8s_spark"].pick(payload.get("cluster"))
    except Exception:
        return None, None


_SPARK_ARGS = re.compile(r"\b(?:with\s+)?(?:args|arguments)\s*[:=]?\s+(.+)$", re.I)
_TRAILING = re.compile(r"(?:\s+(?:on|in|now|please|right away))+\s*$", re.I)


def _spark_said(text):
    return any(p.search(text) for p in (_SPARK_PATH, _SPARK_FILE, _SPARK_IMAGE, _SPARK_CLASS,
                                        _SPARK_ARGS))


def _spark_words(tail, body, errors):
    """Fill a Spark on Kubernetes shorthand from plain words: the file (a
    path, a URI, or a bare name under K8S_SPARK_JOBS_DIR), "image X",
    "class X", "named X" and "with args …". Returns what is left."""
    original = tail
    m = _SPARK_ARGS.search(tail)
    if m and "arguments" not in body:
        try:
            body["arguments"] = shlex.split(_TRAILING.sub("", m.group(1)))
        except ValueError:
            errors.append("arguments: close the quotes in the arguments")
        tail = tail[:m.start()]
    for pattern, key in ((_SPARK_IMAGE, "image"), (_SPARK_CLASS, "main_class"), (_SPARK_NAME, "name")):
        m = pattern.search(tail)
        if m:
            body.setdefault(key, m.group(1))
            tail = tail[:m.start()] + " " + tail[m.end():]
    m = _SPARK_PATH.search(tail)
    if m:
        path = m.group(1)
        body.setdefault("main_file", f"local://{path}" if path.startswith("/") else path)
        tail = tail[:m.start()] + " " + tail[m.end():]
    else:
        m = _SPARK_FILE.search(tail)
        if m:
            folder = os.getenv("K8S_SPARK_JOBS_DIR", "").rstrip("/")
            if folder:
                body.setdefault("main_file", f"{folder}/{m.group(1)}")
            elif "main_file" not in body and "mainApplicationFile" not in body:
                errors.append(f"main_file: where is {m.group(1)}? Give its full path, "
                              f"e.g. local:///opt/jobs/{m.group(1)}")
            tail = tail[:m.start()] + " " + tail[m.end():]
    main = str(body.get("main_file") or "")
    if main.lower().endswith(".jar") and "type" not in body:
        body["type"] = "Java" if re.search(r"\bjava\b", original, re.I) else "Scala"
    elif main.lower().endswith(".r") and "type" not in body:
        body["type"] = "R"
    if "image" not in body and "spec" not in body:
        image = (_cluster_for(body)[1] or {}).get("image") or os.getenv("K8S_SPARK_IMAGE", "").strip()
        if image:
            body["image"] = image          # the cluster's, else the operator's default
    return re.sub(r"\b(?:java|scala|python|with|and)\b", " ", tail, flags=re.I)


_COMMAND_TEXT = re.compile(r"`([^`]+)`|\"([^\"]+)\"|“([^”]+)”")
_COMMAND_START = re.compile(r"\b((?:python3?|bash|sh|uv\s+run|node|java)\s+\S.*)$", re.I)


def _command_said(text):
    return bool(_COMMAND_TEXT.search(text) or _COMMAND_START.search(text))


def _ray_words(tail, body):
    """A Ray entrypoint said outright: `quoted`, or a command that starts
    with python / bash / sh. Returns what is left."""
    if "entrypoint" in body or "script" in body:
        return tail
    m = _COMMAND_TEXT.search(tail)
    if m:
        body["entrypoint"] = next(g for g in m.groups() if g).strip()
        return tail[:m.start()] + " " + tail[m.end():]
    m = _COMMAND_START.search(tail)
    if m:
        body["entrypoint"] = _TRAILING.sub("", m.group(1)).strip()
        return tail[:m.start()]
    return tail


def _artifact(target, payload, user, *, status="needs_input", missing=None, choices=None):
    platform = platforms.PLATFORMS.get(target)
    out = {"execution_mode": "platform_run", "target": target,
           "label": platform.label if platform else "Pipeline platform",
           "payload": copy.deepcopy(payload), "requested_by": user["id"],
           "status": status, "missing": list(missing or [])}
    if choices:
        out["choices"] = list(choices)[:8]      # DAGs a name matched: the card offers them
    return out


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
    missing = _one_per_field(list(request.get("missing") or []) + errors + _missing(target, payload))
    artifact = _artifact(target, payload, user, missing=missing, choices=request.get("choices"))
    if missing:
        return artifact
    if not platforms.PLATFORMS[target].configured():
        artifact.update(status="needs_configuration", missing=[f"configure {artifact['label']} credentials in Studio"])
        return artifact
    if target == "k8s_spark":
        # Name the cluster (and its image) in what is queued: the approver
        # sees where the run goes, and it goes there even if another cluster
        # is connected before the approval.
        cluster, cfg = _cluster_for(payload)
        if cluster and cluster != "default":
            payload["cluster"] = cluster
        full = "spec" in payload or "kind" in payload
        if cfg and not full and not payload.get("image") and cfg.get("image"):
            payload["image"] = cfg["image"]
        artifact["payload"] = copy.deepcopy(payload)
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
