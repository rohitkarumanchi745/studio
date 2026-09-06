"""The self-hosted BitNet path, driven end to end against a stub engine.

Nothing had ever run Studio's BitNet side against a real endpoint: the two gates
in `router.bitnet_ready` (a serving URL + a published `tool_call` adapter) are
circular on a fresh deployment, so the whole cascade shipped unexecuted. These
tests close that loop locally. A threaded `http.server` stands in for
serving/gateway.py's *upstream* — it speaks OpenAI `/v1/chat/completions` and
records the RAW request body — and the real app is pointed at it with
STUDIO_LLM_BASE_URL. Nothing here touches the network or needs a GPU.

What is pinned:
  1. `bitnet_ready` flips True only when BOTH gates are satisfied.
  2. `make_llm(router.bitnet_spec(), user)` targets that endpoint, sends
     model "bitnet", and puts `studio_adapters` on the wire as a TOP-LEVEL body
     key shaped {"kind": {"uri", "version"}} — asserted against the raw JSON the
     stub received, and then fed through serving/gateway.py's OWN
     `_resolve_adapter`, so the contract is checked against the real consumer.
  3. `choose()` sends a learned/repeated prompt to bitnet and a novel one to the
     frontier, and a BitNet turn that produces no SQL escalates — driven through
     the real chat path (POST /api/chat), not a paraphrase of it.
  4. A per-user `user_style` adapter rides alongside the global `tool_call` one
     (the gateway's priority list depends on it).
  5. A 500 or a refused connection from the endpoint still finishes the turn on
     the frontier model, and — the bug this suite was written to catch — the
     frontier is NEVER re-pointed at the BitNet endpoint just because
     STUDIO_LLM_BASE_URL is set.

Run from the backend directory:
    python -m pytest tests/test_bitnet_path.py -q
"""
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-bitnet-path-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest
from fastapi.testclient import TestClient

from app import agent, db, qcache, trainer
from app import router as model_router
from app.connectors.demo import seed

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ADMIN = {"id": "u-admin", "email": "admin@studio.test", "role": "admin", "name": "Admin"}
ANALYST = {"id": "u-analyst", "email": "ana@studio.test", "role": "analyst"}

# A pattern that HAS been learned (repeated + well rewarded) and a variation of
# it whose lexical similarity lands in the learn band [0.6, 0.9) — near enough
# that BitNet has seen the family, far enough that the exact-plan cache misses.
LEARNED_PROMPT = "total revenue by region"
LEARNED_SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region"
REPEAT_PROMPT = "total revenue by region and month"     # jaccard 0.75 → bitnet
NOVEL_PROMPT = "who signed up last week"                # jaccard 0.00 → frontier


