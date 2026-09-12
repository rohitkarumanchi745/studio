import { useEffect, useState } from "react";
import { api } from "../api";

const EXAMPLES = {
  airflow: '{"dag_id":"daily_sales","conf":{}}',
  databricks_jobs: '{"job_id":123,"job_parameters":{}}',
  dbt_cloud: '{"job_id":123,"cause":"Requested in Studio chat"}',
  k8s_spark: '{"main_file":"local:///opt/jobs/etl.py","image":"your-spark-image","arguments":[]}',
};
const TERMINAL = new Set(["succeeded", "failed", "canceled", "cancelled", "rejected", "escalated"]);

function externalUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

export function PlatformComposer({ busy, onSubmit, onClose }) {
  const [platforms, setPlatforms] = useState([]);
  const [target, setTarget] = useState("airflow");
  const [payload, setPayload] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api("/jobs/platforms").then((value) => {
      if (!active) return;
      const options = Array.isArray(value) ? value : value.platforms || [];
      setPlatforms(options);
      setTarget((options.find((p) => p.configured) || options[0])?.name || "airflow");
    }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);
  const selected = platforms.find((p) => p.name === target);
  function submit() {
    try {
      const body = JSON.parse(payload);
      if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Use a JSON object for the job parameters.");
      setError("");
      onSubmit({ platform_action: "submit", platform_target: target, platform_payload: body },
        `Submit this ${selected?.label || target} job for approval`);
    } catch (e) { setError(e.message); }
  }
  return (
    <section className="chat-pipeline" aria-label="Submit a platform job">
      <div className="chat-pipeline-heading"><strong>Run on a platform</strong><button type="button" className="chip" onClick={onClose}>Close</button></div>
      <p className="meta">Choose an existing DAG or job, or provide a platform job definition. An administrator approves it before execution.</p>
      <select aria-label="Execution platform" value={target} disabled={busy} onChange={(e) => { setTarget(e.target.value); setPayload(""); }}>
        {platforms.map((p) => <option key={p.name} value={p.name}>{p.label || p.name}{p.configured ? "" : " (not configured)"}</option>)}
      </select>
      {selected && !selected.configured && <p className="error">Configure this platform's credentials on the server before submitting a job.</p>}
      <textarea className="chat-platform-payload" aria-label="Platform job parameters" rows={4} value={payload}
        placeholder={EXAMPLES[target]} disabled={busy} onChange={(e) => setPayload(e.target.value)} />
      {error && <p className="error">{error}</p>}
      <button type="button" className="primary" disabled={busy || !selected?.configured || !payload.trim()} onClick={submit}>Submit for approval</button>
    </section>
  );
}

export default function ChatPlatformRun({ artifact, busy, messageId, onAction }) {
  const [live, setLive] = useState(artifact.live || null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => {
    let active = true, timer;
    const controller = new AbortController();
    setLive(artifact.live || null);
    setError("");
    setUnavailable(false);
    if (!artifact.job_id) return;
    const started = Date.now();
    const poll = async () => {
      try {
        const value = await api(`/jobs/${encodeURIComponent(artifact.job_id)}/live`, { signal: controller.signal });
        if (!active || !value) return;
        setLive(value);
        setError("");
        if (!TERMINAL.has(value.state) && Date.now() - started < 10 * 60 * 1000) timer = setTimeout(poll, 5000);
      } catch (e) {
        if (!active) return;
        if ([403, 404].includes(e.status)) {
          setLive(null);
          setUnavailable(true);
          setError("This job is unavailable to your account.");
        } else setError("Could not refresh this job. Try again shortly.");
      }
    };
    poll();
    return () => { active = false; clearTimeout(timer); controller.abort(); };
  }, [artifact.job_id, artifact.live, refresh]);
  if (unavailable) return (
    <section className="chat-pipeline" aria-label="Platform job">
      <p className="error">{error}</p>
      <a className="chip" href="/jobs">Open Jobs</a>
    </section>
  );
  const state = live?.state || artifact.status || "unknown";
  const url = externalUrl(live?.url || artifact.job?.result?.url);
  return (
    <section className="chat-pipeline" aria-label="Platform job">
      <div className="chat-pipeline-heading">
        <strong>{artifact.label || artifact.target || "Platform job"}</strong>
        <span className="chat-pipeline-status" role="status">{state.replaceAll("_", " ")}</span>
      </div>
      {state === "awaiting_approval" && <p className="meta">The job has not started. An administrator must approve it in <a href="/jobs">Jobs</a>.</p>}
      {["queued", "running"].includes(state) && <p className="meta">The platform has not reported completion yet.</p>}
      {artifact.missing?.length > 0 && <ul>{artifact.missing.map((item, i) => <li key={i}>{String(item)}</li>)}</ul>}
      {artifact.job?.supervisor_reasons?.length > 0 && <ul>{artifact.job.supervisor_reasons.map((item, i) => <li key={i}>{item}</li>)}</ul>}
      <details><summary>Job parameters</summary><pre className="query-sql">{JSON.stringify(artifact.payload || {}, null, 2)}</pre></details>
      {(live?.detail || artifact.job?.last_error) && <p className="error">{live?.detail || artifact.job.last_error}</p>}
      {live?.logs && <details><summary>Platform logs</summary><pre className="query-sql">{live.logs}</pre></details>}
      {live?.quality?.length > 0 && <ul>{live.quality.map((q, i) => <li key={i}>{q.name}: {q.status}{q.detail ? ` — ${q.detail}` : ""}</li>)}</ul>}
      {error && <p className="error">{error}</p>}
      <div className="chat-platform-actions">
        <a className="chip" href="/jobs">Open Jobs</a>
        {url && <a className="chip" href={url} target="_blank" rel="noreferrer">Open platform run</a>}
        {artifact.job_id && <button type="button" className="chip" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>Refresh status</button>}
        {artifact.job_id && messageId && <button type="button" className="chip" disabled={busy} onClick={() => onAction("status")}>Report status in chat</button>}
        {TERMINAL.has(state) && messageId && <button type="button" className="chip" disabled={busy} onClick={() => onAction("submit")}>Submit another run for approval</button>}
      </div>
    </section>
  );
}
