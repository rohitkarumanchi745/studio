"""Send an answer to Microsoft Teams or Slack.

Proves, through the real API with the channels' webhooks stood in for:
  - only Slack's and Microsoft's webhook hosts are accepted (no internal
    address, look-alike host, plain http, port or user), and only an
    administrator adds a channel — its URL encrypted, never returned;
  - each role sees and sends to only the channels it may;
  - Slack gets Block Kit with the answer escaped (no @channel, no links), a
    capped table, SQL only when asked, and a link back to a conversation the
    sender may open; Teams gets an Adaptive Card with a Table;
  - a channel that refuses the message is a 502; every send is audited.

Run from the backend directory:
    python -m pytest tests/test_share_destinations.py -q
"""
import json
import warnings

import pytest
from fastapi.testclient import TestClient

from app import destinations

warnings.filterwarnings("ignore")

SLACK = "https://hooks.slack.com/services/T000/B000/XXXXSECRET"
TEAMS = "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=SECRET"


@pytest.mark.parametrize("kind, url", [
    ("slack", SLACK),
    ("teams", "https://acme.webhook.office.com/webhookb2/abc@def/IncomingWebhook/x/y"),
    ("teams", TEAMS),
    ("teams", "https://default123.environment.api.powerplatform.com/powerautomate/automations/x"),
])
def test_real_webhooks_are_accepted(kind, url):
    assert destinations.check_url(kind, url) is None


@pytest.mark.parametrize("kind, url", [
    ("slack", "http://hooks.slack.com/services/x"),                   # not https
    ("slack", "https://hooks.slack.com.evil.example/services/x"),     # look-alike
    ("slack", "https://127.0.0.1/hook"), ("slack", "https://localhost/hook"),
    ("slack", "https://user:pw@hooks.slack.com/services/x"),
    ("slack", "https://hooks.slack.com:8443/services/x"),
    ("teams", SLACK),                                                  # a Slack URL for Teams
    ("teams", "https://evilwebhook.office.com/x"),                    # not a subdomain
    ("discord", SLACK), ("slack", "not a url"),
])
def test_anything_else_is_refused(kind, url):
    assert destinations.check_url(kind, url)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("STUDIO_DEMO_MODE", "1")
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    import importlib

    from app import db as _db
    importlib.reload(_db)
    import app.main as main
    importlib.reload(main)
    from app import db
    posted = []

    class Resp:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        if env_ns.fail:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        posted.append((req.full_url, json.loads(req.data)))
        return Resp()
    monkeypatch.setattr(destinations.urllib.request, "urlopen", urlopen)
    with TestClient(main.app) as c:
        def login(email, password):
            tok = c.post("/api/auth/login", json={"email": email, "password": password}
                         ).json()["access_token"]
            return {"Authorization": f"Bearer {tok}"}
        env_ns = type("Env", (), {})()
        env_ns.c, env_ns.db, env_ns.posted, env_ns.fail = c, db, posted, False
        env_ns.admin = login("admin@studio.local", "admin123")
        env_ns.analyst = login("analyst@studio.local", "analyst123")
        env_ns.viewer = login("viewer@studio.local", "viewer123")
        yield env_ns


