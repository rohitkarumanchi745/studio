// The semantic layer: define each business metric ONCE so every phrasing of the
// same question compiles to the same SQL and returns the same number. Admins edit
// the YAML definition; everyone can browse the metric catalog and try a prompt to
// see the exact SQL it compiles to (the "prompts on chat" consistency, made
// visible). Mirrors the Governance editor's shape and reuses its styles.
import { useEffect, useState } from "react";
import { api } from "../api";

// Type a question, see the canonical SQL it resolves to — no execution. This is
// the promise made inspectable: the same meaning always yields the same SQL.
function TryPrompt({ models }) {
  const [source, setSource] = useState("");
  const [prompt, setPrompt] = useState("");
  const [out, setOut] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!source && models.length) setSource(models[0].source);
  }, [models, source]);

  async function run() {
    setBusy(true);
    setError("");
    setOut(null);
    try {
      const d = await api("/semantic/compile", {
        method: "POST",
        body: JSON.stringify({ source, prompt, table: "*" }),
      });
      setOut(d);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const sources = [...new Set(models.map((m) => m.source))];
  return (
    <div className="query-item" style={{ marginTop: 12 }}>
      <div className="query-body">
        <div className="meta">Try a prompt — see the exact SQL it compiles to</div>
        <div className="job-form-row" style={{ marginTop: 6 }}>
          {sources.length > 1 && (
            <select className="chip" value={source} onChange={(e) => setSource(e.target.value)}>
              {sources.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          )}
          <input
            className="sqllab-prompt"
            style={{ flex: 1 }}
            value={prompt}
            placeholder="e.g. revenue by region, monthly sales trend, orders by product"
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && prompt.trim() && run()}
          />
          <button className="chip" onClick={run} disabled={busy || !prompt.trim() || !source}>
            {busy ? "compiling…" : "→ compile"}
          </button>
        </div>
        {error && <div className="error">{error}</div>}
        {out && !out.resolved && (
          <div className="sqllab-result bad" style={{ marginTop: 8 }}>
            No defined metric matches this prompt — chat would hand it to the agent instead.
          </div>
        )}
        {out && out.resolved && (
          <div style={{ marginTop: 8 }}>
            <div className="inputs-row">
              {out.metrics.map((m) => (
                <span key={m} className="chip chip-input">Σ {m}</span>
              ))}
              {out.dimensions.map((d) => (
                <span key={d.name} className="query-tag">
                  {d.name}{d.grain ? ` (${d.grain})` : ""}
                </span>
              ))}
            </div>
            <pre className="flow-json" style={{ marginLeft: 0, whiteSpace: "pre-wrap", marginTop: 6 }}>
              {out.sql}
            </pre>
            <div className="meta">{out.explain}</div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function Semantic({ user, onClose }) {
  const isAdmin = user?.role === "admin";
  const [text, setText] = useState("");
  const [meta, setMeta] = useState({ loaded: false, source: null });
  const [models, setModels] = useState([]);
  const [check, setCheck] = useState(null); // {ok, errors, models, metrics}
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  function refresh() {
    api("/semantic")
      .then((d) => {
        setText(d.yaml || "");
        setMeta({ loaded: d.loaded, source: d.source });
      })
      .catch((e) => setError(e.message));
    // The role-scoped catalog: what metrics THIS user can actually use.
    api("/semantic/metrics")
      .then((d) => setModels(d.models || []))
      .catch(() => setModels([]));
  }
  useEffect(refresh, []);

  async function loadTemplate() {
    setError("");
    try {
      const d = await api("/semantic/template");
      setText(d.yaml);
      setCheck(null);
      setNote("Loaded a starter definition — edit the metrics to match your data.");
    } catch (e) {
      setError(e.message);
    }
  }

  async function validate() {
    setBusy("validate");
    setError("");
    setNote("");
    try {
      const d = await api("/semantic/validate", {
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
      await api("/semantic", { method: "PUT", body: JSON.stringify({ yaml: text }) });
      setNote("Applied — chat now answers these metrics from one definition.");
      refresh();
    } catch (e) {
      setError(typeof e.message === "string" ? e.message : "Invalid document");
    } finally {
      setBusy("");
    }
  }

  async function clear() {
    if (!confirm("Remove the semantic layer? Chat will go back to the agent writing SQL per question.")) return;
    setBusy("clear");
    try {
      await api("/semantic", { method: "DELETE" });
      setCheck(null);
      setNote("Removed. Metrics are no longer compiled from a shared definition.");
      refresh();
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
          <div className="canvas-title">Semantic layer</div>
          <div className="meta">
            Define each metric once, so every phrasing of the same question compiles to
            the same SQL and returns the same number. {meta.loaded
              ? `Active — loaded from ${meta.source}.`
              : "Not active — chat lets the agent write SQL per question."}
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{String(error)}</div>}
      {note && <div className="meta sqllab-saved">{note}</div>}

      {/* The metric catalog — what everyone can ask for in plain English. */}
      {models.length > 0 ? (
        models.map((m) => (
          <div key={m.source + "." + m.table} className="query-item">
            <div className="query-body">
              <div className="canvas-title" style={{ fontSize: 15 }}>
                {m.source} · {m.table}
              </div>
              <div className="inputs-row" style={{ marginTop: 6 }}>
                <span className="meta">metrics</span>
                {m.metrics.map((mt) => (
                  <span key={mt.name} className="chip chip-input" title={mt.description || mt.expr}>
                    Σ {mt.name} = {mt.agg}({mt.expr})
                  </span>
                ))}
              </div>
              <div className="inputs-row" style={{ marginTop: 4 }}>
                <span className="meta">by</span>
                {m.dimensions.map((d) => (
                  <span key={d.name} className="query-tag">
                    {d.name}{d.grain ? ` (${d.grain})` : ""}
                  </span>
                ))}
              </div>
            </div>
          </div>
        ))
      ) : (
        <div className="meta" style={{ margin: "10px 0" }}>
          No metrics defined yet.{isAdmin ? " Load the template below to start." : " Ask an admin to define them."}
        </div>
      )}

      {models.length > 0 && <TryPrompt models={models} />}

      {/* Admin-only: the definition itself. */}
      {isAdmin && (
        <>
          <div className="gov-actions" style={{ marginTop: 16 }}>
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
                ↺ remove
              </button>
            )}
          </div>

          {check && (
            <div className={"sqllab-result " + (check.ok ? "ok" : "bad")}>
              {check.ok
                ? `✓ Valid — ${check.models} model(s), ${check.metrics} metric(s)`
                : "✗ " + (check.errors || []).join("; ")}
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
            placeholder="Load the template to start, or paste a semantic-layer document…"
          />
        </>
      )}
    </section>
  );
}
