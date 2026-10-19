"""Send an answer to Microsoft Teams or Slack.

An administrator adds named destinations — a Teams channel or a Slack channel
— by their incoming-webhook URL, and says which roles may send to each. Under
any answer, a user who may send picks one and posts the answer there: its
text, the first rows of its result as a table, optionally its SQL, and a link
back to the conversation in Studio.

Posting data to a channel shares it with everyone in that channel, so:
- WEBHOOK URLs are secrets: stored encrypted (Fernet, the key connections use)
  and never returned by the API.
- A URL must be https, on the kind's own hosts — hooks.slack.com for Slack;
  *.webhook.office.com, *.logic.azure.com or a Power Platform environment for
  Teams (incoming webhooks and Workflows) — so a destination cannot be aimed
  at an internal address.
- ROLES: each destination names the roles that may send to it (admin and
  analyst by default). The rows sent are the ones the sender already sees —
  governed when their query ran — capped to a preview; Studio queries nothing.
- Message text is escaped: an answer cannot @channel a Slack channel or turn
  into a link.
- Every send, and every refused one, is an activity row ("share", its kind in
  mode).
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/share", tags=["share"])

KINDS = {
    "teams": {"label": "Microsoft Teams",
              "hosts": (".webhook.office.com", ".logic.azure.com",
                        ".environment.api.powerplatform.com", ".api.powerplatform.com")},
    "slack": {"label": "Slack", "hosts": ("hooks.slack.com",)},
}
ROLES = ("admin", "analyst", "viewer")
DEFAULT_ROLES = ["admin", "analyst"]
PREVIEW_ROWS = 15
PREVIEW_COLUMNS = 8
CELL = 60
TIMEOUT_S = 15


def init_tables():
    with db.connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS share_destinations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            url_enc TEXT NOT NULL,
            roles TEXT NOT NULL DEFAULT '["admin","analyst"]',
            created_by TEXT,
            created_at REAL NOT NULL
        );
        """)
        c.commit()


# ── Addresses ────────────────────────────────────────────────────────────

def check_url(kind, url):
    """None when `url` is a webhook of `kind` Studio may post to, else why not."""
    spec = KINDS.get(kind)
    if not spec:
        return "Choose Microsoft Teams or Slack."
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return "That is not a URL."
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return "A webhook URL starts with https://."
    if parts.username or parts.password or (parts.port not in (None, 443)):
        return "A webhook URL has no user, password or port."
    if not any(host == h or (h.startswith(".") and host.endswith(h)) or host == h.lstrip(".")
               for h in spec["hosts"]):
        where = ", ".join(h.lstrip(".") for h in spec["hosts"])
        return f"A {spec['label']} webhook is on {where}."
    return None


def _encrypt(url):
    from .connections import _fernet
    return _fernet().encrypt(url.encode()).decode()


def _decrypt(token):
    from .connections import _fernet
    try:
        return _fernet().decrypt(token.encode()).decode()
    except Exception:
        return None


def _row(r):
    d = dict(r)
    try:
        d["roles"] = json.loads(d.get("roles") or "[]")
    except (TypeError, ValueError):
        d["roles"] = list(DEFAULT_ROLES)
    return d


def _public(d):
    """A destination as the API shows it: never its URL."""
    return {"id": d["id"], "name": d["name"], "kind": d["kind"], "roles": d["roles"],
            "label": KINDS.get(d["kind"], {}).get("label", d["kind"])}


def _all():
    with db.connect() as c:
        return [_row(r) for r in c.execute(
            "SELECT * FROM share_destinations ORDER BY kind, name").fetchall()]


def _get(did):
    with db.connect() as c:
        r = c.execute("SELECT * FROM share_destinations WHERE id=?", (did,)).fetchone()
    return _row(r) if r else None


# ── Messages ─────────────────────────────────────────────────────────────

def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        v = f"{v:,.2f}" if abs(v) < 1e15 else str(v)
    elif isinstance(v, int) and not isinstance(v, bool):
        v = f"{v:,}"
    text = re.sub(r"\s+", " ", str(v)).strip()
    return text if len(text) <= CELL else text[:CELL - 1] + "…"


def preview(columns, rows):
    """(columns, rows, more): the first PREVIEW_COLUMNS columns and
    PREVIEW_ROWS rows as text, and how many rows were left out."""
    cols = [str(c) for c in (columns or [])][:PREVIEW_COLUMNS]
    shown = [[_cell(v) for v in list(r)[:len(cols)]] for r in (rows or [])[:PREVIEW_ROWS]]
    return cols, shown, max(0, len(rows or []) - len(shown))


def _slack_escape(text):
    # Slack's control characters: an answer must not @channel or link anything.
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _bare_links(text):
    """[label](url) → "label (url)": whoever reads the channel sees where a
    link goes — an answer's text never carries a link disguised as words."""
    return _LINK.sub(r"\1 (\2)", str(text or ""))


