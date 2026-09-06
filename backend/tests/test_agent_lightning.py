"""Studio as a real Agent Lightning client.

Proves the two halves of app/lightning.py's delivery path:

DEGRADATION (runs everywhere, package or no package) — with STUDIO_AGL_URL
unset a chat turn records exactly the trace it always did and enqueues
nothing; with it set the turn enqueues exactly ONE agl_emit job and makes no
HTTP call of its own; and a configured server that is unreachable (closed
port, or a URL that is not even a URL) fails the JOB, never the turn.

DELIVERY (skipped where the agentlightning package is not importable — the
suite's interpreter may not have it) — the job posts a RolloutCreate built
from the trace, with a rollout_id derived from the trace id, plus the run's
events and its reward as a RewardData reward event; re-running the same job
creates no second rollout and no duplicate events; a later 👍 SUPERSEDES the
heuristic reward instead of adding a rollout or a second reward; and a server
that is down fails the job so the queue retries it with backoff.

The delivery tests run the REAL Agent Lightning server routes (its rollouts +
events routers over its own in-memory store) on a local port, so the wire
calls are checked against the server that actually accepts them.

Run from the backend directory:
    python -m pytest tests/test_agent_lightning.py -q
"""
import importlib
import importlib.util
import threading
import time
import warnings
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore")

HAS_AGL = importlib.util.find_spec("agentlightning") is not None
needs_agl = pytest.mark.skipif(
    not HAS_AGL, reason="agentlightning is not importable in this interpreter")


