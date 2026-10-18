"""Canonical entities across sources, domain ownership, and metadata freshness.

A business entity — a customer, an order, a product — usually has a key in
more than one source: crm.customers.id here, retail.orders.customer_id there.
This module proposes those mappings from the metadata graph, lets an
administrator or the owner of the data verify or reject each member, and lets
the rest of Studio use VERIFIED mappings only: the multi-source planners are
told which keys carry an entity between sources, and a cross-source blend can
join parts on them.

Design:
- PROPOSED, THEN VERIFIED. Naming proposes (a table named for "customer", a
  customer_id column) in at least two sources; nothing joins on a proposal.
- PERMISSION-AWARE. An entity is shown with only the members whose source and
  table the reader may read; one with no visible member is not shown at all,
  and nothing about a hidden member — its source, table, column or count — is
  disclosed.
- OWNED. A source or table can be given a domain and an owner; the owner may
  review relationships and entity members on what they own, administrators on
  everything.
- FRESHNESS IS MONITORED. Each source's metadata is fresh, stale, failing or
  unknown, with the reasons, and the scheduler logs every change.
"""
import json
import logging
import time
import uuid

from . import db, rbac
from .metarelations import _family, _named_for, _stem

log = logging.getLogger("studio.metagraph")


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta_entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            stem TEXT,
            description TEXT,
            domain TEXT,
            origin TEXT NOT NULL,
            created_by TEXT,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_entity_name ON meta_entities(name);
        CREATE TABLE IF NOT EXISTS meta_entity_members (
            id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            source TEXT NOT NULL,
            object TEXT NOT NULL,
            column_name TEXT NOT NULL,
            role TEXT NOT NULL,
            state TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence TEXT NOT NULL,
            origin TEXT NOT NULL,
            reviewed_by TEXT,
            reviewed_at REAL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            removed_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_member_key
            ON meta_entity_members(entity_id, source, object, column_name);
        CREATE TABLE IF NOT EXISTS meta_ownership (
            source TEXT NOT NULL,
            object TEXT NOT NULL,
            domain TEXT,
            owner_id TEXT,
            set_by TEXT,
            set_at REAL,
            PRIMARY KEY (source, object)
        );
        """)
        c.commit()


# ── Proposals ────────────────────────────────────────────────────────────

def _display(stem):
    return stem.replace("_", " ").capitalize()


def _live_objects(sources):
    """{(source, object): {"kind", "columns"}} for the current objects of `sources`."""
    out = {}
    if not sources:
        return out
    with db.connect() as c:
        ph = ",".join("?" for _ in sources)
        rows = c.execute(f"SELECT id, source, name, kind FROM meta_objects WHERE source IN ({ph}) "
                         f"AND removed_at IS NULL", tuple(sources)).fetchall()
        ids = {r["id"]: (r["source"], r["name"]) for r in rows}
        for r in rows:
            out[(r["source"], r["name"])] = {"kind": r["kind"], "columns": []}
        keys = list(ids)
        for i in range(0, len(keys), 400):
            batch = keys[i:i + 400]
            p2 = ",".join("?" for _ in batch)
            for col in c.execute(f"SELECT object_id, name, type FROM meta_columns WHERE "
                                 f"object_id IN ({p2}) ORDER BY object_id, ordinal",
                                 tuple(batch)).fetchall():
                out[ids[col["object_id"]]]["columns"].append(
                    {"name": col["name"], "type": col["type"]})
    return out


def candidates(objects):
    """{stem: [member]} — key columns that name an entity, across sources.
    A `<stem>_id` column is the HOME of the key in a table named for the stem
    (customers.customer_id) and a REFERENCE anywhere else
    (orders.customer_id); a table named for the stem and keyed by a bare `id`
    is a home too (crm.customers.id). Proposed only when the stem spans at
    least two sources and has a home; members whose type family differs from
    the home's (a text key against a number key) are left out."""
    found = {}
    for (source, obj), info in objects.items():
        if info["kind"] == "view":
            continue
        bare = obj.split(".")[-1]
        for col in info["columns"]:
            stem = _stem(col["name"])
            if not stem:
                continue
            home = _named_for(obj, stem)
            found.setdefault(stem, []).append({
                "source": source, "object": obj, "column": col["name"],
                "role": "home" if home else "reference", "type": col.get("type"),
                "evidence": [f"table {bare} is named for {stem}" if home
                             else f"column {bare}.{col['name']} names {stem}"]})
    for (source, obj), info in objects.items():
        if info["kind"] == "view":
            continue
        bare_id = next((c for c in info["columns"] if c["name"].lower() == "id"), None)
        if bare_id is None:
            continue
        for stem in list(found):
            has_home = any(m["source"] == source and m["object"] == obj and m["role"] == "home"
                           for m in found[stem])
            if _named_for(obj, stem) and not has_home:
                found[stem].append({
                    "source": source, "object": obj, "column": bare_id["name"],
                    "role": "home", "type": bare_id.get("type"),
                    "evidence": [f"table {obj.split('.')[-1]} is named for {stem} "
                                 f"and keyed by id"]})
    out = {}
    for stem, members in found.items():
        homes = [m for m in members if m["role"] == "home"]
        if len({m["source"] for m in members}) < 2 or not homes:
            continue
        family = _family(homes[0].get("type"))
        kept = [m for m in members
                if family == "unknown" or _family(m.get("type")) in ("unknown", family)]
        sources = {m["source"] for m in kept}
        if len(sources) < 2:
            continue
        out[stem] = [{**m, "confidence": 0.75 if m["role"] == "home" else 0.6,
                      "evidence": m["evidence"] + [f"found in {len(sources)} sources"]}
                     for m in kept]
    return out


def propose(sources):
    """Refresh proposed entities from the current objects of `sources` (the
    live ones). Reviews stand: a verified or rejected member keeps its state;
    a member whose table is gone is removed. Returns counts."""
    objects = _live_objects(sources)
    proposals = candidates(objects)
    now = time.time()
    added = 0
    with db.connect() as c:
        entities = {r["stem"]: dict(r) for r in c.execute(
            "SELECT * FROM meta_entities WHERE stem IS NOT NULL").fetchall()}
        seen = set()
        for stem, members in proposals.items():
            ent = entities.get(stem)
            if ent is None:
                name = _display(stem)
                if c.execute("SELECT 1 FROM meta_entities WHERE name=?", (name,)).fetchone():
                    continue                               # a manual entity owns the name
                ent = {"id": str(uuid.uuid4())}
                c.execute("INSERT INTO meta_entities (id, name, stem, origin, created_at) "
                          "VALUES (?,?,?,?,?)", (ent["id"], name, stem, "inferred", now))
            for m in members:
                key = (ent["id"], m["source"], m["object"], m["column"])
                seen.add(key)
                row = c.execute("SELECT id, state FROM meta_entity_members WHERE entity_id=? "
                                "AND source=? AND object=? AND column_name=?", key).fetchone()
                if row is None:
                    c.execute(
                        "INSERT INTO meta_entity_members (id, entity_id, source, object, "
                        "column_name, role, state, confidence, evidence, origin, first_seen_at, "
                        "last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), *key, m["role"], "proposed", m["confidence"],
                         json.dumps(m["evidence"]), "inferred", now, now))
                    added += 1
                else:
                    c.execute("UPDATE meta_entity_members SET last_seen_at=?, removed_at=NULL, "
                              "evidence=?, role=? WHERE id=?",
                              (now, json.dumps(m["evidence"]), m["role"], row["id"]))
        # An inferred member whose table or column is gone is removed; a
        # manual one stays until its table is gone.
        for row in c.execute("SELECT * FROM meta_entity_members WHERE removed_at IS NULL "
                             "AND source IN (" + ",".join("?" for _ in sources) + ")",
                             tuple(sources)).fetchall() if sources else []:
            key = (row["entity_id"], row["source"], row["object"], row["column_name"])
            info = objects.get((row["source"], row["object"]))
            gone = info is None or row["column_name"].lower() not in {
                col["name"].lower() for col in info["columns"]}
            if gone or (row["origin"] == "inferred" and key not in seen
                        and row["state"] == "proposed"):
                c.execute("UPDATE meta_entity_members SET removed_at=? WHERE id=?",
                          (now, row["id"]))
        c.commit()
    return {"entities": len(proposals), "added": added}


