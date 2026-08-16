// Supervised jobs. Agents submit scripts / Spark jobs against real
// environments; a supervisor agent reviews each one (read-only auto-approves,
// writes and jobs need a human). Repeated failures escalate — an admin
// approves a retry or rejects. Admins are the human in the loop.
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

export default function Jobs({ onClose }) {
  const [jobs, setJobs] = useState(null);
  const [canApprove, setCanApprove] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(null);
  const [busy, setBusy] = useState("");

  // Submit form
  const [sources, setSources] = useState([]);
  const [kind, setKind] = useState("sql_script");
  const [target, setTarget] = useState("demo");
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
  }, []);

  async function submit() {
    if (!script.trim() || busy) return;
    setBusy("submit");
    setError("");
    try {
      await api("/jobs", {
        method: "POST",
        body: JSON.stringify({ kind, target, script: script.trim() }),
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

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Jobs</div>
          <div className="meta">
            Scripts and Spark jobs against real environments. A supervisor agent
            reviews every job; writes and jobs need human approval, and repeated
            failures escalate. {canApprove ? "You can approve or reject." : "An admin approves."}
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
          </select>
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            {sources.filter((s) => s.allowed).map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}{!s.configured ? " (not connected)" : ""}
              </option>
            ))}
          </select>
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
            kind === "spark_job"
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
          <div className="empty-sub">Submit a script or Spark job above; the supervisor reviews it.</div>
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
                  <span className="query-tag">{j.target}</span>
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
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
