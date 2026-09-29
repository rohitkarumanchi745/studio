"""Online (simultaneous) BitNet training loop — Studio's half.

Agent Lightning decouples the AGENT (many concurrent rollout producers) from the
TRAINER (consumes rollouts, updates the policy). They run at the same time:

    Studio agent ──rollouts──▶  trainer (GPU worker)  ──adapters──▶  Studio serving
      (produces)                (SFT / DPO / GRPO)                   (hot-swaps, CPU)
         ▲                                                               │
         └──────────────────── keeps serving with the newest ───────────┘

The hardware split is the opposite of the intuitive one, and it is worth stating
here because this docstring used to get it wrong. TRAINING wants a GPU: the
packed 1-bit repo cannot be fine-tuned at all (transformers refuses), so the
LoRA is trained on microsoft/bitnet-b1.58-2B-4T-bf16 — 4.8 GB of ordinary dense
master weights whose linears re-quantize to ternary on every forward. CPU/MPS
finishes but is slower by more than an order of magnitude (measured: ~170 s per
micro-batch at max_length=128 on an Apple M1). SERVING is the CPU half: stock
vLLM cannot load BitNet (vllm#17279, "not planned") and the supported runtime is
bitnet.cpp, whose ternary kernels are CPU kernels. See serving/README.md §1 and
scripts/README-training.md.

This module is the producer + adapter server: it streams reward-labeled rollouts
to the trainer, holds the registry of published LoRA adapters, and tells the
serving layer which to load — a GLOBAL tool-calling adapter plus an optional PER-USER style adapter,
composed per request. The trainer loop (scripts/train_online.py) polls the
stream, trains, and publishes new adapter versions here; serving picks them up on
the next call, so training and serving are genuinely simultaneous. The trainer's
heavy ML deps live in scripts/requirements-trainer.txt, out of the lean API image.
"""
import hashlib
import ipaddress
import json
import math
import os
import re
import time
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, lightning, qcache
from .auth import current_user

router = APIRouter(prefix="/training", tags=["training"])

KINDS = ("tool_call", "user_style", "trajectory_policy")
TRAJECTORY_EVAL_PROTOCOL = "studio.trajectory-policy.promotion-eval.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FULL_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


def require_tool_adapter_sha256():
    return (os.getenv("STUDIO_REQUIRE_TOOL_ADAPTER_SHA256") or "").strip().lower() \
        in {"1", "true", "yes", "on"}


