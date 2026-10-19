"""Read an Autopilot request out of a chat message.

"Every day show revenue by region", "alert me when revenue drops below 1M",
"whenever web_traffic gets new rows, summarize it" — a chat prompt that asks
for something to happen AGAIN, on a schedule or a condition, is a proposed
Autopilot agent, not a question to answer once. parse() returns the proposal
(trigger, its config, the goal and a name) or None; chat shows it as a card
the user checks and creates (autopilot.create_agent) — nothing is created
from words alone.

Deterministic on purpose: the same words always propose the same agent, and
an ordinary question ("what was revenue every day last week?") is never taken
for one — a schedule needs an imperative or a "send/show me" around it, a
threshold needs "alert/notify/tell me when", new data needs "when new rows…".
A leaf module: it imports nothing from the app.
"""
import re

_UNITS = {"minute": 60, "min": 60, "hour": 3600, "hr": 3600, "day": 86400, "week": 604800}
_EVERY = {"hour": 3600, "hourly": 3600, "day": 86400, "daily": 86400, "morning": 86400,
          "evening": 86400, "night": 86400, "weekday": 86400, "week": 604800,
          "weekly": 604800, "monday": 604800, "tuesday": 604800, "wednesday": 604800,
          "thursday": 604800, "friday": 604800, "saturday": 604800, "sunday": 604800}
_INTERVALS = (300, 900, 3600, 21600, 86400, 604800)

_OPS = [
    (r"drops? below|falls? below|goes below|dips? below|is below|is under|is less than|"
     r"gets? below|<", "lt"),
    (r"is at most|<=|≤", "le"),
    (r"goes above|rises above|exceeds|is above|is over|is more than|is greater than|"
     r"gets? above|tops|>", "gt"),
    (r"is at least|reaches|hits|>=|≥", "ge"),
    (r"equals|is exactly|=", "eq"),
]
_NUMBER = r"[-+]?\$?\d[\d,]*(?:\.\d+)?\s*(?:k|m|mm|b|bn|thousand|million|billion|%)?"

_NEGATED = re.compile(r"\b(?:don't|do not|never|stop|cancel|delete|remove|disable)\b")
# "daily"/"hourly"/"weekly" count only as an adverb ("send it daily"), never
# in front of a noun: "show daily revenue" asks for revenue per day.
_SCHEDULE = re.compile(
    r"\b(?:every|each)\s+(?:(?P<n>\d+)\s*(?P<unit>minute|min|hour|hr|day|week)s?|"
    r"(?P<word>hour|day|morning|evening|night|weekday|week|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday))\b|"
    r"\b(?P<adverb>hourly|daily|weekly)\b(?=\s*(?:$|[,.;!?]|at\b|basis\b|please\b))")
_TIME_OF_DAY = {"morning", "evening", "night", "weekday", "monday", "tuesday", "wednesday",
                "thursday", "friday", "saturday", "sunday"}
# What makes "every day" a request to repeat something rather than a breakdown
# ("orders each day"): a delivery, a report, or a word for watching over time.
_SCHEDULE_ASK = re.compile(
    r"\b(?:send|email|post|deliver|remind|notify|message)\s+(?:me|us|the team|it|them|this)\b|"
    r"\b(?:report|summary|digest|brief(?:ing)?|recap)\b|"
    r"\b(?:schedule|automate|autopilot|recurring|monitor|track|watch|keep (?:me|us) posted)\b")
_THRESHOLD = re.compile(
    r"\b(?:alert|notify|tell|warn|ping|email|message|let)\s+(?:me|us)(?:\s+know)?\s+"
    r"(?:when|if|whenever|once|as soon as)\s+(?:the\s+)?(?P<metric>.+?)\s+"
    r"(?P<op>" + "|".join(p for p, _ in _OPS) + r")\s*(?P<value>" + _NUMBER + r")")
_EVENT = re.compile(
    r"\b(?:when|whenever|each time|every time|as soon as)\s+(?:new\s+)?(?:data|rows?|records?)\s+"
    r"(?:arrives?|lands?|comes?\s+in|is\s+added|are\s+added|shows?\s+up)\s+(?:in|to|into|for)\s+"
    r"(?:the\s+)?(?P<table>[a-z_][a-z0-9_]*)|"
    r"\b(?:when|whenever|each time|every time|as soon as)\s+(?:the\s+)?(?P<table2>[a-z_][a-z0-9_]*)\s+"
    r"(?:gets|has|receives)\s+new\s+(?:data|rows?|records?)")


