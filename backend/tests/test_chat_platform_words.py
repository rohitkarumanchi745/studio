"""Platform runs asked for in plain English, in the regular chat.

"Kick off the daily sales pipeline on Airflow", "run /opt/jobs/etl.py on
Spark on Kubernetes with image my/spark:3.5", "run `python etl.py` on Ray".
Locked in:
- more ways of asking ("kick off", "launch", "I'd like to…", "let's…");
- a DAG named in words is looked up among the DAGs Airflow has: the one that
  says the same words, a choice of several (offered on the card, pickable
  by name), or a reason — never a made-up id; without a listing an exact id
  is taken as typed and anything else is asked about;
- key=value pairs become Airflow conf / Databricks job parameters;
- Spark on Kubernetes reads its file, image, class, name and arguments from
  the words, with the operator's K8S_SPARK_IMAGE / K8S_SPARK_JOBS_DIR as the
  only defaults; Ray reads a quoted or python/bash command;
- the old safety rules hold: questions and negations never submit, unmapped
  words ("tomorrow") are asked about, small words are never ids;
- each missing field is reported once, the most specific reason first.

Run from the backend directory:
    python -m pytest tests/test_chat_platform_words.py -q
"""
import pytest

from app import chat_platforms as service
from app import platforms

DAGS = ["daily_sales", "daily_sales_backfill", "orders_load", "CustomerChurnScoring",
        "inventory_snapshot"]
ANALYST = {"id": "words-analyst", "role": "analyst", "email": "analyst@test.invalid"}


@pytest.fixture(autouse=True)
def airflow(monkeypatch):
    """A configured Airflow whose DAG list is DAGS; no network."""
    service._DAG_CACHE.clear()
    air = platforms.PLATFORMS["airflow"]
    monkeypatch.setattr(air, "configured", lambda: True)
    monkeypatch.setattr(air, "_cfg", lambda: {"url": "http://airflow.test", "public_url": "",
                                              "token": "t", "user": "", "password": "",
                                              "version": "v1"})
    calls = []

    def list_dags(limit=1000):
        calls.append(limit)
        return list(DAGS)
    monkeypatch.setattr(air, "list_dags", list_dags)
    for name in ("K8S_SPARK_IMAGE", "K8S_SPARK_JOBS_DIR"):
        monkeypatch.delenv(name, raising=False)
    for p in platforms.PLATFORMS.values():
        monkeypatch.setattr(p, "trigger", lambda *a: pytest.fail("chat triggered a platform"))
    yield calls
    service._DAG_CACHE.clear()


def _ask(prompt, previous=None):
    request = service.intent(prompt, previous)
    assert request is not None, prompt
    return request


# ── Airflow: a DAG named in words ───────────────────────────────────────

@pytest.mark.parametrize("prompt,dag", [
    ("Kick off the daily sales pipeline on Airflow", "daily_sales"),
    ("please launch daily sales on airflow", "daily_sales"),
    ("can you run the orders load dag in airflow", "orders_load"),
    ("I'd like to trigger customer churn scoring on Airflow", "CustomerChurnScoring"),
    ("let's run inventory_snapshot on airflow", "inventory_snapshot"),
    ("Go ahead and fire off the Daily Sales Backfill workflow in Airflow", "daily_sales_backfill"),
    ("re-run orders load on airflow", "orders_load"),
])
def test_a_dag_named_in_words_is_found_among_airflows_dags(prompt, dag):
    request = _ask(prompt)
    assert request["target"] == "airflow" and request["action"] == "submit"
    assert request["payload"] == {"dag_id": dag} and request["missing"] == []


def test_the_dag_list_is_read_once_a_minute(airflow):
    _ask("run the daily sales pipeline on airflow")
    _ask("run orders load on airflow")
    assert len(airflow) == 1