AGENT_ANSWER = {
    "text": "Revenue by region.",
    "sql": "SELECT region, SUM(amount) FROM sales GROUP BY region",
    "columns": ["region", "revenue"],
    "rows": [["West", 10.0], ["East", 7.0]],
    "chart": {"type": "bar"},
    "panels": [], "email": None, "errors": [], "citations": [], "usage": {},
    "agents": [{"name": "Sales Analyst"}], "mode": "agent",
    "model": "anthropic:claude-opus-4-8",
}


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A fresh SQLite app database, no worker running, delivery unconfigured."""
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "agl.db"))
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    for k in ("STUDIO_AGL_URL", "STUDIO_AGL_TOKEN", "ANTHROPIC_API_KEY",
              "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import jobs, lightning
    lightning._TABLES_READY = False          # tables live in the new database
    from app import main as _main
    importlib.reload(_main)
    return SimpleNamespace(db=_db, jobs=jobs, lightning=lightning, main=_main)


@pytest.fixture()
def client(env, monkeypatch):
    """Logged-in API client whose agent answers canned (no provider key here)."""
    from app import agent
    monkeypatch.setattr(agent, "run_agent",
                        lambda **kw: dict(AGENT_ANSWER, model=kw.get("model") or "test"))
    with TestClient(env.main.app) as c:
        tok = c.post("/api/auth/login",
                     json={"email": "analyst@studio.local",
                           "password": "analyst123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c


@pytest.fixture()
def sent(monkeypatch):
    """Every httpx request this process makes, recorded (never blocked)."""
    calls = []
    real = httpx.Client.send

    def spy(self, request, *a, **kw):
        calls.append(str(request.url))
        return real(self, request, *a, **kw)

    monkeypatch.setattr(httpx.Client, "send", spy)
    return calls


@pytest.fixture()
def agl_server():
    """The real Agent Lightning store API on a local port."""
    pytest.importorskip("agentlightning")
    import uvicorn
    from fastapi import FastAPI

    from agentlightning.server import store
    from agentlightning.server.routes import events as event_routes
    from agentlightning.server.routes import rollouts as rollout_routes

    store._rollouts.clear()
    store._events.clear()
    store._terminal_order.clear()

    app = FastAPI()
    app.include_router(rollout_routes.router, prefix="/api")
    app.include_router(event_routes.router, prefix="/api")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "the Agent Lightning test server never came up"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


# ── Helpers ──────────────────────────────────────────────────────────────

def ask(client, prompt="revenue by region"):
    r = client.post("/api/chat", json={"prompt": prompt, "source": "demo",
                                       "table": "sales"})
    assert r.status_code == 200, r.text
    return r.json()["message"]


def agl_jobs(env):
    with env.db.connect() as c:
        rows = c.execute("SELECT * FROM background_jobs WHERE kind='agl_emit'").fetchall()
    return [dict(r) for r in rows]


def trace_row(env, trace_id):
    with env.db.connect() as c:
        r = c.execute("SELECT * FROM agent_traces WHERE id=?", (trace_id,)).fetchone()
    return None if r is None else dict(r)


def get_json(url):
    r = httpx.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def closed_port():
    """A port nothing is listening on."""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── 1. Unconfigured: bit-for-bit today's behaviour ───────────────────────

def test_unconfigured_turn_records_its_trace_and_enqueues_nothing(env, client):
    msg = ask(client)
    tid = msg["trace_id"]

    t = trace_row(env, tid)
    assert t["prompt"] == "revenue by region"
    assert t["sql"] == AGENT_ANSWER["sql"] and t["row_count"] == 2
    assert t["chart_type"] == "bar" and t["mode"] == "agent"
    assert t["reward_source"] == "heuristic" and t["reward"] > 0
    # The trace is the whole story: no queue traffic, no bookkeeping tables.
    assert agl_jobs(env) == []
    assert env.jobs.stats()["queued"] == 0
    assert env.lightning.emit_enabled() is False
    assert env.lightning.emit_trace(tid) == {"skipped": "unconfigured"}
    with env.db.connect() as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    assert "agl_deliveries" not in tables and "agl_status" not in tables


def test_unconfigured_health_is_unchanged(env, client):
    """agl_available() stays falsy when Agent Lightning is not in play, so
    /health and the pipelines banner read exactly as they did."""
    from app import lightning
    if HAS_AGL:                      # installed but unconfigured: honest detail
        info = lightning.agl_available()
        assert info["installed"] and info["configured"] is False
        assert info["reachable"] is None
    else:
        assert lightning.agl_available() is None
    assert client.get("/api/health").json()["status"] == "ok"


def test_sweep_does_nothing_when_unconfigured(env, client):
    tid = ask(client)["trace_id"]
    env.db.set_trace_reward(tid, 1.0, source="user")
    assert env.lightning.sweep_reward_updates() == 0
    assert agl_jobs(env) == []


# ── 2. Configured: the turn only enqueues ────────────────────────────────

def test_configured_turn_enqueues_one_job_and_does_no_http(env, client, monkeypatch, sent):
    url = f"http://127.0.0.1:{closed_port()}"
    monkeypatch.setenv("STUDIO_AGL_URL", url)

    msg = ask(client)

    queued = agl_jobs(env)
    assert len(queued) == 1
    assert queued[0]["status"] == "queued"
    assert queued[0]["payload"] == f'{{"trace_id": "{msg["trace_id"]}"}}'
    assert int(queued[0]["max_attempts"]) == 5
    # The chat path never talks to the server: delivery is the worker's job.
    assert [u for u in sent if u.startswith(url)] == []


def test_unreachable_url_never_raises_into_a_turn(env, client, monkeypatch):
    """Not even a URL: the turn answers, the job carries the failure."""
    monkeypatch.setenv("STUDIO_AGL_URL", "not-a-url")
    msg = ask(client)
    assert msg["text"] == AGENT_ANSWER["text"]
    assert len(agl_jobs(env)) == 1

    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    job = agl_jobs(env)[0]
    assert job["status"] in ("queued", "failed") and job["error"]
    # The answer is untouched by the failure.
    assert trace_row(env, msg["trace_id"])["ok"] == 1


def test_a_failing_delivery_retries_on_the_queue_then_gives_up(env, client, monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", f"http://127.0.0.1:{closed_port()}")
    monkeypatch.setenv("STUDIO_AGL_MAX_ATTEMPTS", "2")
    ask(client)

    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    job = agl_jobs(env)[0]
    assert job["status"] == "queued" and job["attempts"] == 1   # retry scheduled
    assert job["run_after"] > time.time()                       # with backoff

    with env.db.connect() as c:                                 # skip the backoff
        c.execute("UPDATE background_jobs SET run_after=? WHERE kind='agl_emit'",
                  (time.time() - 1,))
        c.commit()
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    assert agl_jobs(env)[0]["status"] == "failed"


def test_emit_skips_a_trace_that_no_longer_exists(env, client, monkeypatch):
    monkeypatch.setenv("STUDIO_AGL_URL", f"http://127.0.0.1:{closed_port()}")
    assert env.lightning.emit_trace("gone")["skipped"] == "unknown_trace"


# ── 3. Delivery against the real Agent Lightning server ──────────────────

@needs_agl
def test_the_job_posts_a_rollout_that_matches_the_trace(env, client, monkeypatch, agl_server):
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    tid = ask(client)["trace_id"]
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    assert agl_jobs(env)[0]["status"] == "done"

    rid = env.lightning.rollout_id_for(tid)
    assert rid == f"studio-{tid}"                    # stable, derived from the trace
    detail = get_json(f"{agl_server}/api/rollouts/{rid}")
    rollout, trace = detail["rollout"], trace_row(env, tid)
    assert rollout["input"]["prompt"] == trace["prompt"]
    assert rollout["input"]["data_id"] == tid        # what /rollouts/terminal exposes
    assert rollout["input"]["source"] == "demo" and rollout["input"]["table"] == "sales"
    assert rollout["metadata"]["studio_trace_id"] == tid
    assert rollout["metadata"]["model"] == trace["model"]
    assert rollout["metadata"]["agents"] == ["Sales Analyst"]
    assert rollout["is_train"] is True
    assert rollout["status"]["state"] == "succeeded"  # terminal: the trainer sees it

    events = get_json(f"{agl_server}/api/rollouts/{rid}/events")
    by_type = {e["event_type"]: e["data"] for e in events}
    assert by_type["studio.query"]["sql"] == trace["sql"]
    assert by_type["studio.query"]["row_count"] == 2
    assert by_type["studio.chart"]["type"] == "bar"
    assert by_type["studio.run"]["model"] == trace["model"]
    assert by_type["reward"]["value"] == pytest.approx(trace["reward"])
    assert by_type["reward"]["source"] == "heuristic"
    assert by_type["reward"]["reason"] == "studio_heuristic"

    # The terminal log the verl trainer pages through carries this rollout.
    page = get_json(f"{agl_server}/api/rollouts/terminal")
    assert [i["rollout_id"] for i in page["items"]] == [rid]
    assert page["items"][0]["data_id"] == tid


@needs_agl
def test_rerunning_the_job_does_not_duplicate_anything(env, client, monkeypatch, agl_server):
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    tid = ask(client)["trace_id"]
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    rid = env.lightning.rollout_id_for(tid)
    before = get_json(f"{agl_server}/api/rollouts/{rid}/events")

    # Exactly what an at-least-once queue does after a worker dies mid-job.
    env.lightning.emit_trace(tid)
    env.lightning.emit_trace(tid)

    after = get_json(f"{agl_server}/api/rollouts/{rid}/events")
    assert [e["event_type"] for e in after] == [e["event_type"] for e in before]
    assert len([e for e in after if e["event_type"] == "reward"]) == 1
    listed = get_json(f"{agl_server}/api/rollouts?state_in=succeeded")
    assert [r["rollout_id"] for r in listed] == [rid]      # one rollout, not three


@needs_agl
def test_a_later_thumbs_up_updates_the_reward(env, client, monkeypatch, agl_server):
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    tid = ask(client)["trace_id"]
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    rid = env.lightning.rollout_id_for(tid)
    heuristic = get_json(f"{agl_server}/api/rollouts/{rid}/events")
    assert [e["data"]["value"] for e in heuristic if e["event_type"] == "reward"] != [1.0]

    # 👍 through the real feedback route, then the reconciler that notices it.
    assert client.post("/api/feedback", json={"trace_id": tid, "score": 1,
                                              "note": "exactly right"}).status_code == 200
    assert env.lightning.sweep_reward_updates() == 1
    assert env.lightning.sweep_reward_updates() == 0        # not queued twice
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True

    # One rollout still, and exactly ONE reward — the user's, not a second one.
    listed = get_json(f"{agl_server}/api/rollouts?state_in=succeeded")
    assert [r["rollout_id"] for r in listed] == [rid]
    events = get_json(f"{agl_server}/api/rollouts/{rid}/events")
    rewards = [e for e in events if e["event_type"] == "reward"]
    assert len(rewards) == 1
    assert rewards[0]["data"]["value"] == 1.0
    assert rewards[0]["data"]["source"] == "user"
    assert rewards[0]["data"]["message"] == "exactly right"
    # The trajectory travels with the superseding attempt, so the trainer still
    # sees the run the reward belongs to.
    assert {e["event_type"] for e in events} >= {"studio.run", "studio.query", "reward"}
    detail = get_json(f"{agl_server}/api/rollouts/{rid}")
    assert len(detail["attempts"]) == 2                     # history kept, not overwritten
    assert env.lightning.delivery(tid)["reward_source"] == "user"


@needs_agl
def test_a_thumbs_down_after_a_thumbs_up_is_delivered_too(env, client, monkeypatch, agl_server):
    """The sweep is state-based, so reward changes keep flowing (a fixed job
    id would have collided on the second change)."""
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    tid = ask(client)["trace_id"]
    env.jobs.run_one("w", kinds=["agl_emit"])
    for score in (1, -1):
        client.post("/api/feedback", json={"trace_id": tid, "score": score})
        assert env.lightning.sweep_reward_updates() == 1
        assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    rid = env.lightning.rollout_id_for(tid)
    rewards = [e for e in get_json(f"{agl_server}/api/rollouts/{rid}/events")
               if e["event_type"] == "reward"]
    assert [r["data"]["value"] for r in rewards] == [0.0]


@needs_agl
def test_health_reports_the_last_contact_with_the_server(env, client, monkeypatch, agl_server):
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    ask(client)
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True

    info = client.get("/api/health").json()["agent_lightning"]
    assert info["installed"] and info["configured"] and info["url"] == agl_server
    assert info["reachable"] is True and info["last_error"] is None
    assert info["version"]

    # Point at a dead server: the next job records the failure, and /health
    # stops claiming everything is fine.
    monkeypatch.setenv("STUDIO_AGL_URL", f"http://127.0.0.1:{closed_port()}")
    ask(client)
    env.jobs.run_one("w", kinds=["agl_emit"])
    info = client.get("/api/health").json()["agent_lightning"]
    assert info["reachable"] is False and info["last_error"]


@needs_agl
def test_a_dead_server_fails_the_job_not_the_turn(env, client, monkeypatch, agl_server):
    """The same delivery that works against a live server fails cleanly when
    the server goes away — on the queue, with the turn already answered."""
    monkeypatch.setenv("STUDIO_AGL_URL", f"http://127.0.0.1:{closed_port()}")
    msg = ask(client)
    assert msg["text"] == AGENT_ANSWER["text"]
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    job = agl_jobs(env)[0]
    assert job["status"] == "queued" and job["attempts"] == 1
    assert job["error"]                       # the connection failure, on the job

    # Server back (a different one, same client): the retry delivers.
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    with env.db.connect() as c:
        c.execute("UPDATE background_jobs SET run_after=? WHERE kind='agl_emit'",
                  (time.time() - 1,))
        c.commit()
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    assert agl_jobs(env)[0]["status"] == "done"
    rid = env.lightning.rollout_id_for(msg["trace_id"])
    assert get_json(f"{agl_server}/api/rollouts/{rid}")["rollout"]["rollout_id"] == rid


@needs_agl
def test_per_agent_rollouts_are_delivered_as_their_own_rollouts(env, client, monkeypatch, agl_server):
    """orchestrator.record_agent_rollout goes through the same door."""
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    from app import db as _db
    user = _db.get_user_by_email("analyst@studio.local")
    tid = env.lightning.record_agent_rollout(
        user, "c1", "revenue by region", "Sales Analyst", "worker",
        dict(AGENT_ANSWER, _source="demo"), duration_ms=42)
    assert len(agl_jobs(env)) == 1
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True

    rid = env.lightning.rollout_id_for(tid)
    rollout = get_json(f"{agl_server}/api/rollouts/{rid}")["rollout"]
    assert rollout["metadata"]["mode"] == "agent:worker"
    assert rollout["metadata"]["agents"] == ["Sales Analyst"]
    reward = [e for e in get_json(f"{agl_server}/api/rollouts/{rid}/events")
              if e["event_type"] == "reward"][0]
    assert reward["data"]["source"] == "per_agent"


@needs_agl
def test_schemas_come_from_the_package_not_from_here(env, client, monkeypatch, agl_server):
    """The wire payloads are the package's models, so a schema change breaks
    loudly here instead of drifting silently."""
    from agentlightning import schemas
    monkeypatch.setenv("STUDIO_AGL_URL", agl_server)
    tid = ask(client)["trace_id"]
    t = env.lightning._trace(tid)

    create = schemas.RolloutCreate(rollout_id=env.lightning.rollout_id_for(tid),
                                   input=env.lightning.rollout_input(t),
                                   metadata=env.lightning.rollout_metadata(t))
    assert create.rollout_id.endswith(tid) and create.is_train is True
    reward = env.lightning._reward_data(t, schemas)
    assert isinstance(reward, schemas.RewardData) and 0 <= reward.value <= 1
    types = [e.event_type for e in env.lightning.trajectory_events(t, schemas)]
    assert types == ["studio.run", "studio.query", "studio.chart"]
    assert all(isinstance(e, schemas.EventCreate)
               for e in env.lightning.trajectory_events(t, schemas))


# ── 4. The full server app: the key, and a real fallback-mode turn ───────

AGL_KEY = "studio-test-key"


@pytest.fixture()
def agl_secure_server():
    """The package's WHOLE server (create_app), started with an API key.

    The agl_server fixture mounts the store routers bare, which never
    exercises the auth dependency create_app wraps them in — and
    STUDIO_AGL_TOKEN exists only to satisfy that dependency. This fixture is
    the same app `python -m agentlightning.server` runs."""
    pytest.importorskip("agentlightning")
    import uvicorn

    from agentlightning.server import store
    from agentlightning.server.app import create_app

    store._rollouts.clear()
    store._events.clear()
    store._terminal_order.clear()

    app = create_app({"key": AGL_KEY,
                      "default_proxy": {"model_name": "test-model",
                                        "include_log_probs": False,
                                        "train": {"temperature": 1},
                                        "val": {"temperature": 0.7}}})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "the Agent Lightning server never came up"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def keyed_get(url, key=AGL_KEY):
    r = httpx.get(url, headers={"Authorization": f"Bearer {key}"}, timeout=10)
    r.raise_for_status()
    return r.json()


@needs_agl
def test_the_token_is_the_key_the_real_server_demands(env, client, monkeypatch,
                                                      agl_secure_server):
    """STUDIO_AGL_TOKEN is sent as the Bearer key the server's auth dependency
    accepts — and a wrong one writes NOTHING, it just fails the job."""
    monkeypatch.setenv("STUDIO_AGL_URL", agl_secure_server)
    assert httpx.get(f"{agl_secure_server}/api/rollouts/terminal").status_code == 401

    monkeypatch.setenv("STUDIO_AGL_TOKEN", "wrong-key")
    tid = ask(client, "denied by the key")["trace_id"]
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    job = agl_jobs(env)[0]
    assert job["status"] == "queued" and "401" in job["error"]
    assert keyed_get(f"{agl_secure_server}/api/rollouts/terminal")["total_terminal"] == 0
    assert client.get("/api/health").json()["agent_lightning"]["reachable"] is False

    monkeypatch.setenv("STUDIO_AGL_TOKEN", AGL_KEY)
    with env.db.connect() as c:               # skip the queue's backoff
        c.execute("UPDATE background_jobs SET run_after=? WHERE kind='agl_emit'",
                  (time.time() - 1,))
        c.commit()
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    assert agl_jobs(env)[0]["status"] == "done"
    rid = env.lightning.rollout_id_for(tid)
    assert keyed_get(f"{agl_secure_server}/api/rollouts/{rid}")["rollout"]["input"]["prompt"] \
        == "denied by the key"
    assert client.get("/api/health").json()["agent_lightning"]["reachable"] is True


@pytest.fixture()
def fallback_client(env):
    """A logged-in client with NO provider key and NO canned agent: the real
    no-LLM fallback path, which is what a keyless deployment answers with."""
    with TestClient(env.main.app) as c:
        tok = c.post("/api/auth/login",
                     json={"email": "analyst@studio.local",
                           "password": "analyst123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c


@needs_agl
def test_a_fallback_turn_is_delivered_unscored_then_scored_by_a_human(
        env, fallback_client, monkeypatch, agl_secure_server):
    """A fallback-mode turn is deliberately unscored: it is delivered as a
    rollout with its trajectory and NO reward event, and the first reward it
    ever gets is the human's."""
    monkeypatch.setenv("STUDIO_AGL_URL", agl_secure_server)
    monkeypatch.setenv("STUDIO_AGL_TOKEN", AGL_KEY)
    msg = ask(fallback_client)
    tid = msg["trace_id"]
    assert msg["mode"] == "fallback"
    assert trace_row(env, tid)["reward"] is None       # unscored, not zero

    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    rid = env.lightning.rollout_id_for(tid)
    events = keyed_get(f"{agl_secure_server}/api/rollouts/{rid}/events")
    assert "studio.run" in {e["event_type"] for e in events}
    assert [e for e in events if e["event_type"] == "reward"] == []

    assert fallback_client.post("/api/feedback",
                                json={"trace_id": tid, "score": 1}).status_code == 200
    assert env.lightning.sweep_reward_updates() == 1
    assert env.jobs.run_one("w", kinds=["agl_emit"]) is True
    rewards = [e for e in keyed_get(f"{agl_secure_server}/api/rollouts/{rid}/events")
               if e["event_type"] == "reward"]
    assert len(rewards) == 1
    assert rewards[0]["data"]["value"] == 1.0 and rewards[0]["data"]["source"] == "user"
    # It stayed one rollout on attempt 0: there was no reward to supersede.
    assert keyed_get(f"{agl_secure_server}/api/rollouts/{rid}")["attempts"] == ["0"]
