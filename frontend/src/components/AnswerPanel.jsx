// The chat's right-hand panel: what is behind an answer, at a glance.
//
//   Steps    each tool call and step the agent took, in order (live while it
//            works), with the source it acted on.
//   Sources  every source the answer read, and under each the tables and the
//            columns its SQL names (the answer's `uses`); documents it cited.
//   Output   what it produced: charts, a result table, a visual, a job.
//   Tools    the kinds of tool it used, and the model.
//
// It follows the newest answer, the run in progress, or the answer whose
// "Details" button was pressed. Steps are the asker's own record (the task
// API checks its owner); everything else is read off the answer itself, so it
// is withheld exactly when the answer is.
import { useEffect, useState } from "react";
import { api } from "../api";
import { SourceIcon } from "./sourceCatalog";
import { sourceIconId, sourceTypeLabel, stepKind } from "./sourceLabels";

// What each kind of step says about the tool it used.
const TOOL_OF_KIND = {
  query: ["🛢", "SQL"], schema: ["▦", "Schema reader"], route: ["🧭", "Metadata graph"],
  chart: ["📊", "Charts"], visual: ["🎨", "D3 visual"], semantic: ["▣", "Semantic layer"],
  verified: ["✓", "Verified queries"], cache: ["⚡", "Plan cache"], plan: ["🗺", "Planner"],
  agents: ["👥", "Agents"], agent: ["👥", "Agents"], aggregate: ["∑", "Aggregator"],
  docs: ["📄", "Knowledge search"], tools: ["🔧", "MCP tools"], model: ["🧠", "Model routing"],
  pipeline: ["⚙", "Pipelines"], web: ["🌐", "Web search"], clock: ["🕒", "Clock"],
  files: ["📁", "Your files"],
};

