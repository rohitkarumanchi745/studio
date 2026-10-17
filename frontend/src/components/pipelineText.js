// Plain-language text for the pipeline screens (Pipelines, Pipeline flow,
// Jobs): what a status means, why a run was rejected, what a job ran. Pure, so
// the wording is tested on its own.

const FLOW_LABELS = {
  succeeded: "Succeeded",
  succeeded_sql_only: "Succeeded — the SQL ran",
  awaiting_approval: "Waiting for an administrator's approval",
  human_review: "Needs human review",
  escalated: "Escalated to a person",
  rejected: "Rejected",
  plan_failed: "Could not plan this request",
  deploy_failed: "Deployment failed",
  rolled_back: "Stopped and rolled back",
  execute_failed: "The run failed",
  failed: "Failed",
  skipped: "Skipped",
};

/** "succeeded_sql_only" → "Succeeded — the SQL ran"; unknown → "Some status". */
export function flowStatusLabel(status) {
  if (!status) return "—";
  if (FLOW_LABELS[status]) return FLOW_LABELS[status];
  const words = String(status).replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** The one line that explains a flow's outcome, or "". */
export function flowStatusNote(flow) {
  if (!flow) return "";
  if (flow.status === "succeeded_sql_only") {
    return "Studio ran the verified SQL steps. The generated Python is a download for you to " +
           "review and deploy yourself — Studio never runs model-written code.";
  }
  if (flow.status === "rejected") {
    const reasons = (flow.deployment?.reasons || []).filter(Boolean);
    return reasons.length ? `Why: ${reasons.join(" · ")}` : "The approval step rejected this run.";
  }
  return "";
}

/** A lineage graph (pipelines.lineage) without step `index`: its table and
 *  source go too when nothing else reads them, and the rest are renumbered. */
export function withoutStep(lineage, index) {
  if (!lineage) return lineage;
  const kept = (lineage.steps || []).filter((s) => s.index !== index);
  const ids = new Map(kept.map((s, k) => [s.id, `step:${k}`]));
  const steps = kept.map((s, k) => ({ ...s, index: k, id: `step:${k}` }));
  const stepEdges = (lineage.edges || [])
    .filter((e) => String(e.to).startsWith("step:") && ids.has(e.to))
    .map((e) => ({ ...e, to: ids.get(e.to) }));
  const usedTables = new Set(stepEdges.map((e) => e.from));
  const tables = (lineage.tables || []).filter((t) => usedTables.has(t.id));
  const tableEdges = (lineage.edges || [])
    .filter((e) => String(e.from).startsWith("s:") && usedTables.has(e.to));
  const usedSources = new Set(tableEdges.map((e) => e.from));
  const sources = (lineage.sources || []).filter((s) => usedSources.has(s.id));
  return { ...lineage, sources, tables, steps, edges: [...tableEdges, ...stepEdges],
           multi_source: sources.length > 1 };
}

/** What a job ran, in one line: a SQL script's first statement line, a
 *  Spark or platform payload's name, an Airflow DAG's id. */
export function jobSummary(job) {
  const script = String(job?.script || "").trim();
  if (!script) return "";
  if (script.startsWith("{")) {
    try {
      const o = JSON.parse(script);
      const name = o.run_name || o.dag_id || o.name || o.job_id || o.pipeline_name;
      if (name) return String(name).slice(0, 90);
      if (Array.isArray(o.tasks)) return `${o.tasks.length} task${o.tasks.length === 1 ? "" : "s"}`;
    } catch {
      /* not JSON after all: fall through to the first line */
    }
  }
  const line = script.split("\n").map((l) => l.trim())
    .find((l) => l && !l.startsWith("--") && !l.startsWith("#")) || "";
  return line.length > 90 ? `${line.slice(0, 89)}…` : line;
}

/** "3 min ago", "2 h ago", or a date — for an epoch-seconds timestamp. */
export function ago(seconds, now = Date.now() / 1000) {
  const s = Number(seconds);
  if (!s) return "";
  const d = Math.max(0, now - s);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return new Date(s * 1000).toLocaleDateString();
}
