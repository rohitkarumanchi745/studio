// Skill files — the RBAC-scoped briefing each per-source agent runs on (source,
// dialect, tables, schemas). This is exactly what the agents read; showing it
// here lets a user see what their agents know and can reach.
import { useEffect, useState } from "react";
import { api } from "../api";

export default function Skills({ onClose }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState({});
  const [fresh, setFresh] = useState({});   // source -> {loading|tables}
  const [error, setError] = useState("");

  useEffect(() => {
    api("/skills").then(setData).catch((e) => setError(e.message));
  }, []);

  // "As of when" is the warehouse's ingestion, not Studio's — Studio reads live
  // data every query, so this reports the newest timestamp each table carries.
  async function checkFreshness(source) {
    setFresh((f) => ({ ...f, [source]: "loading" }));
    try {
      const d = await api(`/freshness/${source}`);
      setFresh((f) => ({ ...f, [source]: d.tables || [] }));
    } catch (e) {
      setFresh((f) => ({ ...f, [source]: [] }));
      setError(e.message);
    }
  }

  const skills = data?.skills || [];

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Skill files</div>
          <div className="meta">
            The briefing each source agent runs on — the tables and schemas your role
            ({data?.role || "…"}) can reach. Auto-rebuilt whenever a schema or your
            access changes; this is exactly what the agents read.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {data && skills.length === 0 && (
        <div className="empty">
          <div className="empty-title">No skill files</div>
          <div className="empty-sub">Your role has no accessible connected sources yet.</div>
        </div>
      )}

      <div className="query-list">
        {skills.map((s) => (
          <div key={s.source} className="query-card">
            <div className="query-head" onClick={() => setOpen((o) => ({ ...o, [s.source]: !o[s.source] }))}
              style={{ cursor: "pointer" }}>
              <div className="query-title">
                {s.agent}
                <span className="query-tag">{s.source}</span>
                <span className="query-tag">{s.dialect}</span>
                <span className="query-tag">{s.tables.length} tables</span>
              </div>
              <span className="flow-caret">{open[s.source] ? "▾" : "▸"}</span>
            </div>
            <div className="query-body">
              <div className="inputs-row">
                <span className="meta">briefed on</span>
                {s.tables.slice(0, 14).map((t) => (
                  <span key={t} className="chip chip-input">▦ {t}</span>
                ))}
                {s.tables.length > 14 && <span className="meta">+{s.tables.length - 14}</span>}
              </div>
              <div className="gov-actions" style={{ marginTop: 6 }}>
                <button className="chip" onClick={(e) => { e.stopPropagation(); checkFreshness(s.source); }}
                  disabled={fresh[s.source] === "loading"}>
                  {fresh[s.source] === "loading" ? "checking…" : "🕒 data freshness"}
                </button>
              </div>
              {Array.isArray(fresh[s.source]) && (
                <div className="fresh-list">
                  <div className="meta">
                    Newest timestamp each table carries — Studio queries live data, so this
                    reflects your warehouse's last ingestion (which Studio doesn't schedule).
                  </div>
                  {fresh[s.source].map((t) => (
                    <div key={t.table} className="fresh-row">
                      <span className="fresh-table">{t.table}</span>
                      {t.column ? (
                        <>
                          <span className="query-tag">{t.kind === "load" ? "load stamp" : "record date"}</span>
                          <span className="meta">{t.column}</span>
                          <b>{String(t.latest ?? "—")}</b>
                          <span className="meta">{t.rows != null ? `${t.rows} rows` : ""}</span>
                        </>
                      ) : (
                        <span className="meta">{t.error || t.note || "no date column"}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {open[s.source] && (
                <pre className="flow-json" style={{ marginLeft: 0, whiteSpace: "pre-wrap" }}>{s.skill}</pre>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
