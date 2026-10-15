"""Power BI semantic models (datasets) as a Studio source.

One source is ONE semantic model in ONE workspace, read with a service
principal: an Entra ID app registration using the client-credentials grant for
https://analysis.windows.net/powerbi/api/.default. The source's tables are the
model's tables and its columns are the model's columns, under SQL-friendly names
— 'Sales Order Lines'[Order Date] is sales_order_lines.order_date — because the
agent writes SQL, and every name it has to quote is a name it can misquote.

Read path. Power BI answers DAX, not SQL, so this connector follows the model
of the marketing connectors (connectors/marketing.py): copy the tables a query
reads into an in-memory SQLite database and run the SQL there, so the agent,
the query guard, governance and the charts work unchanged. Two rules keep that
safe and honest:

- WHAT is copied comes from queryguard — base_tables() for the tables, the
  token stream for the columns — i.e. the guard's own reading of the exact SQL
  RBAC just approved. Only the columns the SQL names are fetched (SELECTCOLUMNS),
  which keeps a wide table's payload small; a `*` or a NATURAL join, which read
  columns without naming them, fetch every column.
- A copy is whole or it is refused. Aggregating a truncated table returns a
  wrong number that looks right, so a table over POWERBI_MAX_ROWS (or over the
  Power BI API's own per-query cap) raises instead. The exception is a bare
  `SELECT <columns> FROM t LIMIT n` — the shape of every preview — where ANY n
  rows answer the query, so only n are asked for (TOPN) and the cap truncates
  it like the row cap on any other source's result.

Two transports, one result. executeDaxQueries (Apache Arrow; reads INFO.VIEW.*
metadata; up to POWERBI_MAX_ROWS rows) when pyarrow is installed and the tenant
serves it; otherwise — or when it fails in a way the JSON API might not share —
executeQueries (JSON; COLUMNSTATISTICS() metadata; at most 100,000 rows or
1,000,000 values per query). POWERBI_API=arrow|json pins one.

Deliberately not covered yet:
- Measures. A DAX measure is a calculation evaluated in a filter context, not
  a column, and SQL over a copied table cannot evaluate one. The model's
  columns are here; its measures are not.
- Row-level security. Power BI refuses executeQueries from a service principal
  on a model with RLS or SSO. Studio's own RBAC and governance are the access
  boundary for this source, exactly as they are for a warehouse credential.

Metadata (tables, columns, types) is cached per connector for
POWERBI_SCHEMA_TTL seconds — list_tables() runs on every gateway call — and a
failed refresh keeps serving the last good copy for a short grace period rather
than failing every query on one throttled call. That widens nothing: rows still
come only from a live read with the app's current permissions.

Secrets: the client secret and access tokens are never logged and never put in
an error message. Entra's own error text is kept (the first line only: its
AADSTS code names the fix and never echoes a secret).
"""
import collections
import datetime
import hashlib
import io
import os
import re
import sqlite3
import threading
import time
import unicodedata

import requests

from .. import limits, queryguard, util
from .base import Connector, to_jsonable

_API = "https://api.powerbi.com/v1.0/myorg"
_LOGIN = "https://login.microsoftonline.com"
_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

_GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
# A directory id or a verified domain. Checked because it is spliced into the
# token URL's path.
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
# The auto date/time tables Power BI Desktop generates for every date column:
# hidden plumbing, not data anyone asks about.
_AUTO_DATE = re.compile(r"^(?:LocalDateTable|DateTableTemplate)_[0-9a-fA-F-]{36}$")
# ISO-8601 as the JSON API serializes a DateTime ("2024-01-31T00:00:00").
_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")

_JSON_MAX_ROWS = 100_000            # executeQueries: rows per query…
_JSON_MAX_VALUES = 1_000_000        # …or cells, whichever comes first
_REFRESH_LEEWAY = 120.0             # re-mint a token this long before it expires
_ARROW_PARK_S = 600.0               # after Arrow fails and JSON works, stay on JSON this long
_STALE_GRACE_S = 30.0               # serve the last good metadata this long after a failed refresh
_BROWSE_WORKSPACES = 200            # the picker is a picker, not a tenant inventory

