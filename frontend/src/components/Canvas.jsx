// Canvas mode: charts take the main screen (chat docks aside). Supports
// multiple panels (e.g. day/month/year level side by side) — click a panel to
// select it; cell edits and prompt edits apply to the selected panel. All data
// here is a LOCAL copy — source tables are never modified.
import { useMemo, useState } from "react";
import { api } from "../api";
import ChartView, { LABELS, computeFit } from "./ChartView";

export default function Canvas({ state, setState, onClose, chatOpen, onToggleChat, conversationId, tableLabel, onNewVersion, onOpenDashboard }) {
  const { panels, selected, note, original } = state;
  const [instruction, setInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [showData, setShowData] = useState(panels.length === 1);

  const cur = panels[selected] || panels[0];
  const [gallery, setGallery] = useState(false);

  // Synthesized spec when a result has no chart yet (table-only answers).
  const spec = useMemo(() => {
    if (!cur) return null;
    if (cur.chart) return cur.chart;
    const first = cur.rows[0] || [];
    const num = cur.columns.filter((c, i) => typeof first[i] === "number");
    return { type: "table", title: "Result", x: cur.columns[0], y: num.slice(0, 2) };
  }, [cur]);

  // Every chart type that fits this data (gallery contents).
  const fitTypes = useMemo(() => {
    if (!cur || !spec) return [];
    const fit = computeFit(cur.columns, cur.rows, spec);
    return Object.keys(LABELS).filter((t) => t !== "table" && fit[t]?.ok);
  }, [cur, spec]);

  function patchPanel(idx, patch) {
    const next = panels.map((p, i) => (i === idx ? { ...p, ...patch } : p));
    setState({ ...state, panels: next });
  }

  async function applyEdit(e) {
    e?.preventDefault();
    const q = instruction.trim();
    if (!q || busy || !cur) return;
    setBusy(true);
    try {
      const d = await api("/canvas/edit", {
        method: "POST",
        body: JSON.stringify({
          instruction: q,
          columns: cur.columns,
          rows: cur.rows,
          chart: cur.chart,
          conversation_id: conversationId || undefined,
          source: state.source,
          table: tableLabel,
          sql: cur.sql,
        }),
      });
      const next = panels.map((p, i) =>
        i === selected ? { ...p, columns: d.columns, rows: d.rows, chart: d.chart } : p
      );
      setState({ ...state, panels: next, note: d.note });
      // Each edit is a NEW version: it appears as its own chat message, so
      // the previous chart stays intact in the history.
      if (d.message && onNewVersion) onNewVersion(d.message);
      setInstruction("");
    } catch (err) {
      setState({ ...state, note: `Edit failed: ${err.message}` });
    } finally {
      setBusy(false);
    }
  }

  function editCell(ri, ci, value) {
    const parsed = value !== "" && !isNaN(Number(value)) ? Number(value) : value;
    const rows = cur.rows.map((r, i) => (i === ri ? r.map((v, j) => (j === ci ? parsed : v)) : r));
    patchPanel(selected, { rows });
    setState((s) => ({ ...s, note: "Cell edited (local only)." }));
  }

  async function pin() {
    if (!cur || busy) return;
    setBusy(true);
    try {
      // No dashboard_id: the server pins into a new dashboard and returns it.
      const d = await api("/dashboards/pin", {
        method: "POST",
        body: JSON.stringify({
          source: state.source,
          table_label: tableLabel,
          sql: cur.sql,
          spec: spec,
          title: spec?.title || "Chart",
          columns: cur.columns,
          rows: cur.rows.slice(0, 500),
        }),
      });
      setState({
        ...state,
        note: `Pinned to "${d.dashboard_title}"${d.created_dashboard ? " (new dashboard)" : ""}.`,
        pinnedDashboardId: d.dashboard_id,
      });
      if (onOpenDashboard) onOpenDashboard(d.dashboard_id);
    } catch (err) {
      setState({ ...state, note: `Pin failed: ${err.message}` });
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setState({
      ...state,
      panels: original.map((p) => ({ ...p, rows: p.rows.map((r) => [...r]) })),
      note: "Reset to original results.",
    });
  }

  return (
    <section className="canvas">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">
            {panels.length > 1 ? `${panels.length} views` : cur?.chart?.title || "Canvas"}
          </div>
          <div className="meta">
            {state.source} · local copy — <b>source tables are never modified</b>
            {panels.length > 1 && cur?.chart?.title && <> · editing: {cur.chart.title}</>}
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={() => setShowData(!showData)}>
            {showData ? "hide data" : "show data"}
          </button>
          <button
            className={"chip" + (gallery ? " chip-active" : "")}
            onClick={() => setGallery(!gallery)}
            title="Render every chart type that fits this data"
          >
            ⊞ all views ({fitTypes.length})
          </button>
          <button className="chip" onClick={pin} disabled={busy} title="Pin this chart to a dashboard">
            📌 pin
          </button>
          <button className="chip" onClick={reset}>reset edits</button>
          {onToggleChat && (
            <button className="chip" onClick={onToggleChat}>
              {chatOpen ? "⇥ hide chat" : "⇤ show chat"}
            </button>
          )}
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
      </div>

      <div className="canvas-body">
        {gallery && cur ? (
          <div className="canvas-grid">
            {fitTypes.map((t) => (
              <div
                key={t}
                className="panel gallery-cell"
                title="Click to use this view"
                onClick={() => {
                  patchPanel(selected, { chart: { ...spec, type: t } });
                  setGallery(false);
                }}
              >
                <div className="gallery-label">{LABELS[t]}</div>
                <ChartView columns={cur.columns} rows={cur.rows} chart={{ ...spec, type: t }} plain />
              </div>
            ))}
            {fitTypes.length === 0 && (
              <div className="meta">No chart type fits this data — see the table below.</div>
            )}
          </div>
        ) : (
        <div className={panels.length > 1 ? "canvas-grid" : ""}>
          {panels.map((p, i) => (
            <div
              key={i}
              className={
                panels.length > 1
                  ? "panel" + (i === selected ? " panel-selected" : "")
                  : ""
              }
              onClick={() => panels.length > 1 && setState({ ...state, selected: i })}
            >
              <ChartView
                columns={p.columns}
                rows={p.rows}
                chart={p.chart}
                tall={panels.length === 1}
              />
            </div>
          ))}
        </div>
        )}

        {showData && cur && !gallery && (
          <div className="canvas-data">
            <div className="meta" style={{ margin: "6px 0" }}>
              {panels.length > 1 && <b>[{cur.chart?.title || `panel ${selected + 1}`}] </b>}
              Click any cell to edit — the chart updates live. {cur.rows.length} rows
              {cur.rows.length > 500 ? " (first 500 editable)" : ""}.
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>{cur.columns.map((c) => <th key={c}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {cur.rows.slice(0, 500).map((r, ri) => (
                    <tr key={ri}>
                      {r.map((v, ci) => (
                        <td key={ci}>
                          <input
                            className="cell-input"
                            value={v ?? ""}
                            onChange={(e) => editCell(ri, ci, e.target.value)}
                          />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {note && <div className="canvas-note">{note}</div>}

      <form className="composer canvas-composer" onSubmit={applyEdit}>
        <input
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder={
            (panels.length > 1 ? `Edit "${cur?.chart?.title || "selected panel"}" — ` : "Edit with a prompt — ") +
            '"make it a pie", "top 5 only", "sort by revenue desc"…'
          }
          disabled={busy}
        />
        <button className="primary" disabled={busy || !instruction.trim()}>
          {busy ? "…" : "Apply"}
        </button>
      </form>
    </section>
  );
}
