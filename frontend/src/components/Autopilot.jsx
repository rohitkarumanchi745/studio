// Autopilot agents — autonomous, proactive agents that fire on a TRIGGER (not a
// user chat), reason over data toward a GOAL, and take GOVERNED actions. A saved
// agent runs HEADLESS as its owner (never above the owner's RBAC): read actions
// run autonomously; any risky/write action is PROPOSED and routed through the
// supervisor's human-approval gate (a pending Job). This manager lets a user
// create agents (goal + trigger + scope), enable/disable them, run one now, and
// inspect run history — with a deep link to Jobs for any pending approval.
//
// Endpoints (degrade gracefully — if the backend isn't wired yet, each 404s and
// the view still renders): GET/POST /autopilot, GET/PATCH/DELETE /autopilot/{id},
// POST /autopilot/{id}/{enable,disable,run}, GET /autopilot/{id}/runs. Scope
// pickers reuse /catalog/sources, /catalog/sources/{s}/tables and /models.
import { useEffect, useState } from "react";
import { api } from "../api";

const TRIGGERS = [
  { id: "schedule", label: "Schedule", hint: "Run on a fixed interval." },
  { id: "threshold", label: "Threshold", hint: "Run when a metric crosses a rule." },
  { id: "event", label: "New data", hint: "Run when a table gets fresh rows." },
  { id: "manual", label: "Manual", hint: "Only when you press Run now." },
];

const OPS = [
  { id: "lt", label: "<" }, { id: "le", label: "≤" },
  { id: "gt", label: ">" }, { id: "ge", label: "≥" },
  { id: "eq", label: "=" }, { id: "ne", label: "≠" },
];
const OP_LABEL = Object.fromEntries(OPS.map((o) => [o.id, o.label]));

const INTERVALS = [
  { s: 300, label: "every 5 min" },
  { s: 900, label: "every 15 min" },
  { s: 3600, label: "hourly" },
  { s: 21600, label: "every 6 hours" },
  { s: 86400, label: "daily" },
  { s: 604800, label: "weekly" },
];

const RUN_TONE = { done: "ok", succeeded: "ok", failed: "bad", skipped: "warn", running: "warn" };

function intervalLabel(s) {
  const found = INTERVALS.find((i) => i.s === Number(s));
  if (found) return found.label;
  if (!s) return "—";
  const n = Number(s);
  if (n % 86400 === 0) return `every ${n / 86400}d`;
  if (n % 3600 === 0) return `every ${n / 3600}h`;
  if (n % 60 === 0) return `every ${n / 60}m`;
  return `every ${n}s`;
}

