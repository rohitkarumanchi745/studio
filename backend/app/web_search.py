"""Studio's own web tools: search (Bing, Google, Brave) and a page reader.

Three search providers, each used the way its terms allow:
    bing    Grounding with Bing Search (Azure AI Foundry): a grounded answer,
            shown to the user exactly as it comes back, with its references;
    google  Grounding with Google Search (Gemini API): a grounded answer, shown
            exactly as it comes back, with Google's Search Suggestions, to the
            person who asked only, for at most two years;
    brave   Brave Search API: plain results (title, link, snippet) that the
            agent writes its own answer from — a "Data for AI" plan grants that.
Google's Custom Search JSON API is closed to new customers and ends on
1 January 2027, so Google is reached through Gemini's grounding.

web_search goes through an Azure AI Foundry project's Responses API with the
`bing_grounding` tool: the supported way to ground AI answers in Bing since
the Bing Search APIs were retired. Foundry runs the search and a model in the
project (FOUNDRY_MODEL_DEPLOYMENT_NAME) writes a grounded summary with
`url_citation` annotations. Studio's agent reads that summary and the cited
links, and can open a page with web_fetch.

Grounding with Bing terms that shape this module (see
https://www.microsoft.com/bing/apis/grounding-legal-enterprise):
- the references (each website link, and the link to the Bing results page
  for the query) are kept and shown with the answer, as provided;
- Bing output must not train or evaluate AI models: an answer that used it is
  flagged (`web_grounding`), and the learning features skip it;
- no database of results is built: nothing here caches search output.

Configuration (all on the server; nothing reaches the browser):
    FOUNDRY_PROJECT_ENDPOINT        https://<resource>.ai.azure.com/api/projects/<project>
    FOUNDRY_MODEL_DEPLOYMENT_NAME   a model deployed in that project
    BING_PROJECT_CONNECTION_ID      the project's Grounding with Bing connection
    AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
                                    a service principal with the Foundry User
                                    role on the project
    BING_MARKET (en-US), BING_COUNT (7)

web_fetch is ours entirely: it reads one public http(s) page as text. It
connects only to addresses it checked are public (never loopback, private,
link-local or cloud metadata), checks each redirect again, honours
robots.txt, and caps size and time.
"""
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

from . import settings

# Fixed: how many redirects a page may take (each is checked again), and the
# Azure scope Foundry tokens are issued for (the API's own).
MAX_REDIRECTS = 5
_TOKEN_SCOPE = "https://ai.azure.com/.default"


# ── Settings (see backend/.env.example) ─────────────────────────────────

def user_agent():
    return settings.text("STUDIO_WEB_USER_AGENT", "StudioBot/1.0 (+web_fetch; reads pages an assistant cites)")


def robots_agent():
    """The name robots.txt rules are matched against: the user agent's first word."""
    return user_agent().split("/")[0].split()[0] or "StudioBot"


def page_bytes():
    return settings.integer("STUDIO_WEB_FETCH_MAX_BYTES", 2 * 1024 * 1024, 64 * 1024, 20 * 1024 * 1024)


def page_chars():
    return settings.integer("STUDIO_WEB_FETCH_MAX_CHARS", 12000, 1000, 200_000)


def max_sources():
    """Pages (and queries) kept per search and per answer."""
    return settings.integer("STUDIO_WEB_MAX_SOURCES", 8, 1, 50)


def query_chars():
    return settings.integer("STUDIO_WEB_QUERY_MAX_CHARS", 400, 50, 2000)


def _https(name, default):
    """An https base URL setting, without a trailing slash."""
    value = settings.text(name, default).rstrip("/")
    return value if urllib.parse.urlsplit(value).scheme == "https" else default.rstrip("/")


# ── Grounding with Bing Search ──────────────────────────────────────────

