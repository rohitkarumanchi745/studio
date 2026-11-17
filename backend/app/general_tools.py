"""Tools any Studio agent can use, whatever the question: the clock and the web.

Studio's agents answer questions about your data, and anything else asked in
the same chat: the time somewhere, a public fact, what a page says. The clock
is computed here from the IANA time-zone database, daylight saving included.

The web — Bing, Google, Brave or Claude's own search, picked per chat — is
described below ("The web").

A search query leaves for the web, so agents are told never to put the
organization's data (names, figures, identifiers from its databases or
documents) into one.
"""
import datetime as dt
import os
import re

from . import settings
from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# Names people use for a place or zone that is not itself a zone's city.
_ALIASES = {
    "utc": "UTC", "gmt": "UTC", "z": "UTC", "zulu": "UTC", "universal": "UTC",
    # North America
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York",
    "eastern": "America/New_York", "eastern time": "America/New_York",
    "nyc": "America/New_York", "new york city": "America/New_York", "manhattan": "America/New_York",
    "boston": "America/New_York", "washington": "America/New_York", "washington dc": "America/New_York",
    "dc": "America/New_York", "philadelphia": "America/New_York", "atlanta": "America/New_York",
    "miami": "America/New_York", "charlotte": "America/New_York",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago",
    "central": "America/Chicago", "central time": "America/Chicago", "dallas": "America/Chicago",
    "houston": "America/Chicago", "austin": "America/Chicago", "minneapolis": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
    "mountain": "America/Denver", "mountain time": "America/Denver", "salt lake city": "America/Denver",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles", "pacific time": "America/Los_Angeles",
    "la": "America/Los_Angeles", "san francisco": "America/Los_Angeles", "sf": "America/Los_Angeles",
    "bay area": "America/Los_Angeles", "silicon valley": "America/Los_Angeles",
    "san jose": "America/Los_Angeles", "seattle": "America/Los_Angeles",
    "san diego": "America/Los_Angeles", "las vegas": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    # Europe, Asia, Oceania
    "uk": "Europe/London", "bst": "Europe/London", "england": "Europe/London",
    "cet": "Europe/Paris", "cest": "Europe/Paris", "france": "Europe/Paris",
    "germany": "Europe/Berlin", "spain": "Europe/Madrid", "italy": "Europe/Rome",
    "netherlands": "Europe/Amsterdam", "ireland": "Europe/Dublin",
    "ist": "Asia/Kolkata", "india": "Asia/Kolkata", "bangalore": "Asia/Kolkata",
    "bengaluru": "Asia/Kolkata", "mumbai": "Asia/Kolkata", "bombay": "Asia/Kolkata",
    "delhi": "Asia/Kolkata", "new delhi": "Asia/Kolkata", "hyderabad": "Asia/Kolkata",
    "chennai": "Asia/Kolkata", "pune": "Asia/Kolkata", "kolkata": "Asia/Kolkata",
    "calcutta": "Asia/Kolkata", "vijayawada": "Asia/Kolkata",
    "jst": "Asia/Tokyo", "japan": "Asia/Tokyo", "osaka": "Asia/Tokyo",
    "kst": "Asia/Seoul", "korea": "Asia/Seoul", "south korea": "Asia/Seoul",
    "china": "Asia/Shanghai", "beijing": "Asia/Shanghai", "shenzhen": "Asia/Shanghai",
    "sgt": "Asia/Singapore", "uae": "Asia/Dubai", "abu dhabi": "Asia/Dubai",
    "aest": "Australia/Sydney", "aedt": "Australia/Sydney", "australia": "Australia/Sydney",
    "nz": "Pacific/Auckland", "new zealand": "Pacific/Auckland",
}
# Regions whose zones are places (not "US/Eastern" or "Etc/GMT+5" aliases).
_REGIONS = ("America/", "Europe/", "Asia/", "Africa/", "Australia/", "Pacific/", "Atlantic/",
            "Indian/", "Antarctica/")
_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$")