function whenText(ts) {
  if (!ts) return "";
  const d = new Date(Number(ts) * (Number(ts) > 1e12 ? 1 : 1000));
  if (isNaN(d)) return "";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// A one-line description of what fires this agent — the same shape the create
// form produces, read back for the list.
function triggerSummary(a) {
  const c = a.trigger_config || {};
  if (a.trigger_type === "schedule") return `⏱ ${intervalLabel(c.interval_seconds)}`;
  if (a.trigger_type === "threshold") {
    const metric = c.metric_prompt || (c.sql ? "custom SQL" : "metric");
    return `⚖ when ${metric} ${OP_LABEL[c.op] || c.op || "?"} ${c.value ?? "?"}`;
  }
  if (a.trigger_type === "event") return `◆ on new data in ${c.table || "?"}`;
  return "▷ manual only";
}

// ─────────────────────────────── Create form ───────────────────────────────
function CreateForm({ sources, models, onCreated }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [trigger, setTrigger] = useState("schedule");
  const [source, setSource] = useState("");
  const [tables, setTables] = useState([]);         // available for the source
  const [sel, setSel] = useState([]);               // chosen tables (empty = all allowed)
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // schedule
  const [interval, setInterval] = useState(3600);
  // threshold
  const [metricMode, setMetricMode] = useState("metric"); // metric | sql
  const [metricPrompt, setMetricPrompt] = useState("");
  const [sql, setSql] = useState("");
  const [column, setColumn] = useState("");
  const [op, setOp] = useState("lt");
  const [value, setValue] = useState("");
  // event
  const [eventTable, setEventTable] = useState("");
  // optional governed action (routed through supervisor → needs approval)
  const [actOpen, setActOpen] = useState(false);
  const [actKind, setActKind] = useState("sql_script");
  const [actTarget, setActTarget] = useState("");
  const [actScript, setActScript] = useState("");

  useEffect(() => {
    const allowed = sources.filter((s) => s.allowed !== false);
    if (!source && allowed.length) setSource(allowed[0].name);
  }, [sources, source]);

  useEffect(() => {
    setSel([]); setTables([]); setEventTable("");
    if (!source) return;
    api(`/catalog/sources/${source}/tables`)
      .then((t) => setTables(Array.isArray(t) ? t : []))
      .catch(() => setTables([]));
  }, [source]);

  function toggleTable(t) {
    setSel((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));
  }

  function reset() {
    setName(""); setGoal(""); setSel([]); setMetricPrompt(""); setSql("");
    setColumn(""); setValue(""); setEventTable(""); setActOpen(false);
    setActKind("sql_script"); setActTarget(""); setActScript(""); setError("");
  }

  function buildConfig() {
    if (trigger === "schedule") return { interval_seconds: Number(interval) };
    if (trigger === "threshold") {
      const cfg = { op, value: Number(value), column: column.trim() || undefined };
      if (metricMode === "sql") cfg.sql = sql.trim();
      else cfg.metric_prompt = metricPrompt.trim();
      return cfg;
    }
    if (trigger === "event") return { table: eventTable, interval_seconds: 300 };
    return {};
  }

  function valid() {
    if (!name.trim() || !goal.trim() || !source) return false;
    if (trigger === "threshold") {
      if (value === "" || isNaN(Number(value))) return false;
      if (metricMode === "sql" ? !sql.trim() : !metricPrompt.trim()) return false;
    }
    if (trigger === "event" && !eventTable) return false;
    return true;
  }

  async function create() {
    if (!valid() || busy) return;
    setBusy(true);
    setError("");
    const body = {
      name: name.trim(),
      goal: goal.trim(),
      trigger_type: trigger,
      trigger_config: buildConfig(),
      source,
      tables: sel,                 // empty = all tables the owner can access
    };
    if (model) body.model = model;
    if (actOpen && actKind && actScript.trim()) {
      body.action = { kind: actKind, target: actTarget.trim() || source, script: actScript.trim() };
    }
    try {
      await api("/autopilot", { method: "POST", body: JSON.stringify(body) });
      reset();
      setOpen(false);
      onCreated();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button className="chip chip-on" style={{ marginBottom: 14 }} onClick={() => setOpen(true)}>
        + New agent
      </button>
    );
  }

  const allowedSources = sources.filter((s) => s.allowed !== false);

  return (
    <div className="query-item ap-create" style={{ marginBottom: 16 }}>
      <div className="query-body">
        <div className="canvas-title" style={{ fontSize: 15, marginBottom: 8 }}>New autopilot agent</div>

        {error && <div className="error" style={{ marginBottom: 8 }}>{error}</div>}

        <div className="job-form-row">
          <input className="sqllab-prompt" style={{ flex: 1, minWidth: 180 }} value={name}
            placeholder="Agent name — e.g. Revenue watchdog" onChange={(e) => setName(e.target.value)} />
        </div>

        <textarea className="ap-goal" value={goal} spellCheck={false}
          placeholder="Goal in plain English — what should this agent find out or do each time it fires? e.g. Summarise today's sales by region and flag anything unusual."
          onChange={(e) => setGoal(e.target.value)} />

        {/* Trigger */}
        <div className="meta ap-label">Trigger</div>
        <div className="inputs-row">
          {TRIGGERS.map((t) => (
            <button key={t.id} title={t.hint}
              className={"chip" + (trigger === t.id ? " chip-on" : "")}
              onClick={() => setTrigger(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="meta ap-hint">{TRIGGERS.find((t) => t.id === trigger)?.hint}</div>

        {trigger === "schedule" && (
          <div className="job-form-row">
            <label className="meta">Run</label>
            <select value={interval} onChange={(e) => setInterval(e.target.value)}>
              {INTERVALS.map((i) => <option key={i.s} value={i.s}>{i.label}</option>)}
            </select>
          </div>
        )}

        {trigger === "threshold" && (
          <div className="ap-threshold">
            <div className="inputs-row">
              <button className={"chip" + (metricMode === "metric" ? " chip-on" : "")}
                onClick={() => setMetricMode("metric")}>Semantic metric</button>
              <button className={"chip" + (metricMode === "sql" ? " chip-on" : "")}
                onClick={() => setMetricMode("sql")}>Guarded SQL</button>
            </div>
            {metricMode === "metric" ? (
              <input className="sqllab-prompt" style={{ width: "100%", marginTop: 6 }} value={metricPrompt}
                placeholder="Metric prompt — e.g. revenue this month (resolved via the semantic layer)"
                onChange={(e) => setMetricPrompt(e.target.value)} />
            ) : (
              <textarea className="ap-goal" style={{ minHeight: 54, marginTop: 6 }} value={sql} spellCheck={false}
                placeholder="SELECT SUM(amount) AS v FROM sales   — SELECT-only, run through the same guard as chat"
                onChange={(e) => setSql(e.target.value)} />
            )}
            <div className="job-form-row" style={{ marginTop: 6 }}>
              <input className="sqllab-prompt" style={{ maxWidth: 140 }} value={column}
                placeholder="column (e.g. v)" onChange={(e) => setColumn(e.target.value)} />
              <select value={op} onChange={(e) => setOp(e.target.value)}>
                {OPS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
              <input className="sqllab-prompt" style={{ maxWidth: 120 }} value={value} type="number"
                placeholder="value" onChange={(e) => setValue(e.target.value)} />
            </div>
            <div className="meta ap-hint">Fires only when the value crosses the rule (not every tick).</div>
          </div>
        )}

        {/* Source + scope */}
        <div className="meta ap-label">Data scope</div>
        <div className="job-form-row">
          <label className="meta">Source</label>
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            {allowedSources.length === 0 && <option value="">no sources</option>}
            {allowedSources.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </select>
        </div>

        {trigger === "event" ? (
          <div className="job-form-row">
            <label className="meta">Watch table</label>
            <select value={eventTable} onChange={(e) => setEventTable(e.target.value)}>
              <option value="">select a table…</option>
              {tables.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
        ) : (
          tables.length > 0 && (
            <>
              <div className="meta ap-hint">Tables (none = all tables you can access)</div>
              <div className="inputs-row ap-tables">
                {tables.map((t) => (
                  <button key={t} className={"chip" + (sel.includes(t) ? " chip-on" : "")}
                    onClick={() => toggleTable(t)}>{t}</button>
                ))}
              </div>
            </>
          )
        )}

        {models.length > 0 && (
          <div className="job-form-row">
            <label className="meta">Model</label>
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">auto / default</option>
              {models.map((m) => (
                <option key={m.spec} value={m.spec} disabled={m.available === false}>
                  {m.name}{m.available === false ? " — no key" : ""}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* Optional governed action — the 'act with approval' seam. */}
        <button className="chip ap-adv" onClick={() => setActOpen((v) => !v)}>
          {actOpen ? "▾" : "▸"} Governed action (optional)
        </button>
        {actOpen && (
          <div className="ap-action">
            <div className="meta ap-hint">
              A write / pipeline this agent proposes after reading. It never runs autonomously —
              it becomes a supervised Job that a human admin approves.
            </div>
            <div className="job-form-row" style={{ marginTop: 6 }}>
              <select value={actKind} onChange={(e) => setActKind(e.target.value)}>
                <option value="sql_script">sql_script</option>
                <option value="spark_job">spark_job</option>
                <option value="platform_run">platform_run</option>
              </select>
              <input className="sqllab-prompt" style={{ maxWidth: 160 }} value={actTarget}
                placeholder="target (default: source)" onChange={(e) => setActTarget(e.target.value)} />
            </div>
            <textarea className="ap-goal" style={{ minHeight: 54, marginTop: 6 }} value={actScript} spellCheck={false}
              placeholder="Script / job body to propose for approval…" onChange={(e) => setActScript(e.target.value)} />
          </div>
        )}

        <div className="gov-actions" style={{ marginTop: 12 }}>
          <button className="chip chip-on" onClick={create} disabled={busy || !valid()}>
            {busy ? "creating…" : "✓ create agent"}
          </button>
          <button className="chip" onClick={() => { reset(); setOpen(false); }} disabled={busy}>cancel</button>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────── Run history ───────────────────────────────
function RunList({ agentId, refreshKey, onOpenJobs }) {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState("");
  const [openRun, setOpenRun] = useState(null);

  useEffect(() => {
    api(`/autopilot/${agentId}/runs`)
      .then((d) => setRuns(d.runs || []))
      .catch((e) => setError(e.message));
  }, [agentId, refreshKey]);

  if (error) return <div className="error" style={{ marginTop: 8 }}>{error}</div>;
  if (runs.length === 0) return <div className="meta" style={{ marginTop: 8 }}>No runs yet.</div>;

  return (
    <div className="ap-runs">
      {runs.map((r) => {
        const tone = RUN_TONE[r.status] || "warn";
        const res = r.result || {};
        const isOpen = openRun === r.id;
        return (
          <div key={r.id} className="ap-run">
            <div className="ap-run-head" onClick={() => setOpenRun(isOpen ? null : r.id)}>
              <span className={"query-tag flow-badge-" + tone}>{r.status}</span>
              <span className="meta">{r.trigger || "run"}</span>
              <span className="ap-run-summary">{r.summary || res.text || "—"}</span>
              <span className="meta">{whenText(r.started_at)}</span>
            </div>
            {r.proposed_job_id && (
              <div className="ap-run-gate">
                <span className="query-tag flow-badge-warn">⏳ pending approval</span>
                <button className="chip" onClick={onOpenJobs}>→ approve in Jobs</button>
              </div>
            )}
            {isOpen && (
              <div className="ap-run-body">
                {res.text && <div className="meta ap-run-text">{res.text}</div>}
                {res.sql && <pre className="flow-json" style={{ marginLeft: 0, whiteSpace: "pre-wrap" }}>{res.sql}</pre>}
                {r.trace_id && <div className="meta">trace {String(r.trace_id).slice(0, 8)}</div>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─────────────────────────────── Agent card ────────────────────────────────
function AgentCard({ agent, isAdmin, onChanged, onOpenJobs }) {
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [runKey, setRunKey] = useState(0);

  async function act(path, verb = "POST") {
    setBusy(path);
    setError("");
    try {
      await api(`/autopilot/${agent.id}${path}`, { method: verb });
      onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function runNow() {
    setBusy("run");
    setError("");
    try {
      await api(`/autopilot/${agent.id}/run`, { method: "POST" });
      setExpanded(true);
      setRunKey((k) => k + 1);
      onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    if (!confirm(`Delete agent "${agent.name}"? It will never fire again.`)) return;
    setBusy("del");
    try {
      await api(`/autopilot/${agent.id}`, { method: "DELETE" });
      onChanged();
    } catch (e) {
      setError(e.message);
      setBusy("");
    }
  }

  const last = agent.last_run;
  const lastTone = last ? RUN_TONE[last.status] || "warn" : "";

  return (
    <div className="query-card ap-card">
      <div className="query-head ap-head">
        <div className="query-title">
          <span className={"ap-status-dot " + (agent.enabled ? "ap-on" : "ap-off")} />
          {agent.name}
          <span className="query-tag">{triggerSummary(agent)}</span>
          {agent.owner_email && isAdmin && <span className="meta ap-owner">{agent.owner_email}</span>}
        </div>
        <div className="query-actions">
          {last && <span className={"query-tag flow-badge-" + lastTone} title={last.summary || ""}>last: {last.status}</span>}
          <button className="chip" onClick={runNow} disabled={!!busy}>
            {busy === "run" ? "…" : "▷ run now"}
          </button>
          <button className={"chip" + (agent.enabled ? " chip-on" : "")}
            onClick={() => act(agent.enabled ? "/disable" : "/enable")}
            disabled={!!busy}>
            {agent.enabled ? "on" : "off"}
          </button>
          <button className="chip" onClick={() => setExpanded((v) => !v)}>{expanded ? "▾" : "▸"} runs</button>
          <button className="chip ctx-danger" onClick={remove} disabled={!!busy}>✕</button>
        </div>
      </div>
      <div className="query-body">
        <div className="meta ap-goal-text">{agent.goal}</div>
        <div className="inputs-row" style={{ marginTop: 4 }}>
          <span className="query-tag">◆ {agent.source}</span>
          {(agent.tables && agent.tables.length > 0)
            ? agent.tables.map((t) => <span key={t} className="query-tag">{t}</span>)
            : <span className="meta">all accessible tables</span>}
          {agent.model && <span className="query-tag">{agent.model}</span>}
          {agent.next_run_at && agent.enabled && (
            <span className="meta">next: {whenText(agent.next_run_at)}</span>
          )}
        </div>
        {error && <div className="error" style={{ marginTop: 6 }}>{error}</div>}
        {expanded && <RunList agentId={agent.id} refreshKey={runKey} onOpenJobs={onOpenJobs} />}
      </div>
    </div>
  );
}

// ─────────────────────────────── Panel ─────────────────────────────────────
export default function Autopilot({ user, onClose, onOpenJobs }) {
  const isAdmin = user?.role === "admin";
  const [agents, setAgents] = useState([]);
  const [sources, setSources] = useState([]);
  const [models, setModels] = useState([]);
  const [loaded, setLoaded] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState("");

  function refresh() {
    api("/autopilot")
      .then((d) => { setAgents(d.agents || []); setUnavailable(false); })
      .catch((e) => {
        // Backend not wired yet → show an empty, non-broken shell.
        if (/404|Not Found/i.test(e.message)) setUnavailable(true);
        else setError(e.message);
      })
      .finally(() => setLoaded(true));
  }

  useEffect(() => {
    refresh();
    api("/catalog/sources").then((s) => setSources(Array.isArray(s) ? s : [])).catch(() => {});
    api("/models").then((m) => setModels(Array.isArray(m) ? m : [])).catch(() => {});
  }, []);

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Autopilot agents</div>
          <div className="meta">
            Autonomous agents that fire on a trigger — a schedule, a metric threshold, new data,
            or a manual run — reason toward a goal as you (never above your access), and take
            governed actions. Reads run on their own; any write is proposed for human approval in Jobs.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {unavailable && (
        <div className="sqllab-result bad" style={{ margin: "8px 0" }}>
          The autopilot service isn't available yet. Once the backend is wired, your agents appear here.
        </div>
      )}

      {!unavailable && <CreateForm sources={sources} models={models} onCreated={refresh} />}

      {loaded && !unavailable && agents.length === 0 && (
        <div className="meta" style={{ margin: "10px 0" }}>
          No agents yet. Create one above — give it a goal, a trigger, and a data scope.
        </div>
      )}

      <div className="query-list ap-list">
        {agents.map((a) => (
          <AgentCard key={a.id} agent={a} isAdmin={isAdmin} onChanged={refresh} onOpenJobs={onOpenJobs} />
        ))}
      </div>

      {isAdmin && agents.length > 0 && (
        <div className="meta" style={{ marginTop: 12 }}>Admin view — showing all users' agents.</div>
      )}
    </section>
  );
}
