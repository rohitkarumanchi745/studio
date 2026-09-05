"""Saved dashboards: pinned chart recipes, layout, and slicer state.

A dashboard stores the RECIPE (sql + chart spec), never rows. Data is
re-fetched per viewer through gateway.execute, so RBAC, the query guard, the
row cap and governance are evaluated at view time — by the one code path
that owns them — and a tile can never leak rows the reader may not query.

Filters (slicers and cross-filter selections) are applied by viz over the
cached base rows — the SQL is never rewritten or wrapped, which is what
keeps the (role, source, sql) cache reusable across every filter
combination and makes cross-filtering cost microseconds, not a warehouse
round trip.
"""
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, gateway, governance, queryguard, rbac, viz
from .auth import current_user
from .limits import MAX_ROWS
from .queryguard import QueryRejected

router = APIRouter(prefix="/dashboards", tags=["dashboards"])

TILE_MAX_ROWS = int(os.getenv("STUDIO_DASH_TILE_ROWS", "5000"))
CACHE_TTL = float(os.getenv("STUDIO_DASH_CACHE_TTL", "60"))
CACHE_MAX_ENTRIES = 200

DEFAULT_LAYOUT = {"cols": 12, "row_h": 80, "gap": 12}
VISIBILITIES = ("private", "org")
# Pinned tiles that need the full width to be readable.
_TILE_SIZE = {"kpi": (12, 3), "table": (12, 5)}
# Rows sanitize_spec needs to type the post-transform frame; it runs the whole
# pipeline to build them, so the render path must not hand it the full frame.
SANITIZE_SAMPLE = int(os.getenv("STUDIO_DASH_SANITIZE_SAMPLE", "200"))
VIEW_LOG_TTL = float(os.getenv("STUDIO_DASH_VIEW_LOG_TTL", "300"))


# ── Tables ──────────────────────────────────────────────────────────────