def _aliases():
    """The built-in nicknames, plus STUDIO_CLOCK_ALIASES ("hq=America/Chicago;
    pune office=Asia/Kolkata") — an entry naming an unknown zone is ignored."""
    extra = {}
    for item in os.getenv("STUDIO_CLOCK_ALIASES", "").split(";"):
        name, _, zone = item.partition("=")
        name, zone = re.sub(r"\s+", " ", name.strip().lower()), zone.strip()
        if name and zone in available_timezones():
            extra[name] = zone
    return {**_ALIASES, **extra}


@lru_cache(maxsize=1)
def _zones():
    """{lowercase zone: zone} and {lowercase city: zone}, places first."""
    names = sorted(available_timezones())
    by_name = {n.lower(): n for n in names}
    by_city = {}
    for n in sorted(names, key=lambda z: (not z.startswith(_REGIONS), z)):
        if n.startswith("Etc/") or "/" not in n:
            continue        # "Etc/GMT-3" is UTC+3 (POSIX signs): never a name to match
        city = n.rsplit("/", 1)[-1].replace("_", " ").lower()
        by_city.setdefault(city, n)
    return by_name, by_city


def resolve(place):
    """(tzinfo, name) for a city, a zone, an abbreviation or an offset, or
    (None, None). "New York" → America/New_York, "PST" → America/Los_Angeles,
    "UTC+5:30" → a fixed offset."""
    text = re.sub(r"\s+", " ", str(place or "").strip().strip(".?!")).lower()
    text = re.sub(r"^(?:the )?(?:time )?(?:in |at )?", "", text) or "utc"
    m = _OFFSET.match(text.replace(" ", ""))
    if m:                    # an offset says its sign as people write it: GMT-3 is UTC-3
        if int(m.group(2)) > 14 or int(m.group(3) or 0) >= 60:
            return None, None
        sign = 1 if m.group(1) == "+" else -1
        delta = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        label = f"UTC{m.group(1)}{int(m.group(2)):02d}:{int(m.group(3) or 0):02d}"
        return dt.timezone(sign * delta, label), label
    by_name, by_city = _zones()
    name = (_aliases().get(text) or by_name.get(text.replace(" ", "_"))
            or by_city.get(text) or by_city.get(text.split(",")[0].strip()))
    if name and not name.startswith("Etc/"):
        try:
            return ZoneInfo(name), name
        except ZoneInfoNotFoundError:
            return None, None
    return None, None


def describe_time(place="UTC", now=None):
    """The current date and time at `place`, as one line an agent can quote."""
    tz, name = resolve(place)
    if tz is None:
        return (f"I don't know a time zone for “{place}”. Give a city, an IANA zone such as "
                "America/New_York, or an offset such as UTC+5:30.")
    moment = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    raw = moment.strftime("%z")
    offset = f"UTC{raw[0]}{raw[1:3]}:{raw[3:]}"
    abbrev = moment.tzname() or ""
    parts = (abbrev if abbrev[:1].isalpha() else "", offset, name)
    where = ", ".join(dict.fromkeys(p for p in parts if p))
    return (f"{moment:%A, %d %B %Y, %H:%M} ({moment:%I:%M %p}) — {where}. "
            f"ISO: {moment.isoformat(timespec='seconds')}")


def clock_tool(emit=None):
    """A LangChain tool for the date and time anywhere. `emit(label)` records
    the step in the activity feed."""
    from langchain_core.tools import tool

    @tool
    def current_time(place: str = "UTC") -> str:
        """The current date and time in a place: a city ("New York", "Bengaluru"),
        an IANA time zone ("Europe/London"), an abbreviation ("PST") or an offset
        ("UTC+5:30"). Use it for today's date, the time anywhere, or how long
        until or since a date. Never guess the date or time.

        Args:
            place: Where. Defaults to UTC.
        """
        _, name = resolve(place)
        if emit:
            emit(f"checking the time in {name or place}")
        return describe_time(place)

    return current_time


