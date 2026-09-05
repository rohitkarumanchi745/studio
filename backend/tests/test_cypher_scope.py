"""The Cypher guard has to reason in CYPHER'S scope, not in one flat table.

The label allowlist is only worth anything if the guard agrees with the engine
about what a variable means. It used to keep one statement-global table of
"variables a label pinned", so three different clauses that DROP a name in real
Cypher — WITH, UNION, and a CALL {} subquery boundary — each left the guard
vouching for a `Person` the engine had already thrown away, and a bare `MATCH
(n)` after one of them read the whole graph with the guard's blessing.

The second half of the file is the other way a label can escape being checked:
a pattern that matches labels it never NAMES — `:!Person`, `:%`, or a dynamic
`:$(expr)` resolved at run time.

Run from the backend directory:
    python -m pytest tests/test_cypher_scope.py -q
"""
import pytest

from app.cypherguard import validate
from app.queryguard import QueryRejected

LABELS = ["Person", "Company"]

UNLABELLED = "must carry a label"
DENIED = "not permitted for your role"
DYNAMIC = "static string literal"
EXCLUSION = "negated or wildcard"


def ok(cypher, allowed=LABELS):
    return validate(cypher, allowed)


def rejected(cypher, match, allowed=LABELS):
    with pytest.raises(QueryRejected, match=match):
        validate(cypher, allowed)


# ── The three scope escapes ─────────────────────────────────────────────

def test_with_that_drops_a_variable_makes_a_later_bare_node_fresh():
    """`WITH count(*) AS c` projects only c, so the `n` after it is a NEW,
    unlabelled binding over every node in the graph — not the Person that the
    first MATCH pinned. The guard used to remember `n` for the whole statement
    and let it through."""
    rejected("MATCH (n:Person) WITH count(*) AS c MATCH (n) RETURN n", UNLABELLED)
    # ...and the same escape one clause further out.
    rejected("MATCH (n:Person) WITH 1 AS x WITH x MATCH (n)-[:R]->(c:Company) "
             "RETURN c", UNLABELLED)


def test_union_starts_a_new_query_part_with_an_empty_scope():
    """Each arm of a UNION is its own query part. Bindings do not cross it, so
    the second arm's `n` is the whole graph."""
    rejected("MATCH (n:Person) RETURN n UNION MATCH (n) RETURN n", UNLABELLED)
    rejected("MATCH (n:Person) RETURN n UNION ALL MATCH (n) RETURN n", UNLABELLED)


def test_a_dynamic_label_is_not_invisible_to_the_allowlist():
    """`:$("Secret")` is a label written as an expression. The extractor only
    understood `:Name`, so the denied label was never even offered to the
    allowlist check and the pattern read Secret nodes."""
    rejected('MATCH (p:Person), (s:$("Secret")) RETURN s', DENIED)


# ── What the scope rules must still allow ───────────────────────────────

def test_a_variable_still_in_scope_may_be_re_matched_bare():
    """The legitimate case the exemption exists for: WITH carries `n` through,
    so continuing the traversal from it is not a fresh whole-graph read."""
    assert ok("MATCH (n:Person) WITH n LIMIT 5 MATCH (n)-->(m:Company) RETURN n, m")
    assert ok("MATCH (n:Person) WITH DISTINCT n MATCH (n)-->(m:Company) RETURN m")
    # A WITH's own WHERE / ORDER BY / SKIP / LIMIT do not narrow what it projects.
    assert ok("MATCH (n:Person) WITH n, n.age AS a WHERE a > 30 "
              "MATCH (n)-->(m:Company) RETURN m")


def test_with_star_preserves_the_whole_scope():
    assert ok("MATCH (n:Person) WITH * MATCH (n)-->(m:Company) RETURN n, m")
    assert ok("MATCH (n:Person) WITH * WITH * MATCH (n)-->(m:Company) RETURN m")


