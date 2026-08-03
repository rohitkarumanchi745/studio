// Dashboard — pinned tiles on a grid, with Power BI-style cross-filtering.
// The dashboard stores the RECIPE (sql + spec); rows are re-fetched per viewer
// through POST /dashboards/{id}/data, so RBAC is evaluated at view time.
// Filtering is server-side, always: a click becomes a Selection, the Selection
// becomes the wire Filter[], and viz places each predicate at the earliest stage
// where its column exists. There is no JS predicate mirror here.
// Props:
//   dashboardId    string                      — dashboard to render
//   user           {id,email,role}             — viewer (display / ownership hints)
//   api            fn(path, options)           — fetch helper; defaults to ../api
//   onClose        () => void
//   onOpenInCanvas (panel, source) => void     — panel {sql, columns, rows, base, chart}
//   readOnly       bool                        — force view-only regardless of can_edit
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api as sharedApi } from "../api";
import ChartView from "./ChartView";
import FilterBar, { EMPTY_CONTEXT } from "./FilterBar";
import { captureKeyOrder } from "./renderers/format";

const DEFAULT_LAYOUT = { cols: 12, row_h: 80, gap: 12 };
const CROSS_DEBOUNCE = 0;    // chart clicks fire immediately
const SLICER_DEBOUNCE = 120; // trailing, for typing / range drags
const SAVE_DEBOUNCE = 1000;  // slicer state persistence
const PIN_SAMPLE_ROWS = 500; // pin payload cap — rows only ever seed the palette

// Click→Selection mapping per chart type. Types absent from both sets never
// select (kpi, gauge, table, histogram bins, radar) — a wrong filter is worse
// than no filter.
const ROW_TYPES = new Set(["bar", "hbar", "stacked_bar", "line", "area", "stacked_area", "combo"]);
const NAME_TYPES = new Set(["pie", "donut", "funnel", "treemap"]);

function num(v, dflt, lo, hi) {
  const n = Number(v);
  if (!Number.isFinite(n)) return dflt;
  return Math.min(hi, Math.max(lo, Math.round(n)));
}

function hasNum(v) {
  return v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
}

function sameValue(a, b) {
  return String(a) === String(b);
}

function selValues(sel) {
  if (!sel) return [];
  return Array.isArray(sel.values) && sel.values.length ? sel.values : [sel.value];
}

// The raw cell behind a rendered label — engines hand back stringified names.
function rawFor(rows, i, label) {
  if (i < 0) return label;
  const key = String(label);
  for (const r of rows) if (r && String(r[i]) === key) return r[i];
  return label;
}

function distinctAt(rows, i) {
  const out = [];
  const seen = new Set();
  for (const r of rows || []) {
    if (!r) continue;
    const k = String(r[i]);
    if (!seen.has(k)) {
      seen.add(k);
      out.push(r[i]);
    }
  }
  return out;
}

// ── PURE: engine click event -> Selection ────────────────────────────────
// `value` is ALWAYS the raw cell, never a formatted label: formatting is the
// renderer's job, matching is the server's.
export function selectionFromChartEvent(params, spec, columns, rows, tileId) {
  try {
    if (!params || !spec) return null;
    const type = spec.type || "bar";
    const cols = Array.isArray(columns) ? columns : [];
    const data = Array.isArray(rows) ? rows : [];
    const xi = cols.indexOf(spec.x);
    const yCol = Array.isArray(spec.y) ? spec.y[0] : null;
    const ev = params.event?.event;
    const base = {
      field: spec.x, value: null, values: null, range: null, pair: null,
      seriesField: params.seriesName ?? null, label: "", type: "point",
      sourceTileId: tileId ?? null,
      additive: !!(ev?.ctrlKey || ev?.metaKey),
    };
    if (params.type === "clear" || params.selectionType === "clear") {
      return { ...base, field: null, type: "clear" };
    }
    if (xi < 0 && type !== "scatter" && type !== "bubble") return null;

    if (type === "waterfall") {
      // series 0 is the transparent base; the trailing bar is the total.
      if (params.seriesIndex === 0) return null;
      const r = data[params.dataIndex];
      if (!r) return null;
      return { ...base, value: r[xi], label: String(r[xi]) };
    }
    if (type === "sankey") {
      if (params.dataType === "edge") return null;
      const name = params.data?.name ?? params.name;
      if (name == null) return null;
      return { ...base, value: rawFor(data, xi, name), label: String(name) };
    }
    if (type === "heatmap") {
      const v = params.value;
      const yi = cols.indexOf(yCol);
      if (!Array.isArray(v) || yi < 0) return null;
      const a = { ...base, field: spec.x, value: rawFor(data, xi, v[0]), label: String(v[0]) };
      const b = { ...base, field: yCol, value: rawFor(data, yi, v[1]), label: String(v[1]) };
      return { ...a, pair: [a, b], label: `${v[0]} · ${v[1]}` };
    }
    if (type === "scatter" || type === "bubble") {
      const v = Array.isArray(params.value) ? params.value : null;
      if (!v) return null;
      const x0 = v[0];
      if (typeof x0 === "number") {
        return { ...base, type: "range", range: [x0, x0], value: x0, label: String(x0) };
      }
      return { ...base, value: rawFor(data, xi, x0), label: String(x0) };
    }
    if (type === "boxplot") {
      // ECharts groups rows into categories, so dataIndex is a category index.
      const cats = distinctAt(data, xi);
      const v = cats[params.dataIndex];
      if (v === undefined) return null;
      return { ...base, value: v, label: String(v) };
    }
    if (NAME_TYPES.has(type)) {
      const name = params.data?.name ?? params.name;
      if (name == null) return null;
      return { ...base, value: rawFor(data, xi, name), label: String(name) };
    }
    if (ROW_TYPES.has(type)) {
      const di = params.dataIndex;
      const r = Number.isInteger(di) ? data[di] : null;
      if (!r) return null;
      return { ...base, value: r[xi], label: String(r[xi]) };
    }
    return null;
  } catch {
    return null;
  }
}

