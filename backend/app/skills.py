"""Per-source agent skill files.

Every database gets its own agent, and every agent gets a skill file: a
markdown briefing describing the source, its SQL dialect, and exactly the
tables the current user's role may access, with column schemas and
dialect-specific query guidance.

Files live in backend/skills/<source>/<role>.md and carry a fingerprint of
(dialect, allowed tables, schemas). When the warehouse schema changes or the
role's access changes (rbac.POLICIES / future AAD-group mapping), the
fingerprint stops matching and the skill is rebuilt automatically — no manual
upkeep. The file on disk is a cache; a read-only filesystem just means the
skill is rebuilt per request.
"""
import hashlib
import json
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"

# Dialect-specific guidance injected into each skill file.
_DIALECT_NOTES = {
    "sqlite": "- Time bucketing: strftime('%Y-%m', date_col). No DATE_TRUNC.",
    "snowflake": "- Time bucketing: DATE_TRUNC('month', date_col). Identifiers fold to UPPERCASE unless quoted.",
    "databricks": "- Time bucketing: date_trunc('month', date_col). Spark SQL dialect.",
}


def fingerprint(dialect, allowed_tables, schemas):
    payload = json.dumps(
        {
            "dialect": dialect,
            "tables": sorted(allowed_tables),
            "schemas": {t: schemas.get(t, []) for t in sorted(schemas)},
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def get_skill(connector, role, allowed_tables, schemas):
    """Skill markdown for (source, role) — served from cache while the
    fingerprint matches, rebuilt the moment schema or access changes."""
    fp = fingerprint(connector.dialect, allowed_tables, schemas)
    path = SKILLS_DIR / connector.name / f"{role}.md"
    try:
        cached = path.read_text()
        if f"fingerprint: {fp}" in cached:
            return cached
    except OSError:
        pass
    text = _build(connector, role, allowed_tables, schemas, fp)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError:
        pass
    return text


def _build(connector, role, allowed_tables, schemas, fp):
    table_lines = []
    for t in allowed_tables:
        cols = schemas.get(t)
        if cols:
            col_text = "\n".join(f"  - {c['name']} ({c['type']})" for c in cols)
            table_lines.append(f"### {t}\n{col_text}")
        else:
            table_lines.append(f"### {t}\n  (schema not loaded — inspect before use)")
    tables_md = "\n\n".join(table_lines) or "(no accessible tables)"
    dialect_note = _DIALECT_NOTES.get(connector.dialect, "- Standard ANSI SQL.")
    summary = ", ".join(allowed_tables[:8]) + ("…" if len(allowed_tables) > 8 else "")
    return f"""---
name: {connector.name}-{role}
description: Query the {connector.name} source ({connector.dialect}) as role "{role}" — tables: {summary}
fingerprint: {fp}
---

# {connector.name} — database agent skill (role: {role})

SQL dialect: {connector.dialect}

Access policy: this role may query ONLY the tables listed below. They are the
complete, RBAC-filtered view for this user — anything else in the warehouse
is invisible and queries touching it are rejected by the query guard.

## Accessible tables

{tables_md}

## Query guidance
{dialect_note}
- Aggregate in SQL (GROUP BY, filters); the warehouse may hold terabytes.
- Single SELECT statements only; every query gets an enforced LIMIT.
- Join only within this source. Cross-database questions are the
  orchestrator's job, not yours.
"""
