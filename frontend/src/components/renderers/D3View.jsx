// D3 renderer — hand-built SVG for the whole chart catalog, so a screen can
// draw every type with D3 alone (the D3 & artifacts window does), plus the
// chart types only D3 draws here: a force-directed network, a beeswarm, a
// calendar heatmap and circle packing (D3_ONLY). Same validated palette as
// every other engine.
//
// d3 is loaded on demand (dynamic import) so the main bundle stays light. The
// data builders (networkData, beeswarmData, …, sankeyData) are pure — rows in,
// plain objects out — so the shapes the charts draw are tested without a DOM.
// Values come straight from a warehouse: they are written with
// .text()/textContent only, never as HTML.
import { useEffect, useRef, useState } from "react";
import {
  AXIS_LINE, BAD, GOOD, GRID_LINE, INK, SEQ, SERIES, SURFACE, TOTAL,
  firstNumericIndex, isNumericCol, resolveSpec,
} from "./common";
import { animate, captureBars } from "./motion";

/** Chart types no other engine draws: these always render with D3. */
export const D3_ONLY = new Set(["network", "beeswarm", "calendar", "circle_pack"]);
export const D3_TYPES = new Set([
  "bar", "hbar", "stacked_bar", "combo", "line", "area", "stacked_area",
  "pie", "donut", "scatter", "bubble", "heatmap",
  "treemap", "funnel", "radar", "gauge", "kpi",
  "histogram", "boxplot", "waterfall", "sankey", "table",
  ...D3_ONLY,
]);
// The type a D3-only screen draws: the one asked for, or a bar for a type D3
// does not know.
export const d3Type = (t) => (D3_TYPES.has(t) ? t : "bar");

// Size limits, shared with ChartView's fit rules so a chart is only offered
// when it will stay readable (and a force layout stays fast).
export const LIMITS = { networkNodes: 300, networkLinks: 2000, beeswarm: 2000,
                        calendarDays: 1100, packLeaves: 400, sankeyNodes: 30,
                        boxGroups: 20, waterfallSteps: 20, funnelStages: 10,
                        radarAxes: 12, tableRows: 200 };

let _d3 = null;
function getD3() {
  if (!_d3) _d3 = import("d3");
  return _d3;
}

// motion: the D3 & artifacts window animates its charts (renderers/motion.js)
// — in on first draw, and between states when a filter changes the rows. A
// resize alone redraws in place.
// fill: take the size of the frame around it (VizFrame) instead of a fixed height.
export default function D3View({ columns, rows, spec, tall, motion = false, fill = false }) {
  const ref = useRef(null);
  const drawn = useRef(null);
  const [err, setErr] = useState("");
  const [size, setSize] = useState(null);

  // Draw to the container's real size: dashboard tiles resize, panels reflow.
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => setErr(""), [columns, rows, spec]);

  useEffect(() => {
    const el = ref.current;
    if (!el || !size || size.w < 60 || size.h < 60) return undefined;
    let dead = false;
    let stop = null;
    getD3()
      .then((d3) => {
        if (dead) return;
        const last = drawn.current;
        const changed = !last || last.rows !== rows || last.columns !== columns
          || last.spec?.type !== spec?.type;
        const before = motion && changed ? captureBars(el) : null;
        el.replaceChildren();
        try {
          stop = draw(d3, el, columns, rows, spec, size);
          if (stop === false) setErr("This data cannot be drawn as this chart type in D3.");
          else if (before) animate(d3, el, spec, last?.spec?.type === spec?.type ? before : new Map());
          drawn.current = { columns, rows, spec };
        } catch (e) {
          setErr("D3 failed to render: " + String(e?.message || e).slice(0, 120));
        }
      })
      .catch((e) => setErr("D3 failed to load: " + String(e?.message || e).slice(0, 120)));
    return () => {
      dead = true;
      if (typeof stop === "function") stop();
    };
  }, [columns, rows, spec, size]);

  return (
    <>
      {err && <div className="chart-note">{err}</div>}
      <div ref={ref} className={"echart d3-chart" + (fill ? " echart-fill" : tall ? " echart-tall" : "")}
           style={err ? { display: "none" } : undefined} />
    </>
  );
}

// ── Pure data builders (exported for tests) ─────────────────────────────

const num = (v) => (typeof v === "number" ? v : Number(v));
const isNum = (v) => v !== null && v !== "" && Number.isFinite(num(v));

/** Nodes and weighted links for a network: x = source, y[0] = target, y[1] =
 *  an optional numeric weight. Repeated pairs add up; self-links are dropped. */
export function networkData(columns, rows, spec) {
  const si = columns.indexOf(spec.x);
  const ti = columns.indexOf(spec.y?.[0]);
  if (si < 0 || ti < 0 || si === ti) return null;
  const wi = spec.y?.[1] != null ? columns.indexOf(spec.y[1]) : -1;
  const pairs = new Map();
  for (const r of rows) {
    const a = r[si], b = r[ti];
    if (a == null || b == null || String(a) === String(b)) continue;
    const w = wi >= 0 ? num(r[wi]) : 1;
    if (!Number.isFinite(w) || w <= 0) continue;
    const key = `${a}\u0000${b}`;
    const hit = pairs.get(key);
    if (hit) hit.weight += w;
    else pairs.set(key, { source: String(a), target: String(b), weight: w });
  }
  const links = [...pairs.values()];
  const degree = new Map();
  for (const l of links) {
    degree.set(l.source, (degree.get(l.source) || 0) + l.weight);
    degree.set(l.target, (degree.get(l.target) || 0) + l.weight);
  }
  const nodes = [...degree].map(([id, d]) => ({ id, degree: d }));
  if (!links.length || nodes.length > LIMITS.networkNodes || links.length > LIMITS.networkLinks) {
    return null;
  }
  return { nodes, links };
}

/** One point per row for a beeswarm: y[0] = the value; x, when it is a
 *  category column, splits the swarm into rows. */
export function beeswarmData(columns, rows, spec) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const vi = yis[0] ?? (isNumericCol(rows, xi) ? xi : -1);
  if (vi < 0) return null;
  const gi = xi >= 0 && xi !== vi && !isNumericCol(rows, xi) ? xi : -1;
  const points = [];
  for (const r of rows) {
    if (!isNum(r[vi])) continue;
    points.push({ value: num(r[vi]), group: gi >= 0 ? String(r[gi]) : "" });
  }
  if (!points.length || points.length > LIMITS.beeswarm) return null;
  const groups = [...new Set(points.map((p) => p.group))];
  if (groups.length > 12) return null;
  return { points, groups, value: columns[vi], groupBy: gi >= 0 ? columns[gi] : "" };
}

const DAY = /^(\d{4})-(\d{2})-(\d{2})/;

/** "YYYY-MM-DD" for a date-ish cell (ISO text, a Date, or epoch ms), else null. */
export function dayKey(v) {
  if (v instanceof Date && !Number.isNaN(v.getTime())) return v.toISOString().slice(0, 10);
  if (typeof v === "string") {
    const m = DAY.exec(v.trim());
    return m ? `${m[1]}-${m[2]}-${m[3]}` : null;
  }
  if (typeof v === "number" && v > 1e11) return new Date(v).toISOString().slice(0, 10);
  return null;
}

/** Daily values for a calendar heatmap: x = a date column, y[0] = the value.
 *  Several rows on one day add up. */
export function calendarData(columns, rows, spec) {
  const di = columns.indexOf(spec.x);
  if (di < 0) return null;
  const vi = columns.indexOf(spec.y?.[0]) >= 0 ? columns.indexOf(spec.y[0])
    : firstNumericIndex(columns, rows, di);
  if (vi < 0) return null;
  const byDay = new Map();
  for (const r of rows) {
    const k = dayKey(r[di]);
    if (!k || !isNum(r[vi])) continue;
    byDay.set(k, (byDay.get(k) || 0) + num(r[vi]));
  }
  if (!byDay.size || byDay.size > LIMITS.calendarDays) return null;
  const days = [...byDay].map(([date, value]) => ({ date, value }))
    .sort((a, b) => (a.date < b.date ? -1 : 1));
  const years = [...new Set(days.map((d) => d.date.slice(0, 4)))];
  if (years.length > 3) return null;
  return { days, years, value: columns[vi] };
}

