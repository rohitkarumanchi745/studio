"""Read-only guardrails for agent-generated Cypher — the neo4j source.

Same discipline as queryguard, a different language. The gateway is the ONE
data gate (RBAC → guard → row cap → run → governance → audit), but its guard
was SQL-only, and neo4j speaks Cypher: `MATCH (n:Person) RETURN n` is the read
shape and SELECT does not exist there, so every valid graph query was rejected
as "not a SELECT" and the source was unusable. Centralized orchestration must
not mean one dialect — so the guard, not the execution path, forks:
gateway.execute picks this module or queryguard by connector.dialect, and the
ordering, the QueryRejected type, the row cap, governance.filter_result and the
audit row are all unchanged.

Rules run over TOKENS, never over the raw string: a regex cannot see that
`//` and `/* */` hide a write, that `'CREATE'` inside a string literal is not
one, or that `` `Delete` `` is a quoted name. validate() returns the
COMMENT-FREE Cypher and the caller must execute THAT string — validating one
text and running another is the bug class both guards exist to prevent.

Invariants (all fail closed):
  * one statement — a ';' anywhere is refused;
  * every mutating or administrative clause is refused by token, wherever it
    appears (CREATE, MERGE, DELETE, DETACH, SET, REMOVE, DROP, FOREACH,
    LOAD CSV, USE, SHOW, GRANT, ...);
  * CALL reaches only an allowlist of read-only procedures (db.labels & co).
    apoc.*, dbms.* and gds.* are refused wherever they appear, procedure or
    function: the surface is far too large to vet call by call, and
    `apoc.cypher.doIt` writes from what looks like a projection;
  * node LABELS are this connector's tables (list_tables() returns db.labels()),
    so every label in a pattern must be in the RBAC allowlist, and a node
    pattern carrying NO label is refused — `MATCH (n) RETURN n` and the
    anonymous hop in `(a:Person)-[]->(x)` read the whole graph, which is
    exactly what the allowlist exists to stop;
  * that "no label" test is answered in CYPHER'S OWN SCOPE, not against a
    statement-global variable table. A bare `(n)` is exempt only while `n` is
    still pinned to a label where it is written; WITH replaces the scope with
    what it projects, UNION starts a new part with an empty one, and a CALL {}
    subquery sees only its importing WITH. Out of scope, `(n)` is a FRESH
    binding over the whole graph and is refused like any other unlabelled node;
  * every label a pattern can match must be NAMED and allowed. `:A|B`, `:A&B`
    and `:(A|B)` are each checked name by name; `:!A` and `:%` match by
    exclusion and are refused outright; a dynamic `:$(…)` label or
    relationship type is refused unless it is a static string literal, which
    is checked as if it had been written out (anything else is unprovable
    before the query runs, so it fails closed);
  * a PATTERN COMPREHENSION (`RETURN [ (a)-->(b) | b ]`) is a traversal, not a
    list literal, and is checked as the pattern it is. Its brackets used to
    hide it twice over: its node patterns were never visited, so it read the
    whole graph, and every `:Label` inside a `[…]` was classified as a
    relationship type (which this guard deliberately does not allowlist), so a
    denied label was reachable from any projection. Only a bracket that
    follows a `-` opens a relationship;
  * a top-level RETURN is required, so enforce_limit() has a well-defined place
    to append the row cap.

Known and deliberate: RELATIONSHIP types (`[r:KNOWS]`) are not allowlisted.
RBAC for this source is keyed on node labels — the only thing list_tables()
can enumerate — so there is no policy to check a type against; the nodes at
both ends of every hop still have to be allowed labels, which is what bounds
the read.
"""
from .queryguard import QueryRejected

_WS = " \t\r\n\f\v"

# The clauses a read query may begin with. START (deprecated) and any
# administrative statement are excluded by omission.
_READ_HEADS = frozenset({"match", "optional", "with", "unwind", "return", "call"})

