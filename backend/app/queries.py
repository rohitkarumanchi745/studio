"""Verified SQL library.

The agent writes SQL for every answer. This module lets a user edit that SQL,
**verify** it — RBAC check, query guard, and a real execution against the
source — and only then save it, together with the requirement prompt that
produced it. Verification is a hard gate: a query that does not verify is
never stored (POST/PATCH re-verify server-side and reject on failure), so the
library only ever holds SQL that provably runs for that user's role.

Like dashboards, a saved query stores the RECIPE (source + sql + prompt),
never rows. Re-running it re-validates against the runner's role, so the
library can never become an RBAC bypass.
"""
import json
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import agent, db, queryguard, rbac
from .auth import current_user
from .catalog import _connector_or_400

router = APIRouter(prefix="/queries", tags=["queries"])

PREVIEW_ROWS = 20


def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS saved_queries (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            sql TEXT NOT NULL,
            source TEXT NOT NULL,
            table_label TEXT,
            columns TEXT,
            row_count INTEGER,
            verified INTEGER NOT NULL DEFAULT 0,
            verified_at REAL,
            edited INTEGER NOT NULL DEFAULT 0,
            visibility TEXT NOT NULL DEFAULT 'private',
            author_role TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_saved_queries_user
            ON saved_queries(user_id, updated_at DESC);
        """
    )
    c.commit()
    c.close()


# ── Verification: the gate everything passes through ────────────────────

def verify_sql(user, source, table_label, sql, full_rows=False):
    """RBAC + query guard + real execution. Returns
    {ok, columns, rows, row_count, sql, error}. Never raises for a rejected
    query — a failed verification is a normal result the caller surfaces.

    `rows` is a PREVIEW (PREVIEW_ROWS) because every caller so far only shows
    the user a sample; `row_count` is always the true count. full_rows=True
    returns every row (still capped at agent.MAX_ROWS) for callers that need the
    data itself — blend.py materializes parts into DuckDB. It stays a flag on
    this one function rather than a second fetch path, so there is exactly one
    gate: re-implementing RBAC + guard + governance elsewhere is how they drift.
    """
    sql = (sql or "").strip()
    if not sql:
        return {"ok": False, "error": "SQL is empty."}
    if not source:
        return {"ok": False, "error": "No source selected."}
    if not rbac.can_access(user["role"], source, table_label or "*"):
        return {"ok": False, "error": f"Your role has no access to {source}."}
    try:
        connector = _connector_or_400(source)
        allowed = rbac.allowed_tables(user["role"], source, connector.list_tables())
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    except Exception as e:
        return {"ok": False, "error": f"Source error: {e}"}
    if not allowed:
        return {"ok": False, "error": "Your role has no access to this source."}

    try:
        cleaned = queryguard.validate(sql, allowed)
        cleaned = queryguard.enforce_limit(cleaned, agent.MAX_ROWS)
    except queryguard.QueryRejected as e:
        return {"ok": False, "error": f"Rejected by the query guard: {e}"}

    t0 = time.time()
    try:
        columns, rows = connector.run_query(cleaned)
    except Exception as e:
        return {"ok": False, "error": f"Query failed: {str(e)[:300]}", "sql": cleaned}
    rows = rows[:agent.MAX_ROWS]
    # Compliance: strip denied columns, mask masked ones, cap rows — before
    # anything leaves the backend, so even SELECT * can't leak a denied column.
    from . import governance
    columns, rows = governance.filter_result(source, cleaned, columns, rows)
    return {
        "ok": True,
        "sql": cleaned,
        "columns": columns,
        "rows": rows if full_rows else rows[:PREVIEW_ROWS],
        "row_count": len(rows),
        "took_ms": int((time.time() - t0) * 1000),
    }


# ── The SQL verifier as a named, traced agent ───────────────────────────

def _verify_reward(result):
    """Reward for a verification rollout. A guard rejection scores low but not
    zero — the guard did its job catching bad SQL; a hard execution failure is
    the worst outcome; a query that runs and returns rows is best."""
    if result.get("ok"):
        return 1.0 if result.get("row_count") else 0.7
    err = (result.get("error") or "").lower()
    return 0.2 if ("guard" in err or "rejected" in err) else 0.0


def verify_agent(user, source, table_label, sql):
    """Run verify_sql AND record it as an Agent Lightning rollout attributed to
    the named "SQL verifier" agent. This is the user-facing verify action; the
    reward feeds the learning loop (failed verifications become recent_failures
    that get injected into the analyst agent's prompt). Internal callers
    (pipelines, the flow Validator) call verify_sql directly to avoid flooding
    the trace store with one rollout per step."""
    from . import roster
    result = verify_sql(user, source, table_label, sql)
    try:
        tid = db.add_trace(
            user, prompt=(sql or "")[:1000], mode="sql_verify", source=source,
            table=table_label, sql=result.get("sql") or sql, ok=result["ok"],
            error=None if result["ok"] else (result.get("error") or "")[:500],
            row_count=result.get("row_count"), duration_ms=result.get("took_ms"),
            reward=_verify_reward(result), reward_source="verifier",
            meta={"agent": roster.SQL_VERIFIER["name"],
                  "agents": [roster.SQL_VERIFIER["name"]]},
        )
    except Exception:
        tid = None
    result["agent"] = roster.SQL_VERIFIER["name"]
    if tid:
        result["trace_id"] = tid
    return result


# ── Persistence ─────────────────────────────────────────────────────────

def _row(r):
    d = dict(r)
    d["columns"] = json.loads(d["columns"]) if d.get("columns") else []
    d["verified"] = bool(d.get("verified"))
    d["edited"] = bool(d.get("edited"))
    d["can_edit"] = None  # filled by the endpoint against the requesting user
    return d


def _own_or_404(qid, user, *, edit=False):
    c = db._conn()
    row = c.execute("SELECT * FROM saved_queries WHERE id=?", (qid,)).fetchone()
    c.close()
    if row is None:
        raise HTTPException(404, "Query not found")
    d = _row(row)
    owner = d["user_id"] == user["id"]
    # Visibility first: a private query is invisible to non-owners on every
    # verb, so probing an id you can't see returns 404 whether you read,
    # edit, or delete — never 403, which would confirm the id exists.
    if not owner and d["visibility"] != "org":
        raise HTTPException(404, "Query not found")
    if edit and not owner:
        raise HTTPException(403, "Only the owner can change this query")
    return d


# ── API ─────────────────────────────────────────────────────────────────

class VerifyIn(BaseModel):
    source: str
    table: str | None = None
    sql: str


@router.post("/verify")
def verify(body: VerifyIn, user=Depends(current_user)):
    """Verify SQL without saving. This is the gate the UI checks before it
    lets the user save. The named SQL verifier agent records the attempt as an
    Agent Lightning rollout, so verifications (and failures) feed the learning
    loop."""
    return verify_agent(user, body.source, body.table, body.sql)


class SaveIn(BaseModel):
    prompt: str          # the requirement this SQL answers
    sql: str
    source: str
    table: str | None = None
    title: str | None = None
    edited: bool = False  # did the user change the agent's SQL?
    visibility: str = "private"


@router.post("", status_code=201)
def create(body: SaveIn, user=Depends(current_user)):
    """Save a query — but only if it verifies. The server re-runs the SQL, so
    a client cannot save an unverified query by skipping the verify call."""
    result = verify_sql(user, body.source, body.table, body.sql)
    if not result["ok"]:
        raise HTTPException(400, f"Not saved — verification failed: {result['error']}")

    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "A requirement prompt is required to save a query")
    title = (body.title or prompt)[:120]
    visibility = body.visibility if body.visibility in ("private", "org") else "private"
    now = time.time()
    qid = str(uuid.uuid4())
    c = db._conn()
    c.execute(
        "INSERT INTO saved_queries (id, user_id, title, prompt, sql, source, table_label, "
        "columns, row_count, verified, verified_at, edited, visibility, author_role, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (qid, user["id"], title, prompt, result["sql"], body.source, body.table,
         json.dumps(result["columns"]), result["row_count"], 1, now,
         1 if body.edited else 0, visibility, user["role"], now, now),
    )
    c.commit()
    c.close()
    db.log_activity(user, "query_save", prompt=title, source=body.source,
                    sql=result["sql"], row_count=result["row_count"])
    return get(qid, user)


@router.get("")
def listing(user=Depends(current_user)):
    """The verified repository: this user's saved queries plus org-shared."""
    c = db._conn()
    rows = c.execute(
        "SELECT * FROM saved_queries WHERE user_id=? OR visibility='org' "
        "ORDER BY updated_at DESC",
        (user["id"],),
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        d = _row(r)
        d["can_edit"] = d["user_id"] == user["id"]
        d["mine"] = d["user_id"] == user["id"]
        d.pop("rows", None)
        out.append(d)
    return {"queries": out}


@router.get("/{qid}")
def get(qid: str, user=Depends(current_user)):
    d = _own_or_404(qid, user)
    d["can_edit"] = d["user_id"] == user["id"]
    d["mine"] = d["user_id"] == user["id"]
    return d


class UpdateIn(BaseModel):
    sql: str | None = None
    prompt: str | None = None
    title: str | None = None
    visibility: str | None = None


@router.patch("/{qid}")
def update(qid: str, body: UpdateIn, user=Depends(current_user)):
    """Edit a saved query. Any SQL change is re-verified before it is stored —
    an edit that no longer runs is rejected, so the library stays trustworthy."""
    existing = _own_or_404(qid, user, edit=True)
    new_sql = body.sql.strip() if body.sql is not None else existing["sql"]
    fields, params = {}, []

    if body.sql is not None and new_sql != existing["sql"]:
        result = verify_sql(user, existing["source"], existing["table_label"], new_sql)
        if not result["ok"]:
            raise HTTPException(400, f"Not saved — verification failed: {result['error']}")
        fields["sql"] = result["sql"]
        fields["columns"] = json.dumps(result["columns"])
        fields["row_count"] = result["row_count"]
        fields["verified"] = 1
        fields["verified_at"] = time.time()
        fields["edited"] = 1

    if body.prompt is not None:
        p = body.prompt.strip()
        if not p:
            raise HTTPException(400, "Requirement prompt cannot be empty")
        fields["prompt"] = p
    if body.title is not None:
        fields["title"] = (body.title.strip() or existing["title"])[:120]
    if body.visibility in ("private", "org"):
        fields["visibility"] = body.visibility

    if fields:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [qid]
        c = db._conn()
        c.execute(f"UPDATE saved_queries SET {sets} WHERE id=?", params)
        c.commit()
        c.close()
    return get(qid, user)


@router.delete("/{qid}")
def remove(qid: str, user=Depends(current_user)):
    _own_or_404(qid, user, edit=True)
    c = db._conn()
    c.execute("DELETE FROM saved_queries WHERE id=?", (qid,))
    c.commit()
    c.close()
    return {"deleted": True}


@router.post("/{qid}/run")
def run(qid: str, user=Depends(current_user)):
    """Re-run a saved query for fresh data. Re-validated against the runner's
    role — an org-shared query still can't return tables this user can't see."""
    d = _own_or_404(qid, user)
    result = verify_sql(user, d["source"], d["table_label"], d["sql"])
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    return {"columns": result["columns"], "rows": result["rows"],
            "row_count": result["row_count"]}
