"""Sharing under dynamic governance, read-time compliance, row retention and
the rerun endpoint's gateway migration.

Proves: (a) a governance YAML that tightens a role also tightens what that
role is shown from stored chat history (whole-source messages); (b) the SQL a
message actually ran, not its client-supplied table label, decides who may
see it; (c) a mask_columns rule applied AFTER rows were stored masks them at
read time — for the owner too — without touching the DB row, and a rule
applied by ANOTHER PROCESS reaches this one's read path within one governance
refresh; (d) retention strips rows from old assistant messages only;
(e) POST /chat/rerun runs through the gateway (one "rerun" audit row) and
returns governed columns.

Run from the backend directory:
    python -m pytest tests/test_sharing_governance.py -q
"""
import json
import os
import tempfile
import time
import uuid
import warnings

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-sharing-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import chat, db, governance

warnings.filterwarnings("ignore")

ANA = {"id": "u-share-ana", "email": "share-ana@studio.test", "role": "analyst", "name": "Ana"}
VIEW = {"id": "u-share-view", "email": "share-view@studio.test", "role": "viewer", "name": "Vi"}

TIGHTEN_ANALYST = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: [sales] } }
  viewer: { sources: { demo: [sales, web_traffic] } }
"""

MASK_LTV = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*" } }
  viewer: { sources: { demo: [sales, web_traffic] } }
compliance:
  demo:
    customers:
      mask_columns: [lifetime_value]
"""


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    governance.init_tables()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac(monkeypatch):
    """Every test starts on built-in RBAC with an EMPTY governance store; a doc
    a test loads (in process, or through the store as another replica would) is
    dropped after, or the freshness refresh would carry it into the next test."""
    monkeypatch.delenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS", raising=False)
    _clear_store()
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)
    yield
    _clear_store()
    governance._STATE.update(doc=None, yaml="", source=None)
    governance._FRESH.update(at=0.0, ident=None)


def _clear_store():
    try:
        c = db._conn()
        c.execute("DELETE FROM governance_docs")
        c.commit()
        c.close()
    except Exception:
        pass


def _apply_elsewhere(text):
    """An apply by a DIFFERENT process: the document lands in governance_docs
    and nothing tells this one. Only its freshness check can notice."""
    c = db._conn()
    c.execute("INSERT INTO governance_docs (id, yaml, applied_by, applied_at) VALUES (?,?,?,?)",
              (uuid.uuid4().hex, text, "other-replica@studio.test", time.time()))
    c.commit()
    c.close()


def _chat_with(owner, content):
    cid = db.create_conversation(owner["id"], "t")
    mid = db.add_message(cid, "assistant", content)
    return cid, mid


def _first(cid, user):
    access = db.conversation_access(cid, user["id"]) or "view"
    return chat._visible_messages(cid, user, access)[0]["content"]


# ── (a) governance tightening reaches stored whole-source messages ───────

def test_whole_source_message_hidden_once_governance_tightens_the_role():
    cid, _ = _chat_with(ANA, {"text": "all of demo", "source": "demo", "table": "*",
                              "author_role": "analyst", "sql": "SELECT region FROM sales",
                              "columns": ["region"], "rows": [["EU"]]})
    assert not _first(cid, ANA).get("redacted")            # built-in: analyst.demo == "*"
    governance._set(TIGHTEN_ANALYST, "test")
    assert governance.loaded()
    assert _first(cid, ANA).get("redacted")                # analyst.demo is now [sales] only
    assert chat._hidden_count(cid, "analyst") == 1


# ── (b) the SQL, not the label, decides ─────────────────────────────────

