"""Learned rules: drafted from failures on their own, live only once an admin approves."""
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import agent, auth, db, jobs, learned_rules

USER = {"id": "u1", "email": "u1@studio.test", "role": "viewer"}
ADMIN = {"id": "a1", "email": "a1@studio.test", "role": "admin"}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "rules.db"))
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(learned_rules, "_LEGACY_PATH", str(tmp_path / "system_learned.txt"))
    monkeypatch.setattr(learned_rules, "MIN_FAILURES", 3)
    db.init_db()
    learned_rules.init_tables()


@pytest.fixture()
def llm(monkeypatch):
    """A model that answers `llm.reply`; every prompt it saw is in `llm.prompts`."""
    state = types.SimpleNamespace(prompts=[], reply="- Aggregate before joining.\n- Always LIMIT raw rows.")

    class Model:
        def invoke(self, prompt):
            state.prompts.append(prompt)
            return types.SimpleNamespace(content=state.reply)

    monkeypatch.setattr(agent, "llm_available", lambda *a, **kw: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **kw: Model())
    return state


def fail(n=1, prompt="revenue by region"):
    for i in range(n):
        db.add_trace(USER, prompt=f"{prompt} {i}", mode="agent", source="demo",
                     sql="SELECT * FROM sales", ok=False, error="no such column: amount",
                     reward=0.0, reward_source="heuristic")


def status():
    return {r["id"]: r["status"] for r in learned_rules.overview()["history"]}


# ── Drafting ─────────────────────────────────────────────────────────────

def test_the_ticker_waits_for_enough_new_failures(llm):
    fail(2)
    learned_rules.tick_once()
    assert llm.prompts == [] and learned_rules.overview()["history"] == []
    fail(1)
    learned_rules.tick_once()
    (proposal,) = learned_rules.overview()["history"]
    assert proposal["status"] == "proposed" and proposal["evidence_count"] == 3


def test_only_failures_after_the_last_draft_count_as_new(llm):
    fail(3)
    first, _ = learned_rules.draft()
    assert first
    fail(2)
    again, reason = learned_rules.draft()
    assert again is None and reason.startswith("2 new")
    fail(1)
    second, _ = learned_rules.draft()
    assert status() == {second["id"]: "proposed", first["id"]: "superseded"}


def test_an_explicit_request_drafts_even_below_the_threshold(llm):
    fail(1)
    proposal, _ = learned_rules.draft(force=True)
    assert proposal["rules"] == "- Aggregate before joining.\n- Always LIMIT raw rows."


def test_no_key_or_no_failures_means_no_draft(llm, monkeypatch):
    assert learned_rules.draft(force=True) == (None, "0 new low-reward runs since the last draft; 3 needed")
    monkeypatch.setattr(agent, "llm_available", lambda *a, **kw: False)
    fail(5)
    assert learned_rules.draft()[1] == "no LLM key is configured on the server"


def test_model_output_is_reduced_to_bounded_bullet_rules(llm):
    llm.reply = "Here are the rules:\n" + "\n".join(f"- rule {i} " + "x" * 400 for i in range(12))
    fail(3)
    proposal, _ = learned_rules.draft()
    lines = proposal["rules"].splitlines()
    assert len(lines) == 8 and all(l.startswith("- rule ") and len(l) <= 302 for l in lines)
    llm.reply = "I could not find a pattern."
    fail(3)
    assert learned_rules.draft() == (None, "the model returned no usable rules")


def test_the_draft_prompt_carries_current_rules_and_asks_for_general_ones(llm):
    fail(3)
    first, _ = learned_rules.draft()
    learned_rules.approve(first["id"], ADMIN["id"])
    fail(3)
    learned_rules.draft()
    prompt = llm.prompts[-1]
    assert "Current rules:\n- Aggregate before joining." in prompt
    assert "never quote user questions" in prompt
    assert "no such column: amount" in prompt


