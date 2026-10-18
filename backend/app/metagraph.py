"""Metadata knowledge graph — what every connected source contains, discovered
automatically and kept as snapshots.

Saving a connection queues a `metagraph_discover` job, in the same
transaction as the connection row (connections.on_source_saved). The job lists
the source's tables and views, reads their columns, fingerprints each object,
and records a snapshot: what was added, changed and removed since the last one. The result is a graph —
source → namespace → table/view → column — readable through /api/metagraph
and mirrored to Neo4j when one is configured for it.

Design:
- STUDIO'S DATABASE IS THE SYSTEM OF RECORD. Snapshots, objects and columns
  live in the app DB (SQLite or Postgres), so discovery works with no Neo4j at
  all, and permission checks reuse Studio's own rules. Neo4j is a mirror,
  rewritten from the record after each successful snapshot.
- THE SOURCE'S OWN SCOPE. list_tables() is the authoritative object set —
  exactly what the catalog, RBAC and the agent already see. Bulk metadata
  (Connector.describe_relations) only enriches it with kinds and columns;
  anything it reports beyond that set is ignored.
- METADATA ONLY. Names, kinds, column names and types. Never a row of data.
- PERMISSION-AWARE READS. The API shows a source only to a role that may use
  it and an object only to a role allowed to read it (rbac.allowed_tables).
  Counts are computed over what the caller can see, so they leak nothing.
- SAFE. A failed listing changes nothing. An object is marked removed only by
  a complete, successful snapshot — never by a truncated listing or one where
  columns could not be read.
- BOUNDED. At most STUDIO_METAGRAPH_MAX_OBJECTS objects and
  STUDIO_METAGRAPH_MAX_COLUMNS columns per object per snapshot.
- INERT. Names and types are data: bound as SQL parameters and passed to
  Neo4j as Cypher parameters, never interpolated into a query.
"""
import hashlib
import json
import logging
import os
import time
import urllib.request
import uuid

from fastapi import APIRouter, Depends, HTTPException

from . import connections, db, jobs, rbac
from .auth import current_user

log = logging.getLogger("studio.metagraph")

router = APIRouter(prefix="/metagraph", tags=["metagraph"])

JOB_KIND = "metagraph_discover"


def _max_objects():
    return max(1, int(os.getenv("STUDIO_METAGRAPH_MAX_OBJECTS", "5000")))


