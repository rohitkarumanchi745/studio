// Shared chart theme — the single validated palette every render engine
// (ECharts, Plotly, Vega-Lite) draws from, so charts read as one system
// regardless of which engine painted them. Validated against the light
// surface; don't tweak values here without re-running the palette validator.
export const SERIES = ["#3987e5", "#199e70", "#c98500", "#008300", "#9085e9", "#e66767", "#d55181", "#d95926"];
export const SEQ = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]; // blue ramp (magnitude)
export const GOOD = "#008300", BAD = "#e66767", TOTAL = "#3987e5";
export const INK = { primary: "#1b1a19", secondary: "#4a4844", muted: "#79766f" };
export const GRID_LINE = "#eceae7";
export const AXIS_LINE = "#d9d6d2";
export const SURFACE = "#ffffff";

export function firstNumericIndex(columns, rows, ...exclude) {
  const first = rows[0] || [];
  for (let i = 0; i < columns.length; i++) {
    if (exclude.includes(i)) continue;
    if (typeof first[i] === "number") return i;
  }
  return -1;
}

export function isNumericCol(rows, i) {
  return typeof (rows[0] || [])[i] === "number";
}

// Resolve the spec against the data: x index, y columns/indices, x values.
export function resolveSpec(columns, rows, spec) {
  const xi = columns.indexOf(spec.x);
  let yCols = (spec.y || []).filter((c) => columns.includes(c));
  if (!yCols.length) {
    const ni = firstNumericIndex(columns, rows, xi);
    if (ni >= 0) yCols = [columns[ni]];
  }
  const yis = yCols.map((c) => columns.indexOf(c));
  const xs = xi >= 0 ? rows.map((r) => r[xi]) : rows.map((_, i) => i + 1);
  return { xi, yCols, yis, xs };
}