# ── The web ─────────────────────────────────────────────────────────────
#
# Providers (web_search.py), each used the way its terms allow:
#   verbatim — Bing, Google: their grounded answer IS the answer, shown as it
#              comes back with its references; the search tool ends the turn
#              (return_direct), so no model rewrites or adds to it;
#   results  — Brave, Claude's own web search: results the agent writes its
#              own answer from, citing them.
# STUDIO_WEB_SEARCH is the server's: "off", "auto" (any configured provider,
# the person picks in the chat — default the first of provider_order()), or one
# provider only. Each chat turn says what its person picked: "off", "auto" or
# a provider. Studio's page reader (web_fetch) comes with the web whenever it
# is on, except beside Claude's own search, which has its own.

PROVIDERS = {
    "bing": {"label": "Bing", "verbatim": True, "note": "From a Bing web search"},
    "google": {"label": "Google", "verbatim": True, "note": "From Google Search"},
    "brave": {"label": "Brave", "verbatim": False, "note": "From a Brave web search"},
    "anthropic": {"label": "Claude web search", "verbatim": False, "note": ""},
}
DEFAULT_ORDER = ("bing", "google", "brave", "anthropic")
CHOICES = ("off", "auto") + DEFAULT_ORDER


def provider_order():
    """The order "Auto" tries providers: STUDIO_WEB_ORDER first, then the rest."""
    first = settings.names("STUDIO_WEB_ORDER", DEFAULT_ORDER, allowed=PROVIDERS)
    return tuple(first + [p for p in DEFAULT_ORDER if p not in first])
# Grounded output none of these may train on (their terms), nor cache.
NO_TRAIN = ("bing", "google", "brave")

# Models that take the dynamic-filtering web tools (web_*_20260209); older
# Claude models take the basic ones. STUDIO_ANTHROPIC_WEB_CURRENT_MODELS
# (model-name prefixes) replaces this list when a new model comes out.
_CURRENT_WEB = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8",
                "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6")


def _current_web_models():
    return tuple(settings.names("STUDIO_ANTHROPIC_WEB_CURRENT_MODELS", _CURRENT_WEB))


def _web_setting():
    value = os.getenv("STUDIO_WEB_SEARCH", "auto").strip().lower()
    if value in ("off", "0", "false", "no"):
        return "off"
    return value if value in PROVIDERS else "auto"


def web_enabled(choice=None):
    """The web is on for this turn: the server allows it and its person did
    not switch it off."""
    return _web_setting() != "off" and choice != "off"


def _claude(spec):
    return str(spec or "").partition(":")[0] == "anthropic"


def provider_ready(pid, spec=None):
    """Configured on this server (and, for Claude's own search, a Claude model)."""
    from . import web_search as web
    if pid == "bing":
        return bool(web.bing_config())
    if pid == "google":
        return bool(web.google_config())
    if pid == "brave":
        return bool(web.brave_config())
    if pid == "anthropic":
        return _claude(spec) if spec is not None else bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    return False


def offered():
    """What the chat's web toggle offers: [{id, label, verbatim}] the server
    allows and has configured (Claude's search: when a Claude key is set)."""
    setting = _web_setting()
    if setting == "off":
        return []
    ids = (setting,) if setting in PROVIDERS else provider_order()
    return [{"id": pid, **PROVIDERS[pid]} for pid in ids if provider_ready(pid)]


def web_provider(spec, choice=None):
    """The search this turn uses: a provider id, or None (then the page reader
    alone, unless the web is off)."""
    if not web_enabled(choice):
        return None
    setting = _web_setting()
    allowed = (setting,) if setting in PROVIDERS else provider_order()
    if choice in PROVIDERS:
        return choice if choice in allowed and provider_ready(choice, spec) else None
    return next((pid for pid in allowed if provider_ready(pid, spec)), None)


def _max_uses():
    return settings.integer("STUDIO_WEB_MAX_USES", 3, 1, 10)


def web_tools(spec, choice=None):
    """Anthropic's web search and web fetch for a Claude model, as server-tool
    definitions — only when Claude's own search is this turn's — else []."""
    provider, _, model = str(spec or "").partition(":")
    if web_provider(spec, choice) != "anthropic":
        return []
    current = model.lower().startswith(_current_web_models())
    uses = _max_uses()
    return [{"type": "web_search_20260209" if current else "web_search_20250305",
             "name": "web_search", "max_uses": uses},
            {"type": "web_fetch_20260209" if current else "web_fetch_20250910",
             "name": "web_fetch", "max_uses": uses}]


