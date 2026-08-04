// Dashboard home: the landing view behind the sidebar's "Dashboards" nav.
// Dashboard.jsx renders nothing without an id, so this is what stands in
// until one is chosen — and it is the only place an empty account is told
// how to make its first dashboard.
import { useEffect, useState } from "react";
import { api } from "../api";

export default function DashboardList({ onOpen, onClose }) {
  const [list, setList] = useState(null); // null = loading
  const [error, setError] = useState("");

  useEffect(() => {
    api("/dashboards")
      .then((d) => setList(d?.dashboards || []))
      .catch((e) => {
        setError(e.message);
        setList([]);
      });
  }, []);

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Dashboards</div>
          <div className="meta">
            Saved sheets. Each tile stores its query, not its rows — data refreshes on open.
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
          <div className="empty-title">No dashboards yet</div>
          <div className="empty-sub">
            Ask a question, then hit <b>📌 pin</b> on the chart — that creates your first
            dashboard and pins the chart to it.
          </div>
        </div>
      ) : (
        <div className="dash-list">
          {list.map((d) => (
            <button key={d.id} className="dash-card" onClick={() => onOpen(d.id)}>
              <div className="dash-card-title">{d.title}</div>
              <div className="meta">
                {d.tile_count ?? 0} tile{(d.tile_count ?? 0) === 1 ? "" : "s"}
                {d.visibility === "org" ? " · org" : " · private"}
                {d.can_edit === false ? " · view only" : ""}
              </div>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
