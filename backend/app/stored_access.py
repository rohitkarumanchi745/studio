"""Read-time access for stored SQL: may a role still see a statement saved
in history (an answer panel, a blend input, a pipeline recipe) under today's
policy? Kept apart from execution, which always goes through the gateway."""
from . import queryguard, rbac


def stored_step_allowed(role, step, source=None):
    """May `role` see a stored step (its SQL), under TODAY's policy?
    Read-time policy/namespace checks without contacting warehouse catalogs.
    Chat uses it for every stored statement it releases to a reader: answer
    panels, blend inputs, and pipeline recipes kept in conversation history.

    Execution still derives its allowlist from the live catalog through the
    gateway. History only needs today's policy and the connector's declared
    namespace; a temporarily offline warehouse must not erase a conversation.
    """
    if not isinstance(step, dict) or not step.get("sql"):
        return False
    source = step.get("source") or source
    if not source or source == "*":
        return False
    policy = rbac._role_policy(role, source)
    if not policy:
        return False
    try:
        from .connectors import get_connector
        connector = get_connector(source)
        dialect = getattr(connector, "dialect", None)
        if dialect == "cypher":
            # Stored graph results need the same dialect guard as live gateway
            # execution.  Treating Cypher as SQL would safely hide every graph
            # message, but would also make legitimate history unusable.
            from . import cypherguard
            tokens, _ = cypherguard._tokens(step["sql"].strip().rstrip(";"))
            references = [name for name, is_rel in cypherguard._label_refs(tokens)
                          if not is_rel]
            allowed = [name for name in references
                       if rbac.can_access(role, source, name)]
            cypherguard.validate(step["sql"], allowed,
                                 qualifiers=connector.qualifiers())
            return True
        # RBAC matches table policies case-insensitively; SQL resolution is
        # dialect-specific. Derive catalog spellings from each reference and
        # keep only names the policy grants, just as the live catalog filter
        # does. Passing ['sales'] as a Snowflake catalog would wrongly hide
        # an allowed bare `sales`, whose stored name is SALES.
        #
        # A source that spans catalogs and schemas (queryguard.FULL_NAMES, e.g.
        # Databricks "all schemas") lists and grants tables by their FULL name
        # and the guard matches references whole, so the candidates must be
        # full names too: reduced to `retail_products`, every stored answer on
        # such a source was refused and showed as "Hidden" — even to its owner.
        qualifiers = connector.qualifiers()
        tokens, _ = queryguard._tokens(step["sql"].strip().rstrip(";"))
        if qualifiers is queryguard.FULL_NAMES:
            # Only names inside the source's own scope stand in for its listing.
            in_scope = getattr(connector, "full_name_in_scope", None) or (lambda _name: True)
            references = [name for name in
                          (".".join(queryguard._canon(part, dialect) for part in parts)
                           for parts, _ in queryguard._table_refs(tokens))
                          if in_scope(name)]
        else:
            references = [queryguard._canon(parts[-1], dialect)
                          for parts, _ in queryguard._table_refs(tokens)]
        allowed = [name for name in references if rbac.can_access(role, source, name)]
        queryguard.validate(step["sql"], allowed, qualifiers=qualifiers, dialect=dialect)
        return True
    except Exception:
        return False
