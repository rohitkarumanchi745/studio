// Skill files — the RBAC-scoped briefing each per-source agent runs on (source,
// dialect, tables, schemas). This is exactly what the agents read; showing it
// here lets a user see what their agents know and can reach.
import { useEffect, useState } from "react";
import { api } from "../api";

export default function Skills({ onClose }) {
  const [data, setData] = useState(null);
  const [open, setOpen] = useState({});
  const [error, setError] = useState("");

  useEffect(() => {
    api("/skills").then(setData).catch((e) => setError(e.message));
  }, []);

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