/** The hierarchy circle packing draws: x = the category (a leaf), y[0] = its
 *  size, and y[1] — when it names a category column — the parent group. */
export function packData(columns, rows, spec) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const vi = yis.find((i) => isNumericCol(rows, i)) ?? -1;
  if (xi < 0 || vi < 0) return null;
  const pi = spec.y?.[1] != null && columns.indexOf(spec.y[1]) >= 0
    && !isNumericCol(rows, columns.indexOf(spec.y[1])) ? columns.indexOf(spec.y[1]) : -1;
  const leaves = rows
    .filter((r) => r[xi] != null && isNum(r[vi]) && num(r[vi]) > 0)
    .map((r) => ({ name: String(r[xi]), value: num(r[vi]),
                   group: pi >= 0 ? String(r[pi] ?? "") : "" }));
  if (!leaves.length || leaves.length > LIMITS.packLeaves) return null;
  if (pi < 0) return { name: "root", children: leaves };
  const groups = new Map();
  for (const l of leaves) {
    if (!groups.has(l.group)) groups.set(l.group, []);
    groups.get(l.group).push(l);
  }
  return { name: "root", children: [...groups].map(([name, children]) => ({ name, children })) };
}

/** Equal-width bins over y[0] (or x when it is the numeric one), like the
 *  ECharts histogram: between 6 and 20 bins, about √n of them. */
export function histogramData(columns, rows, spec) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const vi = yis[0] ?? (xi >= 0 && isNumericCol(rows, xi) ? xi : firstNumericIndex(columns, rows));
  if (vi < 0) return null;
  const vals = rows.map((r) => r[vi]).filter(isNum).map(num);
  if (!vals.length) return null;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const n = Math.min(20, Math.max(6, Math.round(Math.sqrt(vals.length))));
  const width = (hi - lo) / n || 1;
  const bins = Array.from({ length: n }, (_, i) => ({ x0: lo + i * width, x1: lo + (i + 1) * width, count: 0 }));
  for (const v of vals) bins[Math.min(n - 1, Math.floor((v - lo) / width))].count++;
  return { bins, value: columns[vi] };
}

function quantile(sorted, q) {
  const at = (sorted.length - 1) * q;
  const lo = Math.floor(at), hi = Math.ceil(at);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (at - lo);
}

/** One box per x group over y[0]: quartiles, whiskers at 1.5×IQR (kept within
 *  the data), and the points beyond them as outliers. */
export function boxplotData(columns, rows, spec) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const vi = yis[0] ?? -1;
  if (xi < 0 || vi < 0) return null;
  const groups = new Map();
  for (const r of rows) {
    if (!isNum(r[vi])) continue;
    const k = String(r[xi]);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(num(r[vi]));
  }
  if (!groups.size || groups.size > LIMITS.boxGroups) return null;
  const boxes = [...groups].map(([name, vs]) => {
    const s = vs.slice().sort((a, b) => a - b);
    const q1 = quantile(s, 0.25), median = quantile(s, 0.5), q3 = quantile(s, 0.75);
    const iqr = q3 - q1;
    const inside = s.filter((v) => v >= q1 - 1.5 * iqr && v <= q3 + 1.5 * iqr);
    return { name, q1, median, q3, lo: inside[0], hi: inside[inside.length - 1], n: s.length,
             outliers: s.filter((v) => v < inside[0] || v > inside[inside.length - 1]) };
  });
  return { boxes, value: columns[vi] };
}

/** A bridge: each row is a delta (y[0]) from the running total, then the total. */
export function waterfallData(columns, rows, spec) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const vi = yis[0] ?? -1;
  if (xi < 0 || vi < 0 || !rows.length || rows.length > LIMITS.waterfallSteps) return null;
  let run = 0;
  const steps = rows.map((r) => {
    const delta = num(r[vi]) || 0;
    const step = { name: String(r[xi]), start: run, end: run + delta, delta,
                   kind: delta >= 0 ? "up" : "down" };
    run += delta;
    return step;
  });
  steps.push({ name: "total", start: 0, end: run, delta: run, kind: "total" });
  return { steps, value: columns[vi] };
}

/** Stages largest first, each with its share of the first stage. */
export function funnelData(columns, rows, spec) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
  if (xi < 0 || vi < 0) return null;
  const stages = rows.map((r) => ({ name: String(r[xi]), value: Math.max(0, num(r[vi]) || 0) }))
    .sort((a, b) => b.value - a.value);
  if (!stages.length || stages.length > LIMITS.funnelStages || !stages[0].value) return null;
  return { stages: stages.map((st) => ({ ...st, share: st.value / stages[0].value })),
           value: columns[vi] };
}

/** One axis per row (x), one polygon per numeric series (y), on a shared scale. */
export function radarData(columns, rows, spec) {
  const { xi, yCols, yis } = resolveSpec(columns, rows, spec);
  if (xi < 0 || !yis.length || rows.length < 3 || rows.length > LIMITS.radarAxes) return null;
  const series = yis.map((i, k) => ({ name: yCols[k], values: rows.map((r) => Math.max(0, num(r[i]) || 0)) }));
  const max = Math.max(...series.flatMap((s) => s.values)) || 1;
  return { axes: rows.map((r) => String(r[xi])), series, max };
}

/** The one number a KPI or a gauge shows: the single row's value, else the sum
 *  of the column (as the KPI card does). */
export function singleValue(columns, rows, spec) {
  const vi = columns.indexOf(spec?.y?.[0]) >= 0 ? columns.indexOf(spec.y[0]) : firstNumericIndex(columns, rows);
  if (vi < 0 || !rows.length) return null;
  const value = rows.length === 1 ? num(rows[0][vi]) : rows.reduce((a, r) => a + (num(r[vi]) || 0), 0);
  if (!Number.isFinite(value)) return null;
  return { value, label: columns[vi], rows: rows.length };
}

/** A flow diagram's graph: x = source, y[0] = target, y[1] (or the first other
 *  numeric column) = the flow. Repeated pairs add up, self-flows are dropped.
 *  Columns come from the longest path to each node, so a flow that loops back
 *  (a cycle) has no left-to-right layout and is refused. */
export function sankeyData(columns, rows, spec) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const ti = yis[0] ?? -1;
  if (xi < 0 || ti < 0 || ti === xi) return null;
  const vi = yis.length > 1 ? yis[1] : firstNumericIndex(columns, rows, xi, ti);
  if (vi < 0) return null;
  const pairs = new Map();
  for (const r of rows) {
    const a = r[xi], b = r[ti], v = num(r[vi]);
    if (a == null || b == null || String(a) === String(b) || !Number.isFinite(v) || v <= 0) continue;
    const key = `${a}\u0000${b}`;
    const hit = pairs.get(key);
    if (hit) hit.value += v;
    else pairs.set(key, { source: String(a), target: String(b), value: v });
  }
  const links = [...pairs.values()];
  const ids = [...new Set(links.flatMap((l) => [l.source, l.target]))];
  if (!links.length || ids.length > LIMITS.sankeyNodes) return null;
  const indeg = new Map(ids.map((id) => [id, 0]));
  for (const l of links) indeg.set(l.target, indeg.get(l.target) + 1);
  const layer = new Map(ids.map((id) => [id, 0]));
  const queue = ids.filter((id) => indeg.get(id) === 0);
  let seen = 0;
  while (queue.length) {
    const id = queue.shift();
    seen++;
    for (const l of links.filter((x) => x.source === id)) {
      layer.set(l.target, Math.max(layer.get(l.target), layer.get(id) + 1));
      indeg.set(l.target, indeg.get(l.target) - 1);
      if (indeg.get(l.target) === 0) queue.push(l.target);
    }
  }
  if (seen < ids.length) return null;                   // a cycle
  const flow = (id, side) => links.filter((l) => l[side] === id).reduce((a, l) => a + l.value, 0);
  const nodes = ids.map((id) => ({ id, layer: layer.get(id),
                                   value: Math.max(flow(id, "source"), flow(id, "target")) }));
  return { nodes, links, layers: Math.max(...nodes.map((n) => n.layer)) + 1, value: columns[vi] };
}

