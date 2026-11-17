"""Studio's own web tools (Grounding with Bing Search + a page reader) and files
from the machine's shared folder.

Locked in:
- Bing goes through the Foundry project's Responses API with the
  bing_grounding tool and a client-credentials Entra token (reused until it
  nears expiry); the reply's url_citation annotations and Bing query (the
  results page the terms require shown) are read wherever they sit; failures
  are fixed sentences, never the server's words;
- the page reader connects only to the public address it checked (pinned —
  the name is never resolved twice), checks each redirect again, refuses
  loopback, private, link-local and metadata addresses, verifies TLS for the
  name, honours robots.txt, reads text only, and caps size;
- which web a model gets: Bing (any model) when configured, else Anthropic's
  on Claude, else the page reader alone, or nothing when off;
- an answer grounded in Bing is shown as such, and kept out of training,
  learned rules, the plan cache and later traces' history excerpts;
- the shared folder: off unless set, admins and analysts only, nothing hidden
  or outside it (.., absolute paths, links out), size and iCloud checks;
  text, CSV, Excel and Word; search by name and content.

Run from the backend directory:
    python -m pytest tests/test_web_and_files.py -q
"""
import datetime as dt
import io
import ipaddress
import json
import os
import socket
import ssl
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app import agent, chat, db, general_tools, learned_rules, lightning, local_files, qcache
from app import web_search as web

ANA = {"id": "u-webfiles", "email": "webfiles@studio.test", "role": "analyst", "name": "W"}
VIEWER = {"id": "u-webfiles-v", "email": "webfiles-v@studio.test", "role": "viewer", "name": "V"}
BING_ENV = {"FOUNDRY_PROJECT_ENDPOINT": "https://acme.ai.azure.com/api/projects/studio",
            "FOUNDRY_MODEL_DEPLOYMENT_NAME": "gpt-4.1-mini",
            "BING_PROJECT_CONNECTION_ID": "/subscriptions/s/resourceGroups/r/providers/"
                                          "Microsoft.CognitiveServices/accounts/a/projects/p/connections/bing",
            "AZURE_TENANT_ID": "tenant-1", "AZURE_CLIENT_ID": "client-1", "AZURE_CLIENT_SECRET": "s3cret"}

FOUNDRY_REPLY = {
    "id": "resp_1", "status": "completed",
    "output": [
        {"type": "bing_grounding_call", "id": "call_1",
         "arguments": json.dumps({"requesturl": "https://api.bing.microsoft.com/v7.0/search?q=NYC%20weather%20today"})},
        {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "It is 12°C and cloudy in New York.", "annotations": [
                {"type": "url_citation", "url": "https://weather.example/nyc", "title": "NYC weather"},
                {"type": "url_citation", "url": "https://weather.example/nyc", "title": "dup"},
                {"type": "url_citation", "url": "javascript:alert(1)", "title": "bad"}]}]}],
}


@pytest.fixture
def bing(monkeypatch):
    """Foundry and Entra sign-in, faked at urlopen; records every request."""
    for k, v in BING_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("STUDIO_WEB_SEARCH", raising=False)
    web._token.update(value=None, expires=0.0, key=None)
    calls, script = [], {"reply": FOUNDRY_REPLY, "error": None, "token_error": None}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        body = req.data.decode() if req.data else ""
        calls.append({"url": req.full_url, "headers": dict(req.header_items()), "body": body})
        if "login.microsoftonline.com" in req.full_url:
            if script["token_error"]:
                raise urllib.error.HTTPError(req.full_url, script["token_error"], "no", {}, None)
            return Resp(json.dumps({"access_token": "tok-1", "expires_in": 3600}).encode())
        if script["error"]:
            raise urllib.error.HTTPError(req.full_url, script["error"], "detail with secrets", {}, None)
        return Resp(json.dumps(script["reply"]).encode())
    monkeypatch.setattr(web.urllib.request, "urlopen", urlopen)
    return calls, script


# ── Grounding with Bing Search ──────────────────────────────────────────