# ── The stub engine: what serving/gateway.py forwards, minus the LoRA plumbing ──

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):          # keep the test output clean
        pass

    def _send(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        stub = self.server.stub
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        stub.requests.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "raw": raw,                                  # the bytes, unparsed
            "body": json.loads(raw or b"{}"),
        })
        if stub.status != 200:
            return self._send(stub.status, {"error": {"message": "engine on fire",
                                                      "type": "server_error"}})
        return self._send(200, {
            "id": "chatcmpl-stub", "object": "chat.completion", "created": 0,
            "model": "bitnet",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": stub.reply}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


class StubEngine:
    """An OpenAI-compatible engine on localhost. `requests` holds the raw body of
    everything the app sent, which is what the contract assertions read."""

    def __init__(self, status=200, reply="ok"):
        self.status, self.reply, self.requests = status, reply, []
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.stub = self
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture()
def stub():
    s = StubEngine()
    yield s
    s.close()


def _dead_port():
    """A port with nothing listening on it — connection refused, no timeout."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Fresh tables, no adapters, no embedding service (so qcache scores
    lexically and the learn band is deterministic), no stray endpoint."""
    db.init_db()
    trainer.init_tables()
    qcache.init_tables()
    seed()
    with db.connect() as c:
        c.execute("DELETE FROM training_adapters")
        c.execute("DELETE FROM query_cache")
        c.commit()
    for k in ("STUDIO_LLM_BASE_URL", "STUDIO_LLM_API_KEY", "STUDIO_BITNET_LLM",
              "HARRIER_EMBED_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield


def _publish_tool_call(uri="/adapters/tool_call/v7"):
    return trainer.publish("global", "tool_call", uri, base_model="bitnet-2b",
                           metrics={"loss": 0.21, "n_rollouts": 412})


def _learn(source="demo", table_scope="sales", role="analyst", seen=4, reward=0.9):
    """Seed a LEARNED pattern directly (what qcache.store would have accumulated
    over `seen` well-rewarded runs of LEARNED_PROMPT)."""
    import time
    import uuid
    with db.connect() as c:
        c.execute(
            "INSERT INTO query_cache (id, role, source, table_scope, prompt, signature, sql, "
            "chart, text, hits, seen, avg_reward, embedding, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), role, source, table_scope, LEARNED_PROMPT,
             json.dumps(qcache._sig(LEARNED_PROMPT)), LEARNED_SQL, None, "revenue by region",
             0, seen, reward, None, time.time(), time.time()))
        c.commit()


def _capture_init_chat_model(monkeypatch):
    """Record exactly what make_llm hands to langchain's init_chat_model.

    Installs a stand-in `langchain.chat_models` for the duration, so this runs on
    an interpreter with or without langchain installed; the REAL langchain leg is
    exercised separately by test_make_llm_end_to_end_through_langchain."""
    seen = {}

    def init_chat_model(spec, **kwargs):
        seen["spec"], seen["kwargs"] = spec, kwargs
        return object()

    mod = types.ModuleType("langchain.chat_models")
    mod.init_chat_model = init_chat_model
    pkg = sys.modules.get("langchain") or types.ModuleType("langchain")
    monkeypatch.setitem(sys.modules, "langchain", pkg)
    monkeypatch.setitem(sys.modules, "langchain.chat_models", mod)
    return seen


# ── 1. The two gates on bitnet_ready ────────────────────────────────────

def test_bitnet_ready_needs_an_endpoint(monkeypatch, stub):
    _publish_tool_call()                                   # adapter yes, URL no
    assert model_router.bitnet_ready(ANALYST) is False
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", "   ")       # blank doesn't count
    assert model_router.bitnet_ready(ANALYST) is False
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    assert model_router.bitnet_ready(ANALYST) is True


def test_bitnet_ready_needs_a_tool_call_adapter(monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    assert model_router.bitnet_ready(ANALYST) is False      # URL yes, adapter no
    # A per-user STYLE adapter alone is not enough: without a tool-calling
    # policy the engine cannot emit the SQL tool call a turn is made of.
    trainer.publish(ANALYST["id"], "user_style", "/adapters/user/ana/v1")
    assert model_router.bitnet_ready(ANALYST) is False
    _publish_tool_call()
    assert model_router.bitnet_ready(ANALYST) is True
    # Global, not per-user: a different user is served by the same tool_call LoRA.
    assert model_router.bitnet_ready(ADMIN) is True
    assert model_router.bitnet_ready(None) is True


def test_bitnet_ready_survives_a_broken_registry(monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    monkeypatch.setattr(trainer, "active_adapters",
                        lambda uid: (_ for _ in ()).throw(RuntimeError("no table")))
    assert model_router.bitnet_ready(ANALYST) is False      # never raises into a turn


# ── 2. make_llm targets the endpoint and puts the adapters on the wire ──

def test_make_llm_targets_the_endpoint_with_the_adapter_body(monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call("/adapters/tool_call/v7")
    trainer.publish(ANALYST["id"], "user_style", "/adapters/user/ana/v3")
    seen = _capture_init_chat_model(monkeypatch)

    agent.make_llm(model_router.bitnet_spec(), ANALYST)

    assert seen["spec"] == "openai:bitnet"                  # → OpenAI protocol, model "bitnet"
    kw = seen["kwargs"]
    assert kw["base_url"] == stub.base_url
    # The endpoint is reached by URL, not by a provider key; the client still
    # insists on a credential, so a placeholder stands in for an open gateway.
    assert kw["api_key"] == "studio-local"
    assert kw["extra_body"]["studio_adapters"] == {
        "tool_call": {"uri": "/adapters/tool_call/v7", "version": 1},
        "user_style": {"uri": "/adapters/user/ana/v3", "version": 1},
    }


def test_make_llm_uses_the_configured_gateway_key(monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    monkeypatch.setenv("STUDIO_LLM_API_KEY", "gateway-secret")
    _publish_tool_call()
    seen = _capture_init_chat_model(monkeypatch)
    agent.make_llm(model_router.bitnet_spec(), ANALYST)
    assert seen["kwargs"]["api_key"] == "gateway-secret"


def test_make_llm_honours_a_custom_bitnet_spec(monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    monkeypatch.setenv("STUDIO_BITNET_LLM", "openai:bitnet-b1.58-2b")
    _publish_tool_call()
    seen = _capture_init_chat_model(monkeypatch)
    agent.make_llm(model_router.bitnet_spec(), ANALYST)
    assert seen["spec"] == "openai:bitnet-b1.58-2b"
    assert seen["kwargs"]["base_url"] == stub.base_url


def test_wire_body_is_exactly_what_the_serving_gateway_parses(monkeypatch, stub):
    """The kwargs make_llm produces, driven through the OpenAI protocol client,
    must land on the wire in the shape serving/gateway.py pops off the body."""
    openai = pytest.importorskip("openai")
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call("/adapters/tool_call/v7")
    trainer.publish(ANALYST["id"], "user_style", "/adapters/user/ana/v3")
    seen = _capture_init_chat_model(monkeypatch)
    agent.make_llm(model_router.bitnet_spec(), ANALYST)
    kw = seen["kwargs"]

    client = openai.OpenAI(base_url=kw["base_url"], api_key=kw["api_key"], max_retries=0)
    client.chat.completions.create(
        model=seen["spec"].split(":", 1)[1],
        messages=[{"role": "user", "content": "total revenue by region"}],
        extra_body=kw["extra_body"])

    assert len(stub.requests) == 1
    req = stub.requests[0]
    assert req["path"] == "/v1/chat/completions"
    assert req["authorization"] == "Bearer studio-local"
    body = json.loads(req["raw"])                    # the RAW bytes, re-parsed
    assert body["model"] == "bitnet"
    # TOP-LEVEL key (the gateway does payload.pop("studio_adapters")), not nested
    # under extra_body/model_kwargs.
    assert "studio_adapters" in body
    for kind, uri in (("tool_call", "/adapters/tool_call/v7"),
                      ("user_style", "/adapters/user/ana/v3")):
        assert set(body["studio_adapters"][kind]) == {"uri", "version"}
        assert body["studio_adapters"][kind]["uri"] == uri
        assert isinstance(body["studio_adapters"][kind]["version"], int)

    # …and the REAL gateway resolves it. Its documented priority is
    # "user_style,tool_call": the most specific adapter present wins.
    gw = _load_serving_gateway()
    assert gw._resolve_adapter(body["studio_adapters"]) == ("/adapters/user/ana/v3", "user_style")


def _load_serving_gateway():
    """Import serving/gateway.py by path (it is not on sys.path and is not a
    package). Importing only binds config + functions; main() is not run."""
    path = os.path.join(REPO, "serving", "gateway.py")
    spec = importlib.util.spec_from_file_location("studio_serving_gateway", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_make_llm_end_to_end_through_langchain(monkeypatch, stub):
    """The real client stack — no shim: make_llm → langchain → OpenAI SDK → stub."""
    pytest.importorskip("langchain")
    pytest.importorskip("langchain_openai")
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call("/adapters/tool_call/v7")
    trainer.publish(ANALYST["id"], "user_style", "/adapters/user/ana/v3")

    llm = agent.make_llm(model_router.bitnet_spec(), ANALYST)
    answer = llm.invoke("total revenue by region")

    assert answer.content == "ok"
    body = json.loads(stub.requests[0]["raw"])
    assert body["model"] == "bitnet"                 # STUDIO_BITNET_LLM → model field
    assert body["studio_adapters"]["tool_call"] == {"uri": "/adapters/tool_call/v7",
                                                    "version": 1}
    assert body["studio_adapters"]["user_style"] == {"uri": "/adapters/user/ana/v3",
                                                     "version": 1}


def test_frontier_is_never_repointed_at_the_bitnet_endpoint(monkeypatch, stub):
    """The regression that made configuring serving catastrophic: base_url was
    applied to EVERY spec, so the Anthropic frontier — including the escalation a
    failed BitNet turn depends on — was sent to the BitNet gateway."""
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    seen = _capture_init_chat_model(monkeypatch)

    agent.make_llm("anthropic:claude-opus-4-8", ANALYST)
    assert "base_url" not in seen["kwargs"]
    assert "extra_body" not in seen["kwargs"]

    # A hosted OpenAI model the user picked with their OWN key is equally untouched
    # — that key must not be shipped to a self-hosted box either.
    monkeypatch.setattr(agent, "user_key", lambda user, spec=None: "sk-user-byok")
    agent.make_llm("openai:gpt-4o", ANALYST)
    assert "base_url" not in seen["kwargs"]
    assert seen["kwargs"]["api_key"] == "sk-user-byok"
    agent.make_llm(model_router.bitnet_spec(), ANALYST)
    assert seen["kwargs"]["api_key"] == "studio-local"      # BYOK stays with its provider
    assert stub.requests == []                              # nothing reached the endpoint


def test_self_hosted_spec_is_available_without_a_provider_key(monkeypatch, stub):
    """serving/README.md: the base URL + the model spec are "all Studio needs".
    llm_available gates run_agent, so an OPENAI_API_KEY requirement here means the
    endpoint is never called at all."""
    assert agent.llm_available("openai:bitnet", ANALYST) is False   # no endpoint yet
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    assert agent.llm_available("openai:bitnet", ANALYST) is True
    assert agent.llm_available("openai:gpt-4o", ANALYST) is False   # unrelated: still keyed


# ── 3. choose(): learned → bitnet, novel → frontier ─────────────────────

def test_choose_routes_learned_to_bitnet_and_novel_to_frontier(monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    _learn()

    route, pattern = model_router.choose(ANALYST, "demo", "sales", REPEAT_PROMPT)
    assert route == "bitnet"
    assert pattern["sql"] == LEARNED_SQL and pattern["seen"] == 4
    assert qcache.LEARN_THRESHOLD <= pattern["similarity"] < qcache.CACHE_THRESHOLD

    assert model_router.choose(ANALYST, "demo", "sales", NOVEL_PROMPT) == ("frontier", None)


def test_choose_is_frontier_while_the_path_is_dormant(monkeypatch, stub):
    _learn()
    assert model_router.choose(ANALYST, "demo", "sales", REPEAT_PROMPT) == ("frontier", None)
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)     # still no adapter
    assert model_router.choose(ANALYST, "demo", "sales", REPEAT_PROMPT) == ("frontier", None)


def test_choose_requires_the_requesters_own_access(monkeypatch, stub):
    """The learned pattern is centralized; the ROUTING still checks that this
    requester can reach every table it touches."""
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    _learn()
    assert model_router.choose(ANALYST, "demo", "sales", REPEAT_PROMPT)[0] == "bitnet"

    from app import gateway
    monkeypatch.setattr(gateway, "scope", lambda user, source: (None, ["web_traffic"]))
    assert model_router.choose(ANALYST, "demo", "sales", REPEAT_PROMPT) == ("frontier", None)


# ── 4. user_style rides alongside tool_call ─────────────────────────────

def test_user_style_joins_tool_call_and_is_per_user():
    _publish_tool_call("/adapters/tool_call/v7")
    trainer.publish(ANALYST["id"], "user_style", "/adapters/user/ana/v1")

    assert trainer.active_adapters(ANALYST["id"]) == {
        "tool_call": {"uri": "/adapters/tool_call/v7", "version": 1},
        "user_style": {"uri": "/adapters/user/ana/v1", "version": 1},
    }
    # Another user gets the global adapter only — style never leaks across users.
    assert trainer.active_adapters(ADMIN["id"]) == {
        "tool_call": {"uri": "/adapters/tool_call/v7", "version": 1}}
    assert trainer.active_adapters(None) == {
        "tool_call": {"uri": "/adapters/tool_call/v7", "version": 1}}


def test_publishing_supersedes_and_serving_takes_the_newest():
    _publish_tool_call("/adapters/tool_call/v7")
    _publish_tool_call("/adapters/tool_call/v8")
    trainer.publish(ANALYST["id"], "user_style", "/adapters/user/ana/v1")
    trainer.publish(ANALYST["id"], "user_style", "/adapters/user/ana/v2")

    active = trainer.active_adapters(ANALYST["id"])
    assert active["tool_call"] == {"uri": "/adapters/tool_call/v8", "version": 2}
    assert active["user_style"] == {"uri": "/adapters/user/ana/v2", "version": 2}
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) n FROM training_adapters "
                      "WHERE status='active'").fetchone()["n"]
    assert n == 2                                   # one active per (scope, kind)

    # Both kinds present → the gateway's priority picks the per-user one.
    gw = _load_serving_gateway()
    assert gw._resolve_adapter(active) == ("/adapters/user/ana/v2", "user_style")


def test_gateway_falls_back_to_tool_call_for_a_user_without_style():
    _publish_tool_call("/adapters/tool_call/v8")
    gw = _load_serving_gateway()
    assert gw._resolve_adapter(trainer.active_adapters(ADMIN["id"])) == (
        "/adapters/tool_call/v8", "tool_call")


# ── 3b + 5. The real chat path: escalation and safe degradation ─────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "bitnet.db"))
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM",
              "STUDIO_LLM_BASE_URL", "HARRIER_EMBED_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    import importlib
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        tok = c.post("/api/auth/login",
                     json={"email": "analyst@studio.local",
                           "password": "analyst123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c


FRONTIER_ANSWER = {
    "text": "Revenue by region and month.", "sql": LEARNED_SQL,
    "columns": ["region", "revenue"], "rows": [["West", 10.0]], "chart": None,
    "panels": [], "email": None, "errors": [], "citations": [], "usage": {},
    "agents": [], "mode": "agent", "model": "anthropic:claude-opus-4-8",
}


def _ask(client, prompt=REPEAT_PROMPT):
    r = client.post("/api/chat", json={"prompt": prompt, "source": "demo",
                                       "table": "sales"})
    assert r.status_code == 200, r.text
    return r.json()["message"]


def _route_recorder(monkeypatch, bitnet_result):
    """Let the frontier leg answer canned (no provider key in the suite) while
    recording every spec chat asked for. `bitnet_result` is a callable so a test
    can let the REAL agent run against the endpoint."""
    calls = []

    def fake_run_agent(**kw):
        spec = kw.get("model")
        calls.append(spec)
        if agent.self_hosted(spec):
            return bitnet_result(kw)
        return dict(FRONTIER_ANSWER, model=spec)

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    return calls


def test_chat_routes_a_learned_prompt_to_bitnet(client, monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    _learn(role="analyst")
    calls = _route_recorder(monkeypatch, lambda kw: dict(
        FRONTIER_ANSWER, text="BitNet answered.", model=kw["model"]))

    msg = _ask(client)
    assert calls == ["openai:bitnet"]                    # frontier never consulted
    assert msg["served_by"] == "bitnet"
    assert msg["routed"]["model"] == "bitnet" and msg["routed"]["seen"] == 4


def test_chat_sends_a_novel_prompt_straight_to_the_frontier(client, monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    _learn(role="analyst")
    calls = _route_recorder(monkeypatch, lambda kw: pytest.fail("bitnet was called"))

    msg = _ask(client, NOVEL_PROMPT)
    assert calls == [None]                               # None → STUDIO_LLM default
    assert msg["served_by"] == "frontier"


def test_chat_escalates_when_bitnet_produces_no_sql(client, monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    _learn(role="analyst")
    calls = _route_recorder(monkeypatch, lambda kw: dict(
        FRONTIER_ANSWER, sql=None, rows=[], text="I could not write SQL.",
        model=kw["model"]))

    msg = _ask(client)
    assert calls == ["openai:bitnet", None]              # tried, then escalated
    assert msg["served_by"] == "frontier"
    assert msg["sql"] == LEARNED_SQL and msg["rows"]     # the turn still answered


def test_chat_escalates_when_bitnet_reports_an_error(client, monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    _learn(role="analyst")
    calls = _route_recorder(monkeypatch, lambda kw: dict(
        FRONTIER_ANSWER, errors=["rejected: table not allowed"], model=kw["model"]))

    msg = _ask(client)
    assert calls == ["openai:bitnet", None]
    assert msg["served_by"] == "frontier"


def test_chat_escalates_when_bitnet_raises(client, monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    _learn(role="analyst")

    def boom(kw):
        raise RuntimeError("engine exploded")

    calls = _route_recorder(monkeypatch, boom)
    msg = _ask(client)
    assert calls == ["openai:bitnet", None]
    assert msg["served_by"] == "frontier"


@pytest.mark.parametrize("mode", ["http_500", "refused"])
def test_turn_completes_on_the_frontier_when_the_endpoint_is_broken(
        client, monkeypatch, mode):
    """The whole path, degraded: a 500 from the engine and a refused connection
    both leave the turn answered by the frontier model. The BitNet leg here is
    the REAL agent — it genuinely dials the (broken) endpoint."""
    if mode == "http_500":
        broken = StubEngine(status=500)
        base_url = broken.base_url
    else:
        broken = None
        base_url = f"http://127.0.0.1:{_dead_port()}/v1"
    try:
        monkeypatch.setenv("STUDIO_LLM_BASE_URL", base_url)
        _publish_tool_call()
        _learn(role="analyst")

        real_run_agent = agent.run_agent
        calls = _route_recorder(monkeypatch, lambda kw: real_run_agent(**kw))

        msg = _ask(client)
        assert calls == ["openai:bitnet", None]
        assert msg["served_by"] == "frontier"
        assert msg["sql"] == LEARNED_SQL and msg["rows"]
        assert not (msg.get("text") or "").startswith("(Agent error")
        if broken is not None and _HAS_LANGCHAIN:
            assert broken.requests, "the real agent never dialled the endpoint"
    finally:
        if broken is not None:
            broken.close()


_HAS_LANGCHAIN = importlib.util.find_spec("langchain") is not None


def test_a_failed_bitnet_attempt_is_never_served_as_a_bitnet_answer(monkeypatch):
    """The escalation hinges on this: when the endpoint is down, run_agent's
    keyless preview carries a `sql` and (before the fix) no `errors` — exactly
    the shape chat accepts as a good BitNet answer. It must be flagged."""
    pytest.importorskip("langchain")
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", f"http://127.0.0.1:{_dead_port()}/v1")
    _publish_tool_call()
    from app.connectors.demo import DemoConnector
    conn = DemoConnector()
    out = agent.run_agent(prompt=REPEAT_PROMPT, connector=conn, table="sales",
                          allowed_tables=["sales"], schemas={"sales": []},
                          history=[], user=ADMIN, model="openai:bitnet")
    assert out["errors"] and "bitnet unavailable" in out["errors"][0]
    assert out["mode"] == "fallback"          # a preview, not a BitNet answer


# ── Explicitly choosing BitNet picks who answers FIRST, not whether the turn
#    is allowed to fail. ─────────────────────────────────────────────────

def _ask_explicit_bitnet(client, prompt=REPEAT_PROMPT):
    r = client.post("/api/chat", json={"prompt": prompt, "source": "demo",
                                       "table": "sales", "model": "bitnet"})
    assert r.status_code == 200, r.text
    return r.json()["message"]


def test_explicit_bitnet_escalates_when_it_produces_no_sql(client, monkeypatch, stub):
    """The reported lifecycle gap. Selecting 'bitnet' in the model picker took a
    branch that stamped served_by="bitnet" on WHATEVER came back and never
    escalated — so a dead endpoint handed the user the keyless preview labelled
    as a BitNet answer. The automatic tier has always escalated; this branch
    must apply the same test."""
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    calls = _route_recorder(monkeypatch, lambda kw: dict(
        FRONTIER_ANSWER, sql=None, rows=[], text="I could not write SQL.",
        model=kw["model"]))

    msg = _ask_explicit_bitnet(client)
    assert calls == ["openai:bitnet", None], "explicit selection must still escalate"
    assert msg["served_by"] == "frontier", "served_by must name who actually answered"
    assert msg.get("routed", {}).get("escalated_from") == "bitnet"
    assert msg["sql"] and msg["rows"], "the turn still has to answer"


def test_explicit_bitnet_escalates_when_it_errors(client, monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    calls = _route_recorder(monkeypatch, lambda kw: dict(
        FRONTIER_ANSWER, errors=["bitnet unavailable: connection refused"],
        model=kw["model"]))

    msg = _ask_explicit_bitnet(client)
    assert calls == ["openai:bitnet", None]
    assert msg["served_by"] == "frontier"


def test_explicit_bitnet_escalates_when_it_raises(client, monkeypatch, stub):
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    calls = []

    def _run(**kw):
        calls.append(kw["model"])
        if kw["model"] == "openai:bitnet":
            raise RuntimeError("engine exploded")
        return dict(FRONTIER_ANSWER, model=kw["model"])
    monkeypatch.setattr("app.agent.run_agent", lambda **kw: _run(**kw))

    msg = _ask_explicit_bitnet(client)
    assert calls == ["openai:bitnet", None]
    assert msg["served_by"] == "frontier"


def test_explicit_bitnet_still_serves_a_good_answer_as_bitnet(client, monkeypatch, stub):
    """The success path must be untouched: a real BitNet answer is still
    reported as BitNet, with no escalation."""
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", stub.base_url)
    _publish_tool_call()
    calls = _route_recorder(monkeypatch, lambda kw: dict(FRONTIER_ANSWER, model=kw["model"]))

    msg = _ask_explicit_bitnet(client)
    assert calls == ["openai:bitnet"], "a good answer must not consult the frontier"
    assert msg["served_by"] == "bitnet"
