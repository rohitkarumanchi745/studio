// Sign in / register. Registration does NOT create a session: /auth/register
// creates the account UNVERIFIED and returns no token, so the only honest flow
// is register -> enter the emailed 6-digit code -> sign in. Assuming a session
// here would leave the user "logged in" with a token the API never issued.
import { useEffect, useState } from "react";
import { api, setSession } from "../api";

export default function Login({ onLogin }) {
  const [mode, setMode] = useState("login");   // login | register | verify
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState(new URLSearchParams(window.location.search).get("sso_error") || "");
  const [busy, setBusy] = useState(false);
  const [azureReady, setAzureReady] = useState(true);
  // Production closes self-registration. Offering a Register form that can
  // only answer 403 is worse than not offering one, so the link waits for
  // /auth/sso to say signup is open (assume closed until it answers).
  const [openReg, setOpenReg] = useState(false);

  useEffect(() => {
    api("/auth/sso")
      .then((s) => { setAzureReady(!!s.azure); setOpenReg(!!s.open_registration); })
      .catch(() => {});
  }, []);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (mode === "register") {
        // No token comes back — the account is unverified until the code is
        // accepted, so go to the code step instead of pretending to be in.
        await api("/auth/register", {
          method: "POST",
          body: JSON.stringify({ email, password, name }),
        });
        setNotice(`We emailed a 6-digit code to ${email}. Enter it to finish.`);
        setMode("verify");
        return;
      }
      if (mode === "verify") {
        await api("/auth/verify-email", {
          method: "POST",
          body: JSON.stringify({ email, code: code.trim() }),
        });
        // Verification issues no token either: sign in for a real session.
        setNotice("Email verified — sign in to continue.");
        setCode("");
        setMode("login");
        return;
      }
      const data = await api("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
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
          {mode === "verify" ? (
            <input
              placeholder="6-digit code"
              inputMode="numeric"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          ) : (
            <input
              placeholder="Password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
          <button className="primary" disabled={busy}>
            {busy
              ? "…"
              : mode === "login"
                ? "Sign in"
                : mode === "register"
                  ? "Create account"
                  : "Verify email"}
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
        {notice && <div className="meta">{notice}</div>}

        <div className="login-switch">
          {mode === "login" ? (
            openReg && (
              <>
                No account? <a onClick={() => { setNotice(""); setMode("register"); }}>Register</a>
              </>
            )
          ) : (
            <>
              Have an account? <a onClick={() => { setNotice(""); setMode("login"); }}>Sign in</a>
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