def test_bing_is_configured_only_with_every_setting_and_https(monkeypatch):
    for k in BING_ENV:
        monkeypatch.delenv(k, raising=False)
    assert web.bing_config() is None
    for k, v in BING_ENV.items():
        monkeypatch.setenv(k, v)
    cfg = web.bing_config()
    assert cfg["market"] == "en-US" and cfg["count"] == 7
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "http://acme.ai.azure.com/api/projects/studio")
    assert web.bing_config() is None
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", BING_ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.delenv("AZURE_CLIENT_SECRET")
    assert web.bing_config() is None


def test_a_search_signs_in_calls_foundry_and_reads_citations_and_the_bing_query(bing):
    calls, _ = bing
    got = web.bing_search("  NYC   weather today ", freshness="Day")
    token, search = calls
    assert token["url"] == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    assert "grant_type=client_credentials" in token["body"] and "scope=https%3A%2F%2Fai.azure.com%2F.default" in token["body"]
    assert search["url"] == "https://acme.ai.azure.com/api/projects/studio/openai/v1/responses"
    assert search["headers"]["Authorization"] == "Bearer tok-1"
    body = json.loads(search["body"])
    assert set(body) == {"model", "input", "tool_choice", "tools"}
    assert body["model"] == "gpt-4.1-mini" and body["tool_choice"] == "required"
    assert body["input"].endswith("Question: NYC weather today")
    assert body["tools"] == [{"type": "bing_grounding", "bing_grounding": {"search_configurations": [{
        "project_connection_id": BING_ENV["BING_PROJECT_CONNECTION_ID"], "count": 7,
        "market": "en-US", "set_lang": "en", "freshness": "day"}]}}]
    assert got == {"text": "It is 12°C and cloudy in New York.",
                   "citations": [{"url": "https://weather.example/nyc", "title": "NYC weather"}],
                   "queries": [{"query": "NYC weather today",
                                "url": "https://www.bing.com/search?q=NYC+weather+today"}]}
    # The token is reused.
    web.bing_search("again")
    assert [c["url"].split("/")[2] for c in calls].count("login.microsoftonline.com") == 1


def test_without_a_named_query_the_one_asked_is_the_reference(bing):
    _, script = bing
    script["reply"] = {"status": "completed", "output": FOUNDRY_REPLY["output"][1:]}
    got = web.bing_search("tallest building")
    assert got["queries"] == [{"query": "tallest building",
                               "url": "https://www.bing.com/search?q=tallest+building"}]
    assert "[1] NYC weather — https://weather.example/nyc" in web.format_results(got)


@pytest.mark.parametrize("code,said", [
    (401, "sign-in was not accepted"), (403, "Foundry User role"), (404, "BING_PROJECT_CONNECTION_ID"),
    (429, "rate limited"), (500, "HTTP 500")])
def test_failures_are_fixed_sentences(bing, code, said):
    _, script = bing
    script["error"] = code
    with pytest.raises(RuntimeError) as e:
        web.bing_search("x")
    assert said in str(e.value) and "secrets" not in str(e.value)


def test_a_bad_sign_in_or_bad_input_is_said(bing):
    _, script = bing
    script["token_error"] = 400
    with pytest.raises(RuntimeError, match="could not sign in to Azure"):
        web.bing_search("x")
    with pytest.raises(RuntimeError, match="Give a search query"):
        web.bing_search("   ")
    bad = web.bing_search  # freshness that is not a known form is dropped, not sent
    script["token_error"] = None
    web._token.update(value=None, expires=0.0, key=None)
    bad("x", freshness="yesterday; drop")
    assert "freshness" not in json.loads(bing[0][-1]["body"])["tools"][0]["bing_grounding"]["search_configurations"][0]


# ── Which web a model gets ──────────────────────────────────────────────

def test_bing_is_every_models_search_when_configured(bing, monkeypatch):
    seen = {"citations": [], "queries": []}
    for spec in ("anthropic:claude-sonnet-5", "openai:gpt-5"):
        assert general_tools.web_provider(spec) == "bing"
        assert general_tools.web_tools(spec) == []              # no Anthropic search alongside
        assert [t.name for t in general_tools.client_web_tools(spec, seen)] == ["web_search", "web_fetch"]
        assert general_tools.web_rule(spec) == general_tools.VERBATIM_RULE
    monkeypatch.setenv("STUDIO_WEB_SEARCH", "anthropic")
    assert general_tools.web_provider("anthropic:claude-sonnet-5") == "anthropic"
    assert general_tools.client_web_tools("anthropic:claude-sonnet-5", seen) == []
    assert general_tools.web_provider("openai:gpt-5") is None
    assert [t.name for t in general_tools.client_web_tools("openai:gpt-5", seen)] == ["web_fetch"]
    monkeypatch.setenv("STUDIO_WEB_SEARCH", "off")
    assert general_tools.client_web_tools("openai:gpt-5", seen) == []
    assert general_tools.web_rule("openai:gpt-5") == general_tools.NO_WEB_RULE


