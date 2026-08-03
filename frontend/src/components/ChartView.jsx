// ECharts renderer covering the Power BI / Tableau-style catalog (22 types),
// with a grouped type picker per chart. Palette + mark rules follow the
// validated dark-surface reference palette: fixed categorical order, 2px lines,
// rounded bar data-ends, ink-colored text, recessive grid, tooltips on.
// If a type's data requirements aren't met, it falls back to the table view.
import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import {
  AXIS_LINE, BAD, GOOD, GRID_LINE, INK, SEQ, SERIES, SURFACE, TOTAL,
} from "./renderers/common";
import PlotlyView, { PLOTLY_TYPES } from "./renderers/PlotlyView";
import VegaView, { VEGA_TYPES } from "./renderers/VegaView";

const ENGINES = [
  ["echarts", "ECharts"],
  ["plotly", "Plotly"],
  ["vega", "Altair"],
];

const TYPE_GROUPS = [
  ["Comparison", ["bar", "hbar", "stacked_bar", "combo"]],
  ["Trend", ["line", "area", "stacked_area"]],
  ["Part-to-whole", ["pie", "donut", "treemap", "funnel"]],
  ["Distribution", ["histogram", "boxplot"]],
  ["Relationship", ["scatter", "bubble", "heatmap", "radar"]],
  ["Flow & bridge", ["waterfall", "sankey"]],
  ["KPI", ["kpi", "gauge"]],
  ["Data", ["table"]],
];
export const LABELS = {
  bar: "Bar", hbar: "Horizontal bar", stacked_bar: "Stacked bar", combo: "Combo (bar+line)",
  line: "Line", area: "Area", stacked_area: "Stacked area",
  pie: "Pie", donut: "Donut", treemap: "Treemap", funnel: "Funnel",
  histogram: "Histogram", boxplot: "Box plot",
  scatter: "Scatter", bubble: "Bubble", heatmap: "Heatmap", radar: "Radar",
  waterfall: "Waterfall", sankey: "Sankey",
  kpi: "KPI card", gauge: "Gauge", table: "Table",
};


// ── Data-shape suitability: which chart types fit the current result ────
// Prevents forcing e.g. 50 unique IDs into a sankey (unreadable spaghetti).
function colKinds(columns, rows) {
  const first = rows[0] || [];
  return columns.map((c, i) => (typeof first[i] === "number" ? "num" : "cat"));
}
function distinct(rows, i) {
  return new Set(rows.map((r) => String(r[i]))).size;
}
export function computeFit(columns, rows, spec) {
  const kinds = colKinds(columns, rows);
  const numCount = kinds.filter((k) => k === "num").length;
  const xi = spec ? columns.indexOf(spec.x) : -1;
  const yis = spec ? (spec.y || []).map((c) => columns.indexOf(c)).filter((i) => i >= 0) : [];
  const dx = xi >= 0 ? distinct(rows, xi) : rows.length;
  const numericY = yis.some((i) => kinds[i] === "num") || numCount > 0;
  const ok = (v, reason) => (v ? { ok: true } : { ok: false, reason });

  const catValue = (max, what) =>
    xi < 0 || !numericY
      ? { ok: false, reason: `needs a category column + numeric value` }
      : dx > max
      ? { ok: false, reason: `too many categories for a ${what} (${dx} > ${max})` }
      : { ok: true };

  const fit = {
    table: { ok: true },
    kpi: ok(numCount > 0, "needs a numeric column"),
    gauge: ok(numCount > 0, "needs a numeric value"),
    histogram: ok(numCount > 0, "needs a numeric column to bin"),
    bar: ok(xi >= 0 && numericY, "needs a category column + numeric value"),
    hbar: ok(xi >= 0 && numericY, "needs a category column + numeric value"),
    stacked_bar: ok(xi >= 0 && numericY, "needs a category column + numeric value"),
    combo: ok(xi >= 0 && yis.length >= 2, "needs 2+ numeric columns"),
    line: ok(xi >= 0 && numericY, "needs an x column + numeric value"),
    area: ok(xi >= 0 && numericY, "needs an x column + numeric value"),
    stacked_area: ok(xi >= 0 && yis.length >= 2, "needs 2+ numeric series to stack"),
    pie: catValue(12, "pie"),
    donut: catValue(12, "donut"),
    funnel: catValue(10, "funnel"),
    treemap: catValue(40, "treemap"),
    radar: rows.length > 12
      ? { ok: false, reason: `too many rows for a radar (${rows.length} > 12)` }
      : ok(xi >= 0 && numericY, "needs a category column + numeric value"),
    scatter: ok(numericY, "needs a numeric y column"),
    bubble: yis.filter((i) => kinds[i] === "num").length >= 2
      ? { ok: true }
      : { ok: false, reason: "needs 2 numeric columns (value + size)" },
    boxplot: xi >= 0 && dx <= 20 && numericY && rows.length / Math.max(dx, 1) >= 2
      ? { ok: true }
      : { ok: false, reason: "needs groups (\u226420) with several numeric values each" },
    waterfall: rows.length <= 20 && xi >= 0 && numericY
      ? { ok: true }
      : { ok: false, reason: "needs \u226420 stages with numeric deltas" },
    sankey: (() => {
      if (xi < 0 || yis.length < 1) return { ok: false, reason: "needs source, target + value columns" };
      const ti = yis[0];
      if (kinds[ti] === "num") return { ok: false, reason: "target column must be a category, not a number" };
      const nodes = new Set([...rows.map((r) => String(r[xi])), ...rows.map((r) => String(r[ti]))]).size;
      if (nodes > 30) return { ok: false, reason: `too many distinct nodes for a sankey (${nodes} > 30)` };
      const vi = yis.length > 1 ? yis[1] : columns.findIndex((_, i) => kinds[i] === "num" && i !== xi && i !== ti);
      if (vi < 0) return { ok: false, reason: "needs a numeric flow value column" };
      return { ok: true };
    })(),
    heatmap: (() => {
      if (xi < 0 || yis.length < 1) return { ok: false, reason: "needs 2 category columns + a value" };
      const ryi = yis[0];
      if (distinct(rows, xi) > 40 || distinct(rows, ryi) > 30)
        return { ok: false, reason: "too many distinct categories for a heatmap" };
      return { ok: true };
    })(),
  };
  return fit;
}

