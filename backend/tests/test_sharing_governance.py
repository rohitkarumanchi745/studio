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
from fastapi import HTTPException

from app import chat, db, governance, sessions

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
    sessions.init_tables()
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
    # Unattributable SQL (a file path) fails closed even for a whole-source
    # role: stored frames now require a currently valid source/namespace query,
    # not merely a broad role grant.
    opaque = {**honest, "sql": "SELECT * FROM 's3://b/x.parquet'"}
    assert not chat._msg_allowed("viewer", opaque)
    assert not chat._msg_allowed("analyst", opaque)


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


def _blend_content(identity="gov-a"):
    return {
        "text": "Ada's lifetime value is 4200 across both systems.",
        "source": "*", "table": "all sources", "author_role": "analyst",
        "sql": None, "blend_sql": "SELECT * FROM left JOIN right USING (id)",
        "columns": ["id", "lifetime_value"], "rows": [[1, 4200.0]],
        "chart": {"type": "table"},
        "panels": [{"source": "*", "sql": None,
                    "columns": ["id", "lifetime_value"],
                    "rows": [[1, 4200.0]], "chart": {"type": "table"}}],
        "blend_provenance": {
            "kind": "blend_v1", "governance_identity": identity,
            "inputs": [
                {"source": "demo", "table": "sales",
                 "sql": "SELECT id, revenue FROM sales LIMIT 2000"},
                {"source": "demo", "table": "customers",
                 "sql": "SELECT id, lifetime_value FROM customers LIMIT 2000"},
            ],
        },
    }


def test_blend_requires_stable_identity_and_every_verified_input(monkeypatch):
    identities = iter(["gov-a", "gov-a"])
    checked = []
    monkeypatch.setattr(governance, "identity", lambda: next(identities))
    monkeypatch.setattr(
        chat, "_stored_pipeline_step_allowed",
        lambda role, step, source=None: checked.append((source, step["sql"])) or True)

    assert chat._msg_allowed("analyst", _blend_content())
    assert [source for source, _ in checked] == ["demo", "demo"]

    # An input permission failure denies the whole derived artifact; a partial
    # provenance check would let the aggregate retain the denied input's rows.
    monkeypatch.setattr(governance, "identity", lambda: "gov-a")
    monkeypatch.setattr(
        chat, "_stored_pipeline_step_allowed",
        lambda role, step, source=None: "customers" not in step["sql"])
    assert not chat._msg_allowed("analyst", _blend_content())


@pytest.mark.parametrize("mutate", [
    lambda c: c.pop("blend_provenance"),
    lambda c: c["blend_provenance"].update(kind="future_or_forged"),
    lambda c: c["blend_provenance"].pop("governance_identity"),
    lambda c: c["blend_provenance"].update(inputs=[]),
    lambda c: c["blend_provenance"].update(inputs=c["blend_provenance"]["inputs"][:1]),
    lambda c: c["blend_provenance"]["inputs"][0].update(source="*"),
    lambda c: c["blend_provenance"]["inputs"][0].update(sql=""),
])
def test_blend_provenance_is_fail_closed(monkeypatch, mutate):
    content = _blend_content()
    mutate(content)
    monkeypatch.setattr(governance, "identity", lambda: "gov-a")
    monkeypatch.setattr(chat, "_stored_pipeline_step_allowed", lambda *a, **k: True)
    assert not chat._msg_allowed("analyst", content)


def test_blend_read_closes_policy_change_race_and_survives_row_retention(monkeypatch):
    identities = iter(["gov-a", "gov-b"])
    monkeypatch.setattr(governance, "identity", lambda: next(identities))
    monkeypatch.setattr(chat, "_stored_pipeline_step_allowed", lambda *a, **k: True)
    assert not chat._msg_allowed("analyst", _blend_content())

    # Retention removes values but deliberately keeps prose and columns.  The
    # provenance/version gate must still hide that prose under a new policy.
    purged = _blend_content()
    purged["rows"] = []
    purged["rows_purged"] = True
    purged["panels"][0]["rows"] = []
    monkeypatch.setattr(governance, "identity", lambda: "gov-b")
    assert not chat._msg_allowed("analyst", purged)


def test_visible_messages_rechecks_blend_identity_immediately_before_release(monkeypatch):
    identities = iter(["gov-a", "gov-a", "gov-b"])
    monkeypatch.setattr(governance, "identity", lambda: next(identities))
    monkeypatch.setattr(chat, "_stored_pipeline_step_allowed", lambda *a, **k: True)
    cid, _ = _chat_with(ANA, _blend_content())

    shown = chat._visible_messages(cid, ANA, "owner")[0]["content"]
    assert shown.get("redacted") and shown["text"] == chat._REDACTED