def _max_columns():
    return max(1, int(os.getenv("STUDIO_METAGRAPH_MAX_COLUMNS", "1000")))


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta_snapshots (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at REAL NOT NULL,
            finished_at REAL,
            objects INTEGER,
            added INTEGER,
            changed INTEGER,
            removed INTEGER,
            complete INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_meta_snap_source ON meta_snapshots(source, started_at);
        CREATE TABLE IF NOT EXISTS meta_objects (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            name TEXT NOT NULL,
            namespace TEXT,
            kind TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            column_count INTEGER NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL,
            changed_at REAL NOT NULL,
            removed_at REAL,
            snapshot_id TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_meta_obj_key ON meta_objects(source, name);
        CREATE TABLE IF NOT EXISTS meta_columns (
            object_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_meta_col_obj ON meta_columns(object_id);
        CREATE TABLE IF NOT EXISTS meta_sources (
            source TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            snapshot_id TEXT,
            started_at REAL,
            ok_at REAL,
            error TEXT,
            mirror_status TEXT,
            mirror_error TEXT,
            mirrored_at REAL
        );
        """)
        c.commit()


# ── Discovery ────────────────────────────────────────────────────────────

def enqueue(source, trigger, *, conn=None):
    """Queue discovery of one source and mark it queued. `conn` enlists both in
    the caller's open transaction (a connection save), so the row, its job and
    its status land together."""
    job_id = jobs.enqueue(JOB_KIND, {"source": source, "trigger": trigger},
                          max_attempts=3, conn=conn)
    if conn is not None:
        _mark_queued(conn, source)
    else:
        with db.connect() as c:
            _mark_queued(c, source)
            c.commit()
    return job_id


def _mark_queued(c, source):
    """'queued' until the job starts — unless a discovery is running now, in
    which case it stays 'running' (the queued one follows it)."""
    row = c.execute("SELECT status FROM meta_sources WHERE source=?", (source,)).fetchone()
    if row is None:
        c.execute("INSERT INTO meta_sources (source, status) VALUES (?, 'queued')", (source,))
    elif row["status"] != "running":
        c.execute("UPDATE meta_sources SET status='queued' WHERE source=?", (source,))


@connections.on_source_saved
def _discover_saved_source(name, trigger, conn=None):
    """A connection was saved or reconnected: queue its discovery."""
    enqueue(name, trigger, conn=conn)


def _fingerprint(kind, columns):
    """Changes when the object's kind or any column's name, type or order does."""
    text = kind + "\n" + "\n".join(f"{c['name']}:{c.get('type') or ''}" for c in columns)
    return hashlib.sha256(text.encode()).hexdigest()


def _namespace_of(name, described):
    if described and described.get("namespace"):
        return described["namespace"]
    parts = str(name).split(".")
    return ".".join(parts[:-1]) or None


def _clean_columns(columns):
    out = []
    for col in (columns or [])[:_max_columns()]:
        name = str(col.get("name") or "")[:256]
        if name:
            out.append({"name": name, "type": str(col.get("type") or "")[:128]})
    return out


def _safe_error(exc):
    """A short, single-line reason. Connector errors can be long and noisy; the
    full one is logged, the stored one is for an admin's status line."""
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:300]


def _read_source(source):
    """List and describe a source: (objects, complete). Raises when the source
    cannot be listed at all. An object whose columns could not be read is
    returned with columns=None, and makes the snapshot incomplete."""
    from .connectors import get_connector
    connector = get_connector(source)
    if not connector.configured():
        raise RuntimeError(f"source '{source}' is not configured")
    names = list(connector.list_tables())
    cap = _max_objects()
    complete = len(names) <= cap and not getattr(connector, "tables_truncated", False)
    names = names[:cap]
    try:
        described = connector.describe_relations() or {}
    except Exception:
        log.warning("metagraph: bulk describe failed for %s", source, exc_info=True)
        described = {}
    objects = []
    for name in names:
        info = described.get(name)
        if info is not None:
            kind, columns = info.get("kind") or "other", info.get("columns") or []
        else:
            kind = "unknown"
            try:
                columns = connector.get_schema(name)
            except Exception:
                log.warning("metagraph: could not read columns of %s.%s", source, name,
                            exc_info=True)
                columns, complete = None, False
        objects.append({"name": str(name)[:512], "kind": kind,
                        "namespace": _namespace_of(name, info),
                        "columns": None if columns is None else _clean_columns(columns)})
    return objects, complete


def _lock(c, source):
    """One writer per source on Postgres (SQLite already has one writer)."""
    if db.IS_PG:
        key = int.from_bytes(hashlib.sha256(("metagraph:" + source).encode()).digest()[:8],
                             "big", signed=True)
        c.execute("SELECT pg_advisory_xact_lock(?)", (key,))


def _write(source, snapshot_id, objects, complete, now):
    """Apply one snapshot in one transaction. Returns (added, changed, removed)."""
    added = changed = removed = 0
    with db.connect() as c:
        _lock(c, source)
        existing = {r["name"]: dict(r) for r in c.execute(
            "SELECT id, name, fingerprint, removed_at FROM meta_objects WHERE source=?",
            (source,)).fetchall()}
        seen = set()
        for obj in objects:
            seen.add(obj["name"])
            row = existing.get(obj["name"])
            columns = obj["columns"]
            if columns is None and row is not None:
                # Columns unreadable this time: keep what we knew.
                c.execute("UPDATE meta_objects SET last_seen_at=?, snapshot_id=? WHERE id=?",
                          (now, snapshot_id, row["id"]))
                continue
            columns = columns or []
            fp = _fingerprint(obj["kind"], columns)
            if row is None:
                oid = str(uuid.uuid4())
                c.execute(
                    "INSERT INTO meta_objects (id, source, name, namespace, kind, fingerprint, "
                    "column_count, first_seen_at, last_seen_at, changed_at, removed_at, "
                    "snapshot_id) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)",
                    (oid, source, obj["name"], obj["namespace"], obj["kind"], fp, len(columns),
                     now, now, now, snapshot_id))
                _put_columns(c, oid, columns)
                added += 1
            elif row["fingerprint"] != fp or row["removed_at"] is not None:
                c.execute(
                    "UPDATE meta_objects SET namespace=?, kind=?, fingerprint=?, column_count=?, "
                    "last_seen_at=?, changed_at=?, removed_at=NULL, snapshot_id=? WHERE id=?",
                    (obj["namespace"], obj["kind"], fp, len(columns), now, now, snapshot_id,
                     row["id"]))
                c.execute("DELETE FROM meta_columns WHERE object_id=?", (row["id"],))
                _put_columns(c, row["id"], columns)
                if row["removed_at"] is not None:
                    added += 1
                else:
                    changed += 1
            else:
                c.execute("UPDATE meta_objects SET last_seen_at=?, snapshot_id=? WHERE id=?",
                          (now, snapshot_id, row["id"]))
        if complete:
            for name, row in existing.items():
                if name not in seen and row["removed_at"] is None:
                    c.execute("UPDATE meta_objects SET removed_at=? WHERE id=?",
                              (now, row["id"]))
                    removed += 1
        c.execute(
            "UPDATE meta_snapshots SET status=?, finished_at=?, objects=?, added=?, changed=?, "
            "removed=?, complete=? WHERE id=?",
            ("ok" if complete else "partial", time.time(), len(objects), added, changed,
             removed, 1 if complete else 0, snapshot_id))
        c.execute("UPDATE meta_sources SET status=?, snapshot_id=?, ok_at=?, error=NULL "
                  "WHERE source=?", ("ok" if complete else "partial", snapshot_id, now, source))
        c.commit()
    return added, changed, removed


def _put_columns(c, object_id, columns):
    for i, col in enumerate(columns):
        c.execute("INSERT INTO meta_columns (object_id, ordinal, name, type) VALUES (?,?,?,?)",
                  (object_id, i, col["name"], col["type"]))


def _start(source, snapshot_id, trigger, now):
    with db.connect() as c:
        c.execute("INSERT INTO meta_snapshots (id, source, trigger, status, started_at) "
                  "VALUES (?,?,?,?,?)", (snapshot_id, source, trigger, "running", now))
        cur = c.execute("UPDATE meta_sources SET status='running', started_at=? WHERE source=?",
                        (now, source))
        if not cur.rowcount:
            c.execute("INSERT INTO meta_sources (source, status, started_at) VALUES (?,?,?)",
                      (source, "running", now))
        c.commit()


def _fail(source, snapshot_id, error):
    with db.connect() as c:
        c.execute("UPDATE meta_snapshots SET status='failed', finished_at=?, error=? WHERE id=?",
                  (time.time(), error, snapshot_id))
        c.execute("UPDATE meta_sources SET status='failed', error=? WHERE source=?",
                  (error, source))
        c.commit()


def discover(source, trigger="manual"):
    """Discover one source now and record a snapshot. Returns its summary.
    Raises when the source cannot be listed (the job then retries); the failed
    snapshot is recorded either way."""
    snapshot_id, now = str(uuid.uuid4()), time.time()
    _start(source, snapshot_id, trigger, now)
    try:
        objects, complete = _read_source(source)
        added, changed, removed = _write(source, snapshot_id, objects, complete, now)
    except Exception as exc:
        log.warning("metagraph: discovery of %s failed", source, exc_info=True)
        _fail(source, snapshot_id, _safe_error(exc))
        raise
    mirror(source)
    return {"source": source, "snapshot_id": snapshot_id, "objects": len(objects),
            "added": added, "changed": changed, "removed": removed, "complete": complete}


@jobs.handler(JOB_KIND)
def _discover_job(payload, job=None):
    payload = payload or {}
    return discover(payload["source"], payload.get("trigger") or "job")


# ── Reading the graph (permission-aware) ─────────────────────────────────

def _state(source):
    with db.connect() as c:
        row = c.execute("SELECT * FROM meta_sources WHERE source=?", (source,)).fetchone()
    return dict(row) if row else None


def visible_objects(user, source, *, with_columns=True):
    """The current objects of `source` this user may read, with their columns."""
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, name, namespace, kind, column_count, first_seen_at, changed_at "
            "FROM meta_objects WHERE source=? AND removed_at IS NULL ORDER BY name",
            (source,)).fetchall()]
        allowed = set(rbac.allowed_tables(user["role"], source, [r["name"] for r in rows]))
        rows = [r for r in rows if r["name"] in allowed]
        if with_columns and rows:
            cols = {}
            ids = [r["id"] for r in rows]
            for i in range(0, len(ids), 400):
                batch = ids[i:i + 400]
                ph = ",".join("?" for _ in batch)
                for col in c.execute(
                        f"SELECT object_id, name, type FROM meta_columns WHERE object_id IN ({ph}) "
                        f"ORDER BY object_id, ordinal", tuple(batch)).fetchall():
                    cols.setdefault(col["object_id"], []).append(
                        {"name": col["name"], "type": col["type"]})
            for r in rows:
                r["columns"] = cols.get(r["id"], [])
    for r in rows:
        r.pop("id", None)
    return rows


