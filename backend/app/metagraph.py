"""Metadata knowledge graph — what every connected source contains, discovered
automatically and kept as snapshots.

Saving a connection queues a `metagraph_discover` job, in the same
transaction as the connection row (connections.on_source_saved). The job lists
the source's tables and views, reads their columns, fingerprints each object,
and records a snapshot: what was added, changed and removed since the last
one. The result is a graph — source → namespace → table/view → column —
readable through /api/metagraph and mirrored to Neo4j when one is configured.
After each snapshot its relationships are recomputed (metarelations.py): how
the tables join — declared keys, joins seen in views and in Studio's own
queries, naming patterns — and what each view is derived from, every edge with
its evidence, state and confidence.

Across sources (metaentities.py): canonical entities — the keys that carry a
customer or an order from one source to another — proposed from naming and
used only once verified; domain ownership of sources and tables, with owners
reviewing what they own; freshness monitoring of every source's metadata; and
a permission-aware search over all of it. The Neo4j mirror is tenant-scoped
(STUDIO_METAGRAPH_TENANT), so deployments sharing one Neo4j never see or
overwrite each other's graph.

Certified metrics (metametrics.py): every semantic-YAML metric and dimension is
linked to its table and the columns it reads, checked against the record
(valid / broken / unchecked), and versioned from the applied YAML documents.

It then stays current with no one asking (tick_once, the "metagraph_sync"
scheduler), for ANY source: every few minutes each source is compared with the
graph's record through the contract every connector meets (list_tables,
get_schema), and each table found created, altered or dropped becomes a change
event (metacdc.py) that re-reads just that table (discover_asset) — then the
relationships, metric baselines and mirror are refreshed once for the source
(refresh_now). Bulk metadata or a connector's own per-table tokens only make
that cheaper. Where a source cannot be listed, its one cheap change signal
(Connector.change_signal) is compared instead. A full rediscovery still runs
when the last one is too old: the reconciliation for any change no event
reported. Events also come from a connection saved or reconnected, a
supervised write against a source, and a signed webhook (/metagraph/events).

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

from . import connections, db, jobs, metacdc, metaentities, metametrics, metarelations, rbac
from .auth import current_user

log = logging.getLogger("studio.metagraph")

router = APIRouter(prefix="/metagraph", tags=["metagraph"])

JOB_KIND = "metagraph_discover"
REFRESH_KIND = "metagraph_refresh"


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


def _refresh_seconds():
    """After per-table changes, the source's relationships, metric baselines
    and Neo4j mirror are refreshed once per window of this many seconds, not
    once per table. 0 refreshes at once, after every table."""
    return max(0, _env_int("STUDIO_METAGRAPH_REFRESH_SECONDS", 30))


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
            held_at REAL,
            related_at REAL,
            relate_error TEXT,
            freshness TEXT
        );
        """)
        c.commit()
    metarelations.init_tables()
    metaentities.init_tables()
    metametrics.init_tables()
    metacdc.init_tables()


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
        if trigger == "removed":
            metacdc.forget(name)
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
    """List and describe a source: (objects, complete, signal, table tokens).
    Raises when the source cannot be listed at all. An object whose columns
    could not be read is returned with columns=None, and makes the snapshot
    incomplete. The change signal and the per-table tokens are read FIRST, so a
    change made while the listing runs moves them past what this snapshot saw
    and the next tick catches it."""
    from .connectors import get_connector
    connector = get_connector(source)
    if not connector.configured():
        raise RuntimeError(f"source '{source}' is not configured")
    signal = _signal_of(connector)
    tokens = metacdc.read_table_signals(connector)
    names = list(connector.list_tables())
    cap = _max_objects()
    complete = len(names) <= cap and not getattr(connector, "tables_truncated", False)
    names = names[:cap]
    described = {}
    if getattr(connector, "describe_relations", None) is not None:
        try:
            described = connector.describe_relations() or {}
        except Exception:
            log.warning("metagraph: bulk describe failed for %s", source, exc_info=True)
    objects = []
    for name in names:
        info = described.get(name)
        if info is not None:
            kind, columns = info.get("kind") or "other", info.get("columns") or []
        else:
            try:
                info = _describe_one(connector, name, None, bulk=False)
                kind, columns = info.get("kind") or "unknown", info.get("columns") or []
            except Exception:
                log.warning("metagraph: could not read columns of %s.%s", source, name,
                            exc_info=True)
                kind, columns, complete = "unknown", None, False
        objects.append({"name": str(name)[:512], "kind": kind,
                        "namespace": _namespace_of(name, info),
                        "columns": None if columns is None else _clean_columns(columns)})
    return objects, complete, signal, tokens


def _lock(c, source):
    """One writer per source on Postgres (SQLite already has one writer)."""
    if db.IS_PG:
        key = int.from_bytes(hashlib.sha256(("metagraph:" + source).encode()).digest()[:8],
                             "big", signed=True)
        c.execute("SELECT pg_advisory_xact_lock(?)", (key,))


def _read_at(row):
    """When the source was last read about this stored object (seen, or found
    gone). A write based on an older read must not overwrite it."""
    return max(row["last_seen_at"] or 0, row["removed_at"] or 0)


