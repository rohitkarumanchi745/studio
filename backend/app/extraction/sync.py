"""Orchestrates onboard, continuous delta and webhook-nudged sync.

Its ticker is cloned from autopilot exactly (race-safe claim, swallow-per-run,
reschedule-in-finally) and, like autopilot's, is RUN by the job worker
(jobs.Worker.run_schedulers) under the "m365_sync" lease rather than by a
thread of its own — doubly guarded by STUDIO_GRAPH_SYNC_TICKER AND
configured(): with no Azure config no tick runs and no Graph call is ever made.

Lifecycle:
- enqueue_onboard(user_id) is called from the signup / OAuth-callback hook. It is
  a NO-OP when unconfigured. Otherwise it ensures the user's private KAG
  collection m365-{user_id} at scope 'u:{user_id}', writes/updates the account
  row status='onboarding', and sets next_run_at=now so the ticker does the heavy
  first pull OFF the request thread (mirrors autopilot's proactive half; keeps
  signup fast; needs no LLM, safe on the ANTHROPIC_API_KEY-less Railway deploy).
- run_sync(user_id) is the onboard-or-delta pull (ticker + manual). Per drive/mail
  it walks the stored deltaLink (or the initial /delta), and for each added or
  changed item: skips >25MB, skips an unchanged etag, downloads the bytes,
  resolves the ACL scope (skip on None), ingests via kag.ingest_bytes under a
  synthesized admin identity into the private collection, and records the item in
  the ledger. Removed/tombstoned items are deleted via kag.delete_source and the
  ledger row dropped. Delta cursors are advanced; subscriptions renewed.
- process_notification(sub_id, client_state) validates the (decrypted) clientState
  with a constant-time compare and only NUDGES next_run_at=now. It NEVER fetches
  Graph, never ingests inline, and never trusts notification payload content — the
  authenticated pull is always the ticker's job.

Extracted document text is INERT reference exactly like existing KAG: it flows
through kag.ingest_bytes unchanged (quoted, no instruction execution), and Graph
metadata (filenames, subjects) is data, coerced through the name sanitizer,
never instructions.
"""
import hmac
import os
import time

from . import acl, configured, graphauth, parsers, store

# The synthesized identity KAG ingest runs as. role='admin' so it may create the
# private collection and is never blocked by RBAC reachability on ingest; the
# ISOLATION comes from the collection's fixed 'u:{user_id}' scope, not this id.
_ADMIN = {"id": "m365-sync-daemon", "email": "m365-sync@studio.local",
          "role": "admin", "name": "M365 Sync"}

MAX_ITEM_BYTES = int(os.getenv("STUDIO_GRAPH_MAX_BYTES", str(25 * 1024 * 1024)))
SYNC_INTERVAL = int(os.getenv("STUDIO_GRAPH_SYNC_INTERVAL", "900"))
_TICK_SECONDS = int(os.getenv("STUDIO_GRAPH_TICK_SECONDS", "60"))
_SUB_RENEW_WINDOW = 24 * 3600           # renew a subscription within a day of expiry
_SUB_TTL = int(os.getenv("STUDIO_GRAPH_SUB_TTL", str(3 * 24 * 3600)))

# Only file types kag can actually parse are worth downloading.
_SUPPORTED_EXT = {"xlsx", "xlsm", "csv", "pdf", "eml", "docx", "pptx"}


def init_tables():
    store.init_tables()


# ── Collection + onboard ─────────────────────────────────────────────────

def _ensure_collection(user_id):
    """The user's private KAG collection m365-{user_id} at scope 'u:{user_id}',
    created once (idempotent). Uses kag._create_collection directly with the
    synthesized admin so auto-create is allowed and the scope token (which
    contains ':') is stored verbatim, not run through the stricter name check."""
    from .. import kag
    name = f"m365-{user_id}"
    if kag._collection(name) is None:
        kag._create_collection(name, acl.SCOPE_PREFIX + user_id,
                               "Microsoft 365 documents (private)", _ADMIN)
    return name