// ── Drawing ─────────────────────────────────────────────────────────────

function fmtNum(v) {
  if (!Number.isFinite(v)) return String(v);
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function svgFor(d3, el, { w, h }, title) {
  return d3.select(el).append("svg")
    .attr("width", w).attr("height", h).attr("viewBox", `0 0 ${w} ${h}`)
    .attr("role", "img").attr("aria-label", title || "Chart")
    .style("font", "11px inherit").style("display", "block");
}

/** A tooltip owned by this chart. Text only: data never becomes markup. */
function tooltip(el) {
  const tip = document.createElement("div");
  tip.className = "d3-tip";
  el.appendChild(tip);
  return {
    show(event, lines) {
      tip.textContent = lines.join("\n");
      const box = el.getBoundingClientRect();
      tip.style.left = `${Math.min(event.clientX - box.left + 12, box.width - 160)}px`;
      tip.style.top = `${event.clientY - box.top + 12}px`;
      tip.style.opacity = "1";
    },
    hide() { tip.style.opacity = "0"; },
  };
}

function hover(sel, tip, lines) {
  sel.on("mousemove", (event, d) => tip.show(event, lines(d)))
     .on("mouseleave", () => tip.hide());
}

function styleAxis(g) {
  g.selectAll(".domain").attr("stroke", AXIS_LINE);
  g.selectAll(".tick line").attr("stroke", AXIS_LINE);
  g.selectAll(".tick text").attr("fill", INK.muted);
  return g;
}

function gridY(d3, g, y, width) {
  g.append("g").call(d3.axisLeft(y).ticks(5).tickSize(-width).tickFormat(""))
    .call((a) => a.select(".domain").remove())
    .call((a) => a.selectAll(".tick line").attr("stroke", GRID_LINE));
}

/** Draw one chart into `el`. Returns false when the data does not fit, a stop
 *  function for an animated layout, or null. Exported for tests. */
export function draw(d3, el, columns, rows, spec, size) {
  switch (spec.type) {
    case "bar": case "hbar": case "stacked_bar": return drawBars(d3, el, columns, rows, spec, size);
    case "line": case "area": case "stacked_area": return drawLines(d3, el, columns, rows, spec, size);
    case "pie": case "donut": return drawPie(d3, el, columns, rows, spec, size);
    case "scatter": case "bubble": return drawScatter(d3, el, columns, rows, spec, size);
    case "heatmap": return drawHeatmap(d3, el, columns, rows, spec, size);
    case "network": return drawNetwork(d3, el, columns, rows, spec, size);
    case "beeswarm": return drawBeeswarm(d3, el, columns, rows, spec, size);
    case "calendar": return drawCalendar(d3, el, columns, rows, spec, size);
    case "circle_pack": return drawPack(d3, el, columns, rows, spec, size);
    case "combo": return drawCombo(d3, el, columns, rows, spec, size);
    case "treemap": return drawTreemap(d3, el, columns, rows, spec, size);
    case "funnel": return drawFunnel(d3, el, columns, rows, spec, size);
    case "radar": return drawRadar(d3, el, columns, rows, spec, size);
    case "gauge": return drawGauge(d3, el, columns, rows, spec, size);
    case "kpi": return drawKpi(d3, el, columns, rows, spec, size);
    case "histogram": return drawHistogram(d3, el, columns, rows, spec, size);
    case "boxplot": return drawBoxplot(d3, el, columns, rows, spec, size);
    case "waterfall": return drawWaterfall(d3, el, columns, rows, spec, size);
    case "sankey": return drawSankey(d3, el, columns, rows, spec, size);
    case "table": return drawTable(d3, el, columns, rows);
    default: return false;
  }
}

function legend(svg, names, x, y) {
  const g = svg.append("g").attr("transform", `translate(${x},${y})`);
  let at = 0;
  names.forEach((n, i) => {
    const item = g.append("g").attr("transform", `translate(${at},0)`);
    item.append("rect").attr("class", "legend-swatch").attr("width", 10).attr("height", 10).attr("rx", 2)
      .attr("fill", SERIES[i % SERIES.length]);
    const t = item.append("text").attr("x", 14).attr("y", 9).attr("fill", INK.secondary)
      .text(String(n).slice(0, 24));
    at += 22 + (t.node().getComputedTextLength?.() || String(n).length * 6);
  });
}

function drawBars(d3, el, columns, rows, spec, { w, h }) {
  const { xi, yCols, yis } = resolveSpec(columns, rows, spec);
  if (xi < 0 || !yis.length) return false;
  const horiz = spec.type === "hbar";
  const multi = yCols.length > 1;
  const stacked = spec.type === "stacked_bar" && multi;
  const cats = rows.map((r) => String(r[xi]));
  const data = rows.map((r) => Object.fromEntries([["__cat", String(r[xi])],
    ...yCols.map((c, k) => [c, num(r[yis[k]]) || 0])]));
  const labelW = horiz ? Math.min(120, 8 + 6.5 * Math.max(...cats.map((c) => c.length))) : 0;
  const m = { top: multi ? 26 : 12, right: 16, bottom: horiz ? 28 : 48, left: horiz ? labelW : 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);

  const series = stacked ? d3.stack().keys(yCols)(data) : null;
  const lo = Math.min(0, d3.min(stacked ? series.flat().map((d) => d[0]) : data.flatMap((d) => yCols.map((c) => d[c]))));
  const hi = Math.max(0, d3.max(stacked ? series.flat().map((d) => d[1]) : data.flatMap((d) => yCols.map((c) => d[c]))));
  const band = d3.scaleBand().domain(cats).range(horiz ? [0, ih] : [0, iw]).padding(0.2);
  const val = d3.scaleLinear().domain([lo, hi]).nice().range(horiz ? [0, iw] : [ih, 0]);
  const inner = d3.scaleBand().domain(yCols).range([0, band.bandwidth()]).padding(0.08);

  if (horiz) {
    g.append("g").call(d3.axisBottom(val).ticks(5).tickSize(-ih).tickFormat(""))
      .call((a) => a.select(".domain").remove())
      .call((a) => a.selectAll(".tick line").attr("stroke", GRID_LINE));
    styleAxis(g.append("g").attr("transform", `translate(0,${ih})`)
      .call(d3.axisBottom(val).ticks(5, "~s")));
    styleAxis(g.append("g").call(d3.axisLeft(band).tickSizeOuter(0)));
  } else {
    gridY(d3, g, val, iw);
    styleAxis(g.append("g").call(d3.axisLeft(val).ticks(5, "~s")));
    styleAxis(g.append("g").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(band).tickSizeOuter(0)))
      .selectAll(".tick text").attr("text-anchor", "end").attr("transform", "rotate(-30)")
      .text((d) => (d.length > 14 ? d.slice(0, 13) + "…" : d));
  }

  const rect = (sel) => sel.attr("rx", 3).attr("stroke", SURFACE).attr("stroke-width", 1);
  if (stacked) {
    series.forEach((s, k) => {
      const bars = g.append("g").attr("fill", SERIES[k % SERIES.length]).selectAll("rect")
        .data(s).join("rect").call(rect);
      if (horiz) {
        bars.attr("y", (d) => band(d.data.__cat)).attr("height", band.bandwidth())
          .attr("x", (d) => val(d[0])).attr("width", (d) => Math.max(0, val(d[1]) - val(d[0])));
      } else {
        bars.attr("x", (d) => band(d.data.__cat)).attr("width", band.bandwidth())
          .attr("y", (d) => val(d[1])).attr("height", (d) => Math.max(0, val(d[0]) - val(d[1])));
      }
      hover(bars, tip, (d) => [d.data.__cat, `${s.key}: ${fmtNum(d[1] - d[0])}`]);
    });
  } else {
    yCols.forEach((c, k) => {
      const bars = g.append("g").selectAll("rect").data(data).join("rect").call(rect)
        .attr("fill", (d, i) => (multi ? SERIES[k % SERIES.length] : SERIES[i % SERIES.length]));
      const off = (d) => band(d.__cat) + (multi ? inner(c) : 0);
      const thick = multi ? inner.bandwidth() : band.bandwidth();
      if (horiz) {
        bars.attr("y", off).attr("height", thick)
          .attr("x", (d) => val(Math.min(0, d[c]))).attr("width", (d) => Math.abs(val(d[c]) - val(0)));
      } else {
        bars.attr("x", off).attr("width", thick)
          .attr("y", (d) => val(Math.max(0, d[c]))).attr("height", (d) => Math.abs(val(d[c]) - val(0)));
      }
      hover(bars, tip, (d) => [d.__cat, `${c}: ${fmtNum(d[c])}`]);
    });
  }
  if (multi) legend(svg, yCols, m.left, 6);
  return null;
}