# Whole-word tokens that write, load or reconfigure. Matched by exact token, so
# `n.created_at` (a property), `'CREATE'` (a literal) and `` `Set` `` (a quoted
# name) are untouched. A bare alias named `set` or `use` is collateral damage
# and stays rejected: those words have no read-only meaning worth the risk.
FORBIDDEN = frozenset({
    "create", "merge", "delete", "detach", "set", "remove", "drop", "foreach",
    "load", "csv", "periodic", "commit", "use", "grant", "revoke", "deny",
    "alter", "rename", "terminate", "show", "import", "export",
})

# Procedure/function namespaces refused outright, in any position.
_NAMESPACES = frozenset({"apoc", "dbms", "gds"})

# The only procedures CALL may reach: catalog introspection, no arguments that
# reach outside the database. Lowercased for comparison.
READ_PROCEDURES = frozenset({
    "db.labels", "db.relationshiptypes", "db.propertykeys",
    "db.schema.visualization", "db.schema.nodetypeproperties",
    "db.schema.reltypeproperties",
})

# Keywords that end a MATCH pattern at depth 0 (where the node-pattern rule
# stops looking). An inline `(n:P WHERE ...)` sits inside parens, so it is at
# depth > 0 and does not end the pattern.
_PATTERN_END = frozenset({
    "where", "return", "with", "unwind", "call", "order", "skip", "limit",
    "union", "match", "optional", "using", "yield", "detach",
})

_OPEN, _CLOSE = "([{", ")]}"


# ── Tokenizer ───────────────────────────────────────────────────────────

def _tokens(cypher):
    """(kind, text) tokens with comments removed, plus the comment-free text.

    Cypher quoting differs from SQL: '…' and "…" are both STRING literals
    (with backslash escapes) and `…` is the quoted identifier. Comments are
    `//` to end of line and `/* … */`. Whitespace and comments produce no
    token, so `MATCH/*x*/(n:P)` and `MATCH (n:P)` are indistinguishable to
    every rule below. Raises on an unterminated literal or comment rather than
    guessing where it ends.
    """
    toks, out, i, n = [], [], 0, len(cypher)
    while i < n:
        ch = cypher[i]
        if ch in _WS:
            j = i
            while j < n and cypher[j] in _WS:
                j += 1
            out.append(cypher[i:j])
            i = j
        elif cypher.startswith("//", i):
            j = cypher.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif cypher.startswith("/*", i):
            j = cypher.find("*/", i + 2)
            if j < 0:
                raise QueryRejected("Unterminated comment in query")
            i = j + 2
            out.append(" ")
        elif ch in "'\"":
            j = i + 1
            while True:
                if j >= n:
                    raise QueryRejected("Unterminated string literal in query")
                if cypher[j] == "\\":
                    j += 2
                    continue
                if cypher[j] == ch:
                    break
                j += 1
            toks.append(("str", cypher[i:j + 1]))
            out.append(cypher[i:j + 1])
            i = j + 1
        elif ch == "`":
            j = i + 1
            while True:
                j = cypher.find("`", j)
                if j < 0:
                    raise QueryRejected("Unterminated quoted identifier in query")
                if cypher.startswith("``", j):
                    j += 2
                    continue
                break
            toks.append(("ident", cypher[i + 1:j].replace("``", "`")))
            out.append(cypher[i:j + 1])
            i = j + 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (cypher[j].isalnum() or cypher[j] in "_$"):
                j += 1
            toks.append(("word", cypher[i:j]))
            out.append(cypher[i:j])
            i = j
        elif ch.isdigit():
            j = i
            while j < n and (cypher[j].isalnum() or cypher[j] == "."):
                j += 1
            toks.append(("num", cypher[i:j]))
            out.append(cypher[i:j])
            i = j
        else:
            toks.append(("punct", ch))
            out.append(ch)
            i += 1
    return toks, "".join(out).strip()


