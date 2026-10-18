"""Metadata change data capture: schema changes reach the graph as events.

One normalized event contract for every way a change can be noticed:

  {"event_id": "evt-123",            # the sender's id — the dedupe key
   "source": "databricks-3",         # a live Studio source
   "asset": "retail_order_items",    # a table or view; optional
   "event_type": "schema_changed",   # schema_changed | asset_created |
                                     # asset_dropped | data_changed | source_changed
   "source_version": 42,             # optional: the sender's own ordering
   "occurred_at": "2026-10-17T18:00:00Z",   # optional, ISO-8601 or epoch
   "tenant_id": "studio"}            # optional; must be this deployment's

Where events come from: the poller on the scheduler (poll — any connector,
through the list_tables()/get_schema() contract every connector meets, see the
polling section), the signed webhook (POST /api/metagraph/events — anything
that knows a change happened: a CDC tool, a warehouse task, a deployment
pipeline), and Studio itself (an approved write against a source).

What happens to one:
  1. INTAKE. Validated, recorded in meta_events under its event_id (a repeat
     is a duplicate, acknowledged and dropped), and its job queued — the row
     and the job commit together, and only then is the sender told
     "accepted".
  2. ORDER. An event NEVER carries the new schema: the worker re-reads the
     table's CURRENT state from the source, so a late, repeated or reordered
     event cannot roll the graph back. Ordering only saves work — an event is
     superseded when its table was read after the event arrived (Studio's own
     clock), or when an event from the same origin with a later
     source_version (numbers) or occurred_at was already applied to it.
  3. APPLY. A schema event re-reads just that table (metagraph.discover_asset);
     the relationships, metric baselines and mirror that depend on it are
     refreshed once for the source shortly after (metagraph.refresh_soon). A
     source-wide event rediscovers the source. A data_changed event (rows, not
     schema) never touches the graph: it only records when that table's data
     last changed.
  4. RETRY / DEAD LETTER. A failing event is retried with backoff by the job
     queue; after its last attempt it is "dead", listed for administrators,
     and can be replayed.

Snapshot handoff: the poller compares the source with the graph's own
record, so whatever a full discovery missed while it ran differs from the
record and arrives as an event. (A connector's own per-table tokens are read
BEFORE the snapshot lists, and saved as their checkpoint, for the same
effect.) Every table is marked read as of the snapshot's start, so an event
that arrived before it started is superseded by it. The periodic full sync
stays, as the reconciliation for any event that never arrived.
"""
import hashlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from . import db, jobs
from .auth import current_user

log = logging.getLogger("studio.metagraph")

router = APIRouter(prefix="/metagraph", tags=["metagraph"])

JOB_KIND = "metagraph_event"
EVENT_TYPES = ("schema_changed", "asset_created", "asset_dropped", "data_changed",
               "source_changed")
ORIGINS = ("poller", "webhook", "studio")
STATUSES = ("accepted", "applied", "superseded", "ignored", "dead")
MAX_BATCH = 500
MAX_ATTEMPTS = 4
_ID_RE = re.compile(r"^[A-Za-z0-9._:@/+=-]{1,128}$")


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _keep_days():
    """Settled events are kept this long; dead ones until replayed."""
    return max(1, _env_int("STUDIO_METAGRAPH_EVENTS_KEEP_DAYS", 30))