def test_the_bing_tool_returns_bings_answer_as_it_is_and_ends_the_turn(bing):
    seen = {"citations": [], "queries": []}
    steps = []
    search, _ = general_tools.client_web_tools("openai:gpt-5", seen, steps.append)
    assert search.return_direct is True
    out = search.invoke({"question": "NYC weather today"})
    assert out == "It is 12°C and cloudy in New York."            # Bing's words, untouched
    assert seen["direct"] == {"provider": "bing", "text": out, "suggestions_html": ""}
    assert steps == ["searched Bing for “NYC weather today”"]
    pages, grounding = general_tools.grounding(seen, "bing", "u-1")
    assert pages == [{"source": "NYC weather", "url": "https://weather.example/nyc", "kind": "web"}]
    assert grounding == {"provider": "bing", "verbatim": True, "label": "Bing", "note": "From a Bing web search", "asked_by": "u-1", "queries": [
        {"query": "NYC weather today", "url": "https://www.bing.com/search?q=NYC+weather+today"}]}
    bing[1]["error"] = 500
    failed = {"citations": [], "queries": []}
    search2, _ = general_tools.client_web_tools("openai:gpt-5", failed)
    assert search2.invoke({"question": "x"}) == "I couldn't search the web: Bing search failed (HTTP 500)."
    assert general_tools.grounding(failed, "bing")[1]["verbatim"] is False
    assert general_tools.grounding({"citations": [], "queries": []}, "bing") == ([], None)
    # Pages read without a Bing search are not Bing output.
    read_only = {"citations": [{"url": "https://a.example/x", "title": "A"}], "queries": []}
    assert general_tools.grounding(read_only, None) == (
        [{"source": "A", "url": "https://a.example/x", "kind": "web"}], None)


# ── The page reader ─────────────────────────────────────────────────────

@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("2606:4700:4700::1111", True), ("127.0.0.1", False), ("10.1.2.3", False),
    ("192.168.0.9", False), ("172.16.0.1", False), ("169.254.169.254", False), ("::1", False),
    ("::ffff:127.0.0.1", False), ("::ffff:8.8.8.8", True), ("fd00::1", False), ("224.0.0.1", False),
    ("100.64.0.1", False), ("100.100.100.200", False), ("fd00:ec2::254", False),
])
def test_only_public_addresses(ip, public):
    assert web._public(ip) is public


PAGES = {
    "/page": (200, "text/html; charset=utf-8",
              "<html><head><title>Weather in NYC</title><style>.x{}</style><script>var secret=1</script>"
              "</head><body><h1>Today</h1><p>12&deg;C and cloudy.</p><noscript>enable js</noscript>"
              "<ul><li>Wind 10 km/h</li></ul></body></html>"),
    "/latin": (200, "text/plain; charset=latin-1", "caf\xe9"),
    "/json": (200, "application/json", '{"temp": 12}'),
    "/binary": (200, "application/octet-stream", "\x00\x01"),
    "/private/x": (200, "text/plain", "never read"),
    "/gone": (404, "text/plain", "missing"),
    "/long": (200, "text/plain", "a" * 50),
}


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Host"), self.headers.get("User-Agent")))
        if self.path == "/robots.txt":
            body, code, ctype = self.server.robots, 200, "text/plain"
        elif self.path.startswith("/redirect-to/"):
            self.send_response(302)
            self.send_header("Location", self.path[len("/redirect-to/"):].replace("|", "/"))
            self.end_headers()
            return
        elif self.path == "/loop":
            self.send_response(301)
            self.send_header("Location", "/loop")
            self.end_headers()
            return
        else:
            code, ctype, body = PAGES.get(self.path, (404, "text/plain", "no"))
        data = body.encode("latin-1" if "latin" in ctype else "utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def site(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    srv.seen, srv.robots = [], "User-agent: StudioBot\nDisallow: /private\n"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    web._robots.clear()
    # "news.example" is a public name that resolves to this test server; the
    # loopback address counts as public here only.
    monkeypatch.setattr(web, "_public", lambda ip: ip == "127.0.0.1")
    real = socket.getaddrinfo

    def resolve(host, *a, **k):
        if host == "news.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        if host == "mixed.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]
        return real(host, *a, **k)
    monkeypatch.setattr(web.socket, "getaddrinfo", resolve)
    srv.base = f"http://news.example:{port}"
    yield srv
    srv.shutdown()
    srv.server_close()
    web._robots.clear()