def test_table_label_is_not_trusted_when_sql_reads_another_table():
    honest = {"text": "sales", "source": "demo", "table": "sales", "author_role": "viewer",
              "sql": "SELECT region FROM sales", "columns": ["region"], "rows": [["EU"]]}
    lying = {**honest, "sql": "SELECT name FROM customers", "columns": ["name"],
             "rows": [["Ada"]]}
    assert chat._msg_allowed("viewer", honest)
    assert not chat._msg_allowed("viewer", lying)
    assert chat._msg_allowed("analyst", lying)              # analyst may read customers
    # A panel's SQL counts too, and a CTE name is not a table.
    with_panel = {**honest, "panels": [{"sql": "WITH t AS (SELECT * FROM customers) "
                                                "SELECT * FROM t", "columns": ["x"],
                                        "rows": [[1]]}]}
    assert not chat._msg_allowed("viewer", with_panel)
    # Unattributable SQL (a file path) fails closed to the whole-source rule.
    opaque = {**honest, "sql": "SELECT * FROM 's3://b/x.parquet'"}
    assert not chat._msg_allowed("viewer", opaque)
    assert chat._msg_allowed("analyst", opaque)


# ── (c) read-time compliance, owner included, DB untouched ──────────────

def test_mask_rule_applied_later_masks_stored_rows_for_the_owner():
    sql = "SELECT name, lifetime_value FROM customers"
    content = {"text": "ltv", "source": "demo", "table": "customers", "author_role": "analyst",
               "sql": sql, "columns": ["name", "lifetime_value"], "rows": [["Ada", 4200.0]],
               "panels": [{"sql": sql, "columns": ["name", "lifetime_value"],
                           "rows": [["Ada", 4200.0]], "chart": None}]}
    cid, mid = _chat_with(ANA, content)
    assert _first(cid, ANA)["rows"] == [["Ada", 4200.0]]
    governance._set(MASK_LTV, "test")
    seen = _first(cid, ANA)
    assert seen["columns"] == ["name", "lifetime_value"]
    assert seen["rows"] == [["Ada", "***"]]
    assert seen["panels"][0]["rows"] == [["Ada", "***"]]
    assert seen["text"] == "ltv" and seen["sql"] == sql   # everything else intact
    # The stored row is what it was: masking is a read-time view, not a rewrite.
    c = db._conn()
    raw = json.loads(c.execute("SELECT content FROM messages WHERE id=?", (mid,)).fetchone()["content"])
    c.close()
    assert raw["rows"] == [["Ada", 4200.0]] and raw["panels"][0]["rows"] == [["Ada", 4200.0]]


def test_a_mask_rule_applied_by_another_process_reaches_this_one_at_read_time():
    """(c) again, but the rule is applied by a DIFFERENT replica: it is written
    to governance_docs and this process is never told. Stored rows keep coming
    back unmasked until its freshness check fires — then the read path masks
    them, with no restart and no PUT handled here. Without that check a second
    web worker would serve the pre-policy values indefinitely."""
    sql = "SELECT name, lifetime_value FROM customers"
    content = {"text": "ltv", "source": "demo", "table": "customers", "author_role": "analyst",
               "sql": sql, "columns": ["name", "lifetime_value"], "rows": [["Ada", 4200.0]]}
    cid, _ = _chat_with(ANA, content)
    assert _first(cid, ANA)["rows"] == [["Ada", 4200.0]]

    _apply_elsewhere(MASK_LTV)
    assert _first(cid, ANA)["rows"] == [["Ada", 4200.0]]   # inside the refresh TTL
    governance._FRESH["at"] -= 3600                        # one refresh interval later
    assert _first(cid, ANA)["rows"] == [["Ada", "***"]]
    assert governance.loaded() and governance.active_source() == "database"


def test_read_time_refilter_hides_rows_reached_through_a_cte_alias():
    """The alias dodge at read time: rows stored under no rule, then a deny
    or mask rule lands. A derived-table/CTE shape is opaque, so a denied
    column named inside hides the rows; a masked one masks every value."""
    deny = MASK_LTV.replace("mask_columns: [lifetime_value]", "deny_columns: [lifetime_value]")
    cte = "WITH t AS (SELECT name, lifetime_value AS l FROM customers) SELECT * FROM t"
    content = {"text": "ltv", "source": "demo", "table": "customers", "author_role": "analyst",
               "sql": cte, "columns": ["name", "l"], "rows": [["Ada", 4200.0]]}
    cid, _ = _chat_with(ANA, content)
    assert _first(cid, ANA)["rows"] == [["Ada", 4200.0]]
    governance._set(MASK_LTV, "test")
    seen = _first(cid, ANA)
    assert seen["rows"] == [["***", "***"]] and not seen.get("redacted")
    governance._set(deny, "test")
    seen = _first(cid, ANA)
    assert seen["rows"] == [] and seen["columns"] == [] and seen["text"] == "ltv"


