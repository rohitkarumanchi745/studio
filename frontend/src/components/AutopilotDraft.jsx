// A proposed Autopilot agent, in the chat. The chat proposes one when a prompt
// asks for something to happen again ("every morning show me revenue by
// region", "alert me when revenue drops below 1M" — backend
// autopilot_intent.py), and ⏱ Automate turns any answer into one. Nothing is
// created until the user checks the card and presses Create; the agent then
// posts every run to this chat (an agent made in the D3 & artifacts window
// draws its answers with D3).
import { useEffect, useState } from "react";
import { api } from "../api";
import { INTERVALS, OPS } from "./Autopilot";

const TRIGGERS = [
  { id: "schedule", label: "Schedule" },
  { id: "threshold", label: "Threshold" },
  { id: "event", label: "New data" },
];

/** A draft from an answer: its question, on its source, every day. */
export function draftFromAnswer(m, requirement, visual) {
  const goal = (requirement || m.chart?.title || "").trim();
  return {
    trigger_type: "schedule", trigger_config: { interval_seconds: 86400 },
    goal, name: goal.split(/\s+/).slice(0, 5).join(" ") || "Autopilot agent",
    source: m.source && m.source !== "*" && m.source !== "auto" ? m.source : null,
    tables: [], visual: Boolean(visual),
  };
}

/** The agent this card already made, if any — so a reload does not offer
 *  to create it twice. */
export function createdFrom(agents, draft, conversationId) {
  return (agents || []).find((a) => a.conversation_id === conversationId
    && a.goal === draft.goal && a.trigger_type === draft.trigger_type) || null;
}

/** The request body for POST /autopilot. */
export function agentBody(state, draft, conversationId) {
  const config = state.trigger === "schedule" ? { interval_seconds: Number(state.interval) }
    : state.trigger === "threshold" ? { metric_prompt: state.metric.trim(), op: state.op,
                                        value: Number(state.value), interval_seconds: 300 }
    : { table: state.table.trim(), interval_seconds: 300 };
  return {
    name: state.name.trim(), goal: state.goal.trim(), trigger_type: state.trigger,
    trigger_config: config, source: state.source, tables: draft.tables || [],
    conversation_id: conversationId || undefined, visual: Boolean(draft.visual),
  };
}

export default function AutopilotDraft({ draft, conversationId, onCreated, onDismiss }) {
  const cfg = draft.trigger_config || {};
  const [state, setState] = useState({
    name: draft.name || "", goal: draft.goal || "", trigger: draft.trigger_type || "schedule",
    interval: cfg.interval_seconds && draft.trigger_type === "schedule" ? cfg.interval_seconds : 86400,
    metric: cfg.metric_prompt || "", op: cfg.op || "lt",
    value: cfg.value != null ? String(cfg.value) : "", table: cfg.table || "",
    source: draft.source || "",
  });
  const [sources, setSources] = useState([]);
  const [runNow, setRunNow] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState(null);
  const set = (k) => (e) => setState((s) => ({ ...s, [k]: e.target.value }));

  useEffect(() => {
    api("/autopilot").then((d) => setCreated(createdFrom(d.agents, draft, conversationId)))
      .catch(() => {});
    if (!draft.source) {
      api("/catalog/sources")
        .then((list) => setSources((Array.isArray(list) ? list : [])
          .filter((s) => s.allowed && s.configured)))
        .catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  const ready = state.name.trim() && state.goal.trim() && state.source
    && (state.trigger !== "threshold" || (state.metric.trim() && state.value !== ""
                                          && !Number.isNaN(Number(state.value))))
    && (state.trigger !== "event" || state.table.trim());

  async function create() {
    if (!ready || busy) return;
    setBusy(true);
    setError("");
    try {
      const agent = await api("/autopilot", { method: "POST",
        body: JSON.stringify(agentBody(state, draft, conversationId)) });
      if (runNow) await api(`/autopilot/${agent.id}/run`, { method: "POST" });
      setCreated(agent);
      if (onCreated) onCreated(agent);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <div className="ap-draft ap-draft-done" role="status">
        ✓ <b>{created.name}</b> is on — each run posts to this chat
        {created.visual ? ", drawn with D3" : ""}.{" "}
        <a className="chip" href="/autopilot">manage in Autopilot</a>
      </div>
    );
  }

  return (
    <div className="ap-draft" aria-label="Proposed Autopilot agent">
      <div className="ap-draft-head">
        <b>✦ Autopilot agent</b>
        {draft.visual && <span className="artifact-badge">🎨 draws with D3</span>}
        {onDismiss && <button className="chip" onClick={onDismiss} title="Not now">✕</button>}
      </div>
      <input className="sqllab-prompt" value={state.name} onChange={set("name")}
             placeholder="Agent name" aria-label="Agent name" />
      <textarea className="sqllab-prompt" rows={2} value={state.goal} onChange={set("goal")}
                placeholder="What it should find out each time" aria-label="Goal" />
      <div className="inputs-row">
        {TRIGGERS.map((t) => (
          <button key={t.id} className={"chip" + (state.trigger === t.id ? " chip-on" : "")}
                  onClick={() => setState((s) => ({ ...s, trigger: t.id }))}>{t.label}</button>
        ))}
      </div>
      {state.trigger === "schedule" && (
        <div className="inputs-row">
          <span className="meta">Run</span>
          <select value={state.interval} onChange={set("interval")} aria-label="How often">
            {INTERVALS.map((i) => <option key={i.s} value={i.s}>{i.label}</option>)}
          </select>
        </div>
      )}
      {state.trigger === "threshold" && (
        <div className="inputs-row">
          <span className="meta">When</span>
          <input value={state.metric} onChange={set("metric")} placeholder="metric, e.g. revenue"
                 aria-label="Metric" />
          <select value={state.op} onChange={set("op")} aria-label="Comparison">
            {OPS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
          </select>
          <input value={state.value} onChange={set("value")} placeholder="value" aria-label="Value"
                 style={{ width: 110 }} />
          <span className="meta">— checked every 5 minutes</span>
        </div>
      )}
      {state.trigger === "event" && (
        <div className="inputs-row">
          <span className="meta">When new rows land in</span>
          <input value={state.table} onChange={set("table")} placeholder="table" aria-label="Table" />
        </div>
      )}
      <div className="inputs-row">
        <span className="meta">Source</span>
        {draft.source ? <span className="query-tag">◆ {draft.source}</span> : (
          <select value={state.source} onChange={set("source")} aria-label="Source">
            <option value="">choose a source…</option>
            {sources.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </select>
        )}
        {(draft.tables || []).map((t) => <span key={t} className="query-tag">{t}</span>)}
      </div>
      <label className="meta ap-draft-run">
        <input type="checkbox" checked={runNow} onChange={(e) => setRunNow(e.target.checked)} />
        Run once now, so the first answer is here right away
      </label>
      {error && <div className="error">{error}</div>}
      <div className="inputs-row">
        <button className="primary" onClick={create} disabled={!ready || busy}>
          {busy ? "creating…" : "✓ Create agent"}
        </button>
        <span className="meta">Its answers post to this chat. Reads only — any write waits for approval.</span>
      </div>
    </div>
  );
}
