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


def _label_refs(toks):
    """[(name, is_relationship_type)] for every `:Label` outside a map literal.

    Brace depth separates a label from a property key: in `(n:P {name: 'x'})`
    the second colon is a map key. Bracket depth separates a node label from a
    relationship type: `[r:KNOWS]` is an edge, `(n:Person)` is a node.
    """
    out, brace, bracket, i, n = [], 0, 0, 0, len(toks)
    while i < n:
        kind, text = toks[i]
        if kind == "punct":
            if text == "{":
                brace += 1
            elif text == "}":
                brace -= 1
            elif text == "[":
                bracket += 1
            elif text == "]":
                bracket -= 1
            elif text == ":" and brace <= 0:
                j = i + 1
                while j < n:
                    if toks[j] == ("punct", "!"):      # `:!Excluded`
                        j += 1
                        continue
                    if toks[j][0] not in ("word", "ident"):
                        break
                    out.append((toks[j][1].lower(), bracket > 0))
                    j += 1
                    if j < n and toks[j][0] == "punct" and toks[j][1] in "|&:":
                        j += 1                          # `:A|B`, `:A&B`
                        continue
                    break
                i = j
                continue
        i += 1
    return out


def _match_regions(toks):
    """[(start, end)] token ranges holding the pattern of each MATCH clause.

    A region ends at the next clause keyword at depth 0 — or at the bracket
    that CLOSES the group the MATCH lives in (`WHERE EXISTS { MATCH (x:P) }`),
    which is why an unbalanced close ends it too. Without that, the region ran
    on into the enclosing statement and `count(*)` looked like a node pattern.
    """
    regions, i, n = [], 0, len(toks)
    while i < n:
        if toks[i][0] == "word" and toks[i][1].lower() == "match":
            j, depth = i + 1, 0
            while j < n:
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
            regions.append((i + 1, j))
            i = j
            continue
        i += 1
    return regions


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


def _check_node_group(toks, lo, hi, bound):
    """A node pattern must be pinned to a label, and records what it binds.

    `MATCH (n) RETURN n` streams the whole graph and `(a:Person)-[]->(x)`
    returns whatever x happens to be, so an unlabelled node is a read no
    allowlist entry can authorize. Two exemptions, both still bounded:

      * a group that only WRAPS other groups — `shortestPath((a:A)-[*]-(b:B))`
        — is recursed into, so the wrapper cannot launder an unlabelled node;
      * a bare `(n)` re-using a variable ALREADY bound by a labelled pattern
        (`MATCH (n:Person) WITH n LIMIT 5 MATCH (n)-->(m:Company)`) is the
        ordinary way to continue a traversal, and n is already restricted to
        allowed-label nodes. A fresh alias (`WITH n AS m MATCH (m)`) is not
        exempt: only the exact variable the label pinned.
    """
    variable, has_label, nested = _group_parts(toks, lo, hi)
    if has_label:
        if variable:
            bound.add(variable)
        return
    if nested:
        for sub_lo, sub_hi in nested:
            _check_node_group(toks, sub_lo, sub_hi, bound)
        return
    if variable and variable in bound:
        return
    raise QueryRejected(
        "Every node in a MATCH must carry a label (an unlabelled node reads "
        "the whole graph)")


def _check_patterns(toks):
    """Left to right across every MATCH, so a variable is only ever exempt
    AFTER the labelled pattern that bound it."""
    bound = set()
    for lo, hi in _match_regions(toks):
        i = lo
        while i < hi:
            if toks[i] == ("punct", "("):
                close = _match_close(toks, i, hi)
                _check_node_group(toks, i + 1, close, bound)
                i = close + 1
                continue
            i += 1


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
