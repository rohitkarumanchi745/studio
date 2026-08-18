"""Knowledge-Augmented Generation (KAG) over the org's OWN unstructured data.

Studio's structured side is warehouses + SQL (queryguard, rbac, connectors).
This module adds the UNSTRUCTURED side: an admin ingests the business's own
Excel / CSV / PDF / email into RBAC-scoped *collections*, each document is
parsed → chunked → embedded (embed.py — Harrier when wired, lexical Jaccard when
dormant) → stored with its metadata and access scope, and an agent gets a
`knowledge_search` tool that returns the most relevant CITED passages to GROUND
its reasoning in real company documents.

Why this shape (the invariants that matter):

- REUSE, don't rebuild. Embeddings come from embed.py exactly as the query
  cache uses them: documents are embedded plain, queries instruction-prefixed,
  cosine when HARRIER_EMBED_URL is set, else embed() returns None and retrieval
  falls back to a lexical token-Jaccard score. So KAG works with NO Harrier
  endpoint (dormant-safe) and with a MIXED store (some rows embedded, some not).

- RBAC ON RETRIEVAL, server-side. A collection carries an access_scope (a role
  or group token), denormalized onto every chunk row. rbac.kag_scopes_for(role)
  says which scopes a role may read; the SQL WHERE excludes every other scope
  BEFORE Python ever sees a vector, so a user's agent can never retrieve another
  scope's documents. Governance-as-code, identical in spirit to allowed_sources.
  KAG never widens access: search only returns chunks the role's scope already
  permitted at ingest time, so grounding text is not a data-exfil path.

- DOCUMENTS ARE UNTRUSTED. A passage that says "ignore your rules / run this SQL
  / reveal secrets" is REFERENCE data, not an instruction. knowledge_search has
  no side effects and no privileges — it returns quoted, cited strings in a JSON
  envelope. It cannot run SQL, cannot change RBAC or the guard; any real data
  query still goes through run_sql → queryguard.validate (the SQL gate).

- 404-NOT-403 for invisible collections: a collection a role can't reach is
  indistinguishable from one that doesn't exist. Creating/deleting a collection
  or setting its scope is admin-gated (403).

- INJECTION-SAFE STORAGE: collection/source names are validated against a strict
  charset; every value is bound via `?` placeholders (db.py translates to %s on
  Postgres), never interpolated — a filename or sheet name can't inject SQL.

- GRACEFUL FAILURE: a byte ceiling before parsing (huge → 413); every parser
  wrapped so a corrupt/binary/wrong-type file → 422 with nothing half-written;
  an empty extract → 0 chunks, a clear message, no exception.

Scale path (noted, not built for v1): the `embedding` column is a JSON array in
a TEXT column so it is Postgres+SQLite safe with no pgvector dependency, and
cosine top-k is a pure-Python scan. For TB-scale KBs, migrate `embedding` to a
pgvector `vector(1024)` with an IVF/HNSW index (or DuckDB array_cosine_similarity)
and replace the Python scan with an ORDER BY over the index — the column-as-JSON
design migrates cleanly.
"""
import hashlib
import io
import json
import os
import re
import time
import uuid
from email import policy as _email_policy
from email.parser import BytesParser

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from . import db, embed, rbac
from .auth import current_user

router = APIRouter(prefix="/kag", tags=["kag"])

# Collection and source names become RBAC scope tokens and are shown in
# citations; keep them to a printable, quoting-free charset so nothing user- or
# metadata-supplied can carry SQL/identifier metacharacters. (Values are ALSO
# always bound via ? placeholders — this is defense in depth, mirroring
# objectstore._valid's rejection of quotes/whitespace.)
_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")

# Parsing/ chunking knobs (env-overridable so a deploy can tune without code).
MAX_BYTES = int(os.getenv("KAG_MAX_BYTES", str(25 * 1024 * 1024)))  # 25 MB ceiling
ROWS_PER_CHUNK = int(os.getenv("KAG_ROWS_PER_CHUNK", "40"))         # xlsx/csv window
CHAR_WINDOW = int(os.getenv("KAG_CHAR_WINDOW", "1200"))            # long page/body split
TOP_K = int(os.getenv("KAG_TOP_K", "5"))