def _merge(record, got):
    for c in got.get("citations") or []:
        if all(x["url"] != c["url"] for x in record["citations"]):
            record["citations"].append(c)
    for q in got.get("queries") or []:
        if q not in record["queries"]:
            record["queries"].append(q)


def client_web_tools(spec, record, emit=None, choice=None):
    """Studio's own web tools for this turn: the chosen provider's web_search
    and Studio's page reader (web_fetch); the reader alone when no search is
    available; none when Claude's own search is in use (same names) or the
    web is off. `record` collects what they used: citations, queries, and —
    for Bing and Google — the grounded answer itself ("direct")."""
    from langchain_core.tools import tool
    from . import web_search as web
    provider = web_provider(spec, choice)
    if provider == "anthropic" or not web_enabled(choice):
        return []
    say = emit or (lambda label: None)
    tools = []

    if provider in ("bing", "google"):
        label = PROVIDERS[provider]["label"]

        @tool(return_direct=True)
        def web_search(question: str) -> str:
            """Answer the question from the web. The answer comes back grounded in
            web pages and is shown to the person exactly as it is, with its
            sources — it ends your turn. Call it only when the question needs
            the web, pass the person's whole question, and write no answer of
            your own. Never put names, figures or identifiers from this
            organization's databases or documents into it.

            Args:
                question: The person's question, in full.
            """
            say(f"searched {label} for “{str(question)[:120]}”")
            try:
                got = (web.bing_search(question) if provider == "bing"
                       else web.google_search(question))
            except RuntimeError as e:
                record["direct"] = {"provider": provider, "text": f"I couldn't search the web: {e}",
                                    "failed": True}
                return record["direct"]["text"]
            _merge(record, got)
            record["direct"] = {"provider": provider, "text": got["text"],
                                "suggestions_html": got.get("suggestions_html", "")}
            return got["text"]
        tools.append(web_search)
    elif provider == "brave":
        @tool
        def web_search(query: str) -> str:
            """Search the web (Brave) for current events, live facts and anything
            you are unsure of. Returns numbered results — title, link, snippet:
            answer from them and cite the ones you use. Never put names, figures
            or identifiers from this organization's databases or documents into
            the query.

            Args:
                query: What to search for, in plain words.
            """
            say(f"searched Brave for “{str(query)[:120]}”")
            try:
                got = web.brave_search(query)
            except RuntimeError as e:
                return f"SEARCH FAILED: {e}"
            _merge(record, {"citations": [{"url": r["url"], "title": r["title"]}
                                          for r in got["results"]],
                            "queries": got["queries"]})
            return web.format_brave(got)
        tools.append(web_search)

    @tool
    def web_fetch(url: str) -> str:
        """Read a public web page as text: one the person gave, or one a search
        found. Use it for details a search leaves out.

        Args:
            url: The page's full http(s) address.
        """
        say(f"read {str(url)[:160]}")
        try:
            page = web.fetch_page(url)
        except RuntimeError as e:
            return f"COULD NOT READ THE PAGE: {e}"
        record.setdefault("read", [])
        if all(x["url"] != page["url"] for x in record["read"]):
            record["read"].append({"url": page["url"], "title": page["title"]})
        return f"{page['title']}\n{page['url']}\n\n{page['text']}"
    tools.append(web_fetch)
    return tools


def grounding(record, provider, asked_by=None):
    """(citations, web_grounding) for what an answer's own web tools used.

    citations: the pages, as {source, url, kind: "web"} — the ones a Bing or
    Google answer gave, the Brave results the agent had, and pages it read.
    web_grounding names the provider (for its references, and to keep the
    answer out of training), its queries, whether the answer is that
    provider's own (verbatim), who asked (Google answers are theirs alone),
    and Google's Search Suggestions."""
    used = list(record.get("citations") or [])
    for page in record.get("read") or []:
        if all(x["url"] != page["url"] for x in used):
            used.append(page)
    pages = [{"source": c.get("title") or urlsplit(c["url"]).hostname, "url": c["url"], "kind": "web"}
             for c in used if _web_url(c.get("url"))]
    direct = record.get("direct")
    if provider not in NO_TRAIN or not (record.get("queries") or record.get("citations") or direct):
        return pages, None
    from . import web_search as web
    out = {"provider": provider, "queries": list(record.get("queries") or [])[:web.max_sources()],
           "verbatim": bool(direct and not direct.get("failed")),
           "label": PROVIDERS[provider]["label"], "note": PROVIDERS[provider]["note"]}
    if asked_by:
        out["asked_by"] = asked_by
    if direct and direct.get("suggestions_html"):
        out["suggestions_html"] = direct["suggestions_html"]
    return pages, out