def _visible_source(user, source):
    """404 for a source this user cannot use — indistinguishable from one that
    does not exist."""
    if source not in rbac.allowed_sources(user["role"]):
        raise HTTPException(404, "Not found")


def _summary(user, source, objects=None):
    objects = visible_objects(user, source, with_columns=False) if objects is None else objects
    state = _state(source) or {"status": "not_discovered"}
    kinds = {}
    for o in objects:
        kinds[o["kind"]] = kinds.get(o["kind"], 0) + 1
    out = {"source": source, "status": state.get("status"), "discovered_at": state.get("ok_at"),
           "objects": len(objects), "kinds": kinds}
    if user["role"] == "admin":
        out.update({"error": state.get("error"), "mirror_status": state.get("mirror_status"),
                    "mirror_error": state.get("mirror_error")})
    return out


@router.get("/sources")
def list_sources(user=Depends(current_user)):
    """Discovery status of every source this user may use."""
    from .connectors import all_sources
    names = [s["name"] for s in all_sources() if s.get("configured")]
    allowed = set(rbac.allowed_sources(user["role"]))
    return {"sources": [_summary(user, n) for n in names if n in allowed]}


@router.get("/sources/{source}")
def get_source(source: str, user=Depends(current_user)):
    """The objects and columns of one source that this user may read."""
    _visible_source(user, source)
    objects = visible_objects(user, source)
    return {**_summary(user, source, objects), "relations": objects}


