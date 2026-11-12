// Build Python from existing scripts. Agents pick up your scripts through the
// registered MCP servers and generate Python in that context. The output is a
// deliverable — Studio never executes arbitrary Python itself. To RUN it,
// submit it via Jobs, or "Run on Ray Jobs" below: either way it becomes a
// platform run that a human approves before any cluster sees it.
import { useEffect, useState } from "react";
import { api } from "../api";

/** What Build Python submits to Jobs: the module as a Ray Jobs script. */
export function rayRunPayload(python) {
  return { kind: "platform_run", target: "ray",
           script: JSON.stringify({ script: python, metadata: { source: "build_python" } }) };
}

export default function PyBuild({ onClose }) {
  const [prompt, setPrompt] = useState("");
  const [context, setContext] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null); // {python, mode, mcp_servers, note}
  const [error, setError] = useState("");
  // Ray Jobs, as /jobs/platforms lists it: null until known, and for a role
  // that cannot submit platform runs (the listing is refused).
  const [ray, setRay] = useState(null);
  const [submitted, setSubmitted] = useState("");

  useEffect(() => {
    api("/jobs/platforms")
      .then((d) => setRay((Array.isArray(d) ? d : d.platforms || []).find((p) => p.name === "ray") || null))
      .catch(() => setRay(null));
  }, []);

  async function build() {
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setError("");
    setSubmitted("");
    try {
      const d = await api("/python/build", {
        method: "POST",
        body: JSON.stringify({ prompt: prompt.trim(), context: context.trim() || undefined }),
      });
      setResult(d);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function runOnRay() {
    if (!result?.python || busy) return;
    setBusy(true);
    setError("");
    setSubmitted("");
    try {
      const job = await api("/jobs", { method: "POST", body: JSON.stringify(rayRunPayload(result.python)) });
      setSubmitted(`Submitted to Ray Jobs as job ${String(job.id || "").slice(0, 8)} — an administrator `
        + "approves it in Jobs before the cluster runs it; its status and logs show there.");
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Build Python</div>
          <div className="meta">
            The agent picks up your existing scripts through registered MCP servers
            and builds Python in that style. To run it, submit it via Jobs or send it to Ray Jobs —
            a human approves it first.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      <label className="meta sqllab-label">What should the Python do?</label>
      <input
        className="sqllab-prompt"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && build()}
        placeholder="e.g. incremental load from S3 to the warehouse with a watermark"
      />
      <label className="meta sqllab-label">Existing script for context (optional)</label>
      <textarea
        className="gov-yaml"
        style={{ minHeight: 120 }}
        value={context}
        onChange={(e) => setContext(e.target.value)}
        spellCheck={false}
        placeholder="Paste a script the agent should follow the style of — or register an MCP scripts server so it reads them automatically."
      />

      <div className="gov-actions">
        <button className="chip chip-on" onClick={build} disabled={busy || !prompt.trim()}>
          {busy ? "building…" : "⚙ build Python"}
        </button>
        {result && (
          <button className="chip" onClick={() => navigator.clipboard?.writeText(result.python)}>
            ⧉ copy
          </button>
        )}
        {result && ray && (
          <button className="chip" onClick={runOnRay} disabled={busy || !ray.configured}
            title={ray.configured
              ? "Submit it as a Ray Jobs run — an administrator approves it in Jobs first"
              : "Ray Jobs is not configured on the server (RAY_ADDRESS)"}>
            ▶ Run on Ray Jobs
          </button>
        )}
      </div>

      {result && ray && !ray.configured && (
        <div className="meta">Ray Jobs is not configured on this deployment — set RAY_ADDRESS to a Ray head's dashboard to run modules there.</div>
      )}
      {submitted && <div className="meta share-notice">{submitted}</div>}

      {result && (
        <>
          <div className="meta" style={{ margin: "6px 0" }}>
            {result.mode === "agent" ? "Generated by the agent" : "Scaffold (no LLM key)"}
            {result.mcp_servers?.length > 0
              ? ` · MCP context: ${result.mcp_servers.join(", ")}`
              : " · no MCP scripts server registered"}
            {result.note ? ` · ${result.note}` : ""}
          </div>
          <pre className="query-sql" style={{ whiteSpace: "pre", maxHeight: 480, overflow: "auto" }}>
            {result.python}
          </pre>
        </>
      )}
    </section>
  );
}
