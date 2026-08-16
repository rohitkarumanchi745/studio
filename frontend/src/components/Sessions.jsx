// Saved agent sessions — the serialized state of a run: model, scope, the full
// transcript, and the cacheable prefix. Resume rehydrates a run and reuses the
// provider prompt cache (the hosted-API stand-in for a KV-cache snapshot);
// fork branches from a snapshot; the counters show token + cache-read reuse.
import { useEffect, useState } from "react";
import { api } from "../api";

function fmt(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

export default function Sessions({ onResume, onClose }) {
  const [list, setList] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  const load = () =>
    api("/sessions")
      .then((d) => setList(d.sessions || []))
      .catch((e) => {
        setError(e.message);
        setList([]);
      });

  useEffect(() => {
    load();
  }, []);

  async function resume(s) {
    setBusy(s.id);
    try {
      const state = await api(`/sessions/${s.id}/resume`, { method: "POST" });
      // Rehydrate into chat: the conversation carries the same transcript, and
      // replaying its stable prefix is what hits the provider prompt cache.
      onResume?.(state);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function fork(s) {
    setBusy(s.id);
    try {
      await api(`/sessions/${s.id}/fork`, { method: "POST" });
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function remove(s) {
    if (!confirm(`Delete session "${s.title}"?`)) return;
    try {
      await api(`/sessions/${s.id}`, { method: "DELETE" });
      setList((l) => l.filter((x) => x.id !== s.id));
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Sessions</div>
          <div className="meta">
            Every conversation is serialized as a resumable agent session — model,
            scope, full transcript, and a hashed cacheable prefix. Resume continues a
            run and reuses the provider's prompt cache; fork branches from a snapshot.
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
          <div className="empty-title">No saved sessions yet</div>
          <div className="empty-sub">Ask something in chat — each turn checkpoints a session here.</div>
        </div>
      ) : (
        <div className="query-list">
          {list.map((s) => (
            <div key={s.id} className="query-card">
              <div className="query-head">
                <div className="query-title">
                  {s.title}
                  {s.model_spec && <span className="query-tag">{s.model_spec.split(":").pop()}</span>}
                  {s.source && <span className="query-tag">{s.source}{s.table_scope && s.table_scope !== "*" ? `/${s.table_scope}` : ""}</span>}
                  <span className="query-tag">{s.turns} turn{s.turns === 1 ? "" : "s"}</span>
                </div>
                <div className="query-actions">
                  <button className="chip chip-on" onClick={() => resume(s)} disabled={busy === s.id}>
                    {busy === s.id ? "…" : "▷ resume"}
                  </button>
                  <button className="chip" onClick={() => fork(s)} disabled={busy === s.id}>⑃ fork</button>
                  <button className="chip ctx-danger" onClick={() => remove(s)}>✕</button>
                </div>
              </div>
              <div className="query-body">
                <div className="sess-meters">
                  <span className="meta">prefix {s.cache_prefix_len} msg</span>
                  <span className="meta">in {fmt(s.tokens_in)} · out {fmt(s.tokens_out)} tok</span>
                  <span className="meta">
                    cache read {fmt(s.cache_read_tokens)}
                    {s.tokens_in ? ` (${Math.round(s.cache_hit_ratio * 100)}% reuse)` : ""}
                  </span>
                  {s.cache_write_tokens > 0 && <span className="meta">cache write {fmt(s.cache_write_tokens)}</span>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