/** The x scale for a line/area: time when x holds dates, linear when numbers,
 *  otherwise evenly spaced points in row order. */
function xScaleFor(d3, values, width) {
  if (values.length && values.every((v) => dayKey(v))) {
    const ts = values.map((v) => new Date(String(dayKey(v)) + "T00:00:00Z"));
    return { scale: d3.scaleUtc().domain(d3.extent(ts)).range([0, width]),
             at: (i) => ts[i], axis: (s) => d3.axisBottom(s).ticks(Math.max(2, Math.floor(width / 90))) };
  }
  if (values.length && values.every((v) => typeof v === "number")) {
    return { scale: d3.scaleLinear().domain(d3.extent(values)).nice().range([0, width]),
             at: (i) => values[i], axis: (s) => d3.axisBottom(s).ticks(6, "~s") };
  }
  const keys = values.map((v, i) => `${i}\u0000${v}`);
  const point = d3.scalePoint().domain(keys).range([0, width]).padding(0.3);
  const every = Math.max(1, Math.ceil(values.length / Math.max(2, Math.floor(width / 70))));
  return { scale: point, at: (i) => keys[i],
           axis: (s) => d3.axisBottom(s).tickValues(keys.filter((_, i) => i % every === 0))
             .tickFormat((k) => String(k).split("\u0000")[1].slice(0, 12)) };
}

function drawLines(d3, el, columns, rows, spec, { w, h }) {
  const { xi, yCols, yis } = resolveSpec(columns, rows, spec);
  if (xi < 0 || !yis.length) return false;
  const multi = yCols.length > 1;
  const m = { top: multi ? 26 : 12, right: 16, bottom: 32, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const xv = rows.map((r) => r[xi]);
  const X = xScaleFor(d3, xv, iw);
  const data = rows.map((r, i) => Object.fromEntries([["__i", i],
    ...yCols.map((c, k) => [c, num(r[yis[k]]) || 0])]));
  const stacked = spec.type === "stacked_area" && multi;
  // Every series as [base, top] points carrying their row, the shape
  // d3.stack produces — so stacked and plain series draw with one code path.
  const layers = stacked ? d3.stack().keys(yCols)(data)
    : yCols.map((c) => Object.assign(data.map((d) => Object.assign([0, d[c]], { data: d })),
                                     { key: c }));
  const lo = Math.min(0, d3.min(layers.flat(), (p) => (stacked ? p[0] : p[1])));
  const hi = d3.max(layers.flat(), (p) => p[1]);
  const y = d3.scaleLinear().domain([lo, hi]).nice().range([ih, 0]);
  gridY(d3, g, y, iw);
  styleAxis(g.append("g").call(d3.axisLeft(y).ticks(5, "~s")));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`).call(X.axis(X.scale)));
  const px = (p) => X.scale(X.at(p.data.__i));
  const isArea = spec.type !== "line";
  layers.forEach((L, k) => {
    const color = SERIES[k % SERIES.length];
    if (isArea) {
      g.append("path").datum(L).attr("fill", color).attr("opacity", 0.35)
        .attr("d", d3.area().curve(d3.curveMonotoneX).x(px)
          .y0((p) => y(stacked ? p[0] : Math.max(lo, 0))).y1((p) => y(p[1])));
    }
    g.append("path").datum(L).attr("fill", "none").attr("stroke", color).attr("stroke-width", 2)
      .attr("d", d3.line().curve(d3.curveMonotoneX).x(px).y((p) => y(p[1])));
    const dots = g.append("g").selectAll("circle").data(L).join("circle")
      .attr("cx", px).attr("cy", (p) => y(p[1])).attr("r", 7).attr("fill", "transparent");
    hover(dots, tip, (p) => [String(rows[p.data.__i][xi]), `${L.key}: ${fmtNum(p.data[L.key])}`]);
  });
  if (multi) legend(svg, yCols, m.left, 6);
  return null;
}

function drawPie(d3, el, columns, rows, spec, { w, h }) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
  if (xi < 0 || vi < 0) return false;
  const data = rows.map((r) => ({ name: String(r[xi]), value: Math.max(0, num(r[vi]) || 0) }))
    .filter((d) => d.value > 0);
  if (!data.length) return false;
  const total = d3.sum(data, (d) => d.value);
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const legendW = Math.min(170, w * 0.35);
  const r = Math.max(20, Math.min(w - legendW - 24, h - 16) / 2);
  const g = svg.append("g").attr("transform", `translate(${r + 12},${h / 2})`);
  const arc = d3.arc().innerRadius(spec.type === "donut" ? r * 0.58 : 0).outerRadius(r)
    .padAngle(0.01).cornerRadius(3);
  const arcs = g.selectAll("path").data(d3.pie().sort(null).value((d) => d.value)(data))
    .join("path").attr("d", arc).attr("fill", (_, i) => SERIES[i % SERIES.length])
    .attr("stroke", SURFACE).attr("stroke-width", 2);
  hover(arcs, tip, (d) => [d.data.name, `${columns[vi]}: ${fmtNum(d.data.value)}`,
                           `${((d.data.value / total) * 100).toFixed(1)}% of total`]);
  const shown = data.slice(0, Math.floor((h - 16) / 18));
  const lg = svg.append("g").attr("transform", `translate(${2 * r + 36},${(h - shown.length * 18) / 2})`);
  shown.forEach((d, i) => {
    const row = lg.append("g").attr("transform", `translate(0,${i * 18})`);
    row.append("rect").attr("width", 10).attr("height", 10).attr("rx", 2)
      .attr("fill", SERIES[i % SERIES.length]);
    row.append("text").attr("x", 14).attr("y", 9).attr("fill", INK.secondary)
      .text(`${d.name.slice(0, 18)} · ${((d.value / total) * 100).toFixed(0)}%`);
  });
  return null;
}

function drawScatter(d3, el, columns, rows, spec, { w, h }) {
  const { xi, yCols, yis } = resolveSpec(columns, rows, spec);
  if (xi < 0 || !yis.length) return false;
  const si = spec.type === "bubble" && yis.length > 1 ? yis[1] : -1;
  const m = { top: 12, right: 16, bottom: 32, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const pts = rows.filter((r) => isNum(r[yis[0]]));
  const numericX = isNumericCol(rows, xi);
  const x = numericX
    ? d3.scaleLinear().domain(d3.extent(pts, (r) => num(r[xi]))).nice().range([0, iw])
    : d3.scalePoint().domain([...new Set(pts.map((r) => String(r[xi])))]).range([0, iw]).padding(0.5);
  const y = d3.scaleLinear().domain(d3.extent(pts, (r) => num(r[yis[0]]))).nice().range([ih, 0]);
  const rad = si >= 0
    ? d3.scaleSqrt().domain([0, d3.max(pts, (r) => Math.abs(num(r[si])) || 0) || 1]).range([3, 22])
    : () => 5;
  gridY(d3, g, y, iw);
  styleAxis(g.append("g").call(d3.axisLeft(y).ticks(5, "~s")));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`)
    .call(numericX ? d3.axisBottom(x).ticks(6, "~s") : d3.axisBottom(x)));
  const dots = g.append("g").selectAll("circle").data(pts).join("circle")
    .attr("cx", (r) => x(numericX ? num(r[xi]) : String(r[xi]))).attr("cy", (r) => y(num(r[yis[0]])))
    .attr("r", (r) => (si >= 0 ? rad(Math.abs(num(r[si])) || 0) : 5))
    .attr("fill", SERIES[0]).attr("fill-opacity", 0.8).attr("stroke", SURFACE);
  hover(dots, tip, (r) => [`${spec.x}: ${r[xi]}`, `${yCols[0]}: ${fmtNum(num(r[yis[0]]))}`,
                           ...(si >= 0 ? [`${columns[si]}: ${fmtNum(num(r[si]))}`] : [])]);
  return null;
}