/** A link only to a web page: http(s), nothing a browser would run. */
function webLink(url) {
  try {
    const u = new URL(String(url || ""));
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

/** A step's state on the timeline: the newest of a running task is in progress. */
export function stepRows(steps = [], running = false) {
  const list = (steps || []).filter((s) => s && s.label);
  return list.map((s, i) => ({
    ...s,
    state: stepKind(s) === "error" ? "error" : running && i === list.length - 1 ? "running" : "done",
  }));
}

/**
 * The sources an answer read, each with the tables and columns under it:
 * [{key, name, type, engine, tables: [{name, columns, all}]}], documents last
 * as {key: "docs", docs: [...]}. From the answer's `uses`; an older answer
 * without it falls back to the tables its SQL read; a run still going shows
 * the tables its steps grounded on.
 */
export function panelSources(m, steps = []) {
  const out = [];
  const add = (name, type, engine) => {
    let s = out.find((x) => x.name === name);
    if (!s) {
      s = { key: `s:${name}`, name, type, engine, tables: [] };
      out.push(s);
    }
    if (!s.type && type) s.type = type;
    if (!s.engine && engine) s.engine = engine;
    return s;
  };
  const table = (s, name) => {
    let t = s.tables.find((x) => x.name.toLowerCase() === String(name).toLowerCase());
    if (!t) {
      t = { name: String(name), columns: [], all: false };
      s.tables.push(t);
    }
    return t;
  };
  if (Array.isArray(m?.uses) && m.uses.length) {
    for (const u of m.uses) {
      if (!u?.source) continue;
      const s = add(u.source, u.source_type, u.engine);
      for (const t of u.tables || []) {
        const row = table(s, t.name);
        row.columns = [...new Set([...row.columns, ...(t.columns || [])])];
        row.all = row.all || !!t.all_columns;
      }
    }
  } else if (m?.inputs?.tables?.length && m.source && !["*", "auto"].includes(m.source)) {
    const s = add(m.source, m.source_meta?.source_type, m.source_meta?.engine);
    for (const t of m.inputs.tables) table(s, t);
  }
  // A run in progress shows what its steps have grounded on so far; a finished
  // answer that ran no query read no source, whatever its steps looked at.
  const queried = Boolean(m?.sql || m?.panels?.some((p) => p?.sql));
  if (!m?.uses?.length && (!m || queried)) {
    for (const step of steps || []) {
      if (!step?.source || !(step.source_type || step.engine || step.tables?.length)) continue;
      const s = add(step.source, step.source_type, step.engine);
      for (const t of step.tables || []) if (!t.bridge) table(s, t.name);
    }
  }
  const docs = [...new Set((m?.citations || []).filter((c) => !["web", "file"].includes(c.kind))
    .map((c) => c.source).filter(Boolean))];
  if (docs.length) out.push({ key: "docs", docs });
  const files = [...new Set((m?.citations || []).filter((c) => c.kind === "file")
    .map((c) => c.path || c.source).filter(Boolean))];
  if (files.length) out.push({ key: "files", files });
  const pages = [];
  for (const c of m?.citations || []) {
    const url = c.kind === "web" && webLink(c.url);
    if (url && !pages.some((p) => p.url === url)) pages.push({ url, title: c.source || new URL(url).hostname });
  }
  // Grounding with Bing: each query's results page is a required reference.
  const bing = m?.web_grounding?.provider === "bing"
    ? (m.web_grounding.queries || []).filter((q) => q?.query
        && webLink(q.url)?.startsWith("https://www.bing.com/search?"))
      .map((q) => ({ query: String(q.query), url: webLink(q.url) }))
    : [];
  if (pages.length || bing.length) out.push({ key: "web", pages, bing });
  return out;
}

/** What an answer produced, as rows: [{key, icon, label, meta, chart}]. */
export function panelOutputs(m) {
  if (!m) return [];
  const out = [];
  const rowsOf = (p) => (Array.isArray(p?.rows) ? p.rows.length : 0);
  const shape = (p) => [rowsOf(p) ? `${rowsOf(p).toLocaleString()} rows` : "",
                        p?.columns?.length ? `${p.columns.length} columns` : ""].filter(Boolean).join(" · ");
  const views = m.panels?.length ? m.panels : (m.chart || m.columns?.length ? [m] : []);
  views.forEach((p, i) => {
    if (p.visual) {
      out.push({ key: `v${i}`, icon: "🎨", label: p.visual.title || p.chart?.title || "Custom visual",
                 meta: "D3 visual", chart: true });
    } else if (p.chart) {
      out.push({ key: `c${i}`, icon: "📊", label: p.chart.title || "Chart",
                 meta: [p.chart.type ? `${p.chart.type} chart` : "", shape(p)].filter(Boolean).join(" · "),
                 chart: true });
    } else if (p.columns?.length) {
      out.push({ key: `t${i}`, icon: "▦", label: "Result table", meta: shape(p), chart: true });
    }
  });
  if (m.rows_purged) out.push({ key: "expired", icon: "⌛", label: "Result rows expired",
                                meta: "the SQL and chart settings are kept" });
  if (m.platform_run) {
    out.push({ key: "job", icon: "⚙", label: m.platform_run.label || m.platform_run.target || "Platform job",
               meta: String(m.platform_run.status || "").replaceAll("_", " ") });
  }
  if (m.pipeline?.execution_mode === "airflow_dag") {
    out.push({ key: "dag", icon: "⚙", label: m.pipeline.dag_id ? `Airflow DAG ${m.pipeline.dag_id}` : "Airflow DAG",
               meta: String(m.pipeline.status || "").replaceAll("_", " ") });
  }
  if (m.autopilot_draft) out.push({ key: "ap", icon: "✦", label: "Autopilot draft", meta: "" });
  return out;
}

/** The tools an answer used, as chips: [{key, icon, label}]. */
export function panelTools(m, steps = []) {
  const out = new Map();
  const put = (key, icon, label) => { if (!out.has(key)) out.set(key, { key, icon, label }); };
  for (const s of steps || []) {
    const tool = TOOL_OF_KIND[stepKind(s)];
    if (tool) put(tool[1], ...tool);
  }
  if (m) {
    if (m.sql || m.panels?.some((p) => p?.sql)) put("SQL", ...TOOL_OF_KIND.query);
    if (m.chart || m.panels?.some((p) => p?.chart)) put("Charts", ...TOOL_OF_KIND.chart);
    if (m.visual || m.panels?.some((p) => p?.visual)) put("D3 visual", ...TOOL_OF_KIND.visual);
    if (m.citations?.some((c) => !["web", "file"].includes(c.kind))) put("Knowledge search", ...TOOL_OF_KIND.docs);
    if (m.citations?.some((c) => c.kind === "web") || m.web_grounding) put("Web search", ...TOOL_OF_KIND.web);
    if (m.citations?.some((c) => c.kind === "file")) put("Your files", ...TOOL_OF_KIND.files);
    if (m.served_by === "semantic") put("Semantic layer", ...TOOL_OF_KIND.semantic);
    if (m.verified_sql) put("Verified queries", ...TOOL_OF_KIND.verified);
    if (m.cached || m.mode === "cached") put("Plan cache", ...TOOL_OF_KIND.cache);
    if (m.agents?.length > 1) put("Agents", ...TOOL_OF_KIND.agents);
    if (m.platform_run) put(`job:${m.platform_run.target}`, "⚙", m.platform_run.label || "Platform job");
    if (m.pipeline?.execution_mode === "airflow_dag") put("airflow", "⚙", "Airflow");
    const model = m.served_by === "bitnet" ? "BitNet" : m.model;
    if (model) put(`model:${model}`, "🧠", model);
  }
  return [...out.values()];
}

function Section({ title, count, children, empty }) {
  return (
    <details className="ap-section" open>
      <summary>
        <span className="ap-section-title">{title}</span>
        {count != null && <span className="ap-count">{count}</span>}
        <span className="ap-chevron" aria-hidden="true" />
      </summary>
      {children || <p className="ap-empty">{empty}</p>}
    </details>
  );
}

function SourceRow({ name, type, engine }) {
  return (
    <div className="ap-source-head">
      <SourceIcon id={sourceIconId(type, engine)} size={18} />
      <b>{name}</b>
      {(type || engine) && <span className="ap-source-type">{sourceTypeLabel(type, engine)}</span>}
    </div>
  );
}

export default function AnswerPanel({ answer, question, steps, running = false, onSteps, onClose,
                                      onOpen }) {
  const taskId = answer?.activity_task_id;
  const [loaded, setLoaded] = useState({});      // taskId -> steps | "unavailable"
  const known = steps ?? (taskId ? loaded[taskId] : undefined);

  useEffect(() => {
    if (running || steps || !taskId || loaded[taskId]) return undefined;
    let live = true;
    api(`/tasks/${encodeURIComponent(taskId)}`)
      .then((task) => {
        if (!live) return;
        const got = Array.isArray(task?.steps) ? task.steps : [];
        setLoaded((l) => ({ ...l, [taskId]: got }));
        onSteps?.(taskId, got);
      })
      .catch(() => { if (live) setLoaded((l) => ({ ...l, [taskId]: "unavailable" })); });
    return () => { live = false; };
  }, [taskId, running, steps]);

  const stepList = Array.isArray(known) ? known : [];
  const rows = stepRows(stepList, running);
  const done = rows.filter((r) => r.state !== "running").length;
  const sources = panelSources(answer, stepList);
  const outputs = panelOutputs(answer);
  const tools = panelTools(answer, stepList);
  const stepsNote = running ? "Waiting for the first step…"
    : known === "unavailable" ? "Steps are kept for the person who asked."
    : !taskId ? "No steps were recorded for this answer."
    : known === undefined ? "Loading steps…" : "No steps were recorded for this answer.";

  return (
    <aside className="answer-panel" aria-label="Answer details">
      <div className="ap-head">
        <div className="ap-head-text">
          <strong>{running ? "Working on it" : "Answer details"}</strong>
          {question && <span className="ap-question" title={question}>{question}</span>}
        </div>
        <button type="button" className="icon-btn ap-close" onClick={onClose}
                aria-label="Close answer details" title="Close">✕</button>
      </div>

      <Section title="Steps" count={rows.length ? `${done} / ${rows.length}` : null} empty={stepsNote}>
        {rows.length > 0 && (
          <ol className="ap-steps">
            {rows.map((r, i) => (
              <li key={`${r.t ?? ""}-${i}`} className={`ap-step is-${r.state}`}>
                <span className="ap-step-mark" aria-hidden="true" />
                <span className="ap-step-text">
                  <span className="ap-step-label">{r.label}</span>
                  {(r.source && (r.source_type || r.engine) || r.tables?.length > 0) && (
                    <span className="ap-step-where">
                      {r.source && (r.source_type || r.engine) && (
                        <span className="ap-step-source">
                          <SourceIcon id={sourceIconId(r.source_type, r.engine)} size={12} />{r.source}
                        </span>
                      )}
                      {(r.tables || []).map((t) => (
                        <span key={t.name} className="ap-chip">▦ {t.name}{t.bridge ? " ⇄" : ""}</span>
                      ))}
                    </span>
                  )}
                </span>
                <span className="sr-only">
                  {r.state === "running" ? "in progress" : r.state === "error" ? "problem" : "done"}
                </span>
              </li>
            ))}
          </ol>
        )}
      </Section>

      <Section title="Sources" count={sources.length || null}
               empty={running ? "Sources appear as the agent finds them." : "This answer read no data source."}>
        {sources.length > 0 && (
          <ul className="ap-sources">
            {sources.map((s) => s.pages ? (
              <li key={s.key} className="ap-source">
                <div className="ap-source-head">
                  <span aria-hidden="true">🌐</span><b>The web</b>
                  {s.bing.length > 0 && <span className="ap-source-type">Bing web search</span>}
                </div>
                <ul className="ap-tables">
                  {s.bing.map((q) => (
                    <li key={q.url} className="ap-table">
                      <a className="ap-table-name ap-link" href={q.url} target="_blank"
                         rel="noopener noreferrer" title={q.url}>🔎 Searched Bing: {q.query}</a>
                    </li>
                  ))}
                  {s.pages.map((p) => (
                    <li key={p.url} className="ap-table">
                      <a className="ap-table-name ap-link" href={p.url} target="_blank"
                         rel="noopener noreferrer" title={p.url}>{p.title}</a>
                      <span className="ap-web-host">{new URL(p.url).hostname}</span>
                    </li>
                  ))}
                </ul>
              </li>
            ) : s.files ? (
              <li key={s.key} className="ap-source">
                <div className="ap-source-head"><span aria-hidden="true">📁</span><b>Your files</b></div>
                <ul className="ap-tables">
                  {s.files.map((f) => <li key={f} className="ap-table"><span className="ap-table-name">{f}</span></li>)}
                </ul>
              </li>
            ) : s.docs ? (
              <li key={s.key} className="ap-source">
                <div className="ap-source-head"><span aria-hidden="true">📄</span><b>Your documents</b></div>
                <ul className="ap-tables">
                  {s.docs.map((d) => <li key={d} className="ap-table"><span className="ap-table-name">{d}</span></li>)}
                </ul>
              </li>
            ) : (
              <li key={s.key} className="ap-source">
                <SourceRow name={s.name} type={s.type} engine={s.engine} />
                {s.tables.length > 0 && (
                  <ul className="ap-tables">
                    {s.tables.map((t) => (
                      <li key={t.name} className="ap-table">
                        <span className="ap-table-name">▦ {t.name}</span>
                        <span className="ap-columns">
                          {t.all && <span className="ap-chip ap-chip-all">all columns</span>}
                          {t.columns.map((c) => <span key={c} className="ap-chip">{c}</span>)}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Output" count={outputs.length || null}
               empty={running ? "Nothing yet." : "Text only."}>
        {outputs.length > 0 && (
          <ul className="ap-outputs">
            {outputs.map((o) => (
              <li key={o.key}>
                {o.chart && onOpen ? (
                  <button type="button" className="ap-output" onClick={onOpen} title="Open in the canvas">
                    <span className="ap-output-icon" aria-hidden="true">{o.icon}</span>
                    <span className="ap-output-text"><b>{o.label}</b>{o.meta && <span>{o.meta}</span>}</span>
                  </button>
                ) : (
                  <div className="ap-output">
                    <span className="ap-output-icon" aria-hidden="true">{o.icon}</span>
                    <span className="ap-output-text"><b>{o.label}</b>{o.meta && <span>{o.meta}</span>}</span>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title="Tools" count={tools.length || null} empty={running ? "Nothing yet." : "None recorded."}>
        {tools.length > 0 && (
          <div className="ap-tools">
            {tools.map((t) => (
              <span key={t.key} className="ap-tool"><span aria-hidden="true">{t.icon}</span> {t.label}</span>
            ))}
          </div>
        )}
      </Section>
    </aside>
  );
}