def bing_config():
    """The Foundry/Bing settings, or None when any is missing."""
    keys = {"endpoint": "FOUNDRY_PROJECT_ENDPOINT", "deployment": "FOUNDRY_MODEL_DEPLOYMENT_NAME",
            "connection": "BING_PROJECT_CONNECTION_ID", "tenant": "AZURE_TENANT_ID",
            "client_id": "AZURE_CLIENT_ID", "client_secret": "AZURE_CLIENT_SECRET"}
    cfg = {k: os.getenv(v, "").strip() for k, v in keys.items()}
    if not all(cfg.values()):
        return None
    if urllib.parse.urlsplit(cfg["endpoint"]).scheme != "https":
        return None
    path = settings.text("FOUNDRY_RESPONSES_PATH", "/openai/v1/responses")
    cfg.update(endpoint=cfg["endpoint"].rstrip("/"), market=settings.text("BING_MARKET", "en-US"),
               count=settings.integer("BING_COUNT", 7, 1, 50),
               authority=_https("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com"),
               responses_path="/" + path.strip("/"),
               timeout=settings.integer("BING_TIMEOUT_S", 90, 5, 600),
               token_timeout=settings.integer("AZURE_TOKEN_TIMEOUT_S", 20, 2, 120))
    return cfg


_token_lock = threading.Lock()
_token = {"value": None, "expires": 0.0, "key": None}


def _access_token(cfg):
    """An Entra ID token for Foundry (client credentials), reused until five
    minutes before it expires."""
    key = (cfg["tenant"], cfg["client_id"])
    with _token_lock:
        if _token["value"] and _token["key"] == key and time.time() < _token["expires"] - 300:
            return _token["value"]
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials", "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"], "scope": _TOKEN_SCOPE}).encode()
        url = f"{cfg['authority']}/{urllib.parse.quote(cfg['tenant'])}/oauth2/v2.0/token"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=cfg["token_timeout"]) as r:
                got = json.loads(r.read(1_000_000) or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError("Bing search could not sign in to Azure — check AZURE_TENANT_ID, "
                               f"AZURE_CLIENT_ID and AZURE_CLIENT_SECRET (HTTP {e.code}).") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise RuntimeError("Bing search could not reach Azure sign-in.") from None
        if not got.get("access_token"):
            raise RuntimeError("Azure sign-in returned no token for Bing search.")
        _token.update(value=got["access_token"], key=key,
                      expires=time.time() + float(got.get("expires_in") or 3600))
        return _token["value"]


_PROMPT = ("Answer this question from current web search results, concisely and factually — the "
           "key facts, numbers and dates the sources give. Do not add anything the sources do not "
           "say.\n\nQuestion: {query}")


def bing_prompt(query):
    """What Foundry's model is asked (BING_GROUNDING_PROMPT, with {query})."""
    template = settings.text("BING_GROUNDING_PROMPT", _PROMPT)
    return (template if "{query}" in template else _PROMPT).replace("{query}", query)
_BING_QUERY_URL = re.compile(r"https?://(?:api\.bing\.microsoft\.com/v7\.0/search|(?:www\.)?bing\.com/search)"
                             r"\?[^\s\"'<>]+", re.I)


def _http_url(url):
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return None
    return parts.geturl() if parts.scheme in ("http", "https") and parts.hostname else None


def bing_results_url(query):
    """The Bing results page for a query — the reference the terms require."""
    return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})


def _queries_in(value, out):
    """Every Bing query the response names, wherever it sits (a tool call's
    `arguments` may be a JSON string holding a `requesturl`)."""
    if isinstance(value, dict):
        for v in value.values():
            _queries_in(v, out)
    elif isinstance(value, list):
        for v in value:
            _queries_in(v, out)
    elif isinstance(value, str):
        for m in _BING_QUERY_URL.finditer(value):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(m.group(0)).query).get("q", [""])[0]
            q = q.strip().strip('"').strip()
            if q and q not in out:
                out.append(q)
        if value[:1] in "{[":
            try:
                _queries_in(json.loads(value), out)
            except ValueError:
                pass


def parse_bing_response(data, asked):
    """{text, citations: [{url, title}], queries: [{query, url}]} from a
    Foundry Responses API reply."""
    texts, citations = [], []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            texts.append(part.get("text") or "")
            for a in part.get("annotations") or []:
                url = _http_url((a or {}).get("url")) if (a or {}).get("type") == "url_citation" else None
                if url and all(c["url"] != url for c in citations):
                    citations.append({"url": url, "title": (a.get("title") or "").strip()})
    queries = []
    _queries_in(data.get("output") or [], queries)
    if not queries:
        queries = [asked]
    return {"text": "\n".join(t for t in texts if t).strip(), "citations": citations[:max_sources()],
            "queries": [{"query": q, "url": bing_results_url(q)} for q in queries[:max_sources()]]}


