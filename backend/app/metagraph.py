"""Metadata knowledge graph — what every connected source contains, discovered
automatically and kept as snapshots.

Saving a connection queues a `metagraph_discover` job, in the same
transaction as the connection row (connections.on_source_saved). The job lists
the source's tables and views, reads their columns, fingerprints each object,
and records a snapshot: what was added, changed and removed since the last
one. The result is a graph — source → namespace → table/view → column —
readable through /api/metagraph and mirrored to Neo4j when one is configured.

It then stays current with no one asking (tick_once, the "metagraph_sync"
scheduler): every few minutes each source's cheap change signal
(Connector.change_signal) is compared with the one its last discovery saw, and
only a source whose signal moved — or whose last full discovery is too old —
is rediscovered. Events also queue one: a connection saved or reconnected, a
supervised write against a source, or a signed webhook (/metagraph/events).

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
  columns could not be read. A large removal (half the source, say — what a
  revoked grant or a half-broken listing looks like) is HELD until a later
  complete snapshot confirms the same set. Removed objects are tombstones,
  hidden at once and purged only after STUDIO_METAGRAPH_TOMBSTONE_DAYS.
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

from fastapi import APIRouter, Depends, HTTPException, Request

from . import connections, db, jobs, rbac
from .auth import current_user

log = logging.getLogger("studio.metagraph")

router = APIRouter(prefix="/metagraph", tags=["metagraph"])

JOB_KIND = "metagraph_discover"


def _max_objects():
    return max(1, int(os.getenv("STUDIO_METAGRAPH_MAX_OBJECTS", "5000")))


def _max_columns():
    return max(1, int(os.getenv("STUDIO_METAGRAPH_MAX_COLUMNS", "1000")))


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# How often the scheduler checks every source's change signal.
_TICK_SECONDS = max(30, _env_int("STUDIO_METAGRAPH_TICK_SECONDS", 300))


def _full_sync_seconds():
    """Rediscover at least this often even when the signal has not moved (and
    on this cadence for a source that offers no signal)."""
    return max(60, _env_int("STUDIO_METAGRAPH_FULL_SYNC_SECONDS", 6 * 3600))


def _stale_seconds():
    """A discovery queued or running longer than this is presumed lost."""
    return max(60, _env_int("STUDIO_METAGRAPH_STALE_SECONDS", 1800))


def _tombstone_seconds():
    return max(0, _env_int("STUDIO_METAGRAPH_TOMBSTONE_DAYS", 30)) * 86400


def _hold_rule():
    """(minimum count, fraction): a removal at least this large is held for
    confirmation by a later snapshot."""
    try:
        fraction = float(os.getenv("STUDIO_METAGRAPH_HOLD_FRACTION", "0.5"))
    except ValueError:
        fraction = 0.5
    return max(1, _env_int("STUDIO_METAGRAPH_HOLD_MIN", 5)), fraction


def _confirm_delay_seconds():
    """How long a held removal waits before the confirming rediscovery."""
    return max(0, _env_int("STUDIO_METAGRAPH_CONFIRM_SECONDS", 600))


def _retry_seconds():
    """After the job's own retries, a failing source is tried again this often."""
    return max(60, _env_int("STUDIO_METAGRAPH_RETRY_SECONDS", 900))


def _snapshots_kept():
    return max(10, _env_int("STUDIO_METAGRAPH_SNAPSHOTS_KEPT", 200))


