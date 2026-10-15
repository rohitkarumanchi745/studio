// Visuals — every chart in your conversations, and for each one every way
// Studio can draw it with D3: the D3 chart types that fit its data, and custom
// visuals the AI writes as code (run sandboxed — renderers/ArtifactView.jsx).
// "Use this" saves the choice as a new version of that answer (backend
// chat.visual_apply), so the chat, the canvas and anyone it is shared with all
// see the same chart; the earlier version stays in the history.
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { LABELS, computeFit } from "./ChartView";
import D3View, { D3_ONLY, D3_TYPES } from "./renderers/D3View";
import ArtifactView, { downloadArtifact } from "./renderers/ArtifactView";

const keyOf = (c) => `${c.message_id}:${c.panel_index}`;

/** The chart's own spec, with x/y filled in when it never had them (a custom
 *  visual's plain table), so every D3 type has columns to draw. */
export function baseSpec(columns, rows, chart) {
  const first = rows[0] || [];
  const numeric = columns.filter((_, i) => typeof first[i] === "number");
  const x = chart?.x && columns.includes(chart.x) ? chart.x : columns[0];
  const y = chart?.y?.length ? chart.y.filter((c) => columns.includes(c))
    : numeric.filter((c) => c !== x).slice(0, 2);
  return { ...(chart || {}), title: chart?.title || "", x, y };
}

/** Every D3 chart type this data fits, in catalog order. */
export function d3Options(columns, rows, chart) {
  const fit = computeFit(columns, rows, baseSpec(columns, rows, chart));
  return Object.keys(LABELS).filter((t) => D3_TYPES.has(t) && fit[t]?.ok);
}

/** The chart list grouped by conversation (newest first), filtered by `q`. */
export function groupCharts(charts, q = "") {
  const needle = q.trim().toLowerCase();
  const groups = [];
  const byId = new Map();
  for (const c of charts || []) {
    const hay = `${c.title} ${c.conversation_title} ${c.source} ${c.type}`.toLowerCase();
    if (needle && !hay.includes(needle)) continue;
    if (!byId.has(c.conversation_id)) {
      byId.set(c.conversation_id, { id: c.conversation_id, title: c.conversation_title, items: [] });
      groups.push(byId.get(c.conversation_id));
    }
    byId.get(c.conversation_id).items.push(c);
  }
  return groups;
}

/** How a loaded chart is drawn now, in words. */
export function nowDrawn(data) {
  if (data.visual) return "a custom visual";
  const type = data.chart?.type || "table";
  const engine = D3_ONLY.has(type) ? "d3" : data.engine;
  return `${LABELS[type] || type}${engine ? ` · ${engine}` : " · your default engine"}`;
}

