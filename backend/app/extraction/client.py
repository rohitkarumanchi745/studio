"""A thin authenticated Microsoft Graph REST client, shared unchanged by both
auth impls (application + delegated) so all of sync.py is written against one
type and is impl-agnostic.

Design invariants:
- FRESH bearer per call. Every request pulls its token from the bound GraphAuth,
  so a mid-run silent refresh (delegated) or a re-minted app token is transparent
  to the caller — the client never caches a token.
- BOUNDED, POLITE retries. 429 / 503 honor Retry-After (capped), otherwise an
  exponential backoff capped at _MAX_BACKOFF, for at most _MAX_RETRIES tries.
- ALWAYS bounded in time. Every request carries a timeout so a hung Graph call
  can never wedge the ticker thread.
- TOKENS NEVER LEAK. GraphError carries only the HTTP method, the query-stripped
  path, the status code, and a short slice of Graph's own error body — never the
  Authorization header, never a token, and never the query string (delta links
  carry a token param, so the path is truncated at '?').
- requests is imported lazily so `import app.extraction.client` never requires
  the wheel to be present at process start (dormancy).
"""
import os
import time

TIMEOUT = float(os.getenv("STUDIO_GRAPH_TIMEOUT", "30"))
_MAX_RETRIES = int(os.getenv("STUDIO_GRAPH_MAX_RETRIES", "4"))
_MAX_BACKOFF = float(os.getenv("STUDIO_GRAPH_MAX_BACKOFF", "20"))


class GraphError(Exception):
    """A Graph call failed. Its message is safe to log: it never contains a
    token, an Authorization header, or a query string."""


class GraphClient:
    def __init__(self, auth):
        self.auth = auth

    def _url(self, path):
        if path.startswith("http://") or path.startswith("https://"):
            return path                      # an absolute @odata.nextLink/deltaLink
        from . import GRAPH_BASE
        return GRAPH_BASE + path

    @staticmethod
    def _safe(path):
        """Strip any query string so a token-bearing delta URL never lands in an
        exception message or a log line."""
        return path.split("?", 1)[0]

    def _retry_after(self, resp, attempt):
        ra = resp.headers.get("Retry-After")
        if ra:
            try:
                return min(float(ra), _MAX_BACKOFF)
            except ValueError:
                pass
        return min(_MAX_BACKOFF, (2 ** attempt) * 0.5)

    def _do(self, method, path, params=None, json=None):
        import requests
        url = self._url(path)
        attempt = 0
        while True:
            attempt += 1
            headers = {"Authorization": f"Bearer {self.auth.access_token()}",
                       "Accept": "application/json"}
            try:
                resp = requests.request(method, url, headers=headers, params=params,
                                        json=json, timeout=TIMEOUT)
            except requests.RequestException as e:
                raise GraphError(f"Graph {method} {self._safe(path)} request failed: "
                                 f"{type(e).__name__}")
            if resp.status_code in (429, 503) and attempt <= _MAX_RETRIES:
                time.sleep(self._retry_after(resp, attempt))
                continue
            if resp.status_code >= 400:
                raise GraphError(f"Graph {method} {self._safe(path)} -> "
                                 f"{resp.status_code}: {resp.text[:200]}")
            return resp

    def get(self, path, params=None):
        resp = self._do("GET", path, params=params)
        try:
            return resp.json()
        except ValueError:
            return {}

    def get_binary(self, path):
        """Stream a file/attachment body for ingest. requests follows Graph's 302
        to the pre-authenticated download host and drops the Authorization header
        on the cross-host redirect."""
        resp = self._do("GET", path)
        return resp.content

    def post(self, path, json=None):
        resp = self._do("POST", path, json=json)
        try:
            return resp.json()
        except ValueError:
            return {}

    def delete(self, path):
        self._do("DELETE", path)
        return None
