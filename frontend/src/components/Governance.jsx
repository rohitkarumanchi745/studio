// Governance-as-code editor (admin only). One YAML owns per-role source/table
// access and per-table compliance (deny/mask columns, row caps). Validate
// before applying; applying hot-reloads with no redeploy. Clearing reverts to
// Studio's built-in RBAC.
import { useEffect, useState } from "react";
import { api } from "../api";

export default function Governance({ onClose }) {
  const [text, setText] = useState("");
  const [meta, setMeta] = useState({ loaded: false, source: null });
  const [check, setCheck] = useState(null); // {ok, errors, summary}
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    api("/governance")
      .then((d) => {
        setText(d.yaml || "");
        setMeta({ loaded: d.loaded, source: d.source });
      })
      .catch((e) => setError(e.message));
  }, []);

  async function loadTemplate() {
    setError("");
    try {
      const d = await api("/governance/template");
      setText(d.yaml);
      setCheck(null);
      setNote("Loaded a starter document from your live sources and roles.");
    } catch (e) {
      setError(e.message);
    }
  }

  async function validate() {
    setBusy("validate");
    setError("");
    setNote("");
    try {
      const d = await api("/governance/validate", {
        method: "POST",
        body: JSON.stringify({ yaml: text }),
      });
      setCheck(d);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function apply() {
    setBusy("apply");
    setError("");
    setNote("");
    try {
      const d = await api("/governance", {
        method: "PUT",
        body: JSON.stringify({ yaml: text }),
      });
      setMeta({ loaded: d.loaded, source: d.source });
      setNote("Applied — access and compliance are now governed by this document.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function clear() {
    if (!confirm("Revert to Studio's built-in RBAC and drop the applied document?")) return;
    setBusy("clear");
    try {
      const d = await api("/governance", { method: "DELETE" });
      setMeta({ loaded: d.loaded, source: null });
      setCheck(null);
      setNote("Reverted to built-in RBAC.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Governance</div>
          <div className="meta">
            One document owns per-role source/table access and per-table compliance
            (deny / mask columns, row caps). {meta.loaded
              ? `Active — loaded from ${meta.source}.`
              : "Not loaded — Studio is using built-in RBAC."}
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {note && <div className="meta sqllab-saved">{note}</div>}

      <div className="gov-actions">
        <button className="chip" onClick={loadTemplate}>⎘ load template</button>
        <button className="chip" onClick={validate} disabled={busy === "validate" || !text.trim()}>
          {busy === "validate" ? "checking…" : "✓ validate"}
        </button>
        <button
          className={"chip" + (check?.ok ? " chip-on" : "")}
          onClick={apply}
          disabled={busy === "apply" || !text.trim()}
          title="Applies immediately (validated server-side)"
        >
          {busy === "apply" ? "applying…" : "⇧ apply"}
        </button>
        {meta.loaded && (
          <button className="chip ctx-danger" onClick={clear} disabled={busy === "clear"}>
            ↺ revert to built-in RBAC
          </button>
        )}
      </div>

      {check && (
        <div className={"sqllab-result " + (check.ok ? "ok" : "bad")}>
          {check.ok ? "✓ Valid document" : "✗ " + (check.errors || []).join("; ")}
        </div>
      )}
      {check?.ok && check.summary && (
        <div className="gov-summary">
          <div className="meta">Grants</div>
          {Object.entries(check.summary.roles).map(([role, g]) => (
            <div key={role} className="gov-role">
              <b>{role}</b>:{" "}
              {g === "all sources"
                ? "all sources"
                : Object.entries(g)
                    .map(([s, t]) => `${s} → ${Array.isArray(t) ? t.join(", ") : t}`)
                    .join(" · ")}
            </div>
          ))}
          {Object.keys(check.summary.compliance_tables || {}).length > 0 && (
            <div className="meta" style={{ marginTop: 6 }}>
              Compliance on:{" "}
              {Object.entries(check.summary.compliance_tables)
                .map(([s, ts]) => `${s}(${ts.join(", ")})`)
                .join(" · ")}
            </div>
          )}
        </div>
      )}

      <textarea
        className="gov-yaml"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setCheck(null);
        }}
        spellCheck={false}
        placeholder="Load the template to start from your current setup, or paste a governance document…"
      />
    </section>
  );
}
