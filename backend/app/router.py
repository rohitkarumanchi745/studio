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


def _qualifiers_in_namespace(connector, sql):
    """Every QUALIFIED FROM/JOIN target in `sql` names this connector's own
    configured namespace — the same question gateway.check() asks before it
    executes anything.

    Reuses the guard's machinery rather than re-reading dots: the reference is
    tokenized by queryguard._table_refs (so `db.schema."Tbl"` is one reference
    with its qualifier intact), canonicalized for the connector's dialect with
    queryguard._canon, and matched AT ITS ARITY against
    queryguard._declared_qualifiers(connector.qualifiers()) via
    queryguard._qualifier_ok — a whole-prefix comparison, never a suffix one.

    A connector that declares nothing (the base default, and every sqlite /
    in-memory source) accepts NO qualifier, so `secret_schema.sales` is refused
    there instead of collapsing to `sales`. None is read as that same empty
    declaration: validate() would skip the check, and skipping it is the wrong
    way to be wrong on a routing decision.
    """
    from . import queryguard
    dialect = getattr(connector, "dialect", None)
    declare = getattr(connector, "qualifiers", None)
    quals = queryguard._declared_qualifiers(
        (declare() if callable(declare) else None) or frozenset(), dialect)
    toks, _ = queryguard._tokens((sql or "").strip().rstrip(";").strip())
    for parts, _at in queryguard._table_refs(toks):
        qual_parts = [queryguard._canon(p, dialect) for p in parts[:-1]]
        if qual_parts and not queryguard._qualifier_ok(qual_parts, quals):
            return False
    return True


def _has_access(user, source, sql):
    """Could this requester actually READ every base table the learned pattern
    names? The learned pattern is centralized (another user may have
    established it), so only a requester with the access it needs is routed to
    BitNet.

    What that means here, precisely:
      * the pattern's base tables come from queryguard.base_tables() — the
        guard's own tokenizer, so a comma join's second arm is seen and a name
        the query binds as a CTE is not counted as a table, exactly as the
        gateway reads it;
      * every one of them is on the allowlist gateway.scope() built for this
        requester's role, compared case-insensitively (base_tables' lowercase
        convention — the dialect-exact reading of an identifier is the
        executing guard's job);
      * a QUALIFIED reference must name the connector's configured namespace
        (_qualifiers_in_namespace). Dropping the qualifier was the defect this
        docstring used to describe away: `secret_schema.sales` collapsed to
        `sales` and passed on an allowlist entry for a table in a different
        schema.

    This is NOT the access boundary — gateway.execute re-validates every
    statement with the full guard, and that is what actually protects the data.
    It is a ROUTING decision, and a wrong one costs a BitNet call on a pattern
    whose data the requester cannot read plus the escalation after it fails. So
    it fails CLOSED on everything it cannot read for itself: unparseable SQL, a
    query with no attributable base table (an all-CTE pattern included), a
    scope() that raises. A non-SQL dialect (cypher) has no FROM/JOIN target to
    attribute either, so BitNet routing stays SQL-only.

    Cheap by construction: this runs for every candidate prompt on the routing
    hot path, so beyond the one gateway.scope() call it is purely lexical — no
    SQL is executed and the warehouse is never touched.
    """
    from . import gateway, queryguard
    try:
        connector, allowed_tables = gateway.scope(user, source)
        allowed = {str(t).strip('"').strip("`").lower() for t in allowed_tables}
        refs = queryguard.base_tables(sql)      # tokenizer-based, CTE-aware
        if not refs or not refs.issubset(allowed):
            return False
        return _qualifiers_in_namespace(connector, sql)
    except Exception:
        return False


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
