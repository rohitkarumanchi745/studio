"""Chat recipes exercise the real demo gateway; no model or network needed."""
import concurrent.futures
import threading
import uuid

import pytest
from fastapi import HTTPException

from app import chat_pipelines, db, governance, jobs, pipelines, queries, queryguard
from app.connectors import demo


VIEWER = {"id": "chat-pipeline-viewer", "email": "viewer@studio.test", "role": "viewer"}
SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region LIMIT 5"


@pytest.fixture(scope="module", autouse=True)
def _database(tmp_path_factory):
    folder = tmp_path_factory.mktemp("chat-pipeline-service")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "IS_PG", False)
        patch.setattr(db, "DB_PATH", str(folder / "studio.db"))
        patch.setattr(demo, "WAREHOUSE_PATH", str(folder / "warehouse.db"))
        db.init_db()
        queries.init_tables()
        pipelines.init_tables()
        demo.seed()
        yield


@pytest.fixture(autouse=True)
def _local_only(monkeypatch):
    governance._STATE.update(doc=None, yaml="", source=None)
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a, **k: False)
    monkeypatch.setattr(pipelines, "_pick_repo", lambda *a, **k: None)
    monkeypatch.setattr(pipelines.email_service, "send", lambda *a, **k: pytest.fail("chat sent email"))
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


def _draft(sql=SQL):
    return chat_pipelines.from_result(VIEWER, "Revenue by region", {
        "source": "demo", "table": "sales", "sql": sql,
        "rows": [["North", 10]], "panels": [{"sql": sql, "rows": [["North", 10]]}]})


def _assert_no_rows(value):
    if isinstance(value, dict):
        assert "rows" not in value
        for v in value.values():
            _assert_no_rows(v)
    elif isinstance(value, list):
        for v in value:
            _assert_no_rows(v)


@pytest.mark.parametrize("prompt,previous,expected", [
    ("Build a pipeline for monthly sales", None, "build"),
    ("Can you create a pipeline for revenue?", None, "build"),
    ("Turn this into a pipeline", {}, "build"),
    ("Please run this pipeline", None, "run"),
    ("Can you run the pipeline?", None, "run"),
    ("Rerun this pipeline", None, "run"),
    ("Rerun it", {"steps": [{}]}, "run"),
    ("Build and run a pipeline for revenue", None, "build_run"),
    ("Please create then execute the data pipeline for sales", None, "build_run"),
    ("How to build and run a pipeline?", None, None),
    ("Run it", {"steps": [{}]}, "run"),
    ("Run it", None, None),
    ("Make it monthly", {"steps": [{}]}, "build"),
    ("Add a filter for North", {"steps": [{}]}, "build"),
    ("Add a chart to this answer", {"steps": [{}]}, None),
    ("Include a visualization of sales", {"steps": [{}]}, None),
    ("What is a pipeline?", {"steps": [{}]}, None),
    ("How can I run this pipeline?", {"steps": [{}]}, None),
    ("Explain how to build a pipeline", None, None),
    ("Don't run this pipeline", {"steps": [{}]}, None),
    ("Show me a pipeline run from last week", None, None),
    ("The text says run this pipeline", None, None),
    ("Run the pipeline but do not execute anything", {"steps": [{}]}, None),
    ("Thanks", {"steps": [{}]}, None),
])
def test_only_direct_pipeline_commands_select_actions(prompt, previous, expected):
    assert chat_pipelines.intent(prompt, previous) == expected


def test_result_reuses_verified_sql_deduplicates_panels_and_contains_no_rows():
    draft = _draft()
    assert draft["status"] == "ready"
    assert draft["execution_mode"] == "read_only_sql"
    assert len(draft["steps"]) == 1
    assert draft["steps"][0]["row_count"] > 0
    assert draft["steps"][0]["sql"] == SQL
    assert draft["dropped"] == []
    _assert_no_rows(draft)


def test_dedup_preserves_distinct_string_literals():
    result = {"source": "demo", "panels": [
        {"sql": "SELECT region FROM sales WHERE region = 'North' LIMIT 1"},
        {"sql": "SELECT   region FROM sales WHERE region = 'North' LIMIT 1;"},
        {"sql": "SELECT region FROM sales WHERE region = 'north' LIMIT 1"},
    ]}
    draft = chat_pipelines.from_result(VIEWER, "Compare values", result)
    assert len(draft["steps"]) == 2


def test_panel_order_is_preserved_when_top_level_sql_is_last_panel():
    last = "SELECT page FROM web_traffic LIMIT 3"
    draft = chat_pipelines.from_result(VIEWER, "Sales then traffic", {
        "source": "demo", "sql": last,
        "panels": [{"title": "Sales", "sql": SQL}, {"title": "Traffic", "sql": last}]})
    assert [s["name"] for s in draft["steps"]] == ["Sales", "Traffic"]