_SOURCE_TYPES = ("xlsx", "csv", "pdf", "eml")


# ── Store ────────────────────────────────────────────────────────────────

def init_tables():
    c = db._conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS kag_collections (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            access_scope TEXT NOT NULL,
            description TEXT,
            created_by TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kag_chunks (
            id TEXT PRIMARY KEY,
            collection TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding TEXT,
            metadata TEXT NOT NULL,
            access_scope TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kag_coll ON kag_chunks(collection);
        CREATE INDEX IF NOT EXISTS idx_kag_hash ON kag_chunks(content_hash);
        """
    )
    c.commit()
    c.close()


def _valid_name(name):
    return bool(_NAME_RE.match(name or ""))


def _collection(name):
    c = db._conn()
    r = c.execute("SELECT * FROM kag_collections WHERE name=?", (name,)).fetchone()
    c.close()
    return dict(r) if r else None


def _reachable(scope, role):
    """True iff `role` may retrieve from a collection whose access_scope=scope."""
    scopes = rbac.kag_scopes_for(role)
    return scopes == "*" or scope in scopes


def reachable_collections(role):
    """Collection rows this role may retrieve from — RBAC-filtered, the same
    server-side scope test the tool and /kag/search apply. The agent uses the
    non-empty check to decide whether to OFFER knowledge_search at all."""
    scopes = rbac.kag_scopes_for(role)
    c = db._conn()
    if scopes == "*":
        rows = c.execute("SELECT * FROM kag_collections WHERE name IN (SELECT DISTINCT collection FROM kag_chunks) ORDER BY name").fetchall()
    elif not scopes:
        rows = []
    else:
        ph = ",".join("?" for _ in scopes)
        rows = c.execute(
            f"SELECT * FROM kag_collections WHERE access_scope IN ({ph}) "
            f"AND name IN (SELECT DISTINCT collection FROM kag_chunks) ORDER BY name",
            tuple(scopes)).fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── Parsers: bytes → [{"text", "meta"}] ─────────────────────────────────
#
# Each parser is pure and raises on a structurally bad file; the caller wraps
# every call so a corrupt/binary/wrong-type upload becomes a 422, never a crash.

def _basename(name):
    return os.path.basename((name or "").replace("\\", "/")) or "document"


def _windows(text, size=CHAR_WINDOW):
    """Split a long string into ~size-char windows on whitespace boundaries."""
    text = (text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    out, i = [], 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):
            brk = text.rfind(" ", i + int(size * 0.6), end)
            if brk > i:
                end = brk
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        i = end
    return out


def _parse_xlsx(data, source_name):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    author = None
    try:
        author = wb.properties.creator
    except Exception:
        pass
    chunks = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        cols = [str(h) if h is not None else "" for h in (header or [])]
        header_line = " | ".join(cols)
        buf, start = [], 2  # data starts on row 2 (row 1 = header)
        idx = 2

        def flush(buf, rstart, rend):
            body = "\n".join(buf)
            text = f"[Sheet: {ws.title}] {header_line}\n{body}".strip()
            chunks.append({"text": text, "meta": {
                "sheet": ws.title, "row_start": rstart, "row_end": rend,
                "columns": cols, "author": author}})

        for row in rows:
            cells = " | ".join("" if v is None else str(v) for v in row)
            if cells.strip():
                buf.append(cells)
            idx += 1
            if len(buf) >= ROWS_PER_CHUNK:
                flush(buf, start, idx - 1)
                buf, start = [], idx
        if buf:
            flush(buf, start, idx - 1)
    wb.close()
    return chunks


def _parse_csv(data, source_name):
    import csv
    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)
    if not all_rows:
        return []
    header = all_rows[0]
    cols = [str(h) for h in header]
    header_line = " | ".join(cols)
    chunks = []
    body_rows = all_rows[1:]
    for i in range(0, len(body_rows), ROWS_PER_CHUNK):
        window = body_rows[i:i + ROWS_PER_CHUNK]
        body = "\n".join(" | ".join(str(v) for v in r) for r in window)
        chunks.append({"text": f"{header_line}\n{body}".strip(), "meta": {
            "row_start": i + 2, "row_end": i + 1 + len(window), "columns": cols}})
    return chunks


def _parse_pdf(data, source_name):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    author = None
    try:
        author = (reader.metadata or {}).get("/Author")
    except Exception:
        pass
    page_count = len(reader.pages)
    chunks = []
    for pno, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        for piece in _windows(text):
            chunks.append({"text": piece, "meta": {
                "page": pno, "page_count": page_count, "author": author}})
    return chunks


def _parse_eml(data, source_name):
    msg = BytesParser(policy=_email_policy.default).parsebytes(data)
    subject = str(msg.get("subject", "") or "")
    frm = str(msg.get("from", "") or "")
    to = str(msg.get("to", "") or "")
    date = str(msg.get("date", "") or "")
    body = ""
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            body = part.get_content()
    except Exception:
        body = ""
    if not body:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            body = str(msg.get_payload())
    header_block = (f"Subject: {subject}\nFrom: {frm}\nTo: {to}\nDate: {date}").strip()
    meta = {"subject": subject, "from": frm, "to": to, "date": date, "author": frm}
    chunks = []
    windows = _windows(body) or [""]
    for i, piece in enumerate(windows):
        text = f"{header_block}\n\n{piece}".strip() if i == 0 else piece
        if text:
            chunks.append({"text": text, "meta": dict(meta)})
    return chunks or [{"text": header_block, "meta": dict(meta)}]


_PARSERS = {"xlsx": _parse_xlsx, "csv": _parse_csv, "pdf": _parse_pdf, "eml": _parse_eml}


def _sniff_type(name, data):
    """Route to a parser by extension, corroborated by magic bytes where cheap.
    Returns a source_type in _SOURCE_TYPES or None (unsupported)."""
    ext = os.path.splitext((name or "").lower())[1].lstrip(".")
    if ext in ("xlsx", "xlsm"):
        return "xlsx"
    if ext == "csv":
        return "csv"
    if ext == "pdf":
        return "pdf"
    if ext in ("eml", "email"):
        return "eml"
    # Fall back to magic bytes for extensionless uploads.
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:2] == b"PK":            # zip container → xlsx
        return "xlsx"
    return None


def parse(name, data):
    """Parse bytes into chunk dicts. Raises ValueError on an unsupported or
    unparseable file so the caller can turn it into a clean 422."""
    stype = _sniff_type(name, data)
    if stype is None:
        raise ValueError("unsupported file type (need .xlsx/.csv/.pdf/.eml)")
    try:
        chunks = _PARSERS[stype](data, _basename(name))
    except HTTPException:
        raise
    except Exception as e:
        raise ValueError(f"could not parse as {stype}: {e}")
    return stype, [ch for ch in chunks if (ch.get("text") or "").strip()]


# ── Ingestion ────────────────────────────────────────────────────────────

def _hash(source_name, text):
    return hashlib.sha256(f"{source_name}\n{text}".encode("utf-8")).hexdigest()


def ingest_bytes(user, collection, name, data, access_scope=None):
    """Parse → chunk → embed → store one document. Returns a result dict.

    Governance: the target collection must be reachable by the caller's role
    (else 404 — invisible). A missing collection is auto-created ONLY by an admin
    (with access_scope); a non-admin ingesting into a non-existent collection
    gets 404. Re-ingest of the same (collection, source_name) is a wholesale
    UPDATE — old chunks deleted, new ones inserted — so removed pages/rows
    disappear. content_hash dedups identical chunks within the document.
    """
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"file exceeds the {MAX_BYTES} byte KAG limit")
    if not data:
        raise HTTPException(422, "empty file")
    name = _basename(name)
    if not _valid_name(name):
        raise HTTPException(400, "source name has invalid characters")

    coll = _collection(collection)
    if coll is None:
        if user["role"] != "admin":
            raise HTTPException(404, "Not found")   # 404-not-403 for invisible/absent
        scope = (access_scope or "admin").strip()
        if not _valid_name(collection) or not _valid_name(scope):
            raise HTTPException(400, "invalid collection or scope name")
        coll = _create_collection(collection, scope, None, user)
    elif not _reachable(coll["access_scope"], user["role"]):
        raise HTTPException(404, "Not found")       # exists, but not for this role

    scope = coll["access_scope"]

    # Parse OUTSIDE the DB write so a corrupt file leaves nothing half-written.
    # An unsupported type is a 400 (wrong input); a supported type that won't
    # parse (corrupt/binary) is a 422 — either way, graceful, never a crash.
    if _sniff_type(name, data) is None:
        raise HTTPException(400, "unsupported file type (need .xlsx/.csv/.pdf/.eml)")
    try:
        stype, chunks = parse(name, data)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(422, f"could not parse {name}: {e}")
    if not chunks:
        return {"collection": collection, "source_name": name, "chunks": 0,
                "embedded": False, "dormant": not embed.available(),
                "note": "no extractable text (e.g. a scanned image PDF)"}

    now = time.time()
    embedded_any = False
    seen = set()
    rows = []
    for ch in chunks:
        text = ch["text"][:16000]
        h = _hash(name, text)
        if h in seen:                # skip identical chunks within this doc
            continue
        seen.add(h)
        vec = embed.embed(text, kind="document")   # None when dormant → lexical
        if vec:
            embedded_any = True
        meta = dict(ch.get("meta") or {})
        meta.update({"source_name": name, "source_type": stype, "size": len(data)})
        rows.append((str(uuid.uuid4()), collection, stype, name, text,
                     json.dumps(vec) if vec else None, json.dumps(meta, default=str),
                     scope, h, now))

    c = db._conn()
    c.execute("DELETE FROM kag_chunks WHERE collection=? AND source_name=?",
              (collection, name))
    for r in rows:
        c.execute(
            "INSERT INTO kag_chunks (id, collection, source_type, source_name, "
            "chunk_text, embedding, metadata, access_scope, content_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", r)
    c.commit()
    c.close()
    db.log_activity(user, "kag_ingest", prompt=name, source=collection,
                    row_count=len(rows))
    return {"collection": collection, "source_name": name, "chunks": len(rows),
            "embedded": embedded_any, "dormant": not embed.available()}


def _create_collection(name, scope, description, user):
    row = {"id": str(uuid.uuid4()), "name": name, "access_scope": scope,
           "description": description, "created_by": user["id"],
           "created_at": time.time()}
    c = db._conn()
    c.execute("INSERT INTO kag_collections (id, name, access_scope, description, "
              "created_by, created_at) VALUES (?,?,?,?,?,?)",
              (row["id"], name, scope, description, user["id"], row["created_at"]))
    c.commit()
    c.close()
    return row


# ── Retrieval: RBAC-filtered cosine (lexical fallback) top-k ─────────────

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text):
    return set(_TOKEN_RE.findall((text or "").lower()))


def _lexical(query, text):
    """Token-set Jaccard — KAG's dormant retrieval, mirroring embed.py's
    fallback philosophy: catches re-asks and word-order/plural variation even
    with no Harrier endpoint."""
    a, b = _tokens(query), _tokens(text)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def search(query, role, k=None, collection=None):
    """RBAC-scoped top-k cited passages for a query.

    The scope filter is applied SERVER-SIDE in the SQL WHERE, before any vector
    is scored, so a role can never receive a chunk outside its reachable scopes.
    Scoring is cosine when the query and a row both have embeddings, else lexical
    Jaccard — so retrieval works dormant and over a mixed store. Returns
    [{text, source, source_type, page|sheet, score, collection}].
    """
    k = k or TOP_K
    query = (query or "").strip()
    if not query:
        return []
    scopes = rbac.kag_scopes_for(role)
    if not scopes:
        return []

    clauses, params = [], []
    if scopes != "*":
        ph = ",".join("?" for _ in scopes)
        clauses.append(f"access_scope IN ({ph})")
        params.extend(scopes)
    if collection is not None:
        clauses.append("collection=?")
        params.append(collection)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    c = db._conn()
    rows = c.execute(f"SELECT * FROM kag_chunks{where}", tuple(params)).fetchall()
    c.close()

    qvec = embed.embed(query, kind="query")
    scored = []
    for row in rows:
        r = dict(row)
        emb = r.get("embedding")
        if qvec and emb:
            try:
                s = embed.cosine(qvec, json.loads(emb))
            except Exception:
                s = _lexical(query, r["chunk_text"])
        else:
            s = _lexical(query, r["chunk_text"])
        scored.append((s, r))
    scored.sort(key=lambda t: t[0], reverse=True)

    out = []
    for s, r in scored[:k]:
        try:
            meta = json.loads(r["metadata"])
        except Exception:
            meta = {}
        cite = {}
        if "page" in meta:
            cite["page"] = meta["page"]
        if "sheet" in meta:
            cite["sheet"] = meta["sheet"]
        passage = {"text": r["chunk_text"], "source": r["source_name"],
                   "source_type": r["source_type"], "score": round(float(s), 4),
                   "collection": r["collection"]}
        passage.update(cite)
        out.append(passage)
    return out


# ── API ──────────────────────────────────────────────────────────────────

def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "KAG collection administration is admin-only")


def _reach_or_404(name, user):
    """The collection row iff the caller's role may reach it, else 404 — never
    disclosing that another scope's collection exists."""
    coll = _collection(name)
    if not coll or not _reachable(coll["access_scope"], user["role"]):
        raise HTTPException(404, "Not found")
    return coll


class CollectionIn(BaseModel):
    name: str
    access_scope: str
    description: str | None = None


class SearchIn(BaseModel):
    query: str
    collection: str | None = None
    k: int | None = None


@router.post("/ingest")
async def ingest(file: UploadFile = File(None), collection: str = Form(...),
                 uri: str = Form(None), access_scope: str = Form(None),
                 fmt: str = Form(None), user=Depends(current_user)):
    """Ingest an uploaded file OR a registered object-store URI into a
    collection. Either `file` (multipart) or `uri` must be provided."""
    if not _valid_name(collection):
        raise HTTPException(400, "invalid collection name")
    if file is not None:
        data = await file.read()
        name = file.filename
    elif uri:
        _authorize_uri(uri, user)     # admin + under a REGISTERED dataset — BEFORE any fetch
        name, data = _read_uri(uri, user)
    else:
        raise HTTPException(400, "provide a file upload or a uri")
    return ingest_bytes(user, collection, name, data, access_scope=access_scope)


def _authorize_uri(uri, user):
    """Gate a `uri=` ingest BEFORE any blob is fetched: admin-only, and the URI
    must sit under a REGISTERED object-store dataset — never an arbitrary bucket.
    The object-store REGISTRY (not the bucket) is the boundary, and authorizing
    first means no unprivileged / cross-scope caller can drive a server-side
    fetch (no SSRF-ish read primitive)."""
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Only an admin may ingest from an object-store URI")
    from .connectors import objectstore
    src = next((x for x in objectstore._SOURCES
                if uri.startswith(tuple(objectstore._SOURCES[x]["schemes"]))), None)
    if not src:
        raise HTTPException(400, "uri must be an s3:// / az:// / gs:// object")
    prefixes = []
    for d in objectstore.datasets(src):
        p = objectstore._prefix_of(d.get("uri") or "")
        if p:
            prefixes.append(p if p.endswith("/") else p + "/")
    if not prefixes or not any(uri.startswith(p) for p in prefixes):
        raise HTTPException(403, "uri is not under a registered object-store dataset")


def _read_uri(uri, user):
    """Read a single object's bytes from a registered object-store URI, reusing
    objectstore's validation + credentials seam (no new secret handling). Best
    effort and dormant-safe: if DuckDB/credentials aren't configured, it fails
    gracefully with a clear error rather than crashing."""
    from .connectors import objectstore
    src = next((s for s in objectstore._SOURCES if uri.startswith(
        tuple(objectstore._SOURCES[s]["schemes"]))), None)
    if not src:
        raise HTTPException(400, "uri must be an s3:// / az:// / gs:// object")
    # Reuse the exact URI hardening objectstore applies (no query string,
    # quotes or whitespace that could smuggle credentials).
    if "?" in uri or re.search(r"""[\s'"\\]""", uri):
        raise HTTPException(400, "uri contains disallowed characters")
    try:
        import duckdb
        con = duckdb.connect(":memory:")
        creds = objectstore._credentials(src)
        if not creds["present"]:
            raise HTTPException(422, f"object store '{src}' is not configured")
        for ext in objectstore._SOURCES[src]["extensions"]:
            con.execute(f"INSTALL {ext}")
            con.execute(f"LOAD {ext}")
        try:
            con.execute(creds["secret"])
        except Exception:
            for stmt in creds["legacy"]:
                con.execute(stmt)
        data = con.execute("SELECT content FROM read_blob(?)", [uri]).fetchone()[0]
        con.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"could not read object {uri!r}: {e}")
    return objectstore._prefix_of(uri).rstrip("/").split("/")[-1] or _basename(uri), bytes(data)


@router.get("/collections")
def list_collections(user=Depends(current_user)):
    """Collections this role may reach, with doc/chunk counts. RBAC-filtered."""
    colls = reachable_collections(user["role"])
    c = db._conn()
    out = []
    for coll in colls:
        stats = c.execute(
            "SELECT COUNT(*) n, COUNT(DISTINCT source_name) d FROM kag_chunks "
            "WHERE collection=?", (coll["name"],)).fetchone()
        out.append({"name": coll["name"], "access_scope": coll["access_scope"],
                    "description": coll["description"],
                    "doc_count": stats["d"], "chunk_count": stats["n"]})
    c.close()
    return {"collections": out, "role": user["role"]}


@router.post("/collections", status_code=201)
def create_collection(body: CollectionIn, user=Depends(current_user)):
    _admin(user)
    name, scope = (body.name or "").strip(), (body.access_scope or "").strip()
    if not _valid_name(name):
        raise HTTPException(400, "name must match [A-Za-z0-9 _.-]{1,64}")
    if not _valid_name(scope):
        raise HTTPException(400, "access_scope must match [A-Za-z0-9 _.-]{1,64}")
    if _collection(name):
        raise HTTPException(409, "collection already exists")
    _create_collection(name, scope, (body.description or None), user)
    db.log_activity(user, "kag_collection_create", prompt=name, source=scope)
    return {"name": name, "access_scope": scope}


@router.get("/collections/{name}/docs")
def list_docs(name: str, user=Depends(current_user)):
    _reach_or_404(name, user)
    c = db._conn()
    rows = c.execute(
        "SELECT source_name, source_type, COUNT(*) chunks, MIN(metadata) meta, "
        "MAX(created_at) created_at FROM kag_chunks WHERE collection=? "
        "GROUP BY source_name, source_type ORDER BY source_name", (name,)).fetchall()
    c.close()
    docs = []
    for r in rows:
        try:
            meta = json.loads(r["meta"])
        except Exception:
            meta = {}
        docs.append({"source_name": r["source_name"], "source_type": r["source_type"],
                     "chunks": r["chunks"], "size": meta.get("size"),
                     "author": meta.get("author"), "date": meta.get("date"),
                     "created_at": r["created_at"]})
    return {"docs": docs}


@router.post("/search")
def search_preview(body: SearchIn, user=Depends(current_user)):
    """Preview retrieval — the SAME RBAC-scoped path the agent tool uses."""
    if body.collection is not None:
        _reach_or_404(body.collection, user)   # 404 for an invisible collection
    k = min(int(body.k or TOP_K), 20)
    passages = search(body.query, role=user["role"], k=k, collection=body.collection)
    return {"passages": passages}


@router.delete("/collections/{name}/docs/{source_name}")
def delete_doc(name: str, source_name: str, user=Depends(current_user)):
    coll = _reach_or_404(name, user)
    # Deleting content requires admin, or a role that can ingest into the scope.
    if user["role"] != "admin" and not _reachable(coll["access_scope"], user["role"]):
        raise HTTPException(403, "not permitted")
    c = db._conn()
    cur = c.execute("DELETE FROM kag_chunks WHERE collection=? AND source_name=?",
                    (name, _basename(source_name)))
    n = cur.rowcount if cur.rowcount is not None else 0
    c.commit()
    c.close()
    return {"deleted": n}


@router.delete("/collections/{name}")
def delete_collection(name: str, user=Depends(current_user)):
    _admin(user)
    if not _collection(name):
        raise HTTPException(404, "Not found")
    c = db._conn()
    cur = c.execute("DELETE FROM kag_chunks WHERE collection=?", (name,))
    n = cur.rowcount if cur.rowcount is not None else 0
    c.execute("DELETE FROM kag_collections WHERE name=?", (name,))
    c.commit()
    c.close()
    db.log_activity(user, "kag_collection_delete", prompt=name)
    return {"deleted": n}