function drawHeatmap(d3, el, columns, rows, spec, { w, h }) {
  const { xi, yis } = resolveSpec(columns, rows, spec);
  if (xi < 0 || !yis.length) return false;
  const ryi = yis[0];
  const vi = yis.length > 1 ? yis[1] : firstNumericIndex(columns, rows, xi, ryi);
  if (vi < 0) return false;
  const xs = [...new Set(rows.map((r) => String(r[xi])))];
  const ys = [...new Set(rows.map((r) => String(r[ryi])))];
  const m = { top: 12, right: 16, bottom: 48, left: Math.min(120, 8 + 6.5 * Math.max(...ys.map((s) => s.length))) };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const x = d3.scaleBand().domain(xs).range([0, iw]).padding(0.04);
  const y = d3.scaleBand().domain(ys).range([0, ih]).padding(0.04);
  const vals = rows.map((r) => num(r[vi])).filter(Number.isFinite);
  const color = d3.scaleSequential(d3.interpolateRgbBasis(SEQ)).domain(d3.extent(vals));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).tickSizeOuter(0)))
    .selectAll(".tick text").attr("text-anchor", "end").attr("transform", "rotate(-30)");
  styleAxis(g.append("g").call(d3.axisLeft(y).tickSizeOuter(0)));
  const cells = g.append("g").selectAll("rect").data(rows.filter((r) => isNum(r[vi]))).join("rect")
    .attr("x", (r) => x(String(r[xi]))).attr("y", (r) => y(String(r[ryi])))
    .attr("width", x.bandwidth()).attr("height", y.bandwidth()).attr("rx", 2)
    .attr("fill", (r) => color(num(r[vi])));
  hover(cells, tip, (r) => [`${r[xi]} · ${r[ryi]}`, `${columns[vi]}: ${fmtNum(num(r[vi]))}`]);
  return null;
}

function drawNetwork(d3, el, columns, rows, spec, { w, h }) {
  const data = networkData(columns, rows, spec);
  if (!data) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const nodes = data.nodes.map((n) => ({ ...n }));
  const links = data.links.map((l) => ({ ...l }));
  const maxDeg = d3.max(nodes, (n) => n.degree) || 1;
  const radius = d3.scaleSqrt().domain([0, maxDeg]).range([3, 16]);
  const width = d3.scaleLinear().domain([0, d3.max(links, (l) => l.weight) || 1]).range([0.8, 5]);
  const labelled = new Set(nodes.slice().sort((a, b) => b.degree - a.degree).slice(0, 12).map((n) => n.id));
  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((n) => n.id).distance(50))
    .force("charge", d3.forceManyBody().strength(-120))
    .force("center", d3.forceCenter(w / 2, h / 2))
    .force("collide", d3.forceCollide((n) => radius(n.degree) + 2))
    .force("x", d3.forceX(w / 2).strength(0.04))
    .force("y", d3.forceY(h / 2).strength(0.06));
  const link = svg.append("g").attr("stroke", AXIS_LINE).attr("stroke-opacity", 0.8)
    .selectAll("line").data(links).join("line").attr("stroke-width", (l) => width(l.weight));
  const node = svg.append("g").selectAll("circle").data(nodes).join("circle")
    .attr("r", (n) => radius(n.degree)).attr("fill", SERIES[0])
    .attr("stroke", SURFACE).attr("stroke-width", 1.5).style("cursor", "grab");
  const label = svg.append("g").selectAll("text").data(nodes.filter((n) => labelled.has(n.id)))
    .join("text").attr("fill", INK.secondary).attr("dy", -6).attr("text-anchor", "middle")
    .text((n) => n.id.slice(0, 20));
  hover(node, tip, (n) => [n.id, `weighted links: ${fmtNum(n.degree)}`]);
  hover(link, tip, (l) => [`${l.source.id} → ${l.target.id}`, `weight: ${fmtNum(l.weight)}`]);
  // Drag a node to untangle it; let go and it settles back in.
  node.call(d3.drag()
    .on("start", (event, n) => { if (!event.active) sim.alphaTarget(0.3).restart(); n.fx = n.x; n.fy = n.y; })
    .on("drag", (event, n) => { n.fx = event.x; n.fy = event.y; })
    .on("end", (event, n) => { if (!event.active) sim.alphaTarget(0); n.fx = null; n.fy = null; }));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  sim.on("tick", () => {
    for (const n of nodes) {
      n.x = clamp(n.x, 8, w - 8);
      n.y = clamp(n.y, 8, h - 8);
    }
    link.attr("x1", (l) => l.source.x).attr("y1", (l) => l.source.y)
      .attr("x2", (l) => l.target.x).attr("y2", (l) => l.target.y);
    node.attr("cx", (n) => n.x).attr("cy", (n) => n.y);
    label.attr("x", (n) => n.x).attr("y", (n) => n.y - radius(n.degree));
  });
  return () => sim.stop();
}

function drawBeeswarm(d3, el, columns, rows, spec, { w, h }) {
  const data = beeswarmData(columns, rows, spec);
  if (!data) return false;
  const labelW = data.groupBy ? Math.min(120, 8 + 6.5 * Math.max(...data.groups.map((s) => s.length))) : 12;
  const m = { top: 12, right: 16, bottom: 32, left: labelW };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const x = d3.scaleLinear().domain(d3.extent(data.points, (p) => p.value)).nice().range([0, iw]);
  const band = d3.scaleBand().domain(data.groups).range([0, ih]).padding(0.1);
  const r = Math.max(1.5, Math.min(5, Math.sqrt((iw * ih) / (data.points.length * 14))));
  const pts = data.points.map((p) => ({ ...p, x: x(p.value), y: band(p.group) + band.bandwidth() / 2 }));
  // Settle the swarm before drawing: a static layout, no animation to stop.
  const sim = d3.forceSimulation(pts)
    .force("x", d3.forceX((p) => x(p.value)).strength(1))
    .force("y", d3.forceY((p) => band(p.group) + band.bandwidth() / 2).strength(0.08))
    .force("collide", d3.forceCollide(r + 0.6))
    .stop();
  for (let i = 0; i < 160; i++) sim.tick();
  g.append("g").call(d3.axisBottom(x).ticks(6).tickSize(ih).tickFormat(""))
    .call((a) => a.select(".domain").remove())
    .call((a) => a.selectAll(".tick line").attr("stroke", GRID_LINE));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).ticks(6, "~s")));
  if (data.groupBy) styleAxis(g.append("g").call(d3.axisLeft(band).tickSizeOuter(0)));
  const dots = g.append("g").selectAll("circle").data(pts).join("circle")
    .attr("cx", (p) => p.x).attr("cy", (p) => Math.max(r, Math.min(ih - r, p.y))).attr("r", r)
    .attr("fill", (p) => SERIES[Math.max(0, data.groups.indexOf(p.group)) % SERIES.length])
    .attr("fill-opacity", 0.85);
  hover(dots, tip, (p) => [...(data.groupBy ? [`${data.groupBy}: ${p.group}`] : []),
                           `${data.value}: ${fmtNum(p.value)}`]);
  return null;
}