def test_checkpoint_snapshots_the_reader_view_not_the_raw_transcript():
    """An edit-recipient's follow-up checkpoints the conversation into THEIR
    agent_sessions row; what their role may not see must land there as the
    redaction placeholder, never as the hidden text."""
    from app import sessions
    sessions.init_tables()
    secret = "Top customer is Ada Lovelace (ada@x.com), LTV 4200."
    cid = db.create_conversation(ANA["id"], "shared")
    db.add_message(cid, "user", {"text": "who are the top customers?", "source": "demo",
                                 "table": "customers", "author_role": "analyst"})
    db.add_message(cid, "assistant", {"text": secret, "source": "demo", "table": "customers",
                                      "author_role": "analyst",
                                      "sql": "SELECT name, lifetime_value FROM customers",
                                      "columns": ["name", "lifetime_value"],
                                      "rows": [["Ada Lovelace", 4200.0]]})
    db.share_conversation(cid, VIEW["id"], "edit")
    chat._checkpoint(VIEW, cid, {"text": "ok"}, "m", "demo", "customers")
    c = db._conn()
    row = c.execute("SELECT messages FROM agent_sessions WHERE conversation_id=? AND user_id=?",
                    (cid, VIEW["id"])).fetchone()
    c.close()
    assert row is not None
    texts = [m.get("text") or m.get("content") for m in json.loads(row["messages"])]
    assert secret not in " ".join(str(t) for t in texts)
    assert any(chat._REDACTED in str(t) for t in texts)
    # The owner's own checkpoint keeps the full transcript.
    chat._checkpoint(ANA, cid, {"text": "ok"}, "m", "demo", "customers")
    c = db._conn()
    row = c.execute("SELECT messages FROM agent_sessions WHERE conversation_id=? AND user_id=?",
                    (cid, ANA["id"])).fetchone()
    c.close()
    assert secret in row["messages"]


def test_questions_stay_visible_to_a_restricted_reader():
    """User turns and clarifications carry no rows: a viewer reading a shared
    orchestrated chat sees the questions, and only the data-bearing answers
    are redacted."""
    q = {"text": "revenue by region across everything", "source": "*", "table": "all sources",
         "author_role": "analyst"}
    assert chat._msg_allowed("viewer", q, "user")
    assert chat._msg_allowed("viewer", {"text": "q", "source": "demo", "table": "*",
                                        "author_role": "viewer"}, "user")
    clarify = {"text": "which source?", "mode": "clarify", "source": "*", "table": "all sources",
               "author_role": "analyst", "rows": [], "panels": [], "sql": None}
    assert chat._msg_allowed("viewer", clarify, "assistant")
    # Anything carrying data — or SQL, even with purged rows — keeps the full gate.
    answer = {"text": "Ada tops the list", "source": "*", "table": "all sources",
              "author_role": "analyst", "sql": "SELECT name FROM customers", "rows": []}
    assert not chat._msg_allowed("viewer", answer, "assistant")
    assert not chat._msg_allowed("viewer", {**answer, "sql": None, "rows": [["Ada"]]}, "assistant")
    cid = db.create_conversation(ANA["id"], "orch")
    db.add_message(cid, "user", q)
    db.add_message(cid, "assistant", {**answer, "rows": [["Ada"]], "columns": ["name"]})
    db.share_conversation(cid, VIEW["id"], "view")
    shown = chat._visible_messages(cid, VIEW, "view")
    assert shown[0]["content"]["text"] == q["text"] and not shown[0]["content"].get("redacted")
    assert shown[1]["content"].get("redacted")
    assert chat._hidden_count(cid, "viewer") == 1