def test_a_failing_model_never_breaks_the_ticker(monkeypatch):
    monkeypatch.setattr(agent, "llm_available", lambda *a, **kw: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    fail(3)
    learned_rules.tick_once()   # logs, does not raise
    assert learned_rules.overview()["history"] == []


def test_the_worker_schedules_drafting_under_its_own_lease():
    sched = {s["name"]: s for s in jobs._default_schedulers()}["learned_rules"]
    assert sched["fn"] is learned_rules.tick_once
    assert sched["enabled"] is learned_rules.ticker_enabled


# ── Approval gates the prompt ────────────────────────────────────────────

def test_a_proposal_reaches_no_prompt_until_approved(llm):
    fail(3)
    proposal, _ = learned_rules.draft()
    assert agent._learned_rules() == "  (none yet)"
    learned_rules.approve(proposal["id"], ADMIN["id"])
    assert agent._learned_rules() == proposal["rules"]
    active = learned_rules.overview()["active"]
    assert active["decided_by"] == ADMIN["id"]


def test_an_admin_can_edit_rules_before_approving(llm):
    fail(3)
    proposal, _ = learned_rules.draft()
    learned_rules.approve(proposal["id"], ADMIN["id"], rules="- Prefer date_trunc for months.\nchatter")
    assert agent._learned_rules() == "- Prefer date_trunc for months."
    fail(3)
    other, _ = learned_rules.draft()
    with pytest.raises(ValueError):
        learned_rules.approve(other["id"], ADMIN["id"], rules="no bullets here")


def test_approving_a_new_set_supersedes_the_old_one(llm):
    fail(3)
    first, _ = learned_rules.draft()
    learned_rules.approve(first["id"], ADMIN["id"])
    llm.reply = "- Check column names in the skill file."
    fail(3)
    second, _ = learned_rules.draft()
    learned_rules.approve(second["id"], ADMIN["id"])
    assert agent._learned_rules() == "- Check column names in the skill file."
    assert status() == {first["id"]: "superseded"}
    with pytest.raises(ValueError):
        learned_rules.approve(first["id"], ADMIN["id"])


def test_rejecting_and_retiring(llm):
    fail(3)
    proposal, _ = learned_rules.draft()
    assert learned_rules.reject(proposal["id"], ADMIN["id"]) is True
    assert learned_rules.reject(proposal["id"], ADMIN["id"]) is False
    fail(3)
    kept, _ = learned_rules.draft()
    learned_rules.approve(kept["id"], ADMIN["id"])
    assert learned_rules.retire(ADMIN["id"]) is True
    assert agent._learned_rules() == "  (none yet)"


def test_a_legacy_rules_file_is_imported_once_as_the_active_set(tmp_path, monkeypatch):
    legacy = tmp_path / "old" / "system_learned.txt"
    legacy.parent.mkdir()
    legacy.write_text("- Use strftime for months on sqlite.\n")
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "fresh.db"))
    monkeypatch.setattr(learned_rules, "_LEGACY_PATH", str(legacy))
    db.init_db()
    learned_rules.init_tables()
    learned_rules.init_tables()
    assert agent._learned_rules() == "- Use strftime for months on sqlite."
    assert learned_rules.overview()["history"] == []


# ── API ──────────────────────────────────────────────────────────────────

def client_as(user):
    app = FastAPI()
    app.include_router(learned_rules.router, prefix="/api")
    app.dependency_overrides[auth.current_user] = lambda: user
    return TestClient(app)


def test_only_admins_can_see_or_decide(llm):
    viewer = client_as(USER)
    for method, path in (("get", ""), ("post", "/draft"), ("post", "/x/approve"),
                         ("post", "/x/reject"), ("post", "/retire")):
        assert getattr(viewer, method)(f"/api/learned-rules{path}").status_code == 403


def test_admin_drafts_and_approves_through_the_api(llm):
    admin = client_as(ADMIN)
    fail(1)
    proposal = admin.post("/api/learned-rules/draft").json()["proposal"]
    assert proposal["status"] == "proposed"
    assert admin.post("/api/learned-rules/nope/approve").status_code == 404
    body = admin.post(f"/api/learned-rules/{proposal['id']}/approve",
                      json={"rules": "- Edited rule."}).json()
    assert body["active"]["rules"] == "- Edited rule."
    assert admin.post(f"/api/learned-rules/{proposal['id']}/approve").status_code == 400
    assert admin.post("/api/learned-rules/retire").json()["active"] is None