def init_tables():
    """Idempotent; also called at import so a missed wiring cannot break the feature."""
    with db.connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS dashboards (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                visibility TEXT NOT NULL DEFAULT 'private',
                layout TEXT NOT NULL DEFAULT '{}',
                filters TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dashboard_tiles (
                id TEXT PRIMARY KEY,
                dashboard_id TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL,
                table_label TEXT,
                sql TEXT NOT NULL,
                spec TEXT NOT NULL,
                layout TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_dash_user ON dashboards(user_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tile_dash ON dashboard_tiles(dashboard_id, position);
            """
        )
        c.commit()


init_tables()


# ── Coercion helpers (every stored/incoming field degrades, never throws) ──

def _jloads(text, default):
    try:
        val = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return val if isinstance(val, type(default)) else default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _dict(v):
    return v if isinstance(v, dict) else {}


def _list(v):
    return v if isinstance(v, list) else []


def _filters(v):
    """Keep only well-formed predicates — a partial LLM/client filter is dropped."""
    return [f for f in _list(v) if isinstance(f, dict) and f.get("col")]


def _owner_email(user_id):
    user = db.get_user(user_id)
    return user["email"] if user else user_id


def _row_to_dash(row):
    d = dict(row)
    d["layout"] = _jloads(d.get("layout"), {}) or dict(DEFAULT_LAYOUT)
    d["filters"] = _filters(_jloads(d.get("filters"), []))
    return d


def _row_to_tile(row):
    d = dict(row)
    d["spec"] = _jloads(d.get("spec"), {})
    d["layout"] = _jloads(d.get("layout"), {})
    return d


def _public_dash(dash, user, tiles=None):
    out = {
        "id": dash["id"], "title": dash["title"], "description": dash["description"],
        "visibility": dash["visibility"], "layout": dash["layout"],
        "filters": _visible_filters(dash, user), "can_edit": can_edit(dash, user),
        "owner": _owner_email(dash["user_id"]),
        "created_at": dash["created_at"], "updated_at": dash["updated_at"],
    }
    if tiles is not None:
        out["tiles"] = tiles
    return out


def _public_tile(tile, *, denied=False):
    out = {k: tile.get(k) for k in
           ("id", "position", "title", "source", "table_label", "sql", "spec",
            "layout", "created_at", "updated_at")}
    out["denied"] = denied
    if denied:
        # The recipe IS data: the SQL carries WHERE literals and the spec
        # carries palette.key_order, which is a list of real category cells.
        out["sql"] = ""
        out["spec"] = {k: v for k, v in _dict(out["spec"]).items()
                       if k in ("v", "type", "title")}
    return out


def _tile_visible(tile, user):
    return rbac.can_access(user["role"], tile.get("source"),
                           tile.get("table_label") or "*")


_SPEC_COL_KEYS = ("col", "as", "by", "x", "y", "cols", "columns")


def _spec_columns(spec):
    """Every column name a spec names, used to decide which stored slicer
    values a reader could have obtained from a tile they may query."""
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _SPEC_COL_KEYS:
                    if isinstance(v, str):
                        found.add(v)
                    elif isinstance(v, list):
                        found.update(s for s in v if isinstance(s, str))
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(_dict(spec))
    return found


def _visible_filters(dash, user):
    """Stored slicer values are cells lifted out of tile results, so they
    inherit the RBAC of the tile they came from."""
    filters = dash.get("filters") or []
    tiles = dash.get("tiles") or []
    if all(_tile_visible(t, user) for t in tiles):
        return filters
    cols = set()
    for t in tiles:
        if _tile_visible(t, user):
            cols |= _spec_columns(t.get("spec"))
    return [f for f in filters if f.get("col") in cols]


# ── Storage (no HTTP — importable by tests and by a future agent tool) ──

def dashboard_owner(dashboard_id):
    with db.connect() as c:
        row = c.execute("SELECT user_id FROM dashboards WHERE id=?", (dashboard_id,)).fetchone()
    return row["user_id"] if row else None


def create_dashboard(user, title, description="", visibility="private", layout=None):
    did, now = str(uuid.uuid4()), time.time()
    with db.connect() as c:
        c.execute(
            "INSERT INTO dashboards (id, user_id, title, description, visibility, layout, "
            "filters, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (did, user["id"], (title or "Untitled dashboard")[:120], (description or "")[:500],
             visibility if visibility in VISIBILITIES else "private",
             json.dumps(_dict(layout) or DEFAULT_LAYOUT), "[]", now, now),
        )
        c.commit()
    return get_dashboard(did)


def get_dashboard(dashboard_id, *, with_tiles=True):
    with db.connect() as c:
        row = c.execute("SELECT * FROM dashboards WHERE id=?", (dashboard_id,)).fetchone()
        if not row:
            return None
        dash = _row_to_dash(row)
        if with_tiles:
            rows = c.execute(
                "SELECT * FROM dashboard_tiles WHERE dashboard_id=? ORDER BY position, created_at",
                (dashboard_id,)).fetchall()
            dash["tiles"] = [_row_to_tile(r) for r in rows]
    return dash


def list_dashboards(user):
    """The user's own dashboards plus every org-visible one, newest first."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT d.*, u.email owner_email, "
            "(SELECT COUNT(*) FROM dashboard_tiles t WHERE t.dashboard_id=d.id) tile_count "
            "FROM dashboards d LEFT JOIN users u ON u.id=d.user_id "
            "WHERE d.user_id=? OR d.visibility='org' ORDER BY d.updated_at DESC",
            (user["id"],)).fetchall()
    out = []
    for r in rows:
        d = _row_to_dash(r)
        out.append({
            "id": d["id"], "title": d["title"], "description": d["description"],
            "visibility": d["visibility"], "tile_count": d["tile_count"],
            "owner": d["owner_email"] or d["user_id"], "can_edit": can_edit(d, user),
            "created_at": d["created_at"], "updated_at": d["updated_at"],
        })
    return out


_DASH_FIELDS = ("title", "description", "visibility", "layout", "filters")


def update_dashboard(dashboard_id, **fields):
    sets, args = [], []
    for key in _DASH_FIELDS:
        if key not in fields or fields[key] is None:
            continue
        val = fields[key]
        if key == "title":
            val = str(val)[:120] or "Untitled dashboard"
        elif key == "description":
            val = str(val)[:500]
        elif key == "visibility":
            val = val if val in VISIBILITIES else "private"
        elif key == "layout":
            val = json.dumps(_dict(val))
        elif key == "filters":
            val = json.dumps(_filters(val))
        sets.append(f"{key}=?")
        args.append(val)
    if sets:
        with db.connect() as c:
            c.execute(f"UPDATE dashboards SET {', '.join(sets)}, updated_at=? WHERE id=?",
                      (*args, time.time(), dashboard_id))
            c.commit()
    return get_dashboard(dashboard_id)


def delete_dashboard(dashboard_id):
    with db.connect() as c:
        c.execute("DELETE FROM dashboard_tiles WHERE dashboard_id=?", (dashboard_id,))
        c.execute("DELETE FROM dashboards WHERE id=?", (dashboard_id,))
        c.commit()


def _touch(dashboard_id):
    with db.connect() as c:
        c.execute("UPDATE dashboards SET updated_at=? WHERE id=?", (time.time(), dashboard_id))
        c.commit()


def _auto_layout(tiles, chart_type, cols=12):
    """First free slot scanning top-down, left-to-right."""
    w, h = _TILE_SIZE.get(chart_type, (6, 4))
    boxes = []
    for t in tiles:
        lay = _dict(t.get("layout"))
        boxes.append((_int(lay.get("x"), 0), _int(lay.get("y"), 0),
                      max(1, _int(lay.get("w"), 6)), max(1, _int(lay.get("h"), 4))))
    bottom = max((y + bh for _, y, _, bh in boxes), default=0)
    for y in range(bottom + 1):
        for x in range(0, max(1, cols - w + 1)):
            if not any(x < bx + bw and bx < x + w and y < by + bh and by < y + h
                       for bx, by, bw, bh in boxes):
                return {"x": x, "y": y, "w": w, "h": h}
    return {"x": 0, "y": bottom, "w": w, "h": h}


def _spec_for_storage(raw, columns, rows):
    """No column list means the frame is UNKNOWN, never "no columns exist".
    sanitize_spec prunes every reference it cannot resolve, so against [] it
    deletes x, y, every transform stage and every format entry — a pin or a
    title-only PATCH would silently demote an aggregate chart to row detail.
    Without a frame, validate the shape only and let tile_data prune against
    the tile's real result columns at render time."""
    cols = _list(columns)
    if not cols:
        return viz.normalize_spec(_dict(raw)), []
    return viz.sanitize_spec(_dict(raw), cols, _list(rows) or None)


def add_tile(dashboard_id, tile):
    """`tile` is the raw request dict; the spec is sanitized before storage."""
    dash = get_dashboard(dashboard_id) or {"tiles": []}
    spec, warnings = _spec_for_storage(tile.get("spec"), tile.get("columns"),
                                       tile.get("rows"))
    layout = _dict(tile.get("layout")) or _auto_layout(dash.get("tiles") or [], spec.get("type"))
    position = _int(tile.get("position"), len(dash.get("tiles") or []))
    tid, now = str(uuid.uuid4()), time.time()
    with db.connect() as c:
        c.execute(
            "INSERT INTO dashboard_tiles (id, dashboard_id, position, title, source, table_label, "
            "sql, spec, layout, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tid, dashboard_id, position, str(tile.get("title") or spec.get("title") or "")[:120],
             str(tile.get("source") or ""), tile.get("table_label"), str(tile.get("sql") or ""),
             json.dumps(spec), json.dumps(layout), now, now),
        )
        c.commit()
    _touch(dashboard_id)
    return get_tile(dashboard_id, tid), warnings


def get_tile(dashboard_id, tile_id):
    with db.connect() as c:
        row = c.execute("SELECT * FROM dashboard_tiles WHERE id=? AND dashboard_id=?",
                        (tile_id, dashboard_id)).fetchone()
    return _row_to_tile(row) if row else None


_TILE_FIELDS = ("title", "spec", "layout", "sql", "source", "table_label", "position")


def update_tile(dashboard_id, tile_id, **fields):
    """Returns (tile, warnings); tile is None when the id is unknown."""
    existing = get_tile(dashboard_id, tile_id)
    if not existing:
        return None, []
    warnings = []
    sets, args = [], []
    for key in _TILE_FIELDS:
        if key not in fields or fields[key] is None:
            continue
        val = fields[key]
        if key == "title":
            val = str(val)[:120]
        elif key == "spec":
            incoming = _dict(val)
            merged = (viz.merge_spec(existing["spec"], incoming)
                      if fields.get("spec_merge", True) else incoming)
            val, warnings = viz.sanitize_spec(merged, _list(fields.get("columns")),
                                                  _list(fields.get("rows")) or None)
            val = json.dumps(val)
        elif key == "layout":
            val = json.dumps(_dict(val))
        elif key == "position":
            val = _int(val, existing["position"])
        else:
            val = str(val)
        sets.append(f"{key}=?")
        args.append(val)
    if sets:
        with db.connect() as c:
            c.execute(f"UPDATE dashboard_tiles SET {', '.join(sets)}, updated_at=? "
                      "WHERE id=? AND dashboard_id=?", (*args, time.time(), tile_id, dashboard_id))
            c.commit()
        _touch(dashboard_id)
    return get_tile(dashboard_id, tile_id), warnings


def delete_tile(dashboard_id, tile_id):
    with db.connect() as c:
        c.execute("DELETE FROM dashboard_tiles WHERE id=? AND dashboard_id=?",
                  (tile_id, dashboard_id))
        c.commit()
    _touch(dashboard_id)


def set_layout(dashboard_id, layout):
    """Bulk drag/resize commit. Unknown tile ids are ignored, not an error."""
    known = {t["id"] for t in (get_dashboard(dashboard_id) or {}).get("tiles", [])}
    now, updated = time.time(), 0
    with db.connect() as c:
        for i, item in enumerate(_list(layout)):
            if not isinstance(item, dict) or item.get("id") not in known:
                continue
            box = {"x": _int(item.get("x"), 0), "y": _int(item.get("y"), 0),
                   "w": max(1, _int(item.get("w"), 6)), "h": max(1, _int(item.get("h"), 4))}
            c.execute("UPDATE dashboard_tiles SET layout=?, position=?, updated_at=? WHERE id=?",
                      (json.dumps(box), i, now, item["id"]))
            updated += 1
        c.commit()
    if updated:
        _touch(dashboard_id)
    return updated


# ── SQL guard ───────────────────────────────────────────────────────────
#
# queryguard.TABLE_REF is `\b(?:from|join)\s+([a-zA-Z_][\w."]*)`, which cannot
# see `FROM/**/customers`, `FROM(customers)`, `FROM "customers"` or the second
# arm of `FROM sales, customers` — every one of those was verified to reach the
# warehouse. Unlike /chat/rerun, tile SQL is PERSISTED and its rows are cached
# for CACHE_TTL, so a single accepted string keeps serving denied rows. The
# tokenizer below re-derives the table references from real SQL tokens and is
# the authority for this module; queryguard still owns single-statement,
# SELECT-only and the forbidden-keyword sweep.

_WS = " \t\r\n\f\v"
_QUOTES = {'"': '"', "`": "`", "[": "]"}
# A FROM/JOIN target that starts with one of these is a subquery or table
# function, not a name to resolve.
_SUBQUERY_HEAD = {"select", "with", "values", "table", "lateral", "unnest"}
# Keywords that end a FROM list, so a later comma is not another table.
_FROM_STOP = {"where", "group", "having", "order", "limit", "offset", "fetch",
              "union", "intersect", "except", "window", "qualify", "into"}


def _sql_tokens(sql):
    """(kind, text) tokens with comments removed, plus the comment-free SQL.

    Whitespace and comments produce no token, so `FROM/**/t` and `FROM  t` are
    indistinguishable to the caller. Raises on an unterminated literal or
    comment rather than guessing where it ends.
    """
    toks, out, i, n = [], [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in _WS:
            while i < n and sql[i] in _WS:
                i += 1
            out.append(" ")
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                raise queryguard.QueryRejected("Unterminated comment in query")
            i = j + 2
            out.append(" ")
        elif ch == "'":
            j = i + 1
            while True:
                j = sql.find("'", j)
                if j < 0:
                    raise queryguard.QueryRejected("Unterminated string literal in query")
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
                    raise queryguard.QueryRejected("Unterminated quoted identifier in query")
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


def _qualified_name(toks, i):
    """`db.schema."Tbl"` -> ('tbl', index after it). The bare name is what RBAC
    keys on, so only the last part matters."""
    parts = []
    n = len(toks)
    while i < n and toks[i][0] in ("word", "ident"):
        parts.append(toks[i][1])
        i += 1
        if i < n and toks[i] == ("punct", "."):
            i += 1
            continue
        break
    if not parts:
        raise queryguard.QueryRejected("Unreadable table reference after FROM/JOIN")
    return parts[-1].strip('"').lower(), i


def _read_ref(toks, i, depth, refs):
    n = len(toks)
    while i < n and toks[i] == ("punct", "("):
        depth += 1
        i += 1
    if i < n and toks[i][0] == "word" and toks[i][1].lower() in _SUBQUERY_HEAD:
        return i, depth
    name, i = _qualified_name(toks, i)
    refs.append(name)
    return i, depth


def _table_refs(toks):
    """Every name a FROM or JOIN targets, including comma-joined arms."""
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


def _cte_names(toks):
    """`name AS (` and `name (cols) AS (` — legal to reference, not tables."""
    names = set()
    for j in range(len(toks) - 2):
        if toks[j][0] not in ("word", "ident"):
            continue
        k = j + 1
        if toks[k] == ("punct", "("):
            depth = 0
            while k < len(toks):
                if toks[k] == ("punct", "("):
                    depth += 1
                elif toks[k] == ("punct", ")"):
                    depth -= 1
                    if depth == 0:
                        k += 1
                        break
                k += 1
        if (k + 1 < len(toks) and toks[k][0] == "word" and toks[k][1].lower() == "as"
                and toks[k + 1] == ("punct", "(")):
            names.add(toks[j][1].strip('"').lower())
    return names


def validate_sql(sql, allowed_tables, all_tables=()):
    """Raise queryguard.QueryRejected unless `sql` is a single SELECT touching
    only `allowed_tables`. Returns the comment-free SQL, which is what the
    caller must execute — validating one string and running another is exactly
    how `FROM/**/customers` got through."""
    toks, cleaned = _sql_tokens(sql or "")
    if toks and toks[-1] == ("punct", ";"):
        toks = toks[:-1]
    allowed = {str(t).lower() for t in allowed_tables}
    ctes = _cte_names(toks)
    for ref in _table_refs(toks):
        if ref not in allowed and ref not in ctes:
            raise queryguard.QueryRejected(
                f"Access to table '{ref}' is not permitted for your role")
    # A denied table's name may not appear as an identifier anywhere, in any
    # syntactic position — this is what closes the forms the ref walk cannot
    # attribute to a FROM (correlated names, dialect join syntax).
    denied = {str(t).lower() for t in all_tables} - allowed
    if denied:
        for kind, text in toks:
            name = text.strip('"').lower()
            if kind in ("word", "ident") and name in denied:
                raise queryguard.QueryRejected(
                    f"Access to table '{name}' is not permitted for your role")
    return queryguard.validate(cleaned, allowed_tables)


# ── Access ──────────────────────────────────────────────────────────────

def can_view(dash, user):
    return bool(dash) and (dash.get("user_id") == user["id"] or dash.get("visibility") == "org")


def can_edit(dash, user):
    return bool(dash) and dash.get("user_id") == user["id"]


def _own_or_404(dashboard_id, user, *, edit=False):
    """404 unknown · 403 not yours / view-only. ALWAYS before any connector call."""
    dash = get_dashboard(dashboard_id, with_tiles=False)
    if dash is None:
        raise HTTPException(404, "Dashboard not found")
    if edit and not can_edit(dash, user):
        raise HTTPException(403, "Not your dashboard")
    if not can_view(dash, user):
        raise HTTPException(403, "Not your dashboard")
    return dash


def _guard_tile(user, source, table_label, sql):
    """RBAC + query guard for a tile being written. You cannot pin what you
    cannot query. Returns the gateway's allowed table list.

    Two guards, strictest first. validate_sql (this module's tokenizer) rejects
    a denied table's name in ANY identifier position, because tile SQL is
    persisted and its rows cached — one accepted string keeps serving. Then
    gateway.check applies exactly the pipeline tile_data will run at view time,
    so a pin that passes here cannot fail RBAC/guard later for its author.
    The allowlist handed to validate_sql comes from the gateway, never from
    this module: the strict scan only narrows what the gateway already allows.
    """
    if not source:
        raise HTTPException(400, "Tile needs a source")
    label = table_label or "*"
    try:
        connector, allowed = gateway.scope(user, source, table_label=label)
    except QueryRejected:
        raise HTTPException(403, f"Your role has no access to {source}.{label}")
    except HTTPException:
        raise                       # unknown / unconfigured source: 404 / 400
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")
    try:
        all_tables = connector.list_tables()   # the denied set is all - allowed
    except Exception as e:
        raise HTTPException(502, f"Source error: {e}")
    try:
        cleaned = validate_sql(sql or "", allowed, all_tables)
        _connector, allowed, _cleaned = gateway.check(user, source, cleaned, table_label=label)
    except QueryRejected as e:
        raise HTTPException(400, str(e))
    return allowed


def _capture_key_order(spec, columns, rows):
    """Pin-time colour identity: freeze the category order so filtering a
    category out never repaints the survivors (spec §9 HOP 6)."""
    fmt = _dict(spec.get("format"))
    palette = _dict(fmt.get("palette"))
    if palette.get("key_order") or not rows or not columns:
        return spec
    x = spec.get("x")
    if not x or x not in columns:
        return spec
    i, seen = columns.index(x), []
    for row in rows:
        if not isinstance(row, (list, tuple)) or i >= len(row):
            continue
        key = row[i]
        if key is not None and key not in seen:
            seen.append(key)
    if not seen:
        return spec
    palette = {**palette, "mode": palette.get("mode") or "by_key", "key_order": seen}
    return {**spec, "format": {**fmt, "palette": palette}}


# ── Data ────────────────────────────────────────────────────────────────

# (role, source, sha1(sql), governance version) -> (ts, columns, rows) — the
# frame as the gateway returned it under THAT governance version. A policy
# change is a miss (the version is in the key, and governance.on_change clears
# this dict); a hit is re-filtered anyway, so tightening a rule between two
# reads under the same document can never serve pre-policy rows.
_CACHE = OrderedDict()
_REDIS = None
_REDIS_TRIED = False

governance.on_change(_CACHE.clear)


def _redis():
    """Shared tile cache when REDIS_URL is set. A dead Redis must never take
    the app down — every failure falls back to the in-process cache."""
    global _REDIS, _REDIS_TRIED
    if _REDIS_TRIED:
        return _REDIS
    _REDIS_TRIED = True
    url = os.getenv("REDIS_URL", "")
    if url:
        try:
            import redis
            client = redis.from_url(url, socket_timeout=1.5, socket_connect_timeout=1.5)
            client.ping()
            _REDIS = client
        except Exception:
            _REDIS = None
    return _REDIS


def _tile_cap():
    """A tile may lower the server ceiling, never raise it."""
    return min(TILE_MAX_ROWS, MAX_ROWS)


def _cache_key(role, source, sql):
    """(role, source, sha1(sql), governance version) and the matching Redis key.
    Two roles can never share rows; two governance documents can never share a
    frame — on any app instance."""
    digest = hashlib.sha1(sql.encode("utf-8", "replace")).hexdigest()
    gov = governance.version()
    return (role, source, digest, gov), f"studio:tile:{role}:{source}:{digest}:{gov}"


def _cached_query(user, source, sql, *, table_label="*", refresh=False):
    """TTL cache of the base frame, keyed by (role, source, sha1(sql),
    governance version). A miss goes through gateway.execute — RBAC, guard,
    cap, governance and the audit row, all in the gateway — and what comes
    back is cached. A hit still runs gateway.check (RBAC + guard, no
    execution, no audit) AND governance.filter_result on the cached frame, the
    same read-time re-filter chat applies to stored rows, so a policy change
    takes effect immediately — not within the TTL — and the cache can never be
    an RBAC or governance bypass; only the warehouse round trip and the audit
    row are skipped. Raises whatever the gateway raises."""
    role = user["role"]
    key, rkey = _cache_key(role, source, sql)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and not refresh and now - hit[0] < CACHE_TTL:
        gateway.check(user, source, sql, table_label=table_label, max_rows=_tile_cap())
        columns, rows = governance.filter_result(source, sql, hit[1], hit[2])
        return columns, rows, True

    client = _redis()
    if client is not None and not refresh:
        try:
            blob = client.get(rkey)
            payload = json.loads(blob) if blob else None
        except Exception:
            payload = None              # a dead Redis is a miss, never an error
        if payload:
            # Outside the try: a gateway refusal must propagate, not degrade
            # into a warehouse round trip that would then be refused anyway.
            gateway.check(user, source, sql, table_label=table_label, max_rows=_tile_cap())
            columns, rows = payload["columns"], payload["rows"]
            _remember(key, now, columns, rows)
            return governance.filter_result(source, sql, columns, rows) + (True,)

    result = gateway.execute(user, source, sql, "dashboard_tile",
                             table_label=table_label, max_rows=_tile_cap())
    columns, rows = list(result.columns or []), list(result.rows or [])
    if client is not None:
        # Round-trip through JSON even on a miss, so a cached read and a fresh
        # read hand downstream transforms identically-typed values.
        try:
            blob = json.dumps({"columns": columns, "rows": rows}, default=str)
            payload = json.loads(blob)
            columns, rows = payload["columns"], payload["rows"]
            client.setex(rkey, int(max(CACHE_TTL, 1)), blob)
        except Exception:
            pass
    _remember(key, now, columns, rows)
    return columns, rows, False


def _remember(key, now, columns, rows):
    _CACHE[key] = (now, columns, rows)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


def _affected_by(interaction, col):
    """"auto" listens to everything viz can honour; a list is an allow-list."""
    mode = _dict(interaction).get("affected_by", "auto")
    if mode == "none":
        return False
    if isinstance(mode, list):
        return col in mode
    return True


def tile_data(tile, user, filters=None, *, refresh=False):
    """Execute ONE tile for ONE viewer. NEVER raises — a per-tile failure is
    carried on the tile so one bad SQL string cannot blank the page."""
    t0 = time.time()
    out = {
        "tile_id": tile.get("id"), "title": tile.get("title") or "", "spec": None,
        "columns": [], "rows": [], "row_count": 0, "truncated": False,
        "applied_filters": [], "skipped_filters": [], "warnings": [],
        "cached": False, "denied": False, "error": None, "duration_ms": 0,
        # Unfiltered frame for the slicer catalog; popped before the wire.
        "_base": {"columns": [], "rows": []},
    }

    def fail(code, message, denied=False):
        out["error"] = {"code": code, "message": str(message)[:300]}
        out["denied"] = denied
        out["duration_ms"] = int((time.time() - t0) * 1000)
        return out

    source, table_label = tile.get("source"), tile.get("table_label")
    # RBAC → guard → cap → execution → governance → audit all happen inside
    # the gateway (via _cached_query); this function only maps its failures
    # onto the per-tile error contract.
    try:
        columns, rows, cached = _cached_query(user, source, tile.get("sql") or "",
                                              table_label=table_label or "*",
                                              refresh=refresh)
    except QueryRejected as e:
        # The gateway phrases every RBAC refusal as "no access"; anything
        # else is the query guard rejecting the SQL itself.
        denied = "no access" in str(e).lower()
        return fail("forbidden" if denied else "query_rejected", e, denied)
    except HTTPException as e:
        return fail("source_error", e.detail)   # unknown / unconfigured source
    except Exception as e:
        return fail("source_error", e)          # connector failure, timeout
    out["cached"] = cached
    out["_base"] = {"columns": columns, "rows": rows}

    try:
        spec, warnings = viz.sanitize_spec(tile.get("spec"), columns, rows)
        result = viz.run_transform(columns, rows, _dict(spec).get("transform"),
                                       injected=list(filters or []),
                                       max_rows=_tile_cap())
    except Exception as e:
        return fail("transform_error", e)

    out["spec"] = spec
    out["columns"] = _list(result.get("columns"))
    out["rows"] = _list(result.get("rows"))
    out["row_count"] = len(out["rows"])
    out["truncated"] = bool(result.get("truncated"))
    out["applied_filters"] = _list(result.get("injected"))
    out["skipped_filters"] = _list(result.get("skipped"))
    out["warnings"] = list(warnings or []) + _list(result.get("warnings"))
    out["duration_ms"] = int((time.time() - t0) * 1000)
    return out


def _merge_fields(catalog, tile_id, fields):
    """Union one tile's field catalog into the dashboard-wide slicer catalog."""
    for f in _list(fields):
        if not isinstance(f, dict) or not f.get("col"):
            continue
        cur = catalog.get(f["col"])
        if cur is None:
            catalog[f["col"]] = {**f, "tiles": [tile_id]}
            continue
        if tile_id not in cur["tiles"]:
            cur["tiles"].append(tile_id)
        if isinstance(cur.get("values"), list) and isinstance(f.get("values"), list):
            merged = cur["values"] + [v for v in f["values"] if v not in cur["values"]]
            cur["values"] = merged
            cur["value_count"] = len(merged)
        elif f.get("values") is None:
            cur["values"] = None


def render_dashboard(user, dashboard_id, filters=None, tile_ids=None, *,
                     refresh=False, include_fields=True):
    """Fan out over tiles. Always succeeds; per-tile failures ride on the tile."""
    t0 = time.time()
    dash = get_dashboard(dashboard_id) or {"tiles": []}
    wanted = set(tile_ids) if tile_ids else None
    wire = _filters(filters)

    tiles, catalog = [], {}
    for tile in dash.get("tiles", []):
        if wanted is not None and tile["id"] not in wanted:
            continue
        interaction = _dict(_dict(tile.get("spec")).get("interaction"))
        # A tile never filters itself; affected_by is its listen policy.
        injected = [f for f in wire
                    if f.get("source_tile") != tile["id"]
                    and _affected_by(interaction, f.get("col"))]
        data = tile_data(tile, user, injected, refresh=refresh)
        base = data.pop("_base", {"columns": [], "rows": []})
        if include_fields and not data["denied"] and base["columns"]:
            try:
                _merge_fields(catalog, tile["id"],
                              viz.field_catalog(base["columns"], base["rows"],
                                                    data.get("spec")))
            except Exception:
                pass  # a slicer catalog is never worth failing a render for
        tiles.append(data)

    # Fields come from UNFILTERED base rows, so selecting "West" never makes
    # the other regions vanish from the slicer. Shared fields sort first.
    fields = sorted(catalog.values(),
                    key=lambda f: (-len(f.get("tiles") or []), str(f.get("label") or f["col"])))
    took = int((time.time() - t0) * 1000)
    db.log_activity(user, "dashboard_view", prompt=dash.get("title"),
                    row_count=sum(t["row_count"] for t in tiles), duration_ms=took)
    return {
        "dashboard_id": dashboard_id, "generated_at": time.time(), "took_ms": took,
        "filters_applied": wire, "fields": fields if include_fields else [], "tiles": tiles,
    }


# ── Request bodies ──────────────────────────────────────────────────────

class DashboardIn(BaseModel):
    title: str
    description: str = ""
    visibility: str = "private"
    layout: Optional[dict] = None


class DashboardPatch(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    visibility: Optional[str] = None
    layout: Optional[dict] = None
    filters: Optional[List[Any]] = None  # cleaned by _filters — a bad predicate is dropped, not a 422


class TileIn(BaseModel):
    title: str = ""
    source: str
    table_label: Optional[str] = None
    sql: str
    spec: dict = {}
    layout: Optional[dict] = None
    position: Optional[int] = None
    # Optional current frame — only used to sanitize the spec against real columns.
    columns: Optional[List[str]] = None
    rows: Optional[List[list]] = None


class TilePatch(BaseModel):
    title: Optional[str] = None
    spec: Optional[dict] = None
    layout: Optional[dict] = None
    sql: Optional[str] = None
    source: Optional[str] = None
    table_label: Optional[str] = None
    position: Optional[int] = None
    spec_merge: bool = True
    columns: Optional[List[str]] = None
    rows: Optional[List[list]] = None


class PinIn(BaseModel):
    dashboard_id: Optional[str] = None
    dashboard_title: Optional[str] = None
    title: str = ""
    source: str
    table_label: Optional[str] = None
    sql: str
    spec: dict = {}
    # Rows are NOT stored — they only freeze the palette key order at pin time.
    columns: Optional[List[str]] = None
    rows: Optional[List[list]] = None


class LayoutIn(BaseModel):
    tiles: List[dict] = []


class DataIn(BaseModel):
    filters: Optional[List[Any]] = None
    tiles: Optional[List[str]] = None
    refresh: bool = False
    include_fields: bool = True


class TileDataIn(BaseModel):
    filters: Optional[List[Any]] = None
    refresh: bool = False


# ── Routes ──────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create(body: DashboardIn, user=Depends(current_user)):
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "Dashboard needs a title")
    dash = create_dashboard(user, title, body.description or "", body.visibility, body.layout)
    db.log_activity(user, "dashboard_save", prompt=title)
    return _public_dash(dash, user, tiles=[])


@router.get("")
def index(user=Depends(current_user)):
    return {"dashboards": list_dashboards(user)}


@router.post("/pin", status_code=201)
def pin(body: PinIn, user=Depends(current_user)):
    """One-click pin from Chat/Canvas: stores the recipe, never the rows."""
    spec = _dict(body.spec)
    title = (body.title or spec.get("title") or "Untitled chart").strip()
    # Ownership before the connector, and before creating anything.
    dash = _own_or_404(body.dashboard_id, user, edit=True) if body.dashboard_id else None
    _guard_tile(user, body.source, body.table_label, body.sql)
    created = dash is None
    if created:
        dash = create_dashboard(user, (body.dashboard_title or title or "Untitled dashboard"))

    spec = _capture_key_order(spec, _list(body.columns), _list(body.rows))
    tile, warnings = add_tile(dash["id"], {
        "title": title, "source": body.source, "table_label": body.table_label,
        "sql": body.sql, "spec": spec, "columns": body.columns, "rows": body.rows,
    })
    db.log_activity(user, "dashboard_pin", prompt=title, source=body.source,
                    table=body.table_label, sql=body.sql)
    return {"dashboard_id": dash["id"], "dashboard_title": dash["title"],
            "tile": _public_tile(tile), "created_dashboard": created, "warnings": warnings}


@router.get("/{did}")
def detail(did: str, user=Depends(current_user)):
    _own_or_404(did, user)
    dash = get_dashboard(did)
    return _public_dash(dash, user, tiles=[_public_tile(t) for t in dash["tiles"]])


@router.patch("/{did}")
def patch(did: str, body: DashboardPatch, user=Depends(current_user)):
    _own_or_404(did, user, edit=True)
    dash = update_dashboard(did, **body.model_dump(exclude_unset=True))
    db.log_activity(user, "dashboard_save", prompt=dash["title"])
    return _public_dash(dash, user, tiles=[_public_tile(t) for t in dash["tiles"]])


@router.delete("/{did}")
def remove(did: str, user=Depends(current_user)):
    dash = _own_or_404(did, user, edit=True)
    delete_dashboard(did)
    db.log_activity(user, "dashboard_delete", prompt=dash["title"])
    return {"deleted": True}


@router.post("/{did}/tiles", status_code=201)
def create_tile(did: str, body: TileIn, user=Depends(current_user)):
    _own_or_404(did, user, edit=True)
    _guard_tile(user, body.source, body.table_label, body.sql)
    tile, warnings = add_tile(did, body.model_dump())
    db.log_activity(user, "dashboard_save", prompt=tile["title"], source=body.source,
                    table=body.table_label, sql=body.sql)
    return {"tile": _public_tile(tile), "warnings": warnings}


@router.patch("/{did}/tiles/{tid}")
def patch_tile(did: str, tid: str, body: TilePatch, user=Depends(current_user)):
    _own_or_404(did, user, edit=True)
    existing = get_tile(did, tid)
    if not existing:
        raise HTTPException(404, "Tile not found")
    fields = body.model_dump(exclude_unset=True)
    # Re-point the SQL only against the EDITOR's allowed tables.
    if fields.get("sql") is not None or fields.get("source") is not None:
        _guard_tile(user, fields.get("source") or existing["source"],
                    fields.get("table_label", existing["table_label"]),
                    fields.get("sql") or existing["sql"])
    fields.setdefault("spec_merge", True)
    tile, warnings = update_tile(did, tid, **fields)
    if tile is None:
        raise HTTPException(404, "Tile not found")
    return {"tile": _public_tile(tile), "warnings": warnings}


@router.delete("/{did}/tiles/{tid}")
def remove_tile(did: str, tid: str, user=Depends(current_user)):
    _own_or_404(did, user, edit=True)
    if not get_tile(did, tid):
        raise HTTPException(404, "Tile not found")
    delete_tile(did, tid)
    return {"deleted": True}


@router.put("/{did}/layout")
def save_layout(did: str, body: LayoutIn, user=Depends(current_user)):
    _own_or_404(did, user, edit=True)
    return {"updated": set_layout(did, body.tiles)}


@router.post("/{did}/data")
def data(did: str, body: Optional[DataIn] = None, user=Depends(current_user)):
    """Render / refresh / cross-filter. Always 200 — failures ride on tiles."""
    _own_or_404(did, user)
    body = body or DataIn()
    return render_dashboard(user, did, body.filters, body.tiles,
                            refresh=body.refresh, include_fields=body.include_fields)


@router.post("/{did}/tiles/{tid}/data")
def single_tile_data(did: str, tid: str, body: Optional[TileDataIn] = None,
                     user=Depends(current_user)):
    _own_or_404(did, user)
    tile = get_tile(did, tid)
    if not tile:
        raise HTTPException(404, "Tile not found")
    body = body or TileDataIn()
    out = tile_data(tile, user, _filters(body.filters), refresh=body.refresh)
    out.pop("_base", None)
    return out