def bing_search(query, freshness=None):
    """Search the web with Grounding with Bing Search. RuntimeError with a
    fixed sentence when it cannot."""
    cfg = bing_config()
    if cfg is None:
        raise RuntimeError("Bing search is not configured on this server.")
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:query_chars()]
    if not query:
        raise RuntimeError("Give a search query.")
    search = {"project_connection_id": cfg["connection"], "count": cfg["count"],
              "market": cfg["market"], "set_lang": cfg["market"].split("-")[0]}
    if freshness and re.fullmatch(r"(?i)day|week|month|\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?",
                                  str(freshness)):
        search["freshness"] = str(freshness).lower() if str(freshness).isalpha() else str(freshness)
    body = {"model": cfg["deployment"], "input": bing_prompt(query), "tool_choice": "required",
            "tools": [{"type": "bing_grounding", "bing_grounding": {"search_configurations": [search]}}]}
    req = urllib.request.Request(
        f"{cfg['endpoint']}{cfg['responses_path']}", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {_access_token(cfg)}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            data = json.loads(r.read(4_000_000) or b"{}")
    except urllib.error.HTTPError as e:
        said = {401: "Bing search was refused: the service principal's sign-in was not accepted.",
                403: "Bing search was refused: give the service principal the Foundry User role "
                     "on the project.",
                404: "Bing search was not found: check FOUNDRY_PROJECT_ENDPOINT, the model "
                     "deployment name and BING_PROJECT_CONNECTION_ID.",
                429: "Bing search is busy (rate limited) — try again shortly."}
        raise RuntimeError(said.get(e.code, f"Bing search failed (HTTP {e.code}).")) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("Bing search could not reach the Foundry project.") from None
    out = parse_bing_response(data if isinstance(data, dict) else {}, query)
    if data.get("status") not in (None, "completed") and not out["text"]:
        raise RuntimeError(f"Bing search did not complete ({data.get('status')}).")
    return out


def format_results(result):
    """The search result as the agent reads it: the grounded summary, then
    the numbered sources."""
    lines = [result["text"] or "(no summary)", "", "Sources:"]
    lines += [f"[{i}] {c['title'] or c['url']} — {c['url']}"
              for i, c in enumerate(result["citations"], 1)] or ["(none cited)"]
    return "\n".join(lines)


# ── Grounding with Google Search (Gemini API) ───────────────────────────

GOOGLE_RETENTION_DAYS = 730          # Gemini API terms: grounded results, two years at most


def google_retention_days():
    """How long Google-grounded answers are kept: GOOGLE_RETENTION_DAYS, never
    more than the terms' two years."""
    return settings.integer("GOOGLE_RETENTION_DAYS", GOOGLE_RETENTION_DAYS, 1, GOOGLE_RETENTION_DAYS)


def google_config():
    """{key, model, base, timeout} for Gemini grounding, or None."""
    key = os.getenv("GEMINI_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
    if not key:
        return None
    return {"key": key, "model": settings.text("GOOGLE_GROUNDING_MODEL", "gemini-2.5-flash"),
            "base": _https("GOOGLE_API_BASE", "https://generativelanguage.googleapis.com/v1beta"),
            "timeout": settings.integer("GOOGLE_TIMEOUT_S", 90, 5, 600),
            "question_chars": settings.integer("GOOGLE_QUESTION_MAX_CHARS", 2000, 100, 20000)}


def parse_google_response(data):
    """{text, citations, queries, suggestions_html} from a generateContent reply."""
    cand = ((data or {}).get("candidates") or [{}])[0] or {}
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    meta = cand.get("groundingMetadata") or {}
    citations = []
    for ch in meta.get("groundingChunks") or []:
        w = (ch or {}).get("web") or {}
        url = _http_url(w.get("uri"))
        if url and all(c["url"] != url for c in citations):
            citations.append({"url": url, "title": (w.get("title") or "").strip()})
    html = ((meta.get("searchEntryPoint") or {}).get("renderedContent") or "")
    return {"text": text, "citations": citations[:max_sources()],
            "queries": [{"query": str(q)} for q in (meta.get("webSearchQueries") or []) if q][:max_sources()],
            "suggestions_html": html if isinstance(html, str) and len(html) <= 100_000 else ""}


def google_search(question):
    """Answer a question with Grounding with Google Search. RuntimeError with
    a fixed sentence when it cannot."""
    cfg = google_config()
    if cfg is None:
        raise RuntimeError("Google search is not configured on this server.")
    question = str(question or "").strip()[:cfg["question_chars"]]
    if not question:
        raise RuntimeError("Give a question.")
    body = {"contents": [{"role": "user", "parts": [{"text": question}]}],
            "tools": [{"google_search": {}}]}
    url = f"{cfg['base']}/models/{urllib.parse.quote(cfg['model'], safe='-._')}:generateContent"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"x-goog-api-key": cfg["key"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            data = json.loads(r.read(4_000_000) or b"{}")
    except urllib.error.HTTPError as e:
        said = {400: "Google search refused the request — check GOOGLE_GROUNDING_MODEL.",
                401: "Google search refused the API key.",
                403: "Google search refused the API key (no access to grounding).",
                404: "Google search: the model in GOOGLE_GROUNDING_MODEL was not found.",
                429: "Google search is busy (rate limited) — try again shortly."}
        raise RuntimeError(said.get(e.code, f"Google search failed (HTTP {e.code}).")) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("Google search could not be reached.") from None
    out = parse_google_response(data if isinstance(data, dict) else {})
    if not out["text"]:
        raise RuntimeError("Google search returned no answer.")
    return out


# ── Brave Search API ────────────────────────────────────────────────────

def brave_config():
    key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
    if not key:
        return None
    return {"key": key, "count": settings.integer("BRAVE_COUNT", 8, 1, 20),
            "country": settings.text("BRAVE_COUNTRY", ""),
            "url": _https("BRAVE_API_URL", "https://api.search.brave.com/res/v1/web/search"),
            "timeout": settings.integer("BRAVE_TIMEOUT_S", 30, 3, 300),
            "snippet_chars": settings.integer("BRAVE_SNIPPET_MAX_CHARS", 400, 50, 4000)}


def _plain(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def parse_brave_response(data, asked, snippet_chars=400):
    results = []
    for r in ((data or {}).get("web") or {}).get("results") or []:
        url = _http_url((r or {}).get("url"))
        if url and all(x["url"] != url for x in results):
            results.append({"url": url, "title": _plain(r.get("title")),
                            "snippet": _plain(r.get("description"))[:snippet_chars],
                            "age": str(r.get("age") or "")})
    return {"results": results[:max_sources()], "queries": [{"query": asked}]}


def brave_search(query):
    """Plain web results from Brave. RuntimeError with a fixed sentence."""
    cfg = brave_config()
    if cfg is None:
        raise RuntimeError("Brave search is not configured on this server.")
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:query_chars()]
    if not query:
        raise RuntimeError("Give a search query.")
    params = {"q": query, "count": cfg["count"]}
    if cfg["country"]:
        params["country"] = cfg["country"]
    req = urllib.request.Request(
        cfg["url"] + "?" + urllib.parse.urlencode(params),
        headers={"X-Subscription-Token": cfg["key"], "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            data = json.loads(r.read(4_000_000) or b"{}")
    except urllib.error.HTTPError as e:
        said = {401: "Brave search refused the API key.", 403: "Brave search refused the API key.",
                422: "Brave search could not read the query.",
                429: "Brave search is busy (rate limited) — try again shortly."}
        raise RuntimeError(said.get(e.code, f"Brave search failed (HTTP {e.code}).")) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("Brave search could not be reached.") from None
    return parse_brave_response(data if isinstance(data, dict) else {}, query, cfg["snippet_chars"])


def format_brave(result):
    lines = [f"[{i}] {r['title'] or r['url']} — {r['url']}" + (f" ({r['age']})" if r["age"] else "")
             + (f"\n    {r['snippet']}" if r["snippet"] else "")
             for i, r in enumerate(result["results"], 1)]
    return "\n".join(lines) or "(no results)"


# ── The page reader ─────────────────────────────────────────────────────

def _public(ip):
    """A globally routable unicast address — never loopback, private,
    link-local (cloud metadata), reserved or multicast."""
    ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _resolve_public(host, port):
    """The first public address `host` resolves to; RuntimeError when any is
    not public (a name that also points inside is refused whole). Cloud
    metadata addresses are link-local or private, so they never pass."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise RuntimeError(f"Could not find {host}.") from None
    addrs = [i[4][0] for i in infos]
    if not addrs or not all(_public(a) for a in addrs):
        raise RuntimeError("That address is not a public web page.")
    return addrs[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, ip, **kw):
        super().__init__(host, **kw)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to a checked address, with the certificate verified for the name."""

    def __init__(self, host, ip, **kw):
        super().__init__(host, context=ssl.create_default_context(), **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _get(url, timeout=None, limit=None):
    """(status, headers, body bytes, final url) for one GET, following up to
    five redirects, each checked again. The body stops at `limit` bytes."""
    limit = limit or page_bytes()
    timeout = timeout or settings.integer("STUDIO_WEB_FETCH_TIMEOUT_S", 15, 2, 120)
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise RuntimeError("Only http(s) web pages can be read.")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = _resolve_public(parts.hostname, port)
        cls = _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
        conn = cls(parts.hostname, ip, port=port, timeout=timeout)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        try:
            conn.request("GET", path, headers={"User-Agent": user_agent(), "Accept-Encoding": "identity",
                                               "Accept": "text/html, text/plain, application/json;q=0.9, */*;q=0.5"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                url = urllib.parse.urljoin(url, resp.getheader("Location"))
                continue
            body = resp.read(limit)
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body, url
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise RuntimeError(f"Could not read {parts.hostname}.") from None
        finally:
            conn.close()
    raise RuntimeError("That page redirects too many times.")


_robots_lock = threading.Lock()
_robots = {}        # origin -> (parser, fetched_at)


def _allowed_by_robots(url):
    """robots.txt says StudioBot may read `url` (cached an hour per site). A
    site without one allows; one that refuses robots.txt itself disallows."""
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    with _robots_lock:
        cached = _robots.get(origin)
    if not cached or time.time() - cached[1] > settings.integer("STUDIO_WEB_ROBOTS_CACHE_S", 3600, 60, 86400):
        parser = urllib.robotparser.RobotFileParser()
        try:
            status, _, body, _ = _get(origin + "/robots.txt",
                                      timeout=settings.integer("STUDIO_WEB_ROBOTS_TIMEOUT_S", 8, 1, 60),
                                      limit=512 * 1024)
        except RuntimeError:
            status, body = 404, b""
        if status in (401, 403):
            parser.disallow_all = True
        elif status >= 400:
            parser.allow_all = True
        else:
            parser.parse(body.decode("utf-8", "replace").splitlines())
        cached = (parser, time.time())
        with _robots_lock:
            if len(_robots) > 500:
                _robots.clear()
            _robots[origin] = cached
    return cached[0].can_fetch(robots_agent(), url)


class _Text(HTMLParser):
    """Readable text of an HTML page: no scripts, styles or markup."""
    SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "head", "object"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header",
             "footer", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dd", "dt", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self._skip, self._in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)

    def text(self):
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines)).strip()


_READABLE = ("text/html", "application/xhtml+xml", "text/plain", "text/markdown", "text/csv",
             "application/json")


def fetch_page(url, max_chars=None):
    """{url, title, text} of one public web page, as text. RuntimeError with a
    fixed sentence when it cannot be read."""
    url = _http_url(url)
    max_chars = max_chars or page_chars()
    if not url:
        raise RuntimeError("Give a full http(s) address.")
    if not _allowed_by_robots(url):
        raise RuntimeError("That site's robots.txt does not allow reading this page.")
    status, headers, body, final = _get(url)
    if status >= 400:
        raise RuntimeError(f"That page answered HTTP {status}.")
    ctype = (headers.get("content-type") or "text/html").split(";")[0].strip().lower()
    if ctype not in _READABLE:
        raise RuntimeError(f"That page is {ctype}, which cannot be read as text.")
    charset = re.search(r"charset=([\w-]+)", headers.get("content-type") or "", re.I)
    try:
        decoded = body.decode(charset.group(1) if charset else "utf-8", "replace")
    except LookupError:
        decoded = body.decode("utf-8", "replace")
    if ctype in ("text/html", "application/xhtml+xml"):
        parser = _Text()
        parser.feed(decoded)
        title, text = re.sub(r"\s+", " ", parser.title).strip(), parser.text()
    else:
        title, text = "", decoded.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + f"\n… (the page continues; {len(text) - max_chars} more characters)"
    return {"url": final, "title": title or urllib.parse.urlsplit(final).hostname, "text": text}