#: Names a SQL identifier must not take bare: SQLite's keywords plus the words
#: the query guard refuses as writes (queryguard.FORBIDDEN). A model column
#: called "Order" or "Set" becomes order_ / set_, not a query that fails to parse
#: or is rejected as a write.
_RESERVED = frozenset("""
abort action add after all alter always analyze and as asc attach autoincrement
before begin between by call cascade case cast check collate column commit
conflict constraint copy create cross current current_date current_time
current_timestamp database default deferrable deferred delete desc detach
distinct do drop each else end escape except exclude exclusive execute exists
explain export fail filter first following for foreign from full generated
glob grant group groups having if ignore immediate import in index indexed
initially inner insert install instead intersect into is isnull join key last
left like limit load match materialized merge natural no not nothing notnull
null nulls of offset on or order others outer over partition plan pragma
preceding primary query raise range recursive references regexp reindex
release rename replace restrict returning revoke right rollback row rows
savepoint select set table temp temporary then ties to transaction trigger
truncate unbounded union unique update using vacuum values view virtual when
where window with without
""".split())


def _int_env(name, default, low=1):
    try:
        return max(low, int(os.getenv(name, "") or default))
    except ValueError:
        return default


def _max_rows():
    """The most rows one table copy may hold (POWERBI_MAX_ROWS)."""
    return _int_env("POWERBI_MAX_ROWS", 250_000)


def _schema_ttl():
    return _int_env("POWERBI_SCHEMA_TTL", 600, low=0)


def _timeout():
    return _int_env("POWERBI_TIMEOUT_S", 120)


def _api_mode():
    mode = (os.getenv("POWERBI_API") or "auto").strip().lower()
    return mode if mode in ("auto", "arrow", "json") else "auto"


_ARROW = None


def _has_arrow():
    """pyarrow is what reads executeDaxQueries' responses. Checked once — a
    failed import is retried on every call otherwise."""
    global _ARROW
    if _ARROW is None:
        try:
            import pyarrow  # noqa: F401
            import pyarrow.ipc  # noqa: F401
            _ARROW = True
        except ImportError:
            _ARROW = False
    return _ARROW


class PowerBIError(RuntimeError):
    """A Power BI call failed. `fatal` marks a failure the other transport would
    hit too (credentials, permissions, rate limit, size, network), so the JSON
    fallback is skipped — it could only double the wait."""

    def __init__(self, message, fatal=False):
        super().__init__(message)
        self.fatal = fatal


class TooLarge(PowerBIError):
    """A table bigger than one copy may be. Always fatal: neither transport can
    answer from part of a table."""

    def __init__(self, message):
        super().__init__(message, fatal=True)


def _request(method, url, **kw):
    """The one HTTP call site, so a test can stand in for Power BI."""
    return requests.request(method, url, **kw)


# ── Names ───────────────────────────────────────────────────────────────

def _sql_name(name, prefix):
    """'Sales Order Lines' -> sales_order_lines, 'CustomerKey' -> customer_key."""
    s = str(name).replace("ß", "ss")                       # NFKD has no decomposition for it
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)       # SKUCode -> SKU_Code
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)          # orderDate -> order_Date
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    if not s:                                              # nothing ASCII to keep
        return f"{prefix}_{hashlib.sha1(str(name).encode()).hexdigest()[:6]}"
    if s[0].isdigit():
        s = f"{prefix}_{s}"
    return s + "_" if s in _RESERVED else s


def _sql_names(names, prefix):
    """{original: SQL name}. A SQL name two originals would share goes to
    NEITHER — each gets a short stable hash suffix. Giving the bare name to the
    first in some order would let a column added to the model later silently
    take over a name saved SQL already uses; this way that SQL fails loudly."""
    base = {n: _sql_name(n, prefix) for n in names}
    counts = collections.Counter(base.values())
    return {n: b if counts[b] == 1 else f"{b}_{hashlib.sha1(str(n).encode()).hexdigest()[:6]}"
            for n, b in base.items()}


def _dax_table(name):
    return "'" + name.replace("'", "''") + "'"


def _dax_column(name):
    return "[" + name.replace("]", "]]") + "]"


def _dax_string(text):
    return '"' + text.replace('"', '""') + '"'


def _short(key):
    """A result column's own name: 'Sales[Amount]' -> 'Amount', '[n]' -> 'n'.
    Power BI qualifies a model column and brackets a computed one."""
    k = str(key)
    if k.endswith("]") and "[" in k:
        return k[k.rfind("[") + 1:-1]
    return k


# ── Types and values ────────────────────────────────────────────────────

def _temporal_type(declared, fmt):
    """DATE or TIMESTAMP. The model often stores both as DateTime, so the
    format string decides when the type cannot: 'Long Date' or 'yyyy-mm-dd' carry
    no time of day; 'General Date' or 'h:nn' do."""
    if declared == "date":
        return "DATE"
    f = str(fmt or "").strip().lower()
    if f in ("long date", "short date", "medium date"):
        return "DATE"
    if not f or "general" in f or "time" in f or re.search(r"[hns]|am/pm|tt", f):
        return "TIMESTAMP"
    return "DATE"


