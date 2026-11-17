"""Studio's own web tools: search (Grounding with Bing Search) and a page reader.

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

USER_AGENT = "StudioBot/1.0 (+web_fetch; reads pages an assistant cites)"
ROBOTS_AGENT = "StudioBot"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
_TOKEN_SCOPE = "https://ai.azure.com/.default"


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
    try:
        count = int(os.getenv("BING_COUNT", "7"))
    except ValueError:
        count = 7
    cfg.update(endpoint=cfg["endpoint"].rstrip("/"), market=os.getenv("BING_MARKET", "en-US").strip(),
               count=min(50, max(1, count)),
               authority=os.getenv("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/"))
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
            with urllib.request.urlopen(req, timeout=20) as r:
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


_PROMPT = ("Search the web for: {query}\n\nReport what the sources say that answers it — the key "
           "facts, numbers and dates — concisely. Do not add anything the sources do not say.")
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
    return {"text": "\n".join(t for t in texts if t).strip(), "citations": citations,
            "queries": [{"query": q, "url": bing_results_url(q)} for q in queries[:5]]}


def bing_search(query, freshness=None):
    """Search the web with Grounding with Bing Search. RuntimeError with a
    fixed sentence when it cannot."""
    cfg = bing_config()
    if cfg is None:
        raise RuntimeError("Bing search is not configured on this server.")
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:400]
    if not query:
        raise RuntimeError("Give a search query.")
    search = {"project_connection_id": cfg["connection"], "count": cfg["count"],
              "market": cfg["market"], "set_lang": cfg["market"].split("-")[0]}
    if freshness and re.fullmatch(r"(?i)day|week|month|\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?",
                                  str(freshness)):
        search["freshness"] = str(freshness).lower() if str(freshness).isalpha() else str(freshness)
    body = {"model": cfg["deployment"], "input": _PROMPT.format(query=query), "tool_choice": "required",
            "tools": [{"type": "bing_grounding", "bing_grounding": {"search_configurations": [search]}}]}
    req = urllib.request.Request(
        f"{cfg['endpoint']}/openai/v1/responses", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {_access_token(cfg)}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
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


def _get(url, timeout=15, limit=None):
    """(status, headers, body bytes, final url) for one GET, following up to
    five redirects, each checked again. The body stops at `limit` bytes."""
    limit = limit or MAX_PAGE_BYTES
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
            conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
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
    if not cached or time.time() - cached[1] > 3600:
        parser = urllib.robotparser.RobotFileParser()
        try:
            status, _, body, _ = _get(origin + "/robots.txt", timeout=8, limit=512 * 1024)
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
    return cached[0].can_fetch(ROBOTS_AGENT, url)


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


def fetch_page(url, max_chars=12000):
    """{url, title, text} of one public web page, as text. RuntimeError with a
    fixed sentence when it cannot be read."""
    url = _http_url(url)
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