function drawCalendar(d3, el, columns, rows, spec, { w, h }) {
  const data = calendarData(columns, rows, spec);
  if (!data) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const values = new Map(data.days.map((d) => [d.date, d.value]));
  const color = d3.scaleSequential(d3.interpolateRgbBasis(SEQ))
    .domain(d3.extent(data.days, (d) => d.value));
  const left = 28, top = 18, gap = 18;
  const cell = Math.max(4, Math.min((w - left - 8) / 53,
                                    (h - top - gap * (data.years.length - 1) - 4) / (7 * data.years.length)));
  const dayOf = (d) => (d.getUTCDay() + 6) % 7;            // Monday first
  data.years.forEach((year, yi) => {
    const g = svg.append("g").attr("transform", `translate(${left},${top + yi * (cell * 7 + gap)})`);
    g.append("text").attr("x", -6).attr("y", -6).attr("fill", INK.muted).attr("font-weight", 600)
      .text(year);
    ["Mon", "Wed", "Fri"].forEach((name, i) => g.append("text").attr("x", -4)
      .attr("y", (i * 2 + 0.75) * cell).attr("text-anchor", "end").attr("fill", INK.muted)
      .attr("font-size", Math.min(10, cell)).text(name[0]));
    const start = new Date(Date.UTC(+year, 0, 1));
    const days = d3.utcDays(start, new Date(Date.UTC(+year + 1, 0, 1)));
    const week = (d) => d3.utcMonday.count(d3.utcMonday.floor(start), d);
    const cells = g.selectAll("rect").data(days).join("rect")
      .attr("x", (d) => week(d) * cell).attr("y", (d) => dayOf(d) * cell)
      .attr("width", cell - 1).attr("height", cell - 1).attr("rx", 1.5)
      .attr("fill", (d) => {
        const v = values.get(d.toISOString().slice(0, 10));
        return v === undefined ? GRID_LINE : color(v);
      });
    hover(cells, tip, (d) => {
      const k = d.toISOString().slice(0, 10);
      return values.has(k) ? [k, `${data.value}: ${fmtNum(values.get(k))}`] : [k, "no data"];
    });
    d3.utcMonths(start, new Date(Date.UTC(+year + 1, 0, 1))).forEach((mo) => {
      g.append("text").attr("x", week(mo) * cell).attr("y", -6).attr("fill", INK.muted)
        .attr("font-size", Math.min(10, cell + 2)).text(d3.utcFormat("%b")(mo));
    });
  });
  return null;
}

function drawPack(d3, el, columns, rows, spec, { w, h }) {
  const tree = packData(columns, rows, spec);
  if (!tree) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const root = d3.pack().size([w - 4, h - 4]).padding(3)(
    d3.hierarchy(tree).sum((d) => d.value || 0).sort((a, b) => b.value - a.value));
  const grouped = root.height > 1;
  const groupNames = grouped ? root.children.map((c) => c.data.name) : [];
  const g = svg.append("g").attr("transform", "translate(2,2)");
  const circles = g.selectAll("circle").data(root.descendants().slice(1)).join("circle")
    .attr("cx", (d) => d.x).attr("cy", (d) => d.y).attr("r", (d) => d.r)
    .attr("fill", (d) => (d.children ? "none"
      : SERIES[(grouped ? groupNames.indexOf(d.parent.data.name) : d.parent.children.indexOf(d))
               % SERIES.length]))
    .attr("fill-opacity", (d) => (d.children ? 1 : 0.8))
    .attr("stroke", (d) => (d.children ? AXIS_LINE : SURFACE));
  hover(circles, tip, (d) => [d.data.name, `${grouped && !d.children ? d.parent.data.name + " · " : ""}`
    + `${fmtNum(d.value)}`]);
  g.selectAll("text").data(root.leaves().filter((d) => d.r > 18)).join("text")
    .attr("x", (d) => d.x).attr("y", (d) => d.y + 4).attr("text-anchor", "middle")
    .attr("fill", SURFACE).attr("pointer-events", "none")
    .text((d) => d.data.name.slice(0, Math.max(3, Math.floor(d.r / 4))));
  return null;
}