def _slack_mrkdwn(text):
    """An answer's Markdown as Slack mrkdwn, escaped: **bold** and headings
    become *bold*, *italic* becomes _italic_, bullets become •."""
    t = _slack_escape(_bare_links(text))
    t = re.sub(r"^(\s*)[-*+]\s+", "\\1• ", t, flags=re.M)
    t = re.sub(r"^#{1,6}\s+(.+?)\s*#*$", "\x00\\1\x00", t, flags=re.M)
    t = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"\x00{m.group(1) or m.group(2)}\x00", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"_\1_", t)
    return t.replace("\x00", "*")


def _teams_markdown(text):
    """An answer's Markdown as a Teams TextBlock takes it: no headings there
    (they become bold), links shown with their address."""
    return re.sub(r"^#{1,6}\s+(.+?)\s*#*$", r"**\1**", _bare_links(text), flags=re.M)


def _fixed_width(cols, rows):
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    line = lambda vals: "  ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line(cols), line(["─" * w for w in widths])] + [line(r) for r in rows])


def _rows_fact(m, shown):
    """"1,204 rows (first 15 shown)" — the whole result's size, which the
    client may send as row_count with only the rows a preview needs."""
    total = max(len(m.get("rows") or []), m.get("row_count") or 0)
    if not total or not m.get("columns"):
        return None
    return f"{total:,} rows" + (f" (first {shown} shown)" if total > shown else "")


