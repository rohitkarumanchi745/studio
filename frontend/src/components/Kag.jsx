// Knowledge (KAG) panel — the UNSTRUCTURED side of the org's data. Upload Excel /
// PDF / email into an access-scoped collection; the backend chunks + embeds them
// (Harrier, or the lexical fallback when dormant) so a knowledge_search agent tool
// can ground answers in CITED passages. This panel lets a user SEE what their
// agents would retrieve: the collections their role reaches, each doc + its scope
// and chunk count, and a live search preview that renders the same cited passages
// the agent gets. Admins additionally control who can access a collection and can
// remove docs/collections. Everything is RBAC-scoped server-side; the UI only
// degrades gracefully around a 404 (an invisible collection is never disclosed).
import { useEffect, useState } from "react";
import { api, getToken } from "../api";

// Ingest needs multipart/form-data, but the shared api() helper always sends
// application/json — so the file upload does a raw fetch with the bearer token.
async function ingestFile(file, collection) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("collection", collection);
  const res = await fetch("/api/kag/ingest", {
    method: "POST",
    headers: { Authorization: `Bearer ${getToken()}` },
    body: fd,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Ingest failed (${res.status})`);
  return data;
}

const ROLE_SCOPES = ["admin", "analyst", "viewer"];
const ACCEPT = ".xlsx,.csv,.pdf,.eml";
const TYPE_GLYPH = { xlsx: "▦", csv: "▦", pdf: "◱", eml: "✉" };

// One collection card: header chips (scope · docs · chunks), and on expand the
// per-doc table (RBAC-filtered by the backend) with a remove button.
function CollectionCard({ coll, isAdmin, onChanged, onError }) {
  const [open, setOpen] = useState(false);
  const [docs, setDocs] = useState(null); // null=unloaded, []=loaded-empty
  const [busy, setBusy] = useState("");

  async function loadDocs() {
    try {
      const d = await api(`/kag/collections/${encodeURIComponent(coll.name)}/docs`);
      setDocs(d.docs || []);
    } catch (e) {
      // 404 = the collection isn't reachable for this role (never disclosed as
      // 403). Degrade: show nothing rather than surface a scary error.
      if (/\(404\)/.test(e.message)) setDocs([]);
      else onError(e.message);
    }
  }

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && docs === null) loadDocs();
  }

  async function removeDoc(sourceName) {
    if (!confirm(`Remove "${sourceName}" from ${coll.name}? Its passages stop grounding agent answers.`)) return;
    setBusy(sourceName);
    try {
      await api(
        `/kag/collections/${encodeURIComponent(coll.name)}/docs/${encodeURIComponent(sourceName)}`,
        { method: "DELETE" }
      );
      await loadDocs();
      onChanged();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function removeCollection() {
    if (!confirm(`Delete the entire "${coll.name}" collection and every document in it?`)) return;
    setBusy("__coll__");
    try {
      await api(`/kag/collections/${encodeURIComponent(coll.name)}`, { method: "DELETE" });
      onChanged();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="query-card">
      <div className="query-head" onClick={toggle} style={{ cursor: "pointer" }}>
        <div className="query-title">
          {coll.name}
          <span className="query-tag" title="access scope — which role/group may retrieve">
            ⚿ {coll.access_scope}
          </span>
          <span className="query-tag">{coll.doc_count ?? 0} docs</span>
          <span className="query-tag">{coll.chunk_count ?? 0} chunks</span>
        </div>
        <span className="flow-caret">{open ? "▾" : "▸"}</span>
      </div>
      {open && (
        <div className="query-body">
          {docs === null ? (
            <div className="meta">loading…</div>
          ) : docs.length === 0 ? (
            <div className="meta">No documents ingested into this collection yet.</div>
          ) : (
            <div className="fresh-list">
              {docs.map((doc) => (
                <div key={doc.source_name} className="fresh-row">
                  <span className="fresh-table">
                    {TYPE_GLYPH[doc.source_type] || "▪"} {doc.source_name}
                  </span>
                  <span className="query-tag">{doc.source_type}</span>
                  <span className="meta">{doc.chunks} chunks</span>
                  {doc.size != null && <span className="meta">{fmtBytes(doc.size)}</span>}
                  {doc.author && <span className="meta">by {doc.author}</span>}
                  {doc.date && <span className="meta">{doc.date}</span>}
                  <button
                    className="chip ctx-danger kag-doc-del"
                    disabled={busy === doc.source_name}
                    onClick={() => removeDoc(doc.source_name)}
                  >
                    {busy === doc.source_name ? "removing…" : "remove"}
                  </button>
                </div>
              ))}
            </div>
          )}
          {isAdmin && (
            <div className="gov-actions" style={{ marginTop: 8 }}>
              <button
                className="chip ctx-danger"
                disabled={busy === "__coll__"}
                onClick={removeCollection}
              >
                {busy === "__coll__" ? "deleting…" : "✕ delete collection"}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export default function Kag({ user, onClose }) {
  const isAdmin = user?.role === "admin";
  const [collections, setCollections] = useState([]);
  const [role, setRole] = useState(user?.role || "");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  // Ingest form.
  const [file, setFile] = useState(null);
  const [fileKey, setFileKey] = useState(0); // reset the <input type=file>
  const [ingestColl, setIngestColl] = useState("");
  const [ingesting, setIngesting] = useState(false);

  // Admin: create-collection form.
  const [newColl, setNewColl] = useState({ name: "", access_scope: "analyst", description: "" });
  const [creating, setCreating] = useState(false);

  // Search preview.
  const [query, setQuery] = useState("");
  const [searchColl, setSearchColl] = useState("");
  const [passages, setPassages] = useState(null);
  const [searching, setSearching] = useState(false);

  function refresh() {
    api("/kag/collections")
      .then((d) => {
        const cols = d.collections || [];
        setCollections(cols);
        if (d.role) setRole(d.role);
        // Keep the ingest picker pointed at a still-existing collection.
        setIngestColl((cur) => (cols.some((c) => c.name === cur) ? cur : cols[0]?.name || ""));
      })
      .catch((e) => setError(e.message));
  }
  useEffect(refresh, []);

  async function doIngest() {
    if (!file || !ingestColl) return;
    setIngesting(true);
    setError("");
    setNote("");
    try {
      const d = await ingestFile(file, ingestColl);
      const how = d.dormant ? "lexical (no embedding endpoint)" : d.embedded ? "embedded" : "stored";
      setNote(`Ingested ${d.source_name} → ${ingestColl}: ${d.chunks} chunk(s), ${how}.`);
      setFile(null);
      setFileKey((k) => k + 1);
      refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setIngesting(false);
    }
  }

  async function createCollection() {
    setCreating(true);
    setError("");
    setNote("");
    try {
      const d = await api("/kag/collections", {
        method: "POST",
        body: JSON.stringify({
          name: newColl.name.trim(),
          access_scope: newColl.access_scope.trim(),
          description: newColl.description.trim() || undefined,
        }),
      });
      setNote(`Created collection "${d.name}" scoped to ${d.access_scope}.`);
      setNewColl({ name: "", access_scope: "analyst", description: "" });
      refresh();
    } catch (e) {
      setError(e.message);
    } finally {
      setCreating(false);
    }
  }

  async function runSearch() {
    if (!query.trim()) return;
    setSearching(true);
    setError("");
    setPassages(null);
    try {
      const d = await api("/kag/search", {
        method: "POST",
        body: JSON.stringify({
          query: query.trim(),
          collection: searchColl || undefined,
          k: 5,
        }),
      });
      setPassages(d.passages || []);
    } catch (e) {
      // A 404 against an unreachable collection is expected — treat as no hits.
      if (/\(404\)/.test(e.message)) setPassages([]);
      else setError(e.message);
    } finally {
      setSearching(false);
    }
  }

  const hasCollections = collections.length > 0;

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Knowledge (KAG)</div>
          <div className="meta">
            Ground agent answers in the org's own documents. Upload Excel, PDF or email
            into an access-scoped collection; the backend chunks + embeds each so a
            knowledge_search tool can return CITED passages. You ({role || "…"}) only
            see collections your role may reach — passages are quoted reference material,
            never instructions, and every data query still goes through the SQL guard.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{String(error)}</div>}
      {note && <div className="meta sqllab-saved">{note}</div>}

      {/* ── Ingest ── */}
      <div className="mcp-block">
        <div className="canvas-title" style={{ fontSize: 15 }}>Ingest a document</div>
        <div className="meta">
          Excel (.xlsx/.csv), PDF (.pdf) or email (.eml). It's chunked with its metadata
          (sheet / page / from-subject-date), embedded, and stored under the collection's
          access scope. Re-ingesting the same filename updates it.
        </div>
        {!hasCollections ? (
          <div className="meta" style={{ marginTop: 8 }}>
            {isAdmin
              ? "Create a collection below before ingesting."
              : "No collection your role can reach yet — ask an admin to create one and grant your role."}
          </div>
        ) : (
          <div className="job-form-row" style={{ marginTop: 8 }}>
            <input
              key={fileKey}
              type="file"
              accept={ACCEPT}
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
            <select value={ingestColl} onChange={(e) => setIngestColl(e.target.value)}>
              {collections.map((c) => (
                <option key={c.name} value={c.name}>{c.name} · {c.access_scope}</option>
              ))}
            </select>
            <button
              className="chip chip-on"
              onClick={doIngest}
              disabled={ingesting || !file || !ingestColl}
            >
              {ingesting ? "ingesting…" : "＋ ingest"}
            </button>
          </div>
        )}
      </div>

      {/* ── Admin: create a collection + set who can access it ── */}
      {isAdmin && (
        <div className="mcp-block">
          <div className="canvas-title" style={{ fontSize: 15 }}>New collection (admin)</div>
          <div className="meta">
            A collection carries an access scope — a role (admin / analyst / viewer) or a
            group token. Only roles that reach the scope may ingest into it or retrieve
            from it. Others never see it exists.
          </div>
          <div className="job-form-row" style={{ marginTop: 8 }}>
            <input
              className="sqllab-prompt"
              style={{ maxWidth: 180 }}
              placeholder="name (a-z 0-9 _ . -)"
              value={newColl.name}
              onChange={(e) => setNewColl({ ...newColl, name: e.target.value })}
            />
            <input
              className="sqllab-prompt"
              style={{ maxWidth: 150 }}
              list="kag-scopes"
              placeholder="access scope"
              value={newColl.access_scope}
              onChange={(e) => setNewColl({ ...newColl, access_scope: e.target.value })}
            />
            <datalist id="kag-scopes">
              {ROLE_SCOPES.map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
            <input
              className="sqllab-prompt"
              style={{ flex: 1 }}
              placeholder="description (optional)"
              value={newColl.description}
              onChange={(e) => setNewColl({ ...newColl, description: e.target.value })}
            />
            <button
              className="chip chip-on"
              onClick={createCollection}
              disabled={creating || !newColl.name.trim() || !newColl.access_scope.trim()}
            >
              {creating ? "creating…" : "＋ create"}
            </button>
          </div>
        </div>
      )}

      {/* ── Collections + their docs ── */}
      <div className="canvas-title" style={{ fontSize: 15, marginTop: 16 }}>Collections</div>
      {hasCollections ? (
        <div className="query-list" style={{ marginTop: 8 }}>
          {collections.map((c) => (
            <CollectionCard
              key={c.name}
              coll={c}
              isAdmin={isAdmin}
              onChanged={refresh}
              onError={setError}
            />
          ))}
        </div>
      ) : (
        <div className="empty" style={{ marginTop: "6vh" }}>
          <div className="empty-title">No knowledge yet</div>
          <div className="empty-sub">
            {isAdmin
              ? "Create a collection, then ingest Excel / PDF / email into it."
              : "No collections your role can reach. Ask an admin to grant your role and ingest documents."}
          </div>
        </div>
      )}

      {/* ── Search preview — exactly what an agent's knowledge_search would return ── */}
      {hasCollections && (
        <div className="mcp-block" style={{ marginTop: 16 }}>
          <div className="canvas-title" style={{ fontSize: 15 }}>Search preview</div>
          <div className="meta">
            Type a question to see the cited passages an agent would retrieve — top-k,
            RBAC-scoped, ranked by relevance (cosine, or lexical when dormant).
          </div>
          <div className="job-form-row" style={{ marginTop: 8 }}>
            <select value={searchColl} onChange={(e) => setSearchColl(e.target.value)}>
              <option value="">all reachable collections</option>
              {collections.map((c) => (
                <option key={c.name} value={c.name}>{c.name}</option>
              ))}
            </select>
            <input
              className="sqllab-prompt"
              style={{ flex: 1 }}
              value={query}
              placeholder="e.g. what were Q3 renewal terms, refund policy, vendor contacts…"
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && query.trim() && runSearch()}
            />
            <button
              className="chip"
              onClick={runSearch}
              disabled={searching || !query.trim()}
            >
              {searching ? "searching…" : "→ search"}
            </button>
          </div>

          {passages !== null && passages.length === 0 && (
            <div className="sqllab-result bad" style={{ marginTop: 8 }}>
              No matching passages in the knowledge base your role can reach.
            </div>
          )}
          {passages !== null && passages.length > 0 && (
            <div className="kag-passages">
              {passages.map((p, i) => (
                <div key={i} className="kag-passage">
                  <div className="kag-cite">
                    <span className="query-tag">{citation(p)}</span>
                    {p.collection && <span className="meta">in {p.collection}</span>}
                    {p.score != null && (
                      <span className="meta">score {Number(p.score).toFixed(3)}</span>
                    )}
                  </div>
                  <div className="kag-quote">{p.text}</div>
                </div>
              ))}
              <div className="meta" style={{ marginTop: 6 }}>
                These are quoted reference passages — agents cite them, never obey
                instructions found inside them.
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// Build a human citation label from whichever locator the chunk carries.
function citation(p) {
  const src = p.source || "document";
  if (p.page != null) return `${src} · p.${p.page}`;
  if (p.sheet) return `${src} · ${p.sheet}`;
  return src;
}