/** Bars for y[0], lines for the rest, on one value axis (as ECharts' combo). */
function drawCombo(d3, el, columns, rows, spec, { w, h }) {
  const { xi, yCols, yis } = resolveSpec(columns, rows, spec);
  if (xi < 0 || yis.length < 2) return false;
  const m = { top: 26, right: 16, bottom: 48, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const cats = rows.map((r, i) => `${i}\u0000${r[xi]}`);
  const vals = rows.flatMap((r) => yis.map((i) => num(r[i]) || 0));
  const x = d3.scaleBand().domain(cats).range([0, iw]).padding(0.25);
  const y = d3.scaleLinear().domain([Math.min(0, d3.min(vals)), Math.max(0, d3.max(vals))]).nice().range([ih, 0]);
  gridY(d3, g, y, iw);
  styleAxis(g.append("g").call(d3.axisLeft(y).ticks(5, "~s")));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`)
    .call(d3.axisBottom(x).tickFormat((k) => String(k).split("\u0000")[1].slice(0, 13)).tickSizeOuter(0)))
    .selectAll(".tick text").attr("text-anchor", "end").attr("transform", "rotate(-30)");
  const bars = g.append("g").selectAll("rect").data(rows).join("rect")
    .attr("x", (_, i) => x(cats[i])).attr("width", x.bandwidth()).attr("rx", 3)
    .attr("y", (r) => y(Math.max(0, num(r[yis[0]]) || 0)))
    .attr("height", (r) => Math.abs(y(num(r[yis[0]]) || 0) - y(0))).attr("fill", SERIES[0]);
  hover(bars, tip, (r) => [String(r[xi]), `${yCols[0]}: ${fmtNum(num(r[yis[0]]))}`]);
  yis.slice(1).forEach((vi, k) => {
    const color = SERIES[(k + 1) % SERIES.length];
    const at = (r, i) => [x(cats[i]) + x.bandwidth() / 2, y(num(r[vi]) || 0)];
    g.append("path").datum(rows).attr("fill", "none").attr("stroke", color).attr("stroke-width", 2)
      .attr("d", d3.line().x((r, i) => at(r, i)[0]).y((r, i) => at(r, i)[1]));
    const dots = g.append("g").selectAll("circle").data(rows).join("circle")
      .attr("cx", (r, i) => at(r, i)[0]).attr("cy", (r, i) => at(r, i)[1]).attr("r", 3.5)
      .attr("fill", color).attr("stroke", SURFACE);
    hover(dots, tip, (r) => [String(r[xi]), `${yCols[k + 1]}: ${fmtNum(num(r[vi]))}`]);
  });
  legend(svg, yCols, m.left, 6);
  return null;
}

function drawTreemap(d3, el, columns, rows, spec, { w, h }) {
  const tree = packData(columns, rows, spec);
  if (!tree) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const root = d3.treemap().size([w, h]).paddingInner(2).paddingOuter(1).round(true)(
    d3.hierarchy(tree).sum((d) => d.value || 0).sort((a, b) => b.value - a.value));
  const grouped = root.height > 1;
  const groupNames = grouped ? root.children.map((c) => c.data.name) : [];
  const leaves = root.leaves();
  const color = (d, i) => SERIES[(grouped ? groupNames.indexOf(d.parent.data.name) : i) % SERIES.length];
  const cell = svg.selectAll("g").data(leaves).join("g").attr("transform", (d) => `translate(${d.x0},${d.y0})`);
  const tiles = cell.append("rect").attr("width", (d) => Math.max(0, d.x1 - d.x0))
    .attr("height", (d) => Math.max(0, d.y1 - d.y0)).attr("rx", 3).attr("fill", color);
  hover(tiles, tip, (d) => [d.data.name, `${grouped ? d.parent.data.name + " · " : ""}${fmtNum(d.value)}`,
                            `${((d.value / root.value) * 100).toFixed(1)}% of total`]);
  cell.filter((d) => d.x1 - d.x0 > 46 && d.y1 - d.y0 > 20).append("text")
    .attr("x", 6).attr("y", 15).attr("fill", SURFACE).attr("pointer-events", "none")
    .text((d) => d.data.name.slice(0, Math.max(3, Math.floor((d.x1 - d.x0 - 8) / 6.5))));
  return null;
}

function drawFunnel(d3, el, columns, rows, spec, { w, h }) {
  const data = funnelData(columns, rows, spec);
  if (!data) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const labelW = Math.min(220, w * 0.38);
  const fw = w - labelW - 16, gap = 3;
  const sh = (h - 8 - gap * (data.stages.length - 1)) / data.stages.length;
  const cx = fw / 2 + 8;
  data.stages.forEach((st, i) => {
    const next = data.stages[i + 1] ? data.stages[i + 1].share : st.share * 0.85;
    const top = 4 + i * (sh + gap), a = (fw * st.share) / 2, b = (fw * next) / 2;
    const shape = svg.append("path").datum(st)
      .attr("d", `M${cx - a},${top}L${cx + a},${top}L${cx + b},${top + sh}L${cx - b},${top + sh}Z`)
      .attr("fill", SERIES[i % SERIES.length]);
    hover(shape, tip, (d) => [d.name, `${data.value}: ${fmtNum(d.value)}`,
                              `${(d.share * 100).toFixed(1)}% of the first stage`]);
    svg.append("text").attr("x", fw + 24).attr("y", top + sh / 2 + 4).attr("fill", INK.secondary)
      .text(`${st.name.slice(0, 22)} · ${fmtNum(st.value)} (${(st.share * 100).toFixed(0)}%)`);
  });
  return null;
}

function drawRadar(d3, el, columns, rows, spec, { w, h }) {
  const data = radarData(columns, rows, spec);
  if (!data) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const top = data.series.length > 1 ? 26 : 8;
  const r = Math.max(20, Math.min(w, h - top) / 2 - 34);
  const g = svg.append("g").attr("transform", `translate(${w / 2},${top + (h - top) / 2})`);
  const n = data.axes.length;
  const angle = (i) => (i / n) * 2 * Math.PI;
  const radius = d3.scaleLinear().domain([0, data.max * 1.1]).range([0, r]);
  const at = (i, v) => [Math.sin(angle(i)) * radius(v), -Math.cos(angle(i)) * radius(v)];
  for (let level = 1; level <= 4; level++) {
    const v = (data.max * 1.1 * level) / 4;
    g.append("path").attr("fill", "none").attr("stroke", GRID_LINE)
      .attr("d", d3.line()(data.axes.map((_, i) => at(i, v))) + "Z");
  }
  data.axes.forEach((name, i) => {
    const [ex, ey] = at(i, data.max * 1.1);
    g.append("line").attr("x2", ex).attr("y2", ey).attr("stroke", AXIS_LINE);
    const [lx, ly] = at(i, data.max * 1.24);
    g.append("text").attr("x", lx).attr("y", ly + 4).attr("fill", INK.muted)
      .attr("text-anchor", Math.abs(lx) < 4 ? "middle" : lx > 0 ? "start" : "end").text(name.slice(0, 16));
  });
  data.series.forEach((s, k) => {
    const color = SERIES[k % SERIES.length];
    g.append("path").attr("d", d3.line()(s.values.map((v, i) => at(i, v))) + "Z")
      .attr("fill", color).attr("fill-opacity", 0.12).attr("stroke", color).attr("stroke-width", 2);
    const dots = g.append("g").selectAll("circle").data(s.values.map((v, i) => ({ v, i })))
      .join("circle").attr("cx", (d) => at(d.i, d.v)[0]).attr("cy", (d) => at(d.i, d.v)[1])
      .attr("r", 3.5).attr("fill", color).attr("stroke", SURFACE);
    hover(dots, tip, (d) => [data.axes[d.i], `${s.name}: ${fmtNum(d.v)}`]);
  });
  if (data.series.length > 1) legend(svg, data.series.map((s) => s.name), 8, 6);
  return null;
}

function drawGauge(d3, el, columns, rows, spec, { w, h }) {
  const data = singleValue(columns, rows, spec);
  if (!data) return false;
  const max = Math.max(100, 10 ** Math.ceil(Math.log10(Math.abs(data.value) || 1)));
  const svg = svgFor(d3, el, { w, h }, spec.title);
  // A 270° arc reaches r above its centre and r·cos 45° below it.
  const r = Math.max(30, Math.min(w / 2 - 16, (h - 16) / (1 + Math.SQRT1_2)));
  const g = svg.append("g").attr("transform", `translate(${w / 2},${8 + r})`);
  const start = -0.75 * Math.PI, sweep = 1.5 * Math.PI;
  const share = Math.max(0, Math.min(1, data.value / max));
  const arc = d3.arc().innerRadius(r - 14).outerRadius(r).cornerRadius(7);
  g.append("path").attr("d", arc({ startAngle: start, endAngle: start + sweep })).attr("fill", GRID_LINE);
  g.append("path").attr("d", arc({ startAngle: start, endAngle: start + sweep * share })).attr("fill", SERIES[0]);
  g.append("text").attr("text-anchor", "middle").attr("y", -4).attr("fill", INK.primary)
    .attr("font-size", Math.min(34, r / 2.4)).attr("font-weight", 600).text(fmtNum(data.value));
  g.append("text").attr("text-anchor", "middle").attr("y", Math.min(34, r / 2.4)).attr("fill", INK.muted)
    .text(`${spec.title || data.label} · of ${fmtNum(max)}`);
  return null;
}

function drawKpi(d3, el, columns, rows, spec, { w, h }) {
  const data = singleValue(columns, rows, spec);
  if (!data) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const size = Math.max(18, Math.min(56, w / 7, h / 3));
  svg.append("text").attr("x", w / 2).attr("y", h / 2).attr("text-anchor", "middle")
    .attr("fill", INK.primary).attr("font-size", size).attr("font-weight", 600).text(fmtNum(data.value));
  svg.append("text").attr("x", w / 2).attr("y", h / 2 + size * 0.7).attr("text-anchor", "middle")
    .attr("fill", INK.muted).text(spec.title || data.label);
  if (data.rows > 1) {
    svg.append("text").attr("x", w / 2).attr("y", h / 2 + size * 0.7 + 18).attr("text-anchor", "middle")
      .attr("fill", INK.muted).attr("font-size", 10).text(`sum of ${data.rows} rows`);
  }
  return null;
}

function drawHistogram(d3, el, columns, rows, spec, { w, h }) {
  const data = histogramData(columns, rows, spec);
  if (!data) return false;
  const m = { top: 12, right: 16, bottom: 36, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const x = d3.scaleLinear().domain([data.bins[0].x0, data.bins[data.bins.length - 1].x1]).range([0, iw]);
  const y = d3.scaleLinear().domain([0, d3.max(data.bins, (b) => b.count) || 1]).nice().range([ih, 0]);
  gridY(d3, g, y, iw);
  styleAxis(g.append("g").call(d3.axisLeft(y).ticks(5, "~s")));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).ticks(6, "~s")));
  g.append("text").attr("x", iw).attr("y", ih + 32).attr("text-anchor", "end").attr("fill", INK.muted)
    .text(data.value);
  const bars = g.append("g").selectAll("rect").data(data.bins).join("rect")
    .attr("x", (b) => x(b.x0) + 1).attr("width", (b) => Math.max(0, x(b.x1) - x(b.x0) - 2))
    .attr("y", (b) => y(b.count)).attr("height", (b) => ih - y(b.count)).attr("rx", 2).attr("fill", SERIES[0]);
  hover(bars, tip, (b) => [`${fmtNum(b.x0)} – ${fmtNum(b.x1)}`, `count: ${fmtNum(b.count)}`]);
  return null;
}

function drawBoxplot(d3, el, columns, rows, spec, { w, h }) {
  const data = boxplotData(columns, rows, spec);
  if (!data) return false;
  const m = { top: 12, right: 16, bottom: 48, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const x = d3.scaleBand().domain(data.boxes.map((b) => b.name)).range([0, iw]).padding(0.35);
  const all = data.boxes.flatMap((b) => [b.lo, b.hi, ...b.outliers]);
  const y = d3.scaleLinear().domain(d3.extent(all)).nice().range([ih, 0]);
  gridY(d3, g, y, iw);
  styleAxis(g.append("g").call(d3.axisLeft(y).ticks(5, "~s")));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).tickSizeOuter(0)))
    .selectAll(".tick text").attr("text-anchor", "end").attr("transform", "rotate(-30)");
  const box = g.append("g").selectAll("g").data(data.boxes).join("g")
    .attr("transform", (b) => `translate(${x(b.name)},0)`);
  const bw = x.bandwidth(), mid = bw / 2;
  box.append("line").attr("x1", mid).attr("x2", mid).attr("y1", (b) => y(b.lo)).attr("y2", (b) => y(b.hi))
    .attr("stroke", SERIES[0]);
  for (const end of ["lo", "hi"]) {
    box.append("line").attr("x1", bw * 0.3).attr("x2", bw * 0.7).attr("y1", (b) => y(b[end]))
      .attr("y2", (b) => y(b[end])).attr("stroke", SERIES[0]);
  }
  const rects = box.append("rect").attr("width", bw).attr("y", (b) => y(b.q3))
    .attr("height", (b) => Math.max(1, y(b.q1) - y(b.q3))).attr("rx", 3)
    .attr("fill", SERIES[0]).attr("fill-opacity", 0.25).attr("stroke", SERIES[0]).attr("stroke-width", 2);
  box.append("line").attr("x2", bw).attr("y1", (b) => y(b.median)).attr("y2", (b) => y(b.median))
    .attr("stroke", SERIES[0]).attr("stroke-width", 2);
  box.each(function each(b) {
    d3.select(this).selectAll("circle").data(b.outliers).join("circle").attr("cx", mid)
      .attr("cy", (v) => y(v)).attr("r", 3).attr("fill", "none").attr("stroke", SERIES[0]);
  });
  hover(rects, tip, (b) => [b.name, `median ${fmtNum(b.median)} · n ${b.n}`,
                            `quartiles ${fmtNum(b.q1)} – ${fmtNum(b.q3)}`,
                            `whiskers ${fmtNum(b.lo)} – ${fmtNum(b.hi)}`]);
  return null;
}

function drawWaterfall(d3, el, columns, rows, spec, { w, h }) {
  const data = waterfallData(columns, rows, spec);
  if (!data) return false;
  const m = { top: 12, right: 16, bottom: 48, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);
  const tip = tooltip(el);
  const keys = data.steps.map((st, i) => `${i}\u0000${st.name}`);
  const x = d3.scaleBand().domain(keys).range([0, iw]).padding(0.25);
  const ends = data.steps.flatMap((st) => [st.start, st.end]);
  const y = d3.scaleLinear().domain([Math.min(0, d3.min(ends)), Math.max(0, d3.max(ends))]).nice().range([ih, 0]);
  gridY(d3, g, y, iw);
  styleAxis(g.append("g").call(d3.axisLeft(y).ticks(5, "~s")));
  styleAxis(g.append("g").attr("transform", `translate(0,${ih})`)
    .call(d3.axisBottom(x).tickFormat((k) => String(k).split("\u0000")[1].slice(0, 13)).tickSizeOuter(0)))
    .selectAll(".tick text").attr("text-anchor", "end").attr("transform", "rotate(-30)");
  const color = { up: GOOD, down: BAD, total: TOTAL };
  const bars = g.append("g").selectAll("rect").data(data.steps).join("rect")
    .attr("x", (_, i) => x(keys[i])).attr("width", x.bandwidth()).attr("rx", 3)
    .attr("y", (st) => y(Math.max(st.start, st.end)))
    .attr("height", (st) => Math.max(1, Math.abs(y(st.start) - y(st.end))))
    .attr("fill", (st) => color[st.kind]);
  data.steps.slice(0, -1).forEach((st, i) => {
    g.append("line").attr("x1", x(keys[i]) + x.bandwidth()).attr("x2", x(keys[i + 1]))
      .attr("y1", y(st.end)).attr("y2", y(st.end)).attr("stroke", AXIS_LINE).attr("stroke-dasharray", "3,3");
  });
  hover(bars, tip, (st) => [st.name, st.kind === "total" ? `total: ${fmtNum(st.end)}`
    : `${st.delta >= 0 ? "+" : ""}${fmtNum(st.delta)} → ${fmtNum(st.end)}`]);
  return null;
}

/** A layered flow diagram: node columns by longest path, node heights by
 *  flow, links as ribbons whose width is the flow. */
function drawSankey(d3, el, columns, rows, spec, { w, h }) {
  const data = sankeyData(columns, rows, spec);
  if (!data) return false;
  const svg = svgFor(d3, el, { w, h }, spec.title);
  const tip = tooltip(el);
  const nodeW = 12, labelW = Math.min(110, w * 0.18);
  const usable = w - 2 * labelW - nodeW;
  const colX = (layer) => labelW + (data.layers > 1 ? (layer / (data.layers - 1)) * usable : usable / 2);
  const byLayer = d3.groups(data.nodes, (n) => n.layer);
  // Gaps take at most a third of the height, so flows always keep room.
  const most = d3.max(byLayer, ([, ns]) => ns.length);
  const pad = Math.max(1, Math.min(10, (h * 0.33) / Math.max(1, most - 1)));
  const scale = d3.min(byLayer, ([, ns]) => (h - 8 - pad * (ns.length - 1)) / d3.sum(ns, (n) => n.value));
  const pos = new Map();
  for (const [layer, ns] of byLayer) {
    const total = d3.sum(ns, (n) => n.value) * scale + pad * (ns.length - 1);
    let at = (h - total) / 2;
    ns.sort((a, b) => b.value - a.value).forEach((n) => {
      pos.set(n.id, { x: colX(layer), y: at, h: Math.max(1, n.value * scale), out: 0, in: 0 });
      at += n.value * scale + pad;
    });
  }
  const color = new Map(data.nodes.map((n, i) => [n.id, SERIES[i % SERIES.length]]));
  // Each flow is a filled band between two curves — a thick stroke bulges
  // past its ends where a flow drops steeply.
  const ribbons = svg.append("g").selectAll("path").data(data.links).join("path")
    .attr("d", (l) => {
      const s = pos.get(l.source), t = pos.get(l.target), thick = Math.max(1, l.value * scale);
      const a0 = s.y + s.out, b0 = t.y + t.in;
      s.out += thick;
      t.in += thick;
      const x0 = s.x + nodeW, x1 = t.x, xm = (x0 + x1) / 2;
      return `M${x0},${a0}C${xm},${a0} ${xm},${b0} ${x1},${b0}`
        + `L${x1},${b0 + thick}C${xm},${b0 + thick} ${xm},${a0 + thick} ${x0},${a0 + thick}Z`;
    })
    .attr("fill", (l) => color.get(l.source)).attr("fill-opacity", 0.35);
  hover(ribbons, tip, (l) => [`${l.source} → ${l.target}`, `${data.value}: ${fmtNum(l.value)}`]);
  const nodes = svg.append("g").selectAll("rect").data(data.nodes).join("rect")
    .attr("x", (n) => pos.get(n.id).x).attr("y", (n) => pos.get(n.id).y)
    .attr("width", nodeW).attr("height", (n) => pos.get(n.id).h).attr("rx", 2)
    .attr("fill", (n) => color.get(n.id));
  hover(nodes, tip, (n) => [n.id, `${data.value}: ${fmtNum(n.value)}`]);
  svg.append("g").selectAll("text").data(data.nodes).join("text")
    .attr("x", (n) => (n.layer === 0 ? pos.get(n.id).x - 6 : pos.get(n.id).x + nodeW + 6))
    .attr("y", (n) => pos.get(n.id).y + pos.get(n.id).h / 2 + 4)
    .attr("text-anchor", (n) => (n.layer === 0 ? "end" : "start")).attr("fill", INK.secondary)
    .attr("paint-order", "stroke").attr("stroke", SURFACE).attr("stroke-width", 3)
    .attr("pointer-events", "none").text((n) => n.id.slice(0, 18));
  return null;
}

/** The rows as a table, built with D3 like every other view — cells as text. */
function drawTable(d3, el, columns, rows) {
  if (!columns?.length) return false;
  const wrap = d3.select(el).append("div").attr("class", "table-wrap d3-table")
    .style("height", "100%").style("overflow", "auto");
  const table = wrap.append("table");
  table.append("thead").append("tr").selectAll("th").data(columns).join("th").text((c) => c);
  const shown = rows.slice(0, LIMITS.tableRows);
  table.append("tbody").selectAll("tr").data(shown).join("tr")
    .selectAll("td").data((r) => r).join("td")
    .style("text-align", (v) => (typeof v === "number" ? "right" : null))
    .text((v) => (v == null ? "" : typeof v === "number" ? fmtNum(v) : String(v)));
  if (rows.length > shown.length) {
    wrap.append("div").attr("class", "table-more").text(`… ${rows.length - shown.length} more rows`);
  }
  return null;
}