def _match_close(toks, i, hi):
    """Index of the bracket closing the one at `i`, or `hi` if unbalanced."""
    depth = 0
    while i < hi:
        kind, text = toks[i]
        if kind == "punct":
            if text in _OPEN:
                depth += 1
            elif text in _CLOSE:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return hi


# ── Clause / procedure rules ────────────────────────────────────────────

def _check_call(toks, i):
    """`CALL` must open a read-only subquery or name an allowlisted procedure.
    Returns True for an allowlisted PROCEDURE call (the caller counts those as
    a permitted data source), False for a `CALL { … }` subquery.

    A subquery needs no allowlist of its own: its body is scanned by the same
    token loop, so a write inside it is refused like any other.
    """
    n = len(toks)
    j = i + 1
    if j < n and toks[j] == ("punct", "{"):
        return False
    parts = []
    while j < n and toks[j][0] in ("word", "ident"):
        parts.append(toks[j][1])
        j += 1
        if j < n and toks[j] == ("punct", "."):
            j += 1
            continue
        break
    name = ".".join(parts).lower()
    if not parts or name not in READ_PROCEDURES:
        raise QueryRejected(
            f"CALL '{name or '?'}' is not an allowed read-only procedure")
    return True


# ── Label expressions ───────────────────────────────────────────────────

# Words that may precede the '{' of a subquery block. Anything else before a
# brace opens a map literal (`WITH {a: 1} AS m`, `(n:P {a: 1})`).
_SUBQUERY_HEADS = frozenset({"call", "exists", "count", "collect"})