def slack_message(m):
    """Slack Block Kit for an answer `m` ({title, text, columns, rows, sql?,
    source, sender, link?, note?})."""
    cols, rows, _ = preview(m.get("columns"), m.get("rows"))
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": m["title"][:150]}}]
    if m.get("note"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": f"_{_slack_escape(m['note'])[:2900]}_"}})
    if m.get("text"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": _slack_mrkdwn(m["text"])[:2900]}})
    if cols and rows:
        table = _slack_escape(_fixed_width(cols, rows))[:2900]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{table}```"}})
    if m.get("sql"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                   "text": f"```{_slack_escape(m['sql'])[:2900]}```"}})
    facts = [f for f in [_rows_fact(m, len(rows))] if f]
    if m.get("source"):
        facts.append(f"source *{_slack_escape(m['source'])}*")
    facts.append(f"sent from Studio by {_slack_escape(m['sender'])}")
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(facts)}]})
    if m.get("link"):
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "text": {"type": "plain_text", "text": "Open in Studio"},
            "url": m["link"]}]})
    return {"text": m["title"][:150], "blocks": blocks}


def teams_message(m):
    """A Teams message carrying an Adaptive Card (accepted by incoming
    webhooks and by Workflows' "post to a channel when a webhook request is
    received")."""
    cols, rows, _ = preview(m.get("columns"), m.get("rows"))
    body = [{"type": "TextBlock", "text": m["title"][:150], "weight": "Bolder", "size": "Medium",
             "wrap": True}]
    if m.get("note"):
        body.append({"type": "TextBlock", "text": m["note"][:2000], "wrap": True, "isSubtle": True})
    if m.get("text"):
        body.append({"type": "TextBlock", "text": _teams_markdown(m["text"])[:4000], "wrap": True})
    if cols and rows:
        body.append({
            "type": "Table", "firstRowAsHeader": True, "gridStyle": "accent",
            "columns": [{"width": 1} for _ in cols],
            "rows": [{"type": "TableRow", "cells": [
                {"type": "TableCell", "items": [{"type": "TextBlock", "text": v or " ", "wrap": True,
                                                 **({"weight": "Bolder"} if i == 0 else {})}]}
                for v in r]} for i, r in enumerate([cols] + rows)]})
    if m.get("sql"):
        body.append({"type": "TextBlock", "text": m["sql"][:3000], "fontType": "Monospace",
                     "wrap": True, "size": "Small"})
    facts = [f for f in [_rows_fact(m, len(rows))] if f]
    if m.get("source"):
        facts.append(f"source {m['source']}")
    facts.append(f"sent from Studio by {m['sender']}")
    body.append({"type": "TextBlock", "text": " · ".join(facts), "isSubtle": True, "size": "Small",
                 "wrap": True})
    card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5", "body": body, "msteams": {"width": "Full"}}
    if m.get("link"):
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open in Studio", "url": m["link"]}]
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": card}]}


def _post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"The channel refused the message ({e.code}).")
    except Exception as e:
        raise HTTPException(502, f"Could not reach the channel ({type(e).__name__}).")


# ── API ──────────────────────────────────────────────────────────────────

class DestinationIn(BaseModel):
    name: str
    kind: str
    url: str
    roles: Optional[List[str]] = None
    test: bool = False


class SendIn(BaseModel):
    destination_id: str
    title: str
    text: Optional[str] = None
    note: Optional[str] = None
    columns: List[str] = []
    rows: List[list] = []          # the preview needs only the first PREVIEW_ROWS
    row_count: Optional[int] = None  # the whole result's size, when rows is a preview
    sql: Optional[str] = None
    include_sql: bool = False
    include_rows: bool = True
    source: Optional[str] = None
    conversation_id: Optional[str] = None


def _admin(user):
    if user["role"] != "admin":
        raise HTTPException(403, "Only administrators manage Teams and Slack channels")


def _may_send(user, d):
    return user["role"] in (d.get("roles") or DEFAULT_ROLES)


@router.get("/destinations")
def list_destinations(user=Depends(current_user)):
    """An admin sees every destination (with who may send); anyone else sees
    the ones their role may send to."""
    rows = _all()
    if user["role"] == "admin":
        return {"destinations": [_public(d) for d in rows], "kinds": {
            k: v["label"] for k, v in KINDS.items()}}
    return {"destinations": [_public(d) for d in rows if _may_send(user, d)],
            "kinds": {k: v["label"] for k, v in KINDS.items()}}


@router.post("/destinations", status_code=201)
def add_destination(body: DestinationIn, user=Depends(current_user)):
    _admin(user)
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Give the channel a name")
    problem = check_url(body.kind, body.url)
    if problem:
        raise HTTPException(400, problem)
    roles = [r for r in (body.roles if body.roles is not None else DEFAULT_ROLES) if r in ROLES]
    if "admin" not in roles:
        roles.append("admin")
    if body.test:
        _post(body.url.strip(), _message(body.kind, {
            "title": "Studio is connected", "sender": user["email"],
            "text": f"Answers from Studio can now be sent to “{name}”.", "columns": [], "rows": []}))
    d = {"id": str(uuid.uuid4()), "name": name, "kind": body.kind,
         "url_enc": _encrypt(body.url.strip()), "roles": json.dumps(sorted(set(roles))),
         "created_by": user["id"], "created_at": time.time()}
    with db.connect() as c:
        c.execute("INSERT INTO share_destinations (id, name, kind, url_enc, roles, created_by, "
                  "created_at) VALUES (?,?,?,?,?,?,?)",
                  tuple(d[k] for k in ("id", "name", "kind", "url_enc", "roles", "created_by",
                                       "created_at")))
        c.commit()
    db.log_activity(user, "share_destination_add", prompt=name, mode=body.kind)
    return _public(_get(d["id"]))


@router.delete("/destinations/{did}")
def remove_destination(did: str, user=Depends(current_user)):
    _admin(user)
    if _get(did) is None:
        raise HTTPException(404, "Not found")
    with db.connect() as c:
        c.execute("DELETE FROM share_destinations WHERE id=?", (did,))
        c.commit()
    db.log_activity(user, "share_destination_remove", prompt=did)
    return {"removed": True}


def _message(kind, m):
    return teams_message(m) if kind == "teams" else slack_message(m)


def _link(request, user, cid):
    """A link back to the conversation — only one the sender may open. A D3 &
    artifacts conversation (its title starts 🎨) opens in that window."""
    if not cid or db.conversation_access(cid, user["id"]) is None:
        return None
    with db.connect() as c:
        row = c.execute("SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()
    where = "visuals/c" if row and str(row["title"] or "").startswith("🎨") else "c"
    return f"{str(request.base_url).rstrip('/')}/{where}/{urllib.parse.quote(cid)}"


@router.post("/send")
def send(body: SendIn, request: Request, user=Depends(current_user)):
    """Post an answer to a destination the sender's role may use."""
    d = _get(body.destination_id)
    if d is None or not _may_send(user, d):
        raise HTTPException(404, "Destination not found")
    url = _decrypt(d["url_enc"])
    if not url or check_url(d["kind"], url):
        raise HTTPException(409, "This channel's webhook can no longer be used — ask an "
                                 "administrator to add it again")
    m = {"title": (body.title or "Studio answer").strip()[:150], "text": body.text, "note": body.note,
         "columns": body.columns if body.include_rows else [],
         "rows": body.rows if body.include_rows else [],
         "row_count": body.row_count if body.include_rows else None,
         "sql": body.sql if body.include_sql else None, "source": body.source,
         "sender": user["email"], "link": _link(request, user, body.conversation_id)}
    try:
        _post(url, _message(d["kind"], m))
    except HTTPException as e:
        db.log_activity(user, "share", prompt=m["title"], mode=d["kind"], ok=False,
                        error=str(e.detail)[:300])
        raise
    db.log_activity(user, "share", prompt=m["title"], mode=d["kind"], sql=m["sql"],
                    row_count=max(len(m["rows"]), m["row_count"] or 0), source=body.source)
    return {"sent": True, "destination": d["name"], "kind": d["kind"]}