def enqueue_onboard(user_id, auth_mode=None):
    """No-op when unconfigured. Otherwise ensure the collection + account row and
    schedule the heavy pull on the ticker (next_run_at=now)."""
    if not configured():
        return
    mode = auth_mode or os.getenv("STUDIO_GRAPH_AUTH_MODE", "delegated")
    _ensure_collection(user_id)
    store.save_account(user_id, mode, None)
    store.update_fields(user_id, {"status": "onboarding", "next_run_at": time.time()})


# ── The pull (onboard-or-delta) ──────────────────────────────────────────

def run_sync(user_id):
    """One onboard-or-delta pull for a user. Never raises: a hard failure marks
    the account 'error' (or 'revoked' if credentials are gone) and returns a
    result dict. Used by both the ticker and the manual endpoint."""
    if not configured():
        return {"ok": False, "reason": "unconfigured"}
    account = store.get_account(user_id)
    if not account:
        return {"ok": False, "reason": "not_connected"}
    auth = graphauth.for_user(user_id)
    if auth is None:
        return {"ok": False, "reason": "no_auth"}

    collection = _ensure_collection(user_id)
    stats = {"ingested": 0, "removed": 0, "skipped": 0}
    try:
        client = auth.client()
        principals = acl.principal_set(client, auth.principal_id())
        _sync_drive(user_id, auth, client, collection, principals, stats)
        _sync_mail(user_id, auth, client, collection, principals, stats)
        _renew_subscriptions(user_id, auth, client)
        store.update_fields(user_id, {"status": "connected", "last_error": None,
                                      "updated_at": time.time()})
        stats["ok"] = True
        return stats
    except Exception as e:
        # A run never propagates. If the credentials themselves are gone the
        # account is already 'revoked' (fail closed); otherwise it's a soft error
        # and the ticker will simply retry next interval.
        latest = store.get_account(user_id) or {}
        if latest.get("status") != "revoked":
            store.update_fields(user_id, {"status": "error",
                                          "last_error": str(e)[:300]})
        stats["ok"] = False
        stats["error"] = str(e)[:300]
        return stats


def _sync_drive(user_id, auth, client, collection, principals, stats):
    root = auth.root()
    url = store.get_delta(user_id, "drive") or f"{root}/drive/root/delta"
    while url:
        data = client.get(url)
        for item in (data.get("value") or []):
            _handle_drive_item(user_id, auth, client, collection, principals, item, stats)
        nxt = data.get("@odata.nextLink")
        if nxt:
            url = nxt
            continue
        link = data.get("@odata.deltaLink")
        if link:
            store.set_delta(user_id, "drive", link)
        url = None


def _handle_drive_item(user_id, auth, client, collection, principals, item, stats):
    from .. import kag
    graph_id = item.get("id")
    if not graph_id:
        return
    if item.get("deleted") is not None or item.get("@removed") is not None:
        _remove_item(user_id, graph_id, collection, stats)
        return
    if "file" not in item:                        # folders / other facets: skip
        return
    if (item.get("size") or 0) > MAX_ITEM_BYTES:
        stats["skipped"] += 1
        return
    name = item.get("name") or "document"
    ext = os.path.splitext(name.lower())[1].lstrip(".")
    if ext not in _SUPPORTED_EXT:
        stats["skipped"] += 1
        return
    etag = item.get("eTag") or item.get("cTag")
    existing = store.get_item(user_id, graph_id)
    if existing and etag and existing.get("etag") == etag:
        return                                    # unchanged since last sync
    scope = acl.resolve_access_scope({"id": user_id}, "drive", graph_id, client, principals)
    if scope is None:
        stats["skipped"] += 1                     # ACL denies / unresolved → skip
        return
    try:
        data = client.get_binary(f"{auth.root()}/drive/items/{graph_id}/content")
    except Exception:
        stats["skipped"] += 1
        return
    if len(data) > MAX_ITEM_BYTES:
        stats["skipped"] += 1
        return
    source_name = parsers.stable_source_name(graph_id, name)
    try:
        kag.ingest_bytes(_ADMIN, collection, source_name, data, access_scope=scope)
    except Exception:
        stats["skipped"] += 1
        return
    store.upsert_item(user_id, graph_id, "drive", etag, collection, source_name, scope)
    stats["ingested"] += 1