def test_a_union_whose_arms_are_both_labelled_passes():
    assert ok("MATCH (n:Person) RETURN n UNION MATCH (n:Company) RETURN n")
    assert ok("MATCH (n:Person) RETURN n.name AS x "
              "UNION ALL MATCH (c:Company) RETURN c.name AS x")
    # A denied label in the SECOND arm is still checked.
    rejected("MATCH (n:Person) RETURN n UNION MATCH (s:Secret) RETURN s", DENIED)


def test_an_alias_does_not_inherit_the_pin_of_the_variable_it_renames():
    """Only the exact variable a label pinned is exempt; `WITH n AS m` is a new
    name, and the guard will not vouch for it."""
    assert ok("MATCH (n:Person) WITH n AS m RETURN m")
    rejected("MATCH (n:Person) WITH n AS m MATCH (m)-->(c:Company) RETURN c",
             UNLABELLED)
    # A property aliased back to the variable's own name is not a node either.
    rejected("MATCH (n:Person) WITH n.name AS n MATCH (n)-->(c:Company) RETURN c",
             UNLABELLED)


# ── Subquery boundaries ─────────────────────────────────────────────────

def test_a_call_subquery_starts_empty_and_must_label_its_own_nodes():
    rejected("MATCH (n:Person) CALL { MATCH (x) RETURN x } RETURN n, x", UNLABELLED)
    # Even a name that is pinned OUTSIDE is not in scope until a WITH imports it.
    rejected("MATCH (n:Person) CALL { MATCH (n)-->(c:Company) RETURN c } RETURN c",
             UNLABELLED)
    assert ok("MATCH (n:Person) CALL { MATCH (m:Company) RETURN m } RETURN n, m")


def test_an_importing_with_seeds_the_subquery_and_nothing_else_does():
    assert ok("MATCH (n:Person) CALL { WITH n MATCH (n)-->(c:Company) RETURN c } "
              "RETURN c")
    # The importing WITH still only carries the names it lists.
    rejected("MATCH (n:Person) MATCH (c:Company) "
             "CALL { WITH c MATCH (n)-->(x:Company) RETURN x } RETURN x",
             UNLABELLED)


def test_what_a_subquery_returns_is_in_scope_after_it():
    assert ok("MATCH (n:Person) CALL { MATCH (m:Company) RETURN m } "
              "MATCH (m)-->(z:Person) RETURN z")


def test_an_exists_block_imports_the_enclosing_scope_but_exports_nothing():
    assert ok("MATCH (n:Person) WHERE EXISTS { MATCH (n)-->(c:Company) } RETURN n")
    rejected("MATCH (n:Person) WHERE EXISTS { MATCH (x) } RETURN count(*)",
             UNLABELLED)
    # A variable the block binds does not survive it.
    rejected("MATCH (n:Person) WHERE EXISTS { MATCH (c:Company) } "
             "MATCH (c)-->(p:Person) RETURN p", UNLABELLED)


def test_a_count_block_is_walked_like_any_other_subquery():
    assert ok("MATCH (n:Person) "
              "RETURN COUNT { MATCH (n)-->(c:Company) } AS friends")
    rejected("MATCH (n:Person) RETURN COUNT { MATCH (x) } AS everything",
             UNLABELLED)
    rejected("MATCH (n:Person) RETURN COUNT { MATCH (s:Secret) } AS secrets", DENIED)


# ── Every label a pattern can match must be named and allowed ───────────

def test_a_subquery_brace_does_not_hide_a_label_from_the_allowlist():
    """A map literal's braces DO hide a colon (`{name: 'x'}` is a property key),
    and the label scan skipped everything inside any brace to avoid that. A
    subquery brace is not a map, so every denied label written inside one was
    reaching the graph unchecked."""
    for cypher in [
        "MATCH (n:Person) CALL { MATCH (s:Secret) RETURN s } RETURN n, s",
        "MATCH (n:Person) WHERE EXISTS { MATCH (s:Secret) } RETURN n",
        "MATCH (n:Person) WHERE COUNT { MATCH (s:Secret) } > 0 RETURN n",
        "MATCH (n:Person) CALL { MATCH (c:Company {tag: 'x'}) "
        "CALL { MATCH (s:Secret) RETURN s } RETURN s } RETURN s",
    ]:
        rejected(cypher, DENIED)
    # The property map inside a subquery is still read as a map.
    assert ok("MATCH (n:Person) CALL { MATCH (c:Company {kind: 'Secret'}) "
              "RETURN c } RETURN c")