def ticker_enabled():
    return os.getenv("STUDIO_METAGRAPH_TICKER", "1").lower() not in ("0", "false", "no")


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
            mirrored_at REAL,
            queued_at REAL,
            synced_signal TEXT,
            checked_at REAL,
            held_removals TEXT,
            held_at REAL
        );
        """)
        c.commit()


# ── Discovery ────────────────────────────────────────────────────────────

def enqueue(source, trigger, *, conn=None, job_id=None, run_after=None):
    """Queue discovery of one source and mark it queued. `conn` enlists both in
    the caller's open transaction (a connection save), so the row, its job and
    its status land together. `job_id` makes the enqueue idempotent: a second
    call with the same id queues nothing and returns None."""
    def go(c):
        if job_id and c.execute("SELECT 1 FROM background_jobs WHERE id=?",
                                (job_id,)).fetchone():
            return None
        jid = jobs.enqueue(JOB_KIND, {"source": source, "trigger": trigger},
                           max_attempts=3, job_id=job_id, run_after=run_after, conn=c)
        _mark_queued(c, source)
        return jid
    if conn is not None:
        return go(conn)
    with db.connect() as c:
        jid = go(c)
        c.commit()
    return jid


def _mark_queued(c, source):
    """'queued' until the job starts — unless a discovery is running now, in
    which case it stays 'running' (the queued one follows it)."""
    now = time.time()
    row = c.execute("SELECT status FROM meta_sources WHERE source=?", (source,)).fetchone()
    if row is None:
        c.execute("INSERT INTO meta_sources (source, status, queued_at) VALUES (?, 'queued', ?)",
                  (source, now))
    elif row["status"] != "running":
        c.execute("UPDATE meta_sources SET status='queued', queued_at=? WHERE source=?",
                  (now, source))


@connections.on_source_saved
def _on_source_event(name, trigger, conn=None):
    """Connection lifecycle: a saved or reconnected source is discovered; a
    disconnected one leaves the Neo4j mirror (its record stays, hidden, for a
    reconnect); a removed one becomes tombstones, purged after the grace."""
    if trigger in ("disconnected", "removed"):
        now = time.time()
        with db.connect() as c:
            if trigger == "removed":
                c.execute("UPDATE meta_objects SET removed_at=? "
                          "WHERE source=? AND removed_at IS NULL", (now, name))
            c.execute("UPDATE meta_sources SET status=? WHERE source=?", (trigger, name))
            c.commit()
        mirror_drop(name)
        return
    enqueue(name, trigger, conn=conn)


def notify_changed(source, reason, *, key=None):
    """Something outside discovery changed `source` — a supervised write, a
    webhook. Queue a rediscovery unless one is already queued or running.
    `key` dedupes repeated notices of the same event (a polled job status)."""
    state = _state(source)
    if state and state["status"] in ("queued", "running") \
            and time.time() - (state.get("queued_at") or state.get("started_at") or 0) \
            < _stale_seconds():
        return None
    try:
        return enqueue(source, reason, job_id=f"metagraph-{key}" if key else None)
    except Exception:
        log.warning("metagraph: could not queue discovery of %s", source, exc_info=True)
        return None


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


def _signal_of(connector):
    try:
        signal = connector.change_signal()
    except Exception:
        return None
    return None if signal is None else str(signal)[:512]


def _read_source(source):
    """List and describe a source: (objects, complete, signal). Raises when the
    source cannot be listed at all. An object whose columns could not be read
    is returned with columns=None, and makes the snapshot incomplete. The
    change signal is read FIRST, so a change made while the listing runs moves
    the signal past what this snapshot saw and the next tick catches it."""
    from .connectors import get_connector
    connector = get_connector(source)
    if not connector.configured():
        raise RuntimeError(f"source '{source}' is not configured")
    signal = _signal_of(connector)
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
    return objects, complete, signal


def _lock(c, source):
    """One writer per source on Postgres (SQLite already has one writer)."""
    if db.IS_PG:
        key = int.from_bytes(hashlib.sha256(("metagraph:" + source).encode()).digest()[:8],
                             "big", signed=True)
        c.execute("SELECT pg_advisory_xact_lock(?)", (key,))


def _write(source, snapshot_id, objects, complete, now, signal=None):
    """Apply one snapshot in one transaction. Returns (added, changed, removed,
    held): `held` counts removals waiting for confirmation."""
    added = changed = removed = held = 0
    with db.connect() as c:
        _lock(c, source)
        existing = {r["name"]: dict(r) for r in c.execute(
            "SELECT id, name, fingerprint, removed_at FROM meta_objects WHERE source=?",
            (source,)).fetchall()}
        state = c.execute("SELECT held_removals, held_at FROM meta_sources WHERE source=?",
                          (source,)).fetchone()
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
        status, held_names, held_at = ("ok" if complete else "partial"), None, None
        if complete:
            gone = sorted(n for n, row in existing.items()
                          if n not in seen and row["removed_at"] is None)
            live = sum(1 for row in existing.values() if row["removed_at"] is None)
            minimum, fraction = _hold_rule()
            previous = json.loads(state["held_removals"]) if state and state["held_removals"] \
                else None
            if gone and len(gone) >= minimum and len(gone) >= fraction * live \
                    and previous != gone:
                # A large removal is held until a later complete snapshot
                # reports exactly the same set gone (tick_once queues that one).
                status, held_names, held_at, held = "held", gone, now, len(gone)
            else:
                for name in gone:
                    c.execute("UPDATE meta_objects SET removed_at=? WHERE id=?",
                              (now, existing[name]["id"]))
                    removed += 1
        elif state and state["held_removals"]:
            # An incomplete snapshot can neither confirm nor cancel a hold.
            held_names, held_at = json.loads(state["held_removals"]), state["held_at"]
            held = len(held_names)
        c.execute(
            "UPDATE meta_snapshots SET status=?, finished_at=?, objects=?, added=?, changed=?, "
            "removed=?, complete=?, error=? WHERE id=?",
            (status, time.time(), len(objects), added, changed, removed, 1 if complete else 0,
             f"{held} removals held for confirmation" if status == "held" else None,
             snapshot_id))
        c.execute(
            "UPDATE meta_sources SET status=?, snapshot_id=?, ok_at=?, error=NULL, "
            "held_removals=?, held_at=?, "
            "synced_signal=CASE WHEN ? = 1 THEN ? ELSE synced_signal END WHERE source=?",
            (status, snapshot_id, now, json.dumps(held_names) if held_names else None, held_at,
             1 if status in ("ok", "partial") else 0, signal, source))
        c.commit()
    return added, changed, removed, held


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
        objects, complete, signal = _read_source(source)
        added, changed, removed, held = _write(source, snapshot_id, objects, complete, now,
                                               signal)
    except Exception as exc:
        log.warning("metagraph: discovery of %s failed", source, exc_info=True)
        _fail(source, snapshot_id, _safe_error(exc))
        raise
    mirror(source)
    return {"source": source, "snapshot_id": snapshot_id, "objects": len(objects),
            "added": added, "changed": changed, "removed": removed, "held": held,
            "complete": complete}


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


def _live_sources():
    """Sources configured and connected right now. A disconnected or removed
    connection is absent, so its metadata is hidden even from a role that
    still holds a governance grant for it."""
    from .connectors import all_sources
    return [s["name"] for s in all_sources() if s.get("configured")]


def _visible_source(user, source):
    """404 for a source this user cannot use — indistinguishable from one that
    does not exist."""
    if source not in rbac.allowed_sources(user["role"]) or source not in _live_sources():
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
        held = json.loads(state["held_removals"]) if state.get("held_removals") else []
        out.update({"error": state.get("error"), "mirror_status": state.get("mirror_status"),
                    "mirror_error": state.get("mirror_error"), "held": len(held),
                    "checked_at": state.get("checked_at")})
    return out


def _recently_removed(source, limit=50):
    """Tombstones still inside their grace period, newest first (admins)."""
    with db.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT name, kind, removed_at FROM meta_objects WHERE source=? "
            "AND removed_at IS NOT NULL ORDER BY removed_at DESC LIMIT ?",
            (source, limit)).fetchall()]


@router.get("/sources")
def list_sources(user=Depends(current_user)):
    """Discovery status of every source this user may use."""
    names = _live_sources()
    allowed = set(rbac.allowed_sources(user["role"]))
    return {"sources": [_summary(user, n) for n in names if n in allowed]}


@router.get("/sources/{source}")
def get_source(source: str, user=Depends(current_user)):
    """The objects and columns of one source that this user may read."""
    _visible_source(user, source)
    objects = visible_objects(user, source)
    out = {**_summary(user, source, objects), "relations": objects}
    if user["role"] == "admin":
        out["removed"] = _recently_removed(source)
    return out


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
    if source not in _live_sources():
        raise HTTPException(404, "Not found")
    return {"job_id": enqueue(source, "manual"), "source": source}


# ── Continuous sync: the scheduler tick ──────────────────────────────────

def _due(state, signal, now):
    """Why this source should be rediscovered now, or None."""
    if state is None or not state.get("ok_at"):
        if state and state.get("status") == "failed" \
                and now - (state.get("started_at") or 0) < _retry_seconds():
            return None
        return "first_discovery"
    if state.get("held_removals"):
        # A held removal is confirmed by a fresh snapshot after the delay —
        # never sooner, whatever the signal says.
        held_for = now - (state.get("held_at") or now)
        return "confirm_removals" if held_for >= _confirm_delay_seconds() else None
    if state.get("status") == "failed":
        return "retry" if now - (state.get("started_at") or 0) >= _retry_seconds() else None
    if signal is not None and signal != state.get("synced_signal"):
        return "change_detected"
    if now - state["ok_at"] >= _full_sync_seconds():
        return "scheduled"
    return None


def _check(source, now):
    """Compare one source's change signal with what its last discovery saw and
    queue a rediscovery when it moved (or is due anyway). Returns the reason
    queued, or None."""
    from .connectors import get_connector
    state = _state(source)
    if state and state["status"] in ("queued", "running") \
            and now - (state.get("queued_at") or state.get("started_at") or 0) < _stale_seconds():
        return None                      # one is on its way; a lost one goes stale
    signal = _signal_of(get_connector(source))
    with db.connect() as c:
        cur = c.execute("UPDATE meta_sources SET checked_at=? WHERE source=?", (now, source))
        if not cur.rowcount:
            c.execute("INSERT INTO meta_sources (source, status, checked_at) VALUES (?,?,?)",
                      (source, "not_discovered", now))
        c.commit()
    reason = _due(state, signal, now)
    if reason:
        enqueue(source, reason)
    return reason


def _purge_tombstones(now):
    """Delete tombstones whose grace period is over, columns first."""
    cutoff = now - _tombstone_seconds()
    with db.connect() as c:
        c.execute("DELETE FROM meta_columns WHERE object_id IN (SELECT id FROM meta_objects "
                  "WHERE removed_at IS NOT NULL AND removed_at < ?)", (cutoff,))
        cur = c.execute("DELETE FROM meta_objects WHERE removed_at IS NOT NULL "
                        "AND removed_at < ?", (cutoff,))
        c.commit()
    return getattr(cur, "rowcount", 0) or 0


def _prune_snapshots():
    """Keep the newest snapshots per source; the objects carry the current state."""
    keep = _snapshots_kept()
    with db.connect() as c:
        for row in c.execute("SELECT source, COUNT(*) n FROM meta_snapshots GROUP BY source "
                             "HAVING COUNT(*) > ?", (keep,)).fetchall():
            c.execute("DELETE FROM meta_snapshots WHERE source=? AND id NOT IN "
                      "(SELECT id FROM meta_snapshots WHERE source=? "
                      "ORDER BY started_at DESC LIMIT ?)", (row["source"], row["source"], keep))
        c.commit()


def tick_once():
    """One scheduler pass ("metagraph_sync" lease): check every live source,
    retry a failed Neo4j mirror, purge expired tombstones, prune snapshots.
    One source's failure never stops the others."""
    now = time.time()
    queued = {}
    for source in _live_sources():
        try:
            reason = _check(source, now)
            if reason:
                queued[source] = reason
            state = _state(source) or {}
            if state.get("mirror_status") == "failed":
                mirror(source)
        except Exception:
            log.warning("metagraph: sync check of %s failed", source, exc_info=True)
    try:
        purged = _purge_tombstones(now)
        _prune_snapshots()
    except Exception:
        log.warning("metagraph: tombstone purge failed", exc_info=True)
        purged = 0
    return {"queued": queued, "purged": purged}