export default function ChartView({ columns, rows, chart, tall, plain }) {
  const [type, setType] = useState(chart?.type || "table");
  useEffect(() => setType(chart?.type || "table"), [chart]);

  const [engine, setEngine] = useState(localStorage.getItem("studio_renderer") || "echarts");

  const spec = useMemo(() => (chart ? { ...chart, type } : null), [chart, type]);

  if (!columns?.length || !rows?.length) return null;

  const fit = computeFit(columns, rows, spec);
  const typeOk = fit[type]?.ok !== false;
  // The chosen engine draws the chart when it supports the type; otherwise
  // ECharts (which covers the full catalog) steps in.
  const drawEngine =
    engine === "plotly" && PLOTLY_TYPES.has(type) ? "plotly"
    : engine === "vega" && VEGA_TYPES.has(type) ? "vega"
    : "echarts";
  const plottable = typeOk && spec && type !== "table" && type !== "kpi";
  const option = plottable && drawEngine === "echarts" ? buildOption(columns, rows, spec) : null;

  function pickEngine(e) {
    setEngine(e);
    localStorage.setItem("studio_renderer", e);
  }

  return (
    <div className="chartview">
      {!plain && (
      <div className="chart-toolbar">
        <span className="chart-title">{chart?.title || "Result"}</span>
        <span className="toolbar-selects">
        <select
          className="type-select"
          value={engine}
          onChange={(e) => pickEngine(e.target.value)}
          title="Render engine — ECharts steps in for types the engine lacks"
        >
          {ENGINES.map(([v, l]) => (
            <option key={v} value={v}>{l}</option>
          ))}
        </select>
        <select className="type-select" value={type} onChange={(e) => setType(e.target.value)}>
          {TYPE_GROUPS.map(([g, ts]) => (
            <optgroup key={g} label={g}>
              {ts.map((t) => (
                <option key={t} value={t} title={fit[t]?.reason}>
                  {LABELS[t]}{fit[t]?.ok === false ? " \u26a0" : ""}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        </span>
      </div>
      )}
      {!typeOk && (
        <div className="chart-note">
          {LABELS[type]} doesn’t fit this data — {fit[type]?.reason}. Showing the table;
          pick a compatible type or reshape the data with a prompt.
        </div>
      )}
      {plottable && engine !== "echarts" && drawEngine === "echarts" && (
        <div className="chart-note">
          {ENGINES.find(([v]) => v === engine)?.[1]} doesn’t support {LABELS[type]} — rendered
          with ECharts instead.
        </div>
      )}
      {typeOk && type === "kpi" && spec ? (
        <KpiCard columns={columns} rows={rows} spec={spec} />
      ) : plottable && drawEngine === "plotly" ? (
        <PlotlyView columns={columns} rows={rows} spec={spec} tall={tall} />
      ) : plottable && drawEngine === "vega" ? (
        <VegaView columns={columns} rows={rows} spec={spec} tall={tall} />
      ) : option ? (
        <Echart option={option} tall={tall} />
      ) : (
        <DataTable columns={columns} rows={rows} />
      )}
    </div>
  );
}

function KpiCard({ columns, rows, spec }) {
  const yi = columns.indexOf(spec.y?.[0]) >= 0 ? columns.indexOf(spec.y[0]) : firstNumericIndex(columns, rows);
  if (yi < 0) return <DataTable columns={columns} rows={rows} />;
  const value = rows.length === 1 ? rows[0][yi] : rows.reduce((a, r) => a + (Number(r[yi]) || 0), 0);
  return (
    <div className="kpi-card">
      <div className="kpi-value">{formatNum(value)}</div>
      <div className="kpi-label">{spec.title || columns[yi]}</div>
      {rows.length > 1 && <div className="kpi-sub">sum of {rows.length} rows</div>}
    </div>
  );
}

function DataTable({ columns, rows }) {
  const shown = rows.slice(0, 50);
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i}>{r.map((v, j) => <td key={j}>{fmt(v)}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && (
        <div className="table-more">… {rows.length - shown.length} more rows</div>
      )}
    </div>
  );
}

function Echart({ option, tall }) {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || !option) return;
    const inst = echarts.init(el, null, { renderer: "canvas" });
    try {
      inst.setOption(option);
    } catch (e) {
      inst.dispose();
      el.innerHTML =
        '<div class="chart-note">This data cannot be drawn as this chart type (' +
        String(e.message || e).slice(0, 120) +
        '). Pick another type.</div>';
      return;
    }
    const onResize = () => inst.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      inst.dispose();
    };
  }, [option]);
  return <div ref={ref} className={"echart" + (tall ? " echart-tall" : "")} />;
}

// ── Option builders ─────────────────────────────────────────────────────

function buildOption(columns, rows, spec) {
  const xi = columns.indexOf(spec.x);
  let yCols = (spec.y || []).filter((c) => columns.includes(c));
  if (!yCols.length) {
    const ni = firstNumericIndex(columns, rows, xi);
    if (ni >= 0) yCols = [columns[ni]];
  }
  const yis = yCols.map((c) => columns.indexOf(c));
  const xs = xi >= 0 ? rows.map((r) => r[xi]) : rows.map((_, i) => i + 1);

  const base = {
    backgroundColor: "transparent",
    textStyle: { color: INK.secondary, fontSize: 12 },
    color: SERIES,
    tooltip: {
      backgroundColor: SURFACE, borderColor: AXIS_LINE,
      textStyle: { color: INK.primary, fontSize: 12 },
    },
    legend:
      yCols.length > 1
        ? { top: 0, textStyle: { color: INK.secondary }, icon: "circle" }
        : { show: false },
    grid: { left: 8, right: 16, top: yCols.length > 1 ? 32 : 16, bottom: 8, containLabel: true },
  };
  const catAxis = (data) => ({
    type: "category", data,
    axisLine: { lineStyle: { color: AXIS_LINE } }, axisTick: { show: false },
    axisLabel: { color: INK.muted, hideOverlap: true },
  });

  try {
    switch (spec.type) {
      case "bar":
      case "stacked_bar":
      case "line":
      case "area":
      case "stacked_area":
      case "combo": {
        if (xi < 0 || !yis.length) return null;
        const stacked = spec.type.startsWith("stacked");
        const series = yis.map((idx, s) => {
          const data = rows.map((r) => r[idx]);
          const isBar =
            spec.type === "bar" || spec.type === "stacked_bar" ||
            (spec.type === "combo" && s === 0);
          if (isBar) {
            return {
              name: yCols[s], type: "bar", data, barMaxWidth: 28, barGap: "12%",
              stack: stacked ? "total" : undefined,
              colorBy: yis.length === 1 ? "data" : "series",
              itemStyle: stacked
                ? { borderColor: SURFACE, borderWidth: 1 }
                : { borderRadius: [4, 4, 0, 0] },
            };
          }
          return {
            name: yCols[s], type: "line", data, smooth: false, showSymbol: false,
            sampling: data.length > 2000 ? "lttb" : undefined,
            lineStyle: { width: 2 },
            stack: spec.type === "stacked_area" ? "total" : undefined,
            areaStyle: spec.type.includes("area") ? { opacity: 0.18 } : undefined,
          };
        });
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "axis",
            axisPointer: spec.type.includes("bar") || spec.type === "combo"
              ? { type: "shadow" } : { type: "line", lineStyle: { color: AXIS_LINE } } },
          xAxis: catAxis(xs), yAxis: numAxis(), series,
        };
      }

      case "hbar": {
        if (xi < 0 || !yis.length) return null;
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
          xAxis: numAxis(), yAxis: { ...catAxis(xs), inverse: true },
          series: yis.map((idx, s) => ({
            name: yCols[s], type: "bar", data: rows.map((r) => r[idx]),
            barMaxWidth: 22, colorBy: yis.length === 1 ? "data" : "series",
            itemStyle: { borderRadius: [0, 4, 4, 0] },
          })),
        };
      }

      case "pie":
      case "donut": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (xi < 0 || vi < 0) return null;
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          legend: { top: 0, textStyle: { color: INK.secondary }, icon: "circle" },
          series: [{
            type: "pie",
            radius: spec.type === "donut" ? ["45%", "72%"] : "72%",
            top: 24,
            itemStyle: { borderColor: SURFACE, borderWidth: 2 },
            label: { color: INK.secondary },
            data: rows.map((r) => ({ name: String(r[xi]), value: r[vi] })),
          }],
        };
      }

      case "treemap": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (xi < 0 || vi < 0) return null;
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          series: [{
            type: "treemap", roam: false, nodeClick: false,
            breadcrumb: { show: false },
            itemStyle: { borderColor: SURFACE, borderWidth: 2, gapWidth: 2 },
            label: { color: "#fff" },
            data: rows.map((r) => ({ name: String(r[xi]), value: Number(r[vi]) || 0 })),
          }],
        };
      }

      case "funnel": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (xi < 0 || vi < 0) return null;
        const data = rows
          .map((r) => ({ name: String(r[xi]), value: Number(r[vi]) || 0 }))
          .sort((a, b) => b.value - a.value);
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          series: [{
            type: "funnel", top: 10, bottom: 10, gap: 2,
            itemStyle: { borderColor: SURFACE, borderWidth: 2 },
            label: { color: INK.secondary }, data,
          }],
        };
      }

      case "scatter":
      case "bubble": {
        if (xi < 0 || !yis.length) return null;
        const si = spec.type === "bubble" && yis.length > 1 ? yis[1] : null;
        const sizes = si != null ? rows.map((r) => Number(r[si]) || 0) : null;
        const maxS = sizes ? Math.max(...sizes, 1) : 1;
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          xAxis: isNumericCol(rows, xi) ? numAxis(spec.x) : catAxis(xs),
          yAxis: numAxis(yCols[0]),
          series: [{
            name: yCols[0], type: "scatter",
            large: rows.length > 2000,
            symbolSize: si != null
              ? (val) => 8 + 32 * Math.sqrt((val[2] || 0) / maxS)
              : 9,
            itemStyle: { borderColor: SURFACE, borderWidth: 1, opacity: 0.85 },
            data: rows.map((r) =>
              si != null ? [r[xi], r[yis[0]], r[si]] : [r[xi], r[yis[0]]]
            ),
          }],
        };
      }

      case "heatmap": {
        // x category, y[0] = row category, y[1] (or auto) = value
        if (xi < 0 || yis.length < 1) return null;
        const ryi = yis[0];
        const vi = yis.length > 1 ? yis[1] : firstNumericIndex(columns, rows, xi, ryi);
        if (vi < 0) return null;
        const xCats = [...new Set(rows.map((r) => String(r[xi])))];
        const yCats = [...new Set(rows.map((r) => String(r[ryi])))];
        const vals = rows.map((r) => Number(r[vi]) || 0);
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          grid: { ...base.grid, top: 16, bottom: 40 },
          xAxis: catAxis(xCats),
          yAxis: { ...catAxis(yCats), type: "category", data: yCats },
          visualMap: {
            min: Math.min(...vals), max: Math.max(...vals),
            calculable: false, orient: "horizontal", left: "center", bottom: 0,
            inRange: { color: SEQ }, textStyle: { color: INK.muted },
          },
          series: [{
            type: "heatmap",
            data: rows.map((r) => [String(r[xi]), String(r[ryi]), Number(r[vi]) || 0]),
            itemStyle: { borderColor: SURFACE, borderWidth: 2 },
            label: { show: xCats.length * yCats.length <= 60, color: "#fff" },
          }],
        };
      }

      case "radar": {
        if (xi < 0 || !yis.length) return null;
        const cats = rows;
        const maxes = yis.map((idx) => Math.max(...cats.map((r) => Number(r[idx]) || 0)) * 1.1 || 1);
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          legend: { top: 0, textStyle: { color: INK.secondary }, icon: "circle", show: yis.length > 1 },
          radar: {
            indicator: cats.map((r) => ({ name: String(r[xi]), max: Math.max(...maxes) })),
            splitLine: { lineStyle: { color: GRID_LINE } },
            splitArea: { show: false },
            axisLine: { lineStyle: { color: AXIS_LINE } },
            axisName: { color: INK.muted },
          },
          series: [{
            type: "radar",
            data: yis.map((idx, s) => ({
              name: yCols[s],
              value: cats.map((r) => Number(r[idx]) || 0),
              lineStyle: { width: 2 },
              areaStyle: { opacity: 0.12 },
            })),
          }],
        };
      }

      case "gauge": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (vi < 0) return null;
        const value = Number(rows[0][vi]) || 0;
        const max = Math.max(100, Math.pow(10, Math.ceil(Math.log10(Math.abs(value) || 1))));
        return {
          ...base,
          series: [{
            type: "gauge", max,
            progress: { show: true, width: 12, itemStyle: { color: SERIES[0] } },
            axisLine: { lineStyle: { width: 12, color: [[1, GRID_LINE]] } },
            axisTick: { show: false }, splitLine: { show: false },
            axisLabel: { color: INK.muted, distance: 18, fontSize: 10 },
            pointer: { show: false },
            anchor: { show: false },
            detail: { valueAnimation: true, color: INK.primary, fontSize: 28, offsetCenter: [0, "8%"], formatter: (v) => formatNum(v) },
            title: { color: INK.muted, offsetCenter: [0, "38%"] },
            data: [{ value, name: spec.y?.[0] || columns[vi] }],
          }],
        };
      }

      case "histogram": {
        const vi = yis[0] ?? (isNumericCol(rows, xi) ? xi : firstNumericIndex(columns, rows));
        if (vi < 0) return null;
        const vals = rows.map((r) => Number(r[vi])).filter((v) => !isNaN(v));
        if (!vals.length) return null;
        const lo = Math.min(...vals), hi = Math.max(...vals);
        const bins = Math.min(20, Math.max(6, Math.round(Math.sqrt(vals.length))));
        const w = (hi - lo) / bins || 1;
        const counts = Array(bins).fill(0);
        vals.forEach((v) => counts[Math.min(bins - 1, Math.floor((v - lo) / w))]++);
        const labels = counts.map((_, i) => formatNum(lo + i * w));
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
          xAxis: { ...catAxis(labels), name: columns[vi], nameTextStyle: { color: INK.muted } },
          yAxis: numAxis("count"),
          series: [{ type: "bar", data: counts, barCategoryGap: "2%", itemStyle: { borderRadius: [4, 4, 0, 0], borderColor: SURFACE, borderWidth: 1 } }],
        };
      }

      case "boxplot": {
        if (xi < 0 || !yis.length) return null;
        const groups = new Map();
        rows.forEach((r) => {
          const k = String(r[xi]);
          const v = Number(r[yis[0]]);
          if (!isNaN(v)) (groups.get(k) || groups.set(k, []).get(k)).push(v);
        });
        const cats = [...groups.keys()];
        const data = cats.map((k) => quartiles(groups.get(k)));
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          xAxis: catAxis(cats), yAxis: numAxis(yCols[0]),
          series: [{
            type: "boxplot", data,
            itemStyle: { color: "rgba(57,135,229,0.25)", borderColor: SERIES[0], borderWidth: 2 },
          }],
        };
      }

      case "waterfall": {
        if (xi < 0 || !yis.length) return null;
        const deltas = rows.map((r) => Number(r[yis[0]]) || 0);
        let run = 0;
        const bases = [], vals = [], colors = [];
        deltas.forEach((d) => {
          bases.push(d >= 0 ? run : run + d);
          vals.push(Math.abs(d));
          colors.push(d >= 0 ? GOOD : BAD);
          run += d;
        });
        const labels = rows.map((r) => String(r[xi])).concat("total");
        bases.push(0); vals.push(run); colors.push(TOTAL);
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "axis", axisPointer: { type: "shadow" },
            formatter: (ps) => {
              const i = ps[0].dataIndex;
              const v = i < deltas.length ? deltas[i] : run;
              return `${labels[i]}: ${formatNum(v)}`;
            } },
          xAxis: catAxis(labels), yAxis: numAxis(),
          series: [
            { type: "bar", stack: "wf", data: bases, itemStyle: { color: "transparent" }, tooltip: { show: false }, barMaxWidth: 28 },
            { type: "bar", stack: "wf", data: vals.map((v, i) => ({ value: v, itemStyle: { color: colors[i], borderRadius: 3 } })), barMaxWidth: 28 },
          ],
        };
      }

      case "sankey": {
        // x = source, y[0] = target, y[1] (or auto) = value
        if (xi < 0 || !yis.length) return null;
        const ti = yis[0];
        const vi = yis.length > 1 ? yis[1] : firstNumericIndex(columns, rows, xi, ti);
        if (vi < 0) return null;
        const nodes = [...new Set(rows.flatMap((r) => [String(r[xi]), String(r[ti])]))].map((name) => ({ name }));
        return {
          ...base,
          tooltip: { ...base.tooltip, trigger: "item" },
          series: [{
            type: "sankey", nodeGap: 12,
            emphasis: { focus: "adjacency" },
            lineStyle: { color: "gradient", opacity: 0.35 },
            label: { color: INK.secondary },
            data: nodes,
            links: rows
              .filter((r) => String(r[xi]) !== String(r[ti]))
              .map((r) => ({
                source: String(r[xi]), target: String(r[ti]), value: Number(r[vi]) || 0,
              })),
          }],
        };
      }

      default:
        return null;
    }
  } catch {
    return null; // any construction error → table fallback
  }
}