WEB_RULE = ("Use web_search for current events, live facts and anything you are unsure "
            "of, and web_fetch to read a page whose URL is in the conversation; say which "
            "sites you used. Never put names, figures or identifiers from this "
            "organization's databases or documents into a search — search only for "
            "public information.")
VERBATIM_RULE = ("For a question that needs the web (current events, live facts), call "
                 "web_search with the person's whole question: its grounded answer is shown "
                 "as it is and ends your turn, so do not answer it yourself, and do not "
                 "combine it with data. web_fetch reads a page whose URL is in the "
                 "conversation. Never put names, figures or identifiers from this "
                 "organization's databases or documents into a search.")
FETCH_ONLY_RULE = ("You cannot search the web, but web_fetch reads a public page whose "
                   "address is in the conversation. For other live information (news, "
                   "weather, prices), say so plainly and give what you know, with its date.")
NO_WEB_RULE = ("You have no web access: for live information (news, weather, prices), say so "
               "plainly and give what you know, with its date.")


def web_rule(spec, choice=None):
    """The rule matching the web tools this turn gets."""
    provider = web_provider(spec, choice)
    if provider:
        return VERBATIM_RULE if PROVIDERS[provider]["verbatim"] else WEB_RULE
    return FETCH_ONLY_RULE if web_enabled(choice) else NO_WEB_RULE


def _web_url(url):
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    return parts.geturl() if parts.scheme in ("http", "https") and parts.hostname else None


def _blocks(messages):
    """Content blocks of this turn's AI messages (after the last human one)."""
    start = 0
    for i, m in enumerate(messages or []):
        if getattr(m, "type", "") == "human":
            start = i + 1
    for m in (messages or [])[start:]:
        if getattr(m, "type", "") == "ai" and isinstance(m.content, list):
            yield from (b for b in m.content if isinstance(b, dict))


def web_activity(messages, limit=None):
    """(steps, citations) for what the server tools did this turn: the
    searches and reads as activity steps, and the pages used as citations
    ({source, url, kind: "web"}) — the ones the answer cites, else the ones
    the searches found. Only http(s) links."""
    steps, cited, found = [], {}, {}
    for b in _blocks(messages):
        kind = b.get("type")
        if kind == "server_tool_use":
            args = b.get("input") or {}
            if b.get("name") == "web_search" and args.get("query"):
                steps.append(f"searched the web for “{str(args['query'])[:120]}”")
            elif b.get("name") == "web_fetch" and _web_url(args.get("url")):
                steps.append(f"read {_web_url(args['url'])[:160]}")
        elif kind == "web_search_tool_result" and isinstance(b.get("content"), list):
            for r in b["content"]:
                url = _web_url((r or {}).get("url"))
                if url:
                    found.setdefault(url, (r.get("title") or "").strip())
        elif kind == "web_fetch_tool_result" and isinstance(b.get("content"), dict):
            url = _web_url(b["content"].get("url"))
            if url:
                doc = b["content"].get("content") or {}
                found.setdefault(url, (doc.get("title") or "").strip())
        elif kind == "text":
            for c in b.get("citations") or []:
                url = _web_url((c or {}).get("url"))
                if url:
                    cited.setdefault(url, (c.get("title") or "").strip())
    pages = cited or found
    from . import web_search as web
    citations = [{"source": title or urlsplit(url).hostname, "url": url, "kind": "web"}
                 for url, title in list(pages.items())[:limit or web.max_sources()]]
    return steps, citations
