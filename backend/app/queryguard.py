"""Guardrails for agent-generated SQL: read-only, single statement, RBAC tables.

Every rule runs over SQL *tokens*, never over the raw string. A regex cannot
see that `FROM/**/customers`, `FROM "customers"`, `FROM sales, customers` and
`FROM 's3://bucket/x.parquet'` are all data sources, and it cannot see that the
`create` in `WHERE reason = 'create'` is not DDL. Both mistakes were real: the
first let denied tables reach the warehouse, the second rejected valid
analytics SQL. Tokenizing fixes both at once.

validate() returns the COMMENT-FREE SQL and callers must execute that string —
validating one text and running another is how `FROM/**/customers` got through.

Table names are matched on their BARE name (that is what RBAC and governance
key on) but a qualifier is no longer discarded: `secret_schema.sales` is not
`sales`, and the gateway passes the connector's own namespace so a reference
outside it is refused rather than silently reduced to an allowed name.

Identity is DIALECT-AWARE and preserves quotedness. `"CUSTOMERS"` and
`customers` are two different tables on PostgreSQL and Snowflake, and folding
both to `customers` let a quoted CTE stand in for a denied base table (see
_canon). The gateway passes connector.dialect; dialect=None keeps the old
case-insensitive reading for callers that have no connector in hand.
"""
import collections
import re

# Write/DDL keywords, plus the extension/config statements a file-reading
# engine (DuckDB) would need. Matched against individual identifier tokens, so
# `created_at`, `'create'` and `"copy"` no longer trip it. Other modules
# (supervisor, flow) reuse this as a write detector over raw text.
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|merge|copy|"
    r"call|execute|attach|detach|pragma|vacuum|replace|install|load|set|export|"
    r"import|into)\b",
    re.IGNORECASE,
)

# Lexical helper only — NOT the security boundary; validate() resolves refs
# from tokens. Kept because governance, router, pipelines and chat read table
# names out of SQL with it, and widened (quoted/backticked/bracketed names, a
# comment instead of a space) so those callers stop silently seeing zero tables
# for `FROM "customers"` — that is what made PII masking skippable.
#      `\s` one at a time and the unrolled `/*...*/` body keep this linear:
#      `(?:\s+|/\*.*?\*/)*` backtracks catastrophically on `/*a*//*a*/...`.
TABLE_REF = re.compile(
    r"\b(?:from|join)\b(?:\s|/\*[^*]*(?:\*(?!/)[^*]*)*\*/)*[\"`\[]?"
    r"([a-zA-Z_][\w$-]*(?:[\"`\]]?\.[\"`\[]?[a-zA-Z_][\w$-]*)*)",
    re.IGNORECASE,
)

# Functions that read outside the warehouse: object storage, the local
# filesystem, another database, or the engine's own settings/secrets. Rejected
# wherever they appear, because `SELECT read_text('/etc/passwd') FROM sales`
# has a perfectly legal FROM clause.
EXTERNAL_FN = re.compile(
    r"read_\w+|\w*_scan|\w+_scanner|parquet_\w+|glob|sniff_csv|query|query_table|"
    r"current_setting|duckdb_\w+|which_secret|load_aws_credentials|copy_dir|write_blob|"
    r"iceberg_\w+|delta_\w+|postgres_\w+|mysql_\w+|sqlite_\w+|st_read\w*|"
    r"json_execute_serialized_sql|python_map_function|arrow_scan\w*",
    re.IGNORECASE,
)

_WS = " \t\r\n\f\v"
_QUOTES = {'"': '"', "`": "`", "[": "]"}
# A FROM/JOIN target starting with one of these is a subquery or an inline row
# source, not a name to resolve.
_SUBQUERY_HEAD = {"select", "with", "values", "table", "lateral", "unnest"}
# Keywords that end a FROM list, so a later comma is not another table.
_FROM_STOP = {"where", "group", "having", "order", "limit", "offset", "fetch",
              "union", "intersect", "except", "window", "qualify", "into"}