def _sync_mail(user_id, auth, client, collection, principals, stats):
    from .. import kag
    root = auth.root()
    url = store.get_delta(user_id, "mail") or f"{root}/mailFolders/inbox/messages/delta"
    while url:
        data = client.get(url)
        for msg in (data.get("value") or []):
            graph_id = msg.get("id")
            if not graph_id:
                continue
            if msg.get("@removed") is not None or msg.get("deleted") is not None:
                _remove_item(user_id, graph_id, collection, stats)
                continue
            etag = msg.get("@odata.etag") or msg.get("changeKey")
            existing = store.get_item(user_id, graph_id)
            if existing and etag and existing.get("etag") == etag:
                continue
            # The mailbox owner is the connected user → always granted.
            scope = acl.resolve_access_scope({"id": user_id}, "mail", graph_id, client, principals)
            if scope is None:
                stats["skipped"] += 1
                continue
            try:
                raw = client.get_binary(f"{root}/messages/{graph_id}/$value")
            except Exception:
                stats["skipped"] += 1
                continue
            if len(raw) > MAX_ITEM_BYTES:
                stats["skipped"] += 1
                continue
            subject = msg.get("subject") or "message"
            source_name = parsers.stable_source_name(graph_id, subject + ".eml")
            try:
                kag.ingest_bytes(_ADMIN, collection, source_name, raw, access_scope=scope)
            except Exception:
                stats["skipped"] += 1
                continue
            store.upsert_item(user_id, graph_id, "mail", etag, collection, source_name, scope)
            stats["ingested"] += 1
        nxt = data.get("@odata.nextLink")
        if nxt:
            url = nxt
            continue
        link = data.get("@odata.deltaLink")
        if link:
            store.set_delta(user_id, "mail", link)
        url = None


def _remove_item(user_id, graph_id, collection, stats):
    """Tombstone: delete the item's chunks from KAG and drop the ledger row."""
    from .. import kag
    row = store.get_item(user_id, graph_id)
    if row and row.get("source_name"):
        try:
            kag.delete_source(row.get("collection") or collection, row["source_name"])
        except Exception:
            pass
        store.delete_item(user_id, graph_id)
        stats["removed"] += 1


# ── Webhook subscriptions (best-effort, only with a public URL) ──────────

def _renew_subscriptions(user_id, auth, client):
    """Create (once) and renew change-notification subscriptions. Entirely
    best-effort and skipped when STUDIO_PUBLIC_URL is unset — sync then degrades
    to interval delta polling with the account still 'connected'."""
    public = os.getenv("STUDIO_PUBLIC_URL", "").rstrip("/")
    if not public:
        return
    notify_url = public + "/api/m365/webhook"
    try:
        _ensure_subscriptions(user_id, auth, client, notify_url)
    except Exception:
        pass


