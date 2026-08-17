// Supervised jobs. Agents submit scripts / Spark jobs / platform runs
// (Airflow, Databricks Jobs, dbt Cloud, K8s Spark) against real environments;
// a supervisor agent reviews each one (read-only auto-approves, writes and
// jobs need a human). Repeated failures escalate — an admin approves a retry
// or rejects. Admins are the human in the loop. Platform runs expose a live
// panel (status / metrics / logs / quality checks) via GET /jobs/{id}/live.
import { useEffect, useState } from "react";
import { api } from "../api";

const STATUS_LABEL = {
  succeeded: "✓ succeeded",
  running: "running…",
  retrying: "retrying…",
  awaiting_approval: "⏳ awaiting approval",
  escalated: "⚠ escalated — needs a human",
  rejected: "✕ rejected",
};

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
function PlatformLive({ jobId }) {
  const [live, setLive] = useState(null);
  const [err, setErr] = useState("");
  const [showLogs, setShowLogs] = useState(false);

  useEffect(() => {
    let timer = null;
    let gone = false;
    const tick = () =>
      api(`/jobs/${jobId}/live`)
        .then((d) => {
          if (gone) return;
          setErr("");
          setLive(d);
          if (d.state === "queued" || d.state === "running") {
            timer = setTimeout(tick, 5000);
          }
        })
        .catch((e) => {
          if (!gone) setErr(e.message);
        });
    tick();
    return () => {
      gone = true;
      if (timer) clearTimeout(timer);
    };
  }, [jobId]);

  if (err) return <div className="error">{err}</div>;
  if (!live) return <div className="meta">fetching live status…</div>;

  const metrics = Object.entries(live.metrics || {}).filter(
    ([, v]) => typeof v !== "object"
  );
  const quality = Array.isArray(live.quality) ? live.quality : [];
  const polling = live.state === "queued" || live.state === "running";

  return (
    <div className="live-panel">
      <div className="live-head">
        <span className={"job-status job-" + live.state}>{live.state}</span>
        {metrics.map(([k, v]) => (
          <span key={k} className="query-tag">{k}: {String(v)}</span>
        ))}
        {live.url && (
          <a className="chip" href={live.url} target="_blank" rel="noreferrer">
            ↗ open run
          </a>
        )}
        {live.logs && (
          <button className="chip" onClick={() => setShowLogs(!showLogs)}>
            {showLogs ? "▾ hide logs" : "▸ logs"}
          </button>
        )}
        {polling && <span className="meta">refreshing every 5s…</span>}
      </div>
      {live.detail && <div className="meta live-detail">{live.detail}</div>}
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

  const load = () =>
    api("/jobs")
      .then((d) => {
        setJobs(d.jobs || []);
        setCanApprove(!!d.can_approve);
      })
      .catch((e) => setError(e.message));

  useEffect(() => {
    load();
    api("/catalog/sources").then(setSources).catch(() => {});
    api("/jobs/platforms")
      .then((d) => {
        const ps = Array.isArray(d) ? d : d.platforms || [];
        setPlatforms(ps);
        const first = ps.find((p) => p.configured) || ps[0];
        if (first) setPlatform((cur) => cur || first.name);
      })
      .catch(() => {});
  }, []);

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
      await api(`/jobs/${id}/${action}`, { method: "POST" });
      load();
    } catch (e) {
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
            Scripts, Spark jobs and platform runs against real environments. A
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
            <div key={j.id} className="query-card">
              <div className="query-head" onClick={() => setOpen(open === j.id ? null : j.id)}>
                <div className="query-title">
                  <span className={"job-status job-" + j.status}>
                    {STATUS_LABEL[j.status] || j.status}
                  </span>
                  <span className="query-tag">{j.kind}</span>
                  <span className="query-tag">
                    {j.kind === "platform_run" ? platformLabel(j.target) : j.target}
                  </span>
                  <span className={"job-risk job-risk-" + j.risk}>{j.risk}</span>
                  {j.attempts > 0 && (
                    <span className="meta">{j.attempts}/{j.max_retries + 1} attempts</span>
                  )}
                </div>
                {canApprove && (j.status === "awaiting_approval" || j.status === "escalated") && (
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
                  {j.result && (
                    <pre className="query-sql">{JSON.stringify(j.result, null, 2).slice(0, 1200)}</pre>
                  )}
                  {j.kind === "platform_run" && j.result && j.result.run_ref && (
                    <>
                      <div className="query-actions">
                        <button className="chip"
                          onClick={() => setLiveFor(liveFor === j.id ? null : j.id)}>
                          {liveFor === j.id ? "✕ hide live status" : "↻ live status"}
                        </button>
                      </div>
                      {liveFor === j.id && <PlatformLive jobId={j.id} />}
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
