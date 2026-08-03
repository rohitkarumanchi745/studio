// Plotly renderer — same chart specs, same validated palette, drawn by
// plotly.js. Loaded on demand (dynamic import) so the main bundle stays light.
import { useEffect, useRef, useState } from "react";
import {
  AXIS_LINE, BAD, GOOD, GRID_LINE, INK, SEQ, SERIES, SURFACE, TOTAL,
  firstNumericIndex, isNumericCol, resolveSpec,
} from "./common";

export const PLOTLY_TYPES = new Set([
  "bar", "hbar", "stacked_bar", "combo", "line", "area", "stacked_area",
  "pie", "donut", "treemap", "funnel", "histogram", "boxplot",
  "scatter", "bubble", "heatmap", "radar", "waterfall", "sankey", "gauge",
]);

let _plotly = null;
function getPlotly() {
  if (!_plotly) _plotly = import("plotly.js-dist-min").then((m) => m.default || m);
  return _plotly;
}

export default function PlotlyView({ columns, rows, spec, tall }) {
  const ref = useRef(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let dead = false;
    setErr("");
    getPlotly()
      .then((Plotly) => {
        if (dead) return;
        const fig = buildFigure(columns, rows, spec);
        if (!fig) {
          setErr("This data cannot be drawn as this chart type in Plotly.");
          return;
        }
        Plotly.react(el, fig.data, fig.layout, { displayModeBar: false, responsive: true });
      })
      .catch((e) => setErr("Plotly failed to load: " + String(e.message || e).slice(0, 120)));
    return () => {
      dead = true;
      getPlotly().then((Plotly) => Plotly.purge(el)).catch(() => {});
    };
  }, [columns, rows, spec]);

  if (err) return <div className="chart-note">{err}</div>;
  return <div ref={ref} className={"echart" + (tall ? " echart-tall" : "")} />;
}

function baseLayout(showLegend) {
  return {
    paper_bgcolor: "transparent",
    plot_bgcolor: "transparent",
    font: { color: INK.secondary, size: 12 },
    colorway: SERIES,
    margin: { l: 48, r: 16, t: showLegend ? 36 : 16, b: 40 },
    showlegend: showLegend,
    legend: { orientation: "h", y: 1.15, font: { color: INK.secondary } },
    hoverlabel: { bgcolor: SURFACE, bordercolor: AXIS_LINE, font: { color: INK.primary } },
    xaxis: axis(),
    yaxis: axis(),
  };
}
function axis() {
  return {
    gridcolor: GRID_LINE, linecolor: AXIS_LINE, tickcolor: AXIS_LINE,
    tickfont: { color: INK.muted }, zeroline: false, automargin: true,
  };
}

