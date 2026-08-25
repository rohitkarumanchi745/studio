import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import Canvas from "./Canvas";
import ChartView from "./ChartView";
import SqlLab from "./SqlLab";

// "Demo agent" for a single worker; "Snowflake agent + Databricks agent →
// Aggregator" when a question fanned out across sources.
function agentLabel(agents) {
  if (!agents?.length) return "";
  const workers = agents.filter((a) => a.role !== "aggregator");
  const names = workers.map((a) => a.name).join(" + ");
  return agents.some((a) => a.role === "aggregator") ? `${names} → Aggregator` : names;
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
    note: "",
    // `p.rows ?? []`: a panel without rows (a redacted message, a cached plan
    // that returned none) used to throw here — and the caller swallowed it and
    // re-polled forever, so the chat hung on "agent is querying…" with the
    // answer already saved. A missing-rows panel must degrade, never throw.
    original: panels.map((p) => ({ ...p, rows: (p.rows ?? []).map((r) => [...r]) })),
  };
}

export default function Chat({ conversationId, onConversationCreated, onOpenDashboard, modelsEpoch }) {
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Live activity: the steps the agent has emitted so far this turn, refreshed
  // by the task poll — so the "thinking" bubble shows what is actually happening.
  const [liveSteps, setLiveSteps] = useState([]);
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

  useEffect(() => {
    api("/catalog/sources").then(setSources).catch(() => {});
  }, []);

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
    setError("");
    setBusy(false);
    if (!conversationId) {
      setMessages([]);
      setCanvas(null);
      return;
    }
    api(`/conversations/${conversationId}/messages`)
      .then((ms) => {
        loadMessages(ms);
        // If a background task is still running here (e.g. after a refresh or a
        // chat switch), resume the live "working…" state. pollFor dedups, so
        // this never double-polls a task send() already started.
        api(`/conversations/${conversationId}/task`)
          .then((t) => {
            if (t.status === "running") {
              setBusy(true);
              pollFor(t.id, conversationId);
            }
          })
          .catch(() => {});
      })
      .catch(() => {
        setMessages([]);
        setCanvas(null);
      });
  }, [conversationId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  async function send(e) {
    e?.preventDefault();
    const q = prompt.trim();
    if (!q || busy || (!orchestrated && tables.length === 0)) return;
    setPrompt("");
    setError("");
    // Optimistically show the question + a working placeholder; the answer is
    // produced in the background, so the user can switch to another chat and
    // start a different task without waiting.
    setMessages((m) => [...m, { role: "user", text: q, source, table: tableLabel }]);
    setBusy(true);
    setLiveSteps([]);
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
        }),
      });
      if (!conversationId) onConversationCreated(data.conversation_id);
      pollFor(data.task_id, data.conversation_id);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  function loadMessages(ms) {
    const loaded = ms.map((m) => ({ role: m.role, ...m.content }));
    setMessages(loaded);
    // Reopening a conversation restores its charts to the canvas — the
    // visualization is part of the saved conversation, not a transient view.
    const last = [...loaded].reverse().find(
      (m) => m.role === "assistant" && (m.panels?.length || (m.chart && m.rows?.length)));
    setCanvas(last ? buildCanvas(last) : null);
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
        if (activeConvRef.current === cid) setLiveSteps(st.steps || []);
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
      // Clear the spinner even if the user switched chats — leaving busy set
      // is what made the composer look permanently stuck.
      setBusy(false);
      setLiveSteps([]);
      if (activeConvRef.current === cid) {
        try {
          handleModelError(loadMessages(await api(`/conversations/${cid}/messages`)));
        } catch { /* keep the optimistic messages rather than blank the view */ }
        if (st.status === "failed") setError(st.error || "The task failed.");
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
      <main className="chatpane">
      <div className="toolbar">
        <label>Source</label>
        <select value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="*">✳ all sources — multi-agent</option>
          {sources.map((s) => (
            <option key={s.name} value={s.name} disabled={!s.allowed || !s.configured}>
              {s.name}
              {!s.configured ? " (not configured)" : !s.allowed ? " (no access)" : ""}
            </option>
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
      </div>

      <div className="messages">
        {messages.length === 0 && !busy && (
          <div className="empty">
            <div className="empty-title">
              <span className="brand-mark">◆</span> What do you want to know?
            </div>
            <div className="empty-sub">
              {orchestrated
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
              onOpenCanvas={() => setCanvas(buildCanvas(m))}
            />
          )
        )}

        {busy && (
          <div className="msg">
            {liveSteps.length > 1 && (
              <div className="agent-activity">
                {liveSteps.slice(-6, -1).map((s, i) => (
                  <div key={i} className="activity-step">✓ {s.label}</div>
                ))}
              </div>
            )}
            <div className="thinking">
              <span className="dot" />
              <span className="dot" />
              <span className="dot" />
              {liveSteps.length
                ? liveSteps[liveSteps.length - 1].label
                : "agent is querying…"}
            </div>
          </div>
        )}
        {error && <div className="error">{error}</div>}
        <div ref={endRef} />
      </div>

      <form className="composer" onSubmit={send}>
        <div className="composer-pill">
          <input
            className="pill-input"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={
              orchestrated
                ? "Ask across all your databases — agents route the question…"
                : tables.length ? `Ask about ${sel.length ? sel.join(", ") : "your data"}…` : "Pick a source first…"
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
          <button className="primary send-btn" disabled={busy || !prompt.trim() || (!orchestrated && tables.length === 0)}>
            ➤
          </button>
        </div>
      </form>
      </main>
    </div>
  );
}

function AssistantMessage({ m, requirement, onOpenCanvas }) {
  const [showSql, setShowSql] = useState(false);
  const [columns, setColumns] = useState(m.columns);
  const [rows, setRows] = useState(m.rows);
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

  async function vote(score) {
    if (!m.trace_id || voted) return;
    setVoted(score);
    try {
      await api("/feedback", {
        method: "POST",
        body: JSON.stringify({ trace_id: m.trace_id, score }),
      });
      setStatus(score > 0 ? "thanks — the agent learns from this" : "flagged for agent learning");
    } catch (e) {
      setVoted(0);
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
      setStatus(`refreshed ${new Date().toLocaleTimeString()}`);
    } catch (e) {
      setStatus(`refresh failed: ${e.message}`);
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
            {m.cached && (
              <span title={`reused the plan for: "${m.cached.from_prompt}"`}>
                {" · "}reused plan{m.cached.similarity < 1 ? ` (${Math.round(m.cached.similarity * 100)}% match)` : ""} · fresh data
              </span>
            )}
          </span>
        </div>
        {m.text && <p className="answer">{m.text}</p>}
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
                <ChartView key={i} columns={p.columns} rows={p.rows} chart={p.chart} />
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
            {m.chart && (
              <button className="chip" onClick={onOpenCanvas} title="Open on the main screen">
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
              disabled={!!voted}
              title="Good answer — reinforces this behavior"
            >
              👍
            </button>{" "}
            <button
              className={"chip" + (voted === -1 ? " chip-on" : "")}
              onClick={() => vote(-1)}
              disabled={!!voted}
              title="Wrong or unhelpful — the agent learns from flagged runs"
            >
              👎
            </button>
            {!m.sql && status && <span className="meta"> {status}</span>}
          </div>
        )}
        {/* Charts always live in the canvas (center stage), never inline in the
            chat column — chat keeps a button to bring this message's view back. */}
        {(m.panels?.length > 0 || (m.chart && rows?.length > 0)) && (
          <button className="chip chart-open" onClick={onOpenCanvas}>
            ⤢ {m.panels?.length > 1 ? `${m.panels.length} charts` : "chart"} in canvas
          </button>
        )}
      </div>
    </div>
  );
}
