// Staged pipeline flow — one business request driven through named agents that
// each emit a typed JSON contract:
//   Pipeline planner → PipelineSpec → Code generator → GeneratedArtifact →
//   Validator → ValidationResult → Approval agent/human → DeploymentRequest →
//   Airflow/Databricks → ExecutionResult.  Every stage is an Agent Lightning
// trace. This view shows the chain, each artifact, and where it stopped.
import { useEffect, useState } from "react";
import { api } from "../api";

const ARTIFACT_OF = {
  plan: "spec",
  codegen: "artifact",
  validate: "validation",
  approve: "deployment",
  execute: "execution",
};

const STATUS_TONE = {
  succeeded: "ok",
  awaiting_approval: "warn",
  human_review: "warn",
  escalated: "warn",
  rejected: "bad",
  plan_failed: "bad",
  deploy_failed: "bad",
  rolled_back: "bad",
  execute_failed: "bad",
};

const RUN_MARK = { true: "✓", false: "✗", null: "⏸" };

function StageCard({ stage, flow, expanded, onToggle }) {
  const artifact = flow[ARTIFACT_OF[stage.stage]];
  const tone = stage.ok ? "ok" : "bad";
  return (
    <div className="flow-stage">
      <div className="flow-node" onClick={onToggle}>
        <span className={"flow-dot flow-" + tone}>{stage.ok ? "✓" : "✕"}</span>
        <div className="flow-node-body">
          <div className="flow-node-head">
            <b>{stage.agent}</b>
            <span className="query-tag">{stage.produces}</span>
            {stage.repairs > 0 && <span className="query-tag flow-badge-warn">repaired ×{stage.repairs}</span>}
            {stage.trace_id && <span className="meta flow-trace">trace {String(stage.trace_id).slice(0, 8)}</span>}
          </div>
          {artifact && <StageSummary stage={stage.stage} a={artifact} />}
        </div>
        <span className="flow-caret">{expanded ? "▾" : "▸"}</span>
      </div>
      {expanded && artifact && (
        <pre className="flow-json">{JSON.stringify(artifact, null, 2)}</pre>
      )}
    </div>
  );
}

function StageSummary({ stage, a }) {
  if (stage === "plan")
    return <div className="meta">source <b>{a.source}</b> · {a.steps?.length || 0} steps{a.repo ? ` · repo ${a.repo.name}` : ""}</div>;
  if (stage === "codegen")
    return <div className="meta">{a.language} · {a.mode}{a.mcp_servers?.length ? ` · MCP ${a.mcp_servers.join(", ")}` : ""} · {a.code?.length || 0} chars</div>;
  if (stage === "validate")
    return <div className="meta">{a.ok ? "all checks passed" : `${a.errors?.length || 0} error(s)`} · {a.checks?.length || 0} checks</div>;
  if (stage === "approve")
    return <div className="meta">decision <b>{a.decision}</b> · risk {a.risk} · {a.target}/{a.kind}</div>;
  if (stage === "execute")
    return (
      <div className="meta">
        status <b>{a.status}</b> · {a.executor}
        {a.deploy && ` · deploy ${a.deploy.ok ? "✓" : "✗"}`}
        {a.run && ` · run ${RUN_MARK[String(a.run.ok)]}${a.run.attempts ? ` (${a.run.attempts} tr${a.run.attempts === 1 ? "y" : "ies"})` : ""}`}
        {a.rollback && ` · rollback ${a.rollback.done ? "✓" : "⚠ manual"}`}
        {a.job_id && ` · job ${a.job_id.slice(0, 8)}`}
      </div>
    );
  return null;
}