@router.get("/sources/{source}/graph")
def source_graph(source: str, user=Depends(current_user)):
    """The same, as nodes and edges: source → namespace → object → column."""
    _visible_source(user, source)
    nodes, edges = [{"id": f"source:{source}", "type": "source", "name": source}], []
    namespaces = set()
    for obj in visible_objects(user, source):
        parent = f"source:{source}"
        if obj["namespace"]:
            ns_id = f"namespace:{source}:{obj['namespace']}"
            if ns_id not in namespaces:
                namespaces.add(ns_id)
                nodes.append({"id": ns_id, "type": "namespace", "name": obj["namespace"]})
                edges.append({"from": parent, "to": ns_id, "type": "CONTAINS"})
            parent = ns_id
        obj_id = f"object:{source}:{obj['name']}"
        nodes.append({"id": obj_id, "type": obj["kind"], "name": obj["name"]})
        edges.append({"from": parent, "to": obj_id, "type": "CONTAINS"})
        for col in obj["columns"]:
            col_id = f"column:{source}:{obj['name']}:{col['name']}"
            nodes.append({"id": col_id, "type": "column", "name": col["name"],
                          "data_type": col["type"]})
            edges.append({"from": obj_id, "to": col_id, "type": "HAS_COLUMN"})
    return {"source": source, "nodes": nodes, "edges": edges}


