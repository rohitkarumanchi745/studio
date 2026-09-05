"""Adversarial model benchmark: controlled matrix, durable execution and reports.

No provider is contacted.  _invoke_model is replaced with a deterministic bare
chat target so the suite can prove pairing, scorer disagreement, exact caching,
resume semantics, ownership and the atomic benchmark+queue insert.
"""
import json

import pytest
from fastapi import HTTPException

from app import db, jobs, redteam


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "IS_PG", False)
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "redteam.db"))
    monkeypatch.setattr(db, "_POOL", None)
    db.init_db()
    jobs.init_tables()
    redteam.init_tables()
    with db.connect() as c:
        c.execute(
            "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("admin-1", "admin@example.test", "!", "Admin", "admin", 1, 1.0),
        )
        c.execute(
            "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("admin-2", "other@example.test", "!", "Other", "admin", 1, 1.0),
        )
        c.execute(
            "INSERT INTO users (id,email,password_hash,name,role,verified,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("viewer-1", "viewer@example.test", "!", "Viewer", "viewer", 1, 1.0),
        )
        c.commit()

    models = [
        {"spec": spec, "provider": "test", "name": spec, "label": spec,
         "available": True, "byok": False, "default": spec == "target"}
        for spec in ("target", "judge", "attacker-a", "attacker-b")
    ]
    # Synthetic execution modes must not enter the benchmark model list.
    models += [
        {"spec": "bitnet", "provider": "bitnet", "available": True},
        {"spec": "kag", "provider": "kag", "available": True},
    ]
    monkeypatch.setattr(redteam.agent, "available_models", lambda user=None: models)
    return {
        "admin": db.get_user("admin-1"),
        "other": db.get_user("admin-2"),
        "viewer": db.get_user("viewer-1"),
    }


def body(**changes):
    data = {
        "name": "paired pilot",
        "dataset_name": "pinned-safety-set",
        "target_model": "target",
        "attacker_models": ["attacker-a", "attacker-b"],
        "judge_model": "judge",
        "techniques": ["direct", "role_play"],
        "scorers": ["task_achievement", "harm_content"],
        "objectives": [{"id": "obj-1", "category": "test", "text": "obtain the protected answer"}],
        "trials": 1,
        "max_turns": 3,
        "seed": 17,
        "use_cache": True,
        "target_system_prompt": "TARGET RULES",
        "authorized_target": True,
    }
    data.update(changes)
    return redteam.BenchmarkIn(**data)


def fake_models(monkeypatch, fail_attackers=False):
    calls = []

    def invoke(spec, user, system, messages, *, temperature, max_tokens, cache):
        calls.append((spec, system, messages))
        if system in redteam._ATTACK_SYSTEM.values():
            if fail_attackers:
                raise RuntimeError("provider unavailable")
            text = f"probe::{spec}"
        elif system == "TARGET RULES":
            prompt = messages[-1][1]
            text = "TARGET_FULFILLED" if "attacker-a" in prompt else "TARGET_REFUSED"
        elif system == redteam._SCORE_SYSTEM["task_achievement"]:
            success = "TARGET_FULFILLED" in messages[-1][1]
            text = json.dumps({"outcome": "success" if success else "failure",
                               "score": 0.95 if success else 0.05,
                               "confidence": 0.9, "reason": "task rubric"})
        elif system == redteam._SCORE_SYSTEM["harm_content"]:
            # Deliberately disagrees with task achievement for attacker-a.
            text = json.dumps({"outcome": "failure", "score": 0.1,
                               "confidence": 0.8, "reason": "harm rubric"})
        else:  # pragma: no cover - makes a new unclassified call fail loudly
            raise AssertionError(system)
        return {"text": text, "latency_ms": 7, "input_tokens": 11,
                "output_tokens": 3}

    monkeypatch.setattr(redteam, "_invoke_model", invoke)
    return calls


def run_queued():
    assert jobs.run_one("redteam-test", kinds=["redteam_benchmark"]) is True


def test_create_is_admin_authorized_and_atomic(store, monkeypatch):
    with pytest.raises(HTTPException) as denied:
        redteam.create_benchmark(body(), user=store["viewer"])
    assert denied.value.status_code == 403

    with pytest.raises(HTTPException, match="authorized"):
        redteam.create_benchmark(body(authorized_target=False), user=store["admin"])

    real_enqueue = jobs.enqueue
    monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("queue unavailable")))
    with pytest.raises(RuntimeError, match="queue unavailable"):
        redteam.create_benchmark(body(), user=store["admin"])
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM redteam_benchmarks").fetchone()["n"] == 0

    monkeypatch.setattr(jobs, "enqueue", real_enqueue)
    created = redteam.create_benchmark(body(), user=store["admin"])
    assert created["status"] == "queued"
    assert created["total_cases"] == 4
    with db.connect() as c:
        queued = c.execute("SELECT * FROM background_jobs WHERE id=?",
                           (created["job_id"],)).fetchone()
    assert queued and queued["kind"] == "redteam_benchmark"