def test_several_matches_are_offered_and_one_can_be_picked():
    request = _ask("run the sales dag on airflow")
    assert request["payload"] == {}
    assert request["choices"] == ["daily_sales", "daily_sales_backfill"]
    assert request["missing"] == [
        "dag_id: more than one Airflow DAG matches “sales” — pick one: daily_sales, daily_sales_backfill"]
    # The card offers them; saying one on its own picks it, keeping the card's parameters.
    card = service._artifact("airflow", {"conf": {"day": 1}}, ANALYST,
                             missing=request["missing"], choices=request["choices"])
    picked = _ask("daily_sales_backfill", card)
    assert picked["payload"] == {"conf": {"day": 1}, "dag_id": "daily_sales_backfill"}
    assert picked["missing"] == []
    # Only a name the card offered, and only while it waits for one.
    assert service.intent("daily_sales_backfill", {**card, "status": "failed"}) is None
    assert service.intent("thanks", card) is None


def test_no_match_says_what_airflow_has_and_invents_nothing():
    request = _ask("run the payroll dag on airflow")
    assert request["payload"] == {}
    assert request["missing"] == [
        "dag_id: no Airflow DAG matches “payroll” — its DAGs include daily_sales, "
        "daily_sales_backfill, orders_load, CustomerChurnScoring, inventory_snapshot"]


def test_without_a_dag_list_an_exact_id_is_taken_and_words_are_asked_about(monkeypatch):
    monkeypatch.setattr(platforms.PLATFORMS["airflow"], "list_dags",
                        lambda limit=1000: (_ for _ in ()).throw(RuntimeError("HTTP 503")))
    assert _ask("run daily_sales_v2 on airflow")["payload"] == {"dag_id": "daily_sales_v2"}
    request = _ask("kick off the daily sales pipeline on airflow")
    assert request["payload"] == {}
    assert request["missing"] == ["dag_id: which Airflow DAG is “daily sales”? Give its exact id, "
                                  "e.g. “dag daily_sales”"]


def test_key_value_pairs_are_conf_or_job_parameters():
    request = _ask("trigger daily sales on airflow with region=West days=7 dry_run=true rate=0.5 "
                   "note='end of month'")
    assert request["payload"] == {"dag_id": "daily_sales", "conf": {
        "region": "West", "days": 7, "dry_run": True, "rate": 0.5, "note": "end of month"}}
    request = _ask("run databricks job 123 with day=2026-11-01")
    assert request["payload"] == {"job_id": 123, "job_parameters": {"day": "2026-11-01"}}
    request = _ask("run dbt cloud job 456 with full=1")
    assert any("key=value parameters are Airflow conf" in m for m in request["missing"])


# ── The old safety rules ────────────────────────────────────────────────

@pytest.mark.parametrize("prompt", [
    "How do I trigger the daily sales dag on Airflow?", "Don't run the daily sales pipeline on airflow",
    "what were sales by region last month", "Can Airflow run pipelines?", "Run it", "Thanks",
])
def test_questions_negations_and_data_questions_never_submit(prompt):
    assert service.intent(prompt) is None


def test_unmapped_words_are_asked_about_and_small_words_are_never_ids():
    request = _ask("Run Airflow DAG Sales tomorrow")
    assert request["payload"] == {"dag_id": "Sales"}
    assert request["missing"] == ["I could not use “tomorrow” — give extra run parameters as "
                                  "key=value pairs or a JSON payload"]
    for word in ("in", "to", "at", "from", "the"):
        assert _ask(f"run dag {word} airflow")["payload"].get("dag_id") != word


# ── Spark on Kubernetes in words ────────────────────────────────────────

def test_spark_reads_file_image_class_name_and_arguments():
    request = _ask("Run /opt/jobs/etl.py on Spark on Kubernetes with image my/spark:3.5 named nightly-etl "
                   "with args --day 2026-11-01 --mode 'full load'")
    assert request["target"] == "k8s_spark" and request["missing"] == []
    assert request["payload"] == {"main_file": "local:///opt/jobs/etl.py", "image": "my/spark:3.5",
                                  "name": "nightly-etl",
                                  "arguments": ["--day", "2026-11-01", "--mode", "full load"]}
    jar = _ask("submit s3a://lake/jobs/agg.jar to spark on k8s with class com.acme.Agg using image my/spark:3.5")
    assert jar["payload"] == {"main_file": "s3a://lake/jobs/agg.jar", "main_class": "com.acme.Agg",
                              "image": "my/spark:3.5", "type": "Scala"}
    assert jar["missing"] == []
    java = _ask("run the java job s3a://lake/jobs/agg.jar on kubernetes with image x/y:1")
    assert java["payload"]["type"] == "Java"
    assert java["missing"] == ["main_class: provide the Java entry point (class com.example.Main)"]


