"""Every web, clock, folder and agent limit is a server setting, not a
constant in code: each has today's value as its default, takes effect when
set, and is held within safe bounds (a bad value falls back, an extreme one
is clamped). What stays fixed is what the providers' terms or safety
require (verbatim Bing/Google answers, no training, Google's two-year
ceiling, the page reader's address checks, five redirects, hidden files).

Run from the backend directory:
    python -m pytest tests/test_web_settings.py -q
"""
import datetime as dt
import io
import json
import urllib.error

import pytest
from langchain_core.messages import AIMessage

from app import agent, general_tools, local_files, settings
from app import web_search as web
from app.connectors.demo import DemoConnector

ANA = {"id": "u-settings", "email": "settings@studio.test", "role": "analyst", "name": "S"}
VIEWER = {"id": "u-settings-v", "email": "settings-v@studio.test", "role": "viewer", "name": "V"}


# ── The helpers ─────────────────────────────────────────────────────────

def test_numbers_fall_back_when_bad_and_are_clamped(monkeypatch):
    monkeypatch.delenv("X_N", raising=False)
    assert settings.integer("X_N", 7, 1, 10) == 7
    for raw, want in (("9", 9), ("0", 1), ("99", 10), ("lots", 7), ("", 7), (" 4 ", 4)):
        monkeypatch.setenv("X_N", raw)
        assert settings.integer("X_N", 7, 1, 10) == want, raw
    monkeypatch.setenv("X_N", "2.5")
    assert settings.number("X_N", 6.0, 1.0, 60.0) == 2.5
    monkeypatch.setenv("X_L", " Google, nope ,bing,google ")
    assert settings.names("X_L", ("a",), allowed={"google", "bing"}) == ["google", "bing"]
    monkeypatch.setenv("X_L", "nope")
    assert settings.names("X_L", ("a",), allowed={"google"}) == ["a"]
    monkeypatch.setenv("X_T", "   ")
    assert settings.text("X_T", "d") == "d"


# ── Providers ───────────────────────────────────────────────────────────

@pytest.fixture
def keys(monkeypatch):
    for k in ("STUDIO_WEB_SEARCH", "STUDIO_WEB_ORDER", "FOUNDRY_PROJECT_ENDPOINT",
              "STUDIO_ANTHROPIC_WEB_CURRENT_MODELS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")


def test_the_auto_order_is_a_setting(keys, monkeypatch):
    assert general_tools.provider_order() == ("bing", "google", "brave", "anthropic")
    assert general_tools.web_provider("openai:gpt-5") == "google"
    monkeypatch.setenv("STUDIO_WEB_ORDER", "brave, yahoo")
    assert general_tools.provider_order() == ("brave", "bing", "google", "anthropic")
    assert general_tools.web_provider("openai:gpt-5") == "brave"
    assert [p["id"] for p in general_tools.offered()] == ["brave", "google"]
    assert general_tools.web_provider("openai:gpt-5", "google") == "google"     # still pickable


def test_which_claude_models_get_the_newer_web_tools_is_a_setting(keys, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY")
    assert general_tools.web_tools("anthropic:claude-sonnet-5")[0]["type"] == "web_search_20260209"
    assert general_tools.web_tools("anthropic:claude-sonnet-7")[0]["type"] == "web_search_20250305"
    monkeypatch.setenv("STUDIO_ANTHROPIC_WEB_CURRENT_MODELS", "claude-sonnet-7,claude-opus-6")
    assert general_tools.web_tools("anthropic:claude-sonnet-7")[0]["type"] == "web_search_20260209"
    assert general_tools.web_tools("anthropic:Claude-Opus-6-1")[0]["type"] == "web_search_20260209"


def test_more_clock_nicknames_are_a_setting(monkeypatch):
    now = dt.datetime(2026, 11, 17, 18, 0, tzinfo=dt.timezone.utc)
    assert "I don't know" in general_tools.describe_time("hq", now)
    monkeypatch.setenv("STUDIO_CLOCK_ALIASES", "HQ = America/Chicago; moon base=Mars/Base; ;=x")
    assert "America/Chicago" in general_tools.describe_time("hq", now)
    assert "I don't know" in general_tools.describe_time("moon base", now)       # no such zone


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wire(monkeypatch):
    calls = []

    def urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "timeout": timeout,
                      "body": json.loads(req.data) if req.data and req.data[:1] == b"{" else req.data})
        if "oauth2" in req.full_url:
            return _Resp(json.dumps({"access_token": "t", "expires_in": 3600}).encode())
        if "bing" in req.full_url or "openai" in req.full_url or "/v9/" in req.full_url:
            return _Resp(json.dumps({"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "ok", "annotations": [
                    {"type": "url_citation", "url": f"https://s{i}.example/", "title": f"S{i}"}
                    for i in range(12)]}]}]}).encode())
        if "generateContent" in req.full_url:
            return _Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}).encode())
        return _Resp(json.dumps({"web": {"results": [
            {"url": f"https://r{i}.example/", "title": "T", "description": "x" * 900} for i in range(20)]}}).encode())
    monkeypatch.setattr(web.urllib.request, "urlopen", urlopen)
    web._token.update(value=None, expires=0.0, key=None)
    return calls