# Forbidden words that are also ordinary functions in every dialect we target.
# Safe in call position: no DDL/DML form puts `(` straight after them.
_FN_EXEMPT = {"replace", "truncate"}


class QueryRejected(Exception):
    pass


# ── Tokenizer ───────────────────────────────────────────────────────────

def _tokens(sql):
    """(kind, text) tokens with comments removed, plus the comment-free SQL.

    Whitespace and comments produce no token, so `FROM/**/t` and `FROM  t` are
    indistinguishable to the caller. Original spacing is preserved in the
    returned SQL — only comments are replaced (by a space, which is why
    `FROM/**/t` stays valid). Raises on an unterminated literal or comment
    rather than guessing where it ends.
    """
    toks, out, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in _WS:
            j = i
            while j < n and sql[j] in _WS:
                j += 1
            out.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise QueryRejected("Unterminated comment in query")
            i = j + 2
            out.append(" ")
        elif ch == "'":
            j = i + 1
            while True:
                j = sql.find("'", j)
                if j < 0:
                    raise QueryRejected("Unterminated string literal in query")
                if sql.startswith("''", j):
                    j += 2
                    continue
                break
            toks.append(("str", sql[i:j + 1]))
            out.append(sql[i:j + 1])
            i = j + 1
        elif ch in _QUOTES:
            close = _QUOTES[ch]
            j = i + 1
            while True:
                j = sql.find(close, j)
                if j < 0:
                    raise QueryRejected("Unterminated quoted identifier in query")
                if close == '"' and sql.startswith('""', j):
                    j += 2
                    continue
                break
            toks.append(("ident", sql[i + 1:j].replace('""', '"')))
            out.append(sql[i:j + 1])
            i = j + 1
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"):
                j += 1
            toks.append(("word", sql[i:j]))
            out.append(sql[i:j])
            i = j
        elif ch.isdigit():
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            toks.append(("num", sql[i:j]))
            out.append(sql[i:j])
            i = j
        else:
            toks.append(("punct", ch))
            out.append(ch)
            i += 1
    return toks, "".join(out).strip()


def _skip_parens(toks, i):
    """Index just past a balanced (...) starting at `i`, or `i` if none."""
    n = len(toks)
    if i >= n or toks[i] != ("punct", "("):
        return i
    depth = 0
    while i < n:
        if toks[i] == ("punct", "("):
            depth += 1
        elif toks[i] == ("punct", ")"):
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i


# ── Identifier identity ─────────────────────────────────────────────────

# A reference part exactly as WRITTEN: its text plus whether it arrived quoted.
# The tokenizer already knows — ("ident", …) for `"x"` / `x` / [x], ("word", …)
# for a bare x — and discarding that collapsed two different tables into one
# name. PostgreSQL folds a BARE identifier to lower case and keeps a QUOTED one
# verbatim, so with only `sales` allowed
#     WITH "CUSTOMERS" AS (SELECT * FROM sales) SELECT * FROM customers
# binds a CTE named CUSTOMERS while the outer reference resolves to the DENIED
# base relation customers — a guard that lowercased both believed the reference
# was the CTE. Invariant: two identifiers denote the same object only when
# their canonical forms (below) are equal.
_Ident = collections.namedtuple("_Ident", "text quoted")

# How an engine reads an identifier written BARE — listed only for the engines
# that do NOT also fold a quoted one. Every other dialect we target (SQLite,
# DuckDB, Databricks/Spark) is case-insensitive for quoted names as well, so
# both readings fold to lower there; that is also the historical behaviour and
# is what dialect=None keeps.
_BARE_FOLD = {
    "postgres": str.lower,      # bare folds DOWN, quoted is exact
    "snowflake": str.upper,     # bare folds UP, quoted is exact
    "bigquery": lambda t: t,    # dataset/table ids are case-sensitive as written
}


def _canon(ident, dialect=None):
    """The identity the ENGINE would resolve this written identifier to."""
    fold = _BARE_FOLD.get((dialect or "").lower())
    if fold is None:
        return ident.text.lower()               # case-insensitive engine (or unknown)
    return ident.text if ident.quoted else fold(ident.text)


