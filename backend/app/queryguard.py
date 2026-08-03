"""Guardrails for agent-generated SQL: read-only, single statement, RBAC tables."""
import re

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|merge|copy|"
    r"call|execute|attach|detach|pragma|vacuum|replace)\b",
    re.IGNORECASE,
)
TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w.\"]*)", re.IGNORECASE)


class QueryRejected(Exception):
    pass


def validate(sql, allowed_tables):
    """Raise QueryRejected unless `sql` is a single SELECT touching only allowed tables."""
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise QueryRejected("Only a single statement is allowed")
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        raise QueryRejected("Only SELECT queries are allowed")
    if FORBIDDEN.search(s):
        raise QueryRejected("Query contains a forbidden keyword (read-only access)")

    allowed = {t.lower() for t in allowed_tables}
    # Names introduced by CTEs are legal to reference even though they aren't tables.
    ctes = {m.lower() for m in re.findall(r"(?:with|,)\s*([a-zA-Z_]\w*)\s+as\s*\(", s, re.IGNORECASE)}
    for ref in TABLE_REF.findall(s):
        # Ignore subquery openers; normalize quoting and schema prefixes.
        name = ref.strip('"').split(".")[-1].lower()
        if not name or name.startswith("("):
            continue
        if name not in allowed and name not in ctes:
            raise QueryRejected(f"Access to table '{name}' is not permitted for your role")
    return s


def enforce_limit(sql, max_rows=500):
    """Append a LIMIT when the query has none (all three dialects accept LIMIT)."""
    if re.search(r"\blimit\s+\d+", sql, re.IGNORECASE):
        return sql
    return f"{sql} LIMIT {max_rows}"