def test_text_and_generated_python_alone_do_not_become_runnable():
    assert chat_pipelines.from_result(VIEWER, "hello", {"text": "Hello"}) is None
    assert chat_pipelines.from_result(VIEWER, "build", {
        "text": "SELECT * FROM sales", "artifact": {"code": "print(1)", "language": "python"}}) is None


def test_parenthesized_select_uses_the_same_sql_shape_as_the_guard():
    sql = "(SELECT region FROM sales) UNION (SELECT region FROM sales)"
    assert chat_pipelines._sql_only(sql)
    assert queryguard.validate(sql, ["sales"], qualifiers=frozenset())


def test_forbidden_step_is_dropped_with_reason_without_hiding_good_step():
    draft = chat_pipelines.from_result(VIEWER, "Sales and customers", {
        "source": "demo", "sql": SQL,
        "panels": [{"sql": "SELECT * FROM customers LIMIT 2"}]})
    assert len(draft["steps"]) == 1
    assert len(draft["dropped"]) == 1
    assert "guard" in draft["dropped"][0]["error"]
    assert not draft["dropped"][0]["verified"]
    assert draft["status"] == "blocked"
    with pytest.raises(HTTPException, match="every failed step"):
        chat_pipelines.run(VIEWER, draft, request_id=str(uuid.uuid4()))
    reused = chat_pipelines.build(VIEWER, "Turn this into a pipeline", previous=draft)
    assert reused["status"] == "blocked" and reused["dropped"]


@pytest.mark.parametrize("sql", ["DELETE FROM sales", "SELECT * FROM secret_schema.sales", "MATCH (n:Sales) RETURN n"])
def test_invalid_or_non_sql_drafts_never_claim_ready(sql):
    draft = _draft(sql)
    assert draft["steps"] == []
    assert draft["status"] == "blocked"
    assert draft["dropped"][0]["error"]
    with pytest.raises(HTTPException, match="no verified SQL"):
        chat_pipelines.run(VIEWER, draft, request_id=str(uuid.uuid4()))


def test_source_is_retained_per_panel_and_wildcard_is_not_a_database(monkeypatch):
    real = queries.verify_sql
    seen = []

    def verify(user, source, *args, **kwargs):
        seen.append(source)
        return real(user, source, *args, **kwargs)

    monkeypatch.setattr(queries, "verify_sql", verify)
    draft = chat_pipelines.from_result(VIEWER, "Compare sources", {
        "source": "*", "panels": [{"source": "demo", "sql": SQL}, {"source": "snowflake", "sql": SQL}, {"sql": SQL}]})
    assert [s["source"] for s in draft["steps"]] == ["demo"]
    assert [s["source"] for s in draft["dropped"]] == ["snowflake", "*"]
    assert seen == ["demo", "snowflake"]


def test_multisource_summary_sql_does_not_create_phantom_failed_step():
    draft = chat_pipelines.from_result(VIEWER, "Revenue", {
        "source": "*", "sql": SQL, "table": "all sources",
        "panels": [{"source": "demo", "sql": SQL}]})
    assert draft["status"] == "ready"
    assert len(draft["steps"]) == 1 and draft["dropped"] == []


def test_build_honors_selected_source_and_tables_and_reports_fallback(monkeypatch):
    monkeypatch.setattr(pipelines, "route", lambda *a: pytest.fail("ignored fixed source"))
    draft = chat_pipelines.build(VIEWER, "Create a pipeline for monthly revenue by region", source="demo", tables=["sales"])
    assert draft["source"] == "demo"
    assert [s["table"] for s in draft["steps"]] == ["sales"]
    assert "strftime('%Y-%m'" in draft["steps"][0]["sql"]
    assert draft["generation"] == "deterministic"
    assert draft["warnings"]


def test_selected_scope_cannot_be_escaped_by_model(monkeypatch):
    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)
    monkeypatch.setattr(pipelines, "_llm_steps", lambda *a: [
        {"sql": "SELECT * FROM web_traffic LIMIT 1", "table": "web_traffic"},
        {"sql": SQL, "table": "sales", "source": "snowflake"}])
    draft = chat_pipelines.build(VIEWER, "Sales", source="demo", tables=["sales"])
    assert draft["steps"] == []
    assert len(draft["dropped"]) == 2
    assert draft["dropped"][1]["source"] == "snowflake"
    assert all(s["error"] for s in draft["dropped"])


