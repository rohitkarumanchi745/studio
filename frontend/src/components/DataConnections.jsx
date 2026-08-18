// Connect Microsoft 365. Studio can sync a user's own OneDrive / SharePoint
// files and Outlook mail into a PRIVATE, per-user knowledge collection so an
// agent's knowledge_search can ground answers in them — retrievable by nobody
// but that user (and admins). Nothing here ever sees a token: the OAuth grant
// happens on Microsoft's site, the backend stores the refresh token encrypted,
// and every response is non-secret metadata. When the deployment has no Azure
// credentials the feature is dormant and this surface says so instead of 500ing.
import { useEffect, useState } from "react";
import { api } from "../api";

const STATUS_LABEL = {
  onboarding: "Onboarding — first sync running",
  connected: "Connected",
  error: "Error — will retry",
  revoked: "Disconnected — reconnect to resume",
};

function fmtWhen(epoch) {
  if (!epoch) return "never";
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}

export default function DataConnections({ onClose }) {
  const [state, setState] = useState(null); // null until first /status load
  const [busy, setBusy] = useState("");     // "connect" | "sync" | "disconnect"
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  function load() {
    api("/m365/status")
      .then(setState)
      .catch((e) => setError(e.message));
  }

  // On mount, surface the result of an OAuth round-trip. The callback redirects
  // back with ?m365_connected=1 or ?m365_error=… — read it, show a note, then
  // scrub the query so a refresh doesn't replay it.
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.has("m365_connected")) {
      setNote("Microsoft 365 connected — your files and mail are syncing in the background.");
    } else if (q.has("m365_error")) {
      setError(`Microsoft 365 connection failed: ${q.get("m365_error") || "unknown error"}`);
    }
    if (q.has("m365_connected") || q.has("m365_error")) {
      q.delete("m365_connected");
      q.delete("m365_error");
      const rest = q.toString();
      window.history.replaceState(
        {},
        "",
        window.location.pathname + (rest ? `?${rest}` : "") + window.location.hash
      );
    }
    load();
  }, []);

  async function connect() {
    setBusy("connect");
    setError("");
    setNote("");
    try {
      const d = await api("/m365/connect", { method: "POST" });
      if (d.configured === false) {
        // Dormant — refresh so the not-configured notice shows.
        setState(d);
      } else if (d.authorize_url) {
        // Delegated OAuth: hand off to Microsoft; we come back via the callback.
        window.location.href = d.authorize_url;
        return;
      } else {
        // App mode: provisioned server-side, no redirect needed.
        setNote("Microsoft 365 connected — syncing in the background.");
        load();
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function sync() {
    setBusy("sync");
    setError("");
    setNote("");
    try {
      await api("/m365/sync", { method: "POST" });
      setNote("Sync queued — new and changed items will appear shortly.");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function disconnect() {
    if (!confirm("Disconnect Microsoft 365? Synced documents stop grounding agent answers until you reconnect.")) return;
    setBusy("disconnect");
    setError("");
    setNote("");
    try {
      await api("/m365/connect", { method: "DELETE" });
      setNote("Disconnected. Your tokens were wiped.");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const configured = state?.configured;
  const connected = state?.connected;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal m365-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="canvas-title">Data connections</div>
            <div className="meta">
              Connect Microsoft 365 to sync your OneDrive / SharePoint files and
              Outlook mail into a private knowledge collection only you (and admins)
              can retrieve. Documents are quoted reference material, never
              instructions — and Studio never sees your password or tokens.
            </div>
          </div>
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>

        {error && <div className="error">{error}</div>}
        {note && <div className="meta share-notice">{note}</div>}

        <div className="m365-card">
          <div className="m365-head">
            <div className="m365-title">
              <span className="m365-glyph" aria-hidden="true">▦</span>
              Microsoft 365
            </div>
            {state == null ? (
              <span className="m365-badge">checking…</span>
            ) : configured === false ? (
              <span className="m365-badge m365-badge-off">not configured</span>
            ) : connected ? (
              <span className="m365-badge m365-badge-on">
                {STATUS_LABEL[state.status] || "Connected"}
              </span>
            ) : (
              <span className="m365-badge">not connected</span>
            )}
          </div>

          {state == null ? (
            <div className="meta">Loading connection status…</div>
          ) : configured === false ? (
            <div className="meta">
              Microsoft 365 isn't set up on this deployment. An administrator needs to
              configure the Azure app credentials before this connection is available.
            </div>
          ) : connected ? (
            <>
              <div className="m365-facts">
                {state.mode && (
                  <span className="query-tag" title="how Studio authenticates to Graph">
                    {state.mode === "app" ? "app (tenant-wide)" : "delegated (your account)"}
                  </span>
                )}
                <span className="query-tag">{state.item_count ?? 0} items synced</span>
                <span className="meta">last sync {fmtWhen(state.last_sync)}</span>
              </div>
              <div className="m365-actions">
                <button
                  className="chip chip-on"
                  onClick={sync}
                  disabled={!!busy || state.status === "revoked"}
                >
                  {busy === "sync" ? "queuing…" : "↻ Sync now"}
                </button>
                {state.status === "revoked" && (
                  <button className="chip" onClick={connect} disabled={!!busy}>
                    {busy === "connect" ? "connecting…" : "↗ Reconnect"}
                  </button>
                )}
                <button
                  className="chip ctx-danger"
                  onClick={disconnect}
                  disabled={!!busy}
                >
                  {busy === "disconnect" ? "disconnecting…" : "✕ Disconnect"}
                </button>
              </div>
            </>
          ) : (
            <>
              <div className="meta">
                Connect your account to let Studio index your files and mail. You'll be
                sent to Microsoft to sign in and grant read-only access — Studio only
                ever receives an access token it stores encrypted, never your password.
              </div>
              <div className="m365-actions">
                <button className="primary" onClick={connect} disabled={!!busy}>
                  {busy === "connect" ? "connecting…" : "↗ Connect Microsoft 365"}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