// ── PURE: the one place click semantics live ─────────────────────────────
export function reduceSelection(prev, sel, spec) {
  const list = Array.isArray(prev) ? prev.filter((s) => s && s.field) : [];
  if (!sel || sel.type === "clear" || !sel.field) return [];
  if (Array.isArray(sel.pair) && sel.pair.length) {
    return sel.pair.filter((p) => p && p.field).map((p) => ({ ...p, pair: null, sourceTileId: sel.sourceTileId }));
  }
  const multi = spec?.interaction?.multi_select !== false;
  const additive = multi && sel.additive;
  const cur = list.find((s) => s.field === sel.field && s.sourceTileId === sel.sourceTileId);

  if (cur && sel.type !== "range") {
    const vals = selValues(cur);
    const has = vals.some((v) => sameValue(v, sel.value));
    if (additive) {
      const next = has ? vals.filter((v) => !sameValue(v, sel.value)) : [...vals, sel.value];
      if (!next.length) return list.filter((s) => s !== cur);
      return list.map((s) =>
        s === cur ? { ...s, value: next[0], values: next.length > 1 ? next : null } : s
      );
    }
    // Same single value clicked again ⇒ toggle off (Power BI behaviour).
    if (has && vals.length === 1) return list.filter((s) => s !== cur);
  }
  return [{ ...sel, values: Array.isArray(sel.values) && sel.values.length ? sel.values : null }];
}

// ── PURE: FilterContext -> the wire Filter[] ─────────────────────────────
export function filterContextToWire(ctx) {
  const c = { slicers: [], cross: [], ...(ctx && typeof ctx === "object" ? ctx : {}) };
  const out = [];
  for (const f of Array.isArray(c.slicers) ? c.slicers : []) {
    if (f && f.col) out.push({ ...f, origin: "slicer" });
  }
  // Multiple selections on ONE field collapse into one `in`; different fields AND.
  const byField = new Map();
  for (const s of Array.isArray(c.cross) ? c.cross : []) {
    if (!s || !s.field) continue;
    if (Array.isArray(s.range) && s.range.length === 2) {
      out.push({
        col: s.field, op: "between", lo: s.range[0], hi: s.range[1],
        origin: "cross", source_tile: s.sourceTileId ?? null,
      });
      continue;
    }
    const key = JSON.stringify([s.field, s.sourceTileId ?? null]);
    const prev = byField.get(key) || { field: s.field, source: s.sourceTileId ?? null, values: [] };
    for (const v of selValues(s)) {
      if (!prev.values.some((p) => sameValue(p, v))) prev.values.push(v);
    }
    byField.set(key, prev);
  }
  for (const e of byField.values()) {
    if (!e.values.length) continue;
    out.push({ col: e.field, op: "in", values: e.values, origin: "cross", source_tile: e.source });
  }
  return out;
}