def _write(source, snapshot_id, objects, complete, now, signal=None):
    """Apply one snapshot in one transaction. Returns (added, changed, removed,
    held): `held` counts removals waiting for confirmation. An object re-read
    on its own (discover_asset) after this snapshot started is newer than what
    the snapshot saw, and is left as that read recorded it."""
    added = changed = removed = held = 0
    with db.connect() as c:
        _lock(c, source)
        existing = {r["name"]: dict(r) for r in c.execute(
            "SELECT id, name, fingerprint, removed_at, last_seen_at FROM meta_objects "
            "WHERE source=?", (source,)).fetchall()}
        state = c.execute("SELECT held_removals, held_at FROM meta_sources WHERE source=?",
                          (source,)).fetchone()
        seen = set()
        for obj in objects:
            seen.add(obj["name"])
            row = existing.get(obj["name"])
            if row is not None and _read_at(row) > now:
                continue
            outcome = _upsert_object(c, source, row, obj, now, snapshot_id)
            added += outcome == "added"
            changed += outcome == "changed"
        status, held_names, held_at = ("ok" if complete else "partial"), None, None
        if complete:
            gone = sorted(n for n, row in existing.items()
                          if n not in seen and row["removed_at"] is None
                          and _read_at(row) <= now)
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


def _upsert_object(c, source, row, obj, now, snapshot_id):
    """Record one listed object against its stored row (None when new).
    Returns "added" (new, or back from a tombstone), "changed" (its kind or
    columns moved), or "unchanged" — including when its columns could not be
    read this time, which keeps what was known."""
    columns = obj["columns"]
    if columns is None and row is not None:
        c.execute("UPDATE meta_objects SET last_seen_at=?, snapshot_id=? WHERE id=?",
                  (now, snapshot_id, row["id"]))
        return "unchanged"
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
        return "added"
    if row["fingerprint"] != fp or row["removed_at"] is not None:
        c.execute(
            "UPDATE meta_objects SET namespace=?, kind=?, fingerprint=?, column_count=?, "
            "last_seen_at=?, changed_at=?, removed_at=NULL, snapshot_id=? WHERE id=?",
            (obj["namespace"], obj["kind"], fp, len(columns), now, now, snapshot_id,
             row["id"]))
        c.execute("DELETE FROM meta_columns WHERE object_id=?", (row["id"],))
        _put_columns(c, row["id"], columns)
        return "added" if row["removed_at"] is not None else "changed"
    c.execute("UPDATE meta_objects SET last_seen_at=?, snapshot_id=? WHERE id=?",
              (now, snapshot_id, row["id"]))
    return "unchanged"


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
        objects, complete, signal, tokens = _read_source(source)
        added, changed, removed, held = _write(source, snapshot_id, objects, complete, now,
                                               signal)
    except Exception as exc:
        log.warning("metagraph: discovery of %s failed", source, exc_info=True)
        _fail(source, snapshot_id, _safe_error(exc))
        raise
    try:
        # The snapshot-to-stream handoff. A table whose columns could not be
        # read is left out of the checkpoint, so the poller re-reads it alone.
        unread = {o["name"] for o in objects if o["columns"] is None}
        metacdc.after_snapshot(source, now, None if tokens is None else
                               {t: v for t, v in tokens.items() if t not in unread})
    except Exception:
        log.warning("metagraph: change-capture checkpoint of %s failed", source,
                    exc_info=True)
    relationships = _relate(source)
    try:
        metametrics.ensure_baselines(source)
    except Exception:
        log.warning("metagraph: metric baselines for %s failed", source, exc_info=True)
    mirror(source)
    return {"source": source, "snapshot_id": snapshot_id, "objects": len(objects),
            "added": added, "changed": changed, "removed": removed, "held": held,
            "complete": complete, "relationships": relationships}


def _relate(source):
    """Recompute relationships after a snapshot. A failure is recorded on the
    source and never undoes the snapshot."""
    try:
        out = metarelations.relate(source, lock=_lock)
        error = None
    except Exception as exc:
        log.warning("metagraph: relationships of %s failed", source, exc_info=True)
        out, error = None, _safe_error(exc)
    with db.connect() as c:
        c.execute("UPDATE meta_sources SET related_at=?, relate_error=? WHERE source=?",
                  (time.time() if error is None else None, error, source))
        c.commit()
    return out


@jobs.handler(JOB_KIND)
def _discover_job(payload, job=None):
    payload = payload or {}
    return discover(payload["source"], payload.get("trigger") or "job")


# ── One table at a time (change capture) ─────────────────────────────────

def match_asset(asset, names):
    """The one name in `names` that `asset` refers to: itself; the same name
    ignoring case; or, between a bare and a qualified spelling, the one name
    whose trailing parts agree ("orders" ~ "main.sales.orders"). None when no
    name, or more than one, fits."""
    names = list(names)
    if asset in names:
        return asset
    low = str(asset).lower()
    hits = [n for n in names if n.lower() == low]
    if hits:
        return hits[0] if len(hits) == 1 else None
    want = low.split(".")

    def agrees(name):
        have = name.lower().split(".")
        k = min(len(have), len(want))
        return have[-k:] == want[-k:]
    hits = [n for n in names if agrees(n)]
    return hits[0] if len(hits) == 1 else None


def listing(connector):
    """(names, complete): the source's objects as discovery takes them — the
    first STUDIO_METAGRAPH_MAX_OBJECTS of list_tables() — and whether that is
    all of them (only a complete listing can prove a removal)."""
    names = list(connector.list_tables())
    cap = _max_objects()
    complete = len(names) <= cap and not getattr(connector, "tables_truncated", False)
    return [str(n)[:512] for n in names[:cap]], complete


