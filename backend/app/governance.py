"""Governance-as-code — one YAML that owns access and compliance.

Global env-var connections and a hardcoded RBAC dict don't scale: not every
user should reach every source, and compliance rules (PII masking, forbidden
columns, row caps) belong in one auditable place, not scattered through code.

This module loads a single governance document — per-role source/table access
plus per-table compliance — and makes it the source of truth. RBAC delegates
to it; the query path enforces its compliance rules on every result. An admin
edits the YAML in-app; changes hot-reload with no redeploy. When no document
is loaded, Studio falls back to the built-in policies.POLICIES, so nothing
breaks.

Fleet-wide hot reload. The parsed document is cached per PROCESS, but the
store is shared, so `PUT /api/governance` can only reload the replica that
handled it — every other web worker and the job worker would keep enforcing
the older, more permissive document forever. The active document's IDENTITY
is therefore a DB fact: governance_docs already records (id, applied_at), and
every accessor that gates a decision calls _refresh_if_stale() first, which at
most once per STUDIO_GOVERNANCE_REFRESH_S (default 5s) per process reads that
one indexed row and reloads when it differs. Tightening a policy converges
across the fleet within one TTL, with no cache, bus or extra infrastructure —
see _refresh_if_stale() for the tradeoff.

Layering: this module never imports rbac. rbac resolves a role's policy by
asking governance.policies() first, so governance is the lower layer; the
built-in policy dict both need lives in policies.py (a leaf) for that reason.

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
import hashlib
import os
import re
import time

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .policies import POLICIES
from .queryguard import QueryRejected, _tokens, base_tables

router = APIRouter(prefix="/governance", tags=["governance"])

_STATE = {"doc": None, "yaml": "", "source": None}  # in-process cache of the active doc
_ON_CHANGE = []   # callbacks fired whenever the active document changes (cache invalidation)

# Freshness bookkeeping for _refresh_if_stale(): "at" is the monotonic time of
# this process's last store probe, "ident" the (id, applied_at) of the newest
# applied document as of that probe. Both are process-local by design — they
# describe what THIS process has seen, never what is true.
_FRESH = {"at": 0.0, "ident": None}
_UNKNOWN = object()          # store unreadable: keep serving the doc we have
DEFAULT_REFRESH_S = 5.0


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS governance_docs (
            id TEXT PRIMARY KEY,
            yaml TEXT NOT NULL,
            applied_by TEXT,
            applied_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS governance_docs_applied_at
            ON governance_docs (applied_at DESC, id DESC);
        """
    )
    c.commit()
    c.close()


def _refresh_ttl():
    """Seconds a process may serve its cached document without re-checking the
    store. 0 means check on every gated decision."""
    try:
        return max(0.0, float(os.getenv("STUDIO_GOVERNANCE_REFRESH_S", DEFAULT_REFRESH_S)))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH_S


def _newest_ident():
    """(id, applied_at) of the newest applied document — the document's whole
    identity, since a row is never edited in place. None when the table is
    empty; _UNKNOWN when the store cannot be read at all, which must NEVER be
    read as "no document" (that would fall open to built-in RBAC on a blip)."""
    try:
        c = db._conn()
        try:
            row = c.execute(
                "SELECT id, applied_at FROM governance_docs "
                "ORDER BY applied_at DESC, id DESC LIMIT 1").fetchone()
        finally:
            c.close()
    except Exception:
        return _UNKNOWN
    return (row["id"], row["applied_at"]) if row else None


def _refresh_if_stale():
    """Adopt a document another process applied. Called by every accessor that
    gates a decision, and a no-op inside the TTL.

    Why: _STATE is process-local, and PUT /api/governance reloads only the
    process that served it. Without this, a second web worker or the job
    worker keeps enforcing the older, more permissive document indefinitely —
    tightening a policy would not take effect fleet-wide, and stale replicas
    would keep serving dashboard tiles cached under it.

    The tradeoff, stated plainly: ONE single-row indexed read of
    governance_docs per process per STUDIO_GOVERNANCE_REFRESH_S (default 5s),
    whether or not anything changed, in exchange for fleet-wide convergence
    with no Redis, pub/sub or sticky routing. The window is bounded, not zero:
    a replica may enforce the previous document for up to one TTL, so lower
    the TTL (0 = check every decision) where that matters and raise it where
    the store is remote. Nothing here weakens fail-closed behaviour: an
    unreadable store keeps the document already in hand, and a malformed
    stored document still leaves the process on built-in RBAC.

    Racing threads are safe: the timestamp is stamped BEFORE the probe, so
    only one thread queries per TTL, and load() is idempotent."""
    now = time.monotonic()
    if now - _FRESH["at"] < _refresh_ttl():
        return
    # Stamp first: a concurrent caller — or a re-entrant one, from an
    # on_change hook — must not queue a second probe behind this one.
    _FRESH["at"] = now
    ident = _newest_ident()
    if ident is _UNKNOWN or ident == _FRESH["ident"]:
        return
    load()