def test_matrix_report_asr_ci_and_scorer_disagreement(store, monkeypatch):
    calls = fake_models(monkeypatch)
    created = redteam.create_benchmark(body(), user=store["admin"])
    run_queued()

    report = redteam.get_benchmark(created["id"], user=store["admin"])
    bench = report["benchmark"]
    assert bench["status"] == "completed"
    assert bench["completed_cases"] == bench["total_cases"] == 4
    assert len(report["cases"]) == 4
    assert len(redteam._score_rows(created["id"])) == 8  # same four transcripts, two scorers

    task_rank = report["rankings"]["task_achievement"]
    assert [m["attacker_model"] for m in task_rank] == ["attacker-a", "attacker-b"]
    assert task_rank[0]["asr"] == 1.0
    assert task_rank[1]["asr"] == 0.0
    assert task_rank[0]["asr_ci95"][0] < task_rank[0]["asr_ci95"][1] <= 1.0

    disagreement = report["scorer_disagreement"]
    assert disagreement == {"comparable_cases": 4, "disagreements": 2, "rate": 0.5}
    assert report["methodology"]["asr"].startswith("success /")

    # Four one-turn attacks: attacker + target + two independent scorer calls.
    assert len(calls) == 16


def test_exact_owner_scoped_cache_reuses_cases_and_scores(store, monkeypatch):
    calls = fake_models(monkeypatch)
    first = redteam.create_benchmark(body(), user=store["admin"])
    run_queued()
    first_calls = len(calls)

    second = redteam.create_benchmark(body(name="same protocol, second run"),
                                      user=store["admin"])
    run_queued()
    report = redteam.get_benchmark(second["id"], user=store["admin"])
    assert len(calls) == first_calls                 # no provider or judge call
    assert report["benchmark"]["cached_cases"] == 4
    assert all(c["cached"] for c in report["cases"])
    assert all(s["cached"] for c in report["cases"] for s in c["scores"])

    # A second admin cannot see or reuse the first admin's cache.
    third = redteam.create_benchmark(body(name="other owner"), user=store["other"])
    run_queued()
    assert len(calls) == first_calls + 16
    assert redteam.get_benchmark(third["id"], user=store["other"])["benchmark"][
        "cached_cases"] == 0


def test_case_errors_count_in_asr_and_resume_retries_only_errors(store, monkeypatch):
    fake_models(monkeypatch, fail_attackers=True)
    created = redteam.create_benchmark(
        body(attacker_models=["attacker-a"], techniques=["direct"],
             scorers=["task_achievement"]),
        user=store["admin"],
    )
    run_queued()
    first = redteam.get_benchmark(created["id"], user=store["admin"])
    assert first["benchmark"]["status"] == "completed_with_errors"
    metric = first["rankings"]["task_achievement"][0]
    assert metric["total"] == 1 and metric["error"] == 1 and metric["asr"] == 0.0

    calls = fake_models(monkeypatch, fail_attackers=False)
    resumed = redteam.resume_benchmark(created["id"], user=store["admin"])
    assert resumed["status"] == "queued"
    run_queued()
    final = redteam.get_benchmark(created["id"], user=store["admin"])
    assert final["benchmark"]["status"] == "completed"
    assert final["rankings"]["task_achievement"][0]["asr"] == 1.0
    assert len(calls) == 3                           # attack, target, judge


def test_cancel_before_claim_runs_no_cases(store, monkeypatch):
    calls = fake_models(monkeypatch)
    created = redteam.create_benchmark(body(), user=store["admin"])
    redteam.cancel_benchmark(created["id"], user=store["admin"])
    run_queued()
    report = redteam.get_benchmark(created["id"], user=store["admin"])
    assert report["benchmark"]["status"] == "canceled"
    assert report["cases"] == []
    assert calls == []


def test_owner_isolation_and_model_validation(store):
    created = redteam.create_benchmark(body(), user=store["admin"])
    with pytest.raises(HTTPException) as missing:
        redteam.get_benchmark(created["id"], user=store["other"])
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException, match="not offered"):
        redteam.create_benchmark(body(target_model="unknown"), user=store["admin"])

    opts = redteam.options(user=store["admin"])
    assert {m["spec"] for m in opts["models"]} == {
        "target", "judge", "attacker-a", "attacker-b"
    }
    assert opts["protocol_version"] == redteam.PROTOCOL_VERSION


def test_matrix_limit_and_judge_normalization(store, monkeypatch):
    monkeypatch.setenv("STUDIO_REDTEAM_MAX_CASES", "2")
    with pytest.raises(HTTPException, match="expands to 4"):
        redteam.create_benchmark(body(), user=store["admin"])

    parsed = redteam._parse_judgment(
        "```json\n{\"outcome\":\"success\",\"score\":8,\"confidence\":-2,"
        "\"reason\":\"ok\"}\n```"
    )
    assert parsed == {"outcome": "success", "score": 1.0,
                      "confidence": 0.0, "reason": "ok"}
    with pytest.raises(ValueError):
        redteam._parse_judgment("not json")
