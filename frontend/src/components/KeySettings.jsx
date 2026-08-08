// Connect your own provider API key. The key is sent once, verified against
// the provider, then stored encrypted — the server only ever returns the last
// four characters, so nothing here can read a key back out.
import { useEffect, useState } from "react";
import { api } from "../api";

const LABEL = { anthropic: "Anthropic (Claude)", openai: "OpenAI (GPT)" };
const PLACEHOLDER = { anthropic: "sk-ant-…", openai: "sk-…" };
// Anthropic has no OAuth flow a web app can use — API keys, Workload Identity
// Federation and App Attest are the only options — so the best we can do is
// deep-link the page that mints one.
const KEY_PAGE = {
  anthropic: "https://platform.claude.com/settings/keys",
  openai: "https://platform.openai.com/api-keys",
};

export default function KeySettings({ onClose, onChanged }) {
  const [state, setState] = useState(null);
  const [provider, setProvider] = useState("anthropic");
  const [apiKey, setApiKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  const load = () =>
    api("/settings/keys")
      .then(setState)
      .catch((e) => setError(e.message));

  useEffect(() => {
    load();
  }, []);

  async function connect(e) {
    e.preventDefault();
    if (!apiKey.trim() || busy) return;
    setBusy(true);
    setError("");
    setNote("");
    try {
      const d = await api("/settings/keys", {
        method: "POST",
        body: JSON.stringify({ provider, api_key: apiKey.trim() }),
      });
      setState((s) => ({ ...s, keys: d.keys }));
      setApiKey("");
      setNote(`${LABEL[provider]} connected — your questions now use your key.`);
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(p) {
    setBusy(true);
    setError("");
    try {
      const d = await api(`/settings/keys/${p}`, { method: "DELETE" });
      setState((s) => ({ ...s, keys: d.keys }));
      setNote(`${LABEL[p] || p} removed.`);
      onChanged?.();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const connected = state?.keys || [];
  const serverHas = state?.server_keys || [];

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="canvas-title">Your API keys</div>
            <div className="meta">
              Connect your own key to use your account instead of the shared one.
              Stored encrypted; never shown again after saving.
            </div>
          </div>
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>

        <div className="key-steps">
          <a className="chip" href={KEY_PAGE[provider]} target="_blank" rel="noopener noreferrer">
            ↗ Create a key on {provider === "anthropic" ? "Anthropic" : "OpenAI"}
          </a>
          <span className="meta">
            Opens {new URL(KEY_PAGE[provider]).host} — create the key there, then paste it below.
            We verify it against the provider before saving.
          </span>
        </div>

        <form className="share-form" onSubmit={connect}>
          <select value={provider} onChange={(e) => setProvider(e.target.value)} disabled={busy}>
            {(state?.providers || ["anthropic", "openai"]).map((p) => (
              <option key={p} value={p}>{LABEL[p] || p}</option>
            ))}
          </select>
          <input
            type="password"
            autoComplete="off"
            placeholder={PLACEHOLDER[provider] || "API key"}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={busy}
          />
          <button className="primary" disabled={busy || !apiKey.trim()}>
            {busy ? "checking…" : "Connect"}
          </button>
        </form>

        {error && <div className="error">{error}</div>}
        {note && <div className="meta share-notice">{note}</div>}

        <div className="share-list">
          {connected.map((k) => (
            <div key={k.provider} className="share-row">
              <div>
                <div>{LABEL[k.provider] || k.provider}</div>
                <div className="meta">
                  ····{k.last4} · added {new Date(k.created_at * 1000).toLocaleDateString()}
                </div>
              </div>
              <button className="chip" onClick={() => remove(k.provider)} disabled={busy}>
                remove
              </button>
            </div>
          ))}
          {connected.length === 0 && (
            <div className="meta">
              {serverHas.length
                ? `No personal key. Questions use the shared ${serverHas.join(" / ")} key.`
                : "No key connected — the agent runs in preview-only fallback until one is added."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