def _declared_type(datatype, fmt=None):
    """SQL type from the model's declared data type. INFO.VIEW reports friendly
    names ("Text", "Whole Number", "Date") or TOM's ("String", "Int64",
    "DateTime"); both are read. None means skip the column: a binary column
    (an image) gives SQL nothing to do and is the heaviest thing to copy."""
    t = str(datatype or "").strip().lower()
    if "binary" in t:
        return None
    if "bool" in t or "true" in t:
        return "BOOLEAN"
    if "date" in t or "time" in t:
        return _temporal_type(t, fmt)
    if "int" in t or "whole" in t:
        return "INTEGER"
    if any(k in t for k in ("decimal", "double", "currency", "fixed", "number", "float", "real")):
        return "REAL"
    return "TEXT"


def _split_when(value):
    """(YYYY-MM-DD, HH:MM:SS) for a date/datetime value or an ISO string, else None."""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat(), value.strftime("%H:%M:%S")
    if isinstance(value, datetime.date):
        return value.isoformat(), "00:00:00"
    if isinstance(value, str):
        m = _ISO.match(value.strip())
        if m:
            return m.group(1), f"{m.group(2) or '00'}:{m.group(3) or '00'}:{m.group(4) or '00'}"
    return None


def _inferred_type(low, high, max_length):
    """SQL type from COLUMNSTATISTICS(), which reports values, not types: Min and
    Max are typed values, and Max Length is set only for text."""
    if max_length not in (None, ""):
        return "TEXT"
    values = [v for v in (low, high) if v not in (None, "")]
    if not values:
        return "TEXT"
    if all(isinstance(v, bool) for v in values):
        return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
        return "INTEGER"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return "REAL"
    whens = [_split_when(v) if isinstance(v, str) else None for v in values]
    if all(whens):
        return "DATE" if all(w[1] == "00:00:00" for w in whens) else "TIMESTAMP"
    return "TEXT"


def _cell(value, kind):
    """A Power BI value as SQLite stores it. Dates become ISO text — what
    SQLite's date functions read — as YYYY-MM-DD for a DATE column and
    YYYY-MM-DD HH:MM:SS for a TIMESTAMP; a DATE value that does carry a time
    keeps it rather than lose information. Decimals become numbers."""
    if value is None:
        return None
    if kind in ("DATE", "TIMESTAMP"):
        parts = _split_when(value)
        if parts is None:
            return to_jsonable(value)
        day, clock = parts
        return day if kind == "DATE" and clock == "00:00:00" else f"{day} {clock}"
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    return to_jsonable(value)


def _truthy(value):
    return value is True or str(value).strip().lower() in ("true", "1")


# ── Results ─────────────────────────────────────────────────────────────

def _error_text(obj):
    """The readable part of a Power BI error body, which nests its message in
    different places per API: error.message, error['pbi.error'].details[]
    .detail.value, or only a code."""
    if not obj:
        return ""
    if not isinstance(obj, dict):
        return str(obj)[:400]
    err = obj.get("error", obj)
    if not isinstance(err, dict):
        return str(err)[:400]
    parts = [err.get("message")]
    pbi = err.get("pbi.error") or {}
    for d in pbi.get("details") or []:
        if isinstance(d, dict):
            detail = d.get("detail")
            parts.append(detail.get("value") if isinstance(detail, dict) else detail)
    text = " — ".join(str(p) for p in parts if p)
    return (text or str(err.get("code") or pbi.get("code") or ""))[:400]


def _query_error(message):
    """A query-level failure. Power BI reports an over-cap result as an error
    alongside the partial rows (HTTP 200) — that one is a size refusal."""
    text = str(message or "query failed")[:500]
    if re.search(r"more than .{0,40}rows|row count limit|rowcountlimit", text, re.IGNORECASE):
        return TooLarge(f"Power BI refused the read as too large: {text}")
    return PowerBIError(f"Power BI query error: {text}")


def _arrow_iter(table, picks=None):
    """Rows of a pyarrow Table as tuples, one record batch at a time, so a large
    copy is never every row as Python objects at once."""
    picks = range(table.num_columns) if picks is None else picks
    for batch in table.to_batches():
        columns = [batch.column(i).to_pylist() for i in picks]
        yield from zip(*columns)


def _records(table):
    """A metadata table as dicts keyed by lower-cased short column names."""
    names = [_short(n).lower() for n in table.column_names]
    return [dict(zip(names, row)) for row in _arrow_iter(table)]