def _catalog_canon(name, dialect=None):
    """Identity of a name the CATALOG spelled — an RBAC allowlist entry, which
    comes from connector.list_tables(). The catalog reports the STORED name, so
    it is already exact: it gets the same reading as a quoted reference (a
    Snowflake catalog says SALES, which a bare `sales` reaches and a quoted
    `"sales"` does not)."""
    return _canon(_Ident(str(name).strip('"').strip("`"), True), dialect)


def _qualified_name(toks, i):
    """`db.schema."Tbl"` -> ([_Ident(db), _Ident(schema), _Ident(Tbl)], index after).

    Parts are returned as WRITTEN (text + quotedness); the caller canonicalizes
    them for the connector's dialect. The last part is the RBAC key: every
    allowlist and every governance rule is keyed on bare names. But the
    QUALIFIER is returned alongside it, because dropping it was a hole: an
    allowlist containing `sales` also accepted `secret_schema.sales`, so a
    service account whose warehouse credentials can see a second schema/catalog
    read straight past the catalog/RBAC boundary (rbac.allowed_tables only ever
    lists the connector's configured namespace). validate() checks the
    qualifier against the connector's own namespace when the caller passes one;
    see `qualifiers` there.
    """
    parts, n = [], len(toks)
    while i < n and toks[i][0] in ("word", "ident"):
        parts.append(_Ident(toks[i][1], toks[i][0] == "ident"))
        i += 1
        if i < n and toks[i] == ("punct", "."):
            i += 1
            continue
        break
    if not parts:
        raise QueryRejected("Unreadable table reference after FROM/JOIN")
    if any(c in p.text for p in parts for c in "/:\\"):
        # `JOIN "s3://bucket/x.parquet"` — a path wearing an identifier's
        # quotes. Checked on every part, not just the last: the qualifier is a
        # path too in `"s3://bucket"."x.parquet"`.
        raise QueryRejected("A file or URI is not a permitted data source")
    return parts, i


def _read_ref(toks, i, depth, refs):
    n = len(toks)
    while i < n and toks[i] == ("punct", "("):
        depth += 1
        i += 1
    if i < n and toks[i][0] == "str":
        # `FROM 's3://bucket/x.parquet'` is a replacement scan: a file read with
        # no table name anywhere in the statement. No dialect we target accepts
        # a string literal as a table, so this is safe to reject outright.
        raise QueryRejected("A file or URI is not a permitted data source")
    if i < n and toks[i][0] == "word" and toks[i][1].lower() in _SUBQUERY_HEAD:
        return i, depth
    parts, j = _qualified_name(toks, i)
    refs.append((parts, i))
    return j, depth


def _table_refs(toks):
    """[(parts, token index)] for every FROM/JOIN target, comma-joined arms
    included — `FROM sales, customers` hid its second arm from the regex.
    `parts` is the dotted reference as written (see _qualified_name): one
    _Ident for an unqualified name, more when it carries a qualifier."""
    refs, i, n, depth, from_depth = [], 0, len(toks), 0, None
    while i < n:
        kind, text = toks[i]
        if kind == "punct":
            if text == "(":
                depth += 1
            elif text == ")":
                depth -= 1
                if from_depth is not None and depth < from_depth:
                    from_depth = None
            elif text == "," and from_depth is not None and depth == from_depth:
                i, depth = _read_ref(toks, i + 1, depth, refs)
                continue
            i += 1
            continue
        if kind == "word":
            word = text.lower()
            if from_depth is not None and depth == from_depth and word in _FROM_STOP:
                from_depth = None
            if word in ("from", "join"):
                start = depth
                i, depth = _read_ref(toks, i + 1, depth, refs)
                if word == "from":
                    from_depth = start
                continue
        i += 1
    return refs


def _paren_matches(toks):
    """{open index: close index} for every balanced paren pair."""
    match, stack = {}, []
    for i, t in enumerate(toks):
        if t == ("punct", "("):
            stack.append(i)
        elif t == ("punct", ")") and stack:
            match[stack.pop()] = i
    return match


