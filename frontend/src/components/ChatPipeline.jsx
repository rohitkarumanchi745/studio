import { useEffect, useState } from "react";
import { api } from "../api";
import PipelineRecovery, { recoveryIsActive } from "./PipelineRecovery";

function detailText(value) {
  if (typeof value === "string") return value;
  if (value == null) return "No detail provided";
  const detail = value?.error || value?.message || value?.reason || value?.warning;
  return detail ? detailText(detail) : JSON.stringify(value);
}

export default function ChatPipeline({ pipeline, messageId, busy, onRun }) {
  const steps = pipeline.steps || [];
  const dropped = pipeline.dropped || [];
  const warnings = pipeline.warnings || [];
  const run = pipeline.run;
  const [recovery, setRecovery] = useState(run?.recovery || null);
  const [recoveryError, setRecoveryError] = useState("");
  const [unavailable, setUnavailable] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let timer, stopped = false;
    const abort = new AbortController();
    setRecovery(run?.recovery || null);
    setRecoveryError("");
    setUnavailable(false);
    if (!pipeline.id || !run?.id || !(["failed", "error"].includes(run.status) || run.recovery)) return;
    const started = Date.now();
    const poll = async () => {
      try {
        const data = await api(`/pipelines/${encodeURIComponent(pipeline.id)}/runs/${encodeURIComponent(run.id)}/recovery`, { signal: abort.signal });
        if (stopped || !data) return;
        setRecovery(data.recovery || null);
        setRecoveryError("");
        if (recoveryIsActive(data.recovery) && Date.now() - started < 600000) timer = setTimeout(poll, 5000);
      } catch (error) {
        if (stopped) return;
        if ([403, 404].includes(error.status)) {
          setRecovery(null);
          setUnavailable(true);
        } else setRecoveryError("Could not refresh agent recovery. Refresh before requesting another run.");
      }
    };
    poll();
    return () => { stopped = true; clearTimeout(timer); abort.abort(); };
  }, [pipeline.id, run?.id, run?.status, run?.recovery, refresh]);
  const results = run?.steps_result || [];
  const runnable = pipeline.status === "ready" && !!pipeline.source && !dropped.length && steps.length > 0 &&
    steps.every((step) => step.verified && step.sql);
  const failed = !!run?.error || results.some((step) => step.ok === false) ||
    ["failed", "cancelled", "rejected", "escalated"].includes(run?.status);
  const successStatus = run?.status === "success" || run?.status?.startsWith("succeeded");
  const succeeded = successStatus && !dropped.length && results.length === steps.length &&
    results.every((step) => step.ok === true);
  const activeRun = ["queued", "running", "retrying", "awaiting_approval"].includes(run?.status);
  const incompleteRun = successStatus && !succeeded;
  const label = failed ? "Run failed"
    : succeeded ? "Run completed"
    : incompleteRun ? "Run incomplete"
    : run ? `Run ${run.status || "incomplete"}`
    : dropped.length ? "Pipeline blocked · review excluded steps"
    : !runnable ? "No runnable pipeline" : "Ready to run";
  const tone = failed || !runnable ? "bad" : dropped.length || incompleteRun ? "warn" : succeeded ? "ok" : "";

  if (unavailable) return <section className="chat-pipeline"><p>This pipeline run is no longer available to your account.</p></section>;

  return (
    <section className="chat-pipeline" aria-label="Chat pipeline">
      <div className="chat-pipeline-heading">
        <strong>{pipeline.name || "Pipeline from this answer"}</strong>
        <span className={`chat-pipeline-status ${tone}`} role="status">{label}</span>
      </div>
      <p className="chat-pipeline-description">Independent, read-only SQL steps. No table writes or step dependencies.</p>
      {pipeline.memory && <p className="meta">{pipeline.memory.reuse_type === "exact_reverified" ? "Reverified a successful recipe" : pipeline.memory.reuse_type === "model_adapted" ? "Adapted a successful recipe" : "Found a recipe that needs adaptation"}: {pipeline.memory.matched_prompt}</p>}
      {warnings.length > 0 && (
        <ul className="chat-pipeline-warnings">
          {warnings.map((warning, index) => <li key={index}>{detailText(warning)}</li>)}
        </ul>
      )}
      {steps.map((step, index) => (
        <details className="chat-pipeline-step" key={`${step.name}-${index}`}>
          <summary>
            <span>{index + 1}. {step.name || "Query"}</span>
            <span className="meta">{step.source || pipeline.source}{step.table && step.table !== "*" ? ` / ${step.table}` : ""}</span>
            <span className={step.verified ? "meta" : "error"}>
              {step.verified ? "Verified" : "Unverified"}
              {step.verified && Number.isFinite(step.row_count) ? ` · ${step.row_count} rows at verification` : ""}
            </span>
          </summary>
          {step.sql && <pre className="query-sql">{step.sql}</pre>}
          {step.intent_warnings?.length > 0 && (
            <ul className="chat-pipeline-warnings">
              {step.intent_warnings.map((warning, i) => <li key={i}>{detailText(warning)}</li>)}
            </ul>
          )}
        </details>
      ))}
      {dropped.length > 0 && (
        <div className="chat-pipeline-warnings">
          <strong>{dropped.length} {dropped.length === 1 ? "step excluded" : "steps excluded"}</strong>
          <ul>{dropped.map((step, index) => (
            <li key={index}>{step.name ? `${step.name}: ` : ""}{detailText(step)}</li>
          ))}</ul>
        </div>
      )}
      {run && (
        <div className="chat-pipeline-results">
          {results.map((step, index) => (
            <p key={index} className={step.ok === false ? "error" : "meta"}>
              {step.name || `Step ${index + 1}`}: {step.ok === true ? `${step.row_count ?? 0} rows` : step.error || "Did not complete"}
            </p>
          ))}
          {run.error && <p className="error">{detailText(run.error)}</p>}
          {Number.isFinite(run.took_ms) && <p className="meta">Run time: {(run.took_ms / 1000).toFixed(1)}s</p>}
        </div>
      )}
      <PipelineRecovery recovery={recovery} />
      {recoveryError && <p className="error">{recoveryError}</p>}
      <button
        type="button"
        className="primary chat-pipeline-run"
        disabled={busy || activeRun || recoveryIsActive(recovery) || !!recoveryError || !runnable || !messageId || !onRun}
        onClick={onRun}
        title={!runnable ? "Every pipeline step must verify before running" : !messageId ? "Reopen this chat to load its saved pipeline" : "Re-run these verified queries against fresh data"}
      >
        {recoveryIsActive(recovery) ? "Agent handling recovery…" : activeRun ? "Pipeline running…" : run ? (recovery?.child_run_id ? "Run original pipeline again" : "Run pipeline again") : "Run pipeline"}
      </button>
      {run?.id && (recovery || recoveryError) && <button type="button" className="chip" disabled={busy} onClick={() => setRefresh((n) => n + 1)}>Refresh recovery</button>}
    </section>
  );
}