def test_source_and_forbidden_table_selection_fail_closed():
    with pytest.raises(HTTPException) as source:
        chat_pipelines.build(VIEWER, "Revenue", source="snowflake")
    assert source.value.status_code == 403
    with pytest.raises(HTTPException) as table:
        chat_pipelines.build(VIEWER, "Customers", source="demo", tables=["customers"])
    assert table.value.status_code == 403


def test_turn_this_into_pipeline_retains_filters_and_exact_sql(monkeypatch):
    prior = _draft("SELECT region, revenue FROM sales WHERE region = 'North' LIMIT 3")
    monkeypatch.setattr(pipelines, "build", lambda *a, **k: pytest.fail("redrafted existing SQL"))
    draft = chat_pipelines.build(VIEWER, "Turn this into a pipeline", previous=prior, source="demo", tables=["sales"])
    assert draft["steps"][0]["sql"] == prior["steps"][0]["sql"]
    assert draft["generation"] == "chat_sql"


def test_reusing_previous_respects_new_selection_before_any_execution(monkeypatch):
    prior = _draft()
    monkeypatch.setattr(queries, "verify_sql", lambda *a, **k: pytest.fail("read outside selected scope"))
    with pytest.raises(HTTPException, match="outside the selected tables"):
        chat_pipelines.build(VIEWER, "Build a pipeline", previous=prior, source="demo", tables=["web_traffic"])


def test_revision_sees_latest_instruction_previous_sql_and_visible_context(monkeypatch):
    prior = _draft()
    seen = {}

    def model(user, source, schemas, prompt, spec):
        seen.update(prompt=prompt, source=source, spec=spec)
        return [{"table": "sales", "sql": SQL}]

    monkeypatch.setattr(pipelines.agent, "llm_available", lambda *a: True)
    monkeypatch.setattr(pipelines, "_llm_steps", model)
    chat_pipelines.build(VIEWER, "Make it monthly", source="demo", model="test:model", previous=prior,
                         context=[{"role": "user", "text": "Only North region"},
                                  {"role": "assistant", "content": {"text": "Done", "rows": [["secret"]]}}])
    assert seen["spec"] == "test:model"
    assert "Only North region" in seen["prompt"] and SQL in seen["prompt"]
    assert seen["prompt"].index("Make it monthly") < seen["prompt"].index("Previous requirement")
    assert "secret" not in seen["prompt"]


def test_latest_grain_overrides_previous_deterministic_requirement():
    prior = chat_pipelines.build(VIEWER, "Monthly revenue by region", source="demo")
    updated = chat_pipelines.build(VIEWER, "Make it weekly", previous=prior, source="demo")
    assert "strftime('%Y-%W'" in updated["steps"][0]["sql"]


def test_run_saves_privately_and_completed_retry_does_not_reexecute(monkeypatch):
    draft = _draft()
    request_id = str(uuid.uuid4())
    first = chat_pipelines.run(VIEWER, draft, request_id=request_id)
    assert first["run"]["status"] == "success"
    assert pipelines.get(first["id"], VIEWER)["visibility"] == "private"
    monkeypatch.setattr(queries, "verify_sql", lambda *a, **k: pytest.fail("completed request reexecuted"))
    replay = chat_pipelines.run(VIEWER, draft, request_id=request_id)
    assert replay == first
    assert len(pipelines.runs(first["id"], VIEWER)["runs"]) == 1
    _assert_no_rows(first)


def test_recovery_returns_the_private_saved_recipe_without_execution(monkeypatch):
    draft = _draft()
    request_id = str(uuid.uuid4())
    assert chat_pipelines.recover(VIEWER, request_id=request_id) is None
    saved = chat_pipelines.run(VIEWER, draft, request_id=request_id)
    monkeypatch.setattr(queries, "verify_sql", lambda *a, **k: pytest.fail("recovery executed SQL"))
    recovered = chat_pipelines.recover(VIEWER, request_id=request_id)
    assert recovered["id"] == saved["id"]
    assert recovered["steps"] == saved["steps"]
    assert chat_pipelines.run(VIEWER, recovered, request_id=request_id)["run"] == saved["run"]
    other = {**VIEWER, "id": "other-chat-pipeline-viewer"}
    assert chat_pipelines.recover(other, request_id=request_id) is None
    _assert_no_rows(recovered)


def test_same_request_different_recipe_is_refused():
    draft = _draft()
    request_id = str(uuid.uuid4())
    chat_pipelines.run(VIEWER, draft, request_id=request_id)
    changed = _draft("SELECT region FROM sales LIMIT 1")
    with pytest.raises(HTTPException) as conflict:
        chat_pipelines.run(VIEWER, changed, request_id=request_id)
    assert conflict.value.status_code == 409