def test_multi_source_reason_checks_every_panel_before_wildcard_return(monkeypatch):
    content = {
        "text": "Combined answer", "source": "demo", "table": "all sources",
        "author_role": "analyst", "sql": "SELECT region FROM sales",
        "columns": ["region"], "rows": [["EU"]],
        "panels": [
            {"source": "demo", "sql": "SELECT region FROM sales",
             "columns": ["region"], "rows": [["EU"]]},
            {"source": "private", "sql": "SELECT name FROM customers",
             "columns": ["name"], "rows": [["Ada"]]},
        ],
    }
    checked = []
    monkeypatch.setattr(chat, "_unrestricted", lambda *a: True)

    def allowed(role, step, source=None):
        checked.append(source)
        return source != "private"

    monkeypatch.setattr(chat, "_stored_pipeline_step_allowed", allowed)
    assert not chat._msg_allowed("analyst", content)
    assert "private" in checked


def test_unattributed_stored_frames_fail_closed_even_with_author_role(monkeypatch):
    assert not chat._msg_allowed("analyst", {
        "text": "secret", "source": None, "table": "*",
        "author_role": "analyst", "columns": ["secret"], "rows": [["value"]],
    })

    content = {
        "text": "answer", "source": "demo", "table": "*",
        "author_role": "analyst", "sql": "SELECT region FROM sales",
        "columns": ["region"], "rows": [["EU"]],
        "panels": [{"source": "demo", "sql": None,
                    "columns": ["secret"], "rows": [["value"]]}],
    }
    monkeypatch.setattr(chat, "_unrestricted", lambda *a: True)
    monkeypatch.setattr(chat, "_stored_pipeline_step_allowed", lambda *a, **k: True)
    assert not chat._msg_allowed("analyst", content)


def test_canvas_cannot_persist_a_client_copy_without_server_provenance():
    cid = db.create_conversation(ANA["id"], "blend canvas")
    body = chat.CanvasEdit(
        instruction="make this a bar chart", columns=["secret"], rows=[[4200]],
        chart={"type": "table"}, conversation_id=cid, source="*", table="all sources",
        sql=None)
    with pytest.raises(HTTPException) as exc:
        chat.canvas_edit(body, user=ANA)
    assert getattr(exc.value, "status_code", None) == 400
    assert db.list_messages(cid) == []


def test_canvas_persistence_uses_the_governed_parent_not_client_rows(monkeypatch):
    parent = {
        "text": "revenue", "source": "demo", "table": "sales",
        "author_role": "analyst", "sql": "SELECT region, revenue FROM sales",
        "columns": ["region", "revenue"], "rows": [["EU", 125]],
        "chart": {"type": "table"},
    }
    cid, parent_id = _chat_with(ANA, parent)
    seen = {}
    monkeypatch.setattr(chat.agent, "llm_available", lambda *a, **k: False)

    def edit(_instruction, columns, rows, chart, **_kwargs):
        seen.update(columns=columns, rows=rows, chart=chart)
        return {"note": "edited", "columns": columns, "rows": rows,
                "chart": {"type": "bar"}}

    monkeypatch.setattr(chat.agent, "edit_canvas", edit)
    body = chat.CanvasEdit(
        instruction="make this a bar chart",
        # All four fields are hostile client assertions. A saved version must
        # ignore them and derive its frame from parent_id on the server.
        columns=["secret"], rows=[[999999]], chart={"type": "pie"},
        conversation_id=cid, parent_message_id=parent_id,
        source="private", table="customers", sql="SELECT secret FROM customers")

    result = chat.canvas_edit(body, user=ANA)

    assert seen == {"columns": ["region", "revenue"], "rows": [["EU", 125]],
                    "chart": {"type": "table"}}
    assert result["message_id"]
    saved = db.list_messages(cid)[-1]["content"]
    assert saved["rows"] == [["EU", 125]] and saved["source"] == "demo"
    assert saved["sql"] == "SELECT region, revenue FROM sales"
    assert saved["panels"][0]["sql"] == saved["sql"]
    assert saved["canvas_provenance"] == {
        "kind": "canvas_v1", "parent_message_id": parent_id,
        "parent_panel_index": 0,
    }


