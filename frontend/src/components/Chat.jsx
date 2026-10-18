import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import Canvas from "./Canvas";
import ChartView from "./ChartView";
import Markdown from "./Markdown";
import SqlLab from "./SqlLab";
import ChatPipeline from "./ChatPipeline";
import ChatWorkflow from "./ChatWorkflow";
import ChatPlatformRun, { PlatformComposer } from "./ChatPlatformRun";
import { chatPipelineFields, PlanningContextPicker, PlanningSources,
  usePlanningCatalog } from "./Pipelines";

// "Demo agent" for a single worker; "Snowflake agent + Databricks agent →
// Aggregator" when a question fanned out across sources.
function agentLabel(agents) {
  if (!agents?.length) return "";
  const workers = agents.filter((a) => a.role !== "aggregator");
  const names = workers.map((a) => a.name).join(" + ");
  return agents.some((a) => a.role === "aggregator") ? `${names} → Aggregator` : names;
}

const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** How many agents a task used, and which: from a finished answer's `agents`
 *  ({name, role}), or — while it runs — from the agents its progress steps
 *  name. {total, text, detail, names}; total 0 when nothing names an agent. */
export function agentSummary(agents = [], steps = []) {
  const seen = new Map();
  for (const a of agents || []) if (a?.name) seen.set(a.name, a.role || "worker");
  for (const s of steps || []) if (s?.agent && !seen.has(s.agent)) seen.set(s.agent, "agent");
  const names = [...seen.keys()];
  const roles = [...seen.values()];
  const count = (r) => roles.filter((x) => x === r).length;
  const parts = [];
  if (count("planner")) parts.push(plural(count("planner"), "planner"));
  if (count("worker")) parts.push(plural(count("worker"), "worker"));
  if (count("aggregator")) parts.push("Aggregator");
  if (count("orchestrator")) parts.push("Orchestrator");
  return { total: names.length, names, text: plural(names.length, "agent"),
           detail: parts.length > 1 ? parts.join(" · ") : "" };
}

function buildCanvas(m) {
  const panels =
    m.panels?.length > 0
      ? m.panels
      : [{ sql: m.sql, columns: m.columns, rows: m.rows, chart: m.chart }];
  return {
    panels,
    selected: 0,
    source: m.source,
    parentMessageId: m.message_id,
    note: "",
    // `p.rows ?? []`: a panel without rows (a redacted message, a cached plan
    // that returned none) used to throw here — and the caller swallowed it and
    // re-polled forever, so the chat hung on "agent is querying…" with the
    // answer already saved. A missing-rows panel must degrade, never throw.
    original: panels.map((p) => ({ ...p, rows: (p.rows ?? []).map((r) => [...r]) })),
  };
}

