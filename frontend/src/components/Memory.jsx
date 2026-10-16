// What the agent remembers about you — the notes it saved with its `remember`
// tool. Every one of them is added to your agent's instructions on every
// question, so this page is where you check them and remove any that are
// wrong or out of date.
import { useEffect, useState } from "react";
import { api } from "../api";

export function NotesList({ notes, onDelete, busy }) {
  if (notes.length === 0) {
    return (
      <div className="empty">
        <div className="empty-title">Nothing remembered yet</div>
        <div className="empty-sub">
          Tell the agent a lasting preference ("I always want revenue in EUR") and it
          will save a note here.
        </div>
      </div>
    );
  }
  return (
    <div className="query-list">
      {notes.map((n) => (
        <div key={n.id} className="query-card">
          <div className="query-head">
            <div className="query-title">
              {n.note}
              <span className="query-tag">
                {new Date(n.updated_at * 1000).toLocaleDateString()}
              </span>
            </div>
            <button className="chip" disabled={busy} onClick={() => onDelete(n)}
              aria-label={`Forget: ${n.note}`}>
              ✕ forget
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function Memory({ onClose }) {
  const [notes, setNotes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api("/memory").then((d) => setNotes(d.notes || [])).catch((e) => setError(e.message));
  }, []);

  async function run(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const forgetOne = (n) => run(async () => {
    await api(`/memory/${n.id}`, { method: "DELETE" });
    setNotes((all) => all.filter((x) => x.id !== n.id));
  });

  const forgetAll = () => {
    if (!confirm("Forget everything the agent remembers about you? This can't be undone.")) return;
    run(async () => {
      await api("/memory", { method: "DELETE" });
      setNotes([]);
    });
  };

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Memory</div>
          <div className="meta">
            Notes the agent saved about your preferences. It reads them on every
            question; only you can see them.
          </div>
        </div>
        <div className="canvas-actions">
          {notes?.length > 0 && (
            <button className="chip" disabled={busy} onClick={forgetAll}>forget everything</button>
          )}
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>
      {error && <div className="error">{error}</div>}
      {notes && <NotesList notes={notes} onDelete={forgetOne} busy={busy} />}
    </section>
  );
}