def test_a_page_is_read_as_text_from_the_address_that_was_checked(site):
    page = web.fetch_page(site.base + "/page")
    assert page["title"] == "Weather in NYC"
    assert "12°C and cloudy." in page["text"] and "Wind 10 km/h" in page["text"]
    assert "secret" not in page["text"] and "enable js" not in page["text"] and ".x{}" not in page["text"]
    # Pinned: the request went to the checked address, still naming the host.
    path, host, agent_name = site.seen[-1]
    assert path == "/page" and host.startswith("news.example") and agent_name.startswith("StudioBot")


def test_the_name_is_resolved_once_and_the_connection_pinned(site, monkeypatch):
    """DNS rebinding: a name that is public when checked and something else
    when connecting must not reach the something else."""
    state = {"checking": False}
    real_check, real_dns = web._resolve_public, web.socket.getaddrinfo
    port = site.server_address[1]

    def checked(host, p):
        state["checking"] = True
        try:
            return real_check(host, p)
        finally:
            state["checking"] = False

    def dns(host, *a, **k):
        if host == "rebind.example":
            if not state["checking"]:
                raise socket.gaierror("resolved again")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
        return real_dns(host, *a, **k)
    monkeypatch.setattr(web, "_resolve_public", checked)
    monkeypatch.setattr(web.socket, "getaddrinfo", dns)
    assert web.fetch_page(f"http://rebind.example:{port}/page")["title"] == "Weather in NYC"


def test_a_page_stops_at_the_size_cap(site, monkeypatch):
    monkeypatch.setattr(web, "page_bytes", lambda: 20)
    assert web.fetch_page(site.base + "/long")["text"] == "a" * 20


def test_charset_json_and_what_cannot_be_read(site):
    assert web.fetch_page(site.base + "/latin")["text"] == "café"
    assert web.fetch_page(site.base + "/json")["text"] == '{"temp": 12}'
    with pytest.raises(RuntimeError, match="application/octet-stream, which cannot be read"):
        web.fetch_page(site.base + "/binary")
    with pytest.raises(RuntimeError, match="HTTP 404"):
        web.fetch_page(site.base + "/gone")
    assert web.fetch_page(site.base + "/long", max_chars=10)["text"].startswith("aaaaaaaaaa\n… (the page continues; 40")
    for bad in ("ftp://news.example/x", "javascript:alert(1)", "news.example/page", ""):
        with pytest.raises(RuntimeError, match="full http"):
            web.fetch_page(bad)


def test_robots_txt_is_honoured(site):
    with pytest.raises(RuntimeError, match="robots.txt does not allow"):
        web.fetch_page(site.base + "/private/x")
    assert not any(p == "/private/x" for p, _, _ in site.seen)
    site.robots = "User-agent: *\nDisallow: /\n"
    web._robots.clear()
    with pytest.raises(RuntimeError, match="robots.txt"):
        web.fetch_page(site.base + "/page")


def test_redirects_are_checked_again_and_inside_addresses_refused(site):
    assert web.fetch_page(site.base + "/redirect-to/|page")["title"] == "Weather in NYC"
    for inside in ("http:||169.254.169.254|latest|meta-data", "http:||10.0.0.7|admin",
                   "http:||mixed.example|page"):
        with pytest.raises(RuntimeError, match="not a public web page"):
            web.fetch_page(site.base + "/redirect-to/" + inside)
    with pytest.raises(RuntimeError, match="redirects too many times"):
        web.fetch_page(site.base + "/loop")


