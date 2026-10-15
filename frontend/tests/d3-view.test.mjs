// D3: a fourth render engine, and the chart types only it draws.
//
// What is pinned here is what the charts are built FROM — the pure data
// builders and the fit rules that decide when a D3 type is offered — plus the
// routing rule that a D3-only type draws with D3 whichever engine is picked.
// Drawing itself happens in effects, which server rendering never runs.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
// Vite's "?url" import (ArtifactView's d3 file) means nothing to esbuild.
const urlStub = {
  name: "url-stub",
  setup(b) {
    b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
    b.onLoad({ filter: /.*/, namespace: "url-stub" },
             () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
  },
};
const bundle = await build({
  stdin: {
    contents: `export {default as ChartView, computeFit, LABELS} from "./src/components/ChartView.jsx";
      export {D3_ONLY, D3_TYPES, networkData, beeswarmData, calendarData, packData, dayKey}
        from "./src/components/renderers/D3View.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  plugins: [urlStub],
  // The engines load inside effects, which server rendering never runs; they
  // stay out of the bundle (Vega's Node canvas build cannot even be bundled
  // as CommonJS). echarts is imported eagerly, so it is stubbed below.
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
});
const compiled = new Module(path.join(frontend, "d3-test.cjs"));
compiled.filename = path.join(frontend, "d3-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => (id === "echarts" ? { init() { return {}; } } : require(id));
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { ChartView, computeFit, LABELS, D3_ONLY, D3_TYPES, networkData, beeswarmData,
        calendarData, packData, dayKey } = compiled.exports;

globalThis.localStorage = { getItem: () => null, setItem() {} };

test("D3 is an engine for the core types and the only engine for its own", () => {
  for (const t of ["bar", "hbar", "line", "area", "pie", "donut", "scatter", "heatmap"]) {
    assert.ok(D3_TYPES.has(t), t);
  }
  for (const t of ["network", "beeswarm", "calendar", "circle_pack"]) {
    assert.ok(D3_ONLY.has(t) && D3_TYPES.has(t) && LABELS[t], t);
  }
});

test("a network sums repeated links, drops self-links and weighs its nodes", () => {
  const cols = ["from", "to", "calls"];
  const rows = [["api", "db", 3], ["api", "db", 2], ["web", "api", 4], ["db", "db", 9]];
  const net = networkData(cols, rows, { x: "from", y: ["to", "calls"] });
  assert.deepEqual(net.links, [{ source: "api", target: "db", weight: 5 },
                               { source: "web", target: "api", weight: 4 }]);
  const degree = Object.fromEntries(net.nodes.map((n) => [n.id, n.degree]));
  assert.deepEqual(degree, { api: 9, db: 5, web: 4 });
  // Without a weight column every row is one link.
  assert.equal(networkData(cols, rows, { x: "from", y: ["to"] }).links[0].weight, 2);
});

test("a calendar adds up each day, in order, from any date-ish column", () => {
  assert.equal(dayKey("2024-03-05"), "2024-03-05");
  assert.equal(dayKey("2024-03-05 14:30:00"), "2024-03-05");
  assert.equal(dayKey("2024-03-05T14:30:00Z"), "2024-03-05");
  assert.equal(dayKey("March 5"), null);
  const cal = calendarData(["day", "orders"],
    [["2024-01-02", 4], ["2024-01-01", 1], ["2024-01-02 18:00:00", 6]],
    { x: "day", y: ["orders"] });
  assert.deepEqual(cal.days, [{ date: "2024-01-01", value: 1 }, { date: "2024-01-02", value: 10 }]);
  assert.deepEqual(cal.years, ["2024"]);
  assert.equal(calendarData(["region", "orders"], [["West", 4]], { x: "region", y: ["orders"] }), null);
});

test("a beeswarm keeps every point and splits on a category", () => {
  const cols = ["region", "amount"];
  const rows = [["West", 10], ["East", 7], ["West", 3], ["East", null]];
  const swarm = beeswarmData(cols, rows, { x: "region", y: ["amount"] });
  assert.equal(swarm.points.length, 3);                 // the null is skipped, not zeroed
  assert.deepEqual(swarm.groups, ["West", "East"]);
  assert.equal(swarm.groupBy, "region");
});

test("circle packing groups leaves under a parent column when one is given", () => {
  const cols = ["product", "revenue", "category"];
  const rows = [["Lamp", 30, "Home"], ["Desk", 50, "Office"], ["Rug", 20, "Home"], ["Pen", 0, "Office"]];
  const flat = packData(cols, rows, { x: "product", y: ["revenue"] });
  assert.equal(flat.children.length, 3);               // zero-sized leaves are dropped
  const grouped = packData(cols, rows, { x: "product", y: ["revenue", "category"] });
  assert.deepEqual(grouped.children.map((g) => [g.name, g.children.length]),
                   [["Home", 2], ["Office", 1]]);
});

test("the D3 types are offered only when the data fits them", () => {
  const links = { columns: ["from", "to", "calls"], rows: [["a", "b", 1], ["b", "c", 2]] };
  const fit = computeFit(links.columns, links.rows, { x: "from", y: ["to", "calls"] });
  assert.equal(fit.network.ok, true);
  assert.equal(fit.calendar.ok, false);                // no dates here

  const sales = { columns: ["region", "revenue"], rows: [["West", 10], ["East", 20]] };
  const f2 = computeFit(sales.columns, sales.rows, { x: "region", y: ["revenue"] });
  assert.equal(f2.network.ok, false);                  // a numeric target is not a node
  assert.equal(f2.circle_pack.ok, true);
  assert.equal(f2.beeswarm.ok, true);

  const days = { columns: ["day", "orders"], rows: [["2024-01-01", 3], ["2024-01-02", 5]] };
  assert.equal(computeFit(days.columns, days.rows, { x: "day", y: ["orders"] }).calendar.ok, true);

  const many = Array.from({ length: 2500 }, (_, i) => ["g", i]);
  assert.equal(computeFit(["g", "v"], many, { x: "g", y: ["v"] }).beeswarm.ok, false);
});

test("a D3-only type draws with D3 even when ECharts is the picked engine", () => {
  const html = renderToStaticMarkup(React.createElement(ChartView, {
    columns: ["from", "to", "calls"], rows: [["a", "b", 1], ["b", "c", 2]],
    chart: { type: "network", title: "Calls", x: "from", y: ["to", "calls"] },
  }));
  assert.match(html, /d3-chart/);
  assert.doesNotMatch(html, /rendered with ECharts instead/);
});

test("picking the D3 engine draws a core type with D3", () => {
  globalThis.localStorage = { getItem: (k) => (k === "studio_renderer" ? "d3" : null), setItem() {} };
  try {
    const html = renderToStaticMarkup(React.createElement(ChartView, {
      columns: ["region", "revenue"], rows: [["West", 10], ["East", 20]],
      chart: { type: "bar", title: "Revenue", x: "region", y: ["revenue"] },
    }));
    assert.match(html, /d3-chart/);
    assert.match(html, /<option value="d3" selected="">D3<\/option>/);
  } finally {
    globalThis.localStorage = { getItem: () => null, setItem() {} };
  }
});
