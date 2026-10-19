// Send an answer to a Microsoft Teams or Slack channel (backend
// app/destinations.py): pick a channel your role may use, add a note, choose
// whether the table and the SQL go along, and send. Only the rows the
// message shows (the first PREVIEW_ROWS) are uploaded, with the result's size.
import { useEffect, useState } from "react";
import { api, getUser } from "../api";
import { HOW_TO } from "./DestinationsPanel";

const LABEL = { teams: "Microsoft Teams", slack: "Slack" };
/** How many rows a Teams or Slack message shows (backend PREVIEW_ROWS). */
export const PREVIEW_ROWS = 15;

// The channel last sent to, per kind: this browser's convenience only.
const LAST = (kind) => `studio_share_last:${kind}`;
function lastChannel(kind) {
  try { return localStorage.getItem(LAST(kind)) || ""; } catch { return ""; }
}
function rememberChannel(kind, id) {
  try { localStorage.setItem(LAST(kind), id); } catch { /* storage blocked: nothing to remember */ }
}

/** The request body for POST /share/send. */
export function sendBody(state, answer) {
  const rows = answer.rows || [];
  return {
    destination_id: state.destination, title: answer.title, text: answer.text || null,
    note: state.note.trim() || null, columns: answer.columns || [],
    rows: rows.slice(0, PREVIEW_ROWS), row_count: rows.length,
    sql: answer.sql || null, include_sql: Boolean(state.includeSql && answer.sql),
    include_rows: Boolean(state.includeRows), source: answer.source || null,
    conversation_id: answer.conversationId || null,
  };
}

export default function SharePanel({ kind, answer, onClose }) {
  const [channels, setChannels] = useState(null);
  const [state, setState] = useState({ destination: "", note: "", includeRows: true, includeSql: false });
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const isAdmin = getUser()?.role === "admin";

  useEffect(() => {
    api("/share/destinations")
      .then((d) => {
        const mine = (d.destinations || []).filter((c) => c.kind === kind);
        setChannels(mine);
        const last = lastChannel(kind);
        setState((s) => ({ ...s, destination: mine.some((c) => c.id === last) ? last : mine[0]?.id || "" }));
      })
      .catch(() => setChannels([]));
  }, [kind]);

  async function send() {
    if (!state.destination || busy) return;
    setBusy(true);
    setStatus("");
    try {
      const out = await api("/share/send", { method: "POST", body: JSON.stringify(sendBody(state, answer)) });
      setStatus(`✓ Sent to ${out.destination}`);
      rememberChannel(kind, state.destination);
    } catch (e) {
      setStatus(`Not sent: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="share-panel" aria-label={`Send to ${LABEL[kind]}`}>
      <div className="share-panel-head">
        <b>Send to {LABEL[kind]}</b>
        <button type="button" className="chip" onClick={onClose} title="Close">✕</button>
      </div>
      {channels === null ? <div className="meta">loading channels…</div> : channels.length === 0 ? (
        <div className="meta">
          No {LABEL[kind]} channel is set up for you yet.{" "}
          {isAdmin ? <>Add one in <b>Tools & settings → Integrations → {LABEL[kind]}</b>. {HOW_TO[kind]}</>
            : "Ask an administrator to add one."}
        </div>
      ) : (
        <>
          <div className="inputs-row">
            <span className="meta">Channel</span>
            <select value={state.destination} aria-label="Channel"
                    onChange={(e) => setState((s) => ({ ...s, destination: e.target.value }))}>
              {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <textarea className="sqllab-prompt" rows={2} value={state.note} aria-label="Note"
                    placeholder="Add a note (optional)"
                    onChange={(e) => setState((s) => ({ ...s, note: e.target.value }))} />
          <div className="inputs-row">
            {answer.rows?.length > 0 && (
              <label className="meta"><input type="checkbox" checked={state.includeRows}
                onChange={(e) => setState((s) => ({ ...s, includeRows: e.target.checked }))} />
                {" "}the table{answer.rows.length > PREVIEW_ROWS
                  ? ` (first ${PREVIEW_ROWS} of ${answer.rows.length.toLocaleString()} rows)`
                  : ` (${answer.rows.length} row${answer.rows.length === 1 ? "" : "s"})`}</label>
            )}
            {answer.sql && (
              <label className="meta"><input type="checkbox" checked={state.includeSql}
                onChange={(e) => setState((s) => ({ ...s, includeSql: e.target.checked }))} /> the SQL</label>
            )}
          </div>
          <div className="inputs-row">
            <button type="button" className="primary" onClick={send} disabled={busy || !state.destination}>
              {busy ? "sending…" : `Send to ${LABEL[kind]}`}
            </button>
            {status && <span className="meta">{status}</span>}
          </div>
          <div className="meta">Everyone in the channel will see it.</div>
        </>
      )}
    </div>
  );
}