# ── Events from outside: a signed webhook ───────────────────────────────

@router.post("/events", status_code=202)
async def webhook(request: Request):
    """A system that changed a source (a dbt run, a deployment) says so:
    {"source": "..."} or {"sources": [...]}, signed with
    STUDIO_METAGRAPH_WEBHOOK_SECRET as `X-Studio-Signature: sha256=<hex>` over
    the raw body. Unset secret → this endpoint does not exist (404)."""
    import hmac
    secret = os.getenv("STUDIO_METAGRAPH_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(404, "Not found")
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(413, "event too large")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-studio-signature", "")):
        raise HTTPException(401, "bad signature")
    try:
        event = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "body must be JSON")
    names = event.get("sources") or ([event["source"]] if event.get("source") else [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise HTTPException(400, "source must be a name or a list of names")
    live = set(_live_sources())
    queued = [n for n in names[:50] if n in live and notify_changed(n, "webhook")]
    return {"queued": queued, "unknown": [n for n in names[:50] if n not in live]}


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


def mirror_drop(source):
    """Remove one source's subgraph from Neo4j (a disconnected or removed
    connection). Best-effort, like the mirror itself."""
    try:
        cfg = _mirror_cfg()
    except MirrorRefused:
        return False
    if cfg is None:
        return False
    try:
        _neo4j_commit(cfg, [
            {"statement": "MATCH (c:StudioColumn {source: $source}) DETACH DELETE c",
             "parameters": {"source": source}},
            {"statement": "MATCH (t:StudioObject {source: $source}) DETACH DELETE t",
             "parameters": {"source": source}},
            {"statement": "MATCH (s:StudioSource {name: $source}) DETACH DELETE s",
             "parameters": {"source": source}}])
    except Exception as exc:
        log.warning("metagraph: Neo4j removal of %s failed", source, exc_info=True)
        _mirror_state(source, "failed", _safe_error(exc))
        return False
    _mirror_state(source, "removed", None)
    return True


def _mirror_state(source, status, error):
    with db.connect() as c:
        c.execute("UPDATE meta_sources SET mirror_status=?, mirror_error=?, mirrored_at=? "
                  "WHERE source=?", (status, error, time.time() if status == "ok" else None,
                                     source))
        c.commit()