def load():
    """Load the active document: DB (admin-applied) first, else STUDIO_GOVERNANCE
    file, else none (Studio uses built-in RBAC). Cached in _STATE; accessors
    re-check the store every STUDIO_GOVERNANCE_REFRESH_S (_refresh_if_stale),
    and reload() forces it now."""
    c = db._conn()
    row = c.execute("SELECT id, yaml, applied_at FROM governance_docs "
                    "ORDER BY applied_at DESC, id DESC LIMIT 1").fetchone()
    c.close()
    # Record the identity even when the row's YAML is unusable: otherwise every
    # refresh would see a "change" and reload the same broken document forever.
    _FRESH.update(at=time.monotonic(),
                  ident=(row["id"], row["applied_at"]) if row else None)
    if row and row["yaml"].strip():
        _set(row["yaml"], "database")
        return
    path = os.getenv("STUDIO_GOVERNANCE")
    if path and os.path.isfile(path):
        with open(path) as f:
            _set(f.read(), f"file:{path}")
        return
    _STATE.update(doc=None, yaml="", source=None)
    _changed()


def _set(text, source):
    """Install a document in this process. Callers other than load() (a test,
    a direct injection) deliberately leave _FRESH["ident"] alone: what they
    installed stands until the STORE's newest document differs from the one
    this process last observed — at which point the store, the shared fact,
    wins."""
    ok, errors, doc = validate(text)
    if ok:
        _STATE.update(doc=doc, yaml=text, source=source)
    else:
        # A malformed stored doc must fail closed to built-in RBAC, not crash.
        _STATE.update(doc=None, yaml=text, source=f"{source} (invalid: {errors[0] if errors else '?'})")
    _FRESH["at"] = time.monotonic()      # just installed is just checked
    _changed()


def _changed():
    """Fire every on_change hook. Hooks are how higher layers (the dashboard
    tile cache) drop state computed under the previous document without this
    module importing them — governance sits below gateway, which they import."""
    for fn in list(_ON_CHANGE):
        try:
            fn()
        except Exception:
            pass


def on_change(fn):
    """Register a zero-arg callback run after the active document changes
    (apply, clear, reload, or a direct _set). Returns fn, so it can decorate."""
    if fn not in _ON_CHANGE:
        _ON_CHANGE.append(fn)
    return fn


def version():
    """A short digest of the active document ("builtin" when none) — cache
    keys that hold governed rows carry it, so a policy change is a miss on
    every app instance sharing a Redis, not just the one that applied it.

    Refreshes first: a stale replica must not keep minting the OLD version and
    hitting frames cached under the document it no longer should be using."""
    _refresh_if_stale()
    doc = _STATE["doc"]
    if not doc:
        return "builtin"
    return hashlib.sha1(_STATE["yaml"].encode("utf-8", "replace")).hexdigest()[:12]


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
    """policies.POLICIES-shaped dict, or None when no doc is loaded. RBAC's one
    switch, so it refreshes: an access decision is never made on a document
    another process has already replaced (beyond one refresh TTL)."""
    _refresh_if_stale()
    doc = _STATE["doc"]
    return doc["roles"] if doc else None