# ── Reading, permission-aware ────────────────────────────────────────────

def _readable(role, source, obj, live):
    return source in live and source in rbac.allowed_sources(role) and \
        obj in set(rbac.allowed_tables(role, source, [obj]))


def visible(user, live_sources, *, include_rejected=False):
    """Entities with the members this user may see. Nothing about a hidden
    member — its source, table, column — or even how many there are leaves."""
    live = set(live_sources)
    role = user["role"]
    with db.connect() as c:
        ents = [dict(r) for r in c.execute("SELECT * FROM meta_entities ORDER BY name").fetchall()]
        members = [dict(r) for r in c.execute(
            "SELECT * FROM meta_entity_members WHERE removed_at IS NULL "
            "ORDER BY source, object").fetchall()]
    out = []
    for ent in ents:
        shown = []
        for m in members:
            if m["entity_id"] != ent["id"]:
                continue
            if m["state"] == "rejected" and not include_rejected:
                continue
            if not _readable(role, m["source"], m["object"], live):
                continue
            shown.append({"id": m["id"], "source": m["source"], "object": m["object"],
                          "column": m["column_name"], "role": m["role"], "state": m["state"],
                          "confidence": 1.0 if m["state"] == "verified" else m["confidence"],
                          "evidence": json.loads(m["evidence"])})
        if shown:
            out.append({"id": ent["id"], "name": ent["name"], "description": ent["description"],
                        "domain": ent["domain"], "members": shown})
    return out