def test_loopback_is_refused_for_real(monkeypatch):
    web._robots.clear()
    with pytest.raises(RuntimeError, match="not a public web page"):
        web.fetch_page("http://127.0.0.1:9/x")
    with pytest.raises(RuntimeError, match="not a public web page"):
        web.fetch_page("http://localhost:9/x")


def _cert(name, tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    return tmp_path / "c.pem", tmp_path / "k.pem"


def test_tls_is_verified_for_the_name_while_connecting_to_the_checked_address(site, tmp_path, monkeypatch):
    cert, key = _cert("news.example", tmp_path)
    tls = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    tls.seen, tls.robots = [], ""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    port = tls.server_address[1]
    trusted = ssl.create_default_context(cafile=str(cert))
    monkeypatch.setattr(web.ssl, "create_default_context", lambda: trusted)
    try:
        assert web.fetch_page(f"https://news.example:{port}/page")["title"] == "Weather in NYC"
        # The same certificate under another name: refused.
        real = web.socket.getaddrinfo
        monkeypatch.setattr(web.socket, "getaddrinfo", lambda host, *a, **k: real(
            "news.example" if host == "other.example" else host, *a, **k))
        web._robots.clear()
        with pytest.raises(RuntimeError, match="Could not read other.example"):
            web.fetch_page(f"https://other.example:{port}/page")
    finally:
        tls.shutdown()
        tls.server_close()


# ── Bing answers stay out of training ───────────────────────────────────

BING_ANSWER = {"text": "12°C", "mode": "general", "model": "openai:gpt-5", "rows": [], "panels": [],
               "web_grounding": {"provider": "bing", "queries": [{"query": "q", "url": "https://www.bing.com/search?q=q"}]}}


def test_a_bing_answer_is_kept_out_of_training_and_later_history(monkeypatch):
    db.init_db()
    tid = lightning.record_chat_trace(ANA, None, "weather?", BING_ANSWER, 10)
    meta = json.loads(db.get_trace(tid)["meta"]) if hasattr(db, "get_trace") else None
    if meta is None:
        with db.connect() as c:
            meta = json.loads(c.execute("SELECT meta FROM agent_traces WHERE id=?", (tid,)).fetchone()["meta"])
    assert meta["web_grounded"] is True and meta["global_train_eligible"] is False
    assert lightning.global_training_eligible(meta) is False
    # A later data turn whose history holds that answer: excluded, text dropped.
    tid2 = lightning.record_chat_trace(ANA, None, "and revenue?", {"text": "x", "sql": "SELECT 1", "rows": []},
                                       10, history=[{"role": "user", "text": "weather?"},
                                                    {"role": "assistant", "text": "12°C per weather.example",
                                                     "web": "brave"}])
    with db.connect() as c:
        meta2 = json.loads(c.execute("SELECT meta FROM agent_traces WHERE id=?", (tid2,)).fetchone()["meta"])
    assert meta2["global_train_eligible"] is False
    assert meta2["history"][1]["text"] == "[an answer grounded in web search results]"
    assert "weather.example" not in json.dumps(meta2)
    # An ordinary trace is unaffected.
    tid3 = lightning.record_chat_trace(ANA, None, "revenue?", {"text": "x", "sql": "SELECT 1", "rows": []}, 10)
    with db.connect() as c:
        meta3 = json.loads(c.execute("SELECT meta FROM agent_traces WHERE id=?", (tid3,)).fetchone()["meta"])
    assert "bing_grounded" not in meta3 and lightning.global_training_eligible(meta3)


def test_learned_rules_never_see_a_bing_run(monkeypatch):
    traces = [{"prompt": "weather?", "sql": None, "error": None, "reward": 0.0, "reward_source": "h",
               "created_at": 10.0, "meta": json.dumps({"bing_grounded": True})},
              {"prompt": "revenue?", "sql": "SELEC", "error": "syntax", "reward": 0.0,
               "reward_source": "h", "created_at": 11.0, "meta": json.dumps({})}]
    assert learned_rules._bing_grounded(traces[0]) and not learned_rules._bing_grounded(traces[1])
    assert learned_rules._bing_grounded({"meta": "{not json"})
    monkeypatch.setattr(db, "list_traces", lambda **kw: list(traces))
    monkeypatch.setattr(learned_rules, "MIN_FAILURES", 1)
    seen = {}
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)

    class LLM:
        def invoke(self, prompt):
            seen["prompt"] = prompt
            return AIMessage(content="- Check column names.")
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: LLM())
    db.init_db()
    learned_rules.init_tables()
    learned_rules.draft(force=True)
    assert "revenue?" in seen["prompt"] and "weather?" not in seen["prompt"]


