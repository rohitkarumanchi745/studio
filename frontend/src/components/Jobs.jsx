// Supervised jobs. Agents submit scripts / Spark jobs / platform runs
// (Airflow, Databricks Jobs, dbt Cloud, K8s Spark) against real environments;
// a supervisor agent reviews each one (read-only auto-approves, writes and
// jobs need a human). Repeated failures escalate — an admin approves a retry
// or rejects. Admins are the human in the loop. Platform runs expose a live
// panel (status / metrics / logs / quality checks) via GET /jobs/{id}/live.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import PipelineRecovery, { recoveryBlocksJobDecision, recoveryIsActive } from "./PipelineRecovery";

const STATUS_LABEL = {
  succeeded: "✓ succeeded",
  running: "running…",
  approved: "approved — awaiting publication worker…",
  deploying: "published — waiting for Airflow…",
  launching: "requesting Airflow run…",
  queued: "queued…",
  failed: "✕ failed",
  canceled: "canceled",
  cancelled: "canceled",
  retrying: "retrying…",
  awaiting_approval: "⏳ awaiting approval",
  escalated: "⚠ escalated — needs a human",
  rejected: "✕ rejected",
};
const ACTIVE = new Set(["awaiting_approval", "approved", "deploying", "launching", "queued", "running", "retrying"]);
const TERMINAL = new Set(["succeeded", "failed", "canceled", "cancelled", "rejected", "escalated"]);

function externalUrl(value) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}

// Payload shapes per platform — mirrors the platforms.py module docstring.
const PAYLOAD_PLACEHOLDER = {
  airflow:
    '{"dag_id": "etl_daily", "conf": {}}\n(conf optional)',
  databricks_jobs:
    '{"run_name": "nightly_etl", "tasks": [{"task_key": "t1", "spark_python_task": {"python_file": "dbfs:/jobs/etl.py"}}]}\n(a Jobs 2.1 runs/submit body)',
  dbt_cloud:
    '{"job_id": 123, "cause": "why"}\n(job_id falls back to DBT_CLOUD_JOB_ID; extra keys like steps_override pass through)',
  k8s_spark:
    '{"main_file": "local:///opt/jobs/etl.py", "type": "Python", "image": "spark:3.5.0", "arguments": []}\n(shorthand — or paste a full SparkApplication manifest)',
};