def recorded(source):
    """{name: what the record holds for it} — every stored object, tombstones
    included: fingerprint, kind, namespace, last_seen_at, changed_at,
    removed_at."""
    with db.connect() as c:
        return {r["name"]: dict(r) for r in c.execute(
            "SELECT name, fingerprint, kind, namespace, last_seen_at, changed_at, removed_at "
            "FROM meta_objects WHERE source=?", (source,)).fetchall()}


def _describe_one(connector, name, row, *, bulk=True):
    """Kind, namespace and columns of one object, read the way discovery reads
    it — so a re-read and a comparison never disagree over how a reader spells
    a type: Connector.describe_relation when offered; else (with `bulk`) the
    object's entry in describe_relations(); else get_schema(), with the kind
    and namespace already known (unknown for a new object)."""
    for method, args in (("describe_relation", (name,)),
                         ("describe_relations", ()) if bulk else (None, None)):
        fn = getattr(connector, method, None) if method else None
        if fn is None:
            continue
        try:
            info = fn(*args)
        except Exception:
            log.warning("metagraph: %s of %s failed", method, name, exc_info=True)
            continue
        if method == "describe_relations":
            info = info.get(name) if isinstance(info, dict) else None
        if info is not None:
            return info
    return {"kind": (row or {}).get("kind") or "unknown",
            "namespace": (row or {}).get("namespace"),
            "columns": connector.get_schema(name)}


def discover_asset(source, asset, trigger="event"):
    """Re-read ONE table or view and update just its record — the per-table
    path of change capture (metacdc.apply). The listing stays the authority on
    what exists: a listed object is re-read; one a complete listing no longer
    shows becomes a tombstone — unless removals on this source are held for
    confirmation, which the full rediscovery settles. The source's
    relationships, metric baselines and mirror follow (refresh_soon), queued
    in the same commit as the change. Raises when the source cannot be read,
    so the event is retried.

    Returns {"asset", "change"}: change is added, changed, unchanged, removed,
    absent (neither listed nor known — out of scope, or never existed),
    deferred (removal not provable now) or superseded (a newer read of this
    object was recorded while this one ran)."""
    from .connectors import get_connector
    connector = get_connector(source)
    if not connector.configured():
        raise RuntimeError(f"source '{source}' is not configured")
    read_at = time.time()
    listed, complete = listing(connector)
    existing = recorded(source)
    name = match_asset(asset, listed)
    if name is None:
        gone = match_asset(asset, [n for n, r in existing.items() if r["removed_at"] is None])
        if gone is None:
            # Not one of the source's objects — a view the listing leaves out,
            # say. Its change can still move relationships and lineage.
            with db.connect() as c:
                refresh = refresh_soon(source, conn=c)
                c.commit()
            if refresh == "now":
                refresh_now(source)
            return {"asset": asset, "change": "absent", "refresh": refresh}
        if not complete:
            return {"asset": gone, "change": "deferred",
                    "reason": "the listing is truncated, so it cannot prove a removal"}
        if (_state(source) or {}).get("held_removals"):
            return {"asset": gone, "change": "deferred",
                    "reason": "removals on this source are held for confirmation"}
        obj = None
    else:
        info = _describe_one(connector, name, existing.get(name))
        obj = {"name": name, "kind": info.get("kind") or "other",
               "namespace": _namespace_of(name, info),
               "columns": _clean_columns(info.get("columns"))}
    with db.connect() as c:
        _lock(c, source)
        row = c.execute("SELECT id, name, fingerprint, removed_at, last_seen_at "
                        "FROM meta_objects WHERE source=? AND name=?",
                        (source, name or gone)).fetchone()
        row = dict(row) if row else None
        if row is not None and _read_at(row) > read_at:
            return {"asset": name or gone, "change": "superseded"}
        if obj is None:
            if row is None or row["removed_at"] is not None:
                return {"asset": gone, "change": "unchanged"}
            c.execute("UPDATE meta_objects SET removed_at=? WHERE id=?", (read_at, row["id"]))
            change = "removed"
        else:
            change = _upsert_object(c, source, row, obj, read_at, trigger)
        refresh = refresh_soon(source, conn=c) if change != "unchanged" else None
        c.commit()
    if refresh == "now":
        refresh_now(source)
    out = {"asset": name or gone, "change": change, "refresh": refresh}
    if obj is not None:
        out["columns"] = len(obj["columns"])
    return out


def refresh_now(source):
    """What follows a change to some of a source's objects: its relationships,
    metric baselines and Neo4j mirror, recomputed from the record."""
    relationships = _relate(source)
    try:
        metametrics.ensure_baselines(source)
    except Exception:
        log.warning("metagraph: metric baselines for %s failed", source, exc_info=True)
    mirror(source)
    return {"source": source, "relationships": relationships}