def test_the_plan_cache_never_keeps_a_bing_answer(monkeypatch):
    monkeypatch.setattr(qcache, "_sig", lambda p: pytest.fail("cached"))
    assert qcache.store(ANA, "demo", "*", "q", {**BING_ANSWER, "sql": "SELECT 1", "rows": [[1]]}) is None


def test_history_never_feeds_a_bing_or_google_answer_to_a_model():
    db.init_db()
    chat.init_tables()
    cid = db.create_conversation(ANA["id"], "t")
    turn = {"source": "*", "table": "general", "author_role": "analyst"}
    db.add_message(cid, "user", {"text": "weather?", **turn})
    db.add_message(cid, "assistant", {**BING_ANSWER, **turn,
                                      "web_grounding": {**BING_ANSWER["web_grounding"], "verbatim": True}})
    db.add_message(cid, "user", {"text": "and the news?", **turn})
    db.add_message(cid, "assistant", {"text": "Per Brave results, X happened.", **turn,
                                      "web_grounding": {"provider": "brave", "queries": [{"query": "news"}]}})
    db.add_message(cid, "user", {"text": "thanks", **turn})
    db.add_message(cid, "assistant", {"text": "Welcome.", **turn, "mode": "general"})
    history = chat._conversation(cid, ANA, "next")[1]
    assert [h.get("web") for h in history] == [None, "bing", None, "brave", None, None]
    assert history[1]["text"] == "[An answer from Bing web search was shown to the person as it came back.]"
    assert "12°C" not in str(history)
    assert history[3]["text"] == "Per Brave results, X happened."       # the agent's own words


# ── The shared folder ───────────────────────────────────────────────────

@pytest.fixture
def folder(tmp_path, monkeypatch):
    base = tmp_path / "shared"
    (base / "notes").mkdir(parents=True)
    (base / "notes" / "q3.md").write_text("# Q3\nRevenue grew 12% in the West.\n")
    (base / "sales.csv").write_text("region,revenue\nWest,10\n")
    (base / ".env").write_text("SECRET=1")
    (base / ".git").mkdir()
    (base / ".git" / "config").write_text("x")
    (tmp_path / "outside.txt").write_text("outside the folder")
    os.symlink(tmp_path / "outside.txt", base / "link.txt")
    import docx
    d = docx.Document()
    d.add_paragraph("Board memo: hire two engineers.")
    d.save(base / "memo.docx")
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["metric", "value"])
    wb.active.append(["churn", 0.04])
    wb.save(base / "kpis.xlsx")
    (base / "big.txt").write_text("x" * 100)
    monkeypatch.setenv("STUDIO_LOCAL_FILES_DIR", str(base))
    return base


def test_off_unless_set_and_only_for_admins_and_analysts(folder, monkeypatch):
    assert local_files.available(ANA) and local_files.available({"role": "admin"})
    assert not local_files.available(VIEWER)
    assert local_files.tools(VIEWER, []) == []
    assert [t.name for t in local_files.tools(ANA, [])] == ["list_local_files", "search_local_files",
                                                            "read_local_file"]
    monkeypatch.setenv("STUDIO_LOCAL_FILES_DIR", str(folder / "missing"))
    assert local_files.root() is None and local_files.tools(ANA, []) == []
    monkeypatch.delenv("STUDIO_LOCAL_FILES_DIR")
    assert not local_files.available(ANA)


def test_listing_shows_visible_files_inside_only(folder):
    paths = [f["path"] for f in local_files.list_files()]
    assert paths == ["big.txt", "kpis.xlsx", "memo.docx", "sales.csv", "notes/q3.md"]
    assert [f["path"] for f in local_files.list_files("notes")] == ["notes/q3.md"]