def _unquote(text):
    """The body of a 'string' / "string" literal token, escapes resolved."""
    body, out, i = text[1:-1], [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(body[i + 1])
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


def _dynamic_label(toks, i, hi, is_rel, out):
    """`:$(…)` / `:$param` — a DYNAMIC label or relationship type.

    Valid Cypher, and invisible to a guard: `(s:$(row.kind))` names whatever
    label the expression evaluates to at run time, so no allowlist check made
    before execution can be sound. Exactly one form is provable — `:$("Person")`,
    a static string literal — and it is recorded as if it had been written
    `:Person`, so the ordinary allowlist check decides it. Anything else is
    unprovable at guard time and therefore fails closed here.
    """
    j = i + 1
    if (j + 2 < hi and toks[j] == ("punct", "(") and toks[j + 1][0] == "str"
            and toks[j + 2] == ("punct", ")")):
        out.append((_unquote(toks[j + 1][1]).lower(), is_rel))
        return j + 3
    raise QueryRejected(
        "A dynamic label or relationship type is only allowed when it is a "
        "static string literal (any other expression cannot be checked "
        "before the query runs)")


def _label_expression(toks, i, hi, is_rel, out):
    """Parse the label expression that starts at `i` (just past its ':').

    Returns the index one past the expression and appends every label NAME it
    can prove to `out`. The invariant is that EVERY label a pattern can match
    is named in `out`, so a form that matches by exclusion instead of by name
    cannot be allowed to contribute nothing and pass:

      * `!A` and `%` match by exclusion — `(n:!Person)` reads every node that
        is NOT a Person, i.e. the whole graph minus one label, and `(n:%)` is
        every labelled node. Both are refused;
      * `(A|B)` groups are recursed into, so a parenthesised alternative
        cannot hide a denied label from the check;
      * `$(...)` is delegated to _dynamic_label, which fails closed.

    A token that cannot begin a label (a number in `list[1:2]`, a keyword in
    `(n:Person WHERE …)`) simply ends the expression: this is a colon that was
    never a label, and it contributes no name.
    """
    while i < hi:
        kind, text = toks[i]
        if kind in ("word", "ident"):
            out.append((text.lower(), is_rel))
            i += 1
        elif kind == "punct" and text in ("!", "%"):
            raise QueryRejected(
                "A negated or wildcard label expression can match labels "
                "outside your allowlist")
        elif kind == "punct" and text == "(":
            close = _match_close(toks, i, hi)
            _label_expression(toks, i + 1, close, is_rel, out)
            i = close + 1
        elif kind == "punct" and text == "$":
            i = _dynamic_label(toks, i, hi, is_rel, out)
        else:
            return i
        if i < hi and toks[i][0] == "punct" and toks[i][1] in "|&:":
            i += 1                          # `:A|B`, `:A&B`, `:A:B`
            continue
        return i
    return i


def _label_refs(toks):
    """[(name, is_relationship_type)] for every `:Label` outside a map literal.

    Brace depth separates a label from a property key: in `(n:P {name: 'x'})`
    the second colon is a map key. Only a MAP brace hides a label, though —
    a subquery brace (`CALL {`, `EXISTS {`, `COUNT {`, `COLLECT {`) holds
    ordinary patterns, and counting it as depth let `CALL { MATCH (s:Secret) }`
    slip past the allowlist entirely. The stack records which kind each open
    brace was, so a subquery block stays transparent to this scan.

    The bracket KIND — not bracket depth — separates a node label from a
    relationship type. Only a bracket that OPENS A RELATIONSHIP holds an edge,
    and one always follows a `-` (`-[r:KNOWS]->`, `<-[:OWNS]-`). Every other
    `[` is a list: a literal, an index, or a PATTERN COMPREHENSION, and a
    comprehension holds ordinary node patterns. Counting depth instead read
    `RETURN [ (n)-->(x:Secret) | x ]` as an edge and skipped :Secret entirely,
    so a denied label was reachable from any projection. The stack records what
    each open bracket was, and only the INNERMOST one decides: inside
    `[ (n)-[r:KNOWS]->(x:Secret) | x ]`, KNOWS is a type and Secret is a label.
    """
    out, braces, brackets, i, n = [], [], [], 0, len(toks)
    while i < n:
        kind, text = toks[i]
        if kind == "punct":
            if text == "{":
                braces.append(not (i and toks[i - 1][0] == "word"
                                   and toks[i - 1][1].lower() in _SUBQUERY_HEADS))
            elif text == "}":
                if braces:
                    braces.pop()
            elif text == "[":
                brackets.append(i > 0 and toks[i - 1] == ("punct", "-"))
            elif text == "]":
                if brackets:
                    brackets.pop()
            elif text == ":" and not any(braces):
                is_rel = bool(brackets) and brackets[-1]
                i = _label_expression(toks, i + 1, n, is_rel, out)
                continue
        i += 1
    return out


# ── Clause-level scopes ─────────────────────────────────────────────────
#
# The scope threaded through the walk below is the set of variables PINNED to
# an allowed label — bound by a labelled node pattern and still visible. It is
# not a statement-global table: Cypher's own scoping rules decide what a name
# means, so the guard has to follow them or it will vouch for a variable the
# engine has already thrown away (see _walk).

_CLAUSE_HEADS = frozenset({
    "match", "optional", "with", "unwind", "return", "call", "where",
    "order", "skip", "limit", "union", "yield", "using",
})

def _pattern_end(toks, lo, hi):
    """Index where the pattern starting at `lo` stops.

    A pattern ends at the next clause keyword at depth 0 — or at the bracket
    that CLOSES the group it lives in, which is why an unbalanced close ends it
    too. Without that, the region ran on into the enclosing statement and
    `count(*)` looked like a node pattern.
    """
    j, depth = lo, 0
    while j < hi:
        kind, text = toks[j]
        if kind == "punct":
            if text in _OPEN:
                depth += 1
            elif text in _CLOSE:
                depth -= 1
                if depth < 0:
                    break
        elif kind == "word" and depth == 0 and text.lower() in _PATTERN_END:
            break
        j += 1
    return j


def _split_commas(toks, lo, hi):
    """[(start, end)] for the comma-separated items of one clause, depth 0."""
    items, start, depth, i = [], lo, 0, lo
    while i < hi:
        kind, text = toks[i]
        if kind == "punct":
            if text in _OPEN:
                depth += 1
            elif text in _CLOSE:
                depth -= 1
            elif text == "," and depth == 0:
                items.append((start, i))
                start = i + 1
        i += 1
    if start < hi:
        items.append((start, hi))
    return items


def _project(toks, lo, hi, scope):
    """The scope a WITH (or a subquery's RETURN) hands to what follows it.

    WITH is a hard boundary in Cypher: the next clause sees EXACTLY the names
    projected here and nothing else. So the new scope is built from scratch —
    a name survives only if it was pinned upstream and is projected by itself.
    `WITH *` carries the whole scope through, which is what `*` means.

    An aliased item (`expr AS x`) never pins x, even for `WITH n AS m`: the
    alias is a new name, and the guard treats only the exact variable a label
    pinned as exempt from the unlabelled-node rule.
    """
    out, i = set(), lo
    if i < hi and toks[i][0] == "word" and toks[i][1].lower() == "distinct":
        i += 1
    for a, b in _split_commas(toks, i, hi):
        item = toks[a:b]
        if len(item) == 1 and item[0] == ("punct", "*"):
            out |= scope
        elif (len(item) == 1 and item[0][0] in ("word", "ident")
                and item[0][1].lower() in scope):
            out.add(item[0][1].lower())
    return out


def _is_pattern_comprehension(toks, lo, hi):
    """Are the contents of a `[ … ]` a PATTERN COMPREHENSION rather than a list?

    `[ (a)-->(b) WHERE … | b ]` is a MATCH wearing list brackets: it traverses
    the graph, so its node patterns need exactly the label check a MATCH gets.
    Stepping over the bracket instead let `RETURN [ (a)-->(b) | b ]` read the
    whole graph and `[ (a:Secret)-->(b) | b ]` read a denied label.

    The test is deliberately narrow, because a false positive would reject
    ordinary arithmetic: the contents must START with a `(` group AND contain a
    relationship ARROW (`--`, `->`, `<-`, `-[`) at the bracket's own depth.
    `[1, 2]` and `[{a: 1}]` do not start with `(`; `[ (x) - (y) ]` is
    subtraction, whose `-` is followed by `(` and so is not an arrow.
    """
    if lo >= hi or toks[lo] != ("punct", "("):
        return False
    depth = 0
    for j in range(lo, hi):
        kind, text = toks[j]
        if kind != "punct":
            continue
        if text in _OPEN:
            depth += 1
        elif text in _CLOSE:
            depth -= 1
        elif depth == 0 and j + 1 < hi:
            nxt = toks[j + 1]
            if text == "-" and nxt in (("punct", "-"), ("punct", ">"), ("punct", "[")):
                return True
            if text == "<" and nxt == ("punct", "-"):
                return True
    return False


def _check_comprehension(toks, lo, hi, scope):
    """Check the pattern half of a pattern comprehension, in a LOCAL scope.

    `[ pattern WHERE pred | expr ]` binds its variables only inside the
    brackets, so the enclosing scope is COPIED in and nothing escapes: the
    comprehension may re-use a variable the surrounding query pinned
    (`[ (n)-->(c:Company) | c ]`), and cannot pin one for a later clause.
    """
    inner, end, depth = set(scope), hi, 0
    for j in range(lo, hi):
        kind, text = toks[j]
        if kind == "punct":
            if text in _OPEN:
                depth += 1
            elif text in _CLOSE:
                depth -= 1
            elif text == "|" and depth == 0:
                end = j
                break
        elif kind == "word" and depth == 0 and text.lower() == "where":
            end = j
            break
    _check_pattern(toks, lo, end, inner)
    _scan_expression(toks, end, hi, inner)


def _scan_expression(toks, lo, hi, scope):
    """Recurse into every subquery block and pattern comprehension reachable
    from an expression region.

    `EXISTS { … }`, `COUNT { … }` and `COLLECT { … }` import the enclosing
    scope automatically (no WITH), and nothing they bind escapes — so each is
    walked with a COPY of `scope` and its result discarded. A brace that is
    just a map literal is still descended into, because a subquery can sit
    inside one. A `[ … ]` is descended into for the same reason, and is checked
    as a PATTERN when it holds one (_is_pattern_comprehension): a projection is
    the one place a graph traversal can hide outside a MATCH.
    """
    i = lo
    while i < hi:
        if toks[i] == ("punct", "{"):
            close = _match_close(toks, i, hi)
            if i and toks[i - 1][0] == "word" and toks[i - 1][1].lower() in _SUBQUERY_HEADS:
                _walk(toks, i + 1, close, set(scope))
            else:
                _scan_expression(toks, i + 1, close, scope)
            i = close + 1
            continue
        if toks[i] == ("punct", "["):
            close = _match_close(toks, i, hi)
            if _is_pattern_comprehension(toks, i + 1, close):
                _check_comprehension(toks, i + 1, close, scope)
            else:
                _scan_expression(toks, i + 1, close, scope)
            i = close + 1
            continue
        i += 1


def _group_parts(toks, lo, hi):
    """(variable, has_label, [nested (lo, hi) groups]) for one (…) group, read
    at its IMMEDIATE level: anything inside `{…}` (a property map) or `[…]` (a
    relationship pattern) belongs to a deeper level and is skipped. `variable`
    is the pattern variable in `(n:Person)` — the name bound to the node."""
    variable, has_label, nested, i = None, False, [], lo
    while i < hi:
        kind, text = toks[i]
        if kind == "punct":
            if text in "{[":
                i = _match_close(toks, i, hi) + 1
                continue
            if text == "(":
                close = _match_close(toks, i, hi)
                nested.append((i + 1, close))
                i = close + 1
                continue
            if text == ":":
                has_label = True
        elif kind in ("word", "ident") and variable is None and not has_label:
            variable = text.lower()
        i += 1
    return variable, has_label, nested


def _check_node_group(toks, lo, hi, scope):
    """A node pattern must be pinned to a label, and records what it binds.

    `MATCH (n) RETURN n` streams the whole graph and `(a:Person)-[]->(x)`
    returns whatever x happens to be, so an unlabelled node is a read no
    allowlist entry can authorize. Two exemptions, both still bounded:

      * a group that only WRAPS other groups — `shortestPath((a:A)-[*]-(b:B))`
        — is recursed into, so the wrapper cannot launder an unlabelled node;
      * a bare `(n)` re-using a variable that is pinned IN THE CURRENT SCOPE
        (`MATCH (n:Person) WITH n LIMIT 5 MATCH (n)-->(m:Company)`) is the
        ordinary way to continue a traversal, and n is already restricted to
        allowed-label nodes. Out of scope means out: after `WITH count(*) AS c`
        or a UNION, `n` is a FRESH unlabelled binding over the whole graph and
        is refused exactly as a bare `MATCH (n)` is.
    """
    variable, has_label, nested = _group_parts(toks, lo, hi)
    if has_label:
        if variable:
            scope.add(variable)
        return
    if nested:
        for sub_lo, sub_hi in nested:
            _check_node_group(toks, sub_lo, sub_hi, scope)
        return
    if variable and variable in scope:
        return
    raise QueryRejected(
        "Every node in a MATCH must carry a label (an unlabelled node reads "
        "the whole graph)")


def _check_pattern(toks, lo, hi, scope):
    """Check the node groups of one pattern, left to right, so a variable is
    only ever exempt AFTER the labelled pattern that bound it.

    Only `(…)` at the pattern's own level is a node. A relationship's `[…]`
    holds no node, and the parentheses INSIDE one belong to an expression —
    `[r:$("KNOWS")]`, `[r WHERE f(r.x)]` — so the whole bracket is stepped over
    rather than descended into, or those would be read as unlabelled nodes.
    The trailing _scan_expression still visits every bracket, which is where a
    PATTERN COMPREHENSION hiding in an expression gets checked as a pattern.
    """
    i = lo
    while i < hi:
        if toks[i][0] == "punct" and toks[i][1] in "[{":
            i = _match_close(toks, i, hi) + 1
            continue
        if toks[i] == ("punct", "("):
            close = _match_close(toks, i, hi)
            _check_node_group(toks, i + 1, close, scope)
            i = close + 1
            continue
        i += 1
    _scan_expression(toks, lo, hi, scope)


def _walk(toks, lo, hi, scope):
    """Walk one query part's clauses in order, threading Cypher's own scope.

    A statement-global variable table is not sound here, because Cypher throws
    names away at three places and the guard has to throw them away too — or it
    will keep vouching for a `Person` the engine no longer has:

      * WITH replaces the scope with exactly what it projects, so after
        `WITH count(*) AS c` a later `MATCH (n)` is a NEW unlabelled binding;
      * UNION / UNION ALL starts a new query part with an empty scope, so
        `… RETURN n UNION MATCH (n) RETURN n` is a fresh `n` over the graph;
      * a CALL {} subquery starts empty and sees only the names its importing
        WITH hands it (`CALL (n) { … }`, the variable-scope form, never gets
        this far: _check_call refuses a CALL that is neither a plain `{` block
        nor an allowlisted procedure).

    `scope` is mutated in place by pattern checks and rebound at boundaries.
    Returns the pinned names the part's RETURN projects, which is what a CALL
    subquery exports to the clause after it.
    """
    i, exported = lo, set()
    if i < hi and not (toks[i][0] == "word" and toks[i][1].lower() in _CLAUSE_HEADS):
        # A brace block may hold a bare pattern: `EXISTS { (a)-->(b:Person) }`.
        end = _pattern_end(toks, i, hi)
        _check_pattern(toks, i, end, scope)
        i = end
    while i < hi:
        kind, text = toks[i]
        if kind == "punct" and text in "([":
            # The bracket itself is included, not just its contents: a
            # top-level `[ (a)-->(b) | b ]` is a pattern comprehension, and
            # _scan_expression is what recognizes one.
            close = _match_close(toks, i, hi)
            _scan_expression(toks, i, close + 1, scope)
            i = close + 1
            continue
        if kind == "punct" and text == "{":
            _scan_expression(toks, i, _match_close(toks, i, hi) + 1, scope)
            i = _match_close(toks, i, hi) + 1
            continue
        if kind != "word":
            i += 1
            continue
        word = text.lower()
        if word == "union":
            scope = set()                   # a new query part, empty scope
            i += 1
        elif word == "match":
            end = _pattern_end(toks, i + 1, hi)
            _check_pattern(toks, i + 1, end, scope)
            i = end
        elif word in ("with", "return"):
            end = _pattern_end(toks, i + 1, hi)
            _scan_expression(toks, i + 1, end, scope)
            projected = _project(toks, i + 1, end, scope)
            if word == "with":
                scope = projected
            else:
                exported |= projected
            i = end
        elif word == "call":
            j = i + 1
            if j < hi and toks[j] == ("punct", "{"):
                close = _match_close(toks, j, hi)
                # A subquery starts EMPTY. Only an importing WITH (the block's
                # first clause) carries outer variables in, and it trims the
                # seed to the names it lists — exactly Cypher's rule. What the
                # subquery returns becomes visible to the clause after it.
                importing = (j + 1 < close and toks[j + 1][0] == "word"
                             and toks[j + 1][1].lower() == "with")
                seed = set(scope) if importing else set()
                scope = scope | _walk(toks, j + 1, close, seed)
                i = close + 1
            else:
                i = j                       # a procedure call, not a subquery
        else:
            i += 1
    return exported


def _check_patterns(toks):
    """Entry point: walk the statement with an empty scope."""
    _walk(toks, 0, len(toks), set())


# ── Public API (mirrors queryguard's, so the gateway can dispatch) ───────

def validate(cypher, allowed_tables, qualifiers=None):
    """Raise QueryRejected unless `cypher` is a single read-only statement
    touching only allowed labels. Returns the comment-free Cypher the caller
    must execute.

    `allowed_tables` is the RBAC allowlist for this source: node labels, which
    are what the neo4j connector reports as its tables. `qualifiers` exists
    only for signature parity with queryguard.validate so the gateway can
    dispatch without special-casing; Cypher labels carry no namespace, so there
    is nothing to check and an empty set from the connector must NOT be read as
    "reject everything".
    """
    del qualifiers                       # see docstring: parity, not a rule
    raw = (cypher or "").strip().rstrip(";").strip()
    toks, cleaned = _tokens(raw)
    if not toks:
        raise QueryRejected("Empty query")

    head = toks[0]
    if head[0] != "word" or head[1].lower() not in _READ_HEADS:
        raise QueryRejected(
            "Only read-only Cypher (MATCH / OPTIONAL MATCH / WITH / UNWIND / "
            "RETURN / CALL) is allowed")

    has_return, procedures = False, 0
    depth = 0
    for i, (kind, text) in enumerate(toks):
        if kind == "punct":
            if text == ";":     # a ';' inside a literal or comment is not one
                raise QueryRejected("Only a single statement is allowed")
            depth += 1 if text in _OPEN else -1 if text in _CLOSE else 0
            continue
        if kind != "word":
            continue
        word = text.lower()
        if i and toks[i - 1] in (("punct", "."), ("punct", "$")):
            continue                     # a property key / parameter name
        if word in _NAMESPACES:
            raise QueryRejected(
                f"The '{text}' procedure namespace is not permitted")
        if word in FORBIDDEN:
            raise QueryRejected(
                f"Clause '{text.upper()}' writes or reconfigures the graph "
                f"(read-only access)")
        if word == "call":
            if _check_call(toks, i):
                procedures += 1
        elif word == "return" and depth == 0:
            has_return = True
    if not has_return:
        raise QueryRejected("A read query must end with a RETURN clause")

    _check_patterns(toks)

    allowed = {str(t).strip("`").strip('"').lower() for t in allowed_tables}
    labels = [name for name, is_rel in _label_refs(toks) if not is_rel]
    for name in labels:
        if name not in allowed:
            raise QueryRejected(
                f"Access to label '{name}' is not permitted for your role")
    if not labels and not procedures:
        # Fail closed, exactly as the SQL guard does for an unattributable
        # query: a read we cannot pin to a permitted label is a parser gap,
        # and every gap so far has been a silent allow. The exception is a
        # statement whose only source is an allowlisted catalog procedure
        # (`CALL db.labels()`), which returns schema metadata and no node data.
        raise QueryRejected("Query must read at least one permitted label")
    return cleaned


def enforce_limit(cypher, max_rows=500):
    """Append a LIMIT when the final RETURN has none.

    Only a LIMIT after the last top-level RETURN bounds the result: `WITH n
    LIMIT 5 MATCH (n)-->(m) RETURN m` caps an intermediate step and can still
    return millions of rows. Runs on tokens, so a trailing `// note` cannot
    swallow the appended clause and a `/* LIMIT 1 */` cannot pass for one.
    """
    try:
        toks, cleaned = _tokens((cypher or "").strip().rstrip(";").strip())
    except QueryRejected:               # malformed: validate() already refused it
        return cypher
    depth, last_return, tail_limit = 0, -1, False
    for i, (kind, text) in enumerate(toks):
        if kind == "punct":
            depth += 1 if text in _OPEN else -1 if text in _CLOSE else 0
            continue
        if kind == "word" and depth == 0:
            word = text.lower()
            if word == "return":
                last_return, tail_limit = i, False
            elif word == "limit" and last_return >= 0:
                tail_limit = True
    if tail_limit:
        return cleaned
    return f"{cleaned} LIMIT {max_rows}"