export default function Flow({ onClose, onOpenJobs }) {
  const [request, setRequest] = useState("");
  const [target, setTarget] = useState("");   // "" = deploy to the planned source
  const [kind, setKind] = useState("sql_script");
  const [busy, setBusy] = useState(false);
  const [flow, setFlow] = useState(null);
  const [expanded, setExpanded] = useState({});
  const [history, setHistory] = useState([]);
  const [error, setError] = useState("");

  const loadHistory = () => api("/flow").then((d) => setHistory(d.flows || [])).catch(() => {});
  useEffect(() => { loadHistory(); }, []);

  async function run() {
    if (!request.trim() || busy) return;
    setBusy(true);
    setError("");
    setFlow(null);
    try {
      const body = { request: request.trim(), kind };
      if (target.trim()) body.target = target.trim();
      const d = await api("/flow/run", { method: "POST", body: JSON.stringify(body) });
      setFlow(d);
      setExpanded({});
      loadHistory();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function open(id) {
    setError("");
    try {
      const d = await api(`/flow/${id}`);
      setFlow(d);
      setExpanded({});
    } catch (e) {
      setError(e.message);
    }
  }

  async function remove(id, e) {
    e.stopPropagation();
    try {
      await api(`/flow/${id}`, { method: "DELETE" });
      setHistory((h) => h.filter((f) => f.id !== id));
      if (flow?.id === id) setFlow(null);
    } catch (err) {
      setError(err.message);
    }
  }

  const tone = flow ? STATUS_TONE[flow.status] || (flow.status === "succeeded" ? "ok" : "warn") : "";

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Pipeline flow</div>
          <div className="meta">
            One request → planner → generator → validator → approval → executor. Each
            stage is a named agent that emits a typed JSON contract, and each is
            recorded as an Agent Lightning trace.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="composer-pill">
        <input
          className="pill-input"
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
          placeholder="e.g. supply-chain status: inventory levels and downtime by plant"
          disabled={busy}
        />
        <button className="primary send-btn" onClick={run} disabled={busy || !request.trim()}>
          {busy ? "…" : "▷ run flow"}
        </button>
      </div>
      <div className="flow-opts">
        <label className="meta">deploy to</label>
        <input className="sqllab-prompt" style={{ maxWidth: 150 }} value={target}
          onChange={(e) => setTarget(e.target.value)} placeholder="planned source" />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="sql_script">sql_script (read-only)</option>
          <option value="spark_job">spark_job (needs approval)</option>
        </select>
      </div>

      {flow && (
        <>
          <div className={"flow-status flow-status-" + tone}>
            {flow.status.replace(/_/g, " ")}
            {flow.status === "awaiting_approval" && (
              <button className="chip" style={{ marginLeft: 10 }} onClick={onOpenJobs}>
                → approve in Jobs
              </button>
            )}
          </div>
          {flow.status === "human_review" && (
            <div className="meta flow-note">
              Automated repair was exhausted — the pipeline needs human review before it can proceed. You were alerted by email.
            </div>
          )}
          {flow.status === "deploy_failed" && (
            <div className="meta flow-note">Deployment failed — the error was reported and policy was not bypassed; nothing ran.</div>
          )}
          {flow.status === "rolled_back" && (
            <div className="meta flow-note">The run kept failing after retries — it was stopped, you were alerted, and it was rolled back.</div>
          )}
          <div className="flow-chain">
            {flow.stages.map((s) => (
              <StageCard key={s.stage} stage={s} flow={flow} expanded={!!expanded[s.stage]}
                onToggle={() => setExpanded((e) => ({ ...e, [s.stage]: !e[s.stage] }))} />
            ))}
          </div>
        </>
      )}

      {history.length > 0 && (
        <>
          <div className="meta" style={{ margin: "16px 0 6px" }}>Recent flows</div>
          <div className="query-list">
            {history.map((f) => (
              <div key={f.id} className="query-card flow-hist" onClick={() => open(f.id)}>
                <div className="query-head">
                  <div className="query-title">
                    {f.request}
                    <span className="query-tag">{f.target}/{f.kind}</span>
                  </div>
                  <div className="query-actions">
                    <span className={"query-tag flow-badge-" + (STATUS_TONE[f.status] || "warn")}>
                      {f.status.replace(/_/g, " ")}
                    </span>
                    <button className="chip ctx-danger" onClick={(e) => remove(f.id, e)}>✕</button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}