@router.post("/sources/{source}/discover", status_code=202)
def request_discovery(source: str, user=Depends(current_user)):
    """Queue a fresh discovery of one source (administrators)."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only administrators can run discovery")
    from .connectors import all_sources
    if source not in {s["name"] for s in all_sources() if s.get("configured")}:
        raise HTTPException(404, "Not found")
    return {"job_id": enqueue(source, "manual"), "source": source}


# ── Neo4j mirror ─────────────────────────────────────────────────────────

class MirrorRefused(RuntimeError):
    pass


def _mirror_cfg():
    """The Neo4j the graph is mirrored to, or None when not configured. It
    must not be the database agents query as the `neo4j` data source: there
    its labels would surface as tables, outside the metadata permissions."""
    cfg = {"uri": os.getenv("METAGRAPH_NEO4J_URI", "").strip().rstrip("/"),
           "user": os.getenv("METAGRAPH_NEO4J_USER", "").strip(),
           "password": os.getenv("METAGRAPH_NEO4J_PASSWORD", ""),
           "database": os.getenv("METAGRAPH_NEO4J_DATABASE", "").strip() or "neo4j"}
    if not (cfg["uri"] and cfg["user"] and cfg["password"]):
        return None
    source_uri = os.getenv("NEO4J_URI", "").strip().rstrip("/")
    source_db = os.getenv("NEO4J_DATABASE", "").strip() or "neo4j"
    if source_uri and source_uri == cfg["uri"] and source_db == cfg["database"]:
        raise MirrorRefused("METAGRAPH_NEO4J_* points at the database agents query as the "
                            "neo4j source; use a separate database or instance")
    return cfg


def _neo4j_commit(cfg, statements):
    import base64
    url = f"{cfg['uri']}/db/{cfg['database']}/tx/commit"
    body = json.dumps({"statements": statements}).encode()
    auth = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode()).decode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Basic {auth}", "Content-Type": "application/json",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode())
    if out.get("errors"):
        first = out["errors"][0]
        raise RuntimeError(f"{first.get('code', 'Neo4jError')}: {first.get('message', '')}"[:300])
    return out


def _projection(source):
    """Every current object and column of a source, straight from the record —
    the mirror is a projection of it, not a second source of truth."""
    with db.connect() as c:
        objects = [dict(r) for r in c.execute(
            "SELECT id, name, namespace, kind, fingerprint, column_count FROM meta_objects "
            "WHERE source=? AND removed_at IS NULL", (source,)).fetchall()]
        names = {o["id"]: o["name"] for o in objects}
        columns = []
        ids = list(names)
        for i in range(0, len(ids), 400):
            batch = ids[i:i + 400]
            ph = ",".join("?" for _ in batch)
            for col in c.execute(f"SELECT object_id, ordinal, name, type FROM meta_columns "
                                 f"WHERE object_id IN ({ph})", tuple(batch)).fetchall():
                columns.append({"object": names[col["object_id"]], "name": col["name"],
                                "type": col["type"], "ordinal": col["ordinal"]})
    for o in objects:
        o.pop("id", None)
    return objects, columns


def mirror(source):
    """Rewrite one source's subgraph in Neo4j from the record, in one Neo4j
    transaction. Best-effort: a failure is recorded on the source's status and
    never fails discovery. Labels are fixed; every value is a parameter."""
    try:
        cfg = _mirror_cfg()
    except MirrorRefused as exc:
        _mirror_state(source, "refused", str(exc))
        return False
    if cfg is None:
        _mirror_state(source, "off", None)
        return False
    objects, columns = _projection(source)
    params = {"source": source, "now": time.time(), "objects": objects, "columns": columns,
              "names": [o["name"] for o in objects],
              "pairs": [[col["object"], col["name"]] for col in columns]}
    statements = [
        {"statement": "MERGE (s:StudioSource {name: $source}) SET s.synced_at = $now",
         "parameters": params},
        {"statement": "UNWIND $objects AS o "
                      "MERGE (t:StudioObject {source: $source, name: o.name}) "
                      "SET t.kind = o.kind, t.namespace = o.namespace, "
                      "t.fingerprint = o.fingerprint, t.column_count = o.column_count "
                      "WITH t MATCH (s:StudioSource {name: $source}) MERGE (s)-[:CONTAINS]->(t)",
         "parameters": params},
        {"statement": "UNWIND $columns AS col "
                      "MATCH (t:StudioObject {source: $source, name: col.object}) "
                      "MERGE (c:StudioColumn {source: $source, object: col.object, name: col.name}) "
                      "SET c.type = col.type, c.ordinal = col.ordinal "
                      "MERGE (t)-[:HAS_COLUMN]->(c)",
         "parameters": params},
        {"statement": "MATCH (c:StudioColumn {source: $source}) "
                      "WHERE NOT [c.object, c.name] IN $pairs DETACH DELETE c",
         "parameters": params},
        {"statement": "MATCH (t:StudioObject {source: $source}) "
                      "WHERE NOT t.name IN $names DETACH DELETE t",
         "parameters": params},
    ]
    try:
        _neo4j_commit(cfg, statements)
    except Exception as exc:
        log.warning("metagraph: Neo4j mirror of %s failed", source, exc_info=True)
        _mirror_state(source, "failed", _safe_error(exc))
        return False
    _mirror_state(source, "ok", None)
    return True


def _mirror_state(source, status, error):
    with db.connect() as c:
        c.execute("UPDATE meta_sources SET mirror_status=?, mirror_error=?, mirrored_at=? "
                  "WHERE source=?", (status, error, time.time() if status == "ok" else None,
                                     source))
        c.commit()
