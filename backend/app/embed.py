"""Pluggable text embeddings — Harrier (microsoft/harrier-oss-v1-0.6b).

Harrier is a decoder-only multilingual TEXT-EMBEDDING model (1024-dim, last-token
pooling + L2-norm, MIT). It's what powers semantic matching for the query cache
and BitNet's learned scope: lexical token-Jaccard (the fallback) catches re-asks
and word-order/plural variation but misses paraphrases that use *different words*
for the same idea ("revenue by product" vs "sales per item"). Harrier cosine
similarity catches those.

Two Harrier specifics this honours:
- **Queries need an instruction prefix** ("...required for queries, otherwise you
  will see a performance degradation"); stored documents do not. So an incoming
  prompt is embedded as a QUERY (instruction-prefixed) and matched against stored
  patterns embedded as DOCUMENTS (plain) — the standard instruct-retrieval setup.
- It's served over HTTP (TEI / a sentence-transformers wrapper exposing
  /embeddings), so no torch in Studio's lean API image.

Opt-in and safe: with HARRIER_EMBED_URL unset, or if the endpoint is unreachable,
embed() returns None and callers fall back to the lexical signature — nothing
changes until Harrier is wired up.
"""
import json
import math
import os
import urllib.request

# The task instruction Harrier prepends to queries (documents stay plain).
_DEFAULT_INSTRUCT = "Retrieve analytics questions that ask for the same thing"


def available():
    return bool(os.getenv("HARRIER_EMBED_URL", "").strip())


def embed(text, kind="query"):
    """Embed one string via the Harrier endpoint (OpenAI-compatible /embeddings).
    kind='query' prepends the required instruction; kind='document' sends plain
    text. Returns a 1024-dim vector, or None on any failure (→ lexical fallback)."""
    url = os.getenv("HARRIER_EMBED_URL", "").strip()
    if not url or not (text or "").strip():
        return None
    text = text[:8000]
    if kind == "query":
        instruct = os.getenv("HARRIER_EMBED_INSTRUCT", _DEFAULT_INSTRUCT)
        text = f"Instruct: {instruct}\nQuery: {text}"
    model = os.getenv("HARRIER_EMBED_MODEL", "microsoft/harrier-oss-v1-0.6b")
    key = os.getenv("HARRIER_EMBED_KEY", "")
    body = json.dumps({"model": model, "input": text}).encode()
    req = urllib.request.Request(
        url.rstrip("/") + "/embeddings", data=body,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        vec = data["data"][0]["embedding"]
        return vec if isinstance(vec, list) and vec else None
    except Exception:
        return None


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