@pytest.mark.parametrize("path,said", [
    ("../outside.txt", "outside the shared folder"), ("notes/../../outside.txt", "outside"),
    ("link.txt", "outside the shared folder"), (".env", "Hidden files"), (".git/config", "Hidden"),
    ("missing.md", "no file"), ("/etc/passwd", "no file"),
])
def test_nothing_hidden_or_outside_is_read(folder, path, said):
    with pytest.raises(ValueError, match=said):
        local_files.read_file(path)


def test_text_csv_word_and_excel_are_read(folder):
    assert "Revenue grew 12%" in local_files.read_file("notes/q3.md")["text"]
    assert local_files.read_file("sales.csv")["text"].startswith("region,revenue")
    assert local_files.read_file("memo.docx")["text"] == "Board memo: hire two engineers."
    xlsx = local_files.read_file("kpis.xlsx")["text"]
    assert "churn" in xlsx and "0.04" in xlsx


def test_size_and_icloud_are_checked(folder, monkeypatch):
    monkeypatch.setattr(local_files, "max_read_bytes", lambda: 50)
    with pytest.raises(ValueError, match="larger than"):
        local_files.read_file("big.txt")
    monkeypatch.setattr(local_files, "_offloaded", lambda st: True)
    with pytest.raises(ValueError, match="in iCloud"):
        local_files.read_file("notes/q3.md")


def test_search_by_name_and_content(folder):
    # Every word, in the name or the text; the folder's own files before its subfolders'.
    assert local_files.search("west revenue") == [
        {"path": "sales.csv", "line": "West,10"},
        {"path": "notes/q3.md", "line": "Revenue grew 12% in the West."}]
    hits = {h["path"]: h["line"] for h in local_files.search("west")}
    assert hits == {"notes/q3.md": "Revenue grew 12% in the West.", "sales.csv": "West,10"}
    assert [h["path"] for h in local_files.search("memo")] == ["memo.docx"]
    assert local_files.search("secret") == []                 # .env is never searched
    with pytest.raises(ValueError, match="Give words"):
        local_files.search("  ")


# ── The assistant with Bing and the folder ──────────────────────────────

class _Model(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        _BOUND.append([t.get("type") if isinstance(t, dict) else t.name for t in tools])
        return self


_BOUND = []


def _scripted(monkeypatch, *replies):
    _BOUND.clear()
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    state = {"replies": iter(replies)}
    monkeypatch.setattr(agent, "make_llm", lambda *a, **k: _Model(messages=state["replies"]))
    return state


def test_the_assistant_answers_with_bings_words_and_nothing_after(bing, folder, monkeypatch):
    state = _scripted(monkeypatch,
                      AIMessage(content="Let me look that up.",
                                tool_calls=[{"name": "web_search",
                                             "args": {"question": "NYC weather today"}, "id": "a"}]),
                      AIMessage(content="never read"))
    out = agent.run_general("weather in NYC today?", ANA, [], "anthropic:claude-sonnet-5", sources=["demo"])
    assert _BOUND[0] == ["current_time", "use_data_sources", "web_search", "web_fetch",
                         "list_local_files", "search_local_files", "read_local_file"]
    assert out["text"] == "It is 12°C and cloudy in New York."         # as Bing gave it
    assert next(state["replies"]).content == "never read"               # no model wrote after it
    assert out["citations"] == [{"source": "NYC weather", "url": "https://weather.example/nyc", "kind": "web"}]
    assert out["web_grounding"] == {"provider": "bing", "verbatim": True, "label": "Bing", "note": "From a Bing web search", "asked_by": ANA["id"], "queries": [
        {"query": "NYC weather today", "url": "https://www.bing.com/search?q=NYC+weather+today"}]}


def test_the_assistant_reads_a_file_and_answers_in_its_own_words(folder, monkeypatch):
    _scripted(monkeypatch,
              AIMessage(content="", tool_calls=[{"name": "read_local_file", "args": {"path": "notes/q3.md"}, "id": "b"}]),
              AIMessage(content="Your notes say the West grew 12%."))
    out = agent.run_general("what do my Q3 notes say?", ANA, [], "openai:gpt-5", sources=["demo"])
    assert out["text"] == "Your notes say the West grew 12%."
    assert out["citations"] == [{"source": "notes/q3.md", "path": "notes/q3.md", "kind": "file"}]
    assert "web_grounding" not in out
