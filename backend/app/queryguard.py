"""Guardrails for agent-generated SQL: read-only, single statement, RBAC tables.

Every rule runs over SQL *tokens*, never over the raw string. A regex cannot
see that `FROM/**/customers`, `FROM "customers"`, `FROM sales, customers` and
`FROM 's3://bucket/x.parquet'` are all data sources, and it cannot see that the
`create` in `WHERE reason = 'create'` is not DDL. Both mistakes were real: the
first let denied tables reach the warehouse, the second rejected valid
analytics SQL. Tokenizing fixes both at once.

validate() returns the COMMENT-FREE SQL and callers must execute that string —
validating one text and running another is how `FROM/**/customers` got through.
"""
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


def _qualified_name(toks, i):
    """`db.schema."Tbl"` -> ('tbl', index after it). The bare name is what RBAC
    keys on, so only the last part matters."""
    parts, n = [], len(toks)
    while i < n and toks[i][0] in ("word", "ident"):
        parts.append(toks[i][1])
        i += 1
        if i < n and toks[i] == ("punct", "."):
            i += 1
            continue
        break
    if not parts:
        raise QueryRejected("Unreadable table reference after FROM/JOIN")
    name = parts[-1].strip('"').lower()
    if any(c in name for c in "/:\\"):
        # `JOIN "s3://bucket/x.parquet"` — a path wearing an identifier's quotes.
        raise QueryRejected("A file or URI is not a permitted data source")
    return name, i


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
    name, j = _qualified_name(toks, i)
    refs.append((name, i))
    return j, depth


def _table_refs(toks):
    """[(name, token index)] for every FROM/JOIN target, comma-joined arms
    included — `FROM sales, customers` hid its second arm from the regex."""
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
            name, at = toks[i][1].strip('"').lower(), i
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


def _cte_legal(bindings, name, at):
    """Is the ref at token index `at` resolved by a CTE the ENGINE would also
    resolve it to? Bound earlier, still in scope, and not a non-recursive
    self-reference (which the engine sends to the base table)."""
    for bname, bat, scope_end, body_open, body_close, recursive in bindings:
        if bname != name or not (bat < at < scope_end):
            continue
        if body_open < at < body_close and not recursive:
            continue
        return True
    return False


# ── Public API ──────────────────────────────────────────────────────────

def validate(sql, allowed_tables):
    """Raise QueryRejected unless `sql` is a single SELECT touching only
    allowed tables. Returns the comment-free SQL the caller must execute."""
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

    allowed = {str(t).strip('"').lower() for t in allowed_tables}
    ctes = _cte_bindings(toks)
    refs = _table_refs(toks)
    for name, at in refs:
        # A CTE legalizes a ref only where the ENGINE would resolve it to that
        # CTE: bound earlier, within the binding's paren scope, and never a
        # non-recursive self-reference. Order-only tracking let an inner-scoped
        # binding launder an outer ref to a denied base table.
        if name in allowed or _cte_legal(ctes, name, at):
            continue
        raise QueryRejected(f"Access to table '{name}' is not permitted for your role")
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