def verified_bridges(user, live_sources):
    """Verified member pairs in DIFFERENT sources, both readable by the user:
    [(entity name, member a, member b)]."""
    out = []
    for ent in visible(user, live_sources):
        ok = [m for m in ent["members"] if m["state"] == "verified"]
        for i, a in enumerate(ok):
            for b in ok[i + 1:]:
                if a["source"] != b["source"]:
                    out.append((ent["name"], a, b))
    return out


def bridge_lines(user, live_sources, relevant=None):
    """The planner's hint lines, optionally only for pairs touching `relevant`
    {(source, object)}."""
    lines = []
    for name, a, b in verified_bridges(user, live_sources):
        if relevant and (a["source"], a["object"]) not in relevant \
                and (b["source"], b["object"]) not in relevant:
            continue
        lines.append(f"{name}: {a['source']}.{a['object']}.{a['column']} = "
                     f"{b['source']}.{b['object']}.{b['column']}  (verified)")
    return lines


def join_columns(parts, user):
    """For a cross-source blend: {part name: key column} when one verified
    entity has a member in every part's source and table whose column the part
    returns. None when no single entity spans every part."""
    from . import queryguard
    live = {p["source"] for p in parts}
    found = None
    for ent in visible(user, live):
        keys = {}
        ok = [m for m in ent["members"] if m["state"] == "verified"]
        for p in parts:
            tables = {t.lower() for t in (queryguard.base_tables(p.get("sql") or "") or [])}
            if p.get("table"):
                tables.add(str(p["table"]).lower())
            cols = {c.lower(): c for c in p["columns"]}
            hit = next((cols[m["column"].lower()] for m in ok
                        if m["source"] == p["source"]
                        and m["object"].split(".")[-1].lower() in
                        {t.split(".")[-1] for t in tables}
                        and m["column"].lower() in cols), None)
            if hit is None:
                break
            keys[p["name"]] = hit
        else:
            if len({p["source"] for p in parts}) > 1:
                found = (ent["name"], keys)
                break
    return found


def review_member(member_id, decision, user):
    if decision not in ("verified", "rejected", "proposed"):
        raise ValueError("decision must be verified, rejected or proposed")
    with db.connect() as c:
        row = c.execute("SELECT * FROM meta_entity_members WHERE id=? AND removed_at IS NULL",
                        (member_id,)).fetchone()
        if row is None:
            return None
        if not may_review(user, row["source"], row["object"]):
            raise PermissionError("not yours to review")
        c.execute("UPDATE meta_entity_members SET state=?, reviewed_by=?, reviewed_at=? "
                  "WHERE id=?", (decision, user["id"], time.time(), member_id))
        c.commit()
    return dict(row)