// Live status panel for a platform run. Polls every ~5s while the run is
// queued/running; stops on a terminal state or unmount (card collapsed).
function PlatformLive({ jobId, onUnavailable }) {
  const [live, setLive] = useState(null);
  const [err, setErr] = useState("");
  const [showLogs, setShowLogs] = useState(false);

  useEffect(() => {
    let timer = null;
    let gone = false;
    const abort = new AbortController();
    const started = Date.now();
    setLive(null);
    setErr("");
    setShowLogs(false);
    const tick = () =>
      api(`/jobs/${encodeURIComponent(jobId)}/live`, { signal: abort.signal })
        .then((d) => {
          if (gone || !d) return;
          setErr("");
          setLive(d);
          if ((!TERMINAL.has(d.state) || recoveryIsActive(d.job?.recovery || d.recovery)) && Date.now() - started < 600000) {
            timer = setTimeout(tick, 5000);
          }
        })
        .catch((e) => {
          if (gone) return;
          if ([403, 404].includes(e.status)) {
            setLive(null);
            setShowLogs(false);
            setErr("This job is no longer available to your account.");
            onUnavailable?.(jobId);
          } else setErr("Could not refresh this job. Reopen live status to try again.");
        });
    tick();
    return () => {
      gone = true;
      if (timer) clearTimeout(timer);
      abort.abort();
    };
  }, [jobId]);

  if (err) return <div className="error">{err}</div>;
  if (!live) return <div className="meta">fetching live status…</div>;

  const metrics = Object.entries(live.metrics || {}).filter(
    ([, v]) => typeof v !== "object"
  );
  const quality = Array.isArray(live.quality) ? live.quality : [];
  const recovery = live.job?.recovery || live.recovery;
  const polling = !TERMINAL.has(live.state) || recoveryIsActive(recovery);
  const url = externalUrl(live.url);

  return (
    <div className="live-panel">
      <div className="live-head">
        <span className={"job-status job-" + live.state}>{live.state}</span>
        {metrics.map(([k, v]) => (
          <span key={k} className="query-tag">{k}: {String(v)}</span>
        ))}
        {url && (
          <a className="chip" href={url} target="_blank" rel="noreferrer">
            ↗ open run
          </a>
        )}
        {live.logs && (
          <button className="chip" onClick={() => setShowLogs(!showLogs)}>
            {showLogs ? "▾ hide logs" : "▸ logs"}
          </button>
        )}
        {polling && <span className="meta">Refreshes every 5s for up to 10 minutes. Reopen to resume.</span>}
      </div>
      {live.detail && <div className="meta live-detail">{live.detail}</div>}
      {live.state === "approved" && <div className="meta live-detail">Approval is recorded. DAG publication is queued for the worker; this does not mean an Airflow run has started.</div>}
      <PipelineRecovery recovery={recovery} />
      {showLogs && <pre className="query-sql live-logs">{live.logs}</pre>}
      {quality.length > 0 && (
        <div className="qc-list">
          <div className="meta">Quality checks</div>
          {quality.map((q, i) => (
            <div key={i} className="qc-row">
              <span className={"qc-badge qc-" + (q.status || "unknown")}>
                {q.status || "?"}
              </span>
              <span className="qc-name">{q.name}</span>
              {q.detail && <span className="qc-detail">{q.detail}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Jobs({ onClose }) {
  const [jobs, setJobs] = useState(null);
  const [canApprove, setCanApprove] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState("");
  const [liveFor, setLiveFor] = useState(null);

  // Submit form
  const [sources, setSources] = useState([]);
  const [platforms, setPlatforms] = useState([]);
  const [kind, setKind] = useState("sql_script");
  const [target, setTarget] = useState("demo");
  const [platform, setPlatform] = useState("");
  const [script, setScript] = useState("");

  const load = useCallback((signal) =>
    api("/jobs", signal ? { signal } : {})
      .then((d) => {
        if (!d || signal?.aborted) return;
        setJobs(d.jobs || []);
        setCanApprove(!!d.can_approve);
      })
      .catch((e) => {
        if (signal?.aborted) return;
        if ([403, 404].includes(e.status)) {
          setJobs([]);
          setCanApprove(false);
          setOpen(null);
          setLiveFor(null);
        }
        setError(e.message);
      }), []);

  useEffect(() => {
    const abort = new AbortController();
    load(abort.signal);
    api("/catalog/sources", { signal: abort.signal }).then((d) => {
      if (!abort.signal.aborted && Array.isArray(d)) setSources(d);
    }).catch(() => {});
    api("/jobs/platforms", { signal: abort.signal })
      .then((d) => {
        if (!d || abort.signal.aborted) return;
        const ps = Array.isArray(d) ? d : d.platforms || [];
        setPlatforms(ps);
        const first = ps.find((p) => p.configured) || ps[0];
        if (first) setPlatform((cur) => cur || first.name);
      })
      .catch(() => {});
    return () => abort.abort();
  }, [load]);

  useEffect(() => {
    if (!jobs?.some((job) => ACTIVE.has(job.status) || recoveryIsActive(job.recovery))) return;
    const abort = new AbortController();
    const timer = setTimeout(() => load(abort.signal), 5000);
    return () => { clearTimeout(timer); abort.abort(); };
  }, [jobs, load]);

  const unavailable = (id) => {
    setJobs((current) => current?.filter((job) => job.id !== id) || []);
    setOpen((current) => current === id ? null : current);
    setLiveFor((current) => current === id ? null : current);
    setError("A job is no longer available to your account; its cached details were cleared.");
  };

  async function submit() {
    const tgt = kind === "platform_run" ? platform : target;
    if (!script.trim() || !tgt || busy) return;
    setBusy("submit");
    setError("");
    try {
      await api("/jobs", {
        method: "POST",
        body: JSON.stringify({ kind, target: tgt, script: script.trim() }),
      });
      setScript("");
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function decide(id, action) {
    setBusy(id + action);
    setError("");
    try {
      await api(`/jobs/${encodeURIComponent(id)}/${action}`, { method: "POST" });
      load();
    } catch (e) {
      if ([403, 404].includes(e.status)) unavailable(id);
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const platformLabel = (name) =>
    (platforms.find((p) => p.name === name) || {}).label || name;

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Jobs</div>
          <div className="meta">
            Scripts, generated Airflow DAGs, Spark jobs and platform runs against real environments. A
            supervisor agent reviews every job; writes and jobs need human
            approval, and repeated failures escalate. {canApprove ? "You can approve or reject." : "An admin approves."}
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}

      <div className="pl-build">
        <div className="job-form-row">
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="sql_script">SQL script</option>
            <option value="spark_job">Spark job (Databricks)</option>
            <option value="platform_run">Platform run</option>
          </select>
          {kind === "platform_run" ? (
            <select value={platform} onChange={(e) => setPlatform(e.target.value)}>
              {platforms.length === 0 && <option value="">no platforms</option>}
              {platforms.map((p) => (
                <option key={p.name} value={p.name} disabled={!p.configured}>
                  {p.label}{!p.configured ? " (not configured)" : ""}
                </option>
              ))}
            </select>
          ) : (
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              {sources.filter((s) => s.allowed).map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}{!s.configured ? " (not connected)" : ""}
                </option>
              ))}
            </select>
          )}
          <button className="chip chip-on" onClick={submit} disabled={busy === "submit" || !script.trim()}>
            {busy === "submit" ? "submitting…" : "⚙ submit to supervisor"}
          </button>
        </div>
        <textarea
          className="gov-yaml"
          style={{ minHeight: 120 }}
          value={script}
          onChange={(e) => setScript(e.target.value)}
          spellCheck={false}
          placeholder={
            kind === "platform_run"
              ? PAYLOAD_PLACEHOLDER[platform] || "JSON payload for the selected platform"
              : kind === "spark_job"
              ? '{"run_name":"nightly_etl","tasks":[{"task_key":"t","spark_python_task":{"python_file":"dbfs:/jobs/etl.py"}}]}'
              : "SELECT … (read-only auto-approves) — or an UPDATE / CREATE (needs human approval)"
          }
        />
      </div>

      {jobs === null ? (
        <div className="meta">loading…</div>
      ) : jobs.length === 0 ? (
        <div className="empty">
          <div className="empty-title">No jobs yet</div>
          <div className="empty-sub">Submit a script, Spark job or platform run above; the supervisor reviews it.</div>
        </div>
      ) : (
        <div className="query-list">
          {jobs.map((j) => (
            <div key={j.id} id={`job-${j.id}`} className="query-card">
              <div className="query-head" onClick={() => setOpen(open === j.id ? null : j.id)}>
                <div className="query-title">
                  <span className={"job-status job-" + j.status}>
                    {STATUS_LABEL[j.status] || j.status}
                  </span>
                  <span className="query-tag">{j.kind}</span>
                  <span className="query-tag">
                    {["platform_run", "airflow_dag"].includes(j.kind) ? platformLabel(j.target) : j.target}
                  </span>
                  <span className={"job-risk job-risk-" + j.risk}>{j.risk}</span>
                  {j.attempts > 0 && (
                    <span className="meta">{j.attempts}/{j.max_retries + 1} attempts</span>
                  )}
                  {recoveryIsActive(j.recovery) && <span className="meta">agent recovery: {j.recovery.state.replaceAll("_", " ")}</span>}
                </div>
                {canApprove && !recoveryBlocksJobDecision(j.recovery, j.id) && (j.status === "awaiting_approval" || j.status === "escalated") && (
                  <div className="query-actions" onClick={(e) => e.stopPropagation()}>
                    <button className="chip chip-on" onClick={() => decide(j.id, "approve")}
                      disabled={busy === j.id + "approve"}>
                      {j.status === "escalated" ? "▷ approve retry" : "▷ approve"}
                    </button>
                    <button className="chip ctx-danger" onClick={() => decide(j.id, "reject")}
                      disabled={busy === j.id + "reject"}>
                      ✕ reject
                    </button>
                  </div>
                )}
              </div>

              {open === j.id && (
                <div className="query-body">
                  <div className="meta">
                    Supervisor: {j.supervisor_decision}
                    {Array.isArray(j.supervisor_reasons) && j.supervisor_reasons.length > 0 &&
                      ` — ${j.supervisor_reasons.join(" · ")}`}
                    {j.human_by && ` · human: ${j.human_by}`}
                  </div>
                  <pre className="query-sql">{j.script}</pre>
                  {j.last_error && <div className="error">{j.last_error}</div>}
                  <PipelineRecovery recovery={j.recovery} />
                  {j.result && (
                    <pre className="query-sql">{JSON.stringify(j.result, null, 2).slice(0, 1200)}</pre>
                  )}
                  {["platform_run", "airflow_dag"].includes(j.kind) && (
                    <>
                      <div className="query-actions">
                        <button className="chip"
                          onClick={() => setLiveFor(liveFor === j.id ? null : j.id)}>
                          {liveFor === j.id ? "✕ hide live status" : "↻ live status"}
                        </button>
                      </div>
                      {liveFor === j.id && <PlatformLive jobId={j.id} onUnavailable={unavailable} />}
                    </>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
