"""Governance-as-code — one YAML that owns access and compliance.

Global env-var connections and a hardcoded RBAC dict don't scale: not every
user should reach every source, and compliance rules (PII masking, forbidden
columns, row caps) belong in one auditable place, not scattered through code.

This module loads a single governance document — per-role source/table access
plus per-table compliance — and makes it the source of truth. RBAC delegates
to it; the query path enforces its compliance rules on every result. An admin
edits the YAML in-app; changes hot-reload with no redeploy. When no document
is loaded, Studio falls back to the built-in rbac.POLICIES, so nothing breaks.

Document shape:

    version: 1
    roles:
      admin:   { sources: "*" }            # every source, every table
      analyst:
        sources:
          demo: "*"                        # all tables in demo
          snowflake: [sales, orders]       # only these
      viewer:
        sources:
          demo: [sales, web_traffic]
    compliance:
      defaults: { max_rows: 50000 }
      demo:
        customers:
          deny_columns: [name, city]       # stripped from every result
          mask_columns: [lifetime_value]   # returned as ***
          max_rows: 5000
"""
import time

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db

router = APIRouter(prefix="/governance", tags=["governance"])

_STATE = {"doc": None, "yaml": "", "source": None}  # in-process cache of the active doc


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS governance_config (
            id INTEGER PRIMARY KEY,
            yaml TEXT NOT NULL,
            applied_by TEXT,
            applied_at REAL NOT NULL
        );
        """
    )
    c.commit()
    c.close()


def load():
    """Load the active document: DB (admin-applied) first, else STUDIO_GOVERNANCE
    file, else none (Studio uses built-in RBAC). Cached; call reload() to refresh."""
    import os

    c = db._conn()
    row = c.execute("SELECT yaml FROM governance_config ORDER BY applied_at DESC LIMIT 1").fetchone()
    c.close()
    if row and row["yaml"].strip():
        _set(row["yaml"], "database")
        return
    path = os.getenv("STUDIO_GOVERNANCE")
    if path and os.path.isfile(path):
        with open(path) as f:
            _set(f.read(), f"file:{path}")
        return
    _STATE.update(doc=None, yaml="", source=None)


def _set(text, source):
    ok, errors, doc = validate(text)
    if ok:
        _STATE.update(doc=doc, yaml=text, source=source)
    else:
        # A malformed stored doc must fail closed to built-in RBAC, not crash.
        _STATE.update(doc=None, yaml=text, source=f"{source} (invalid: {errors[0] if errors else '?'})")


def loaded():
    return _STATE["doc"] is not None


def current_yaml():
    return _STATE["yaml"]


def active_source():
    return _STATE["source"]


# ── Validation ──────────────────────────────────────────────────────────

def validate(text):
    """(ok, errors, doc). Parses and structurally checks a governance YAML."""
    errors = []
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return False, [f"YAML parse error: {e}"], None
    if not isinstance(raw, dict):
        return False, ["Top level must be a mapping"], None

    roles = raw.get("roles")
    if not isinstance(roles, dict) or not roles:
        errors.append("`roles` must be a non-empty mapping")
        roles = {}
    norm_roles = {}
    for role, spec in roles.items():
        if not isinstance(spec, dict) or "sources" not in spec:
            errors.append(f"role '{role}' must have a `sources` entry")
            continue
        src = spec["sources"]
        if src == "*":
            norm_roles[role] = "*"
            continue
        if not isinstance(src, dict):
            errors.append(f"role '{role}'.sources must be '*' or a mapping")
            continue
        norm_src = {}
        for source, tables in src.items():
            if tables == "*":
                norm_src[source] = "*"
            elif isinstance(tables, list):
                norm_src[source] = {str(t) for t in tables}
            else:
                errors.append(f"role '{role}'.sources.{source} must be '*' or a list")
        norm_roles[role] = norm_src

    comp = raw.get("compliance") or {}
    norm_comp = {}
    if not isinstance(comp, dict):
        errors.append("`compliance` must be a mapping")
        comp = {}
    for source, tables in comp.items():
        if source == "defaults":
            if isinstance(tables, dict):
                norm_comp["defaults"] = {"max_rows": tables.get("max_rows")}
            continue
        if not isinstance(tables, dict):
            errors.append(f"compliance.{source} must be a mapping of tables")
            continue
        norm_comp[source] = {}
        for table, rules in tables.items():
            rules = rules or {}
            if not isinstance(rules, dict):
                errors.append(f"compliance.{source}.{table} must be a mapping")
                continue
            norm_comp[source][table.lower()] = {
                "deny_columns": {str(c).lower() for c in (rules.get("deny_columns") or [])},
                "mask_columns": {str(c).lower() for c in (rules.get("mask_columns") or [])},
                "max_rows": rules.get("max_rows"),
            }

    if errors:
        return False, errors, None
    return True, [], {"roles": norm_roles, "compliance": norm_comp}


# ── Access (RBAC delegates here when a doc is loaded) ────────────────────

def policies():
    """rbac.POLICIES-shaped dict, or None when no doc is loaded."""
    doc = _STATE["doc"]
    return doc["roles"] if doc else None


# ── Compliance enforcement (called from every query execution path) ─────

def _referenced_tables(sql):
    from .queryguard import TABLE_REF
    return {ref.strip('"').split(".")[-1].lower() for ref in TABLE_REF.findall(sql or "")}


def _rules_for(source, tables):
    """Union of compliance rules across the tables a query touches."""
    doc = _STATE["doc"]
    if not doc:
        return None
    comp = doc["compliance"].get(source, {})
    deny, mask, caps = set(), set(), []
    for t in tables:
        r = comp.get(t)
        if r:
            deny |= r["deny_columns"]
            mask |= r["mask_columns"]
            if r["max_rows"] is not None:
                caps.append(r["max_rows"])
    default_cap = (doc["compliance"].get("defaults") or {}).get("max_rows")
    cap = min(caps) if caps else default_cap
    if not deny and not mask and cap is None:
        return None
    return {"deny": deny, "mask": mask, "max_rows": cap}


def filter_result(source, sql, columns, rows):
    """Apply compliance to a result BEFORE it leaves the backend: drop denied
    columns, mask masked ones, cap rows. Result-time enforcement means even a
    SELECT * can never leak a denied column. Returns (columns, rows)."""
    if not source or not columns:
        return columns, rows
    r = _rules_for(source, _referenced_tables(sql))
    if not r:
        return columns, rows

    keep = [i for i, c in enumerate(columns) if str(c).lower() not in r["deny"]]
    mask_idx = {i for i in keep if str(columns[i]).lower() in r["mask"]}
    out_cols = [columns[i] for i in keep]
    out_rows = []
    limit = r["max_rows"]
    for ri, row in enumerate(rows):
        if limit is not None and ri >= limit:
            break
        out_rows.append(["***" if i in mask_idx else row[i] for i in keep])
    return out_cols, out_rows


# ── Persistence / apply ─────────────────────────────────────────────────

def apply_yaml(text, user):
    ok, errors, doc = validate(text)
    if not ok:
        return False, errors
    c = db._conn()
    c.execute("INSERT INTO governance_config (yaml, applied_by, applied_at) VALUES (?,?,?)",
              (text, (user or {}).get("email"), time.time()))
    c.commit()
    c.close()
    reload()
    db.log_activity(user, "governance_apply", prompt=f"roles={list(doc['roles'])}")
    return True, []


def reload():
    load()


def template():
    """Scaffold a governance YAML from the live sources and current roles, so an
    admin edits a working document instead of a blank page."""
    from .connectors import all_sources
    from . import rbac

    sources = [s["name"] for s in all_sources()]
    lines = ["version: 1", "roles:"]
    for role, pol in rbac.POLICIES.items():
        lines.append(f"  {role}:")
        if all(pol.get(s) == "*" for s in pol) and set(pol) >= set(sources):
            lines.append('    sources: "*"')
            continue
        lines.append("    sources:")
        for s in sources:
            p = pol.get(s)
            if p == "*":
                lines.append(f'      {s}: "*"')
            elif p:
                lines.append(f"      {s}: [{', '.join(sorted(p))}]")
    lines += [
        "compliance:",
        "  defaults:",
        "    max_rows: 50000",
        "  demo:",
        "    customers:",
        "      mask_columns: [lifetime_value]   # returned as ***",
        "      deny_columns: []                 # stripped from every result",
        "      max_rows: 5000",
    ]
    return "\n".join(lines) + "\n"


# ── Admin API ───────────────────────────────────────────────────────────

def _summary(doc):
    """A compact description of what a doc grants — for the UI's validate view."""
    roles = {r: ("all sources" if p == "*" else
                 {s: ("all tables" if t == "*" else sorted(t)) for s, t in p.items()})
             for r, p in doc["roles"].items()}
    comp = {s: sorted(tables) for s, tables in doc["compliance"].items() if s != "defaults"}
    return {"roles": roles, "compliance_tables": comp,
            "default_max_rows": (doc["compliance"].get("defaults") or {}).get("max_rows")}


