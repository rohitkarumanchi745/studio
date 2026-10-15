"""The Visuals screen: every chart, drawn the way its owner chooses.

Locked in here:
- the chart list covers the user's conversations, carries metadata (never
  rows), hides versions a newer canvas version replaced, and flags view-only;
- loading a chart and writing a visual for it read the governed SERVER copy,
  under the reader's current access — a client never supplies the data;
- choosing a D3 chart saves a new canvas version: that panel's type and engine
  change, its old visual goes, every sibling (and its visual/engine) stays, and
  the version is a valid parent for the next canvas edit, which keeps the engine;
- choosing a custom visual accepts only code this server wrote (signature) and
  still lints it; view-only access cannot choose; bad choices are refused.
"""
import json
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-visuals-screen-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi import HTTPException

from app import agent, chat, db, governance

ANA = {"id": "u-vs-ana", "email": "vs-ana@studio.test", "role": "analyst", "name": "Ana"}
BO = {"id": "u-vs-bo", "email": "vs-bo@studio.test", "role": "analyst", "name": "Bo"}

CODE = ("d3.select(el).append('svg').selectAll('text').data(data).join('text')"
        ".text(d => d.region);")
VISUAL = {"title": "Rings", "code": CODE, "explanation": "", "request": "rings"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    governance.init_tables()
    yield


@pytest.fixture(autouse=True)
def _builtin_rbac():
    governance._STATE.update(doc=None, yaml="", source=None)
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


def _answer():
    return {
        "text": "Revenue two ways", "source": "demo", "table": "sales",
        "author_role": "analyst", "sql": "SELECT region, revenue FROM sales",
        "columns": ["region", "revenue"], "rows": [["EU", 125], ["US", 90]],
        "chart": {"type": "bar", "x": "region", "y": ["revenue"], "title": "By region"},
        "panels": [
            {"source": "demo", "sql": "SELECT region, revenue FROM sales",
             "columns": ["region", "revenue"], "rows": [["EU", 125], ["US", 90]],
             "chart": {"type": "bar", "x": "region", "y": ["revenue"], "title": "By region"}},
            {"source": "demo", "sql": "SELECT month, revenue FROM sales",
             "columns": ["month", "revenue"], "rows": [["Jan", 300]],
             "chart": {"type": "table", "title": "Rings"}, "visual": VISUAL, "engine": "d3"},
        ],
    }


def _seed(owner=ANA):
    cid = db.create_conversation(owner["id"], "revenue chat")
    mid = db.add_message(cid, "assistant", _answer())
    return cid, mid


def _charts(user, cid):
    return [c for c in chat.visual_charts(user=user) if c["conversation_id"] == cid]


def test_the_list_carries_every_chart_as_metadata_only():
    cid, mid = _seed()
    charts = _charts(ANA, cid)
    assert [(c["message_id"], c["panel_index"], c["title"]) for c in charts] == \
        [(mid, 0, "By region"), (mid, 1, "Rings")]
    assert charts[1]["has_visual"] and charts[1]["engine"] == "d3"
    assert charts[0]["row_count"] == 2 and charts[0]["can_edit"] is True
    assert all("rows" not in c for c in charts)


def test_a_chart_loads_from_the_server_copy_under_current_access():
    cid, mid = _seed()
    got = chat.visual_chart(cid, mid, 0, user=ANA)
    assert got["rows"] == [["EU", 125], ["US", 90]] and got["chart"]["type"] == "bar"
    with pytest.raises(HTTPException) as e:
        chat.visual_chart(cid, mid, 0, user=BO)             # not shared with Bo
    assert e.value.status_code == 404


def test_choosing_a_d3_chart_saves_a_new_version_and_keeps_siblings():
    cid, mid = _seed()
    out = chat.visual_apply(chat.VisualChoice(
        conversation_id=cid, message_id=mid, panel_index=0, chart_type="circle_pack",
        engine="d3"), user=ANA)
    panels = out["message"]["panels"]
    assert panels[0]["chart"]["type"] == "circle_pack" and panels[0]["engine"] == "d3"
    assert panels[1]["visual"] == VISUAL and panels[1]["engine"] == "d3"   # sibling intact
    assert out["message"]["canvas_provenance"]["parent_message_id"] == mid
    # The list now shows the new version only.
    assert {c["message_id"] for c in _charts(ANA, cid)} == {out["message_id"]}
    # It is a valid parent for a canvas edit, which keeps the chosen engine.
    edit = chat.canvas_edit(chat.CanvasEdit(
        instruction="top 1", columns=[], rows=[], conversation_id=cid,
        parent_message_id=out["message_id"], parent_panel_index=0), user=ANA)
    assert edit["panels"][0]["engine"] == "d3"


def test_choosing_a_chart_for_a_visual_panel_drops_the_visual():
    cid, mid = _seed()
    out = chat.visual_apply(chat.VisualChoice(
        conversation_id=cid, message_id=mid, panel_index=1, chart_type="bar",
        engine="echarts"), user=ANA)
    panel = out["message"]["panels"][1]
    assert "visual" not in panel and panel["engine"] == "echarts"
    assert panel["chart"]["x"] == "month" and panel["chart"]["y"] == ["revenue"]


@pytest.mark.parametrize("choice,status", [
    ({"chart_type": "rocket"}, 400),
    ({"chart_type": "bar", "engine": "flash"}, 400),
    ({}, 400),
    ({"chart_type": "bar", "visual": VISUAL}, 400),
])
def test_bad_choices_are_refused(choice, status):
    cid, mid = _seed()
    with pytest.raises(HTTPException) as e:
        chat.visual_apply(chat.VisualChoice(conversation_id=cid, message_id=mid,
                                            panel_index=0, **choice), user=ANA)
    assert e.value.status_code == status


def test_view_only_access_cannot_choose():
    cid, mid = _seed()
    db.share_conversation(cid, BO["id"], "view")
    assert all(c["can_edit"] is False for c in _charts(BO, cid))
    with pytest.raises(HTTPException) as e:
        chat.visual_apply(chat.VisualChoice(conversation_id=cid, message_id=mid,
                                            chart_type="bar"), user=BO)
    assert e.value.status_code == 403


class _Reply:
    def __init__(self, text):
        self.content = text


class _LLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def invoke(self, messages):
        self.calls.append(messages)
        return _Reply(self.reply)


def test_a_visual_is_written_from_the_server_copy_and_saved_signed(monkeypatch):
    cid, mid = _seed()
    llm = _LLM(json.dumps({"title": "Rings", "code": CODE, "explanation": "One ring each."}))
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **k: llm)
    gen = chat.visual_generate(chat.VisualRequest(
        conversation_id=cid, message_id=mid, panel_index=0, request="rings"), user=ANA)
    sent = json.loads(llm.calls[0][1][1])
    assert sent["sample_rows"] == [["EU", 125], ["US", 90]]       # the stored rows
    assert gen["code"] == CODE and gen["signature"]

    out = chat.visual_apply(chat.VisualChoice(
        conversation_id=cid, message_id=mid, panel_index=0, visual=gen), user=ANA)
    assert out["message"]["panels"][0]["visual"]["code"] == CODE
    assert "signature" not in out["message"]["panels"][0]["visual"]


def test_only_code_this_server_wrote_can_be_saved():
    cid, mid = _seed()
    forged = {**VISUAL, "signature": "0" * 64}
    with pytest.raises(HTTPException) as e:
        chat.visual_apply(chat.VisualChoice(conversation_id=cid, message_id=mid,
                                            visual=forged), user=ANA)
    assert e.value.status_code == 400 and "written here" in e.value.detail
    # Signed but unsafe (a hand-signed test value): the lint still refuses it.
    bad = "fetch('https://x.example'); " + CODE
    with pytest.raises(HTTPException) as e:
        chat.visual_apply(chat.VisualChoice(conversation_id=cid, message_id=mid, visual={
            **VISUAL, "code": bad, "signature": chat._visual_signature(bad)}), user=ANA)
    assert e.value.status_code == 400 and "fetch" in e.value.detail


def test_writing_a_visual_needs_a_model(monkeypatch):
    cid, mid = _seed()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: False)
    with pytest.raises(HTTPException) as e:
        chat.visual_generate(chat.VisualRequest(conversation_id=cid, message_id=mid,
                                                request="rings"), user=ANA)
    assert e.value.status_code == 400 and "model" in e.value.detail
