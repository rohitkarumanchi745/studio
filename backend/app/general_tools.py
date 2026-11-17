"""Tools any Studio agent can use, whatever the question: the clock and, on
Claude models, the web.

Studio's agents answer questions about your data, and anything else asked in
the same chat: the time somewhere, a public fact, what a page says. The clock
is computed here from the IANA time-zone database, daylight saving included.

The web, by STUDIO_WEB_SEARCH (default "auto"):
    bing       Studio's own web_search (Grounding with Bing Search, through
               Azure AI Foundry) and web_fetch (our page reader), for any model
               — web_search.py;
    anthropic  Anthropic's server tools, which Anthropic runs inside the model
               call — Claude models only;
    auto       bing when it is configured, else anthropic for a Claude model;
               any other model still gets web_fetch, to read a page it is given;
    off        no web at all.

A search query leaves for the web, so agents are told never to put the
organization's data (names, figures, identifiers from its databases or
documents) into one.
"""
import datetime as dt
import os
import re
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
    name = (_ALIASES.get(text) or by_name.get(text.replace(" ", "_"))
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


# ── The web (Anthropic server tools) ───────────────────────────────────

# Models that take the dynamic-filtering web tools (web_*_20260209); older
# Claude models take the basic ones.
_CURRENT_WEB = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8",
                "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6")


def _web_setting():
    value = os.getenv("STUDIO_WEB_SEARCH", "auto").strip().lower()
    return "off" if value in ("off", "0", "false", "no") else value


def web_enabled():
    return _web_setting() != "off"


def web_provider(spec):
    """Which web search a model gets: "bing", "anthropic", or None (then only
    the page reader, unless the web is off)."""
    from . import web_search
    setting = _web_setting()
    if setting == "off":
        return None
    claude = str(spec or "").partition(":")[0] == "anthropic"
    if setting in ("auto", "on", "bing") and web_search.bing_config():
        return "bing"
    if setting in ("auto", "on", "anthropic") and claude:
        return "anthropic"
    return None


def _max_uses():
    try:
        return min(10, max(1, int(os.getenv("STUDIO_WEB_MAX_USES", "3"))))
    except ValueError:
        return 3


def web_tools(spec):
    """Anthropic's web search and web fetch for a Claude model, as server-tool
    definitions, or [] (Bing is in use, another provider, or switched off)."""
    provider, _, model = str(spec or "").partition(":")
    if web_provider(spec) != "anthropic":
        return []
    current = model.startswith(_CURRENT_WEB)
    uses = _max_uses()
    return [{"type": "web_search_20260209" if current else "web_search_20250305",
             "name": "web_search", "max_uses": uses},
            {"type": "web_fetch_20260209" if current else "web_fetch_20250910",
             "name": "web_fetch", "max_uses": uses}]


def client_web_tools(spec, record, emit=None):
    """Studio's own web tools for this model: web_search (Bing) and web_fetch
    when Bing is configured; web_fetch alone when no search is available; none
    when Anthropic's server tools are in use (same names) or the web is off.
    `record` collects what they used: {"citations": [...], "queries": [...]}."""
    from langchain_core.tools import tool
    from . import web_search as web
    provider = web_provider(spec)
    if provider == "anthropic" or not web_enabled():
        return []
    say = emit or (lambda label: None)

    @tool
    def web_search(query: str, freshness: str = "") -> str:
        """Search the web (Bing) for current events, live facts and anything you
        are unsure of. Returns a summary grounded in web pages, with numbered
        sources: cite the ones you use. Never put names, figures or identifiers
        from this organization's databases or documents into the query.

        Args:
            query: What to search for, in plain words.
            freshness: Optional: Day, Week, Month, or a date range
                YYYY-MM-DD..YYYY-MM-DD.
        """
        say(f"searched Bing for “{str(query)[:120]}”")
        try:
            got = web.bing_search(query, freshness or None)
        except RuntimeError as e:
            return f"SEARCH FAILED: {e}"
        for c in got["citations"]:
            if all(x["url"] != c["url"] for x in record["citations"]):
                record["citations"].append(c)
        for q in got["queries"]:
            if q not in record["queries"]:
                record["queries"].append(q)
        return web.format_results(got)

    @tool
    def web_fetch(url: str) -> str:
        """Read a public web page as text: one the user gave, or one a search
        found. Use it for details a search summary leaves out.

        Args:
            url: The page's full http(s) address.
        """
        say(f"read {str(url)[:160]}")
        try:
            page = web.fetch_page(url)
        except RuntimeError as e:
            return f"COULD NOT READ THE PAGE: {e}"
        if all(x["url"] != page["url"] for x in record["citations"]):
            record["citations"].append({"url": page["url"], "title": page["title"]})
        return f"{page['title']}\n{page['url']}\n\n{page['text']}"

    return [web_search, web_fetch] if provider == "bing" else [web_fetch]


def grounding(record, provider):
    """What an answer's own web tools used: (citations, web_grounding) —
    citations as {source, url, kind: "web"}; web_grounding names Bing and its
    queries, which the terms require to be shown, and keeps the answer out of
    training."""
    pages = [{"source": c.get("title") or urlsplit(c["url"]).hostname, "url": c["url"], "kind": "web"}
             for c in record["citations"] if _web_url(c.get("url"))]
    if provider == "bing" and (record["queries"] or pages):
        return pages, {"provider": "bing", "queries": record["queries"][:10]}
    return pages, None


WEB_RULE = ("Use web_search for current events, live facts and anything you are unsure "
            "of, and web_fetch to read a page whose URL is in the conversation; say which "
            "sites you used. Never put names, figures or identifiers from this "
            "organization's databases or documents into a search — search only for "
            "public information.")
FETCH_ONLY_RULE = ("You cannot search the web, but web_fetch reads a public page whose "
                   "address is in the conversation. For other live information (news, "
                   "weather, prices), say so plainly and give what you know, with its date.")


def web_rule(spec):
    """The rule matching the web tools this model gets."""
    if web_provider(spec):
        return WEB_RULE
    return FETCH_ONLY_RULE if web_enabled() else NO_WEB_RULE


NO_WEB_RULE = ("You have no web access: for live information (news, weather, prices), say so "
               "plainly and give what you know, with its date.")


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


def web_activity(messages, limit=8):
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
    citations = [{"source": title or urlsplit(url).hostname, "url": url, "kind": "web"}
                 for url, title in list(pages.items())[:limit]]
    return steps, citations
