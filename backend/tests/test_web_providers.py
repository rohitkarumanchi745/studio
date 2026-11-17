"""Web search from several providers, chosen per chat, each used the way its
terms allow.

Locked in:
- Google (Grounding with Google Search, Gemini API): the request, the
  grounded answer and its Search Suggestions, and failures as fixed
  sentences; the answer is shown as it comes back (the tool ends the turn);
  only its asker sees it, and for two years at most — read hidden, then
  removed by the retention ticker, which runs whenever Google is set;
- Brave (Brave Search API): plain results, HTML-free and deduplicated, that
  the agent answers from and cites;
- the chat's toggle: "off" removes the web, "auto" takes the first
  configured, a named provider is used only if configured and allowed;
  STUDIO_WEB_SEARCH bounds every choice; /web/providers lists what is
  offered;
- a data agent whose web search answered: the answer is that provider's,
  with no SQL or chart mixed in;
- none of these providers' output trains anything or is cached.

Run from the backend directory:
    python -m pytest tests/test_web_providers.py -q
"""
import io
import json
import time
import urllib.error

import pytest
from langchain_core.messages import AIMessage

from app import agent, chat, db, general_tools, lightning, qcache, retention
from app import web_search as web
from app.connectors.demo import DemoConnector

ANA = {"id": "u-webprov", "email": "webprov@studio.test", "role": "analyst", "name": "A"}
OTHER = {"id": "u-webprov-2", "email": "webprov2@studio.test", "role": "analyst", "name": "B"}

GEMINI_REPLY = {"candidates": [{
    "content": {"parts": [{"text": "Spain won Euro 2024, "}, {"text": "beating England 2–1."}]},
    "groundingMetadata": {
        "webSearchQueries": ["euro 2024 winner"],
        "groundingChunks": [
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "title": "uefa.com"}},
            {"web": {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc", "title": "dup"}},
            {"web": {"uri": "javascript:alert(1)", "title": "bad"}}],
        "searchEntryPoint": {"renderedContent": "<style>.chip{}</style><div class=\"chip\"><a href=\"https://www.google.com/search?q=euro+2024+winner\">euro 2024 winner</a></div>"},
    }}]}
BRAVE_REPLY = {"type": "search", "web": {"results": [
    {"title": "Euro <strong>2024</strong> final", "url": "https://news.example/euro", "description": "Spain <strong>beat</strong> England.", "age": "2 days ago"},
    {"title": "dup", "url": "https://news.example/euro", "description": "x"},
    {"title": "bad", "url": "javascript:alert(1)", "description": "x"}]}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def providers(monkeypatch):
    for k in ("FOUNDRY_PROJECT_ENDPOINT", "STUDIO_WEB_SEARCH", "BRAVE_COUNTRY", "GOOGLE_GROUNDING_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "brave-key")
    calls, script = [], {"error": None}

    def urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "headers": dict(req.header_items()),
                      "body": req.data.decode() if req.data else None, "method": req.get_method()})
        if script["error"]:
            raise urllib.error.HTTPError(req.full_url, script["error"], "secret detail", {}, None)
        reply = GEMINI_REPLY if "generativelanguage" in req.full_url else BRAVE_REPLY
        return _Resp(json.dumps(reply).encode())
    monkeypatch.setattr(web.urllib.request, "urlopen", urlopen)
    return calls, script


# ── Google ──────────────────────────────────────────────────────────────

