import { useEffect, useState } from "react";
import { api } from "../api";

const EXAMPLES = {
  airflow: '{"dag_id":"daily_sales","conf":{}}',
  databricks_jobs: '{"job_id":123,"job_parameters":{}}',
  dbt_cloud: '{"job_id":123,"cause":"Requested in Studio chat"}',
  k8s_spark: '{"main_file":"local:///opt/jobs/etl.py","image":"your-spark-image","arguments":[]}',
  ray: '{"entrypoint":"python etl.py","runtime_env":{"working_dir":"https://…/job.zip"}}',
};
const TERMINAL = new Set(["succeeded", "failed", "canceled", "cancelled", "rejected", "escalated"]);
// Where a queued Spark job runs (its payload's cluster; none named is the
// server's own). A card still waiting for input says what is missing instead.
export const CLUSTER_LABEL = { aks: "Azure Kubernetes Service", gke: "Google Kubernetes Engine",
                               local: "Local Kubernetes", default: "the server's Kubernetes" };

function externalUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

/** "A, B and C". */
const listOf = (names) => (names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0] || "");

/** Compact by design: it opens under the composer, so every line it takes
 *  pushes the conversation up. With nothing connected it is one sentence. */
export function PlatformComposer({ busy, onSubmit, onClose }) {
  const [platforms, setPlatforms] = useState([]);
  const [loaded, setLoaded] = useState(false);
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
      setLoaded(true);
    }).catch((e) => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);
  const selected = platforms.find((p) => p.name === target);
  const connected = platforms.filter((p) => p.configured);
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
    <section className="chat-pipeline chat-platform-panel" aria-label="Submit a platform job">
      <div className="chat-pipeline-heading"><strong>Run on a platform</strong><button type="button" className="chip" onClick={onClose}>Close</button></div>
      {loaded && !connected.length ? (
        <p className="meta chat-platform-none">
          No platform is connected yet — an administrator connects {listOf(platforms.map((p) => p.label || p.name))} on
          the server. Then ask here, or just in the chat: “kick off the daily sales pipeline on Airflow”.
        </p>
      ) : (
        <>
          <div className="chat-platform-row">
            <select aria-label="Execution platform" value={target} disabled={busy}
                    onChange={(e) => { setTarget(e.target.value); setPayload(""); }}>
              {platforms.map((p) => (
                <option key={p.name} value={p.name} disabled={!p.configured}>
                  {p.label || p.name}{p.configured ? "" : " — not connected"}
                </option>
              ))}
            </select>
            <button type="button" className="primary chat-platform-submit"
                    disabled={busy || !selected?.configured || !payload.trim()} onClick={submit}>Submit for approval</button>
          </div>
          <textarea className="chat-platform-payload" aria-label="Platform job parameters" rows={2} value={payload}
            placeholder={EXAMPLES[target]} disabled={busy} onChange={(e) => setPayload(e.target.value)} />
          <p className="meta chat-platform-hint">The DAG or job and its parameters, as JSON — an administrator approves
            every run. Or just ask in the chat: “kick off the daily sales pipeline on Airflow”.</p>
        </>
      )}
      {error && <p className="error">{error}</p>}
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
      {artifact.target === "k8s_spark" && artifact.job_id && CLUSTER_LABEL[artifact.payload?.cluster] && (
        <p className="meta chat-platform-cluster">Runs on {CLUSTER_LABEL[artifact.payload.cluster]}</p>
      )}
      {state === "awaiting_approval" && <p className="meta">The job has not started. An administrator must approve it in <a href="/jobs">Jobs</a>.</p>}
      {["queued", "running"].includes(state) && <p className="meta">The platform has not reported completion yet.</p>}
      {artifact.missing?.length > 0 && <ul>{artifact.missing.map((item, i) => <li key={i}>{String(item)}</li>)}</ul>}
      {state === "needs_input" && artifact.choices?.length > 0 && messageId && (
        <div className="chat-platform-choices" role="group" aria-label="Pick the DAG">
          {artifact.choices.map((dag) => (
            <button key={dag} type="button" className="chip" disabled={busy}
                    onClick={() => onAction("submit", { payload: { ...(artifact.payload || {}), dag_id: dag },
                                                        text: `Run Airflow DAG ${dag}` })}>
              {dag}
            </button>
          ))}
        </div>
      )}
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