export default function Visuals({ onClose, onOpenChat }) {
  const [charts, setCharts] = useState(null);   // null = loading
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(null);         // a chart summary from the list
  const [data, setData] = useState(null);       // the selected chart, loaded
  const [drafts, setDrafts] = useState([]);     // visuals written for it this visit
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  function loadList() {
    return api("/visuals/charts")
      .then((d) => setCharts(Array.isArray(d) ? d : []))
      .catch((e) => { setError(e.message); setCharts([]); });
  }
  useEffect(() => { loadList(); }, []);

  const selKey = sel ? keyOf(sel) : "";
  useEffect(() => {
    if (!sel) return;
    setData(null);
    setDrafts([]);
    setRequest("");
    const qs = new URLSearchParams({ conversation_id: sel.conversation_id,
      message_id: sel.message_id, panel_index: String(sel.panel_index) });
    api(`/visuals/chart?${qs}`).then(setData).catch((e) => setError(e.message));
  }, [selKey]);

  const groups = useMemo(() => groupCharts(charts, q), [charts, q]);
  const spec = useMemo(() => (data ? baseSpec(data.columns, data.rows, data.chart) : null), [data]);
  const options = useMemo(() => (data ? d3Options(data.columns, data.rows, data.chart) : []), [data]);

  async function generate(e) {
    e?.preventDefault?.();
    const text = request.trim();
    if (!text || busy || !sel) return;
    setBusy("generate");
    setError("");
    try {
      const out = await api("/visuals/generate", {
        method: "POST",
        body: JSON.stringify({ conversation_id: sel.conversation_id, message_id: sel.message_id,
                               panel_index: sel.panel_index, request: text }),
      });
      setDrafts((d) => [out, ...d]);
      setRequest("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  async function use(choice, label) {
    if (!sel || busy) return;
    setBusy(`use:${label}`);
    setError("");
    setNote("");
    try {
      const out = await api("/visuals/apply", {
        method: "POST",
        body: JSON.stringify({ conversation_id: sel.conversation_id, message_id: sel.message_id,
                               panel_index: sel.panel_index, ...choice }),
      });
      setNote(`Saved — “${data?.title || sel.title}” now uses ${label}. It's a new version of `
        + "the answer, so the chat shows it too and the earlier version stays in the history.");
      await loadList();
      setSel({ ...sel, message_id: out.message_id });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="dashboard visuals-screen">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">Your charts — D3 &amp; artifacts</div>
          <div className="meta">
            Every chart from your chats, drawn every way D3 can. Pick the one each chart
            should use — as a D3 chart type, or a custom visual the AI writes for it.
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={onClose}>← back to the prompt</button>
        </div>
      </div>

      {error && <div className="error">{error}</div>}
      {note && <div className="meta share-notice">{note}</div>}

      <div className="visuals-layout">
        <aside className="visuals-list">
          <input className="conn-search" placeholder="Search charts…" value={q}
                 onChange={(e) => setQ(e.target.value)} />
          {charts === null ? (
            <div className="meta">Loading your charts…</div>
          ) : !groups.length ? (
            <div className="meta">
              {q ? "No chart matches." : "No charts yet — ask a question in chat and its charts appear here."}
            </div>
          ) : groups.map((g) => (
            <div key={g.id} className="visuals-group">
              <div className="conv-label">{g.title}</div>
              {g.items.map((c) => (
                <button key={keyOf(c)} onClick={() => { setSel(c); setNote(""); setError(""); }}
                        className={"visuals-item" + (selKey === keyOf(c) ? " visuals-item-on" : "")}>
                  <b>{c.title}</b>
                  <span className="meta">
                    {c.has_visual ? "✨ custom visual"
                      : `${LABELS[c.type] || c.type}${c.engine ? ` · ${c.engine}` : ""}`}
                    {" · "}{c.row_count} rows{c.can_edit ? "" : " · view only"}
                  </span>
                </button>
              ))}
            </div>
          ))}
        </aside>

        <main className="visuals-main">
          {!sel ? (
            <div className="meta visuals-empty">Pick a chart to see every way to draw it.</div>
          ) : !data ? (
            <div className="meta visuals-empty">Loading the chart…</div>
          ) : (
            <>
              <div className="visuals-head">
                <div>
                  <div className="canvas-title">{data.title}</div>
                  <div className="meta">
                    {data.source} · {data.rows.length} rows · now drawn as <b>{nowDrawn(data)}</b>
                    {!data.can_edit && " · view only — the owner can change it"}
                  </div>
                </div>
                {onOpenChat && (
                  <button className="chip" onClick={() => onOpenChat(sel.conversation_id)}>
                    open chat
                  </button>
                )}
              </div>

              <h3 className="visuals-h">
                D3 charts <span className="meta">— every type that fits this data</span>
              </h3>
              {options.length ? (
                <div className="visuals-grid">
                  {options.map((t) => {
                    const inUse = !data.visual && data.chart?.type === t
                      && (data.engine === "d3" || D3_ONLY.has(t));
                    return (
                      <div key={t} className="panel visuals-card">
                        <div className="visuals-card-head">
                          <b>{LABELS[t]}</b>
                          <button className="chip" disabled={!data.can_edit || !!busy || inUse}
                                  onClick={() => use({ chart_type: t, engine: "d3" }, `${LABELS[t]} (D3)`)}>
                            {inUse ? "✓ in use" : busy === `use:${LABELS[t]} (D3)` ? "saving…" : "Use this"}
                          </button>
                        </div>
                        <D3View columns={data.columns} rows={data.rows} spec={{ ...spec, type: t }} />
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="meta">No D3 chart type fits this data — try a custom visual.</div>
              )}

              <h3 className="visuals-h">
                Custom visuals <span className="meta">— written as code by the AI, run sandboxed</span>
              </h3>
              <form className="composer visuals-composer" onSubmit={generate}>
                <input value={request} onChange={(e) => setRequest(e.target.value)} disabled={!!busy}
                       placeholder='Describe a visual — "a radial bar per region, sorted by revenue, with hover tooltips"' />
                <button className="primary" disabled={!!busy || !request.trim()}>
                  {busy === "generate" ? "writing…" : "✨ Write it"}
                </button>
              </form>
              <div className="visuals-grid">
                {data.visual && (
                  <VisualCard visual={data.visual} data={data} inUse busy={busy} />
                )}
                {drafts.map((v, i) => (
                  <VisualCard key={`${i}:${v.signature}`} visual={v} data={data} busy={busy}
                              onUse={data.can_edit ? () => use({ visual: v }, `“${v.title}”`) : null} />
                ))}
                {!data.visual && !drafts.length && (
                  <div className="meta">No custom visual yet — describe one above and the AI writes it.</div>
                )}
              </div>
            </>
          )}
        </main>
      </div>
    </section>
  );
}

function VisualCard({ visual, data, inUse = false, onUse = null, busy = "" }) {
  const [showCode, setShowCode] = useState(false);
  const [saveError, setSaveError] = useState("");
  return (
    <div className="panel visuals-card visuals-card-wide">
      <div className="visuals-card-head">
        <b>{visual.title}</b>
        <span className="toolbar-selects">
          <button className="chip" onClick={() => setShowCode(!showCode)}>
            {showCode ? "hide code" : "‹/› code"}
          </button>
          <button className="chip" onClick={() => downloadArtifact({ visual, columns: data.columns, rows: data.rows })
            .catch((e) => setSaveError(String(e?.message || e)))}>⬇ HTML</button>
          {inUse ? (
            <button className="chip" disabled>✓ in use</button>
          ) : onUse && (
            <button className="chip" disabled={!!busy} onClick={onUse}>
              {busy === `use:“${visual.title}”` ? "saving…" : "Use this"}
            </button>
          )}
        </span>
      </div>
      {visual.explanation && <div className="meta">{visual.explanation}</div>}
      {saveError && <div className="chart-note">Could not save the file: {saveError}</div>}
      <ArtifactView visual={visual} columns={data.columns} rows={data.rows} />
      {showCode && <pre className="artifact-code">{visual.code}</pre>}
    </div>
  );
}
