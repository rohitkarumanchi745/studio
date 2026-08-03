// Altair-style renderer — Vega-Lite is the grammar Altair compiles to, so this
// is the Streamlit Altair experience in the browser. Same validated palette.
// Loaded on demand (dynamic import) so the main bundle stays light.
import { useEffect, useRef, useState } from "react";
import {
  AXIS_LINE, GRID_LINE, INK, SEQ, SERIES, SURFACE,
  firstNumericIndex, isNumericCol, resolveSpec,
} from "./common";

export const VEGA_TYPES = new Set([
  "bar", "hbar", "stacked_bar", "line", "area", "stacked_area",
  "pie", "donut", "scatter", "bubble", "heatmap", "histogram", "boxplot",
]);

let _embed = null;
function getEmbed() {
  if (!_embed) _embed = import("vega-embed").then((m) => m.default || m);
  return _embed;
}

export default function VegaView({ columns, rows, spec, tall }) {
  const ref = useRef(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let dead = false, view = null;
    setErr("");
    const vl = buildVegaSpec(columns, rows, spec);
    if (!vl) {
      setErr("This data cannot be drawn as this chart type in Altair.");
      return;
    }
    getEmbed()
      .then((embed) => {
        if (dead) return;
        return embed(el, vl, { actions: false, renderer: "canvas" });
      })
      .then((res) => { if (res) view = res; })
      .catch((e) => setErr("Altair failed to render: " + String(e.message || e).slice(0, 120)));
    return () => {
      dead = true;
      try { view?.finalize(); } catch {}
    };
  }, [columns, rows, spec]);

  if (err) return <div className="chart-note">{err}</div>;
  return <div ref={ref} className={"echart vega-fit" + (tall ? " echart-tall" : "")} />;
}

const CONFIG = {
  background: "transparent",
  axis: {
    gridColor: GRID_LINE, domainColor: AXIS_LINE, tickColor: AXIS_LINE,
    labelColor: INK.muted, titleColor: INK.muted, labelFontSize: 11, titleFontSize: 11,
  },
  legend: { labelColor: INK.secondary, titleColor: INK.muted, orient: "top" },
  range: { category: SERIES, ramp: SEQ, heatmap: SEQ },
  view: { stroke: null },
  font: "inherit",
};

function toObjects(columns, rows) {
  return rows.map((r) => Object.fromEntries(columns.map((c, i) => [c, r[i]])));
}