def test_spark_defaults_come_only_from_the_operator(monkeypatch):
    request = _ask("run etl.py on spark on kubernetes")
    assert request["payload"] == {}
    assert request["missing"] == [
        "main_file: where is etl.py? Give its full path, e.g. local:///opt/jobs/etl.py",
        "image: provide the Spark container image"]
    monkeypatch.setenv("K8S_SPARK_JOBS_DIR", "local:///opt/jobs/")
    monkeypatch.setenv("K8S_SPARK_IMAGE", "registry.acme/spark:4.0.4")
    request = _ask("run etl.py on spark on kubernetes")
    assert request["payload"] == {"main_file": "local:///opt/jobs/etl.py",
                                  "image": "registry.acme/spark:4.0.4"}
    assert request["missing"] == []
    # A named image still wins over the default.
    assert _ask("run etl.py on k8s with image mine:1")["payload"]["image"] == "mine:1"


def test_a_spark_card_is_filled_by_words_alone():
    card = service._artifact("k8s_spark", {"main_file": "local:///opt/jobs/etl.py"}, ANALYST,
                             missing=["image: provide the Spark container image"])
    request = _ask("image my/spark:3.5", card)
    assert request["payload"] == {"main_file": "local:///opt/jobs/etl.py", "image": "my/spark:3.5"}
    assert request["missing"] == []


# ── Ray in words ────────────────────────────────────────────────────────

def test_ray_reads_a_quoted_or_spelled_out_command():
    assert _ask("let's run `python etl.py --day 2026-11-01` on ray")["payload"] == {
        "entrypoint": "python etl.py --day 2026-11-01"}
    assert _ask("run python train.py --epochs 3 on ray please")["payload"] == {
        "entrypoint": "python train.py --epochs 3"}
    assert _ask("run the training on ray")["missing"]


# ── The card carries the choices ────────────────────────────────────────

def test_a_submitted_ambiguous_request_is_a_card_with_its_choices(monkeypatch):
    monkeypatch.setattr(service.supervisor, "submit", lambda *a, **k: pytest.fail("queued"))
    card = service.submit(_ask("run the sales dag on airflow"), ANALYST, request_id="r1")
    assert card["status"] == "needs_input"
    assert card["choices"] == ["daily_sales", "daily_sales_backfill"]
    many = service._artifact("airflow", {}, ANALYST, choices=[f"d{i}" for i in range(20)])
    assert len(many["choices"]) == 8


# ── Listing Airflow's DAGs ──────────────────────────────────────────────

def test_list_dags_pages_through_and_skips_inactive(monkeypatch):
    for name, value in (("AIRFLOW_URL", "http://airflow.test"), ("AIRFLOW_TOKEN", "tok")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AIRFLOW_API_VERSION", raising=False)
    seen = []

    def fake_json(method, url, headers=None, body=None, timeout=30, context=None):
        seen.append((method, url, headers.get("Authorization"), timeout))
        offset = int(url.rsplit("offset=", 1)[1])
        dags = [{"dag_id": f"dag_{offset + i}", "is_active": (offset + i) != 3} for i in range(100)]
        return {"dags": dags if offset < 200 else [], "total_entries": 200}
    monkeypatch.setattr(platforms, "_json", fake_json)
    air = platforms.AirflowPlatform()
    dags = air.list_dags()
    assert len(dags) == 199 and "dag_3" not in dags and dags[0] == "dag_0"
    assert [u for _, u, _, _ in seen] == ["http://airflow.test/api/v1/dags?limit=100&offset=0",
                                          "http://airflow.test/api/v1/dags?limit=100&offset=100"]
    assert all(m == "GET" and auth == "Bearer tok" and t == 10 for m, _, auth, t in seen)
    assert len(air.list_dags(limit=50)) == 50
    monkeypatch.delenv("AIRFLOW_URL")
    with pytest.raises(RuntimeError, match="not configured"):
        air.list_dags()