def number(text):
    """'1.5M' → 1500000.0, '$2,000' → 2000.0, '25%' → 25.0; None when not a number."""
    t = str(text or "").strip().lower().replace("$", "").replace(",", "")
    m = re.fullmatch(r"([-+]?\d*\.?\d+)\s*(k|m|mm|b|bn|thousand|million|billion|%)?", t)
    if not m:
        return None
    scale = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9,
             "bn": 1e9, "billion": 1e9}.get(m.group(2) or "", 1)
    return float(m.group(1)) * scale


def _interval(m):
    if m.group("n"):
        seconds = int(m.group("n")) * _UNITS[m.group("unit")]
    else:
        seconds = _EVERY[m.group("word") or m.group("adverb")]
    # The nearest cadence Autopilot offers, never under its 5-minute floor.
    return min(_INTERVALS, key=lambda s: abs(s - max(seconds, 300)))


def _clean(text):
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    text = re.sub(r"^(?:and|then|to|please|,)\s+", "", text, flags=re.I)
    return text.strip(" ,.;:-")


def _goal(text):
    """A goal from what is left of the prompt: no "send me a … report"
    wrapper, a capital letter."""
    text = _clean(text)
    text = re.sub(r"^(?:please\s+)?(?:send|email|post|deliver|give|show)\s+(?:me|us|the team)\s+"
                  r"(?:(?:a|an|the)\s+)?", "", text, flags=re.I)
    wrapped = re.search(r"\s+(?:report|summary|digest|recap|update)$", text, flags=re.I)
    if wrapped:
        text = "Summarize " + text[:wrapped.start()]
    return text[:1].upper() + text[1:] if text else text


def _name(goal):
    words = re.findall(r"[A-Za-z0-9%$]+", goal)[:5]
    return " ".join(words).capitalize() or "Autopilot agent"


def parse(prompt):
    """The Autopilot agent a chat message asks for, or None:
        {"trigger_type", "trigger_config", "goal", "name", "matched"}."""
    raw = re.sub(r"\s+", " ", str(prompt or "")).strip()
    text = raw.lower()
    if not text or _NEGATED.search(text):
        return None

    m = _THRESHOLD.search(text)
    if m:
        value = number(m.group("value"))
        op = next(code for pattern, code in _OPS if re.fullmatch(pattern, m.group("op")))
        if value is not None:
            metric = _clean(m.group("metric"))
            rest = _clean(raw[:m.start()] + " " + raw[m.end():])
            goal = f"Report {metric} and {rest[:1].lower() + rest[1:]}" if rest \
                else f"Report {metric} and explain what changed"
            return {"trigger_type": "threshold",
                    "trigger_config": {"metric_prompt": metric, "op": op, "value": value,
                                       "interval_seconds": 300},
                    "goal": goal, "name": _name(f"{metric} alert"), "matched": m.group(0)}

    m = _EVENT.search(text)
    if m:
        table = m.group("table") or m.group("table2")
        rest = _goal(raw[:m.start()] + " " + raw[m.end():])
        if rest and re.search(r"\b(?:it|them|these|those)\b", rest, flags=re.I):
            rest = f"{rest} — the new rows in {table}"
        goal = rest or f"Summarize the new rows in {table}"
        return {"trigger_type": "event",
                "trigger_config": {"table": table, "interval_seconds": 300},
                "goal": goal, "name": _name(f"New {table}"), "matched": m.group(0)}

    m = _SCHEDULE.search(text)
    lead = re.sub(r"^(?:please|,|\s)+", "", text)
    if m and (lead.startswith(m.group(0)) or (m.group("word") in _TIME_OF_DAY)
              or _SCHEDULE_ASK.search(text)):
        # "what was revenue every day last week?" asks about the past, not for a schedule.
        if re.search(r"\b(?:last|past|previous|yesterday|ago)\b", text) and \
                not re.search(r"\b(?:send|email|post|remind|alert|notify)\b", text):
            return None
        goal = _goal(raw[:m.start()] + " " + raw[m.end():])
        if not goal:
            return None
        return {"trigger_type": "schedule",
                "trigger_config": {"interval_seconds": _interval(m)},
                "goal": goal, "name": _name(goal), "matched": m.group(0)}
    return None
