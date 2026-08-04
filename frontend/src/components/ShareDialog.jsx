// Share a conversation with other Studio users. Sharing hands over stored
// result rows, so the server refuses recipients whose role cannot query the
// underlying tables — that rejection is surfaced here verbatim.
import { useEffect, useState } from "react";
import { api } from "../api";

export default function ShareDialog({ conversationId, title, onClose }) {
  const [shares, setShares] = useState([]);
  const [canShare, setCanShare] = useState(false);
  const [email, setEmail] = useState("");
  const [permission, setPermission] = useState("edit");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    api(`/conversations/${conversationId}/shares`)
      .then((d) => {
        setShares(d.shares || []);
        setCanShare(!!d.can_share);
      })
      .catch((e) => setError(e.message));
  }, [conversationId]);

  async function add(e) {
    e.preventDefault();
    if (!email.trim() || busy) return;
    setBusy(true);
    setError("");
    try {
      const d = await api(`/conversations/${conversationId}/shares`, {
        method: "POST",
        body: JSON.stringify({ email: email.trim(), permission }),
      });
      setShares(d.shares || []);
      setEmail("");
      // Results their role cannot query stay hidden from them — say so.
      setNotice(
        d.hidden_for_recipient > 0
          ? `Shared. ${d.hidden_for_recipient} message${d.hidden_for_recipient > 1 ? "s" : ""} will stay hidden — their role can't access that data.`
          : ""
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(userId) {
    setBusy(true);
    try {
      const d = await api(`/conversations/${conversationId}/shares/${userId}`, { method: "DELETE" });
      setShares(d.shares || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="canvas-title">Share chat</div>
            <div className="meta">{title}</div>
          </div>
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>

        {canShare && (
          <form className="share-form" onSubmit={add}>
            <input
              placeholder="Teammate's Studio email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={busy}
              autoFocus
            />
            <select value={permission} onChange={(e) => setPermission(e.target.value)} disabled={busy}>
              <option value="edit">Can edit</option>
              <option value="view">Can view</option>
            </select>
            <button className="primary" disabled={busy || !email.trim()}>
              {busy ? "…" : "Share"}
            </button>
          </form>
        )}

        {error && <div className="error">{error}</div>}
        {notice && <div className="meta share-notice">{notice}</div>}

        <div className="share-list">
          {shares.map((s) => (
            <div key={s.user_id} className="share-row">
              <div>
                <div>{s.email || s.user_id}</div>
                <div className="meta">{s.permission === "edit" ? "can edit" : "can view"}</div>
              </div>
              {canShare && (
                <button className="chip" onClick={() => revoke(s.user_id)} disabled={busy}>
                  remove
                </button>
              )}
            </div>
          ))}
          {shares.length === 0 && (
            <div className="meta">
              {canShare ? "Not shared with anyone yet." : "This chat was shared with you."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