// ── helpers ─────────────────────────────────────────────────────────────

function numAxis(name) {
  return {
    type: "value", name: name || undefined,
    nameTextStyle: { color: INK.muted },
    axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: INK.muted },
    splitLine: { lineStyle: { color: GRID_LINE } },
  };
}

function firstNumericIndex(columns, rows, ...exclude) {
  const first = rows[0] || [];
  for (let i = 0; i < columns.length; i++) {
    if (exclude.includes(i)) continue;
    if (typeof first[i] === "number") return i;
  }
  return -1;
}

function isNumericCol(rows, i) {
  return typeof (rows[0] || [])[i] === "number";
}

function quartiles(vals) {
  const s = [...vals].sort((a, b) => a - b);
  const q = (p) => {
    const idx = (s.length - 1) * p;
    const lo = Math.floor(idx), hi = Math.ceil(idx);
    return s[lo] + (s[hi] - s[lo]) * (idx - lo);
  };
  return [s[0], q(0.25), q(0.5), q(0.75), s[s.length - 1]];
}

function formatNum(v) {
  const n = Number(v);
  if (isNaN(n)) return String(v);
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

function fmt(v) {
  if (typeof v === "number" && !Number.isInteger(v)) return v.toFixed(2);
  return String(v ?? "");
}