def test_bing_settings(wire, monkeypatch):
    for k, v in {"FOUNDRY_PROJECT_ENDPOINT": "https://a.ai.azure.com/api/projects/p",
                 "FOUNDRY_MODEL_DEPLOYMENT_NAME": "m", "BING_PROJECT_CONNECTION_ID": "c",
                 "AZURE_TENANT_ID": "t", "AZURE_CLIENT_ID": "i", "AZURE_CLIENT_SECRET": "s",
                 "BING_TIMEOUT_S": "45", "AZURE_TOKEN_TIMEOUT_S": "9", "BING_COUNT": "99",
                 "FOUNDRY_RESPONSES_PATH": "openai/v9/responses/",
                 "BING_GROUNDING_PROMPT": "Look up: {query}", "STUDIO_WEB_MAX_SOURCES": "3",
                 "AZURE_AUTHORITY_HOST": "http://insecure.example"}.items():
        monkeypatch.setenv(k, v)
    got = web.bing_search("tallest building")
    token, search = wire
    assert token["url"].startswith("https://login.microsoftonline.com/") and token["timeout"] == 9
    assert search["url"] == "https://a.ai.azure.com/api/projects/p/openai/v9/responses" and search["timeout"] == 45
    assert search["body"]["input"] == "Look up: tallest building"
    assert search["body"]["tools"][0]["bing_grounding"]["search_configurations"][0]["count"] == 50
    assert len(got["citations"]) == 3
    monkeypatch.setenv("BING_GROUNDING_PROMPT", "no placeholder")
    assert web.bing_prompt("q").endswith("Question: q")