def _cte_bindings(toks):
    """[(name, at, scope_end, body_open, body_close, recursive)] for every WITH
    clause, nested ones too.

    Anchored on the WITH keyword, because any `x AS (` pair used to count: a
    window definition (`WINDOW w AS (...), customers AS (...)`) or a forged one
    inside a comment both minted a fake CTE and unlocked a denied table.

    Scope matters as much as order. A CTE bound INSIDE a subquery or another
    CTE's body is invisible outside that paren group, yet the engine resolves
    an outer same-named ref to the real base table — counting bindings globally
    was a proven RBAC bypass (`WITH t AS (WITH customers AS (SELECT 1) SELECT 1)
    SELECT * FROM customers` read the denied table). So a binding legalizes
    refs only between its own position and the END OF ITS ENCLOSING PAREN
    GROUP. Its own body is excluded unless RECURSIVE: under non-recursive
    scoping (DuckDB/Postgres) `WITH customers AS (SELECT * FROM customers)`
    resolves the inner ref to the BASE table, not the CTE.
    """
    match = _paren_matches(toks)
    out, stack, n = [], [], len(toks)
    for idx, tok in enumerate(toks):
        if tok == ("punct", "("):
            stack.append(idx)
            continue
        if tok == ("punct", ")"):
            if stack:
                stack.pop()
            continue
        if tok[0] != "word" or tok[1].lower() != "with":
            continue
        scope_end = match.get(stack[-1], n) if stack else n
        i = idx + 1
        recursive = i < n and toks[i][0] == "word" and toks[i][1].lower() == "recursive"
        if recursive:
            i += 1
        while i < n and toks[i][0] in ("word", "ident"):
            name, at = _Ident(toks[i][1], toks[i][0] == "ident"), i
            i = _skip_parens(toks, i + 1)          # optional (col, col) list
            if not (i < n and toks[i][0] == "word" and toks[i][1].lower() == "as"):
                break
            i += 1
            while i < n and toks[i][0] == "word" and toks[i][1].lower() in ("not", "materialized"):
                i += 1                              # AS [NOT] MATERIALIZED (...)
            if not (i < n and toks[i] == ("punct", "(")):
                break
            out.append((name, at, scope_end, i, match.get(i, n), recursive))
            i = _skip_parens(toks, i)
            if i < n and toks[i] == ("punct", ","):
                i += 1
                continue
            break
    return out


def _cte_legal(bindings, canon, at, dialect=None):
    """Is the ref whose canonical identity is `canon`, at token index `at`,
    resolved by a CTE the ENGINE would also resolve it to? Bound earlier, still
    in scope, and not a non-recursive self-reference (which the engine sends to
    the base table).

    Names are compared CANONICALLY, so a quoted CTE cannot shadow an unquoted
    base table (or the reverse): on PostgreSQL the CTE `"CUSTOMERS"` does not
    answer a bare `customers`, and the guard must not pretend it does."""
    for bident, bat, scope_end, body_open, body_close, recursive in bindings:
        if _canon(bident, dialect) != canon or not (bat < at < scope_end):
            continue
        if body_open < at < body_close and not recursive:
            continue
        return True
    return False


# ── Public API ──────────────────────────────────────────────────────────

