// Build tool / MCP — describe a need, and an agent authors either a self-contained
// MCP server (stdio) or a single tool, GROUNDED in the data sources + existing MCP
// servers this user can actually reach. The generated code is a DELIVERABLE, not
// something that runs: it is inert until a human admin approves the supervised job
// in Jobs. Only on approval is it written to a sandbox and registered as a stdio
// MCP server that agents pick up. Studio never exec/imports the generated code.
import { useEffect, useState } from "react";
import { api } from "../api";

// draft → awaiting_approval → registered  (rejected is terminal)
const STATUS_TONE = {
  draft: "warn",
  awaiting_approval: "warn",
  registered: "ok",
  rejected: "bad",
};
const STATUS_LABEL = {
  draft: "draft",
  awaiting_approval: "awaiting approval",
  registered: "registered",
  rejected: "rejected",
};

export default function ToolBuilder({ onClose, onOpenJobs }) {
  const [prompt, setPrompt] = useState("");
  const [kind, setKind] = useState("mcp"); // 'mcp' | 'tool'
  const [anchor, setAnchor] = useState(""); // optional source name
  const [ground, setGround] = useState({ sources: [], existing_servers: [] });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null); // freshly built artifact {id,kind,code,mode,...}
  const [artifacts, setArtifacts] = useState([]);
  const [open, setOpen] = useState(null); // {…full artifact incl code, job}
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState(false);

  // Grounding preview — what the generation is scoped to (RBAC-filtered server-side).
  function loadGround() {
    api("/toolbuilder/sources")
      .then((d) => setGround({ sources: d.sources || [], existing_servers: d.existing_servers || [] }))
      .catch(() => {});
  }
  // The user's built artifacts (owner-scoped). A 404 means the feature isn't
  // deployed yet — degrade to a friendly notice rather than an error wall.
  function loadList() {
    api("/toolbuilder")
      .then((d) => {
        setArtifacts(d.artifacts || []);
        setUnavailable(false);
      })
      .catch((e) => {
        if (/404|not found/i.test(e.message || "")) setUnavailable(true);
        else setError(e.message);
      });
  }
  useEffect(() => {
    loadGround();
    loadList();
  }, []);

  // Poll while anything is mid-approval, so 'awaiting_approval' flips to
  // 'registered' the moment an admin approves the supervised job in Jobs.
  useEffect(() => {
    const pending = artifacts.some((a) => a.status === "awaiting_approval");
    if (!pending) return;
    const h = setInterval(() => {
      loadList();
      if (open?.id) refreshOpen(open.id);
    }, 5000);
    return () => clearInterval(h);
  }, [artifacts, open?.id]);

  async function build() {
    if (!prompt.trim() || busy) return;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const d = await api("/toolbuilder/build", {
        method: "POST",
        body: JSON.stringify({ prompt: prompt.trim(), kind }),
      });
      setResult(d);
      setOpen(null);
      loadList();
    } catch (e) {
      if (/404|not found/i.test(e.message || "")) setUnavailable(true);
      else setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function submit(id) {
    setError("");
    try {
      const body = anchor ? { source: anchor } : {};
      const d = await api(`/toolbuilder/${id}/submit`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      // Reflect awaiting_approval immediately on the freshly-built preview.
      if (result?.id === id) setResult((r) => ({ ...r, status: "awaiting_approval", job: d.job }));
      if (open?.id === id) setOpen((o) => ({ ...o, status: "awaiting_approval", job: d.job }));
      loadList();
    } catch (e) {
      setError(e.message);
    }
  }

  async function refreshOpen(id) {
    try {
      const d = await api(`/toolbuilder/${id}`);
      setOpen(d);
    } catch (e) {
      if (/404|not found/i.test(e.message || "")) {
        setOpen(null);
        loadList();
      }
    }
  }

  async function view(id) {
    if (open?.id === id) {
      setOpen(null);
      return;
    }
    setError("");
    await refreshOpen(id);
  }

  async function remove(id, e) {
    e?.stopPropagation();
    if (!confirm("Remove this built tool? If it was registered, the MCP server is unregistered and its sandbox file deleted.")) return;
    setError("");
    try {
      await api(`/toolbuilder/${id}`, { method: "DELETE" });
      setArtifacts((a) => a.filter((x) => x.id !== id));
      if (open?.id === id) setOpen(null);
      if (result?.id === id) setResult(null);
    } catch (err) {
      setError(err.message);
    }
  }

  const anchorHint = anchor || ground.sources[0]?.name;

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Build tool / MCP</div>
          <div className="meta">
            Describe a capability and an agent authors an MCP server (or a single tool),
            grounded in the sources and tools you can reach. The code is a deliverable —
            it stays inert until a human admin approves it in Jobs, and only then runs as
            an isolated stdio subprocess that agents load. Studio never executes it itself.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{String(error)}</div>}
      {unavailable && (
        <div className="sqllab-result bad" style={{ margin: "8px 0" }}>
          The tool builder backend isn’t available on this deployment yet — the surface is
          shown, but generation and approval require the <code>/toolbuilder</code> API.
        </div>
      )}

      {/* Grounding context — exactly what generation is scoped to (RBAC-filtered). */}
      <div className="tb-ground">
        <span className="meta">grounded in</span>
        {ground.sources.length === 0 && (
          <span className="meta">no accessible data source</span>
        )}
        {ground.sources.map((s) => (
          <span
            key={s.name}
            className="chip chip-input"
            title={(s.tables || []).join(", ") || "no tables listed"}
          >
            ◈ {s.name}
            {s.dialect ? ` · ${s.dialect}` : ""}
            {s.tables?.length ? ` · ${s.tables.length} tables` : ""}
          </span>
        ))}
        {ground.existing_servers.length > 0 && (
          <>
            <span className="meta">reuse</span>
            {ground.existing_servers.map((n) => (
              <span key={n} className="query-tag">⚇ {n}</span>
            ))}
          </>
        )}
      </div>

      {/* Composer: describe the need, pick a kind + optional source anchor, generate. */}
      <label className="meta sqllab-label" style={{ marginTop: 12 }}>What should the tool do?</label>
      <textarea
        className="gov-yaml"
        style={{ minHeight: 96 }}
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        spellCheck={false}
        placeholder="e.g. a tool that looks up a customer's open orders and lifetime value from the warehouse by email"
      />

      <div className="gov-actions">
        <div className="tb-kind">
          <button
            className={"chip" + (kind === "mcp" ? " chip-on" : "")}
            onClick={() => setKind("mcp")}
            title="A self-contained stdio MCP server exposing one or more tools"
          >
            ⚇ MCP server
          </button>
          <button
            className={"chip" + (kind === "tool" ? " chip-on" : "")}
            onClick={() => setKind("tool")}
            title="A single tool function (wrapped into a minimal server on registration)"
          >
            ⟨⟩ single tool
          </button>
        </div>
        {ground.sources.length > 0 && (
          <select
            className="chip"
            value={anchor}
            onChange={(e) => setAnchor(e.target.value)}
            title="RBAC anchor — the source this tool is scoped to (defaults to the first)"
          >
            <option value="">anchor: {ground.sources[0]?.name} (default)</option>
            {ground.sources.map((s) => (
              <option key={s.name} value={s.name}>anchor: {s.name}</option>
            ))}
          </select>
        )}
        <button className="chip chip-on" onClick={build} disabled={busy || !prompt.trim()}>
          {busy ? "generating…" : "⚙ generate"}
        </button>
      </div>

      {/* Freshly generated code — a preview only; nothing is live. */}
      {result && (
        <div className="query-item" style={{ marginTop: 6 }}>
          <div className="query-body" style={{ paddingTop: 12 }}>
            <div className="tb-result-head">
              <span className={"flow-status flow-status-" + (STATUS_TONE[result.status] || "warn")} style={{ margin: 0 }}>
                {STATUS_LABEL[result.status] || result.status || "draft"}
              </span>
              <span className="query-tag">{result.kind === "tool" ? "single tool" : "MCP server"}</span>
              <span className="meta">
                {result.mode === "agent" ? "generated by the agent" : "scaffold (no LLM key)"}
                {result.grounded_sources?.length ? ` · scoped to ${result.grounded_sources.join(", ")}` : ""}
                {result.mcp_servers?.length ? ` · MCP context: ${result.mcp_servers.join(", ")}` : ""}
                {result.note ? ` · ${result.note}` : ""}
              </span>
            </div>
            <pre className="flow-json" style={{ margin: "8px 0 0", maxHeight: 420 }}>{result.code}</pre>
            <div className="query-actions">
              <button className="chip" onClick={() => navigator.clipboard?.writeText(result.code)}>⧉ copy</button>
              {result.status === "draft" && (
                <button className="chip chip-on" onClick={() => submit(result.id)}>
                  ⇧ submit for approval
                </button>
              )}
              {result.status === "awaiting_approval" && (
                <>
                  <span className="meta">waiting for an admin to approve in Jobs</span>
                  {onOpenJobs && <button className="chip" onClick={onOpenJobs}>→ open Jobs</button>}
                </>
              )}
            </div>
            {result.status === "draft" && (
              <div className="meta">
                Submitting scopes this to <b>{anchorHint || "your source"}</b> and files a supervised
                job — it will not run until a human admin approves it.
              </div>
            )}
          </div>
        </div>
      )}

      {/* The user's built tools — status + open code + remove. */}
      {artifacts.length > 0 && (
        <>
          <div className="meta" style={{ margin: "18px 0 6px" }}>Your built tools</div>
          <div className="query-list">
            {artifacts.map((a) => (
              <div key={a.id} className="query-card">
                <div className="query-head" onClick={() => view(a.id)}>
                  <div className="query-title">
                    {a.prompt}
                    <span className="query-tag">{a.kind === "tool" ? "single tool" : "MCP server"}</span>
                    {a.source && <span className="query-tag">◈ {a.source}</span>}
                    {a.status === "registered" && a.server_name && (
                      <span className="query-tag" title="Live MCP server providing agent tools">⚇ {a.server_name}</span>
                    )}
                  </div>
                  <div className="query-actions" style={{ margin: 0 }}>
                    <span className={"flow-badge-" + (STATUS_TONE[a.status] || "warn")} style={{ fontSize: 11, fontWeight: 600 }}>
                      {STATUS_LABEL[a.status] || a.status}
                    </span>
                    <button className="chip ctx-danger" onClick={(e) => remove(a.id, e)}>✕</button>
                  </div>
                </div>

                {open?.id === a.id && (
                  <div className="query-body">
                    {a.status === "registered" ? (
                      <div className="sqllab-result ok" style={{ marginBottom: 8 }}>
                        ✓ Live — registered as MCP server <b>{a.server_name}</b>. Agents now load its
                        tool(s) as an isolated stdio subprocess.
                      </div>
                    ) : a.status === "awaiting_approval" ? (
                      <div className="sqllab-result bad" style={{ background: "transparent", color: "var(--warn)", marginBottom: 8 }}>
                        ⏸ Waiting for a human admin to approve the supervised job in Jobs — nothing is live.
                        {open.job?.id && <> Job <b>{String(open.job.id).slice(0, 8)}</b> · {open.job.status}.</>}
                        {onOpenJobs && (
                          <button className="chip" style={{ marginLeft: 8 }} onClick={onOpenJobs}>→ open Jobs</button>
                        )}
                      </div>
                    ) : a.status === "rejected" ? (
                      <div className="sqllab-result bad" style={{ marginBottom: 8 }}>
                        ✗ An admin rejected this — it was never registered.
                      </div>
                    ) : null}

                    {open.job?.supervisor_reasons?.length > 0 && (
                      <div className="meta" style={{ marginBottom: 6 }}>
                        supervisor: {open.job.supervisor_reasons.join("; ")}
                      </div>
                    )}

                    <pre className="flow-json" style={{ margin: 0, maxHeight: 420 }}>
                      {open.code || "(code unavailable)"}
                    </pre>
                    <div className="query-actions">
                      <button className="chip" onClick={() => navigator.clipboard?.writeText(open.code || "")}>⧉ copy</button>
                      {a.status === "draft" && (
                        <button className="chip chip-on" onClick={() => submit(a.id)}>⇧ submit for approval</button>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}

      {artifacts.length === 0 && !unavailable && (
        <div className="meta" style={{ margin: "14px 0" }}>
          No built tools yet — describe a capability above and generate one.
        </div>
      )}
    </section>
  );
}
