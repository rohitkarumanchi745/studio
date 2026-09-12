import { useEffect, useState } from "react";
import { api } from "../api";
import PipelineRecovery, { recoveryIsActive } from "./PipelineRecovery";

const ACTIVE = new Set(["awaiting_approval", "approved", "deploying", "launching", "queued", "running"]);
const TERMINAL = new Set(["succeeded", "failed", "canceled", "cancelled", "rejected", "escalated"]);
const MEMORY_LABEL = {
  exact_revalidated: "Revalidated a previously successful recipe",
  exact_reverified: "Reverified a previously successful recipe",
  model_adapted: "Drafted an adaptation of a previously successful recipe; review it before approval",
  adaptation_required: "Found a related successful recipe; adaptation is still required",
  adaptation_unconfirmed: "Found a related successful recipe; adaptation has not been confirmed",
};

function externalUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export default function ChatWorkflow({ pipeline, messageId, busy, onRun, onStatus }) {
  const [live, setLive] = useState(pipeline.live || null);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let timer, stopped = false;
    const abort = new AbortController();
    setLive(pipeline.live || null);
    setUnavailable(false);
    setError("");
    if (!pipeline.job_id) return;
    const started = Date.now();
    const poll = async () => {
      try {
        const data = await api(`/jobs/${encodeURIComponent(pipeline.job_id)}/live`, { signal: abort.signal });
        if (stopped || !data) return;
        setLive(data);
        setError("");
        if ((!TERMINAL.has(data.state) || recoveryIsActive(data.job?.recovery || data.recovery)) && Date.now() - started < 600000) timer = setTimeout(poll, 5000);
      } catch (error) {
        if (stopped) return;
        if ([403, 404].includes(error.status)) {
          setLive(null);
          setUnavailable(true);
        } else {
          setError("Could not refresh execution status. The last reported state may be stale; refresh to retry.");
        }
      }
    };
    poll();
    return () => { stopped = true; clearTimeout(timer); abort.abort(); };
  }, [pipeline.job_id, pipeline.live, refresh]);
  if (unavailable) return <section className="chat-pipeline"><p>This pipeline is no longer available to your account.</p></section>;
  const state = live?.state || pipeline.status || "unknown";
  const recovery = live?.job?.recovery || live?.recovery || pipeline.recovery;
  const tasks = Array.isArray(pipeline.tasks) ? pipeline.tasks : [];
  const runnable = tasks.length > 0 && !["blocked", "needs_input"].includes(pipeline.status)
    && !error && !recoveryIsActive(recovery) && !ACTIVE.has(state) && (pipeline.job_id
      ? ["succeeded", "failed", "canceled", "cancelled", "rejected"].includes(state)
      : ["ready", "needs_configuration"].includes(state));
  const url = externalUrl(live?.url);
  function download() {
    const blob = new Blob([pipeline.artifact.source], { type: "text/x-python;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = pipeline.artifact.filename || "studio_dag.py";
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <section className="chat-pipeline" aria-label="Generated Airflow pipeline">
    <div className="chat-pipeline-heading"><strong>{pipeline.name || "Airflow pipeline"}</strong><span role="status">{state?.replaceAll("_", " ")}</span></div>
    <p className="meta">Dependency-aware SQL tasks. Publishing and execution require administrator approval. Manual runs only.</p>
    {pipeline.memory && <p className="meta">{MEMORY_LABEL[pipeline.memory.reuse_type] || "Related successful recipe found"}: {pipeline.memory.matched_prompt}</p>}
    {[...(pipeline.missing || []), ...(pipeline.errors || [])].map((issue, i) => <p className="error" key={i}>{typeof issue === "string" ? issue : JSON.stringify(issue)}</p>)}
    {(pipeline.warnings || []).map((warning, i) => <p className="meta" key={i}>{typeof warning === "string" ? warning : JSON.stringify(warning)}</p>)}
    {tasks.length > 0 && <table className="data-table"><thead><tr><th>Task</th><th>Runs after</th><th>Produces</th></tr></thead><tbody>{tasks.map((task) => <tr key={task.id}><td>{task.id}</td><td>{task.depends_on?.join(", ") || "Start"}</td><td>{task.produces || "Read only"}</td></tr>)}</tbody></table>}
    {tasks.map((task) => <details key={task.id}><summary>{task.name || task.id} · {task.source}</summary><pre className="query-sql">{task.sql}</pre></details>)}
    {state === "awaiting_approval" && <p className="meta">Nothing is published or running yet. Approve the request in Jobs.</p>}
    {state === "approved" && <p className="meta">Approval is recorded. Waiting for the publication worker; no Airflow run has been confirmed.</p>}
    {state === "deploying" && <p className="meta">Published; waiting for Airflow registration. No run has started.</p>}
    {state === "launching" && <p className="meta">The trigger request is in progress. Airflow has not confirmed the run yet.</p>}
    {state === "unknown" && <p className="meta">Execution state is unknown. Check Airflow or refresh before requesting another run.</p>}
    {state === "escalated" && <p className="error">An administrator must inspect the job and its Airflow state before deciding whether to retry.</p>}
    {state === "failed" && !recovery && <p className="error">Ask “repair this pipeline” to draft a correction. The failed run remains recorded.</p>}
    <PipelineRecovery recovery={recovery} />
    {live?.detail && <p className="meta">{live.detail}</p>}
    {live?.logs && <details><summary>Airflow logs</summary><pre className="query-sql">{live.logs}</pre></details>}
    {error && <p className="error">{error}</p>}
    <div className="chat-platform-actions">
      <button type="button" className="primary" disabled={busy || !messageId || !runnable || !onRun} onClick={onRun}>{recoveryIsActive(recovery) ? "Agent handling recovery…" : pipeline.job_id ? (recovery?.child_job_id ? "Submit original for approval" : "Submit another run for approval") : "Submit for approval"}</button>
      {pipeline.artifact?.source && <button type="button" className="chip" onClick={download}>Download DAG</button>}
      {url && <a className="chip" href={url} target="_blank" rel="noreferrer">Open Airflow run</a>}
      {pipeline.job_id && <><a className="chip" href="/jobs">Open Jobs</a>
        <button type="button" className="chip" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>Refresh status</button>
        <button type="button" className="chip" disabled={busy || !messageId || !onStatus} onClick={onStatus}>Report status in chat</button></>}
    </div>
  </section>;
}