def _read_arrow(content):
    """One pyarrow Table per EVALUATE from executeDaxQueries' body, which is
    Arrow IPC streams laid end to end. An error arrives as HTTP 200 with a
    stream whose schema metadata says IsError=true; it is raised, never read as
    an empty result."""
    import pyarrow as pa
    import pyarrow.ipc  # noqa: F401

    content = content or b""
    buf, out = io.BytesIO(content), []
    while buf.tell() < len(content):
        try:
            reader = pa.ipc.open_stream(buf)
        except pa.ArrowInvalid:
            break
        table = reader.read_all()
        meta = {k.decode("utf-8", "replace"): v.decode("utf-8", "replace")
                for k, v in (reader.schema.metadata or {}).items()}
        if meta.get("IsError", "").lower() == "true":
            message = meta.get("FaultString")
            if not message:
                rows = _records(table)
                message = rows[0].get("errormessage") if rows else None
            raise _query_error(message)
        out.append(table)
    if content and not out:
        raise PowerBIError("Power BI returned a response that is not an Arrow stream")
    return out


def _json_body(response):
    try:
        return response.json() or {}
    except ValueError:
        raise PowerBIError(f"Power BI returned a non-JSON response ({response.status_code})")


# ── HTTP and auth ───────────────────────────────────────────────────────

_TOKENS = {}
_TOKENS_LOCK = threading.Lock()

_HINTS = {
    401: ("Power BI rejected the app's token. In the admin portal's Developer "
          "settings, allow service principals to use Power BI / Fabric APIs for a "
          "security group that contains this app"),
    403: ("Power BI denied access. Add the app to the workspace as Contributor, Member "
          "or Admin (or Viewer plus Build permission on the semantic model), and check "
          "that the 'Semantic model (dataset) Execute Queries REST API' tenant setting "
          "is on"),
    404: ("Power BI could not find it — check the workspace and semantic model, and "
          "that both are shared with the app"),
    429: ("Power BI's query rate limit was hit (120 requests a minute per identity, "
          "40 on Pro/PPU capacity) — try again shortly"),
}


def _token(cfg):
    """A Power BI access token for the app, minted with the client-credentials
    grant and cached until shortly before it expires."""
    tenant = cfg["tenant_id"]
    if not _TENANT.match(tenant):
        raise PowerBIError("The tenant must be a directory ID (GUID) or a domain such "
                           "as contoso.onmicrosoft.com", fatal=True)
    # The secret is part of the key (hashed): two connections sharing an app id
    # must not vouch for each other's secret.
    key = (tenant.lower(), cfg["client_id"],
           hashlib.sha256(cfg["client_secret"].encode()).hexdigest())
    now = time.time()
    with _TOKENS_LOCK:
        hit = _TOKENS.get(key)
    if hit and hit[1] - _REFRESH_LEEWAY > now:
        return hit[0]
    try:
        r = _request("POST", f"{_LOGIN}/{tenant}/oauth2/v2.0/token", timeout=30, data={
            "grant_type": "client_credentials", "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"], "scope": _SCOPE})
    except requests.RequestException as e:
        raise PowerBIError(f"Could not reach Entra ID: {type(e).__name__}", fatal=True)
    if r.status_code != 200:
        try:
            desc = str((r.json() or {}).get("error_description") or "").strip()
        except ValueError:
            desc = ""
        first = desc.splitlines()[0][:240] if desc else ""
        raise PowerBIError(f"Entra ID refused the app credentials ({r.status_code})"
                           + (f": {first}" if first else ""), fatal=True)
    data = _json_body(r)
    access = data.get("access_token")
    if not access:
        raise PowerBIError("Entra ID returned no access token", fatal=True)
    with _TOKENS_LOCK:
        _TOKENS[key] = (access, now + float(data.get("expires_in") or 3600))
    return access


def _one(items, name, noun):
    """The one item called `name`, exact spelling first, then ignoring case.
    Power BI allows two semantic models with one name in a workspace; guessing
    between them would read the wrong one."""
    for found in ([i for i in items if i.get("name") == name],
                  [i for i in items if str(i.get("name") or "").lower() == name.lower()]):
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise PowerBIError(f"{len(found)} {noun}s are named '{name}' — use the "
                               f"{noun}'s ID instead", fatal=True)
    raise PowerBIError(f"No {noun} named '{name}' is shared with this app", fatal=True)


# ── Planning a read ─────────────────────────────────────────────────────