def _max_table_events():
    """A poll that finds more changed tables than this rediscovers the source
    instead: past some size, one full read is cheaper than many single ones."""
    return max(1, _env_int("STUDIO_METAGRAPH_MAX_TABLE_EVENTS", 50))


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta_events (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            asset TEXT,
            event_type TEXT NOT NULL,
            source_version TEXT,
            occurred_at REAL NOT NULL,
            received_at REAL NOT NULL,
            origin TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            result TEXT,
            applied_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_meta_events_status ON meta_events(status, received_at);
        CREATE INDEX IF NOT EXISTS idx_meta_events_source ON meta_events(source, received_at);
        CREATE TABLE IF NOT EXISTS meta_asset_versions (
            source TEXT NOT NULL,
            asset TEXT NOT NULL,
            read_at REAL,
            origin TEXT,
            version TEXT,
            occurred_at REAL,
            event_id TEXT,
            data_changed_at REAL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (source, asset)
        );
        CREATE TABLE IF NOT EXISTS meta_cdc_offsets (
            source TEXT NOT NULL,
            adapter TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (source, adapter)
        );
        """)
        c.commit()


# ── The contract ─────────────────────────────────────────────────────────

class Rejected(ValueError):
    pass


def _time(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise Rejected("occurred_at must be ISO-8601 or epoch seconds")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise Rejected("occurred_at must be ISO-8601 or epoch seconds")


def normalize(raw, live_sources, tenant, now):
    """One raw event → the stored shape, or Rejected with the reason."""
    if not isinstance(raw, dict):
        raise Rejected("an event must be an object")
    eid = raw.get("event_id")
    if not isinstance(eid, str) or not _ID_RE.match(eid):
        raise Rejected("event_id must be 1-128 letters, digits or ._:@/+=-")
    if raw.get("tenant_id") not in (None, "", tenant):
        raise Rejected("the event is for another tenant")
    etype = raw.get("event_type")
    if etype not in EVENT_TYPES:
        raise Rejected(f"event_type must be one of {', '.join(EVENT_TYPES)}")
    source = raw.get("source")
    if not isinstance(source, str) or source not in live_sources:
        raise Rejected("unknown source")
    asset = raw.get("asset")
    if asset is not None and (not isinstance(asset, str) or not asset.strip()
                              or len(asset) > 512):
        raise Rejected("asset must be a table or view name")
    asset = asset.strip() if asset else None
    if etype == "source_changed":
        asset = None
    elif asset is None:
        if etype == "data_changed":
            raise Rejected("data_changed needs an asset")
        etype = "source_changed"              # no table named: the whole source
    version = raw.get("source_version")
    if version is not None and (isinstance(version, bool)
                                or not isinstance(version, (str, int, float))
                                or len(str(version)) > 128):
        raise Rejected("source_version must be a short label or a number")
    return {"event_id": eid, "source": source, "asset": asset, "event_type": etype,
            "source_version": None if version is None else str(version),
            "occurred_at": _time(raw.get("occurred_at"), now)}


def ingest(events, origin, *, live_sources=None):
    """Durably accept events: each is recorded and its job queued in ONE
    commit, and only then reported accepted. Returns {"accepted": [ids],
    "duplicates": [ids], "rejected": [{"event_id", "reason"}]}."""
    from . import metagraph
    assert origin in ORIGINS
    events = list(events)
    live = set(live_sources if live_sources is not None else metagraph._live_sources())
    tenant = metagraph._tenant()
    out = {"accepted": [], "duplicates": [], "rejected": []}
    for raw in events[:MAX_BATCH]:
        now = time.time()
        try:
            e = normalize(raw, live, tenant, now)
        except Rejected as exc:
            eid = raw.get("event_id") if isinstance(raw, dict) else None
            out["rejected"].append({"event_id": eid if isinstance(eid, str) else None,
                                    "reason": str(exc)})
            continue
        with db.connect() as c:
            cur = c.execute(
                "INSERT INTO meta_events (event_id, source, asset, event_type, source_version, "
                "occurred_at, received_at, origin, status) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                (e["event_id"], e["source"], e["asset"], e["event_type"], e["source_version"],
                 e["occurred_at"], now, origin, "accepted"))
            if getattr(cur, "rowcount", 0) != 1:
                out["duplicates"].append(e["event_id"])
                continue
            jobs.enqueue(JOB_KIND, {"event_id": e["event_id"]}, max_attempts=MAX_ATTEMPTS,
                         conn=c)
            c.commit()
        out["accepted"].append(e["event_id"])
    if len(events) > MAX_BATCH:
        out["rejected"].append({"event_id": None,
                                "reason": f"a batch holds at most {MAX_BATCH} events"})
    return out


# ── Applying one ─────────────────────────────────────────────────────────

def get_event(event_id):
    with db.connect() as c:
        row = c.execute("SELECT * FROM meta_events WHERE event_id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def _asset_row(source, asset):
    with db.connect() as c:
        row = c.execute("SELECT * FROM meta_asset_versions WHERE source=? AND asset=?",
                        (source, asset)).fetchone()
    return dict(row) if row else None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def superseded(event, record):
    """Why this event needs no work, or None. It only ever saves work: the
    worker re-reads current state either way."""
    if record is None:
        return None
    if (record.get("read_at") or 0) >= event["received_at"]:
        return "this table was read from the source after the event arrived"
    if record.get("origin") != event["origin"] or not record.get("event_id"):
        return None        # different clocks and counters: no comparing them
    mine, theirs = _number(event.get("source_version")), _number(record.get("version"))
    if mine is not None and theirs is not None:
        return ("an event with a later source_version was already applied"
                if mine <= theirs else None)
    if record.get("occurred_at") is not None and event["occurred_at"] <= record["occurred_at"]:
        return "a later event for this table was already applied"
    return None


def _record(source, asset, *, read_at=None, event=None, data_changed_at=None):
    """Move what is known about one table forward — never back."""
    from . import metagraph
    with db.connect() as c:
        metagraph._lock(c, source)
        row = c.execute("SELECT * FROM meta_asset_versions WHERE source=? AND asset=?",
                        (source, asset)).fetchone()
        row = dict(row) if row else {"read_at": None, "origin": None, "version": None,
                                     "occurred_at": None, "event_id": None,
                                     "data_changed_at": None}
        new = dict(row)
        if read_at is not None:
            new["read_at"] = max(row["read_at"] or 0, read_at)
        if event is not None and superseded({**event, "received_at": float("inf")},
                                            row) is None:
            new.update(origin=event["origin"], version=event.get("source_version"),
                       occurred_at=event["occurred_at"], event_id=event["event_id"])
        if data_changed_at is not None:
            new["data_changed_at"] = max(row["data_changed_at"] or 0, data_changed_at)
        c.execute("DELETE FROM meta_asset_versions WHERE source=? AND asset=?", (source, asset))
        c.execute("INSERT INTO meta_asset_versions (source, asset, read_at, origin, version, "
                  "occurred_at, event_id, data_changed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (source, asset, new["read_at"], new["origin"], new["version"],
                   new["occurred_at"], new["event_id"], new["data_changed_at"], time.time()))
        c.commit()


def _resolve(source, asset):
    """The stored name an event's asset refers to, when one fits; else the
    asset as written."""
    from . import metagraph
    with db.connect() as c:
        names = [r["name"] for r in c.execute(
            "SELECT name FROM meta_objects WHERE source=?", (source,)).fetchall()]
    return metagraph.match_asset(asset, names) or asset


def apply(event):
    """Apply one accepted event. Returns (status, result)."""
    from . import metagraph
    source = event["source"]
    if source not in metagraph._live_sources():
        return "ignored", {"reason": "the source is not connected"}
    if event["event_type"] == "source_changed":
        state = metagraph._state(source) or {}
        if (state.get("ok_at") or 0) >= event["received_at"]:
            return "superseded", {"reason": "the source was rediscovered after the event "
                                            "arrived"}
        if state.get("status") == "queued" and (state.get("queued_at") or 0) \
                >= event["received_at"] - metagraph._stale_seconds():
            # Queued and not yet started: it reads the source after this event
            # arrived, so it covers it.
            return "superseded", {"reason": "a rediscovery of the source is already queued"}
        return "applied", metagraph.discover(source, trigger=f"event:{event['event_id']}")
    asset = _resolve(source, event["asset"])
    if event["event_type"] == "data_changed":
        # Rows changed, not the schema: the graph is not touched — only the
        # marker of when this table's data last changed.
        _record(source, asset, data_changed_at=event["occurred_at"])
        return "applied", {"asset": asset, "data_changed_at": event["occurred_at"]}
    why = superseded(event, _asset_row(source, asset))
    if why:
        return "superseded", {"asset": asset, "reason": why}
    read_at = time.time()
    result = metagraph.discover_asset(source, event["asset"],
                                      trigger=f"event:{event['event_id']}")
    if result["change"] == "superseded":
        return "superseded", {**result, "reason": "a newer read of this table was recorded"}
    _record(source, result.get("asset") or asset, read_at=read_at, event=event)
    return ("ignored" if result["change"] == "absent" else "applied"), result


@jobs.handler(JOB_KIND)
def _process(payload, job=None):
    event = get_event((payload or {}).get("event_id"))
    if event is None or event["status"] != "accepted":
        return {"skipped": True}
    attempts = (job or {}).get("attempts") or event["attempts"] + 1
    try:
        status, result = apply(event)
    except Exception as exc:
        final = job is None or attempts >= (job.get("max_attempts") or MAX_ATTEMPTS)
        log.warning("metagraph: event %s failed%s", event["event_id"],
                    " for the last time" if final else "", exc_info=True)
        with db.connect() as c:
            c.execute("UPDATE meta_events SET attempts=?, error=?, status=? WHERE event_id=?",
                      (attempts, " ".join(f"{type(exc).__name__}: {exc}".split())[:300],
                       "dead" if final else "accepted", event["event_id"]))
            c.commit()
        raise
    with db.connect() as c:
        c.execute("UPDATE meta_events SET status=?, result=?, error=NULL, attempts=?, "
                  "applied_at=? WHERE event_id=?",
                  (status, json.dumps(result, default=str), attempts, time.time(),
                   event["event_id"]))
        c.commit()
    return {"status": status}


def replay(event_id):
    """Queue an event again (a dead one, or any one an administrator wants
    re-applied). Returns the job id, or None when there is no such event."""
    with db.connect() as c:
        cur = c.execute("UPDATE meta_events SET status='accepted', attempts=0, error=NULL, "
                        "result=NULL, applied_at=NULL WHERE event_id=?", (event_id,))
        if getattr(cur, "rowcount", 0) != 1:
            return None
        jid = jobs.enqueue(JOB_KIND, {"event_id": event_id}, max_attempts=MAX_ATTEMPTS,
                           conn=c)
        c.commit()
    return jid


# ── Polling: any source, compared with the graph's own record ────────────
#
# Change capture rests on the contract EVERY connector already meets —
# list_tables() and get_schema() — so any database, warehouse, API or file
# store a connector reaches is covered with no code of its own:
#
#   created / dropped  the listing against the graph's record, every tick;
#   altered            fresh column fingerprints against the recorded ones:
#                      all tables at once where the connector offers bulk
#                      metadata (describe_relations), else the next tables in
#                      rotation within STUDIO_METAGRAPH_PROBE_TABLES and
#                      STUDIO_METAGRAPH_PROBE_SECONDS per tick.
#
# A connector MAY also report a change token per table (table_signals) — a
# catalog's last-altered time, say. That only makes it cheaper: the tables
# whose token moved are re-read and nothing else is probed. Whatever the
# connector offers, the graph's record is the reference, so the snapshot
# handoff is exact and nothing a connector reports is trusted beyond "look
# at this table".

def _probe_budget():
    return max(0, _env_int("STUDIO_METAGRAPH_PROBE_TABLES", 100))


def _probe_seconds():
    return max(1, _env_int("STUDIO_METAGRAPH_PROBE_SECONDS", 20))


def offsets(source, adapter="native"):
    """(saved value, saved at) for one of a source's capture checkpoints:
    "native" (the connector's per-table tokens), "probe" (the rotation's
    place), "poll" (what the last poll did)."""
    with db.connect() as c:
        row = c.execute("SELECT value, updated_at FROM meta_cdc_offsets WHERE source=? AND "
                        "adapter=?", (source, adapter)).fetchone()
    return (json.loads(row["value"]), row["updated_at"]) if row else (None, None)


def save_offsets(source, value, adapter="native"):
    with db.connect() as c:
        c.execute("DELETE FROM meta_cdc_offsets WHERE source=? AND adapter=?", (source, adapter))
        c.execute("INSERT INTO meta_cdc_offsets (source, adapter, value, updated_at) "
                  "VALUES (?,?,?,?)", (source, adapter, json.dumps(value), time.time()))
        c.commit()


def reset_offsets(source):
    with db.connect() as c:
        c.execute("DELETE FROM meta_cdc_offsets WHERE source=?", (source,))
        c.commit()


def read_table_signals(connector):
    """The connector's {table: token}, or None when it offers none."""
    fn = getattr(connector, "table_signals", None)
    if fn is None:
        return None
    try:
        out = fn()
    except Exception:
        log.warning("metagraph: table_signals failed", exc_info=True)
        return None
    if not isinstance(out, dict):
        return None
    return {str(k)[:512]: str(v)[:512] for k, v in out.items() if str(k).strip()}


def event_id(source, asset, basis):
    """A change seen again — re-sent after a crash, or by two schedulers — gets
    the same id and is a duplicate. `basis` names the change against what it
    was compared with (the record's fingerprint and last change, or the token
    checkpoint's time), so a table that changes back and forth gets a new id
    each time."""
    return "poll:" + hashlib.sha256(f"{source}\x00{asset}\x00{basis}".encode()).hexdigest()[:40]


def _examine(source, connector, names, record):
    """{table: fresh fingerprint} for some of `names`, and how they were read:
    "bulk" (all at once), "probe" (the next ones in rotation, within the
    budget). A table that cannot be read now is skipped; it comes round again."""
    from . import metagraph
    out = {}
    fn = getattr(connector, "describe_relations", None)
    bulk = None
    if fn is not None:
        try:
            bulk = fn()
        except Exception:
            log.warning("metagraph: bulk describe of %s failed", source, exc_info=True)
    if isinstance(bulk, dict):
        for name in names:
            info = bulk.get(name)
            if info is not None:
                out[name] = metagraph._fingerprint(
                    info.get("kind") or "other", metagraph._clean_columns(info.get("columns")),
                    metagraph._description(info.get("description")))
    rest = sorted(n for n in names if n not in out)
    if not rest:
        return out, "bulk"
    (saved, _at) = offsets(source, "probe")
    start = (saved or {}).get("cursor", 0) % len(rest)
    order, began, probed = rest[start:] + rest[:start], time.monotonic(), 0
    for name in order[:_probe_budget()]:
        if time.monotonic() - began > _probe_seconds():
            break
        probed += 1
        try:
            info = metagraph._describe_one(connector, name, record[name], bulk=False)
        except Exception:
            log.debug("metagraph: probe of %s.%s failed", source, name, exc_info=True)
            continue
        out[name] = metagraph._fingerprint(info.get("kind") or record[name]["kind"],
                                           metagraph._clean_columns(info.get("columns")),
                                           metagraph._description(info.get("description")))
    save_offsets(source, {"cursor": (start + probed) % len(rest)}, "probe")
    return out, ("bulk" if isinstance(bulk, dict) else "probe")


def poll(source, connector, state, now):
    """Find the tables that changed since the graph last read them and turn
    each into an event (see the section comment). Returns what the caller
    does with the source-wide change signal:
      "per_table"  — handled here: skip it;
      "fallback"   — the source cannot be listed now, or was never
                     discovered: compare it (and the first discovery);
      "rediscover" — a change too big for events: rediscover the source. A
                     removal as large as the hold rule's goes this way, so the
                     hold applies; so do more changed tables than
                     STUDIO_METAGRAPH_MAX_TABLE_EVENTS.
    Token checkpoints move only after the events are accepted."""
    from . import metagraph
    if not state or not state.get("ok_at") or not connector.configured():
        return "fallback"
    try:
        names, complete = metagraph.listing(connector)
    except Exception:
        log.warning("metagraph: listing %s for change capture failed", source, exc_info=True)
        return "fallback"
    record = metagraph.recorded(source)
    live = {n for n, r in record.items() if r["removed_at"] is None}
    listed = set(names)
    created = [n for n in names if n not in live]
    # A removal is taken only from a complete listing, and never while
    # removals on this source are held (their confirmation settles them).
    dropped = sorted(live - listed) if complete and not state.get("held_removals") else []
    tokens = read_table_signals(connector)
    seen, seen_at = offsets(source, "native") if tokens is not None else (None, None)
    if seen is not None:
        how = "native"
        changed = {t: f"native|{seen_at}|{tokens[t]}" for t in tokens
                   if seen.get(t) != tokens[t] and t not in created}
    else:
        fresh, how = _examine(source, connector, [n for n in names if n in live], record)
        # Keyed on the record's fingerprint and last change: a re-read that
        # leaves the record as it was finds the same id next time (a duplicate,
        # never a loop), and changing back and forth gets a new one each time.
        changed = {t: f"{record[t]['fingerprint']}|{fp}|{record[t]['changed_at']}"
                   for t, fp in fresh.items() if fp != record[t]["fingerprint"]}
    minimum, fraction = metagraph._hold_rule()
    if dropped and len(dropped) >= minimum and len(dropped) >= fraction * max(1, len(live)):
        return "rediscover"
    if len(changed) + len(created) + len(dropped) > _max_table_events():
        return "rediscover"
    day = int(now // 86400)
    events = ([{"event_id": event_id(source, t, basis), "source": source, "asset": t,
                "event_type": "schema_changed", "occurred_at": now}
               for t, basis in sorted(changed.items())]
              + [{"event_id": event_id(source, t, f"created|{(record.get(t) or {}).get('removed_at')}"
                                                  f"|{day}"),
                  "source": source, "asset": t, "event_type": "asset_created",
                  "occurred_at": now} for t in created]
              + [{"event_id": event_id(source, t, f"dropped|{record[t]['changed_at']}"),
                  "source": source, "asset": t, "event_type": "asset_dropped",
                  "occurred_at": now} for t in dropped])
    if events:
        out = ingest(events, "poller", live_sources=[source])
        for r in out["rejected"]:
            log.warning("metagraph: poll of %s could not record %s: %s", source,
                        r["event_id"], r["reason"])
    if tokens is not None:
        save_offsets(source, tokens, "native")
    save_offsets(source, {"how": how, "tables": len(names), "events": len(events)}, "poll")
    return "per_table"


def after_snapshot(source, started_at, table_tokens):
    """The snapshot-to-stream handoff (see the module docstring)."""
    from . import metagraph
    if table_tokens is not None:
        save_offsets(source, table_tokens, "native")
    with db.connect() as c:
        metagraph._lock(c, source)
        names = [r["name"] for r in c.execute(
            "SELECT name FROM meta_objects WHERE source=? AND removed_at IS NULL",
            (source,)).fetchall()]
        known = {r["asset"]: r["read_at"] for r in c.execute(
            "SELECT asset, read_at FROM meta_asset_versions WHERE source=?",
            (source,)).fetchall()}
        now = time.time()
        for name in names:
            if name not in known:
                c.execute("INSERT INTO meta_asset_versions (source, asset, read_at, "
                          "updated_at) VALUES (?,?,?,?)", (source, name, started_at, now))
            elif (known[name] or 0) < started_at:
                c.execute("UPDATE meta_asset_versions SET read_at=?, updated_at=? "
                          "WHERE source=? AND asset=?", (started_at, now, source, name))
        c.commit()


def data_changed(source):
    """{table: when its data last changed}, from data_changed events."""
    with db.connect() as c:
        return {r["asset"]: r["data_changed_at"] for r in c.execute(
            "SELECT asset, data_changed_at FROM meta_asset_versions WHERE source=? "
            "AND data_changed_at IS NOT NULL", (source,)).fetchall()}


def forget(source):
    """A removed connection: its checkpoint and per-table versions go."""
    with db.connect() as c:
        c.execute("DELETE FROM meta_cdc_offsets WHERE source=?", (source,))
        c.execute("DELETE FROM meta_asset_versions WHERE source=?", (source,))
        c.commit()


def prune(now):
    """Forget settled events after STUDIO_METAGRAPH_EVENTS_KEEP_DAYS (dead
    ones stay until replayed), and the per-table versions of tables gone from
    the record."""
    cutoff = now - _keep_days() * 86400
    with db.connect() as c:
        cur = c.execute("DELETE FROM meta_events WHERE status IN ('applied', 'superseded', "
                        "'ignored') AND received_at < ?", (cutoff,))
        pruned = getattr(cur, "rowcount", 0) or 0
        c.execute("DELETE FROM meta_asset_versions WHERE updated_at < ? AND NOT EXISTS "
                  "(SELECT 1 FROM meta_objects o WHERE o.source = meta_asset_versions.source "
                  "AND o.name = meta_asset_versions.asset)", (cutoff,))
        c.commit()
    return pruned


# ── Status and administration ────────────────────────────────────────────

def status(source, now=None):
    """What an administrator needs about one source's change capture."""
    now = now or time.time()
    last, polled_at = offsets(source, "poll")
    with db.connect() as c:
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM meta_events WHERE source=? GROUP BY status",
            (source,)).fetchall()}
        oldest = c.execute("SELECT MIN(received_at) t FROM meta_events WHERE source=? "
                           "AND status='accepted'", (source,)).fetchone()["t"]
        newest = c.execute("SELECT MAX(received_at) t FROM meta_events WHERE source=?",
                           (source,)).fetchone()["t"]
    # How the last poll read the source: "native" (its own per-table tokens),
    # "bulk" (all columns in one read) or "probe" (tables in rotation).
    return {"source": source, "capture": (last or {}).get("how"),
            "tables": (last or {}).get("tables", 0), "polled_at": polled_at,
            "last_event_at": newest, "events": counts,
            "waiting_seconds": int(now - oldest) if oldest else 0,
            "dead": counts.get("dead", 0)}


