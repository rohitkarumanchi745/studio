// The verified SQL library — every saved query with the requirement it
// answers. Everything here is verified: the backend re-runs SQL before it
// stores or updates it, so a broken query can never be in this list.
import { useEffect, useState } from "react";
import { api } from "../api";

export default function QueryLibrary({ onClose }) {
  const [list, setList] = useState(null); // null = loading
  const [error, setError] = useState("");
  const [open, setOpen] = useState(null); // expanded query id
  const [run, setRun] = useState({}); // id -> {columns, rows, row_count} | "loading" | err

  const load = () =>
    api("/queries")
      .then((d) => setList(d.queries || []))
      .catch((e) => {
        setError(e.message);
        setList([]);
      });

  useEffect(() => {
    load();
  }, []);

  async function runQuery(q) {
    setRun((r) => ({ ...r, [q.id]: "loading" }));
    try {
      const d = await api(`/queries/${q.id}/run`, { method: "POST" });
      setRun((r) => ({ ...r, [q.id]: d }));
    } catch (e) {
      setRun((r) => ({ ...r, [q.id]: { error: e.message } }));
    }
  }

  async function remove(q) {
    if (!confirm(`Delete "${q.title}"?`)) return;
    try {
      await api(`/queries/${q.id}`, { method: "DELETE" });
      setList((l) => l.filter((x) => x.id !== q.id));
    } catch (e) {
      setError(e.message);
    }
  }

  function copy(sql) {
    navigator.clipboard?.writeText(sql);
  }

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Saved SQL</div>
          <div className="meta">
            Verified queries with the requirement each answers. Every entry ran
            successfully for your role before it was saved.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      {list === null ? (
        <div className="meta">loading…</div>
      ) : list.length === 0 ? (
        <div className="empty">
          <div className="empty-title">No saved queries yet</div>
          <div className="empty-sub">
            Ask a question, open <b>◦ edit / verify / save SQL</b> under the answer,
            verify it, and save. Verified queries land here.
          </div>
        </div>
      ) : (
        <div className="query-list">
          {list.map((q) => {
            const r = run[q.id];
            return (
              <div key={q.id} className="query-card">
                <div className="query-head" onClick={() => setOpen(open === q.id ? null : q.id)}>
                  <div className="query-title">
                    {q.title}
                    <span className="query-badge" title="Verified on save">✓ verified</span>
                    {q.edited && <span className="query-tag">edited</span>}
                    {q.visibility === "org" && <span className="query-tag">team</span>}
                    {!q.mine && <span className="query-tag">shared by {q.owner_email || "teammate"}</span>}
                  </div>
                  <div className="meta">
                    {q.source}{q.table_label ? `/${q.table_label}` : ""} · {q.row_count} rows
                    {q.columns?.length ? ` · ${q.columns.length} cols` : ""}
                  </div>
                </div>

                {open === q.id && (
                  <div className="query-body">
                    <div className="meta query-prompt">“{q.prompt}”</div>
                    <pre className="query-sql">{q.sql}</pre>
                    <div className="query-actions">
                      <button className="chip" onClick={() => runQuery(q)} disabled={r === "loading"}>
                        {r === "loading" ? "running…" : "▷ run"}
                      </button>
                      <button className="chip" onClick={() => copy(q.sql)}>⧉ copy SQL</button>
                      {q.mine && (
                        <button className="chip ctx-danger" onClick={() => remove(q)}>✕ delete</button>
                      )}
                    </div>
                    {r && r !== "loading" && (
                      r.error ? (
                        <div className="error">{r.error}</div>
                      ) : (
                        <div className="table-wrap query-result">
                          <table>
                            <thead>
                              <tr>{r.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                            </thead>
                            <tbody>
                              {r.rows.slice(0, 20).map((row, ri) => (
                                <tr key={ri}>{row.map((v, ci) => <td key={ci}>{String(v ?? "")}</td>)}</tr>
                              ))}
                            </tbody>
                          </table>
                          <div className="meta">{r.row_count} rows (showing {Math.min(20, r.rows.length)})</div>
                        </div>
                      )
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
