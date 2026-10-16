"""Custom visuals — the agent writes a D3 visualization as code (an artifact).

When no chart type fits what the user asked for — a radial timeline, an
annotated story, small multiples, "an interactive D3 view of …" — the agent can
write one: the body of a JavaScript function ``draw(d3, data, el, size)``. The
browser runs it in a sandboxed iframe (frontend renderers/ArtifactView.jsx):
scripts only, an opaque origin with no access to Studio's page, cookies, storage
or login token, no navigation, and a Content-Security-Policy with no network.
The panel's own rows — already through the gateway's RBAC and governance — are
the data it draws.

The sandbox and its CSP are the security boundary. lint() here is defense in
depth: it turns the usual mistakes (a fetch, data written as HTML, reaching for
the parent window) into a retry with feedback, so a visual does not fail in the
sandbox or leave a reviewer second-guessing it. It is not relied on as a filter.

Only a sample of the rows reaches the model — the preview the chat agent
already sees — never the full result; the code runs over the full rows in the
browser.
"""
import json
import re

MAX_CODE_CHARS = 20_000
SAMPLE_ROWS = 30

# Mirrors frontend renderers/common.js, so a custom visual reads as part of the
# same system as the built-in charts.
_SERIES = ["#3987e5", "#199e70", "#c98500", "#008300", "#9085e9", "#e66767", "#d55181", "#d95926"]
_SEQ = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]

PROMPT = f"""You write ONE custom data visualization with D3 v7, as code.

Reply with ONLY a JSON object:
{{"title": "<short title>", "code": "<JavaScript>", "explanation": "<one or two sentences: what it shows and how to read it>"}}

"code" is the BODY of:   function draw(d3, data, el, size) {{ <code> }}
- d3 is the whole D3 v7 library. data is an array of row objects keyed by column
  name (numbers may arrive as strings; values may be null). el is an empty <div>
  filling the frame. size is {{width, height}} in pixels.
- Draw into el only, sized to size.width x size.height. draw may be called again
  on resize: build everything from scratch each time.
- Write every data value with .text() — never .html(), innerHTML or
  insertAdjacentHTML.
- There is no network and no other code: no fetch, XMLHttpRequest, WebSocket,
  import(), eval or new Function; no window.parent, top or opener; no cookies or
  storage. The sandbox blocks all of them and the visual would fail.
- Interaction is welcome: tooltips (an absolutely positioned <div> filled with
  .text()), hover, zoom, brush, drag, transitions.
- Colors: categories {", ".join(_SERIES)}; magnitude ramp {", ".join(_SEQ)}.
  Text #4a4844, muted text #79766f, gridlines #eceae7, background white,
  font 11-12px.
- Under 250 lines. No external data, images or fonts.
"""

# Mistakes worth a retry, as (pattern, what to say). Bare `parent.` / `top.` /
# `opener.` only: d3 hierarchy nodes legitimately have `d.parent`.
_LINT = [
    (r"\bfetch\s*\(", "fetch() — the sandbox has no network"),
    (r"\bXMLHttpRequest\b", "XMLHttpRequest — the sandbox has no network"),
    (r"\b(?:WebSocket|EventSource|sendBeacon|RTCPeerConnection|WebTransport)\b",
     "a network API — the sandbox has no network"),
    (r"\bimport\s*\(", "a dynamic import — d3 is already loaded"),
    (r"\beval\s*\(|\bnew\s+Function\b|(?<![\w$.])Function\s*\(", "eval / new Function"),
    (r"\bwindow\s*\.\s*(?:parent|top|opener)\b|(?<![\w$.])(?:parent|top|opener)\s*\.",
     "the parent window"),
    (r"\bdocument\s*\.\s*cookie\b|\b(?:localStorage|sessionStorage|indexedDB)\b",
     "cookies or storage"),
    (r"\b(?:innerHTML|outerHTML|insertAdjacentHTML)\b|\.html\s*\(|\bdocument\s*\.\s*write",
     "writing HTML — use .text()"),
    (r"\bpostMessage\b", "postMessage"),
]


class VisualError(ValueError):
    """The model could not produce a usable visual."""


def lint(code):
    """The rule each problem in `code` breaks, as short phrases. [] = clean."""
    return [why for pattern, why in _LINT if re.search(pattern, code or "")]


def _kind(values):
    present = [v for v in values if v not in (None, "")]
    if not present:
        return "empty"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "number"
    if all(isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v) for v in present):
        return "date"
    return "text"


def column_types(columns, rows):
    """{column: number | date | text | empty}, read off the sample rows."""
    sample = rows[:SAMPLE_ROWS]
    return {c: _kind([r[i] if i < len(r) else None for r in sample])
            for i, c in enumerate(columns)}


def _text(reply):
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


def parse(text):
    """{"title", "code", "explanation"} from a model reply, or VisualError."""
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise VisualError("the reply held no JSON object")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise VisualError(f"the reply was not valid JSON ({e.msg})")
    if not isinstance(obj, dict):
        raise VisualError("the reply was not a JSON object")
    code = obj.get("code")
    if not isinstance(code, str) or not code.strip():
        raise VisualError("the reply had no code")
    if len(code) > MAX_CODE_CHARS:
        raise VisualError(f"the code is over {MAX_CODE_CHARS:,} characters")
    return {"title": str(obj.get("title") or "Custom visual").strip()[:120],
            "code": code,
            "explanation": str(obj.get("explanation") or "").strip()[:600]}


def generate(llm, columns, rows, request, title=""):
    """Ask `llm` for a visual of this result. One retry, with the reason, when
    the reply is unusable or the lint finds a problem; then VisualError."""
    payload = json.dumps({
        "request": request,
        "title_hint": title,
        "columns": columns,
        "column_types": column_types(columns, rows),
        "row_count": len(rows),
        "sample_rows": rows[:SAMPLE_ROWS],
    }, default=str)
    feedback = ""
    last = "no reply"
    for _ in range(2):
        reply = llm.invoke([("system", PROMPT), ("user", payload + feedback)])
        try:
            out = parse(_text(reply))
        except VisualError as e:
            last = str(e)
            feedback = f"\n\nYour previous reply was not usable: {last}. Reply with the JSON object only."
            continue
        problems = lint(out["code"])
        if not problems:
            return out
        last = "the code used " + "; ".join(problems)
        feedback = (f"\n\nYour previous code was rejected — it used {'; '.join(problems)}. "
                    "Rewrite it without those.")
    raise VisualError(last)