def health(source, now):
    """(reasons this source's change capture needs attention, its status)."""
    st = status(source, now)
    out = []
    if st["dead"]:
        out.append(f"{st['dead']} metadata change event(s) failed on every attempt — "
                   f"replay them from the event log")
    if st["waiting_seconds"] > 900:
        out.append(f"metadata change events have waited {st['waiting_seconds'] // 60} min")
    return out, st


def _admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Only administrators can manage change capture")


@router.get("/events")
def list_events(source: str = None, status: str = None, limit: int = 100,
                user=Depends(current_user)):
    """The change-event log, newest first (administrators), filtered by
    source and by status: accepted, applied, superseded, ignored or dead."""
    _admin(user)
    if status is not None and status not in STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(STATUSES)}")
    clauses, params = [], []
    if source:
        clauses.append("source=?")
        params.append(source)
    if status:
        clauses.append("status=?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.connect() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM meta_events{where} ORDER BY received_at DESC LIMIT ?",
            (*params, max(1, min(int(limit), 500)))).fetchall()]
    for r in rows:
        r["result"] = json.loads(r["result"]) if r.get("result") else None
    return {"events": rows}


@router.post("/events/{event_id}/replay", status_code=202)
def replay_event(event_id: str, user=Depends(current_user)):
    _admin(user)
    jid = replay(event_id)
    if jid is None:
        raise HTTPException(404, "Not found")
    db.log_activity(user, "metagraph_event_replay", prompt=event_id)
    return {"event_id": event_id, "job_id": jid}


@router.get("/cdc")
def cdc_status(user=Depends(current_user)):
    """Change capture per live source (administrators): whether it is polled
    per table and its checkpoint, the last event, events by status, how long
    the oldest unapplied one has waited, and failures."""
    _admin(user)
    from . import metagraph
    return {"sources": [status(s) for s in metagraph._live_sources()]}


@router.post("/sources/{source}/cdc/reset", status_code=202)
def reset_cdc(source: str, user=Depends(current_user)):
    """Drop a source's checkpoint and queue a full rediscovery, which records
    a fresh one: change capture for that source, started over."""
    _admin(user)
    from . import metagraph
    if source not in metagraph._live_sources():
        raise HTTPException(404, "Not found")
    reset_offsets(source)
    jid = metagraph.enqueue(source, "cdc_reset", job_id=f"metagraph-cdc-reset-{uuid.uuid4()}")
    db.log_activity(user, "metagraph_cdc_reset", source=source)
    return {"source": source, "job_id": jid}
