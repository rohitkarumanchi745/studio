const ACTIVE_RECOVERY = new Set(["pending", "diagnosing", "retrying", "awaiting_approval"]);

export function recoveryIsActive(recovery) {
  return !!recovery && ACTIVE_RECOVERY.has(recovery.state);
}

export function recoveryBlocksJobDecision(recovery, jobId) {
  // Approval belongs to the new correction, never to its failed parent.
  return recoveryIsActive(recovery) && recovery.child_job_id !== jobId;
}

const LABELS = {
  pending: "Agent recovery queued",
  diagnosing: "Agent diagnosing the failure",
  retrying: "Agent correction in progress",
  awaiting_approval: "Agent correction needs approval",
  succeeded: "Agent correction succeeded",
  escalated: "Agent recovery needs attention",
  exhausted: "Agent recovery budget exhausted",
};

export default function PipelineRecovery({ recovery }) {
  if (!recovery || !recovery.state) return null;
  const reason = typeof recovery.reason === "string" ? recovery.reason : null;
  const hasBudget = Number.isInteger(recovery.attempt) && Number.isInteger(recovery.max_attempts);
  const needsAttention = ["escalated", "exhausted"].includes(recovery.state);
  return <aside className="chat-pipeline-results" aria-label="Agent pipeline recovery">
    <p className={needsAttention ? "error" : "meta"} role="status">
      <strong>{LABELS[recovery.state] || "Agent recovery status unavailable"}</strong>
      {hasBudget && ` · attempt ${recovery.attempt} of ${recovery.max_attempts}`}
    </p>
    <p className="meta">The original failed run remains recorded. Corrections have separate run identities.</p>
    {reason && <p className="meta">{reason}</p>}
    {recovery.state === "awaiting_approval" && <p className="meta">Review and approve the correction in Jobs. No corrected write is authorized by the earlier approval.</p>}
    {needsAttention && <p className="meta">Automatic recovery has stopped. Review the failure and any remote run before requesting another attempt.</p>}
    {recovery.child_job_id && <p className="meta"><a href={`/jobs#job-${encodeURIComponent(recovery.child_job_id)}`}>Open correction job</a>{recovery.child_state && ` · ${String(recovery.child_state).replaceAll("_", " ")}`}</p>}
    {recovery.child_run_id && <p className="meta">Correction run: {recovery.child_run_id}{recovery.child_state && ` · ${String(recovery.child_state).replaceAll("_", " ")}`}</p>}
    {recovery.repairs_run_id && <p className="meta">Correction linked to failed run: {recovery.repairs_run_id}</p>}
  </aside>;
}