# ── Compliance enforcement (called from every query execution path) ─────
#
# Fail-closed lineage. A deny/mask rule names a BASE column, but a query can
# rename it (`name AS n`), hide the rename inside a derived table or CTE, or
# reach the table through a comma join the FROM/JOIN regex never saw — and
# every one of those shapes walked the raw values straight out of the gate.
# The rules now are:
#
#   * referenced tables come from queryguard.base_tables (the tokenizer that
#     RBAC already trusts: comma joins and CTE names included); a statement
#     with NO attributable table is treated as touching every governed table
#     of the source, never as touching none;
#   * a SIMPLE statement (one SELECT, no WITH/UNION) whose select list maps
#     1:1 onto the output columns is governed per column, on the output name
#     OR any identifier inside the column's defining expression;
#   * a simple all-star projection (`SELECT *`, `t.*`) is governed on output
#     names, which ARE the base column names in that shape;
#   * anything else (derived table, CTE, set operation, a projection we cannot
#     map) is OPAQUE: if a denied column is named anywhere in the statement it
#     is rejected outright, and if a masked column is named anywhere every
#     output column is masked — the lineage is unknown, so every column could
#     carry the value. Simplicity and fail-closed beat cleverness here.

def _governed_tables(source):
    doc = _STATE["doc"]
    return set((doc["compliance"].get(source) or {})) - {"defaults"} if doc else set()


def _referenced_tables(source, sql):
    """Base tables a statement reads. Unparseable / unattributable SQL (or no
    SQL at all, e.g. a stored message with rows but no query) fails closed to
    every governed table of the source."""
    tables = base_tables(sql or "")
    return tables or _governed_tables(source)


def _rules_for(source, tables):
    """Union of compliance rules across the tables a query touches."""
    _refresh_if_stale()
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


def column_rules(source, table):
    """The deny/mask column sets for ONE table, as {"deny": set, "mask": set}
    (both empty when no document is loaded or the table has no rules).

    filter_result() governs rows; this governs the DESCRIPTION of a table —
    the schema metadata the catalog shows the UI and hands to the suggestion
    LLM. A denied column must not exist there either: listing it invites
    questions about it, and the query guard then rejects nothing because
    the guard checks tables, not columns. Masked columns stay listed — their
    values are replaced at result time, the column itself is not a secret.
    Names are lowercased to match the normalised document."""
    _refresh_if_stale()
    doc = _STATE["doc"]
    if not doc or not source or not table:
        return {"deny": set(), "mask": set()}
    r = (doc["compliance"].get(source) or {}).get(str(table).lower())
    if not r:
        return {"deny": set(), "mask": set()}
    return {"deny": set(r["deny_columns"]), "mask": set(r["mask_columns"])}


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SET_OPS = {"with", "union", "intersect", "except"}


def _split_top_commas(text):
    """Split on commas that are not inside parentheses or quotes."""
    items, buf, depth = [], [], 0
    in_sq = in_dq = False
    for ch in text:
        if in_sq:
            buf.append(ch)
            if ch == "'":
                in_sq = False
            continue
        if in_dq:
            buf.append(ch)
            if ch == '"':
                in_dq = False
            continue
        if ch == "'":
            in_sq = True
        elif ch == '"':
            in_dq = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        items.append("".join(buf))
    return items


def _item_name(item):
    """The output name a select-list item produces, lowercased: its alias, or
    the bare/qualified column it is. None for `*`, `t.*` or an expression
    with no alias (the engine names those, and we do not guess)."""
    s = item.strip()
    m = re.search(r'\bas\s+("?)([A-Za-z_][A-Za-z0-9_]*)\1\s*$', s, re.IGNORECASE)
    if m:
        return m.group(2).lower()
    m = re.fullmatch(r'(?:[A-Za-z_][A-Za-z0-9_]*\.)*"?([A-Za-z_][A-Za-z0-9_]*)"?', s)
    return m.group(1).lower() if m else None


def _select_items(sql, toks):
    """The top-level select-list items of a SIMPLE statement, or None when the
    statement has a CTE, a set operation, more than one SELECT, or no FROM at
    depth 0 — shapes whose lineage this module does not try to follow."""
    words = [t[1].lower() for t in toks if t[0] == "word"]
    if not words or words[0] != "select" or words.count("select") != 1:
        return None
    if _SET_OPS & set(words):
        return None
    s = sql.strip()
    depths, d = [], 0
    in_sq = in_dq = False
    for ch in s:
        depths.append(d)
        if in_sq:
            if ch == "'":
                in_sq = False
        elif in_dq:
            if ch == '"':
                in_dq = False
        elif ch == "'":
            in_sq = True
        elif ch == '"':
            in_dq = True
        elif ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
    from_at = -1
    for m in re.finditer(r"\bfrom\b", s.lower()):
        if depths[m.start()] == 0:
            from_at = m.start()
            break
    if from_at < 0:
        return None
    head = re.match(r"\s*select\s+(?:distinct\s+|all\s+)?", s, re.IGNORECASE)
    return _split_top_commas(s[head.end():from_at])