def _ensure_subscriptions(user_id, auth, client, notify_url):
    """One subscription per resource (drive root + inbox messages). Near expiry
    we DELETE the old one and create a fresh one (the client speaks create/delete,
    not PATCH), minting a new random clientState each time — never reusing a
    stale secret."""
    import secrets
    root = auth.root()
    wanted = (f"{root}/drive/root", f"{root}/mailFolders('inbox')/messages")
    have = {s["resource"]: s for s in store.subscriptions_for(user_id)}
    now = time.time()
    for resource in wanted:
        existing = have.get(resource)
        if existing and (existing.get("expires_at") or 0) - now > _SUB_RENEW_WINDOW:
            continue                              # still fresh — nothing to do
        if existing:
            try:
                client.delete(f"/subscriptions/{existing['id']}")
            except Exception:
                pass
            store.delete_subscription(existing["id"])
        client_state = secrets.token_urlsafe(24)
        body = {"changeType": "created,updated,deleted", "notificationUrl": notify_url,
                "resource": resource, "expirationDateTime": _iso(now + _SUB_TTL),
                "clientState": client_state}
        res = client.post("/subscriptions", json=body)
        sub_id = res.get("id")
        if sub_id:
            store.save_subscription(sub_id, user_id, resource, client_state, now + _SUB_TTL)


def _iso(epoch):
    import datetime
    return datetime.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_notification(sub_id, client_state):
    """Validate a change notification and NUDGE the account's next_run_at. Does
    NO Graph I/O and trusts no payload content. constant-time clientState check;
    any mismatch / unknown sub / undecryptable secret → False (ignored)."""
    sub = store.subscription(sub_id)
    if not sub:
        return False
    expected = sub.get("client_state")
    if expected is None:
        return False
    if not hmac.compare_digest(str(expected), str(client_state or "")):
        return False
    store.update_fields(sub["user_id"], {"next_run_at": time.time()})
    return True


# ── Disconnect / revoke ──────────────────────────────────────────────────

def disconnect(user_id, purge=False):
    """Tear down: delete Graph subscriptions (best-effort), wipe tokens, mark
    revoked, and optionally purge the private collection."""
    try:
        auth = graphauth.for_user(user_id)
        if auth is not None:
            client = auth.client()
            for sub in store.subscriptions_for(user_id):
                try:
                    client.delete(f"/subscriptions/{sub['id']}")
                except Exception:
                    pass
    except Exception:
        pass
    store.revoke(user_id)
    if purge:
        try:
            _purge_collection(user_id)
        except Exception:
            pass


def _purge_collection(user_id):
    from .. import db
    name = f"m365-{user_id}"
    c = db._conn()
    c.execute("DELETE FROM kag_chunks WHERE collection=?", (name,))
    c.execute("DELETE FROM kag_collections WHERE name=?", (name,))
    c.commit()
    c.close()
    cc = db._conn()
    cc.execute("DELETE FROM graph_items WHERE user_id=?", (user_id,))
    cc.commit()
    cc.close()


# ── The ticker (autopilot clone) ─────────────────────────────────────────

def _claim(now):
    return store.claim_due(now)


def tick_once():
    """One pass: claim due accounts, run each (isolated), reschedule + release in
    finally so one failure never kills the ticker. Called by the job worker
    under the "m365_sync" lease; tests call it directly. A no-op when
    unconfigured, so it is safe to call in any deployment."""
    if not configured():
        return
    now = time.time()
    for acct in _claim(now):
        user_id = acct["user_id"]
        try:
            run_sync(user_id)
        except Exception:
            pass
        finally:
            try:
                store.update_fields(user_id, {"next_run_at": time.time() + SYNC_INTERVAL,
                                              "claimed_at": None})
            except Exception:
                pass


_tick = tick_once


def ticker_enabled():
    """STUDIO_GRAPH_SYNC_TICKER kill-switch AND configured(): both are read on
    every tick, so an unconfigured or disabled deployment never takes the
    m365_sync lease."""
    if os.getenv("STUDIO_GRAPH_SYNC_TICKER", "1").lower() in ("0", "false", "no"):
        return False
    return configured()


def start_ticker():
    """Compatibility shim: the daemon ticker thread is gone. The job worker
    runs tick_once() under the "m365_sync" lease whenever ticker_enabled();
    nothing starts here, so the dormant guarantee (no thread, no Graph call)
    holds by construction."""
    return None


def stop_ticker():
    return None