def test_canvas_reused_panel_inherits_parent_sql_for_read_time_governance(monkeypatch):
    parent = {
        "text": "revenue", "source": "demo", "table": "sales",
        "author_role": "analyst", "sql": "SELECT region, revenue FROM sales",
        "columns": ["region", "revenue"], "rows": [["EU", 125]],
        "chart": {"type": "table"},
    }
    cid, parent_id = _chat_with(ANA, parent)
    monkeypatch.setattr(chat.agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(chat, "_canvas_source", lambda *a, **k: (None, [], {}))
    monkeypatch.setattr(chat.agent, "compose_canvas", lambda *a, **k: {
        "note": "reused", "panels": [{
            "sql": None, "columns": parent["columns"], "rows": parent["rows"],
            "chart": {"type": "bar"},
        }],
    })

    result = chat.canvas_edit(chat.CanvasEdit(
        instruction="show the same data as bars", columns=[], rows=[],
        conversation_id=cid, parent_message_id=parent_id), user=ANA)

    saved = result["message"]
    assert saved["panels"][0]["sql"] == parent["sql"]
    assert chat._msg_allowed("analyst", saved)


def test_multi_panel_canvas_versions_keep_siblings_and_can_chain_edits(monkeypatch):
    parent = {
        "text": "two views", "source": "demo", "table": "sales",
        "author_role": "analyst", "sql": "SELECT region, revenue FROM sales",
        "columns": ["region", "revenue"], "rows": [["EU", 125]],
        "chart": {"type": "bar"},
        "panels": [
            {"source": "demo", "sql": "SELECT region, revenue FROM sales",
             "columns": ["region", "revenue"], "rows": [["EU", 125]],
             "chart": {"type": "bar"}},
            {"source": "demo", "sql": "SELECT month, revenue FROM sales",
             "columns": ["month", "revenue"], "rows": [["Jan", 300]],
             "chart": {"type": "line"}},
        ],
    }
    cid, parent_id = _chat_with(ANA, parent)
    seen = []
    monkeypatch.setattr(chat.agent, "llm_available", lambda *a, **k: False)

    def edit(_instruction, columns, rows, chart, **_kwargs):
        seen.append((columns, rows, chart))
        return {"note": "edited", "columns": columns, "rows": rows,
                "chart": {"type": "area"}}

    monkeypatch.setattr(chat.agent, "edit_canvas", edit)
    first = chat.canvas_edit(chat.CanvasEdit(
        instruction="change the monthly panel", columns=[], rows=[],
        conversation_id=cid, parent_message_id=parent_id, parent_panel_index=1), user=ANA)

    assert len(first["panels"]) == 2
    assert first["panels"][0]["rows"] == [["EU", 125]]
    assert first["panels"][1]["chart"] == {"type": "area"}
    # The new full-sheet message is a valid parent at the same index.
    second = chat.canvas_edit(chat.CanvasEdit(
        instruction="edit it again", columns=[], rows=[], conversation_id=cid,
        parent_message_id=first["message_id"], parent_panel_index=1), user=ANA)
    assert len(second["panels"]) == 2
    assert seen[1][0] == ["month", "revenue"] and seen[1][1] == [["Jan", 300]]


def test_conversation_session_read_resume_and_fork_rebuild_visible_transcript(monkeypatch):
    current = {"identity": "gov-a"}
    monkeypatch.setattr(governance, "identity", lambda: current["identity"])
    monkeypatch.setattr(chat, "_stored_pipeline_step_allowed", lambda *a, **k: True)

    secret = "Ada's lifetime value is 4200 across both systems."
    cid = db.create_conversation(ANA["id"], "governed blend")
    content = _blend_content("gov-a")
    content["text"] = secret
    db.add_message(cid, "assistant", content)
    sid = sessions.snapshot(
        ANA, conversation_id=cid, title="governed blend", model_spec="m",
        source="*", table_scope="all sources",
        messages=[{"role": "assistant", "text": secret}])

    assert secret in " ".join(m["text"] for m in sessions.get(sid, user=ANA)["messages"])
    forked_under_a = sessions.fork(sid, user=ANA)
    assert forked_under_a["conversation_id"] == cid
    assert forked_under_a["id"] != sid
    assert secret in " ".join(m["text"] for m in forked_under_a["messages"])

    # A snapshot and its fork are stable branches, not aliases for the live
    # conversation. Later turns must not silently appear in either session.
    later = "this question was added after the snapshot"
    db.add_message(cid, "user", {"text": later, "author_role": "analyst"})
    for payload in (sessions.get(sid, user=ANA),
                    sessions.get(forked_under_a["id"], user=ANA)):
        assert later not in " ".join(m["text"] for m in payload["messages"])
        assert len(payload["messages"]) == 1

    current["identity"] = "gov-b"
    read = sessions.get(sid, user=ANA)
    resumed = sessions.resume(sid, user=ANA)
    old_fork = sessions.get(forked_under_a["id"], user=ANA)
    forked = sessions.fork(sid, user=ANA)
    for payload in (read, resumed, old_fork, forked):
        text = " ".join(m["text"] for m in payload["messages"])
        assert secret not in text
        assert later not in text
        assert chat._REDACTED in text


def test_deleting_canonical_session_never_turns_a_fork_into_live_checkpoint(monkeypatch):
    monkeypatch.setattr(chat, "_stored_pipeline_step_allowed", lambda *a, **k: True)
    content = {"text": "first answer", "source": "demo", "table": "sales",
               "author_role": "analyst", "sql": "SELECT region FROM sales",
               "columns": ["region"], "rows": [["EU"]]}
    cid, _ = _chat_with(ANA, content)
    original = sessions.snapshot(
        ANA, conversation_id=cid, messages=[{"role": "assistant", "text": "first answer"}])
    branch = sessions.fork(original, user=ANA)
    sessions.remove(original, user=ANA)

    db.add_message(cid, "user", {"text": "later question", "author_role": "analyst"})
    replacement = sessions.snapshot(
        ANA, conversation_id=cid, messages=[
            {"role": "assistant", "text": "first answer"},
            {"role": "user", "text": "later question"},
        ])

    assert replacement != branch["id"]
    stable = sessions.get(branch["id"], user=ANA)
    assert [m["text"] for m in stable["messages"]] == ["first answer"]


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