def test_google_settings_and_its_two_year_ceiling(wire, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GOOGLE_API_BASE", "https://gateway.example/v2/")
    monkeypatch.setenv("GOOGLE_TIMEOUT_S", "12")
    monkeypatch.setenv("GOOGLE_QUESTION_MAX_CHARS", "100")
    web.google_search("q" * 500)
    call = wire[-1]
    assert call["url"] == "https://gateway.example/v2/models/gemini-2.5-flash:generateContent"
    assert call["timeout"] == 12 and len(call["body"]["contents"][0]["parts"][0]["text"]) == 100
    monkeypatch.setenv("GOOGLE_API_BASE", "http://plain.example")
    assert web.google_config()["base"] == "https://generativelanguage.googleapis.com/v1beta"
    monkeypatch.setenv("GOOGLE_RETENTION_DAYS", "90")
    assert web.google_retention_days() == 90
    monkeypatch.setenv("GOOGLE_RETENTION_DAYS", "5000")
    assert web.google_retention_days() == 730                         # the terms' ceiling


def test_brave_settings(wire, monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "b")
    monkeypatch.setenv("BRAVE_API_URL", "https://brave-proxy.example/search")
    monkeypatch.setenv("BRAVE_TIMEOUT_S", "7")
    monkeypatch.setenv("BRAVE_SNIPPET_MAX_CHARS", "60")
    monkeypatch.setenv("STUDIO_WEB_MAX_SOURCES", "4")
    monkeypatch.setenv("STUDIO_WEB_QUERY_MAX_CHARS", "50")
    got = web.brave_search("w" * 300)
    call = wire[-1]
    assert call["url"].startswith("https://brave-proxy.example/search?q=" + "w" * 50 + "&")
    assert call["timeout"] == 7
    assert len(got["results"]) == 4 and len(got["results"][0]["snippet"]) == 60


def test_page_reader_settings(monkeypatch):
    assert web.user_agent().startswith("StudioBot/1.0") and web.robots_agent() == "StudioBot"
    monkeypatch.setenv("STUDIO_WEB_USER_AGENT", "AcmeReader/2.0 (+https://acme.example/bot)")
    assert web.robots_agent() == "AcmeReader"
    monkeypatch.setenv("STUDIO_WEB_FETCH_MAX_BYTES", "1")
    assert web.page_bytes() == 64 * 1024                               # clamped up
    monkeypatch.setenv("STUDIO_WEB_FETCH_MAX_BYTES", str(10 ** 9))
    assert web.page_bytes() == 20 * 1024 * 1024                        # clamped down
    monkeypatch.setenv("STUDIO_WEB_FETCH_MAX_CHARS", "5000")
    assert web.page_chars() == 5000


# ── The shared folder ───────────────────────────────────────────────────

@pytest.fixture
def folder(tmp_path, monkeypatch):
    base = tmp_path / "shared"
    base.mkdir()
    (base / "a.md").write_text("alpha " * 600)
    (base / "app.conf").write_text("port = 8080\n")
    (base / "b.txt").write_text("beta\n")
    monkeypatch.setenv("STUDIO_LOCAL_FILES_DIR", str(base))
    for k in ("STUDIO_LOCAL_FILES_ROLES", "STUDIO_LOCAL_FILES_TYPES", "STUDIO_LOCAL_FILES_TEXT_TYPES"):
        monkeypatch.delenv(k, raising=False)
    return base


def test_folder_roles_and_file_kinds_are_settings(folder, monkeypatch):
    assert not local_files.available(VIEWER)
    monkeypatch.setenv("STUDIO_LOCAL_FILES_ROLES", "admin, viewer, superuser")
    assert local_files.available(VIEWER) and not local_files.available(ANA)
    with pytest.raises(ValueError, match="cannot be read"):
        local_files.read_file("app.conf")
    monkeypatch.setenv("STUDIO_LOCAL_FILES_TEXT_TYPES", "conf, ../bad")
    assert local_files.read_file("app.conf")["text"] == "port = 8080"
    monkeypatch.setenv("STUDIO_LOCAL_FILES_TYPES", "md")
    with pytest.raises(ValueError, match="cannot be read"):
        local_files.read_file("b.txt")
    assert local_files.read_file("a.md")["text"].startswith("alpha")
    monkeypatch.setenv("STUDIO_LOCAL_FILES_TYPES", "exe")                  # nothing supported: all
    assert local_files.read_file("b.txt")["text"] == "beta"


def test_folder_size_and_search_limits_are_settings(folder, monkeypatch):
    monkeypatch.setenv("STUDIO_LOCAL_FILES_MAX_CHARS", "1000")
    assert "more characters" in local_files.read_file("a.md")["text"]
    assert len(local_files.read_file("a.md")["text"].split("\n")[0]) <= 1000
    monkeypatch.setenv("STUDIO_LOCAL_FILES_LIST_MAX", "10")
    assert len(local_files.list_files()) == 3
    monkeypatch.setenv("STUDIO_LOCAL_FILES_TEXT_TYPES", "conf")
    monkeypatch.setenv("STUDIO_LOCAL_FILES_SEARCH_HITS", "1")
    assert len(local_files.search("a")) == 1
    assert local_files.max_read_bytes() == 10 * 1024 * 1024
    monkeypatch.setenv("STUDIO_LOCAL_FILES_MAX_MB", "3")
    assert local_files.max_read_bytes() == 3 * 1024 * 1024


# ── Agent step limits ───────────────────────────────────────────────────

def test_agent_step_limits_are_settings(monkeypatch):
    from app import db
    db.init_db()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    seen = []

    def fake_graph(llm, tools, system, spec, volatile=None):
        class G:
            def invoke(self, state, config=None):
                seen.append(config["recursion_limit"])
                return {"messages": [state["messages"][-1], AIMessage(content="ok")]}
        return G()
    monkeypatch.setattr(agent, "_graph", fake_graph)
    run = lambda: agent.run_agent("q", DemoConnector(), "*", ["sales"], {"sales": []}, [], ANA, model="openai:gpt-5")
    run()
    monkeypatch.setenv("STUDIO_AGENT_MAX_STEPS", "30")
    run()
    monkeypatch.setenv("STUDIO_AGENT_MAX_STEPS", "1")
    run()
    assert seen == [16, 30, 4]
    monkeypatch.setenv("STUDIO_ASSISTANT_MAX_STEPS", "20")
    agent.run_general("hi", ANA, [], "openai:gpt-5")
    assert seen[-1] == 20