// ── PURE: identity of the filters a tile is (or would be) rendered under ─
// Compares a tile's `applied_filters` against the current wire. `stage` is the
// server's placement annotation, not part of the filter; a tile never receives
// its own selection. Any mismatch — including one caused by server-side
// normalisation — falls back to refetching, which is always correct.
export function injectedKey(filters, tileId) {
  const out = [];
  for (const f of Array.isArray(filters) ? filters : []) {
    if (!f || typeof f !== "object") continue;
    if ((f.source_tile ?? null) === tileId) continue;
    const { stage, ...rest } = f;
    out.push(rest);
  }
  return JSON.stringify(out);
}

// ── PURE: drill. A LOCAL re-group — no SQL is ever rewritten. ────────────
export function drillDown(tile, sel) {
  const spec = tile?.spec || {};
  const inter = spec.interaction || {};
  const h = Array.isArray(inter.drill?.hierarchy) ? inter.drill.hierarchy : [];
  const level = num(inter.drill?.level, 0, 0, 64);
  const next = level + 1;
  if (!h.length || next >= h.length || !sel || sel.field == null) return tile;
  const tf = spec.transform || {};
  const measures = (spec.fields?.measures || []).map((m) => ({
    col: m.col, agg: m.agg || "sum", as: m.col,
  }));
  return {
    ...tile,
    spec: {
      ...spec,
      x: h[next],
      transform: {
        ...tf,
        filters: [
          ...(Array.isArray(tf.filters) ? tf.filters : []),
          { col: h[level], op: "eq", value: sel.value, origin: "spec" },
        ],
        group: {
          by: [h[next]],
          measures: measures.length ? measures : tf.group?.measures || [],
        },
      },
      interaction: { ...inter, drill: { hierarchy: h, level: next } },
    },
  };
}

export function drillUp(tile) {
  const spec = tile?.spec || {};
  const inter = spec.interaction || {};
  const h = Array.isArray(inter.drill?.hierarchy) ? inter.drill.hierarchy : [];
  const level = num(inter.drill?.level, 0, 0, 64);
  if (!h.length || level <= 0) return tile;
  const prev = level - 1;
  const tf = spec.transform || {};
  const filters = (Array.isArray(tf.filters) ? tf.filters : []).filter((f) => f?.col !== h[prev]);
  const measures = (spec.fields?.measures || []).map((m) => ({
    col: m.col, agg: m.agg || "sum", as: m.col,
  }));
  return {
    ...tile,
    spec: {
      ...spec,
      x: h[prev],
      transform: {
        ...tf,
        filters,
        group: { by: [h[prev]], measures: measures.length ? measures : tf.group?.measures || [] },
      },
      interaction: { ...inter, drill: { hierarchy: h, level: prev } },
    },
  };
}

// ── Pin: fire-and-forget, recipe only (rows are never stored) ────────────
// Colour identity is frozen from the RENDERED frame, not `panel.base`: the
// renderer derives category keys from post-transform columns/rows, so a
// key_order taken from base rows would order categories the chart never shows
// and repaint the ones it does.
export async function pinPanelToDashboard(panel, opts = {}) {
  let spec = panel?.chart || null;
  const title = opts.title || spec?.title || "Untitled chart";
  const columns = Array.isArray(panel?.columns) ? panel.columns : [];
  const rows = Array.isArray(panel?.rows) ? panel.rows : [];
  if (spec && !spec.format?.palette?.key_order) {
    const keyOrder = captureKeyOrder(columns, rows, spec);
    if (keyOrder.length) {
      const fmt = spec.format && typeof spec.format === "object" ? spec.format : {};
      const palette = fmt.palette && typeof fmt.palette === "object" ? fmt.palette : {};
      spec = {
        ...spec,
        format: { ...fmt, palette: { ...palette, mode: palette.mode || "by_key", key_order: keyOrder } },
      };
    }
  }
  const d = await sharedApi("/dashboards/pin", {
    method: "POST",
    body: JSON.stringify({
      dashboard_id: opts.dashboardId || null,
      dashboard_title: opts.dashboardTitle || title,
      title,
      source: opts.source,
      table_label: opts.table ?? null,
      sql: panel?.sql || "",
      spec,
      // Server-side fallback for specs captureKeyOrder cannot resolve; PinIn
      // uses them for key_order only and never persists them.
      columns,
      rows: rows.slice(0, PIN_SAMPLE_ROWS),
    }),
  });
  return {
    dashboard_id: d?.dashboard_id ?? null,
    tile_id: d?.tile?.id ?? null,
    created_dashboard: !!d?.created_dashboard,
    warnings: d?.warnings || [],
  };
}