# ── (d) retention strips old rows only ──────────────────────────────────

def test_purge_message_rows_strips_old_rows_and_keeps_fresh_ones(monkeypatch):
    body = {"text": "old", "source": "demo", "table": "sales", "author_role": "analyst",
            "sql": "SELECT region FROM sales", "columns": ["region"], "rows": [["EU"]],
            "chart": {"type": "bar"},
            "panels": [{"sql": "SELECT region FROM sales", "columns": ["region"],
                        "rows": [["EU"]], "chart": {"type": "bar"}}]}
    cid, old_id = _chat_with(ANA, body)
    fresh_id = db.add_message(cid, "assistant", {**body, "text": "fresh"})
    c = db._conn()
    c.execute("UPDATE messages SET created_at=? WHERE id=?", (time.time() - 40 * 86400, old_id))
    c.commit()
    c.close()

    assert chat.purge_message_rows() == 0                   # default: keep forever
    monkeypatch.setenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS", "30")
    assert chat.purge_message_rows() == 1
    assert chat.purge_message_rows() == 0                   # idempotent

    by_id = {m["id"]: m["content"] for m in db.list_messages(cid)}
    old, fresh = by_id[old_id], by_id[fresh_id]
    assert old["rows"] == [] and old["panels"][0]["rows"] == [] and old["rows_purged"] is True
    assert old["columns"] == ["region"] and old["sql"] and old["chart"] == {"type": "bar"}
    assert fresh["rows"] == [["EU"]] and "rows_purged" not in fresh
    # Still renders (and is not redacted) through the read path.
    shown = {m["id"]: m["content"] for m in chat._visible_messages(cid, ANA, "owner")}
    assert shown[old_id]["rows_purged"] is True and not shown[old_id].get("redacted")


# ── (e) /chat/rerun goes through the gateway ────────────────────────────

def test_rerun_endpoint_audits_once_and_returns_governed_columns(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setenv("STUDIO_DEMO_MODE", "1")             # seed users exist only here
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    with TestClient(main.app) as c:
        tok = c.post("/api/auth/login", json={"email": "analyst@studio.local",
                                              "password": "analyst123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        analyst = db.get_user_by_email("analyst@studio.local")
        governance._set(MASK_LTV, "test")                   # after startup's governance.load()
        before = [r for r in db.list_activity(analyst["id"]) if r["action"] == "rerun"]
        r = c.post("/api/chat/rerun", json={"source": "demo",
                                            "sql": "SELECT name, lifetime_value FROM customers"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["columns"] == ["name", "lifetime_value"]
        assert body["rows"] and all(row[1] == "***" for row in body["rows"])
        after = [r for r in db.list_activity(analyst["id"]) if r["action"] == "rerun"]
        assert len(after) == len(before) + 1                # exactly one row, from the gateway
        assert after[0]["ok"] == 1 and after[0]["source"] == "demo"
        assert after[0]["sql"].endswith(f"LIMIT {chat.agent.MAX_ROWS}")   # the cleaned SQL
        assert after[0]["row_count"] == len(body["rows"])

        # Rejections keep their HTTP mapping and are audited by the gateway too.
        r = c.post("/api/chat/rerun", json={"source": "demo", "sql": "DELETE FROM sales"})
        assert r.status_code == 403
        rej = [r for r in db.list_activity(analyst["id"]) if r["action"] == "rerun"]
        assert len(rej) == len(after) + 1 and rej[0]["ok"] == 0
        # A viewer cannot rerun customers: 403, not a leak.
        vt = c.post("/api/auth/login", json={"email": "viewer@studio.local",
                                             "password": "viewer123"}).json()["access_token"]
        r = c.post("/api/chat/rerun", headers={"Authorization": f"Bearer {vt}"},
                   json={"source": "demo", "sql": "SELECT name FROM customers"})
        assert r.status_code == 403
