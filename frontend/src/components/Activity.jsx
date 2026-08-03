// Activity log: every prompt, SQL, and outcome saved per user.
// Regular users see their own history; admins can switch to all users.
import { useEffect, useState } from "react";
import { api, getUser } from "../api";

export default function Activity({ onClose }) {
  const user = getUser();
  const [entries, setEntries] = useState([]);
  const [scope, setScope] = useState("me");
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    setError("");
    api(`/audit?all=${scope === "all"}&limit=300`)
      .then(setEntries)
      .catch((e) => setError(e.message));
  }, [scope]);

  const q = filter.toLowerCase();
  const shown = entries.filter(
    (e) =>
      !q ||
      (e.prompt || "").toLowerCase().includes(q) ||
      (e.email || "").toLowerCase().includes(q) ||
      (e.sql || "").toLowerCase().includes(q) ||
      (e.action || "").includes(q)
  );

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal activity" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="canvas-title">Activity</div>
            <div className="meta">Every prompt, query, and outcome — saved per user.</div>
          </div>
          <div className="canvas-actions">
            {user?.role === "admin" && (
              <>
                <button
                  className={"chip" + (scope === "me" ? " chip-active" : "")}
                  onClick={() => setScope("me")}
                >
                  my activity
                </button>
                <button
                  className={"chip" + (scope === "all" ? " chip-active" : "")}
                  onClick={() => setScope("all")}
                >
                  all users
                </button>
              </>
            )}
            <button className="chip" onClick={onClose}>✕ close</button>
          </div>
        </div>

        <input
          className="activity-filter"
          placeholder="Filter by prompt, user, SQL, action…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        {error && <div className="error">{error}</div>}

        <div className="table-wrap activity-table">
          <table>
            <thead>
              <tr>
                <th>time</th>
                {scope === "all" && <th>user</th>}
                {scope === "all" && <th>role</th>}
                <th>action</th>
                <th>prompt</th>
                <th>source/table</th>
                <th>rows</th>
                <th>ok</th>
                <th>ms</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((e) => (
                <tr key={e.id} title={e.sql || ""}>
                  <td>{new Date(e.created_at * 1000).toLocaleString()}</td>
                  {scope === "all" && <td>{e.email}</td>}
                  {scope === "all" && (
                    <td><span className={"rolebadge role-" + e.role}>{e.role}</span></td>
                  )}
                  <td>{e.action}</td>
                  <td className="prompt-cell">{e.prompt || "—"}</td>
                  <td>{[e.source, e.tbl].filter(Boolean).join("/") || "—"}</td>
                  <td>{e.row_count ?? "—"}</td>
                  <td>{e.ok ? "✓" : <span className="error-mark" title={e.error}>✗</span>}</td>
                  <td>{e.duration_ms ?? "—"}</td>
                </tr>
              ))}
              {shown.length === 0 && (
                <tr><td colSpan={9} className="meta">No activity yet.</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="meta" style={{ marginTop: 6 }}>
          Hover a row to see its SQL. Showing {shown.length} of {entries.length} entries.
        </div>
      </div>
    </div>
  );
}