def _declared_qualifiers(qualifiers, dialect=None):
    """Connector.qualifiers() grouped BY ARITY: {n_parts: {canonical prefix}}.

    Arity is the question, not just the text. A one-part prefix and a two-part
    prefix mean different things to the engine — Snowflake reads `x.sales` as
    SCHEMA.object and `a.b.sales` as DATABASE.SCHEMA.object — so a connector
    that accepts a database name only in the two-part position must not have it
    matched in the one-part position. Grouping makes that impossible to get
    wrong by accident and lets the refusal say which arities exist.

    A declared prefix is read as a CATALOG spelling — the STORED name of the
    namespace, canonicalized exactly like an allowlist entry (_catalog_canon).
    That is what each connector reports: it is the spelling its own
    list_tables() queries the catalog with, and (on PostgreSQL) the spelling
    its search_path is pinned to. Each connector is responsible for applying
    its vendor's folding to the raw env value before declaring it — Snowflake
    stores an unquoted `public` as PUBLIC, so snowflake_conn declares PUBLIC.

    Admitting BOTH readings (bare-folded and quoted-verbatim) instead was a
    fail-open: with POSTGRES_SCHEMA=Analytics — a quote-created schema, which
    is exactly how list_tables and the search_path pin read it — a folded
    `analytics` was also admitted, so `analytics.sales` passed the namespace
    check while PostgreSQL resolved it to a DIFFERENT schema the catalog had
    never described. One spelling in, one identity out.
    """
    if qualifiers is None:
        return None
    by_arity = {}
    for q in qualifiers:
        parts = [p.strip('"').strip("`") for p in str(q).split(".")]
        parts = [p for p in parts if p]
        if not parts:
            continue
        by_arity.setdefault(len(parts), set()).add(
            ".".join(_catalog_canon(p, dialect) for p in parts))
    return by_arity


def _qualifier_ok(canon_parts, by_arity):
    """Is this canonicalized prefix one of the namespaces this connector was
    configured with, AT THIS ARITY?

    Whole-prefix comparison only, deliberately. Accepting a qualifier because
    its LAST part matches would re-open the hole this check exists to close:
    with a configured schema of `public`, `other_db.public.sales` would pass
    while reading another database's PUBLIC schema. Connectors therefore
    declare every spelling they accept (schema, database.schema, ...) in
    Connector.qualifiers(), built from the same env they connect with.
    """
    return ".".join(canon_parts) in by_arity.get(len(canon_parts), ())


def validate(sql, allowed_tables, qualifiers=None, dialect=None):
    """Raise QueryRejected unless `sql` is a single SELECT touching only
    allowed tables. Returns the comment-free SQL the caller must execute.

    `qualifiers` is the connector's own namespace — the prefixes a table
    reference may carry, each declared at the ARITY the engine gives it
    (Connector.qualifiers(); the gateway passes it), matched canonically. When given, `secret_schema.sales` is refused even though the
    bare name `sales` is allowed, because RBAC's allowlist only ever describes
    the CONFIGURED namespace and a warehouse credential usually sees more than
    that. An unqualified name is always fine, an empty set means "no qualifier
    at all" (sqlite/DuckDB/API sources), and None keeps the historical
    behaviour of not looking at qualifiers — the default so that callers
    without a connector in hand (blend, dashboards) are unaffected.

    `dialect` is the connector's dialect (the gateway passes it) and decides
    how identifiers FOLD: what `"CUSTOMERS"` and `customers` mean is a property
    of the engine, not of the guard. None keeps the historical
    case-insensitive-everything reading, so blend and dashboards are unaffected.
    """
    quals = _declared_qualifiers(qualifiers, dialect)
    raw = (sql or "").strip().rstrip(";").strip()
    toks, cleaned = _tokens(raw)
    if not toks:
        raise QueryRejected("Empty query")

    # Leading parens are legal: `(SELECT ...) UNION (SELECT ...)`.
    head = next((t for t in toks if t != ("punct", "(")), None)
    if not head or head[0] != "word" or head[1].lower() not in ("select", "with"):
        raise QueryRejected("Only SELECT queries are allowed")

    for i, (kind, text) in enumerate(toks):
        if kind == "punct":
            if text == ";":     # a ';' inside a literal or comment is not one
                raise QueryRejected("Only a single statement is allowed")
            continue
        if kind not in ("word", "ident"):
            continue
        call = i + 1 < len(toks) and toks[i + 1] == ("punct", "(")
        if call and EXTERNAL_FN.fullmatch(text):
            # Quoted too: "read_text"('/etc/passwd') is the same function.
            raise QueryRejected(f"Function '{text}' reads outside the warehouse")
        if kind == "ident":
            continue                                # a quoted name is never a keyword
        if i and toks[i - 1] == ("punct", "."):
            continue                                # `s.copy` is a column
        if call and text.lower() in _FN_EXEMPT:
            continue                                # replace()/truncate() are functions
        if FORBIDDEN.fullmatch(text):
            raise QueryRejected("Query contains a forbidden keyword (read-only access)")

    # Allowlist entries are CATALOG spellings, so they are canonicalized the
    # exact way a reference is — never a naive .lower(), which would let a
    # quoted `"sales"` pass for a Snowflake table stored as SALES.
    allowed = {_catalog_canon(t, dialect) for t in allowed_tables}
    ctes = _cte_bindings(toks)
    refs = _table_refs(toks)
    for parts, at in refs:
        name = _canon(parts[-1], dialect)
        qual_parts = [_canon(p, dialect) for p in parts[:-1]]
        written = ".".join(p.text for p in parts)
        if quals is not None and qual_parts:
            if not _qualifier_ok(qual_parts, quals):
                raise QueryRejected(
                    f"Table '{written}' is outside the configured "
                    f"namespace for this source")
            # A qualified reference names a real table in that namespace, never
            # a CTE (CTE names are bare), so it must be on the allowlist itself
            # — otherwise `WITH sales AS (...) SELECT * FROM other.sales` would
            # launder the CTE's permission onto the base table.
            if name not in allowed:
                raise QueryRejected(
                    f"Access to table '{parts[-1].text}' is not permitted for your role")
            continue
        # A CTE legalizes a ref only where the ENGINE would resolve it to that
        # CTE: bound earlier, within the binding's paren scope, and never a
        # non-recursive self-reference. Order-only tracking let an inner-scoped
        # binding launder an outer ref to a denied base table.
        if name in allowed or _cte_legal(ctes, name, at, dialect):
            continue
        raise QueryRejected(
            f"Access to table '{parts[-1].text}' is not permitted for your role")
    if not refs:
        # Fail closed: a query we cannot attribute to a permitted table is a
        # parser gap, and every gap so far has been a silent allow.
        raise QueryRejected("Query must read at least one permitted table")
    return cleaned