def refresh_soon(source, *, conn):
    """Queue the refresh for the end of the current STUDIO_METAGRAPH_REFRESH_SECONDS
    window, once per source per window, on the caller's transaction — so many
    table changes in a burst cost one refresh, and a recorded change always has
    one coming. Returns the job id, or "now" when the window is 0 (the caller
    then runs refresh_now after committing). A refresh queued for a window
    starts only after it ends, so it always follows the change that found it
    already queued."""
    window = _refresh_seconds()
    if window == 0:
        return "now"
    bucket = int(time.time() // window)
    jid = f"mgrefresh-{bucket}-{hashlib.sha256(source.encode()).hexdigest()[:16]}"
    if conn.execute("SELECT 1 FROM background_jobs WHERE id=?", (jid,)).fetchone() is None:
        jobs.enqueue(REFRESH_KIND, {"source": source}, max_attempts=3, job_id=jid,
                     run_after=(bucket + 1) * window + 1, conn=conn)
    return jid


@jobs.handler(REFRESH_KIND)
def _refresh_job(payload, job=None):
    source = (payload or {}).get("source")
    if source not in _live_sources():
        return {"skipped": True}
    return refresh_now(source)


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
    names = {o["name"] for o in objects}
    joins = [r for r in metarelations.current(source, names) if r["kind"] == "join"]
    fresh, reasons = metaentities.freshness(state, time.time(), tick_s=_TICK_SECONDS,
                                            full_s=_full_sync_seconds())
    out = {"source": source, "status": state.get("status"), "discovered_at": state.get("ok_at"),
           "objects": len(objects), "kinds": kinds, "relationships": len(joins),
           "freshness": fresh}
    if user["role"] == "admin":
        held = json.loads(state["held_removals"]) if state.get("held_removals") else []
        out.update({"error": state.get("error"), "mirror_status": state.get("mirror_status"),
                    "mirror_error": state.get("mirror_error"), "held": len(held),
                    "checked_at": state.get("checked_at"),
                    "relate_error": state.get("relate_error"),
                    "freshness_reasons": reasons})
    return out


def _user_names(ids):
    ids = [i for i in ids if i]
    if not ids:
        return {}
    with db.connect() as c:
        ph = ",".join("?" for _ in ids)
        return {r["id"]: r["name"] for r in c.execute(
            f"SELECT id, name FROM users WHERE id IN ({ph})", tuple(ids)).fetchall()}


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
    own = metaentities.ownership(source)
    names = _user_names({e.get("owner_id") for e in own.values()})
    for obj in objects:
        entry = own.get(obj["name"]) or own.get("") or {}
        obj["domain"] = (own.get(obj["name"]) or {}).get("domain") or \
            (own.get("") or {}).get("domain")
        obj["owner"] = names.get(entry.get("owner_id"))
    defs = metametrics.for_source(source, {o["name"] for o in objects})
    data_changed = metacdc.data_changed(source)
    for obj in objects:
        obj["metrics"] = [{"name": d["name"], "kind": d["kind"], "status": d["status"],
                           "certified": d["certified"]}
                          for d in defs if d["object"] == obj["name"]]
        obj["data_changed_at"] = data_changed.get(obj["name"])
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
    for d in metametrics.for_source(source, {o["name"] for o in visible_objects(
            user, source, with_columns=False)}):
        if not d["object"]:
            continue
        mid = f"{d['kind']}:{source}:{d['object']}:{d['name']}"
        nodes.append({"id": mid, "type": d["kind"], "name": d["name"], "status": d["status"],
                      "certified": d["certified"]})
        edges.append({"from": mid, "to": f"object:{source}:{d['object']}",
                      "type": "MEASURES" if d["kind"] == "metric" else "SLICES"})
        for col in d["columns"]:
            edges.append({"from": mid, "to": f"column:{source}:{d['object']}:{col}",
                          "type": "USES"})
    return {"source": source, "nodes": nodes, "edges": edges}


@router.get("/sources/{source}/relationships")
def source_relationships(source: str, min_confidence: float = 0.0,
                         user=Depends(current_user)):
    """How this source's tables join, and what each view is derived from —
    every edge with its state, confidence and evidence. Only edges whose two
    tables this user may read; rejected ones only for administrators."""
    _visible_source(user, source)
    visible = {o["name"] for o in visible_objects(user, source, with_columns=False)}
    rels = metarelations.current(source, visible, include_rejected=user["role"] == "admin")
    return {"source": source,
            "relationships": [r for r in rels if r["confidence"] >= min_confidence
                              or r["state"] == "rejected"]}


@router.get("/sources/{source}/paths")
def source_paths(source: str, tables: str, min_confidence: float = 0.6,
                 user=Depends(current_user)):
    """The join path connecting `tables` (comma-separated), over relationships
    this user may see, preferring fewer and surer joins."""
    _visible_source(user, source)
    wanted = [t.strip() for t in tables.split(",") if t.strip()][:20]
    visible = {o["name"] for o in visible_objects(user, source, with_columns=False)}
    unknown = [t for t in wanted if t not in visible]
    joins, missing = metarelations.join_path(
        metarelations.current(source, visible), [t for t in wanted if t in visible],
        min_confidence)
    return {"source": source, "tables": wanted, "joins": joins,
            "connected": not missing and not unknown, "unreachable": missing,
            "unknown": unknown}


@router.post("/sources/{source}/relationships/{rel_id}/review")
def review_relationship(source: str, rel_id: str, body: dict, user=Depends(current_user)):
    """An administrator confirms or rejects a relationship ({"decision":
    "verified" | "rejected" | "clear"}). The review outlives recomputation."""
    _visible_source(user, source)
    with db.connect() as c:
        row = c.execute("SELECT from_object, to_object FROM meta_relationships WHERE id=? "
                        "AND source=?", (rel_id, source)).fetchone()
    if row is None:
        raise HTTPException(404, "Not found")
    if not (metaentities.may_review(user, source, row["from_object"])
            or metaentities.may_review(user, source, row["to_object"])):
        raise HTTPException(403, "Only administrators or the owner of these tables can "
                                 "review this relationship")
    try:
        found = metarelations.review(source, rel_id, (body or {}).get("decision"), user)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not found:
        raise HTTPException(404, "Not found")
    db.log_activity(user, "metagraph_review", prompt=f"{rel_id}: {body.get('decision')}",
                    source=source)
    return {"id": rel_id, "decision": body.get("decision")}


def _metric_view(d, user, *, detail=False):
    out = {k: d[k] for k in ("kind", "source", "table", "name", "agg", "expr", "filter",
                             "model_filter", "grain", "description", "synonyms", "owner",
                             "certified", "declared_version", "status", "columns")}
    out["object"] = d["object"]
    out["baseline_at"] = d.get("baseline_at")
    if user["role"] == "admin" or detail:
        out["reasons"] = d["reasons"]
    hist = metametrics.history(d["source"], d["table"], d["name"], d["kind"])
    current = [v for v in hist if not v.get("removed")]
    out["version"] = current[-1]["version"] if current else None
    if detail:
        names = _user_names({v.get("applied_by") for v in hist})
        out["history"] = [{**v, "applied_by": names.get(v.get("applied_by"), v.get("applied_by"))}
                          for v in hist]
    return out


def _metrics_for(user, source):
    """Definitions on `source` this user may see: an admin all of them (a
    broken one may name a table that is gone); others those on tables they
    may read."""
    if user["role"] == "admin":
        return metametrics.for_source(source)
    return metametrics.for_source(source, {o["name"] for o in visible_objects(
        user, source, with_columns=False)})


@router.get("/metrics")
def list_metrics(user=Depends(current_user)):
    """Every semantic-layer metric and dimension this user may see, linked to
    its table and columns, with its check (valid / broken / unchecked), owner,
    certification and version."""
    live = set(_live_sources()) & set(rbac.allowed_sources(user["role"]))
    return {"metrics": [_metric_view(d, user) for source in sorted(live)
                        for d in _metrics_for(user, source)]}


@router.get("/metrics/{source}/{name}")
def get_metric(source: str, name: str, kind: str = "metric", user=Depends(current_user)):
    """One definition with its version history from the applied YAML."""
    _visible_source(user, source)
    d = next((d for d in _metrics_for(user, source)
              if d["name"].lower() == name.lower() and d["kind"] == kind), None)
    if d is None:
        raise HTTPException(404, "Not found")
    return _metric_view(d, user, detail=True)


@router.post("/metrics/{source}/{name}/revalidate")
def revalidate_metric(source: str, name: str, kind: str = "metric",
                      user=Depends(current_user)):
    """Accept a drifted definition against its columns as they are now: an
    administrator, or the owner of its table."""
    _visible_source(user, source)
    d = next((d for d in _metrics_for(user, source)
              if d["name"].lower() == name.lower() and d["kind"] == kind), None)
    if d is None:
        raise HTTPException(404, "Not found")
    if not d.get("object") or not metaentities.may_review(user, source, d["object"]):
        raise HTTPException(403, "Only administrators or the owner of this table can "
                                 "revalidate it")
    try:
        result = metametrics.revalidate(d, user)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.log_activity(user, "metagraph_revalidate", source=source, prompt=f"{kind} {name}")
    return {"name": d["name"], "kind": kind, "status": result["status"],
            "reasons": result["reasons"]}


@router.get("/entities")
def list_entities(user=Depends(current_user)):
    """Canonical entities and the members this user may see. Nothing about a
    hidden member leaves; rejected members only for administrators."""
    return {"entities": metaentities.visible(user, _live_sources(),
                                             include_rejected=user["role"] == "admin")}


@router.post("/entities", status_code=201)
def create_entity(body: dict, user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Only administrators can create entities")
    try:
        eid = metaentities.create_entity((body or {}).get("name"), body.get("description"),
                                         body.get("domain"), user)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": eid}


@router.post("/entities/{entity_id}/members", status_code=201)
def add_entity_member(entity_id: str, body: dict, user=Depends(current_user)):
    """Map a column to an entity by hand (verified at once): administrators,
    or the owner of that table."""
    body = body or {}
    source, obj, column = body.get("source"), body.get("object"), body.get("column")
    if source not in _live_sources() or source not in rbac.allowed_sources(user["role"]) \
            or obj not in set(rbac.allowed_tables(user["role"], source, [obj])):
        raise HTTPException(404, "Not found")
    try:
        mid = metaentities.add_member(entity_id, source, obj, column, user)
    except PermissionError:
        raise HTTPException(403, "Only administrators or this table's owner can map it")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if mid is None:
        raise HTTPException(404, "Not found")
    db.log_activity(user, "metagraph_map", prompt=f"{entity_id}: {obj}.{column}", source=source)
    return {"id": mid}


@router.post("/entities/members/{member_id}/review")
def review_entity_member(member_id: str, body: dict, user=Depends(current_user)):
    """Verify, reject, or return to proposed one entity member: administrators,
    or the owner of the member's table."""
    try:
        row = metaentities.review_member(member_id, (body or {}).get("decision"), user)
    except PermissionError:
        raise HTTPException(403, "Only administrators or this table's owner can review it")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if row is None:
        raise HTTPException(404, "Not found")
    db.log_activity(user, "metagraph_entity_review",
                    prompt=f"{member_id}: {body.get('decision')}", source=row["source"])
    return {"id": member_id, "decision": body.get("decision")}


@router.put("/sources/{source}/ownership")
def set_ownership(source: str, body: dict, user=Depends(current_user)):
    """Give a source — or one of its tables — a domain and an owner
    ({"domain", "owner_email", "object"}); empty values clear it."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only administrators can assign ownership")
    _visible_source(user, source)
    body = body or {}
    owner_id = None
    if body.get("owner_email"):
        owner = db.get_user_by_email(str(body["owner_email"]).strip().lower())
        if not owner:
            raise HTTPException(400, "No Studio user with that email")
        owner_id = owner["id"]
    metaentities.set_ownership(source, body.get("object") or "", body.get("domain"), owner_id,
                               user)
    db.log_activity(user, "metagraph_ownership", source=source,
                    prompt=f"{body.get('object') or '(source)'}: {body.get('domain')} / "
                           f"{body.get('owner_email')}")
    return {"source": source, "object": body.get("object") or "",
            "domain": body.get("domain"), "owner_id": owner_id}


@router.get("/health")
def metadata_health(user=Depends(current_user)):
    """Freshness of every live source's metadata, with the reasons
    (administrators)."""
    if user["role"] != "admin":
        raise HTTPException(403, "Only administrators can see metadata health")
    now, rows = time.time(), []
    for source in _live_sources():
        fresh, reasons = metaentities.freshness(_state(source), now, tick_s=_TICK_SECONDS,
                                                full_s=_full_sync_seconds())
        broken = [f"{d['kind']} {d['name']} is {d['status']}: {'; '.join(d['reasons'])}"
                  for d in metametrics.for_source(source)
                  if d["status"] in metametrics.INVALID]
        if broken and fresh == "fresh":
            fresh = "failing"
        # A dead or long-waiting change event: a change may not have reached
        # the graph yet (the full sync will still reconcile it).
        cdc_reasons, cdc = metacdc.health(source, now)
        if cdc_reasons and fresh == "fresh":
            fresh = "stale"
        rows.append({"source": source, "freshness": fresh,
                     "reasons": reasons + broken + cdc_reasons,
                     "broken_metrics": len(broken), "dead_events": cdc["dead"],
                     "events_waiting_seconds": cdc["waiting_seconds"]})
    worst = next((f for f in ("failing", "stale", "unknown")
                  if any(r["freshness"] == f for r in rows)), "fresh")
    return {"status": worst, "sources": rows}


@router.get("/search")
def search(q: str, user=Depends(current_user)):
    """Tables, columns and entities matching `q` across every source this user
    may use — only what the user may read; nothing else is counted."""
    term = " ".join(str(q or "").lower().split())[:80]
    if len(term) < 2:
        raise HTTPException(400, "search for at least two characters")
    live = set(_live_sources())
    hits = []
    for source in sorted(live & set(rbac.allowed_sources(user["role"]))):
        for obj in visible_objects(user, source):
            if term in obj["name"].lower():
                hits.append({"type": obj["kind"], "source": source, "object": obj["name"]})
            for col in obj["columns"]:
                if term in col["name"].lower():
                    hits.append({"type": "column", "source": source, "object": obj["name"],
                                 "column": col["name"], "data_type": col["type"]})
            if len(hits) >= 100:
                break
    for ent in metaentities.visible(user, live):
        if term in ent["name"].lower():
            hits.append({"type": "entity", "entity": ent["name"],
                         "members": [f"{m['source']}.{m['object']}.{m['column']}"
                                     for m in ent["members"]]})
    return {"q": term, "results": hits[:100]}


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
    """Turn the tables that changed since the graph read them into change
    events (metacdc.poll); where the source cannot be listed now, compare its
    one change signal with what its last discovery saw. Queue a rediscovery
    when that moved, when the change is too big for per-table events, or when
    one is due anyway. Returns the reason queued, or None."""
    from .connectors import get_connector
    state = _state(source)
    if state and state["status"] in ("queued", "running") \
            and now - (state.get("queued_at") or state.get("started_at") or 0) < _stale_seconds():
        return None                      # one is on its way; a lost one goes stale
    connector = get_connector(source)
    mode = metacdc.poll(source, connector, state, now)
    signal = _signal_of(connector) if mode == "fallback" else None
    with db.connect() as c:
        cur = c.execute("UPDATE meta_sources SET checked_at=? WHERE source=?", (now, source))
        if not cur.rowcount:
            c.execute("INSERT INTO meta_sources (source, status, checked_at) VALUES (?,?,?)",
                      (source, "not_discovered", now))
        c.commit()
    reason = _due(state, signal, now)
    if reason is None and mode == "rediscover" and (state or {}).get("status") != "failed":
        reason = "large_change"
    if reason:
        enqueue(source, reason)
    return reason


def _purge_tombstones(now):
    """Delete tombstones whose grace period is over, columns first."""
    cutoff = now - _tombstone_seconds()
    with db.connect() as c:
        c.execute("DELETE FROM meta_columns WHERE object_id IN (SELECT id FROM meta_objects "
                  "WHERE removed_at IS NOT NULL AND removed_at < ?)", (cutoff,))
        c.execute("DELETE FROM meta_asset_versions WHERE EXISTS (SELECT 1 FROM meta_objects o "
                  "WHERE o.source = meta_asset_versions.source "
                  "AND o.name = meta_asset_versions.asset "
                  "AND o.removed_at IS NOT NULL AND o.removed_at < ?)", (cutoff,))
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
        metacdc.prune(now)
    except Exception:
        log.warning("metagraph: tombstone purge failed", exc_info=True)
        purged = 0
    live = _live_sources()
    try:
        metaentities.propose(live)
        mirror_entities()
    except Exception:
        log.warning("metagraph: entity proposals failed", exc_info=True)
    try:
        metametrics.ensure_baselines()
    except Exception:
        log.warning("metagraph: metric baselines failed", exc_info=True)
    _watch_freshness(live, now)
    return {"queued": queued, "purged": purged}


def _watch_freshness(sources, now):
    """Record each source's freshness and log every change of it, so a source
    going stale or failing is visible in the logs as well as on its row."""
    for source in sources:
        state = _state(source)
        fresh, reasons = metaentities.freshness(state, now, tick_s=_TICK_SECONDS,
                                                full_s=_full_sync_seconds())
        if state and state.get("freshness") != fresh:
            (log.warning if fresh in ("stale", "failing") else log.info)(
                "metagraph: %s metadata is %s%s", source, fresh,
                f" ({'; '.join(reasons)})" if reasons else "")
            with db.connect() as c:
                c.execute("UPDATE meta_sources SET freshness=? WHERE source=?", (fresh, source))
                c.commit()


# ── Events from outside: a signed webhook ───────────────────────────────

@router.post("/events", status_code=202)
async def webhook(request: Request):
    """A system that changed a source says so, signed with
    STUDIO_METAGRAPH_WEBHOOK_SECRET as `X-Studio-Signature: sha256=<hex>` over
    the raw body. Unset secret → this endpoint does not exist (404).

    Change events (metacdc's contract): one event, or {"events": [...]} (at
    most 500). The reply says which were accepted — recorded and queued
    before this answer — which were duplicates of ones already received, and
    which were rejected and why; 400 when none was accepted or a duplicate.
    The older form, {"source": "..."} or {"sources": [...]}, still queues a
    rediscovery of each source named."""
    import hmac
    secret = os.getenv("STUDIO_METAGRAPH_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(404, "Not found")
    body = await request.body()
    if len(body) > 512 * 1024:
        raise HTTPException(413, "event too large")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, request.headers.get("x-studio-signature", "")):
        raise HTTPException(401, "bad signature")
    try:
        event = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "body must be JSON")
    if not isinstance(event, dict):
        raise HTTPException(400, "body must be a JSON object")
    if "events" in event or "event_id" in event:
        batch = event["events"] if "events" in event else [event]
        if not isinstance(batch, list) or not batch:
            raise HTTPException(400, "events must be a non-empty list")
        out = metacdc.ingest(batch, "webhook")
        if not out["accepted"] and not out["duplicates"]:
            raise HTTPException(400, {"rejected": out["rejected"]})
        return out
    names = event.get("sources") or ([event["source"]] if event.get("source") else [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise HTTPException(400, "source must be a name or a list of names")
    live = set(_live_sources())
    queued = [n for n in names[:50] if n in live and notify_changed(n, "webhook")]
    return {"queued": queued, "unknown": [n for n in names[:50] if n not in live]}


# ── Neo4j mirror ─────────────────────────────────────────────────────────

class MirrorRefused(RuntimeError):
    pass


def _tenant():
    """The deployment's partition of a shared Neo4j. Every mirrored node and
    edge carries it and every statement is scoped by it."""
    return (os.getenv("STUDIO_METAGRAPH_TENANT", "").strip() or "studio")[:64]


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
    rels = [{"kind": r["kind"], "from": r["from_object"], "to": r["to_object"],
             "from_columns": json.dumps(r["from_columns"]),
             "to_columns": json.dumps(r["to_columns"]), "state": r["state"],
             "confidence": r["confidence"], "evidence": json.dumps(r["evidence"])}
            for r in metarelations.current(source, {o["name"] for o in objects},
                                           include_rejected=True)]
    params = {"tenant": _tenant(), "source": source, "now": time.time(),
              "objects": objects, "columns": columns,
              "names": [o["name"] for o in objects],
              "pairs": [[col["object"], col["name"]] for col in columns],
              "relationships": rels,
              "rel_keys": [[r["kind"], r["from"], r["from_columns"], r["to"], r["to_columns"]]
                           for r in rels]}
    defs = [d for d in metametrics.for_source(source) if d["object"]]
    params["metrics"] = [{"kind": d["kind"], "name": d["name"], "object": d["object"],
                          "agg": d.get("agg"), "expr": d["expr"], "status": d["status"],
                          "certified": d["certified"], "owner": d.get("owner"),
                          "columns": d["columns"]} for d in defs]
    params["metric_keys"] = [[d["kind"], d["name"]] for d in defs]
    statements = [
        {"statement": "MERGE (s:StudioSource {tenant: $tenant, name: $source}) SET s.synced_at = $now",
         "parameters": params},
        {"statement": "UNWIND $objects AS o "
                      "MERGE (t:StudioObject {tenant: $tenant, source: $source, name: o.name}) "
                      "SET t.kind = o.kind, t.namespace = o.namespace, "
                      "t.fingerprint = o.fingerprint, t.column_count = o.column_count "
                      "WITH t MATCH (s:StudioSource {tenant: $tenant, name: $source}) MERGE (s)-[:CONTAINS]->(t)",
         "parameters": params},
        {"statement": "UNWIND $columns AS col "
                      "MATCH (t:StudioObject {tenant: $tenant, source: $source, name: col.object}) "
                      "MERGE (c:StudioColumn {tenant: $tenant, source: $source, object: col.object, name: col.name}) "
                      "SET c.type = col.type, c.ordinal = col.ordinal "
                      "MERGE (t)-[:HAS_COLUMN]->(c)",
         "parameters": params},
        {"statement": "MATCH (c:StudioColumn {tenant: $tenant, source: $source}) "
                      "WHERE NOT [c.object, c.name] IN $pairs DETACH DELETE c",
         "parameters": params},
        {"statement": "MATCH (t:StudioObject {tenant: $tenant, source: $source}) "
                      "WHERE NOT t.name IN $names DETACH DELETE t",
         "parameters": params},
        {"statement": "UNWIND $relationships AS r "
                      "MATCH (a:StudioObject {tenant: $tenant, source: $source, name: r.from}) "
                      "MATCH (b:StudioObject {tenant: $tenant, source: $source, name: r.to}) "
                      "MERGE (a)-[e:RELATES {kind: r.kind, from_columns: r.from_columns, "
                      "to_columns: r.to_columns}]->(b) "
                      "SET e.state = r.state, e.confidence = r.confidence, "
                      "e.evidence = r.evidence",
         "parameters": params},
        {"statement": "MATCH (a:StudioObject {tenant: $tenant, source: $source})-[e:RELATES]->(b:StudioObject) "
                      "WHERE NOT [e.kind, a.name, e.from_columns, b.name, e.to_columns] "
                      "IN $rel_keys DELETE e",
         "parameters": params},
        {"statement": "UNWIND $metrics AS m "
                      "MATCH (t:StudioObject {tenant: $tenant, source: $source, name: m.object}) "
                      "MERGE (x:StudioMetric {tenant: $tenant, source: $source, kind: m.kind, "
                      "name: m.name}) "
                      "SET x.agg = m.agg, x.expr = m.expr, x.status = m.status, "
                      "x.certified = m.certified, x.owner = m.owner "
                      "MERGE (x)-[:MEASURES]->(t) "
                      "WITH x, m UNWIND m.columns AS col "
                      "MATCH (c:StudioColumn {tenant: $tenant, source: $source, object: m.object, "
                      "name: col}) MERGE (x)-[:USES]->(c)",
         "parameters": params},
        {"statement": "MATCH (x:StudioMetric {tenant: $tenant, source: $source}) "
                      "WHERE NOT [x.kind, x.name] IN $metric_keys DETACH DELETE x",
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
            {"statement": "MATCH (c:StudioColumn {tenant: $tenant, source: $source}) DETACH DELETE c",
             "parameters": {"tenant": _tenant(), "source": source}},
            {"statement": "MATCH (t:StudioObject {tenant: $tenant, source: $source}) DETACH DELETE t",
             "parameters": {"tenant": _tenant(), "source": source}},
            {"statement": "MATCH (s:StudioSource {tenant: $tenant, name: $source}) DETACH DELETE s",
             "parameters": {"tenant": _tenant(), "source": source}}])
    except Exception as exc:
        log.warning("metagraph: Neo4j removal of %s failed", source, exc_info=True)
        _mirror_state(source, "failed", _safe_error(exc))
        return False
    _mirror_state(source, "removed", None)
    return True


def mirror_entities():
    """Rewrite this tenant's entities in Neo4j: (:StudioEntity)-[:KEYED_BY
    {column, role, state}]->(:StudioObject). Best-effort, like the rest."""
    try:
        cfg = _mirror_cfg()
    except MirrorRefused:
        return False
    if cfg is None:
        return False
    with db.connect() as c:
        ents = [dict(r) for r in c.execute("SELECT id, name, description, domain "
                                           "FROM meta_entities").fetchall()]
        members = [dict(r) for r in c.execute(
            "SELECT entity_id, source, object, column_name, role, state, confidence "
            "FROM meta_entity_members WHERE removed_at IS NULL").fetchall()]
    names = {e["id"]: e["name"] for e in ents}
    rows = [{"entity": names[m["entity_id"]], "source": m["source"], "object": m["object"],
             "column": m["column_name"], "role": m["role"], "state": m["state"],
             "confidence": m["confidence"]} for m in members if m["entity_id"] in names]
    params = {"tenant": _tenant(),
              "entities": [{"name": e["name"], "description": e["description"],
                            "domain": e["domain"]} for e in ents],
              "members": rows, "names": [e["name"] for e in ents],
              "keys": [[r["entity"], r["source"], r["object"], r["column"]] for r in rows]}
    try:
        _neo4j_commit(cfg, [
            {"statement": "UNWIND $entities AS e "
                          "MERGE (n:StudioEntity {tenant: $tenant, name: e.name}) "
                          "SET n.description = e.description, n.domain = e.domain",
             "parameters": params},
            {"statement": "UNWIND $members AS m "
                          "MATCH (n:StudioEntity {tenant: $tenant, name: m.entity}) "
                          "MATCH (t:StudioObject {tenant: $tenant, source: m.source, "
                          "name: m.object}) "
                          "MERGE (n)-[k:KEYED_BY {column: m.column}]->(t) "
                          "SET k.role = m.role, k.state = m.state, k.confidence = m.confidence",
             "parameters": params},
            {"statement": "MATCH (n:StudioEntity {tenant: $tenant})-[k:KEYED_BY]->"
                          "(t:StudioObject) WHERE NOT [n.name, t.source, t.name, k.column] "
                          "IN $keys DELETE k",
             "parameters": params},
            {"statement": "MATCH (n:StudioEntity {tenant: $tenant}) "
                          "WHERE NOT n.name IN $names DETACH DELETE n",
             "parameters": params}])
    except Exception:
        log.warning("metagraph: Neo4j entity mirror failed", exc_info=True)
        return False
    return True


def _mirror_state(source, status, error):
    with db.connect() as c:
        c.execute("UPDATE meta_sources SET mirror_status=?, mirror_error=?, mirrored_at=? "
                  "WHERE source=?", (status, error, time.time() if status == "ok" else None,
                                     source))
        c.commit()
