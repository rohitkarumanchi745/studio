"""Model router — decide which model answers a prompt.

The point of training BitNet is that *learned, repeated* work should stop paying
for the frontier LLM. So a turn is routed as a cascade:

    1. semantic cache hit  → serve the stored plan, no model at all   (qcache)
    2. BitNet learned it    → the self-hosted BitNet answers            (this)
    3. novel                → the frontier LLM (Claude / GPT)           (default)

This module owns step 2 vs 3. "BitNet learned it" means the prompt matches a
pattern that has recurred and scored well (qcache.learned) AND a self-hosted
BitNet with a trained tool-calling adapter is actually serving. If BitNet is not
configured, every prompt routes to the frontier LLM — so this is dormant until
BitNet is wired up, and never changes current behaviour on its own. When BitNet
does answer, its SQL still passes the query guard, and if it fails or returns
nothing usable the caller escalates to the frontier LLM — BitNet being wrong
costs a retry, never a bad answer.
"""
import os

from . import qcache, trainer


def bitnet_spec():
    """The model spec for the self-hosted BitNet (served OpenAI-compatibly)."""
    return os.getenv("STUDIO_BITNET_LLM", "openai:bitnet")


def bitnet_ready(user):
    """True only when a self-hosted BitNet endpoint is configured AND a trained
    tool-calling adapter exists to serve — otherwise there is nothing to route to."""
    if not os.getenv("STUDIO_LLM_BASE_URL", "").strip():
        return False
    try:
        return bool(trainer.active_adapters(user.get("id") if user else None).get("tool_call"))
    except Exception:
        return False


def _has_access(user, source, sql):
    """The requester's role can reach EVERY table the learned pattern touches —
    'same access'. The pattern is centralized (some other user may have
    established it), so only a requester who can actually access its data is
    routed to BitNet. Execution is guarded regardless; this gates the routing."""
    from . import gateway, queryguard
    try:
        _conn, allowed_tables = gateway.scope(user, source)
        allowed = {t.lower() for t in allowed_tables}
    except Exception:
        return False
    refs = {r.strip('"').split(".")[-1].lower() for r in queryguard.TABLE_REF.findall(sql or "")}
    return bool(refs) and refs.issubset(allowed)


def choose(user, source, table_scope, prompt):
    """Which model answers this prompt, by BitNet's SCOPE.

    BitNet's scope is the set of use cases it has learned — patterns that have
    recurred and scored well (centralized across users). A prompt IN scope, that
    the requester has access to, goes to BitNet. A NEW prompt, out of scope, goes
    to the frontier LLM — whose successful answers accumulate until that use case
    is itself learned and joins BitNet's scope. So the scope grows over time and
    the frontier is always just the frontier of what's new.

    Returns ('bitnet', pattern) or ('frontier', None). The caller escalates to
    the frontier if a BitNet attempt fails (e.g. BitNet hasn't trained on a
    just-learned case yet) — so routing is safe even during the training lag."""
    if not bitnet_ready(user):
        return "frontier", None
    pattern = qcache.learned(source, table_scope, prompt)   # centralized, role-agnostic
    if pattern and _has_access(user, source, pattern["sql"]):
        return "bitnet", pattern
    return "frontier", None   # out of scope → frontier, then it becomes learnable