export function DashboardPicker({ onPick, onClose }) {
  const [list, setList] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    // GET /dashboards also returns org-visible dashboards; POST /dashboards/pin
    // is owner-only, so anything without can_edit would 403 after composing.
    sharedApi("/dashboards")
      .then((d) => setList((d?.dashboards || []).filter((x) => x && x.can_edit)))
      .catch((e) => setError(e.message));
  }, []);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal dash-picker" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div>
            <div className="canvas-title">Pin to dashboard</div>
            <div className="meta">The chart recipe is saved — data refreshes for every viewer.</div>
          </div>
          <button className="chip" onClick={onClose}>✕ close</button>
        </div>
        {error && <div className="error">{error}</div>}
        <div className="dash-picker-list">
          <button className="suggestion" onClick={() => onPick?.(null)}>＋ New dashboard</button>
          {list.map((d) => (
            <button key={d.id} className="suggestion" onClick={() => onPick?.(d.id)}>
              {d.title}
              <span className="meta">
                {" "}· {d.tile_count ?? 0} tiles · {d.visibility === "org" ? "org" : "private"}
              </span>
            </button>
          ))}
          {list.length === 0 && !error && <div className="meta">No dashboards you can pin to yet.</div>}
        </div>
      </div>
    </div>
  );
}

// ── Tile ─────────────────────────────────────────────────────────────────
// data is the TileData from POST /dashboards/{id}/data. Every extra prop
// beyond {tile,data,highlight,onSelect,onEdit,editable} is optional chrome.
export function DashboardTile({
  tile, data, highlight, onSelect, onEdit, editable,
  onRemove, onOpen, onRefresh, onDrillUp, onDragStart, pending,
}) {
  const [prompt, setPrompt] = useState("");
  const [asking, setAsking] = useState(false);
  const [showAsk, setShowAsk] = useState(false);
  const [note, setNote] = useState("");
  const [warnings, setWarnings] = useState([]);

  const spec = data?.spec || tile?.spec || null;
  const columns = data?.columns || [];
  const rows = data?.rows || [];
  const denied = !!data?.denied;
  const error = data?.error;
  const skipped = data?.skipped_filters || [];
  const drill = spec?.interaction?.drill;
  const level = num(drill?.level, 0, 0, 64);

  async function ask(e) {
    e?.preventDefault();
    const q = prompt.trim();
    if (!q || asking || !onEdit) return;
    setAsking(true);
    try {
      const n = await onEdit(q);
      setNote(n || "Updated.");
      setPrompt("");
    } catch (err) {
      setNote(`Edit failed: ${err.message}`);
    } finally {
      setAsking(false);
    }
  }

  function takeWarnings(w) {
    const next = Array.isArray(w) ? w : [];
    setWarnings((cur) => (cur.join("|") === next.join("|") ? cur : next));
  }

  return (
    <>
      <div
        className={"dash-tile-head" + (editable ? " dash-tile-drag" : "")}
        onPointerDown={editable && onDragStart ? onDragStart : undefined}
      >
        <div className="dash-tile-title">
          {tile?.title || spec?.title || "Untitled"}
          {level > 0 && Array.isArray(drill?.hierarchy) && (
            <span className="meta">
              {" "}· {drill.hierarchy.slice(0, level + 1).join(" › ")}
              <button className="chip" onClick={onDrillUp} title="Drill up">↰</button>
            </span>
          )}
        </div>
        <div className="dash-tile-actions" onPointerDown={(e) => e.stopPropagation()}>
          {pending && <span className="meta">…</span>}
          {onEdit && (
            <button className="chip" onClick={() => setShowAsk(!showAsk)} title="Edit this visual with a prompt">✎</button>
          )}
          {onRefresh && <button className="chip" onClick={onRefresh} title="Re-run this tile">⟳</button>}
          {onOpen && <button className="chip" onClick={onOpen} title="Open in canvas">⤢</button>}
          {editable && onRemove && (
            <button className="chip" onClick={onRemove} title="Remove this tile">✕</button>
          )}
        </div>
      </div>

      {showAsk && onEdit && (
        <form className="dash-tile-ask" onSubmit={ask}>
          <input
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder='"top 5 only", "as % of total", "add data labels"…'
            disabled={asking}
          />
          <button className="chip" disabled={asking || !prompt.trim()}>{asking ? "…" : "apply"}</button>
        </form>
      )}

      <div className="dash-tile-body">
        {denied || error ? (
          <div className="chart-note">
            {(typeof error === "string" ? error : error?.message) ||
              (denied ? "Your role has no access to this tile." : "This tile could not be rendered.")}
          </div>
        ) : !data ? (
          <div className="meta dash-tile-empty">loading…</div>
        ) : rows.length === 0 ? (
          <div className="meta dash-tile-empty">{spec?.format?.empty_text || "No data"}</div>
        ) : (
          <ChartView
            columns={columns}
            rows={rows}
            chart={spec}
            plain
            tileId={tile?.id}
            highlight={highlight || null}
            onSelect={onSelect || null}
            onWarnings={takeWarnings}
          />
        )}
      </div>

      {(skipped.length > 0 || warnings.length > 0 || note || data?.truncated) && (
        <div className="tile-warn">
          {skipped.map((s, i) => (
            <span key={i} className="chip" title={s.reason}>not filtered by {s.col || s.stage}</span>
          ))}
          {data?.truncated && <span className="chip">truncated at {data.row_count} rows</span>}
          {warnings.map((w, i) => (
            <span key={`w${i}`} className="meta">{w}</span>
          ))}
          {note && <span className="meta">{note}</span>}
        </div>
      )}
    </>
  );
}