def _limit_only(toks):
    """n when the query is `SELECT <list> FROM <table> [[AS] alias] LIMIT n
    [OFFSET m]` (or SQLite's `LIMIT m, n`) and the list has no call, subquery
    or DISTINCT: then ANY n (+ m) rows of the table answer it. None otherwise —
    an aggregate, a filter, a sort or a join needs the whole table."""
    def word(i, *values):
        return i < len(toks) and toks[i][0] == "word" and toks[i][1].lower() in values

    def number(i):
        return i < len(toks) and toks[i][0] == "num" and toks[i][1].isdigit()

    if not word(0, "select"):
        return None
    start = next((i for i in range(len(toks)) if word(i, "from")), None)
    if start is None or any(t == ("punct", "(") or (t[0] == "word" and t[1].lower() == "distinct")
                            for t in toks[1:start]):
        return None
    i = start + 1
    if i >= len(toks) or toks[i][0] not in ("word", "ident"):
        return None
    i += 1
    if word(i, "as"):
        i += 1
    if i < len(toks) and toks[i][0] in ("word", "ident") and not word(i, "limit"):
        i += 1                                                  # the alias
    if not (word(i, "limit") and number(i + 1)):
        return None
    n, i = int(toks[i + 1][1]), i + 2
    if i < len(toks) and toks[i] == ("punct", ",") and number(i + 1):
        n, i = n + int(toks[i + 1][1]), i + 2                  # LIMIT offset, count
    elif word(i, "offset") and number(i + 1):
        n, i = n + int(toks[i + 1][1]), i + 2
    return n if i == len(toks) else None


def _plan(sql):
    """(tables, words, star, limit) — what a query needs copied.

    tables  the base tables it reads (queryguard.base_tables, the guard's reading)
    words   every identifier it mentions, lower-cased; a column not among them is
            never referenced, so it is not fetched
    star    a `*` select-list item or a NATURAL join, which read columns without
            naming them; then every column is fetched. COUNT(*) is not one.
    limit   see _limit_only
    """
    toks = queryguard._tokens((sql or "").strip().rstrip(";").strip())[0]
    words = {text.lower() for kind, text in toks if kind in ("word", "ident")}
    opens_list = {("punct", ","), ("punct", "."), ("word", "select"), ("word", "distinct"),
                  ("word", "all")}
    star = "natural" in words or any(
        tok == ("punct", "*") and i > 0 and (toks[i - 1][0], toks[i - 1][1].lower()) in opens_list
        for i, tok in enumerate(toks))
    return queryguard.base_tables(sql), words, star, _limit_only(toks)


def _strict(con):
    """Make an unknown double-quoted name an error. SQLite's legacy fallback
    reads "Order Date" — the model's own spelling, which these tables do not
    use — as the string 'Order Date', so a misquoted column would come back as
    a column of identical strings instead of failing."""
    flag = getattr(sqlite3, "SQLITE_DBCONFIG_DQS_DML", None)
    if flag is not None and hasattr(con, "setconfig"):
        con.setconfig(flag, False)


def _too_large(table, cap, rows=None, json_api=False):
    size = f"{rows:,} rows" if rows is not None else f"more than {cap:,} rows"
    if json_api:
        why = (f"over the {cap:,} rows Power BI's executeQueries API returns in one read "
               "(the executeDaxQueries path reads up to POWERBI_MAX_ROWS — it needs "
               "pyarrow and the tenant's XMLA endpoint setting)")
    else:
        why = f"over POWERBI_MAX_ROWS ({cap:,})"
    return TooLarge(
        f"Power BI table '{table['dax']}' has {size}, {why}. Studio copies a Power BI "
        "table whole before running SQL over it and won't answer from part of one: "
        "ask about a smaller table, raise the limit, or land this model in a "
        "warehouse source.")