function elapsedTime(seconds) {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const hours = Math.floor(minutes / 60);
  if (hours) return `${hours}:${String(minutes % 60).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

// Task progress is a record of observable work, not the model's reasoning.
// Completed steps are loaded from the owner-only task endpoint on expansion.
export function AgentActivity({ steps = [], graph, running = false, failed = false, taskId, startedAt,
                               agents }) {
  const [loadedSteps, setLoadedSteps] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const mountedAt = useRef(Date.now());
  const requested = useRef(false);
  const events = loadedSteps ?? steps;
  const firstEvent = events.find((step) => Number.isFinite(step?.t));
  const lastEvent = [...events].reverse().find((step) => Number.isFinite(step?.t));
  const start = startedAt || (firstEvent ? firstEvent.t * 1000 : mountedAt.current);
  const end = running ? now : (lastEvent ? lastEvent.t * 1000 : start);
  const duration = elapsedTime((end - start) / 1000);
  const current = events[events.length - 1]?.label;
  const nodes = Array.isArray(graph?.nodes) ? graph.nodes : [];
  const nodeNames = Object.fromEntries(nodes.map((node) => [node.id, node.agent || node.source || node.id]));
  const recorded = events.length
    ? `${events.length} ${events.length === 1 ? "event" : "events"}`
    : loadedSteps !== null ? "No recorded events" : "View recorded steps";
  const summaryStatus = failed ? `Run failed · ${recorded}` : running ? current || "Working…" : recorded;
  // How many agents this task is using: live from the steps they emit, then
  // the answer's final roster once it is saved.
  const crew = agentSummary(agents, events);

  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  async function onToggle(event) {
    if (!event.currentTarget.open || running || !taskId || requested.current || events.length) return;
    requested.current = true;
    setLoadError("");
    setLoading(true);
    try {
      const task = await api(`/tasks/${encodeURIComponent(taskId)}`);
      setLoadedSteps(Array.isArray(task.steps) ? task.steps : []);
    } catch (error) {
      if (error.status !== 403 && error.status !== 404) requested.current = false;
      setLoadError("Activity details aren't available for this conversation.");
    } finally {
      setLoading(false);
    }
  }

  const canShowGraph = running || events.length > 0 || loadedSteps !== null;
  return (
    <details className={"agent-activity-panel" + (failed ? " is-failed" : "")} open={running} onToggle={onToggle}>
      <summary className="agent-activity-summary">
        <span className={"activity-indicator" + (running ? " is-running" : failed ? " is-failed" : "")} aria-hidden="true" />
        <span className="activity-summary-text">
          <strong>Agent activity</strong>
          <span className="activity-current" role={running ? "status" : undefined} aria-live={running ? "polite" : undefined}>
            {summaryStatus}
          </span>
        </span>
        {crew.total > 0 && (
          <span className="activity-agents" title={crew.names.join(", ")}
                aria-label={`${crew.text}${running ? " working" : ""}`}>
            👥 {crew.text}{running ? " working" : ""}
          </span>
        )}
        {(running || events.length > 0) && <span className="activity-duration" aria-label={`${duration} elapsed`}>{duration}</span>}
        <span className="activity-chevron" aria-hidden="true" />
      </summary>
      <div className="activity-content">
        {loading && <p className="activity-note" role="status">Loading activity…</p>}
        {loadError && <p className="activity-note">{loadError}</p>}
        {!loading && !loadError && events.length > 0 && (
          <ol className="activity-events">
            {events.map((step, i) => (
              <li key={`${step.t ?? ""}-${i}`} className="activity-event">
                <span className="activity-event-label">{step.label}</span>
                {Number.isFinite(step.t) && firstEvent && (
                  <span className="activity-event-time">{elapsedTime(step.t - firstEvent.t)}</span>
                )}
              </li>
            ))}
          </ol>
        )}
        {!loading && !loadError && running && !events.length && (
          <p className="activity-note">Waiting for the first progress update…</p>
        )}
        {!loading && !loadError && !running && loadedSteps !== null && !events.length && (
          <p className="activity-note">No progress events were recorded for this {failed ? "run" : "answer"}.</p>
        )}
        {crew.total > 0 && !loadError && (
          <section className="activity-crew" aria-label="Agents">
            <h4>Agents · {crew.text}{crew.detail ? ` (${crew.detail})` : ""}</h4>
            <div className="activity-crew-list">
              {crew.names.map((name) => <span key={name} className="chip activity-crew-agent">{name}</span>)}
            </div>
          </section>
        )}
        {canShowGraph && nodes.length > 0 && !loadError && (
          <section className="activity-graph" aria-label="Agent graph">
            <h4>Agent graph</h4>
            <ol className="activity-nodes">
              {nodes.map((node) => (
                <li key={node.id} className="activity-node">
                  <span className="activity-node-name">{node.agent || node.source || node.id}</span>
                  <span className={`activity-node-status is-${node.status === "ok" || node.status === "failed" ? node.status : "pending"}`}>
                    {node.status === "ok" ? "Finished" : node.status === "failed" ? "Failed" : "Pending"}
                  </span>
                  {node.depends_on?.length > 0 && (
                    <span className="activity-node-deps">After {node.depends_on.map((id) => nodeNames[id] || id).join(", ")}</span>
                  )}
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
    </details>
  );
}

/** A D3 & artifacts conversation (its title carries the 🎨 the server gave it). */
export function isVisualConversation(c) {
  return typeof c?.title === "string" && c.title.startsWith("🎨");
}

// visualMode: the D3 & artifacts window. Same agent, sources, history and
// tasks as the chat — but every answer is visualized, drawn with D3 alone (or
// as a custom visual the AI writes), and shown right in the thread.
export default function Chat({ conversationId, onConversationCreated, onOpenDashboard, modelsEpoch,
                               visualMode = false, onOpenCharts, onConversationMissing }) {
  const [canvas, setCanvas] = useState(null);
  const [chatOpen, setChatOpen] = useState(true);
  const [models, setModels] = useState([]);
  const [model, setModel] = useState(localStorage.getItem("studio_model") || "");

  useEffect(() => {
    api("/models")
      .then((ms) => {
        setModels(ms);
        if (!ms.find((m) => m.spec === localStorage.getItem("studio_model"))) {
          const def = ms.find((m) => m.default) || ms[0];
          if (def) setModel(def.spec);
        }
      })
      .catch(() => {});
  }, [modelsEpoch]);

  useEffect(() => {
    if (model) localStorage.setItem("studio_model", model);
  }, [model]);
  const [sources, setSources] = useState([]);
  const [source, setSource] = useState("demo");
  const [tables, setTables] = useState([]);
  const [sel, setSel] = useState([]); // selected tables; empty = all
  const [pickerOpen, setPickerOpen] = useState(false);
  // "*" = multi-agent mode: one agent per database, orchestrated server-side.
  const orchestrated = source === "*";
  const tableLabel = orchestrated
    ? "all sources"
    : sel.length === 0 ? "✳ All tables" : sel.length === 1 ? sel[0] : `${sel.length} tables`;
  const [messages, setMessages] = useState([]);
  const [prompt, setPrompt] = useState("");
  const [buildPipeline, setBuildPipeline] = useState(false);
  const [buildAirflow, setBuildAirflow] = useState(false);
  const [repositoryId, setRepositoryId] = useState("");
  const [confluencePageIds, setConfluencePageIds] = useState([]);
  const planningCatalog = usePlanningCatalog();
  const [showPlatform, setShowPlatform] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Why this is a new chat, when the one that was open turned out to be gone.
  const [notice, setNotice] = useState("");
  const [failedActivity, setFailedActivity] = useState(null);
  // Live activity: the steps the agent has emitted so far this turn, refreshed
  // by the task poll — so the "thinking" bubble shows what is actually happening.
  const [liveSteps, setLiveSteps] = useState([]);
  const [activityByTask, setActivityByTask] = useState({});
  const [activeStartedAt, setActiveStartedAt] = useState(null);
  const endRef = useRef(null);
  // Background-task polling lives in refs, NOT state/effects: creating a new
  // conversation changes conversationId, and an effect keyed on it used to
  // reset the poll mid-flight — so a fast (cached) task finished but nothing
  // reloaded and the chat sat on "agent is querying…" forever. Refs survive
  // re-renders and conversation switches; the loop reconciles against the
  // *current* open conversation each tick.
  const activeConvRef = useRef(conversationId);
  activeConvRef.current = conversationId;
  const pollingRef = useRef(null); // task id currently being polled (dedup)
  const taskViewEpochRef = useRef(0); // invalidates an older reopen lookup after a new turn

  useEffect(() => {
    api("/catalog/sources").then(setSources).catch(() => {});
  }, []);

  // Group the picker by warehouse, and show each source's namespace beside it.
  // A Studio source IS one namespace — it is pinned to the database/schema it
  // was connected with — so "warehouse then schema" is spelled here as a group
  // of sources rather than a second dropdown: picking `sales-sf · ACME.SALES`
  // is picking that schema. Env-configured sources carry no namespace metadata
  // and fall into a group of their own at the end.
  const sourceGroups = useMemo(() => {
    const ENV = "Built-in and environment sources";
    const groups = [];
    for (const s of sources) {
      const label = s.type_label || ENV;
      let g = groups.find((x) => x.label === label);
      if (!g) groups.push((g = { label, items: [] }));
      g.items.push(s);
    }
    for (const g of groups) g.items.sort((a, b) => a.name.localeCompare(b.name));
    return groups.sort((a, b) =>
      a.label === ENV ? 1 : b.label === ENV ? -1 : a.label.localeCompare(b.label));
  }, [sources]);

  // An unsent draft survives a refresh / conversation switch: keyed per
  // conversation in localStorage, cleared once the prompt is sent (send()
  // empties the input, which removes the stored draft).
  const draftKey = "studio_draft:" + (conversationId || "new");
  useEffect(() => {
    setPrompt(localStorage.getItem(draftKey) || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);
  useEffect(() => {
    if (prompt) localStorage.setItem(draftKey, prompt);
    else localStorage.removeItem(draftKey);
  }, [prompt, draftKey]);

  useEffect(() => {
    setTables([]);
    setSel([]);
    if (!source || source === "*") return;
    api(`/catalog/sources/${source}/tables`)
      .then((t) => {
        setTables(t);
        // default: chat across the whole source (empty selection)
      })
      .catch((e) => setError(e.message));
  }, [source]);

  useEffect(() => {
    const epoch = ++taskViewEpochRef.current;
    setError("");
    setFailedActivity(null);
    setBusy(false);
    setLiveSteps([]);
    setActiveStartedAt(null);
    // Another conversation's chart must not stay open over this one.
    setCanvas(null);
    setChatOpen(true);
    if (!conversationId) {
      setMessages([]);
      return;
    }
    setNotice("");
    api(`/conversations/${conversationId}/messages`)
      .then((ms) => {
        if (taskViewEpochRef.current !== epoch || activeConvRef.current !== conversationId) return;
        loadMessages(ms);
        // If a background task is still running here (e.g. after a refresh or a
        // chat switch), resume the live "working…" state. pollFor dedups, so
        // this never double-polls a task send() already started.
        api(`/conversations/${conversationId}/task`)
          .then((t) => {
            if (taskViewEpochRef.current !== epoch || activeConvRef.current !== conversationId) return;
            if (t.status === "running") {
              setBusy(true);
              pollFor(t.id, conversationId);
            } else if (t.status === "failed" && t.id) {
              setError(t.error || "The task failed.");
              setFailedActivity({taskId: t.id, steps: []});
              // The latest-task endpoint gives the task ID and error. Fetch
              // recorded steps from the owner-only task endpoint on reopen.
              api(`/tasks/${encodeURIComponent(t.id)}`)
                .then((st) => {
                  if (taskViewEpochRef.current !== epoch || activeConvRef.current !== conversationId) return;
                  setFailedActivity({taskId: t.id, steps: Array.isArray(st.steps) ? st.steps : []});
                  setError(st.error || t.error || "The task failed.");
                })
                .catch(() => {}); // the disclosure can retry when opened
            }
          })
          .catch(() => {});
      })
      .catch((err) => {
        if (taskViewEpochRef.current !== epoch || activeConvRef.current !== conversationId) return;
        setMessages([]);
        setCanvas(null);
        if (err?.status === 404) leaveMissingConversation(conversationId);
      });
  }, [conversationId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  // A conversation can disappear under an open link: deleted in another tab,
  // no longer shared, or never in this database at all (Studio once ran on its
  // fallback store for an hour). Its id used to trap the user — the chat opened
  // empty and every question failed with "Conversation not found". Now they
  // land in a new chat, told why, with whatever they had asked or typed.
  function leaveMissingConversation(id, question) {
    if (!onConversationMissing) return false;
    const draft = question || localStorage.getItem("studio_draft:" + id) || "";
    localStorage.removeItem("studio_draft:" + id);
    if (draft) localStorage.setItem("studio_draft:new", draft);
    setNotice("That conversation isn't available any more — it may have been deleted or is no "
              + "longer shared with you — so this is a new chat."
              + (draft ? " What you asked is in the box below; send it to ask here." : ""));
    onConversationMissing(id);
    return true;
  }

  // A send that failed. A 404 may mean the conversation itself is gone, which
  // is checked with the server (not guessed from the message) before leaving.
  async function sendFailed(err, question) {
    const id = conversationId;
    if (err?.status === 404 && id && onConversationMissing) {
      const gone = await api(`/conversations/${id}/messages`)
        .then(() => false, (e) => e?.status === 404);
      if (gone && activeConvRef.current === id && leaveMissingConversation(id, question)) {
        setBusy(false);
        return;
      }
    }
    setError(err.message);
    setBusy(false);
  }

  async function send(e) {
    e?.preventDefault();
    const q = prompt.trim();
    if (!q || busy) return;
    taskViewEpochRef.current++;
    setPrompt("");
    setError("");
    setFailedActivity(null);
    // Optimistically show the question + a working placeholder; the answer is
    // produced in the background, so the user can switch to another chat and
    // start a different task without waiting.
    setMessages((m) => [...m, { role: "user", text: q, source, table: tableLabel }]);
    setBusy(true);
    setLiveSteps([]);
    setActiveStartedAt(Date.now());
    try {
      const data = await api("/chat/background", {
        method: "POST",
        body: JSON.stringify({
          prompt: q,
          source,
          table: orchestrated || sel.length !== 1 ? "*" : sel[0],
          tables: !orchestrated && sel.length > 1 ? sel : undefined,
          conversation_id: conversationId,
          model: model || undefined,
          ...(visualMode ? { visual: true } : chatPipelineFields(buildPipeline, buildAirflow,
            repositoryId, confluencePageIds)),
        }),
      });
      // Building is a one-turn action; follow-ups such as "run this pipeline"
      // must be interpreted normally after the draft has been requested.
      setBuildPipeline(false);
      setBuildAirflow(false);
      if (!conversationId) onConversationCreated(data.conversation_id);
      pollFor(data.task_id, data.conversation_id);
    } catch (err) {
      await sendFailed(err, q);
    }
  }

  async function runPipeline(message, action = "run") {
    const pipeline = message.pipeline;
    const workflow = pipeline?.execution_mode === "airflow_dag";
    if (busy || !conversationId || !message.message_id || (!workflow && pipeline?.status !== "ready")) return;
    // Workflow cards use freshly polled state. The saved message may still
    // say awaiting_approval after completion; the server checks the live job
    // before deciding whether to reuse its pending request or create a rerun.
    // Bind this action to the saved answer and its source. The current picker
    // may have changed since this pipeline was drafted.
    const pipelineSource = pipeline.source;
    if (!pipelineSource || (!workflow && (pipeline.dropped?.length || !pipeline.steps?.length || pipeline.steps.some((s) => !s.verified || !s.sql)))) return;
    taskViewEpochRef.current++;
    const q = action === "status" ? "Check pipeline status" : workflow ? "Submit this pipeline" : "Run this pipeline";
    setError("");
    setFailedActivity(null);
    setMessages((ms) => [...ms, { role: "user", text: q, source: pipelineSource, table: "*" }]);
    setBusy(true);
    setLiveSteps([]);
    setActiveStartedAt(Date.now());
    try {
      const data = await api("/chat/background", {
        method: "POST",
        body: JSON.stringify({
          prompt: q,
          source: pipelineSource,
          table: "*",
          conversation_id: conversationId,
          model: model || undefined,
          pipeline_action: action,
          pipeline_message_id: message.message_id,
        }),
      });
      pollFor(data.task_id, data.conversation_id);
    } catch (err) {
      await sendFailed(err);
    }
  }

  async function platformAction(fields, text) {
    if (busy) return;
    taskViewEpochRef.current++;
    setError("");
    setFailedActivity(null);
    setBusy(true);
    setLiveSteps([]);
    setActiveStartedAt(Date.now());
    setMessages((ms) => [...ms, { role: "user", text, source: "*", table: "platform run" }]);
    try {
      const data = await api("/chat/background", {
        method: "POST",
        body: JSON.stringify({ prompt: text, source: "*", table: "*", conversation_id: conversationId, ...fields }),
      });
      setShowPlatform(false);
      if (!conversationId) onConversationCreated(data.conversation_id);
      pollFor(data.task_id, data.conversation_id);
    } catch (e) { await sendFailed(e); }
  }

  // A clarification chip re-asks the same question scoped the way the user
  // chose: ONE source (the picker follows, so follow-ups stay there) or all
  // sources side by side (allow_ambiguous skips the check for this ask).
  async function resend(q, opt) {
    if (busy) return;
    taskViewEpochRef.current++;
    setError("");
    setFailedActivity(null);
    const both = opt.source === "*";
    // The picker follows the choice both ways, so the toolbar always shows
    // where the question went and where the next typed one will go.
    setSource(both ? "*" : opt.source);
    setSel([]);
    setMessages((m) => [...m, { role: "user", text: q, source: opt.source,
                                table: both ? "all sources" : opt.source }]);
    setBusy(true);
    setLiveSteps([]);
    setActiveStartedAt(Date.now());
    try {
      const data = await api("/chat/background", {
        method: "POST",
        body: JSON.stringify({
          prompt: q,
          source: opt.source,
          table: "*",
          conversation_id: conversationId,
          model: model || undefined,
          allow_ambiguous: both,
          ...(visualMode ? { visual: true } : {}),
        }),
      });
      if (!conversationId) onConversationCreated(data.conversation_id);
      pollFor(data.task_id, data.conversation_id);
    } catch (err) {
      await sendFailed(err, q);
    }
  }

  function loadMessages(ms) {
    const loaded = ms.map((m) => ({ role: m.role, ...m.content, message_id: m.id }));
    setMessages(loaded);
    // The canvas opens when the user asks for it (⤢ on an answer), never on
    // its own: a new answer or a reopened conversation shows the chat. A
    // canvas the user already has open follows the newest chart, so a
    // follow-up question updates what they are looking at. (The D3 &
    // artifacts window never opens it: it draws its charts in the thread.)
    const last = [...loaded].reverse().find(
      (m) => m.role === "assistant" && (m.panels?.length || (m.chart && m.rows?.length)));
    setCanvas((open) => (open && last && !visualMode ? buildCanvas(last) : null));
    return loaded;
  }

  // Poll one background task to completion, then — only if its conversation is
  // still the open one — reload it. Ref-driven so re-renders and conversation
  // switches can't cancel it (the old effect-based poll did, and lost the race
  // on a fast task → stuck on "agent is querying…"); dedups on task id so it
  // never runs twice.
  function pollFor(taskId, cid) {
    if (pollingRef.current === taskId) return;
    pollingRef.current = taskId;
    const started = Date.now();
    let failures = 0;
    const tick = async () => {
      if (pollingRef.current !== taskId) return;        // superseded by a newer task
      let st;
      try {
        st = await api(`/tasks/${taskId}`);
        failures = 0;
      } catch (e) {
        // Never spin forever on a broken poll: after repeated failures, say so.
        if (++failures >= 5) {
          pollingRef.current = null;
          setBusy(false);
          setError(`Lost contact with the server while waiting for the answer (${e.message}). It may still have completed — reopen this chat to check.`);
          return;
        }
        setTimeout(tick, 3000);
        return;
      }
      if (st.status === "running") {
        if (activeConvRef.current === cid) {
          setLiveSteps(st.steps || []);
          setActiveStartedAt((at) => at || (st.steps?.[0]?.t ? st.steps[0].t * 1000 : Date.now()));
        }
        // Watchdog: a task that never reaches a terminal state must not hang the
        // UI silently — surface it so the failure is diagnosable, not invisible.
        if (Date.now() - started > 5 * 60 * 1000) {
          pollingRef.current = null;
          setBusy(false);
          setError("The agent is taking unusually long. It's still running server-side — reopen this chat shortly to see the answer.");
          return;
        }
        setTimeout(tick, 1500);
        return;
      }
      pollingRef.current = null;                        // done or failed
      if (activeConvRef.current === cid && st.status === "done" && Array.isArray(st.steps)) {
        setActivityByTask((saved) => ({...saved, [taskId]: st.steps}));
      }
      if (activeConvRef.current === cid && st.status === "failed") {
        setFailedActivity({taskId, steps: Array.isArray(st.steps) ? st.steps : []});
        setError(st.error || "The task failed.");
      }
      // Clear the spinner even if the user switched chats — leaving busy set
      // is what made the composer look permanently stuck.
      setBusy(false);
      setLiveSteps([]);
      if (activeConvRef.current === cid) {
        try {
          const ms = await api(`/conversations/${cid}/messages`);
          if (activeConvRef.current === cid) handleModelError(loadMessages(ms));
        } catch { /* keep the optimistic messages rather than blank the view */ }
      }
    };
    setTimeout(tick, 1500);
  }

  // A model can be "available" yet fail (no credit, revoked key). After a run
  // completes, drop back to the server default so a stale per-device choice
  // can't leave this browser stuck.
  function handleModelError(loaded) {
    const me = [...loaded].reverse().find((m) => m.role === "assistant")?.model_error;
    if (me?.retryable_with_default) {
      const def = models.find((x) => x.default) || models[0];
      if (def && def.spec !== me.spec) {
        setModel(def.spec);
        setError(`${me.spec} failed (${me.detail.slice(0, 90)}…) — switched to ${def.name}. Ask again.`);
      }
    }
  }

  // Starter questions come from the selected table's own schema, so they can
  // actually be answered by the data in front of you.
  const [suggestions, setSuggestions] = useState([]);
  const [sugBusy, setSugBusy] = useState(false);
  const selKey = orchestrated ? "*" : sel.length ? sel.join(",") : "*";

  useEffect(() => {
    if (!source) return;
    let cancelled = false;
    setSugBusy(true);
    setSuggestions([]);
    api(`/catalog/sources/${orchestrated ? "demo" : source}/suggestions?table=${encodeURIComponent(selKey)}`)
      .then((d) => !cancelled && setSuggestions(d.suggestions || []))
      .catch(() => !cancelled && setSuggestions([]))
      .finally(() => !cancelled && setSugBusy(false));
    return () => {
      cancelled = true;
    };
  }, [source, selKey, orchestrated]);

  return (
    <div className={"workspace" + (canvas ? " with-canvas" : "") + (canvas && !chatOpen ? " chat-hidden" : "")}>
      {canvas && (
        <Canvas
          state={canvas}
          setState={setCanvas}
          onClose={() => { setCanvas(null); setChatOpen(true); }}
          chatOpen={chatOpen}
          onToggleChat={() => setChatOpen(!chatOpen)}
          conversationId={conversationId}
          tableLabel={tableLabel}
          onNewVersion={(msg) => setMessages((m) => [...m, { role: "assistant", ...msg }])}
          onOpenDashboard={onOpenDashboard}
        />
      )}
      {canvas && !chatOpen && (
        <button className="chat-reopen" onClick={() => setChatOpen(true)} title="Open chat">
          💬
        </button>
      )}
      <main className={"chatpane" + (visualMode ? " visual-chat" : "")}>
      <div className="toolbar">
        <label>Source</label>
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="*">✳ all sources — multi-agent</option>
          {sourceGroups.map((g) => (
            <optgroup key={g.label} label={g.label}>
              {g.items.map((s) => (
                <option key={s.name} value={s.name} disabled={!s.allowed || !s.configured}>
                  {s.name}{s.namespace ? ` · ${s.namespace}` : ""}
                  {!s.configured ? " (not configured)" : !s.allowed ? " (no access)" : ""}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        <label>Table</label>
        <div className="tpicker">
          <button type="button" className="tpicker-btn" disabled={orchestrated} onClick={() => setPickerOpen(!pickerOpen)}>
            {orchestrated ? "agent-routed" : tableLabel} ▾
          </button>
          {pickerOpen && (
            <div className="tpicker-panel">
              <label className="tpicker-item">
                <input type="checkbox" checked={sel.length === 0} onChange={() => setSel([])} />
                ✳ All tables
              </label>
              {tables.map((t) => (
                <label key={t} className="tpicker-item">
                  <input
                    type="checkbox"
                    checked={sel.includes(t)}
                    onChange={() =>
                      setSel(sel.includes(t) ? sel.filter((x) => x !== t) : [...sel, t])
                    }
                  />
                  {t}
                </label>
              ))}
              <button type="button" className="chip" onClick={() => setPickerOpen(false)}>
                done
              </button>
            </div>
          )}
        </div>
        {visualMode && (
          <span className="visual-toolbar-right">
            <span className="artifact-badge">🎨 D3 &amp; artifacts</span>
            {onOpenCharts && (
              <button type="button" className="chip" onClick={onOpenCharts}
                      title="Choose a D3 chart or a custom visual for a chart you already have">
                ⊞ your charts
              </button>
            )}
          </span>
        )}
      </div>

      <div className="messages">
        {notice && (
          <div className="msg">
            <div className="chat-notice" role="status">{notice}</div>
          </div>
        )}
        {messages.length === 0 && !busy && (
          <div className="empty">
            <div className="empty-title">
              {visualMode ? <>🎨 What do you want to see?</>
                : <><span className="brand-mark">◆</span> What do you want to know?</>}
            </div>
            <div className="empty-sub">
              {visualMode ? (
                <>
                  Describe a visualization of any data source above. Every answer is drawn
                  with D3 — a D3 chart, or a custom interactive visual the AI writes for it.
                  {orchestrated && " Pick one source (not all) for custom visuals."}
                </>
              ) : orchestrated
                ? "Ask across every database you can see — agents route the question."
                : `Ask about ${sel.length ? sel.join(", ") : "your data"} in plain English. The agent writes the SQL, runs it, and charts the answer.`}
            </div>
            <div className="suggestions">
              {sugBusy && <div className="meta">reading the schema…</div>}
              {suggestions.map((s) => (
                <button
                  key={s.question + s.table}
                  className="suggestion"
                  onClick={() => setPrompt(s.question)}
                  title={`answerable from ${s.table}`}
                >
                  {s.question}
                  {sel.length !== 1 && <span className="sug-table">▦ {s.table}</span>}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="msg msg-user">
              <div className="bubble-user">{m.text}</div>
            </div>
          ) : (
            <AssistantMessage
              key={i}
              m={m}
              requirement={messages[i - 1]?.role === "user" ? messages[i - 1].text : ""}
              onOpenCanvas={(view) => setCanvas(buildCanvas(view || m))}
              onClarify={resend}
              onRunPipeline={() => runPipeline(m)}
              onPipelineStatus={() => runPipeline(m, "status")}
              onPlatformAction={(action) => platformAction({ platform_action: action, platform_message_id: m.message_id },
                action === "status" ? "Check this platform job's status" : "Submit this platform job again for approval")}
              busy={busy}
              activitySteps={activityByTask[m.activity_task_id]}
              visualMode={visualMode}
            />
          )
        )}

        {busy && (
          <div className="msg">
            <AgentActivity running steps={liveSteps} startedAt={activeStartedAt} />
          </div>
        )}
        {!busy && (failedActivity || error) && (
          <div className="msg">
            {failedActivity && <AgentActivity key={failedActivity.taskId} failed taskId={failedActivity.taskId} steps={failedActivity.steps} />}
            {error && <div className="error">{error}</div>}
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form className="composer chat-composer" onSubmit={send}>
        <div className="composer-pill">
          <input
            className="pill-input"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={
              visualMode
                ? "Describe the visualization — e.g. “revenue by region as circle packing” or “a custom radial timeline of orders”…"
                : orchestrated
                ? "Ask across all your databases — agents route the question…"
                : tables.length ? `Ask about ${sel.length ? sel.join(", ") : "your data"}…` : "Choose a data source, or ask to run a platform job…"
            }
            disabled={busy}
          />
          <select
            className="model-mini"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            title="Which AI model answers"
          >
            {models.map((m) => (
              <option key={m.spec} value={m.spec} disabled={!m.available}>
                {m.label || m.name}{!m.available ? (m.note ? ` — ${m.note}` : " — no key") : ""}
              </option>
            ))}
          </select>
          <button className="primary send-btn" aria-label={buildAirflow ? "Build Airflow DAG" : buildPipeline ? "Build pipeline" : "Send message"} disabled={busy || !prompt.trim()}>
            ➤
          </button>
        </div>
        {!visualMode && (<>
        <label className="composer-pipeline-option">
          <input type="checkbox" checked={buildPipeline} disabled={busy} onChange={(e) => {
            setBuildPipeline(e.target.checked);
            if (e.target.checked) setBuildAirflow(false);
          }} />
          Build a read-only pipeline from this prompt
        </label>
        <label className="composer-pipeline-option">
          <input type="checkbox" checked={buildAirflow} disabled={busy} onChange={(e) => {
            setBuildAirflow(e.target.checked);
            if (e.target.checked) setBuildPipeline(false);
          }} />
          Build an approval-gated Airflow DAG from this prompt
        </label>
        {(buildPipeline || buildAirflow) && (
          <PlanningContextPicker {...planningCatalog}
            repositoryId={repositoryId} confluencePageIds={confluencePageIds}
            onRepositoryChange={setRepositoryId} onConfluenceChange={setConfluencePageIds}
            disabled={busy} />
        )}
        <button type="button" className="chip" disabled={busy} onClick={() => setShowPlatform((v) => !v)}>Run on Airflow / another platform</button>
        {showPlatform && <PlatformComposer busy={busy} onSubmit={platformAction} onClose={() => setShowPlatform(false)} />}
        </>)}
      </form>
      </main>
    </div>
  );
}

export function AssistantMessage({ m, requirement, onOpenCanvas, onClarify, onRunPipeline, onPipelineStatus, onPlatformAction, busy, activitySteps, visualMode = false }) {
  const [showSql, setShowSql] = useState(false);
  const [columns, setColumns] = useState(m.columns);
  const [rows, setRows] = useState(m.rows);
  // Stored result rows expire under the retention policy (rows_purged): the
  // answer, SQL and chart stay, the rows do not. Re-running brings them back
  // for this view; until then the answer says so instead of drawing nothing.
  const [refreshed, setRefreshed] = useState(false);
  const expired = Boolean(m.rows_purged) && !refreshed;
  // The live re-run of an expired answer, for the canvas and the D3 window. Not
  // tied to the stored message (whose rows are gone), so canvas edits stay local.
  const freshView = () => ({ ...m, message_id: undefined, panels: undefined, columns, rows });
  const [status, setStatus] = useState("");
  const [voted, setVoted] = useState(0);
  const [sample, setSample] = useState(null);
  const [sampleBusy, setSampleBusy] = useState("");

  async function loadSample(t) {
    // Every click re-queries the source — the sample is always latest data.
    setSampleBusy(t);
    try {
      const d = await api(`/catalog/sources/${m.source}/tables/${t}/sample`);
      setSample({ table: t, panels: d.panels, at: new Date().toLocaleTimeString() });
    } catch (e) {
      setStatus(`sample failed: ${e.message}`);
    } finally {
      setSampleBusy("");
    }
  }

  // Clicking your vote again takes it back (score 0); clicking the other one
  // switches it. The server keeps one vote per answer either way.
  async function vote(score) {
    if (!m.trace_id) return;
    const previous = voted;
    const next = voted === score ? 0 : score;
    setVoted(next);
    try {
      await api("/feedback", {
        method: "POST",
        body: JSON.stringify({ trace_id: m.trace_id, score: next }),
      });
      setStatus(next === 0 ? "feedback removed"
        : next > 0 ? "thanks — the agent learns from this" : "flagged for agent learning");
    } catch (e) {
      setVoted(previous);
      setStatus(`feedback failed: ${e.message}`);
    }
  }

  async function refresh() {
    if (!m.sql) return;
    setStatus("refreshing…");
    try {
      const data = await api("/chat/rerun", {
        method: "POST",
        body: JSON.stringify({ source: m.source, sql: m.sql }),
      });
      setColumns(data.columns);
      setRows(data.rows);
      setRefreshed(true);
      setStatus(`refreshed ${new Date().toLocaleTimeString()}`);
      return data;
    } catch (e) {
      setStatus(`refresh failed: ${e.message}`);
      return null;
    }
  }

  async function rerunExpired() {
    const data = await refresh();
    if (data && !visualMode) {
      onOpenCanvas?.({ ...m, message_id: undefined, panels: undefined,
                       columns: data.columns, rows: data.rows });
    }
  }

  async function emailReport() {
    setStatus("emailing…");
    try {
      const d = await api("/reports/email", {
        method: "POST",
        body: JSON.stringify({
          subject: m.chart?.title || `Query on ${m.table}`,
          text: m.text || "",
          sql: m.sql,
          columns,
          rows: rows.slice(0, 200),
        }),
      });
      setStatus(d.mode === "smtp" ? `emailed to ${d.to}` : `saved to outbox (no SMTP configured)`);
    } catch (e) {
      setStatus(`email failed: ${e.message}`);
    }
  }

  return (
    <div className="msg">
      <div className="assistant">
        <div className="assistant-head">
          <span className="brand-mark">◆</span>
          <span className="meta">
            {m.served_by === "semantic"
              ? "▣ Semantic layer"
              : m.served_by === "bitnet"
                ? "🧠 BitNet (learned)"
                : m.mode === "cached"
                  ? "⚡ cached"
                  : m.mode === "fallback"
                    ? "fallback"
                    : m.model}
            {" · "}{m.source}/{m.table === "*" ? "all tables" : m.table}
            {m.agents?.length > 0
              ? ` · ${agentLabel(m.agents)}`
              : m.mode === "orchestrated" && m.agents_used?.length > 0
                ? ` · agents: ${m.agents_used.join(", ")}`
                : ""}
            {(() => {
              const crew = agentSummary(m.agents?.length ? m.agents
                : (m.agents_used || []).map((name) => ({ name, role: "worker" })));
              return crew.total > 0 && (
                <span className="agent-count" title={crew.names.join(", ")}>
                  {" "}· 👥 {crew.text}{crew.detail ? ` (${crew.detail})` : ""}
                </span>
              );
            })()}
            {m.cached && (
              <span title={`reused your earlier plan for: "${m.cached.from_prompt || ""}"`}>
                {" · "}reused plan{m.cached.similarity < 1 ? ` (${Math.round(m.cached.similarity * 100)}% match)` : ""} · fresh data
              </span>
            )}
            {m.verified_sql && (
              <span title={m.verified_sql.match === "exact"
                ? `re-ran ${m.verified_sql.mine ? "your" : "the organization's"} verified query “${m.verified_sql.title}” as you — verified to run when saved, not certified`
                : `the agent adapted the related verified query “${m.verified_sql.title}”`}>
                {" · "}{m.verified_sql.match === "exact"
                  ? `verified query “${m.verified_sql.title}” · fresh data`
                  : `guided by verified query “${m.verified_sql.title}”`}
              </span>
            )}
          </span>
        </div>
        {m.activity_task_id && <AgentActivity key={m.activity_task_id} taskId={m.activity_task_id} steps={activitySteps || []} graph={m.graph} agents={m.agents} />}
        {m.text && <Markdown className="answer" text={m.text} />}
        {expired && (
          <div className="rows-expired" role="note">
            <span><b>Result rows expired.</b> Studio keeps an answer's data for a limited time;
              the question, SQL and chart settings are kept.</span>
            {m.sql && (
              <button className="chip" onClick={rerunExpired}
                      title="Run this answer's SQL again for current data">
                ⟳ re-run for current data
              </button>
            )}
          </div>
        )}
        {/* The D3 & artifacts window draws every chart right in the thread,
            with D3 alone, under the answer it illustrates. */}
        {visualMode && !expired && (m.panels?.length > 0 || (m.chart && rows?.length > 0)) && (
          <div className="msg-visuals">
            {(m.panels?.length && !m.rows_purged ? m.panels : [{ columns, rows, chart: m.chart }]).map((p, i) => (
              <div key={i} className="msg-visual">
                <ChartView columns={p.columns} rows={p.rows} chart={p.chart} visual={p.visual}
                           engine="d3" d3Only tall />
              </div>
            ))}
          </div>
        )}
        <PlanningSources sources={m.pipeline?.planning_sources} />
        {m.pipeline?.execution_mode === "airflow_dag"
          ? <ChatWorkflow key={m.pipeline.job_id || m.message_id} pipeline={m.pipeline} messageId={m.message_id} busy={busy} onRun={onRunPipeline} onStatus={onPipelineStatus} />
          : m.pipeline && <ChatPipeline pipeline={m.pipeline} messageId={m.message_id} busy={busy} onRun={onRunPipeline} />}
        {m.platform_run && <ChatPlatformRun key={m.platform_run.job_id || m.message_id} artifact={m.platform_run} messageId={m.message_id} busy={busy} onAction={onPlatformAction} />}
        {m.clarify && onClarify && (
          <div className="clarify-chips">
            {m.clarify.options.map((o) => (
              <button
                key={o.source}
                className="chip"
                onClick={() => onClarify(m.clarify.prompt, o)}
                title={o.source === "*"
                  ? "Query every source that has this table and compare the answers — remembered for this chat"
                  : `Answer from ${o.source} only (has ${(o.tables || []).join(", ")}) — follow-ups stay there`}
              >
                {o.source === "*"
                  ? (m.clarify.options.length > 3
                      ? `⧉ All ${m.clarify.options.length - 1}, side by side`
                      : "⧉ Both, side by side")
                  : `▣ Use ${o.source}`}
              </button>
            ))}
          </div>
        )}
        {/* What the answer was computed from — read off the executed SQL, so it
            reflects the tables actually queried, not the ones picked up top. */}
        {m.inputs?.tables?.length > 0 && (
          <div className="inputs-row">
            <span className="meta">answered from</span>
            {m.inputs.tables.map((t) => (
              <span key={t} className="chip chip-input" title="table referenced by the executed SQL">
                ▦ {t}
              </span>
            ))}
            {m.inputs.columns?.length > 0 && (
              <span className="meta" title={m.inputs.columns.join(", ")}>
                · {m.inputs.columns.slice(0, 4).join(", ")}
                {m.inputs.columns.length > 4 ? ` +${m.inputs.columns.length - 4}` : ""}
              </span>
            )}
            {m.inputs.row_count > 0 && <span className="meta">· {m.inputs.row_count} rows</span>}
          </div>
        )}
        {/* KAG: documents the agent grounded this answer on (knowledge_search). */}
        {m.citations?.length > 0 && (
          <div className="inputs-row">
            <span className="meta">grounded on</span>
            {m.citations.map((c, i) => (
              <span key={i} className="chip chip-input"
                title={`from your documents${c.collection ? ` · ${c.collection}` : ""}${c.score != null ? ` · relevance ${Math.round(c.score * 100)}%` : ""}`}>
                📄 {c.source}{c.page ? ` p.${c.page}` : c.sheet ? ` · ${c.sheet}` : ""}
              </span>
            ))}
          </div>
        )}
        {m.matched_tables?.length > 0 && (
          <div className="match-row">
            <span className="meta">matched tables:</span>
            {m.matched_tables.map((mt) => (
              <button
                key={mt.table}
                className={"chip" + (sample?.table === mt.table ? " chip-on" : "")}
                onClick={() => loadSample(mt.table)}
                disabled={sampleBusy === mt.table}
                title={`matched on: ${mt.why.join(", ")}\ncolumns: ${mt.columns.join(", ")}\nclick for the latest sample — click again to refresh`}
              >
                ▦ {mt.table}{sampleBusy === mt.table ? " …" : ""}
              </button>
            ))}
            {sample && (
              <button className="chip" onClick={() => setSample(null)} title="Close sample">
                ✕
              </button>
            )}
          </div>
        )}
        {sample && (
          <div className="sample-view">
            <div className="meta">▦ {sample.table} — latest data, fetched {sample.at}</div>
            <div className="msg-panels">
              {sample.panels.map((p, i) => (
                <ChartView key={i} columns={p.columns} rows={p.rows} chart={p.chart}
                           {...(visualMode ? { engine: "d3", d3Only: true } : {})} />
              ))}
            </div>
          </div>
        )}
        {m.sql && (
          <div className="sqlbox">
            <button className="chip" onClick={() => setShowSql(!showSql)}>
              {showSql ? "hide SQL" : "◦ edit / verify / save SQL"}
            </button>{" "}
            <button className="chip" onClick={refresh} title="Re-run this query for the newest records">
              ⟳ refresh data
            </button>{" "}
            <button className="chip" onClick={emailReport} title="Email this report to yourself">
              ✉ email report
            </button>{" "}
            {m.chart && !visualMode && !expired && (
              <button className="chip" onClick={() => onOpenCanvas(m.rows_purged ? freshView() : undefined)}
                      title="Open on the main screen">
                ⤢ canvas
              </button>
            )}
            {status && <span className="meta"> {status}</span>}
            {showSql && (
              <SqlLab sql={m.sql} source={m.source} table={m.table} requirement={requirement} />
            )}
          </div>
        )}
        {m.trace_id && (
          <div className="feedback-row">
            <button
              className={"chip" + (voted === 1 ? " chip-on" : "")}
              onClick={() => vote(1)}
              aria-pressed={voted === 1}
              title={voted === 1 ? "Take this back" : "Good answer — reinforces this behavior"}
            >
              👍
            </button>{" "}
            <button
              className={"chip" + (voted === -1 ? " chip-on" : "")}
              onClick={() => vote(-1)}
              aria-pressed={voted === -1}
              title={voted === -1 ? "Take this back" : "Wrong or unhelpful — the agent learns from flagged runs"}
            >
              👎
            </button>
            {!m.sql && status && <span className="meta"> {status}</span>}
          </div>
        )}
        {/* Charts always live in the canvas (center stage), never inline in the
            chat column — chat keeps a button to bring this message's view back. */}
        {!visualMode && !expired && (m.panels?.length > 0 || (m.chart && rows?.length > 0)) && (
          <button className="chip chart-open"
                  onClick={() => onOpenCanvas(m.rows_purged ? freshView() : undefined)}>
            ⤢ {m.panels?.length > 1 ? `${m.panels.length} charts` : "chart"} in canvas
          </button>
        )}
      </div>
    </div>
  );
}
