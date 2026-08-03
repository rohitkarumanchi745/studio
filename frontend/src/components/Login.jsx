import { useEffect, useState } from "react";
import { api, setSession } from "../api";

export default function Login({ onLogin }) {
  const [mode, setMode] = useState("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState(new URLSearchParams(window.location.search).get("sso_error") || "");
  const [busy, setBusy] = useState(false);
  const [azureReady, setAzureReady] = useState(true);

  useEffect(() => {
    api("/auth/sso").then((s) => setAzureReady(!!s.azure)).catch(() => {});
  }, []);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = mode === "login" ? { email, password } : { email, password, name };
      const data = await api(`/auth/${mode}`, { method: "POST", body: JSON.stringify(body) });
      setSession(data.access_token, data.user);
      onLogin(data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function azure() {
    if (!azureReady) {
      setError(
        "Microsoft sign-in needs an Entra app registration on this deployment — use a demo login below."
      );
      return;
    }
    // Full-page redirect: backend sends us to Microsoft, then back with a
    // Studio token (or ?sso_error if the flow fails).
    window.location.href = "/api/auth/azure/login";
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="brand">
          <span className="brand-mark">◆</span> Studio
        </div>
        <p className="login-sub">Ask your data anything.</p>

        <form onSubmit={submit}>
          {mode === "register" && (
            <input placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
          )}
          <input
            placeholder="Email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoFocus
          />
          <input
            placeholder="Password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <button className="primary" disabled={busy}>
            {busy ? "…" : mode === "login" ? "Sign in" : "Create account"}
          </button>
        </form>

        <button
          className="sso"
          onClick={azure}
          style={azureReady ? undefined : { opacity: 0.6 }}
          title={azureReady ? "Sign in with your Microsoft account" : "Entra SSO — configured via AZURE_* env vars"}
        >
          <svg width="14" height="14" viewBox="0 0 21 21">
            <rect x="0" y="0" width="10" height="10" fill="#f25022" />
            <rect x="11" y="0" width="10" height="10" fill="#7fba00" />
            <rect x="0" y="11" width="10" height="10" fill="#00a4ef" />
            <rect x="11" y="11" width="10" height="10" fill="#ffb900" />
          </svg>
          Sign in with Microsoft
        </button>

        {error && <div className="error">{error}</div>}

        <div className="login-switch">
          {mode === "login" ? (
            <>
              No account? <a onClick={() => setMode("register")}>Register</a>
            </>
          ) : (
            <>
              Have an account? <a onClick={() => setMode("login")}>Sign in</a>
            </>
          )}
        </div>

        <div className="demo-creds">
          Demo logins — admin@studio.local / admin123 · analyst@studio.local / analyst123 ·
          viewer@studio.local / viewer123
        </div>
      </div>
    </div>
  );
}