def enforce_limit(sql, max_rows=500):
    """Append a LIMIT when the query has none (all three dialects accept LIMIT).

    Runs on tokens: a trailing `-- note` would otherwise swallow the appended
    LIMIT, and a `/* limit 1 */` or `'limit 1'` would otherwise pass for one.
    """
    try:
        toks, cleaned = _tokens((sql or "").strip().rstrip(";").strip())
    except QueryRejected:                       # malformed: leave it to the engine
        if re.search(r"\blimit\s+\d+", sql or "", re.IGNORECASE):
            return sql
        return f"{sql} LIMIT {max_rows}"
    depth = 0
    for kind, text in toks:
        if kind == "punct":
            depth += 1 if text == "(" else -1 if text == ")" else 0
        elif kind == "word" and depth == 0 and text.lower() == "limit":
            return cleaned                      # a LIMIT inside a CTE bounds nothing
    return f"{cleaned} LIMIT {max_rows}"


def base_tables(sql):
    """Lowercased names of every FROM/JOIN target that is NOT resolved by a CTE
    binding — the real tables a query reads. BARE names, qualifier dropped:
    governance and chat key their rules on bare names, and validate() is where
    the qualifier is checked against the connector's namespace. Purely lexical
    (no warehouse access), so chat can re-check a stored message against the
    reader's role at read time. Returns an empty set for empty or unparseable
    input rather than raising: callers treat "no attributable table" as
    fail-closed."""
    try:
        toks, _ = _tokens((sql or "").strip().rstrip(";").strip())
        ctes = _cte_bindings(toks)
        # Dialect-free (case-insensitive) on purpose: governance and chat key
        # their rules on lowercase bare names and have no connector in hand.
        return {_canon(parts[-1]) for parts, at in _table_refs(toks)
                if not _cte_legal(ctes, _canon(parts[-1]), at)}
    except QueryRejected:
        return set()