function buildVegaSpec(columns, rows, spec) {
  const { xi, yCols, yis } = resolveSpec(columns, rows, spec);
  const multi = yCols.length > 1;
  const values = toObjects(columns, rows);
  const base = {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    width: "container", height: "container",
    autosize: { type: "fit", contains: "padding" },
    config: CONFIG,
    data: { values },
  };
  const xEnc = { field: spec.x, type: "nominal", sort: null, axis: { labelAngle: -30, labelLimit: 90 } };
  const catColor = { field: spec.x, type: "nominal", legend: null, scale: { range: SERIES } };
  const foldColor = { field: "series", type: "nominal", scale: { range: SERIES } };

  try {
    switch (spec.type) {
      case "bar":
      case "hbar":
      case "stacked_bar": {
        if (xi < 0 || !yis.length) return null;
        const horiz = spec.type === "hbar";
        const stacked = spec.type === "stacked_bar";
        const mark = {
          type: "bar", stroke: SURFACE, strokeWidth: 1,
          ...(horiz ? { cornerRadiusEnd: 4 } : { cornerRadiusTopLeft: 4, cornerRadiusTopRight: 4 }),
        };
        if (!multi) {
          const val = { field: yCols[0], type: "quantitative" };
          return { ...base, mark, encoding: horiz
            ? { y: { ...xEnc, axis: { labelLimit: 110 } }, x: val, color: catColor }
            : { x: xEnc, y: val, color: catColor } };
        }
        const enc = {
          x: xEnc,
          y: { field: "value", type: "quantitative", title: null },
          color: foldColor,
          ...(stacked ? {} : { xOffset: { field: "series" } }),
        };
        return {
          ...base,
          transform: [{ fold: yCols, as: ["series", "value"] }],
          mark,
          encoding: horiz
            ? { y: xEnc, x: enc.y, color: foldColor, ...(stacked ? {} : { yOffset: { field: "series" } }) }
            : enc,
        };
      }

      case "line":
      case "area":
      case "stacked_area": {
        if (xi < 0 || !yis.length) return null;
        const isArea = spec.type !== "line";
        const mark = isArea
          ? { type: "area", line: { strokeWidth: 2 }, opacity: 0.35 }
          : { type: "line", strokeWidth: 2 };
        if (!multi) {
          return { ...base, mark: { ...mark, color: SERIES[0] }, encoding: {
            x: { ...xEnc, axis: { labelAngle: -30, labelLimit: 90 } },
            y: { field: yCols[0], type: "quantitative" },
          } };
        }
        return {
          ...base,
          transform: [{ fold: yCols, as: ["series", "value"] }],
          mark,
          encoding: {
            x: xEnc,
            y: { field: "value", type: "quantitative", title: null,
                 stack: spec.type === "stacked_area" ? "zero" : null },
            color: foldColor,
          },
        };
      }

      case "pie":
      case "donut": {
        const vi = yis[0] ?? firstNumericIndex(columns, rows, xi);
        if (xi < 0 || vi < 0) return null;
        return {
          ...base,
          mark: {
            type: "arc", stroke: SURFACE, strokeWidth: 2,
            innerRadius: spec.type === "donut" ? 55 : 0,
          },
          encoding: {
            theta: { field: columns[vi], type: "quantitative" },
            color: { field: spec.x, type: "nominal", scale: { range: SERIES } },
          },
        };
      }

      case "scatter":
      case "bubble": {
        if (xi < 0 || !yis.length) return null;
        const si = spec.type === "bubble" && yis.length > 1 ? yis[1] : null;
        return {
          ...base,
          mark: { type: "point", filled: true, size: 80, opacity: 0.85,
                  stroke: SURFACE, strokeWidth: 1, color: SERIES[0] },
          encoding: {
            x: isNumericCol(rows, xi)
              ? { field: spec.x, type: "quantitative" }
              : xEnc,
            y: { field: yCols[0], type: "quantitative" },
            ...(si != null ? { size: { field: columns[si], type: "quantitative", legend: null } } : {}),
          },
        };
      }

      case "heatmap": {
        if (xi < 0 || yis.length < 1) return null;
        const ryi = yis[0];
        const vi = yis.length > 1 ? yis[1] : firstNumericIndex(columns, rows, xi, ryi);
        if (vi < 0) return null;
        return {
          ...base,
          mark: { type: "rect", stroke: SURFACE, strokeWidth: 2 },
          encoding: {
            x: xEnc,
            y: { field: columns[ryi], type: "nominal", sort: null },
            color: { field: columns[vi], type: "quantitative", scale: { range: SEQ } },
          },
        };
      }

      case "histogram": {
        const vi = yis[0] ?? (isNumericCol(rows, xi) ? xi : firstNumericIndex(columns, rows));
        if (vi < 0) return null;
        return {
          ...base,
          mark: { type: "bar", color: SERIES[0], stroke: SURFACE, strokeWidth: 1,
                  cornerRadiusTopLeft: 4, cornerRadiusTopRight: 4 },
          encoding: {
            x: { field: columns[vi], type: "quantitative", bin: { maxbins: 20 } },
            y: { aggregate: "count", type: "quantitative" },
          },
        };
      }

      case "boxplot": {
        if (xi < 0 || !yis.length) return null;
        return {
          ...base,
          mark: { type: "boxplot", color: SERIES[0], median: { color: INK.primary } },
          encoding: {
            x: xEnc,
            y: { field: yCols[0], type: "quantitative" },
          },
        };
      }

      default:
        return null;
    }
  } catch {
    return null;
  }
}