function buildFigure(columns, rows, spec) {
  const { xi, yCols, yis, xs } = resolveSpec(columns, rows, spec);
  const multi = yCols.length > 1;

  try {
    switch (spec.type) {
      case "bar":
      case "hbar":
      case "stacked_bar":
      case "combo": {
        if (xi < 0 || !yis.length) return null;
        const stacked = spec.type === "stacked_bar";
        const horiz = spec.type === "hbar";
        const data = yis.map((idx, s) => {
          const vals = rows.map((r) => r[idx]);
          const isLine = spec.type === "combo" && s > 0;
          if (isLine) {
            return { type: "scatter", mode: "lines", name: yCols[s],
                     x: xs, y: vals, line: { width: 2, color: SERIES[s % SERIES.length] } };
          }
          return {
            type: "bar", name: yCols[s],
            x: horiz ? vals : xs, y: horiz ? xs : vals,
            orientation: horiz ? "h" : "v",
            marker: {
              color: multi ? SERIES[s % SERIES.length] : xs.map((_, i) => SERIES[i % SERIES.length]),
              line: { color: SURFACE, width: 1 },
            },
          };
        });
        const layout = baseLayout(multi);
        layout.barmode = stacked ? "stack" : "group";
        if (horiz) layout.yaxis.autorange = "reversed";
        return { data, layout };
      }

      case "line":
      case "area":
      case "stacked_area": {
        if (xi < 0 || !yis.length) return null;
        const data = yis.map((idx, s) => ({
          type: "scatter", mode: "lines", name: yCols[s], x: xs,
          y: rows.map((r) => r[idx]),
          line: { width: 2, color: SERIES[s % SERIES.length] },
          fill: spec.type === "area" ? "tozeroy" : undefined,
          stackgroup: spec.type === "stacked_area" ? "one" : undefined,
          opacity: spec.type === "line" ? 1 : 0.85,
        }));
        return { data, layout: baseLayout(multi) };
      }

      case "pie":
      case "donut": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (xi < 0 || vi < 0) return null;
        return {
          data: [{
            type: "pie",
            labels: rows.map((r) => String(r[xi])),
            values: rows.map((r) => Number(r[vi]) || 0),
            hole: spec.type === "donut" ? 0.55 : 0,
            marker: { colors: SERIES, line: { color: SURFACE, width: 2 } },
            textfont: { color: INK.primary },
          }],
          layout: { ...baseLayout(true), legend: { orientation: "v", font: { color: INK.secondary } } },
        };
      }

      case "treemap": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (xi < 0 || vi < 0) return null;
        const labels = rows.map((r) => String(r[xi]));
        return {
          data: [{
            type: "treemap", labels, parents: labels.map(() => ""),
            values: rows.map((r) => Number(r[vi]) || 0),
            marker: { colors: labels.map((_, i) => SERIES[i % SERIES.length]), line: { color: SURFACE, width: 2 } },
            textfont: { color: "#fff" },
          }],
          layout: baseLayout(false),
        };
      }

      case "funnel": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (xi < 0 || vi < 0) return null;
        const sorted = rows
          .map((r) => [String(r[xi]), Number(r[vi]) || 0])
          .sort((a, b) => b[1] - a[1]);
        return {
          data: [{
            type: "funnel", y: sorted.map((r) => r[0]), x: sorted.map((r) => r[1]),
            marker: { color: sorted.map((_, i) => SERIES[i % SERIES.length]), line: { color: SURFACE, width: 2 } },
            textfont: { color: INK.primary }, connector: { line: { color: AXIS_LINE } },
          }],
          layout: baseLayout(false),
        };
      }

      case "histogram": {
        const vi = yis[0] ?? (isNumericCol(rows, xi) ? xi : firstNumericIndex(columns, rows));
        if (vi < 0) return null;
        return {
          data: [{
            type: "histogram", x: rows.map((r) => Number(r[vi])).filter((v) => !isNaN(v)),
            marker: { color: SERIES[0], line: { color: SURFACE, width: 1 } },
          }],
          layout: baseLayout(false),
        };
      }

      case "boxplot": {
        if (xi < 0 || !yis.length) return null;
        return {
          data: [{
            type: "box",
            x: rows.map((r) => String(r[xi])),
            y: rows.map((r) => Number(r[yis[0]])),
            marker: { color: SERIES[0] }, line: { color: SERIES[0], width: 2 },
            fillcolor: "rgba(57,135,229,0.25)",
          }],
          layout: baseLayout(false),
        };
      }

      case "scatter":
      case "bubble": {
        if (xi < 0 || !yis.length) return null;
        const si = spec.type === "bubble" && yis.length > 1 ? yis[1] : null;
        const sizes = si != null ? rows.map((r) => Number(r[si]) || 0) : null;
        const maxS = sizes ? Math.max(...sizes, 1) : 1;
        return {
          data: [{
            type: "scatter", mode: "markers", name: yCols[0],
            x: xs, y: rows.map((r) => r[yis[0]]),
            marker: {
              color: SERIES[0], opacity: 0.85,
              line: { color: SURFACE, width: 1 },
              size: sizes ? sizes.map((v) => 8 + 32 * Math.sqrt(v / maxS)) : 9,
            },
          }],
          layout: baseLayout(false),
        };
      }

      case "heatmap": {
        if (xi < 0 || yis.length < 1) return null;
        const ryi = yis[0];
        const vi = yis.length > 1 ? yis[1] : firstNumericIndex(columns, rows, xi, ryi);
        if (vi < 0) return null;
        const xCats = [...new Set(rows.map((r) => String(r[xi])))];
        const yCats = [...new Set(rows.map((r) => String(r[ryi])))];
        const z = yCats.map(() => xCats.map(() => null));
        rows.forEach((r) => {
          z[yCats.indexOf(String(r[ryi]))][xCats.indexOf(String(r[xi]))] = Number(r[vi]) || 0;
        });
        return {
          data: [{
            type: "heatmap", x: xCats, y: yCats, z,
            colorscale: SEQ.map((c, i) => [i / (SEQ.length - 1), c]),
            xgap: 2, ygap: 2,
            colorbar: { tickfont: { color: INK.muted }, outlinecolor: AXIS_LINE },
          }],
          layout: baseLayout(false),
        };
      }

      case "radar": {
        if (xi < 0 || !yis.length) return null;
        const theta = rows.map((r) => String(r[xi]));
        return {
          data: yis.map((idx, s) => ({
            type: "scatterpolar", name: yCols[s], fill: "toself",
            r: rows.map((r) => Number(r[idx]) || 0),
            theta, opacity: 0.85, line: { width: 2, color: SERIES[s % SERIES.length] },
          })),
          layout: {
            ...baseLayout(multi),
            polar: {
              bgcolor: "transparent",
              radialaxis: { gridcolor: GRID_LINE, tickfont: { color: INK.muted } },
              angularaxis: { gridcolor: GRID_LINE, tickfont: { color: INK.muted }, linecolor: AXIS_LINE },
            },
          },
        };
      }

      case "waterfall": {
        if (xi < 0 || !yis.length) return null;
        const labels = rows.map((r) => String(r[xi])).concat("total");
        const deltas = rows.map((r) => Number(r[yis[0]]) || 0).concat(0);
        return {
          data: [{
            type: "waterfall", x: labels, y: deltas,
            measure: rows.map(() => "relative").concat("total"),
            increasing: { marker: { color: GOOD } },
            decreasing: { marker: { color: BAD } },
            totals: { marker: { color: TOTAL } },
            connector: { line: { color: AXIS_LINE, width: 1 } },
          }],
          layout: baseLayout(false),
        };
      }

      case "sankey": {
        if (xi < 0 || !yis.length) return null;
        const ti = yis[0];
        const vi = yis.length > 1 ? yis[1] : firstNumericIndex(columns, rows, xi, ti);
        if (vi < 0) return null;
        const nodes = [...new Set(rows.flatMap((r) => [String(r[xi]), String(r[ti])]))];
        const links = rows.filter((r) => String(r[xi]) !== String(r[ti]));
        return {
          data: [{
            type: "sankey",
            node: {
              label: nodes, pad: 12, thickness: 14,
              color: nodes.map((_, i) => SERIES[i % SERIES.length]),
              line: { color: SURFACE, width: 1 },
            },
            link: {
              source: links.map((r) => nodes.indexOf(String(r[xi]))),
              target: links.map((r) => nodes.indexOf(String(r[ti]))),
              value: links.map((r) => Number(r[vi]) || 0),
              color: "rgba(137,135,129,0.25)",
            },
          }],
          layout: baseLayout(false),
        };
      }

      case "gauge": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (vi < 0) return null;
        const value = Number(rows[0][vi]) || 0;
        const max = Math.max(100, Math.pow(10, Math.ceil(Math.log10(Math.abs(value) || 1))));
        return {
          data: [{
            type: "indicator", mode: "gauge+number", value,
            number: { font: { color: INK.primary } },
            title: { text: spec.y?.[0] || columns[vi], font: { color: INK.muted, size: 13 } },
            gauge: {
              axis: { range: [0, max], tickcolor: AXIS_LINE, tickfont: { color: INK.muted } },
              bar: { color: SERIES[0], thickness: 0.35 },
              bgcolor: "transparent", borderwidth: 0,
              steps: [{ range: [0, max], color: GRID_LINE }],
            },
          }],
          layout: baseLayout(false),
        };
      }

      default:
        return null;
    }
  } catch {
    return null;
  }
}