def _shape(sql, columns):
    """("mapped", per-column ident sets) | ("star", None) | ("opaque", None).

    "mapped": every output column has exactly one defining select-list item,
    positionally when the counts agree, else by name — so a frame whose denied
    columns were ALREADY dropped (a cache hit, a stored message re-filtered at
    read time) still maps instead of failing closed twice. "star": a simple
    all-star projection, where output names are base names. "opaque": lineage
    unknown — callers fail closed."""
    try:
        toks, cleaned = _tokens((sql or "").strip().rstrip(";").strip())
    except QueryRejected:
        return "opaque", None
    items = _select_items(cleaned, toks)
    if items is None:
        return "opaque", None
    names = [c.lower() for c in columns]
    idents = [{t.lower() for t in _IDENT_RE.findall(it)} for it in items]
    if len(items) == len(columns):
        return "mapped", idents
    if all(it.strip() == "*" or it.strip().endswith(".*") for it in items):
        return "star", None
    by_name = {}
    for it, ids in zip(items, idents):
        nm = _item_name(it)
        if nm is not None:
            by_name.setdefault(nm, []).append(ids)
    if all(len(by_name.get(n, [])) == 1 for n in names):
        return "mapped", [by_name[n][0] for n in names]
    return "opaque", None


def _all_idents(sql):
    """Every identifier token in the statement, lowercased (quoted ones too)."""
    try:
        toks, _ = _tokens((sql or "").strip().rstrip(";").strip())
    except QueryRejected:
        return set()
    return {t[1].strip('"').lower() for t in toks if t[0] in ("word", "ident")}


def filter_result(source, sql, columns, rows):
    """Apply compliance to a result BEFORE it leaves the backend: drop denied
    columns, mask masked ones, cap rows. Result-time enforcement means even a
    SELECT * can never leak a denied column. Returns (columns, rows).

    Raises QueryRejected when a denied column is referenced inside a shape
    whose lineage cannot be followed (derived table, CTE, set operation, an
    unmappable projection): the value could be in any output column, so the
    statement is refused rather than guessed at. Callers that re-filter stored
    rows at read time catch it and hide the rows."""
    if not source or not columns:
        return columns, rows
    # Before _referenced_tables, which reads _STATE for the governed-table
    # fallback: both halves of the decision must see the same document.
    _refresh_if_stale()
    r = _rules_for(source, _referenced_tables(source, sql))
    if not r:
        return columns, rows

    shape, src = _shape(sql, columns)
    mask_all = False
    if shape == "opaque":
        named = _all_idents(sql)
        hit = sorted(named & r["deny"])
        if hit:
            raise QueryRejected(
                f"Column '{hit[0]}' is denied for this table and is referenced inside a "
                f"subquery, CTE, set operation or an unmappable projection; select it "
                f"directly at the top level or drop it")
        mask_all = bool(named & r["mask"])

    def _hit(i, colset):
        if str(columns[i]).lower() in colset:
            return True
        return src is not None and bool(src[i] & colset)

    keep = [i for i in range(len(columns)) if not _hit(i, r["deny"])]
    mask_idx = set(keep) if mask_all else {i for i in keep if _hit(i, r["mask"])}
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
    c.execute("INSERT INTO governance_docs (id, yaml, applied_by, applied_at) VALUES (?,?,?,?)",
              (__import__("uuid").uuid4().hex, text, (user or {}).get("email"), time.time()))
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

    sources = [s["name"] for s in all_sources()]
    lines = ["version: 1", "roles:"]
    for role, pol in POLICIES.items():
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
    # Whichever replica answers, show the document the fleet is converging on.
    _refresh_if_stale()
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
    c.execute("DELETE FROM governance_docs")
    c.commit()
    c.close()
    reload()
    db.log_activity(user, "governance_clear")
    return {"cleared": True, "loaded": loaded()}