// ── Dashboard ────────────────────────────────────────────────────────────
export default function Dashboard({
  dashboardId, user, api: apiFn = sharedApi, onClose, onOpenInCanvas, readOnly = false,
}) {
  const [doc, setDoc] = useState(null);
  const [data, setData] = useState({});
  const [fields, setFields] = useState([]);
  const [ctx, setCtx] = useState(EMPTY_CONTEXT);
  const [pending, setPending] = useState(false);
  const [inFlight, setInFlight] = useState([]); // tile ids being refetched
  const [error, setError] = useState("");
  const [arrange, setArrange] = useState(false);
  const [selected, setSelected] = useState(null);
  const [drag, setDrag] = useState(null);

  const docRef = useRef(null);
  const ctxRef = useRef(EMPTY_CONTEXT);
  const dataRef = useRef({});
  const dragRef = useRef(null);
  const seqRef = useRef(0);
  const abortRef = useRef(null);
  const loadedRef = useRef(false);
  const ctxReadyRef = useRef(false); // ctx is the restored one, not the initial empty
  const slicerKeyRef = useRef("[]");
  docRef.current = doc;
  ctxRef.current = ctx;
  dataRef.current = data;
  dragRef.current = drag;

  const canEdit = !readOnly && !!doc?.can_edit;
  const layout = { ...DEFAULT_LAYOUT, ...(doc?.layout && typeof doc.layout === "object" ? doc.layout : {}) };
  const cols = num(layout.cols, 12, 1, 24);
  const rowH = num(layout.row_h, 80, 24, 400);
  const gap = num(layout.gap, 12, 0, 48);

  const wire = useMemo(() => filterContextToWire(ctx), [ctx]);
  const wireKey = JSON.stringify(wire);
  const slicerKey = JSON.stringify(ctx.slicers || []);

  // ── document ──
  useEffect(() => {
    if (!dashboardId) return;
    let live = true;
    loadedRef.current = false;
    ctxReadyRef.current = false;
    setDoc(null);
    setData({});
    setFields([]);
    apiFn(`/dashboards/${dashboardId}`)
      .then((d) => {
        if (!live || !d) return;
        setDoc({ ...d, tiles: (Array.isArray(d.tiles) ? d.tiles : []).filter((t) => t && t.id) });
        // Session context: localStorage mirror wins, persisted slicers otherwise.
        let restored = null;
        try {
          restored = JSON.parse(localStorage.getItem("studio_dash_" + dashboardId) || "null");
        } catch {}
        const next = restored && typeof restored === "object"
          ? { slicers: restored.slicers || [], cross: restored.cross || [] }
          : { slicers: Array.isArray(d.filters) ? d.filters : [], cross: [] };
        slicerKeyRef.current = JSON.stringify(next.slicers);
        ctxReadyRef.current = true; // before setCtx: unblocks the mirror effect
        setCtx(next);
        setError("");
      })
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [dashboardId, apiFn]);

  // ── data ──
  const load = useCallback(
    async (opts = {}) => {
      const d = docRef.current;
      if (!d?.id) return;
      const seq = ++seqRef.current;
      const wantFields = opts.includeFields !== false;
      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;
      setPending(true);
      setInFlight(opts.tiles ?? (d.tiles || []).map((t) => t.id));
      try {
        const res = await apiFn(`/dashboards/${d.id}/data`, {
          method: "POST",
          signal: ac.signal,
          body: JSON.stringify({
            filters: opts.filters ?? filterContextToWire(ctxRef.current),
            tiles: opts.tiles ?? null,
            refresh: !!opts.refresh,
            include_fields: wantFields,
          }),
        });
        if (seq !== seqRef.current) return; // a newer request won
        setData((prev) => {
          const next = { ...prev };
          for (const t of res?.tiles || []) if (t?.tile_id) next[t.tile_id] = t;
          return next;
        });
        // render_dashboard returns `fields: []` whenever include_fields is
        // false — only a request that ASKED for the catalog may replace it.
        if (wantFields && Array.isArray(res?.fields)) setFields(res.fields);
        loadedRef.current = true;
        setError("");
      } catch (e) {
        if (e?.name === "AbortError") return;
        if (seq === seqRef.current) setError(e.message || String(e));
      } finally {
        if (seq === seqRef.current) {
          setPending(false);
          setInFlight([]);
        }
      }
    },
    [apiFn]
  );

  // Cross-filter round trips skip the clicked tile (it highlights locally) and
  // skip `fields` (options come from unfiltered base rows and never change).
  // A source tile may only be skipped when the rows it already holds were
  // rendered under exactly the filter set it would get now — otherwise it is
  // still showing a PREVIOUS selection's rows (a tile never filters itself, so
  // becoming the source drops filters that were applied to it a moment ago).
  useEffect(() => {
    if (!doc?.id) return;
    const crossOnly = loadedRef.current && slicerKeyRef.current === slicerKey;
    slicerKeyRef.current = slicerKey;
    const sources = new Set((ctx.cross || []).map((s) => s?.sourceTileId).filter(Boolean));
    const fresh = (id) => {
      const held = dataRef.current[id];
      return !!held && injectedKey(held.applied_filters, id) === injectedKey(wire, id);
    };
    const ids = crossOnly && sources.size
      ? (doc.tiles || []).map((t) => t.id).filter((id) => !(sources.has(id) && fresh(id)))
      : null;
    if (ids && ids.length === 0) return;
    const delay = loadedRef.current ? (crossOnly ? CROSS_DEBOUNCE : SLICER_DEBOUNCE) : 0;
    const h = setTimeout(
      () => load({ tiles: ids, includeFields: !loadedRef.current }),
      delay
    );
    return () => clearTimeout(h);
  }, [doc?.id, wireKey, slicerKey, load]);

  // Session mirror + slicer persistence (cross-filter state is never persisted).
  // The mirror must NOT run before the document effect has restored ctx, or the
  // mount-time EMPTY_CONTEXT overwrites the very key the restore reads back.
  useEffect(() => {
    if (!dashboardId || !ctxReadyRef.current) return;
    try {
      localStorage.setItem("studio_dash_" + dashboardId, JSON.stringify(ctx));
    } catch {}
  }, [ctx, dashboardId]);

  useEffect(() => {
    if (!doc?.id || !canEdit) return;
    if (JSON.stringify(doc.filters || []) === slicerKey) return;
    const h = setTimeout(() => {
      apiFn(`/dashboards/${doc.id}`, { method: "PATCH", body: JSON.stringify({ filters: ctxRef.current.slicers || [] }) })
        .then((d) => d && setDoc((x) => (x ? { ...x, filters: d.filters ?? x.filters } : x)))
        .catch(() => {});
    }, SAVE_DEBOUNCE);
    return () => clearTimeout(h);
  }, [slicerKey, doc?.id, canEdit, apiFn]);

  // ── layout ──
  const positions = useMemo(() => {
    const map = {};
    let cx = 0, cy = 0, rowMax = 0;
    for (const t of doc?.tiles || []) {
      const l = t?.layout && typeof t.layout === "object" ? t.layout : {};
      const w = num(l.w, 6, 1, cols);
      const h = num(l.h, 4, 1, 40);
      if (hasNum(l.x) && hasNum(l.y)) {
        map[t.id] = { x: num(l.x, 0, 0, Math.max(0, cols - w)), y: num(l.y, 0, 0, 9999), w, h };
      } else {
        if (cx + w > cols) {
          cx = 0;
          cy += rowMax || h;
          rowMax = 0;
        }
        map[t.id] = { x: cx, y: cy, w, h };
        cx += w;
        rowMax = Math.max(rowMax, h);
      }
    }
    return map;
  }, [doc, cols]);

  const gridRef = useRef(null);

  function beginDrag(e, tile, mode) {
    if (!canEdit || !arrange) return;
    const grid = gridRef.current;
    if (!grid) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = grid.getBoundingClientRect();
    const colPitch = (rect.width + gap) / cols;
    setDrag({
      id: tile.id, mode, x0: e.clientX, y0: e.clientY,
      colPitch: colPitch || 1, rowPitch: rowH + gap,
      base: positions[tile.id] || { x: 0, y: 0, w: 6, h: 4 },
      cur: positions[tile.id] || { x: 0, y: 0, w: 6, h: 4 },
    });
  }

  useEffect(() => {
    if (!drag) return;
    const move = (e) => {
      setDrag((d) => {
        if (!d) return d;
        const dx = Math.round((e.clientX - d.x0) / d.colPitch);
        const dy = Math.round((e.clientY - d.y0) / d.rowPitch);
        const b = d.base;
        const cur =
          d.mode === "move"
            ? { ...b, x: Math.min(Math.max(0, b.x + dx), Math.max(0, cols - b.w)), y: Math.max(0, b.y + dy) }
            : { ...b, w: Math.min(Math.max(1, b.w + dx), cols - b.x), h: Math.max(2, b.h + dy) };
        return { ...d, cur };
      });
    };
    const up = () => commitDrag();
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [drag?.id, drag?.mode, cols]);

  async function commitDrag() {
    const d = dragRef.current;
    setDrag(null);
    const cur = docRef.current;
    if (!d || !cur) return;
    const b = d.base, c = d.cur;
    if (b.x === c.x && b.y === c.y && b.w === c.w && b.h === c.h) return;
    const tiles = (cur.tiles || []).map((t) =>
      t.id === d.id ? { ...t, layout: c } : { ...t, layout: positions[t.id] || t.layout }
    );
    setDoc((x) => (x ? { ...x, tiles } : x));
    try {
      await apiFn(`/dashboards/${cur.id}/layout`, {
        method: "PUT",
        body: JSON.stringify({
          tiles: tiles.map((t) => ({ id: t.id, ...(t.layout || {}) })),
        }),
      });
    } catch (e) {
      setError(e.message);
    }
  }

  // ── interaction ──
  // A tile with a drill hierarchy drills instead of cross-filtering — but only
  // when the drill can actually be written back; otherwise the click still filters.
  function handleSelect(tile, sel) {
    if (!sel) return;
    const spec = data[tile.id]?.spec || tile.spec || {};
    const inter = spec.interaction || {};
    if (canEdit && sel.type !== "clear") {
      const cur = { ...tile, spec };
      const next = drillDown(cur, sel);
      if (next !== cur) {
        applyDrill(tile, next);
        return;
      }
    }
    if (inter.cross_filter === "none") return;
    setCtx((c) => ({ ...c, cross: reduceSelection(c.cross, sel, spec) }));
  }

  // Drill re-groups the tile's own spec; the server renders from the STORED
  // spec, so a drill has to be written back. View-only viewers can't drill.
  async function applyDrill(tile, next) {
    if (!canEdit || next === tile) return;
    setDoc((x) => (x ? { ...x, tiles: x.tiles.map((t) => (t.id === tile.id ? next : t)) } : x));
    try {
      await apiFn(`/dashboards/${doc.id}/tiles/${tile.id}`, {
        method: "PATCH",
        body: JSON.stringify({ spec: next.spec, spec_merge: false }),
      });
      await load({ tiles: [tile.id], includeFields: false });
    } catch (e) {
      setError(e.message);
    }
  }

  function highlightFor(tile) {
    const spec = data[tile.id]?.spec || tile.spec || {};
    if (spec.interaction?.self_highlight === false) return null;
    const mine = (ctx.cross || []).filter((s) => s && s.sourceTileId === tile.id && s.field);
    if (!mine.length) return null;
    const values = mine.flatMap(selValues);
    return { col: mine[0].field, values };
  }

  async function removeTile(tile) {
    if (!canEdit || !doc) return;
    setDoc((x) => (x ? { ...x, tiles: x.tiles.filter((t) => t.id !== tile.id) } : x));
    try {
      await apiFn(`/dashboards/${doc.id}/tiles/${tile.id}`, { method: "DELETE" });
    } catch (e) {
      setError(e.message);
    }
  }

  // One system prompt, one parser: prompt edits reuse POST /canvas/edit, then
  // the merged spec is written back to the tile.
  async function editTile(tile, instruction) {
    const d = data[tile.id] || {};
    const res = await apiFn("/canvas/edit", {
      method: "POST",
      body: JSON.stringify({
        instruction,
        columns: d.columns || [],
        rows: (d.rows || []).slice(0, 200),
        chart: d.spec || tile.spec,
        source: tile.source,
        table: tile.table_label,
        sql: tile.sql,
      }),
    });
    if (!res?.chart) return "No change.";
    const patched = await apiFn(`/dashboards/${doc.id}/tiles/${tile.id}`, {
      method: "PATCH",
      body: JSON.stringify({ spec: res.chart, spec_merge: false }),
    });
    const nextTile = patched?.tile || { ...tile, spec: res.chart };
    setDoc((x) => (x ? { ...x, tiles: x.tiles.map((t) => (t.id === tile.id ? nextTile : t)) } : x));
    await load({ tiles: [tile.id], includeFields: false });
    return res.note || "Updated.";
  }

  function openInCanvas(tile) {
    const d = data[tile.id];
    if (!onOpenInCanvas || !d) return;
    // /data returns post-transform rows only — `base` stays null so the lead
    // re-derives from the tile SQL instead of re-applying the transform.
    onOpenInCanvas(
      { sql: tile.sql, columns: d.columns || [], rows: d.rows || [], base: null, chart: d.spec || tile.spec },
      tile.source
    );
  }

  if (!dashboardId) return null;

  const tiles = doc?.tiles || [];

  return (
    <section className="dashboard">
      <div className="canvas-head">
        <div>
          <div className="canvas-title">{doc?.title || "Dashboard"}</div>
          <div className="meta">
            {doc?.description || `${tiles.length} tile${tiles.length === 1 ? "" : "s"}`}
            {doc?.owner && doc.owner !== user?.email && <> · shared by {doc.owner}</>}
            {!canEdit && doc && <> · view only</>}
          </div>
        </div>
        <div className="canvas-actions">
          <button className="chip" onClick={() => load({ refresh: true, includeFields: true })} disabled={pending}>
            ⟳ refresh
          </button>
          {!readOnly && doc?.can_edit && (
            <button
              className={"chip" + (arrange ? " chip-active" : "")}
              onClick={() => setArrange(!arrange)}
              title="Drag tile headers to move, corner to resize"
            >
              ⇹ arrange
            </button>
          )}
          {onClose && <button className="chip" onClick={onClose}>✕ close</button>}
        </div>
      </div>

      <FilterBar
        fields={fields}
        value={ctx}
        onChange={setCtx}
        onClear={() => setCtx(EMPTY_CONTEXT)}
        pending={pending}
      />

      {error && <div className="error">{error}</div>}

      <div
        className={"dashboard-grid" + (arrange ? " dashboard-arrange" : "")}
        ref={gridRef}
        style={{
          gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
          gridAutoRows: `${rowH}px`,
          gap: `${gap}px`,
        }}
      >
        {tiles.map((t) => {
          const pos = (drag && drag.id === t.id ? drag.cur : positions[t.id]) || { x: 0, y: 0, w: 6, h: 4 };
          const busy = inFlight.includes(t.id) && !!data[t.id];
          return (
            <div
              key={t.id}
              className={
                "dash-tile" +
                (selected === t.id ? " dash-tile-selected" : "") +
                (drag && drag.id === t.id ? " dash-tile-dragging" : "") +
                (busy ? " dash-tile-pending" : "")
              }
              style={{
                gridColumn: `${pos.x + 1} / span ${pos.w}`,
                gridRow: `${pos.y + 1} / span ${pos.h}`,
              }}
              onMouseDown={() => setSelected(t.id)}
            >
              <DashboardTile
                tile={t}
                data={data[t.id]}
                highlight={highlightFor(t)}
                onSelect={(sel) => handleSelect(t, sel)}
                onEdit={canEdit ? (q) => editTile(t, q) : null}
                editable={canEdit}
                onRemove={() => removeTile(t)}
                onOpen={onOpenInCanvas ? () => openInCanvas(t) : null}
                onRefresh={() => load({ tiles: [t.id], includeFields: false, refresh: true })}
                onDrillUp={() => applyDrill(t, drillUp(t))}
                onDragStart={arrange ? (e) => beginDrag(e, t, "move") : null}
                pending={busy}
              />
              {canEdit && arrange && (
                <div
                  className="dash-resize"
                  title="Drag to resize"
                  onPointerDown={(e) => beginDrag(e, t, "resize")}
                />
              )}
            </div>
          );
        })}
      </div>

      {doc && tiles.length === 0 && (
        <div className="empty">
          <div className="empty-title">Nothing pinned yet</div>
          <div className="empty-sub">
            Pin a chart from a chat answer or the canvas — the dashboard saves the query and the
            spec, then re-runs them for every viewer.
          </div>
        </div>
      )}
      {!doc && !error && <div className="meta">loading dashboard…</div>}
    </section>
  );
}
