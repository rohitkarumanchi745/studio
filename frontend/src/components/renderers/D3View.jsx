// D3 renderer — hand-built SVG for the core chart types, plus the chart types
// only D3 draws here: a force-directed network, a beeswarm, a calendar heatmap
// and circle packing (D3_ONLY). Same validated palette as every other engine.
//
// d3 is loaded on demand (dynamic import) so the main bundle stays light. The
// data builders (networkData, beeswarmData, calendarData, packData) are pure —
// rows in, plain objects out — so the shapes the charts draw are tested
// without a DOM. Values come straight from a warehouse: they are written with
// .text()/textContent only, never as HTML.
import { useEffect, useRef, useState } from "react";
import {
  AXIS_LINE, GRID_LINE, INK, SEQ, SERIES, SURFACE,
  firstNumericIndex, isNumericCol, resolveSpec,
} from "./common";

/** Chart types no other engine draws: these always render with D3. */
export const D3_ONLY = new Set(["network", "beeswarm", "calendar", "circle_pack"]);
export const D3_TYPES = new Set([
  "bar", "hbar", "stacked_bar", "line", "area", "stacked_area",
  "pie", "donut", "scatter", "bubble", "heatmap",
  ...D3_ONLY,
]);
// What the D3 & artifacts screens draw: D3 without the line chart (backend
// agent.VISUAL_CHART_TYPES). A line chart there becomes an area.
export const VISUAL_TYPES = new Set([...D3_TYPES].filter((t) => t !== "line"));
export const visualType = (t) => (VISUAL_TYPES.has(t) ? t : t === "line" ? "area" : "bar");

// Size limits, shared with ChartView's fit rules so a chart is only offered
// when it will stay readable (and a force layout stays fast).
export const LIMITS = { networkNodes: 300, networkLinks: 2000, beeswarm: 2000,
                        calendarDays: 1100, packLeaves: 400 };

let _d3 = null;
function getD3() {
  if (!_d3) _d3 = import("d3");
  return _d3;
}

export default function D3View({ columns, rows, spec, tall }) {
  const ref = useRef(null);
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
        el.replaceChildren();
        try {
          stop = draw(d3, el, columns, rows, spec, size);
          if (stop === false) setErr("This data cannot be drawn as this chart type in D3.");
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
      <div ref={ref} className={"echart d3-chart" + (tall ? " echart-tall" : "")}
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
    default: return false;
  }
}

function legend(svg, names, x, y) {
  const g = svg.append("g").attr("transform", `translate(${x},${y})`);
  let at = 0;
  names.forEach((n, i) => {
    const item = g.append("g").attr("transform", `translate(${at},0)`);
    item.append("rect").attr("width", 10).attr("height", 10).attr("rx", 2)
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