def test_google_grounding_request_and_answer(providers):
    calls, _ = providers
    got = web.google_search("Who won Euro 2024?")
    req = calls[-1]
    assert req["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/"
                          "gemini-2.5-flash:generateContent")
    assert req["headers"]["X-goog-api-key"] == "gem-key"
    assert json.loads(req["body"]) == {"contents": [{"role": "user", "parts": [{"text": "Who won Euro 2024?"}]}],
                                       "tools": [{"google_search": {}}]}
    assert got["text"] == "Spain won Euro 2024, beating England 2–1."
    assert got["citations"] == [{"url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                                 "title": "uefa.com"}]
    assert got["queries"] == [{"query": "euro 2024 winner"}]
    assert got["suggestions_html"].startswith("<style>")


@pytest.mark.parametrize("code,said", [(401, "refused the API key"), (404, "GOOGLE_GROUNDING_MODEL"),
                                       (429, "rate limited"), (500, "HTTP 500")])
def test_google_failures_are_fixed_sentences(providers, code, said):
    providers[1]["error"] = code
    with pytest.raises(RuntimeError) as e:
        web.google_search("x")
    assert said in str(e.value) and "secret" not in str(e.value)


def test_googles_answer_is_shown_as_it_comes_back_with_its_suggestions(providers, monkeypatch):
    seen = {"citations": [], "queries": []}
    search, fetch = general_tools.client_web_tools("openai:gpt-5", seen, choice="google")
    assert search.return_direct is True and fetch.name == "web_fetch"
    assert search.invoke({"question": "Who won Euro 2024?"}) == "Spain won Euro 2024, beating England 2–1."
    pages, grounding = general_tools.grounding(seen, "google", ANA["id"])
    assert grounding["provider"] == "google" and grounding["verbatim"] is True
    assert grounding["asked_by"] == ANA["id"] and grounding["suggestions_html"].startswith("<style>")
    assert pages == [{"source": "uefa.com", "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc",
                      "kind": "web"}]


# ── Brave ───────────────────────────────────────────────────────────────

def test_brave_results_are_plain_and_the_agent_answers_from_them(providers):
    calls, _ = providers
    got = web.brave_search("euro 2024 final")
    req = calls[-1]
    assert req["url"] == "https://api.search.brave.com/res/v1/web/search?q=euro+2024+final&count=8"
    assert req["headers"]["X-subscription-token"] == "brave-key" and req["method"] == "GET"
    assert got == {"results": [{"url": "https://news.example/euro", "title": "Euro 2024 final",
                                "snippet": "Spain beat England.", "age": "2 days ago"}],
                   "queries": [{"query": "euro 2024 final"}]}
    seen = {"citations": [], "queries": []}
    search, _ = general_tools.client_web_tools("openai:gpt-5", seen, choice="brave")
    assert search.return_direct is False
    out = search.invoke({"query": "euro 2024 final"})
    assert out == "[1] Euro 2024 final — https://news.example/euro (2 days ago)\n    Spain beat England."
    pages, grounding = general_tools.grounding(seen, "brave", ANA["id"])
    assert pages == [{"source": "Euro 2024 final", "url": "https://news.example/euro", "kind": "web"}]
    assert grounding["provider"] == "brave" and grounding["verbatim"] is False


# ── The chat's toggle ───────────────────────────────────────────────────

def test_the_toggle_picks_within_what_the_server_allows(providers, monkeypatch):
    claude, gpt = "anthropic:claude-sonnet-5", "openai:gpt-5"
    assert general_tools.web_provider(gpt) == "google"          # auto: first configured (no Bing here)
    assert general_tools.web_provider(gpt, "auto") == "google"
    assert general_tools.web_provider(gpt, "brave") == "brave"
    assert general_tools.web_provider(gpt, "anthropic") is None  # not a Claude model
    assert general_tools.web_provider(claude, "anthropic") == "anthropic"
    assert general_tools.web_provider(gpt, "bing") is None       # not configured
    assert general_tools.web_provider(gpt, "off") is None
    assert general_tools.client_web_tools(gpt, {"citations": [], "queries": []}, choice="off") == []
    assert general_tools.web_rule(gpt, "off") == general_tools.NO_WEB_RULE
    assert general_tools.web_rule(gpt, "brave") == general_tools.WEB_RULE
    assert general_tools.web_rule(gpt, "google") == general_tools.VERBATIM_RULE
    # The server pins one provider: nothing else, whatever is picked.
    monkeypatch.setenv("STUDIO_WEB_SEARCH", "brave")
    assert general_tools.web_provider(gpt, "google") is None
    assert general_tools.web_provider(gpt) == "brave"
    assert [p["id"] for p in general_tools.offered()] == ["brave"]
    monkeypatch.setenv("STUDIO_WEB_SEARCH", "off")
    assert general_tools.web_provider(gpt, "brave") is None and general_tools.offered() == []
    assert general_tools.client_web_tools(gpt, {"citations": [], "queries": []}, choice="brave") == []


def test_the_providers_endpoint_lists_what_the_toggle_offers(providers, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    out = chat.web_providers(ANA)
    assert out["enabled"] is True and out["default"] == "google"
    assert [(p["id"], p["label"], p["verbatim"]) for p in out["providers"]] == [
        ("google", "Google", True), ("brave", "Brave", False), ("anthropic", "Claude web search", False)]
    monkeypatch.setenv("STUDIO_WEB_SEARCH", "off")
    assert chat.web_providers(ANA) == {"enabled": False, "providers": [], "default": None}


def test_a_chat_request_takes_only_known_choices():
    assert chat.Ask(prompt="q", source="demo", table="*", web="google").web == "google"
    with pytest.raises(Exception):
        chat.Ask(prompt="q", source="demo", table="*", web="yahoo")


# ── A data agent whose web search answered ──────────────────────────────

def test_a_data_agents_web_answer_has_no_data_mixed_in(providers, monkeypatch):
    db.init_db()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})

    def fake_graph(llm, tools, system, spec, volatile=None):
        by_name = {t.name: t for t in tools if not isinstance(t, dict)}

        class G:
            def invoke(self, state, config=None):
                by_name["run_sql"].invoke({"sql": "SELECT region, SUM(revenue) FROM sales GROUP BY 1"})
                by_name["web_search"].invoke({"question": "Who won Euro 2024?"})
                return {"messages": [state["messages"][-1], AIMessage(content="my own rewrite")]}
        return G()
    monkeypatch.setattr(agent, "_graph", fake_graph)
    out = agent.run_agent("revenue and who won euro 2024?", DemoConnector(), "*", ["sales"], {"sales": []}, [],
                          ANA, model="openai:gpt-5", web="google")
    assert out["text"] == "Spain won Euro 2024, beating England 2–1."
    assert out["sql"] is None and out["rows"] == [] and out["panels"] == [] and out["chart"] is None
    assert out["web_grounding"]["provider"] == "google" and out["web_grounding"]["asked_by"] == ANA["id"]


# ── Google: its asker only, two years at most ───────────────────────────

def _google_chat(created_offset=0.0):
    db.init_db()
    chat.init_tables()
    cid = db.create_conversation(ANA["id"], "t")
    turn = {"source": "*", "table": "general", "author_role": "analyst"}
    db.add_message(cid, "user", {"text": "who won euro 2024?", **turn})
    mid = db.add_message(cid, "assistant", {"text": "Spain won.", "mode": "general", **turn,
                                            "web_grounding": {"provider": "google", "verbatim": True,
                                                              "asked_by": ANA["id"], "queries": []}})
    if created_offset:
        with db.connect() as c:
            c.execute("UPDATE messages SET created_at=? WHERE id=?", (time.time() - created_offset, mid))
            c.commit()
    db.share_conversation(cid, OTHER["id"], "edit")
    return cid


def test_a_google_answer_is_shown_to_its_asker_only():
    cid = _google_chat()
    mine = chat._visible_messages(cid, ANA, "owner")[-1]["content"]
    theirs = chat._visible_messages(cid, OTHER, "edit")[-1]["content"]
    assert mine["text"] == "Spain won."
    assert theirs["web_withheld"] is True and "Spain" not in json.dumps(theirs)
    assert "only to the person who asked" in theirs["text"]


def _answer(cid, user, access):
    return next(m for m in chat._visible_messages(cid, user, access) if m["role"] == "assistant")


def test_after_two_years_it_is_hidden_and_then_removed(monkeypatch, providers):
    cid = _google_chat(created_offset=731 * 86400)          # (it now sorts before its question)
    assert _answer(cid, ANA, "owner")["content"]["web_withheld"] is True
    assert chat.expire_google_answers() >= 1
    with db.connect() as c:
        rows = c.execute("SELECT content FROM messages WHERE conversation_id=? AND role='assistant'",
                         (cid,)).fetchall()
    stored = json.loads(rows[-1]["content"])
    assert "Spain" not in json.dumps(stored) and stored["web_grounding"] == {"provider": "google", "expired": True}
    # A recent one stays.
    recent = _google_chat()
    chat.expire_google_answers()
    assert _answer(recent, ANA, "owner")["content"]["text"] == "Spain won."


def test_the_retention_ticker_runs_whenever_google_is_set(monkeypatch, providers):
    monkeypatch.setenv("STUDIO_MESSAGE_ROWS_RETENTION_DAYS", "0")
    monkeypatch.setattr(chat, "_retention_days", lambda: 0)
    assert retention.ticker_enabled() is True
    monkeypatch.delenv("GEMINI_API_KEY")
    assert retention.ticker_enabled() is False
    called = []
    monkeypatch.setattr(chat, "expire_google_answers", lambda now=None: called.append(now))
    assert retention.tick_once(now=123.0) is None and called == [123.0]


# ── No provider's output trains anything or is cached ───────────────────

@pytest.mark.parametrize("provider", ["bing", "google", "brave"])
def test_no_web_providers_output_trains_or_is_cached(provider, monkeypatch):
    db.init_db()
    answer = {"text": "x", "sql": "SELECT 1", "rows": [[1]], "panels": [],
              "web_grounding": {"provider": provider, "queries": []}}
    tid = lightning.record_chat_trace(ANA, None, "q", answer, 5)
    with db.connect() as c:
        meta = json.loads(c.execute("SELECT meta FROM agent_traces WHERE id=?", (tid,)).fetchone()["meta"])
    assert meta["web_grounded"] is True and not lightning.global_training_eligible(meta)
    monkeypatch.setattr(qcache, "_sig", lambda p: pytest.fail("cached"))
    assert qcache.store(ANA, "demo", "*", "q", answer) is None


def test_a_data_agent_with_the_web_off_has_none(providers, monkeypatch):
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    seen = {}

    def fake_graph(llm, tools, system, spec, volatile=None):
        seen["tools"] = [t.get("name") if isinstance(t, dict) else t.name for t in tools]
        seen["system"] = system

        class G:
            def invoke(self, state, config=None):
                return {"messages": [state["messages"][-1], AIMessage(content="ok")]}
        return G()
    monkeypatch.setattr(agent, "_graph", fake_graph)
    agent.run_agent("q", DemoConnector(), "*", ["sales"], {"sales": []}, [], ANA,
                    model="anthropic:claude-sonnet-5", web="off")
    assert not {"web_search", "web_fetch"} & set(seen["tools"])
    assert general_tools.NO_WEB_RULE in seen["system"]