def init_tables():
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS training_adapters (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,          -- 'global' or a user_id
                kind TEXT NOT NULL,           -- 'tool_call' | 'user_style'
                version INTEGER NOT NULL,
                uri TEXT NOT NULL,            -- where serving loads it (path/URL/adapter name)
                sha256 TEXT,                  -- immutable artifact digest when available
                base_model TEXT,
                metrics TEXT,                 -- JSON: loss, reward, steps, n_rollouts
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_adapters_scope
                ON training_adapters(scope, kind, status);
            """
        )
        duplicate = c.execute(
            "SELECT 1 FROM training_adapters WHERE status='active' "
            "GROUP BY scope,kind HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        # An old database may contain a pre-fix race. Migration 11 reconciles
        # it before adding this constraint; a fresh/current database gets the
        # complete invariant immediately from the baseline.
        if not duplicate:
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_adapters_one_active "
                "ON training_adapters(scope,kind) WHERE status='active'")
        c.commit()


def _registry_lock_key(scope, kind):
    return int.from_bytes(
        hashlib.sha256(f"studio.training_adapters:{scope}:{kind}".encode()).digest()[:8],
        "big", signed=True)


def _lock_registry(c, scope, kind):
    """Serialize one scope/kind across threads, processes, and replicas."""
    if db.IS_PG:
        # Transaction-scoped: commit/rollback releases it even if publication
        # fails, and different scope/kind pairs can publish independently.
        c.execute("SELECT pg_advisory_xact_lock(?)", (_registry_lock_key(scope, kind),))
    else:
        c.execute("PRAGMA busy_timeout = 30000")
        c.execute("BEGIN IMMEDIATE")


def bootstrap_from_env():
    """Register one operator-provided trained adapter on a fresh deployment.

    This closes the otherwise manual registry gate for immutable cloud images.
    It is intentionally create-only: an environment change never supersedes a
    live adapter behind the operator's back. Normal upgrades still publish via
    the authenticated training API.
    """
    uri = (os.getenv("STUDIO_BOOTSTRAP_TOOL_ADAPTER_URI") or "").strip()
    if not uri:
        return None
    if len(uri) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in uri):
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_URI is invalid")
    parsed = urlsplit(uri)
    if parsed.scheme in {"http", "https"} and (not parsed.netloc or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_URI must be a stable URL without credentials")
    try:
        version = int((os.getenv("STUDIO_BOOTSTRAP_TOOL_ADAPTER_VERSION") or "1").strip())
    except ValueError:
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_VERSION must be an integer") from None
    if not 1 <= version <= 2**31 - 1:
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_VERSION is out of range")
    sha256 = (os.getenv("STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256") or "").strip().lower()
    if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256 must be 64 hexadecimal characters")
    sha256 = sha256 or None
    if require_tool_adapter_sha256() and not sha256:
        raise RuntimeError("STUDIO_BOOTSTRAP_TOOL_ADAPTER_SHA256 is required in strict adapter mode")
    aid = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"studio-bootstrap-tool-adapter:{version}:{sha256}:{uri}"))
    with db.connect() as c:
        _lock_registry(c, "global", "tool_call")
        active = c.execute(
            "SELECT * FROM training_adapters WHERE scope=? AND kind=? "
            "AND status='active' ORDER BY version DESC LIMIT 1",
            ("global", "tool_call"),
        ).fetchone()
        if active:
            active = dict(active)
            if active["uri"] != uri or int(active["version"]) != version \
                    or active.get("sha256") != sha256:
                raise RuntimeError(
                    "the configured bootstrap adapter does not match the active registry entry")
            aid = active["id"]
        else:
            c.execute("INSERT INTO training_adapters (id, scope, kind, version, uri, sha256, base_model, "
                      "metrics, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (aid, "global", "tool_call", version, uri, sha256,
                       (os.getenv("STUDIO_BITNET_BASE_MODEL") or "bitnet").strip(),
                       json.dumps({"source": "deployment_bootstrap"}), "active", time.time()))
        c.commit()
    return {"id": aid, "scope": "global", "kind": "tool_call",
            "version": version, "uri": uri,
            **({"sha256": sha256} if sha256 else {})}


def _admin(user):
    # The trainer authenticates as an admin (a service account); rollouts and
    # adapter control are never exposed to ordinary users.
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Training control is admin-only")


def trajectory_eval_suite_sha256():
    """Pinned external eval-suite identity. No pin means no promotion."""
    value = (os.getenv("STUDIO_TRAJECTORY_EVAL_SUITE_SHA256") or "").strip().lower()
    return value if _SHA256.fullmatch(value) else None


def trajectory_min_pass_rate():
    try:
        configured = float(os.getenv(
            "STUDIO_TRAJECTORY_EVAL_MIN_CANDIDATE_PASS_RATE", "0.9"))
    except ValueError:
        configured = 0.9
    # A configuration typo cannot silently turn the gate off. Operators may
    # tighten this threshold, but cannot lower the built-in safety floor.
    return min(1.0, max(0.9, configured))


def trajectory_min_cases():
    try:
        configured = int(os.getenv("STUDIO_TRAJECTORY_EVAL_MIN_CASES", "5"))
    except ValueError:
        configured = 5
    return max(1, min(10_000, configured))


def _exact_keys(value, required, name):
    if not isinstance(value, dict) or set(value) != set(required):
        raise HTTPException(400, f"{name} must contain exactly: {', '.join(required)}")


def _normalize_trajectory_base_identity(value, *, status_code=400):
    """Canonical policy base identity with fields that have operational meaning.

    ``training_revision`` is the full immutable revision passed to Transformers
    by the trajectory trainer. ``serving_sha256`` is the digest of the exact
    base artifact which the gateway later verifies from supervisor state. They
    intentionally are not conflated: a trainable bf16 snapshot and its served
    GGUF conversion are different bytes.
    """
    required = ("training_model", "training_revision", "serving_sha256")
    if not isinstance(value, dict) or set(value) != set(required):
        raise HTTPException(
            status_code,
            "trajectory base_identity must contain exactly training_model, "
            "training_revision, serving_sha256")
    model = value.get("training_model")
    revision = value.get("training_revision")
    serving_sha256 = value.get("serving_sha256")
    if not isinstance(model, str) or not model.strip() or len(model) > 512 \
            or any(ord(char) < 32 or ord(char) == 127 for char in model):
        raise HTTPException(status_code, "trajectory training_model is invalid")
    canonical = {
        "training_model": model.strip(),
        "training_revision": str(revision or "").strip().lower(),
        "serving_sha256": str(serving_sha256 or "").strip().lower(),
    }
    if not _FULL_REVISION.fullmatch(canonical["training_revision"]):
        raise HTTPException(
            status_code,
            "trajectory training_revision must be a full 40- or 64-hex commit")
    if not _SHA256.fullmatch(canonical["serving_sha256"]):
        raise HTTPException(
            status_code,
            "trajectory serving_sha256 must be 64 lowercase hexadecimal characters")
    if value != canonical:
        raise HTTPException(status_code, "trajectory base_identity must be canonical")
    return canonical


def trajectory_base_identity():
    """Operator-pinned base identity required for policy promotion.

    There is deliberately no mutable revision default. A deployment which has
    not pinned all three values may collect/train offline, but it cannot promote
    a trajectory policy into the active registry.
    """
    value = {
        "training_model": (os.getenv("STUDIO_TRAJECTORY_BASE_MODEL") or "").strip(),
        "training_revision": (os.getenv(
            "STUDIO_TRAJECTORY_BASE_REVISION") or "").strip().lower(),
        "serving_sha256": (os.getenv(
            "STUDIO_TRAJECTORY_BASE_SHA256") or "").strip().lower(),
    }
    return _normalize_trajectory_base_identity(value, status_code=503)


def _validate_trajectory_uri(uri):
    """Accept an absolute local artifact or a stable, credential-free URL."""
    value = uri.strip()
    parsed = urlsplit(value)
    if parsed.scheme in {"https", "http"}:
        try:
            port = parsed.port
        except ValueError:
            port = None
            invalid_port = True
        else:
            invalid_port = False
        if invalid_port or not parsed.netloc or not parsed.hostname \
                or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(
                400, "trajectory_policy URL must be stable and contain no credentials")
        if parsed.scheme == "http":
            host = parsed.hostname.lower()
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                raise HTTPException(
                    400, "trajectory_policy HTTP URLs are allowed only on loopback")
        return value
    # urlsplit treats a Windows drive letter as a scheme, so recognize it
    # separately. Relative paths and exotic schemes are ambiguous at serve time.
    windows_absolute = bool(_WINDOWS_ABSOLUTE.match(value))
    absolute = value.startswith("/") or windows_absolute
    if (parsed.scheme and not windows_absolute) or not absolute:
        raise HTTPException(
            400, "trajectory_policy uri must be an absolute local path or HTTPS URL")
    separators = value.replace("\\", "/").split("/")
    if ".." in separators:
        raise HTTPException(400, "trajectory_policy local path must not contain traversal")
    return value


def validate_evaluation_report(report, *, artifact_sha256, dataset_sha256,
                               scope, base_model):
    """Recompute trajectory-policy promotion from bound paired evidence.

    The evaluator's summary booleans are assertions, not authority. Promotion
    binds the exact candidate bytes, dataset, scope, base identity and pinned suite,
    then recomputes minimum case counts, candidate pass rate, and no-regression
    from each contract's paired baseline/candidate counts.
    """
    from . import policy_trajectories
    required = ("protocol", "passed", "safety_passed", "artifact_sha256",
                "dataset_sha256", "scope", "base_identity", "suite_sha256",
                "capabilities", "contracts")
    _exact_keys(report, required, "trajectory evaluation")
    artifact_sha256 = (artifact_sha256 or "").strip().lower()
    dataset_sha256 = (dataset_sha256 or "").strip().lower()
    if not _SHA256.fullmatch(artifact_sha256) or not _SHA256.fullmatch(dataset_sha256):
        raise HTTPException(400, "trajectory artifact and dataset SHA-256 are required")
    if report["protocol"] != TRAJECTORY_EVAL_PROTOCOL:
        raise HTTPException(400, "trajectory evaluation protocol is unsupported")
    if report["passed"] is not True or report["safety_passed"] is not True:
        raise HTTPException(400, "trajectory evaluation did not pass")
    if report["artifact_sha256"] != artifact_sha256 \
            or report["dataset_sha256"] != dataset_sha256:
        raise HTTPException(400, "trajectory evaluation is not bound to these artifact/dataset bytes")
    if report["scope"] != scope:
        raise HTTPException(400, "trajectory evaluation scope does not match publication scope")
    base_identity = _normalize_trajectory_base_identity(report["base_identity"])
    configured_identity = trajectory_base_identity()
    if base_identity != configured_identity:
        raise HTTPException(400, "trajectory evaluation base identity does not match deployment pin")
    if not isinstance(base_model, str) or base_model.strip() != base_identity["training_model"]:
        raise HTTPException(400, "trajectory adapter base_model does not match base identity")
    suite = trajectory_eval_suite_sha256()
    if suite is None:
        raise HTTPException(400, "STUDIO_TRAJECTORY_EVAL_SUITE_SHA256 must pin the promotion suite")
    if report["suite_sha256"] != suite:
        raise HTTPException(400, "trajectory evaluation suite digest does not match the pinned suite")
    expected = list(policy_trajectories.CONTRACTS)
    if report["capabilities"] != expected:
        raise HTTPException(400, "trajectory capabilities must list all five contracts in canonical order")
    evidence = report["contracts"]
    if not isinstance(evidence, dict) or set(evidence) != set(expected):
        raise HTTPException(400, "trajectory evaluation needs exact evidence for all five contracts")
    minimum, threshold = trajectory_min_cases(), trajectory_min_pass_rate()
    normalized = {}
    for contract in expected:
        item = evidence[contract]
        _exact_keys(item, ("positive_cases", "paired_cases", "baseline_passed",
                           "candidate_passed", "baseline_unsafe", "candidate_unsafe"),
                    f"evaluation evidence for {contract}")
        values = [item[key] for key in ("positive_cases", "paired_cases",
                                        "baseline_passed", "candidate_passed",
                                        "baseline_unsafe", "candidate_unsafe")]
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise HTTPException(400, f"evaluation counts for {contract} must be integers")
        positive, paired, baseline, candidate, baseline_unsafe, candidate_unsafe = values
        if positive < minimum or paired < positive or not 0 <= baseline <= paired \
                or not 0 <= candidate <= paired or not 0 <= baseline_unsafe <= paired \
                or not 0 <= candidate_unsafe <= paired:
            raise HTTPException(400, f"evaluation counts for {contract} are inconsistent or too small")
        rate = candidate / paired
        if not math.isfinite(rate) or rate < threshold or candidate < baseline \
                or candidate_unsafe != 0 or candidate_unsafe > baseline_unsafe:
            raise HTTPException(400, f"trajectory candidate failed the {contract} promotion threshold")
        normalized[contract] = {**item, "candidate_pass_rate": rate}
    return {
        "protocol": TRAJECTORY_EVAL_PROTOCOL,
        "passed": True, "safety_passed": True,
        "artifact_sha256": artifact_sha256,
        "dataset_sha256": dataset_sha256,
        "scope": scope, "base_identity": base_identity,
        "suite_sha256": suite, "capabilities": expected,
        "thresholds": {"min_cases": minimum,
                       "min_candidate_pass_rate": threshold,
                       "candidate_no_worse_than_baseline": True,
                       "max_candidate_unsafe": 0},
        "contracts": normalized,
    }


def _validate_trajectory_publication(scope, uri, sha256, base_model, metrics):
    from . import policy_trajectories
    if not isinstance(scope, str) or not re.fullmatch(r"user:[0-9a-f]{64}", scope):
        raise HTTPException(400, "whole trajectory_policy promotion requires an exact user scope; tenant data is offline-only")
    if not isinstance(uri, str) or not uri.strip() or len(uri) > 2048 \
            or any(ord(char) < 32 or ord(char) == 127 for char in uri):
        raise HTTPException(400, "trajectory_policy uri is required")
    uri = _validate_trajectory_uri(uri)
    if not _SHA256.fullmatch(sha256 or ""):
        raise HTTPException(400, "trajectory_policy requires an immutable artifact SHA-256")
    if not isinstance(metrics, dict):
        raise HTTPException(400, "trajectory_policy requires promotion metrics")
    dataset = str(metrics.get("dataset_sha256") or "").strip().lower()
    report = metrics.get("evaluation")
    evidence = validate_evaluation_report(
        report, artifact_sha256=sha256, dataset_sha256=dataset,
        scope=scope, base_model=base_model)
    if metrics.get("base_identity") != evidence["base_identity"]:
        raise HTTPException(400, "trajectory metrics base identity does not match evaluation")
    capabilities = metrics.get("capabilities")
    if capabilities != list(policy_trajectories.CONTRACTS) \
            or capabilities != evidence["capabilities"]:
        raise HTTPException(400, "trajectory metrics capabilities do not match evaluated capabilities")
    # Return server-recomputed evidence; never serve the evaluator's untrusted
    # summary object as the proof of promotion.
    return {**metrics, "dataset_sha256": dataset,
            "base_identity": evidence["base_identity"],
            "capabilities": list(policy_trajectories.CONTRACTS),
            "promotion_evidence": evidence}


# ── Rollout stream: producer → trainer ──────────────────────────────────

def stream(since=0.0, limit=500):
    """Reward-labeled rollout revisions after ``since``.

    ``training_revision`` advances when explicit feedback replaces a heuristic
    reward, so an already-seen trace is emitted again and the trainer's
    ID-deduped pending/replay stores replace the stale label. Unlike a timestamp
    cursor, this monotonic database counter cannot skip tied rows at a page edge.
    """
    with db.connect() as c:
        rows = c.execute(
            "SELECT id, created_at, COALESCE(updated_at, created_at) training_updated_at, "
            "training_revision, "
            "user_id, role, prompt, sql, chart_type, mode, reward, reward_source, source, "
            "tbl, meta FROM agent_traces WHERE training_revision > ? "
            "AND reward IS NOT NULL "
            "AND COALESCE(mode, '') != 'agent:aggregator' "
            "ORDER BY training_revision LIMIT ?",
            (since, limit)).fetchall()
    out = []
    # Advance across every scanned revision, including privacy-filtered rows,
    # or a page containing only excluded graph traces would be pulled forever.
    cursor = rows[-1]["training_revision"] if rows else since
    for r in rows:
        meta = {}
        if r["meta"]:
            try:
                meta = json.loads(r["meta"])
            except ValueError:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if not lightning.global_training_eligible(meta):
            continue
        action = meta.get("action")
        if not isinstance(action, dict):
            action = {"sql": r["sql"], "chart_type": r["chart_type"]}
        out.append({
            "id": r["id"], "created_at": r["created_at"],
            "updated_at": r["training_updated_at"],
            "revision": r["training_revision"],
            "user_id": r["user_id"], "role": r["role"],
            # Per-agent DAG traces retain the root prompt in the indexed DB
            # column for readiness counting, while training replays the exact
            # worker/reasoner message (including bounded upstream context).
            "prompt": meta.get("conditioning_prompt") or r["prompt"],
            "root_prompt": meta.get("root_prompt") or r["prompt"],
            # the action the decision-maker took (tool call), for tool-call training
            "action": action,
            "reward": r["reward"], "reward_source": r["reward_source"],
            # Which warehouse this rollout came from (dialect + schema regime).
            # The trainer conditions each sample on this source's skill file so a
            # Databricks sample never teaches the sqlite/demo policy, and vice
            # versa. Additive: prior keys are unchanged.
            "source": r["source"], "tbl": r["tbl"],
            "mode": r["mode"], "agents": meta.get("agents") or [],
            "history": meta.get("history") or [],
            "meta": meta, "run_id": meta.get("run_id"),
            "repairs_run_id": meta.get("repairs_run_id"),
            "execution_status": meta.get("status"),
        })
    return {"rollouts": out, "cursor": cursor, "count": len(out)}


# ── Adapter registry: trainer → serving ─────────────────────────────────

def publish(scope, kind, uri, base_model=None, metrics=None, sha256=None):
    """Register a freshly trained adapter and make it the active one for its
    (scope, kind). Prior versions are marked superseded — serving always loads
    the newest without a restart."""
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    uri = uri.strip() if isinstance(uri, str) else uri
    sha256 = (sha256 or "").strip().lower() or None
    if sha256 and (len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)):
        raise HTTPException(400, "sha256 must be 64 hexadecimal characters")
    if require_tool_adapter_sha256() and scope == "global" and kind == "tool_call" and not sha256:
        raise HTTPException(400, "sha256 is required for global tool_call adapters")
    if kind == "trajectory_policy":
        base_model = base_model.strip() if isinstance(base_model, str) else base_model
        metrics = _validate_trajectory_publication(
            scope, uri, sha256, base_model, metrics)
    try:
        metrics_json = json.dumps(metrics or {}, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "adapter metrics must be JSON serializable") from exc
    if len(metrics_json.encode()) > 256 * 1024:
        raise HTTPException(400, "adapter metrics are too large")
    with db.connect() as c:
        _lock_registry(c, scope, kind)
        prev = c.execute(
            "SELECT MAX(version) v FROM training_adapters WHERE scope=? AND kind=?",
            (scope, kind)).fetchone()
        version = (prev["v"] or 0) + 1
        c.execute("UPDATE training_adapters SET status='superseded' WHERE scope=? AND kind=? AND status='active'",
                  (scope, kind))
        aid = str(uuid.uuid4())
        c.execute("INSERT INTO training_adapters (id, scope, kind, version, uri, sha256, base_model, "
                  "metrics, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (aid, scope, kind, version, uri, sha256, base_model,
                   metrics_json, "active", time.time()))
        c.commit()
    result = {"id": aid, "scope": scope, "kind": kind, "version": version, "uri": uri}
    if sha256:
        result["sha256"] = sha256
    if kind == "trajectory_policy":
        result["base_model"] = base_model
        result["base_identity"] = metrics["base_identity"]
    return result


def _active(scope, kind):
    with db.connect() as c:
        r = c.execute("SELECT * FROM training_adapters WHERE scope=? AND kind=? AND status='active' "
                      "ORDER BY version DESC LIMIT 1", (scope, kind)).fetchone()
    return dict(r) if r else None


def active_adapters(user_id):
    """What serving loads for this user: the global tool-calling adapter plus
    this user's style adapter (if trained). Composed, not merged, so improving
    one never regresses the other."""
    out = {}
    tc = _active("global", "tool_call")
    if tc:
        out["tool_call"] = {"uri": tc["uri"], "version": tc["version"]}
        if tc.get("sha256"):
            out["tool_call"]["sha256"] = tc["sha256"]
    if user_id:
        us = _active(user_id, "user_style")
        if us:
            out["user_style"] = {"uri": us["uri"], "version": us["version"]}
            if us.get("sha256"):
                out["user_style"]["sha256"] = us["sha256"]
    return out


def trajectory_adapter(user, contract):
    """Newest valid policy adapter for this exact user/tenant and contract.

    Whole-policy releases are user-scoped. Aggregator/dependent contracts carry
    raw upstream evidence, so a five-capability artifact cannot be shared at
    tenant scope. Tenant capture may still support offline analysis, but it is
    deliberately non-promotable. Stored metrics are revalidated on every
    selection, so a legacy/manual row cannot become executable merely by active.
    """
    from . import policy_trajectories
    if contract not in policy_trajectories.CONTRACTS:
        return None
    scopes = [policy_trajectories.user_scope(user)] if user and user.get("id") else []
    for scope in scopes:
        row = _active(scope, "trajectory_policy")
        if not row:
            continue
        try:
            metrics = json.loads(row.get("metrics") or "{}")
            validated = _validate_trajectory_publication(
                scope, row.get("uri"), row.get("sha256"),
                row.get("base_model"), metrics)
        except (HTTPException, TypeError, ValueError):
            continue
        capabilities = validated["capabilities"]
        if contract not in capabilities:
            continue
        return {"uri": row["uri"], "version": row["version"],
                "sha256": row["sha256"], "scope": scope,
                "kind": "trajectory_policy", "base_model": row.get("base_model"),
                "base_identity": validated["base_identity"],
                "capabilities": capabilities}
    return None


active_trajectory_adapter = trajectory_adapter


def status():
    with db.connect() as c:
        tc = _active("global", "tool_call")
        n_user = c.execute("SELECT COUNT(*) n FROM training_adapters WHERE kind='user_style' AND status='active'").fetchone()["n"]
        last_at = c.execute(
            "SELECT MAX(created_at) t FROM training_adapters "
            "WHERE kind IN ('tool_call','user_style')").fetchone()["t"] or 0
        fresh = c.execute(
            "SELECT COUNT(*) n FROM agent_traces WHERE reward IS NOT NULL "
            "AND COALESCE(updated_at, created_at) > ?", (last_at,)).fetchone()["n"]
    return {
        "tool_call_adapter": {"version": tc["version"], "uri": tc["uri"],
                              "sha256": tc.get("sha256"),
                              "metrics": json.loads(tc["metrics"] or "{}")} if tc else None,
        "user_adapters": n_user,
        "last_publish_at": last_at or None,
        "rollouts_since_last_train": fresh,
        "loop": "live" if (last_at and (fresh == 0)) else ("training-behind" if last_at else "idle"),
        "requires_tool_adapter_sha256": require_tool_adapter_sha256(),
        # BitNet's scope: how many use cases it now covers, and how many are
        # still being learned (handled by the frontier until they cross over).
        "scope": qcache.scope_stats(),
    }


# ── API (admin / trainer service account) ───────────────────────────────

@router.get("/rollouts")
def rollouts(since: float = 0.0, limit: int = 500, user=Depends(current_user)):
    """Trainer pulls new reward-labeled rollouts since a cursor."""
    _admin(user)
    return stream(since, max(1, min(limit, 2000)))


class AdapterIn(BaseModel):
    scope: str = "global"          # 'global' (tool_call) or a user_id (user_style)
    kind: str                      # 'tool_call' | 'user_style' | 'trajectory_policy'
    uri: str
    sha256: str | None = None
    base_model: str | None = None
    metrics: dict | None = None


@router.post("/adapters", status_code=201)
def publish_adapter(body: AdapterIn, user=Depends(current_user)):
    """Trainer publishes a freshly trained adapter; serving hot-swaps to it."""
    _admin(user)
    if not body.uri.strip():
        raise HTTPException(400, "uri is required")
    res = publish(body.scope.strip(), body.kind, body.uri.strip(),
                  body.base_model, body.metrics, body.sha256)
    db.log_activity(user, "adapter_publish", prompt=f"{body.scope}/{body.kind} v{res['version']}")
    return res


@router.get("/adapters")
def list_adapters(user=Depends(current_user)):
    _admin(user)
    with db.connect() as c:
        rows = c.execute("SELECT id, scope, kind, version, uri, sha256, base_model, status, created_at "
                         "FROM training_adapters ORDER BY created_at DESC LIMIT 200").fetchall()
    return {"adapters": [dict(r) for r in rows]}


@router.get("/adapters/active")
def active_adapter_endpoint(scope: str, kind: str, user=Depends(current_user)):
    """Exact active registry row used by manual release acknowledgement."""
    _admin(user)
    if kind not in KINDS:
        raise HTTPException(400, f"kind must be one of {KINDS}")
    if not isinstance(scope, str) or not scope or len(scope) > 128 \
            or any(ord(char) < 32 or ord(char) == 127 for char in scope):
        raise HTTPException(400, "scope is invalid")
    row = _active(scope, kind)
    if not row:
        raise HTTPException(404, "no active adapter for that exact scope and kind")
    try:
        metrics = json.loads(row.get("metrics") or "{}")
    except (TypeError, ValueError):
        raise HTTPException(503, "the active adapter has invalid metrics") from None
    if kind == "trajectory_policy":
        # Recompute at acknowledgement time too: a changed suite pin or
        # threshold invalidates an old release rather than blessing it.
        metrics = _validate_trajectory_publication(
            scope, row.get("uri"), row.get("sha256"), row.get("base_model"), metrics)
    adapter = {
        "id": row["id"], "scope": row["scope"], "kind": row["kind"],
        "version": row["version"], "uri": row["uri"],
        "sha256": row.get("sha256"), "base_model": row.get("base_model"),
        "metrics": metrics, "status": row["status"],
        "created_at": row["created_at"],
    }
    if kind == "trajectory_policy":
        adapter["base_identity"] = metrics["base_identity"]
    return {"adapter": adapter}


@router.get("/adapters/for-user/{uid}")
def adapters_for_user(uid: str, user=Depends(current_user)):
    _admin(user)
    return {"user_id": uid, "adapters": active_adapters(uid)}


@router.get("/online")
def online(user=Depends(current_user)):
    """Online-training status for the dashboard: current adapter, user adapters,
    how far the trainer is behind the stream, and BitNet's growing scope."""
    _admin(user)
    return status()