def create_entity(name, description, domain, user):
    name = " ".join(str(name or "").split())[:80]
    if not name:
        raise ValueError("name an entity")
    eid = str(uuid.uuid4())
    with db.connect() as c:
        if c.execute("SELECT 1 FROM meta_entities WHERE name=?", (name,)).fetchone():
            raise ValueError(f"an entity named {name} exists")
        c.execute("INSERT INTO meta_entities (id, name, description, domain, origin, created_by, "
                  "created_at) VALUES (?,?,?,?,?,?,?)",
                  (eid, name, (description or "")[:500] or None, (domain or "")[:80] or None,
                   "manual", user["id"], time.time()))
        c.commit()
    return eid


def add_member(entity_id, source, obj, column, user):
    """A member placed by hand is verified at once — by someone allowed to."""
    if not may_review(user, source, obj):
        raise PermissionError("not yours to map")
    now = time.time()
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM meta_entities WHERE id=?", (entity_id,)).fetchone():
            return None
        exists = c.execute(
            "SELECT 1 FROM meta_objects o JOIN meta_columns col ON col.object_id = o.id "
            "WHERE o.source=? AND o.name=? AND o.removed_at IS NULL AND col.name=?",
            (source, obj, column)).fetchone()
        if not exists:
            raise ValueError("no such column in the metadata graph")
        mid = str(uuid.uuid4())
        c.execute("INSERT INTO meta_entity_members (id, entity_id, source, object, column_name, "
                  "role, state, confidence, evidence, origin, reviewed_by, reviewed_at, "
                  "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (mid, entity_id, source, obj, column, "home", "verified", 1.0,
                   json.dumps(["mapped by hand"]), "manual", user["id"], now, now, now))
        c.commit()
    return mid


# ── Ownership ────────────────────────────────────────────────────────────

def ownership(source):
    """{object ('' for the source itself): {"domain", "owner_id"}}."""
    with db.connect() as c:
        return {r["object"]: {"domain": r["domain"], "owner_id": r["owner_id"]}
                for r in c.execute("SELECT * FROM meta_ownership WHERE source=?",
                                   (source,)).fetchall()}


def owner_of(source, obj):
    """The object's own owner, else its source's."""
    own = ownership(source)
    entry = own.get(obj) or own.get("") or {}
    return entry.get("owner_id"), (own.get(obj) or {}).get("domain") or \
        (own.get("") or {}).get("domain")


def may_review(user, source, obj):
    if user["role"] == "admin":
        return True
    owner, _domain = owner_of(source, obj)
    return owner is not None and owner == user["id"]


def set_ownership(source, obj, domain, owner_id, user):
    with db.connect() as c:
        c.execute("DELETE FROM meta_ownership WHERE source=? AND object=?", (source, obj or ""))
        if domain or owner_id:
            c.execute("INSERT INTO meta_ownership (source, object, domain, owner_id, set_by, "
                      "set_at) VALUES (?,?,?,?,?,?)",
                      (source, obj or "", (domain or "")[:80] or None, owner_id, user["id"],
                       time.time()))
        c.commit()


# ── Freshness ────────────────────────────────────────────────────────────

def freshness(state, now, *, tick_s, full_s):
    """("fresh" | "stale" | "failing" | "unknown", [reasons]) for one source."""
    if not state or not state.get("ok_at"):
        if state and state.get("status") == "failed":
            return "failing", [f"discovery failing: {state.get('error') or 'unknown error'}"]
        return "unknown", ["not discovered yet"]
    reasons = []
    if state.get("status") == "failed":
        reasons.append(f"the last discovery failed: {state.get('error') or 'unknown error'}")
    if state.get("relate_error"):
        reasons.append(f"relationships failed: {state['relate_error']}")
    if state.get("mirror_status") == "failed":
        reasons.append(f"Neo4j mirror failed: {state.get('mirror_error') or ''}".strip())
    if reasons:
        return "failing", reasons
    age = now - state["ok_at"]
    if age > 2 * full_s:
        reasons.append(f"last discovered {int(age // 3600)} h ago")
    checked = state.get("checked_at")
    if checked and now - checked > 3 * tick_s + 60:
        reasons.append(f"not checked for {int((now - checked) // 60)} min — is the scheduler "
                       f"running?")
    if state.get("held_removals") and now - (state.get("held_at") or now) > 86400:
        reasons.append("a held removal has waited more than a day")
    return ("stale", reasons) if reasons else ("fresh", [])
