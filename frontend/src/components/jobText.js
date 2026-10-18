// Plain-language text for the Jobs screen: what a job ran and when. Pure, so
// the wording is tested on its own.

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