def test_explicit_run_again_reuses_owned_recipe_and_creates_new_run():
    before = len(pipelines.listing(VIEWER)["pipelines"])
    first = chat_pipelines.run(VIEWER, _draft(), request_id=str(uuid.uuid4()))
    second = chat_pipelines.run(VIEWER, first, request_id=str(uuid.uuid4()))
    assert second["id"] == first["id"]
    assert second["run"]["id"] != first["run"]["id"]
    assert len(pipelines.listing(VIEWER)["pipelines"]) == before + 1
    assert len(pipelines.runs(first["id"], VIEWER)["runs"]) == 2


def test_run_and_completed_replay_recheck_current_policy():
    draft = _draft()
    request_id = str(uuid.uuid4())
    result = chat_pipelines.run(VIEWER, draft, request_id=request_id)
    governance._set("version: 1\nroles:\n  viewer:\n    sources:\n      demo: [web_traffic]\n", "test")
    with pytest.raises(HTTPException):
        chat_pipelines.run(VIEWER, draft, request_id=request_id)
    with pytest.raises(HTTPException):
        chat_pipelines.run(VIEWER, draft, request_id=str(uuid.uuid4()))
    assert len(pipelines.runs(result["id"], VIEWER)["runs"]) == 1


def test_step_failure_is_recorded_without_email_or_empty_success(monkeypatch):
    draft = _draft()
    real = queries.verify_sql
    calls = 0

    def failing(user, source, table, sql, **kw):
        nonlocal calls
        calls += 1
        return real(user, source, table, sql, **kw) if calls == 1 else {"ok": False, "error": "source became unavailable"}

    monkeypatch.setattr(queries, "verify_sql", failing)
    result = chat_pipelines.run(VIEWER, draft, request_id=str(uuid.uuid4()))
    assert result["run"]["status"] == "failed"
    assert result["run"]["failed_step"] == 0
    assert result["status"] == "ready"
    assert result["run"]["emailed"] is False
    monkeypatch.setattr(queries, "verify_sql", real)
    retry = chat_pipelines.run(VIEWER, result, request_id=str(uuid.uuid4()))
    assert retry["id"] == result["id"]
    assert retry["run"]["status"] == "success"


def test_interrupted_run_retry_completes_one_record(monkeypatch):
    draft = _draft()
    request_id = str(uuid.uuid4())
    real = pipelines.run_pipeline

    def interrupt(pipeline, user, *, run_id, notify_failure):
        # A crashed worker reserved this id but did not finish its SQL.
        with db.connect() as c:
            c.execute("INSERT INTO pipeline_runs (id,pipeline_id,user_id,status,started_at) VALUES (?,?,?,?,?)",
                      (run_id, pipeline["id"], user["id"], "running", 1.0))
            c.commit()
        raise jobs.ClaimLost("worker interrupted")

    monkeypatch.setattr(pipelines, "run_pipeline", interrupt)
    with pytest.raises(jobs.ClaimLost):
        chat_pipelines.run(VIEWER, draft, request_id=request_id)
    monkeypatch.setattr(pipelines, "run_pipeline", real)
    completed = chat_pipelines.run(VIEWER, draft, request_id=request_id)
    assert completed["run"]["status"] == "success"
    assert len(pipelines.runs(completed["id"], VIEWER)["runs"]) == 1


def test_concurrent_retries_save_one_pipeline_and_one_run(monkeypatch):
    draft = _draft()
    request_id = str(uuid.uuid4())
    real = pipelines.save_pipeline
    barrier = threading.Barrier(2)
    local = threading.local()

    def together(*args, **kwargs):
        if not getattr(local, "waited", False):
            local.waited = True
            barrier.wait(timeout=5)
        return real(*args, **kwargs)

    monkeypatch.setattr(pipelines, "save_pipeline", together)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(chat_pipelines.run, VIEWER, draft, request_id=request_id) for _ in range(2)]
        results = [f.result(timeout=15) for f in futures]
    assert results[0]["id"] == results[1]["id"]
    assert results[0]["run"]["id"] == results[1]["run"]["id"]
    assert all(r["run"]["status"] == "success" for r in results)
    assert len(pipelines.runs(results[0]["id"], VIEWER)["runs"]) == 1


def test_lost_job_claim_prevents_save(monkeypatch):
    draft = _draft()
    before = len(pipelines.listing(VIEWER)["pipelines"])
    def lost():
        raise jobs.ClaimLost("reclaimed")
    monkeypatch.setattr(jobs, "check_claim", lost)
    with pytest.raises(jobs.ClaimLost):
        chat_pipelines.run(VIEWER, draft, request_id=str(uuid.uuid4()))
    assert len(pipelines.listing(VIEWER)["pipelines"]) == before