def test_an_alternative_label_is_checked_name_by_name():
    assert ok("MATCH (n:Person|Company) RETURN n")
    rejected("MATCH (n:Person|Secret) RETURN n", DENIED)
    rejected("MATCH (n:Person&Secret) RETURN n", DENIED)


def test_a_parenthesised_label_expression_cannot_hide_a_denied_label():
    assert ok("MATCH (n:(Person|Company)) RETURN n")
    rejected("MATCH (p:Person), (n:(Secret)) RETURN n", DENIED)
    rejected("MATCH (n:(Person|Secret)&Company) RETURN n", DENIED)


def test_a_negated_or_wildcard_label_matches_by_exclusion_and_is_refused():
    """`:!Person` is every node that is not a Person — the whole graph minus one
    label — and `:%` is every labelled node. Neither NAMES what it reads, so
    there is nothing the allowlist can approve."""
    rejected("MATCH (n:!Person) RETURN n", EXCLUSION)
    rejected("MATCH (n:%) RETURN n", EXCLUSION)
    rejected("MATCH (n:Person&!Company) RETURN n", EXCLUSION)
    rejected("MATCH (p:Person)-[:R]->(n:!Company) RETURN n", EXCLUSION)


def test_a_dynamic_label_is_allowed_only_as_a_static_string_literal():
    """A literal is provable before the query runs, so it is checked exactly as
    if it had been written `:Person`. Any other expression resolves at run time
    and cannot be checked at all, so it fails closed."""
    assert ok('MATCH (n:$("Person")) RETURN n')
    assert ok("MATCH (n:$('Company')) RETURN n")
    rejected('MATCH (n:$("Secret")) RETURN n', DENIED)
    for cypher in [
        "MATCH (n:$(x)) RETURN n",
        "MATCH (n:$param) RETURN n",
        'MATCH (n:Person) MATCH (m:$(n.kind)) RETURN m',
        'MATCH (n:$("Per" + "son")) RETURN n',
        'MATCH (n:Person|$(x)) RETURN n',
    ]:
        rejected(cypher, DYNAMIC)


def test_a_dynamic_relationship_type_fails_closed_the_same_way():
    """Relationship types carry no allowlist of their own (RBAC for this source
    is keyed on labels), but an unprovable one is still refused: the guard will
    not sign off on a pattern whose text it cannot resolve."""
    assert ok('MATCH (n:Person)-[r:$("KNOWS")]->(m:Person) RETURN m')
    rejected("MATCH (n:Person)-[r:$(x)]->(m:Person) RETURN m", DYNAMIC)


def test_a_label_predicate_in_where_is_checked_like_one_in_a_pattern():
    rejected("MATCH (n:Person) WHERE n:$(\"Secret\") RETURN n", DENIED)
    rejected("MATCH (n:Person) WHERE n:!Company RETURN n", EXCLUSION)


def test_a_colon_that_is_not_a_label_is_left_alone():
    """The label-expression parser runs at every ':' outside a map literal, so
    it must not mistake a map key, a slice or a quoted name for a label."""
    assert ok("MATCH (n:Person {name: 'x'}) RETURN n")
    assert ok("MATCH (n:Person) RETURN [1, 2, 3][0:2]")
    assert ok("MATCH (n:`Person`) RETURN n {.name}")
    assert ok("MATCH (n:Person) WITH {kind: 'Secret'} AS m RETURN m.kind")