def add(env, **kw):
    r = env.c.post("/api/share/destinations", headers=env.admin,
                   json={"name": "#sales", "kind": "slack", "url": SLACK, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def test_only_an_admin_adds_a_channel_and_its_url_is_never_shown(env):
    r = env.c.post("/api/share/destinations", headers=env.analyst,
                   json={"name": "#x", "kind": "slack", "url": SLACK})
    assert r.status_code == 403
    r = env.c.post("/api/share/destinations", headers=env.admin,
                   json={"name": "#x", "kind": "slack", "url": "https://evil.example/hook"})
    assert r.status_code == 400 and "hooks.slack.com" in r.json()["detail"]
    d = add(env, test=True, roles=["analyst"])
    assert d["roles"] == ["admin", "analyst"]                        # an admin may always send
    assert env.posted[-1][0] == SLACK
    assert env.posted[-1][1]["blocks"][0]["text"]["text"] == "Studio is connected"
    listing = env.c.get("/api/share/destinations", headers=env.admin).json()
    assert "SECRET" not in json.dumps(listing)
    with env.db.connect() as conn:
        stored = conn.execute("SELECT url_enc FROM share_destinations").fetchone()["url_enc"]
    assert "hooks.slack.com" not in stored and "SECRET" not in stored


def test_each_role_sees_and_sends_only_where_it_may(env):
    open_to_all = add(env, name="#everyone", roles=["admin", "analyst", "viewer"])
    analysts = add(env, name="#analysts", kind="teams", url=TEAMS)
    seen = lambda h: {d["name"] for d in env.c.get("/api/share/destinations", headers=h).json()["destinations"]}
    assert seen(env.analyst) == {"#everyone", "#analysts"}
    assert seen(env.viewer) == {"#everyone"}
    r = env.c.post("/api/share/send", headers=env.viewer, json={
        "destination_id": analysts["id"], "title": "x", "columns": [], "rows": []})
    assert r.status_code == 404
    assert env.c.post("/api/share/send", headers=env.viewer, json={
        "destination_id": open_to_all["id"], "title": "x"}).status_code == 200


ANSWER = {"title": "Revenue by region",
          "text": "## Summary\n**West** leads. <!channel> see <https://evil.example|here>",
          "columns": ["region", "revenue"], "rows": [[f"R{i}", 1000.5 * i] for i in range(20)],
          "sql": "SELECT region, SUM(revenue) FROM sales GROUP BY region", "source": "demo"}


def test_slack_gets_the_answer_escaped_a_capped_table_and_a_link_back(env):
    d = add(env)
    cid = env.c.post("/api/chat", headers=env.analyst, json={
        "prompt": "revenue by region", "source": "demo", "table": "*"}).json()["conversation_id"]
    r = env.c.post("/api/share/send", headers=env.analyst, json={
        **ANSWER, "destination_id": d["id"], "conversation_id": cid, "note": "For Monday's review"})
    assert r.json() == {"sent": True, "destination": "#sales", "kind": "slack"}
    blocks = env.posted[-1][1]["blocks"]
    text = json.dumps(blocks)
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": "Revenue by region"}}
    assert "&lt;!channel&gt;" in text and "<!channel>" not in text
    assert "*Summary*\\n*West* leads." in text and "**West**" not in text   # Slack's own bold
    assert "<https://evil.example" not in text
    assert "For Monday" in text
    table = next(b["text"]["text"] for b in blocks if b["text"]["text"].startswith("```region"))
    assert table.count("\n") == 16                                    # header, rule, 15 rows
    assert "1,000.50" in table
    assert "20 rows (first 15 shown)" in text and "analyst@studio.local" in text
    assert "SELECT region" not in text                                # SQL only when asked
    button = blocks[-1]["elements"][0]
    assert button["text"]["text"] == "Open in Studio" and button["url"].endswith(f"/c/{cid}")
    # SQL when asked; no link to a conversation the sender cannot open.
    theirs = env.c.post("/api/chat", headers=env.admin, json={
        "prompt": "revenue", "source": "demo", "table": "*"}).json()["conversation_id"]
    env.c.post("/api/share/send", headers=env.analyst, json={
        **ANSWER, "destination_id": d["id"], "include_sql": True, "conversation_id": theirs})
    text = json.dumps(env.posted[-1][1])
    assert "SELECT region" in text and "Open in Studio" not in text


def test_a_preview_carries_the_whole_results_size_and_a_d3_chat_links_to_its_window(env):
    d = add(env)
    cid = env.c.post("/api/chat", headers=env.analyst, json={
        "prompt": "revenue by region", "source": "demo", "table": "*"}).json()["conversation_id"]
    with env.db.connect() as conn:
        conn.execute("UPDATE conversations SET title=? WHERE id=?", ("🎨 revenue", cid))
        conn.commit()
    env.c.post("/api/share/send", headers=env.analyst, json={
        **ANSWER, "rows": ANSWER["rows"][:15], "row_count": 1204, "destination_id": d["id"],
        "conversation_id": cid})
    text = json.dumps(env.posted[-1][1])
    assert "1,204 rows (first 15 shown)" in text
    assert env.posted[-1][1]["blocks"][-1]["elements"][0]["url"].endswith(f"/visuals/c/{cid}")
    assert next(a for a in env.db.list_activity() if a["action"] == "share")["row_count"] == 1204
    # No table sent, no row count claimed; a short result is not "first N shown".
    env.c.post("/api/share/send", headers=env.analyst, json={
        **ANSWER, "row_count": 1204, "include_rows": False, "destination_id": d["id"]})
    assert " rows" not in json.dumps(env.posted[-1][1]) and "```region" not in json.dumps(env.posted[-1][1])
    assert next(a for a in env.db.list_activity() if a["action"] == "share")["row_count"] == 0
    env.c.post("/api/share/send", headers=env.analyst, json={
        **ANSWER, "rows": ANSWER["rows"][:3], "destination_id": d["id"]})
    text = json.dumps(env.posted[-1][1], ensure_ascii=False)
    assert "3 rows · source" in text and "shown" not in text


def test_an_answers_markdown_reads_right_in_each_and_no_link_is_disguised():
    md = ("## Summary\n**West** leads, *ahead* of East.\n- one\n* two\n"
          "See [the report](https://evil.example/x) and 2*3*4.")
    slack = destinations._slack_mrkdwn(md)
    assert slack.splitlines() == ["*Summary*", "*West* leads, _ahead_ of East.", "• one", "• two",
                                  "See the report (https://evil.example/x) and 2*3*4."]
    assert destinations._slack_mrkdwn("a <!here> & b") == "a &lt;!here&gt; &amp; b"
    teams = destinations._teams_markdown(md)
    assert teams.splitlines()[:2] == ["**Summary**", "**West** leads, *ahead* of East."]
    assert "the report (https://evil.example/x)" in teams and "](" not in teams


def test_teams_gets_an_adaptive_card_with_a_table(env):
    d = add(env, name="Data team", kind="teams", url=TEAMS)
    env.c.post("/api/share/send", headers=env.analyst, json={**ANSWER, "destination_id": d["id"]})
    url, payload = env.posted[-1]
    assert url == TEAMS
    card = payload["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    body = card["content"]["body"]
    assert card["content"]["version"] == "1.5"
    assert body[0]["text"] == "Revenue by region"
    assert body[1]["text"].startswith("**Summary**\n**West** leads.")       # no headings in Teams
    table = next(b for b in body if b["type"] == "Table")
    assert table["firstRowAsHeader"] and len(table["rows"]) == 16       # header + 15
    assert table["rows"][0]["cells"][0]["items"][0]["text"] == "region"


def test_a_refused_message_is_an_error_and_every_send_is_audited(env):
    d = add(env)
    env.fail = True
    r = env.c.post("/api/share/send", headers=env.analyst, json={**ANSWER, "destination_id": d["id"]})
    assert r.status_code == 502 and "refused the message (403)" in r.json()["detail"]
    env.fail = False
    env.c.post("/api/share/send", headers=env.analyst, json={**ANSWER, "destination_id": d["id"]})
    rows = [a for a in env.db.list_activity() if a["action"] == "share"]
    assert [(a["mode"], bool(a["ok"])) for a in rows][::-1] == [("slack", False), ("slack", True)]
    assert rows[0]["row_count"] == 20
    # Removing is an admin's job; a removed channel is gone.
    assert env.c.delete(f"/api/share/destinations/{d['id']}", headers=env.analyst).status_code == 403
    assert env.c.delete(f"/api/share/destinations/{d['id']}", headers=env.admin).json() == {"removed": True}
    assert env.c.post("/api/share/send", headers=env.analyst, json={
        **ANSWER, "destination_id": d["id"]}).status_code == 404