def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Governance is admin-only")


from .auth import current_user  # noqa: E402 (router deps)


@router.get("")
def get_config(user=Depends(current_user)):
    _admin(user)
    return {"yaml": current_yaml(), "loaded": loaded(), "source": active_source()}


@router.get("/template")
def get_template(user=Depends(current_user)):
    _admin(user)
    return {"yaml": template()}


class YamlIn(BaseModel):
    yaml: str


@router.post("/validate")
def validate_config(body: YamlIn, user=Depends(current_user)):
    _admin(user)
    ok, errors, doc = validate(body.yaml)
    return {"ok": ok, "errors": errors, "summary": _summary(doc) if ok else None}


@router.put("")
def apply_config(body: YamlIn, user=Depends(current_user)):
    _admin(user)
    ok, errors = apply_yaml(body.yaml, user)
    if not ok:
        raise HTTPException(400, "; ".join(errors))
    return {"applied": True, "loaded": loaded(), "source": active_source()}


@router.delete("")
def clear_config(user=Depends(current_user)):
    """Revert to built-in RBAC (removes the applied document)."""
    _admin(user)
    c = db._conn()
    c.execute("DELETE FROM governance_config")
    c.commit()
    c.close()
    reload()
    db.log_activity(user, "governance_clear")
    return {"cleared": True, "loaded": loaded()}