class PowerBIConnector(Connector):
    name = "powerbi"
    # The SQL runs in SQLite over the copied tables. No qualifiers(): the copies
    # have bare names in a throwaway database, so the base class's empty set is
    # right — a qualified reference names nothing here and the guard refuses it.
    dialect = "sqlite"

    def __init__(self):
        self._lock = threading.Lock()
        self._refresh = threading.Lock()
        self._meta = None               # {"key", "model", "expires"}
        self._ids_hit = None            # (key, (workspace_id, dataset_id), expires)
        self._arrow_parked_until = 0.0

    # ── Config ──────────────────────────────────────────────────────────

    def _cfg(self):
        """The credential and the pinned model. The tenant falls back to the SSO
        tenant (AZURE_TENANT_ID) — it is the same directory. The app should
        not: Power BI permissions belong on their own app registration."""
        raw = {
            "tenant_id": os.getenv("POWERBI_TENANT_ID") or os.getenv("AZURE_TENANT_ID"),
            "client_id": os.getenv("POWERBI_CLIENT_ID"),
            "client_secret": os.getenv("POWERBI_CLIENT_SECRET"),
            "workspace": os.getenv("POWERBI_WORKSPACE"),
            "dataset": os.getenv("POWERBI_DATASET"),
        }
        return {k: (v or "").strip() for k, v in raw.items()}

    def configured(self):
        """The credential AND the semantic model it reads: a source with no model
        pinned has no tables to offer. The connect screen does not ask this
        before a model is picked — it signs in through list_namespaces(), which
        needs only the credential."""
        c = self._cfg()
        return all(c[k] for k in ("tenant_id", "client_id", "client_secret",
                                  "workspace", "dataset"))

    # ── HTTP ────────────────────────────────────────────────────────────

    def _api(self, method, path, *, retry=True, **kw):
        headers = {"Authorization": f"Bearer {_token(self._cfg())}"}
        try:
            r = _request(method, _API + path, headers=headers, timeout=_timeout(), **kw)
        except requests.RequestException as e:
            raise PowerBIError(f"Could not reach Power BI: {type(e).__name__}", fatal=True)
        if r.status_code == 429 and retry:
            wait = str(r.headers.get("Retry-After") or "2").strip()
            if wait.isdigit() and int(wait) <= 10:
                time.sleep(int(wait))
                return self._api(method, path, retry=False, **kw)
        if r.status_code >= 400:
            try:
                detail = _error_text(r.json())
            except ValueError:
                detail = ""
            hint = _HINTS.get(r.status_code, "")
            raise PowerBIError(
                (f"Power BI API error {r.status_code}" + (f": {hint}" if hint else "")
                 + (f" ({detail})" if detail else ""))[:700],
                fatal=r.status_code in (401, 403, 429))
        return r

    def _workspaces(self):
        rows = _json_body(self._api("GET", "/groups", params={"$top": 5000})).get("value") or []
        return [{"id": w["id"], "name": str(w.get("name") or w["id"]).strip()}
                for w in rows if _GUID.match(str(w.get("id") or ""))]

    def _datasets(self, workspace_id):
        if not _GUID.match(workspace_id):
            raise PowerBIError(f"'{workspace_id}' is not a workspace ID")
        rows = _json_body(self._api("GET", f"/groups/{workspace_id}/datasets")).get("value") or []
        return [d for d in rows if _GUID.match(str(d.get("id") or ""))]

    def _ids(self):
        """(workspace_id, dataset_id) of the pinned model. A name is resolved to
        its ID once per schema TTL; an ID is used as written."""
        cfg = self._cfg()
        workspace, dataset = cfg["workspace"], cfg["dataset"]
        if not dataset:
            raise PowerBIError("No semantic model is pinned to this source", fatal=True)
        if not workspace:
            raise PowerBIError("Name the workspace that holds this semantic model — a "
                               "service principal has no My workspace", fatal=True)
        key = (workspace, dataset)
        with self._lock:
            hit = self._ids_hit
        if hit and hit[0] == key and hit[2] > time.time():
            return hit[1]
        ws_id = workspace if _GUID.match(workspace) else _one(
            self._workspaces(), workspace, "workspace")["id"]
        ds_id = dataset if _GUID.match(dataset) else _one(
            self._datasets(ws_id), dataset, "semantic model")["id"]
        with self._lock:
            self._ids_hit = (key, (ws_id, ds_id), time.time() + max(_schema_ttl(), 60))
        return ws_id, ds_id

    # ── Transports ──────────────────────────────────────────────────────

    def _arrow(self, query, row_limit):
        """executeDaxQueries: [pyarrow.Table] — one per EVALUATE."""
        ws, ds = self._ids()
        r = self._api("POST", f"/groups/{ws}/datasets/{ds}/executeDaxQueries",
                      json={"query": query, "resultSetRowCountLimit": row_limit})
        if "json" in str(r.headers.get("Content-Type") or "").lower():
            raise _query_error(_error_text(_json_body(r)) or "unexpected JSON response")
        return _read_arrow(r.content)

    def _json(self, query):
        """executeQueries: the rows (dicts) of the query's one table. An error at
        any level — including the over-cap one that rides along with partial
        rows on an HTTP 200 — is raised."""
        ws, ds = self._ids()
        data = _json_body(self._api(
            "POST", f"/groups/{ws}/datasets/{ds}/executeQueries",
            json={"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}))
        result = (data.get("results") or [{}])[0] or {}
        table = (result.get("tables") or [{}])[0] or {}
        error = data.get("error") or result.get("error") or table.get("error")
        if error:
            raise _query_error(_error_text(error) or "query failed")
        return table.get("rows") or []

    def _arrow_ok(self):
        mode = _api_mode()
        if mode == "json" or not _has_arrow():
            return False
        return mode == "arrow" or time.time() >= self._arrow_parked_until

    def _either(self, arrow, json_):
        """`arrow()` when executeDaxQueries is usable, else `json_()`. In auto mode
        an Arrow failure the JSON API might not share — an endpoint this tenant
        doesn't serve, a model it can't read — falls back to JSON and parks Arrow
        for a while. Fatal failures (credential, permission, rate limit, size)
        are raised as they are: JSON would hit them too."""
        if not self._arrow_ok():
            return json_()
        try:
            return arrow()
        except PowerBIError as e:
            if e.fatal or _api_mode() == "arrow":
                raise
            arrow_error = e
        try:
            out = json_()
        except PowerBIError as e:
            raise PowerBIError(f"{e} (executeDaxQueries failed too: {arrow_error})",
                               fatal=e.fatal) from e
        self._arrow_parked_until = time.time() + _ARROW_PARK_S
        return out

    # ── Metadata ────────────────────────────────────────────────────────

    def _discover_arrow(self):
        found = self._arrow("EVALUATE INFO.VIEW.TABLES()\nEVALUATE INFO.VIEW.COLUMNS()", 100_000)
        if len(found) < 2:
            raise PowerBIError("Power BI returned no model metadata")
        tables = {}
        for t in _records(found[0]):
            name = t.get("name")
            if (not name or _AUTO_DATE.match(name) or _truthy(t.get("isprivate"))
                    or _truthy(t.get("showasvariationonly"))
                    or t.get("calculationgroupprecedence") not in (None, "")):
                continue                    # date plumbing, private, calculation groups
            tables[name] = []
        for c in _records(found[1]):
            table, name = c.get("table"), c.get("name")
            if table not in tables or not name or str(name).startswith("RowNumber-"):
                continue
            if "rownumber" in (str(c.get("type") or "").lower(),
                               str(c.get("datacategory") or "").lower()):
                continue
            kind = _declared_type(c.get("datatype"), c.get("formatstring"))
            if kind:
                tables[table].append((str(name), kind))
        return _build_model(tables)

    def _discover_json(self):
        tables = collections.defaultdict(list)
        for row in self._json("EVALUATE COLUMNSTATISTICS()"):
            rec = {_short(k).lower(): v for k, v in row.items()}
            table, name = rec.get("table name"), rec.get("column name")
            if not table or not name or _AUTO_DATE.match(str(table)) \
                    or str(name).startswith("RowNumber-"):
                continue
            tables[str(table)].append(
                (str(name), _inferred_type(rec.get("min"), rec.get("max"), rec.get("max length"))))
        return _build_model(tables)

    def _model(self):
        """{sql table: {"dax", "columns": [{"sql", "dax", "type"}]}} for the pinned
        model, cached for POWERBI_SCHEMA_TTL seconds."""
        cfg = self._cfg()
        key = (cfg["tenant_id"], cfg["client_id"], cfg["workspace"], cfg["dataset"])
        hit = self._meta
        if hit and hit["key"] == key and hit["expires"] > time.time():
            return hit["model"]
        with self._refresh:
            hit = self._meta
            if hit and hit["key"] == key and hit["expires"] > time.time():
                return hit["model"]
            try:
                model = self._either(self._discover_arrow, self._discover_json)
            except PowerBIError:
                if hit and hit["key"] == key:
                    self._meta = {**hit, "expires": time.time() + _STALE_GRACE_S}
                    return hit["model"]
                raise
            self._meta = {"key": key, "model": model, "expires": time.time() + _schema_ttl()}
            return model

    # ── Connector interface ─────────────────────────────────────────────

    def list_tables(self):
        return sorted(self._model())

    def get_schema(self, table):
        t = self._model().get(str(table).lower())
        if t is None:
            raise ValueError(f"Unknown table {table}")
        return [{"name": c["sql"], "type": c["type"]} for c in t["columns"]]

    def list_namespaces(self):
        """Every semantic model the app can see, as {"database": workspace,
        "schema": model} — names where they are unambiguous (they read better
        in the picker and in source names), IDs where two share a name."""
        every = self._workspaces()
        spaces = every[:_BROWSE_WORKSPACES]
        # Read by connections._browse, which tells the picker the list is partial.
        self.namespaces_truncated = len(every) > len(spaces)
        found = util.pmap(lambda w: self._datasets(w["id"]), spaces, workers=8, default=[])
        ws_names = collections.Counter(w["name"] for w in spaces)
        out = []
        for ws, sets in sorted(zip(spaces, found), key=lambda p: p[0]["name"].lower()):
            unique_ws = ws_names[ws["name"]] == 1 and not _GUID.match(ws["name"])
            ws_label = ws["name"] if unique_ws else ws["id"]
            names = collections.Counter(d.get("name") for d in sets)
            for d in sorted(sets, key=lambda d: str(d.get("name") or "").lower()):
                name = str(d.get("name") or "")
                unique = name and names[d.get("name")] == 1 and not _GUID.match(name)
                out.append({"database": ws_label, "schema": name if unique else d["id"]})
        return out

    def _fetch(self, table, cols, limit=None):
        """Rows of `cols` from one model table, in that order: the WHOLE table or
        TooLarge — unless `limit` (a bare LIMIT query) says any n rows do."""
        dax_t = _dax_table(table["dax"])
        picks = ",\n        ".join(f"{_dax_string(c['sql'])}, {dax_t}{_dax_column(c['dax'])}"
                                    for c in cols)
        expr = f"SELECTCOLUMNS(\n        {dax_t},\n        {picks}\n    )"
        order = [c["sql"] for c in cols]
        cap = _max_rows()

        def arrow():
            n = min(limit, cap) if limit is not None else cap
            body = expr if limit is None else f"TOPN({n}, {expr})"
            # One row over the cap is asked for, so a table that is too big
            # shows itself instead of arriving truncated at exactly the cap.
            found = self._arrow(f"EVALUATE\n    {body}", n + 1)
            if not found:
                raise PowerBIError(f"Power BI returned no result for '{table['dax']}'")
            t = found[0]
            if limit is None and t.num_rows > cap:
                raise _too_large(table, cap)
            index = {_short(name).lower(): i for i, name in enumerate(t.column_names)}
            missing = [c for c in order if c not in index]
            if missing:
                raise PowerBIError(f"Power BI's result lacks {', '.join(missing)}")
            return _arrow_iter(t, [index[c] for c in order])

        def json_():
            per_read = min(cap, _JSON_MAX_ROWS, _JSON_MAX_VALUES // max(1, len(cols)))
            if limit is not None:
                rows = self._json(f"EVALUATE\n    TOPN({min(limit, per_read)}, {expr})")
            else:
                count = self._json(f'EVALUATE ROW("n", COUNTROWS({dax_t}))')
                n = int((list(count[0].values()) or [0])[0] or 0) if count else 0
                if n > per_read:
                    raise _too_large(table, per_read, n, json_api=per_read < cap)
                rows = self._json(f"EVALUATE\n    {expr}")
                if len(rows) != n:
                    raise PowerBIError(
                        f"Power BI returned {len(rows):,} of {n:,} rows of '{table['dax']}' "
                        "(its 15 MB per-query cap, or a refresh between reads) — Studio "
                        "won't answer from part of a table", fatal=True)
            if not rows:
                return iter(())
            keys = {_short(k).lower(): k for k in rows[0]}
            missing = [c for c in order if c not in keys]
            if missing:
                raise PowerBIError(f"Power BI's result lacks {', '.join(missing)}")
            ks = [keys[c] for c in order]
            return ([r.get(k) for k in ks] for r in rows)

        return self._either(arrow, json_)

    def run_query(self, sql):
        model = self._model()
        tables, words, star, limit = _plan(sql)
        con = sqlite3.connect(":memory:")
        try:
            _strict(con)
            for name in sorted(tables & model.keys()):
                table = model[name]
                cols = table["columns"] if star else (
                    [c for c in table["columns"] if c["sql"] in words] or table["columns"][:1])
                kinds = [c["type"] for c in cols]
                rows = self._fetch(table, cols, limit)
                names = ", ".join(f'"{c["sql"]}"' for c in cols)
                con.execute(f'CREATE TABLE "{name}" ({names})')
                con.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(cols))})',
                                ([_cell(v, k) for v, k in zip(row, kinds)] for row in rows))
            cur = con.execute(sql)
            columns = [d[0] for d in cur.description or ()]
            out = [list(r) for r in cur.fetchmany(limits.MAX_ROWS)]
        finally:
            con.close()
        return columns, out


def _build_model(tables):
    """The model map from {dax table: [(dax column, sql type)]}. A table with no
    readable column is left out: there is nothing for SQL to select."""
    tables = {t: cols for t, cols in tables.items() if cols}
    table_names = _sql_names(tables, "t")
    model = {}
    for dax_table, cols in tables.items():
        seen, unique = set(), []
        for col, kind in cols:
            if col not in seen:
                seen.add(col)
                unique.append((col, kind))
        col_names = _sql_names([c for c, _ in unique], "c")
        model[table_names[dax_table]] = {
            "dax": dax_table,
            "columns": [{"sql": col_names[c], "dax": c, "type": k} for c, k in unique],
        }
    return model
