// Every chart a result's data can make, read from the data (recommend.js),
// and the D3 window's "All the ways to see this": each view drawn from its own
// summarized data; picking one draws it as the answer's chart; back returns.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import React from "react";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
let hooks = null;
const bundle = await build({
  stdin: {
    contents: `export * from "./src/components/recommend.js";
      export {default as ChartView, drawableViews} from "./src/components/ChartView.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  loader: { ".json": "json" },
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
  plugins: [{
    name: "url-stub",
    setup(b) {
      b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
      b.onLoad({ filter: /.*/, namespace: "url-stub" },
               () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
    },
  }],
});
const compiled = new Module(path.join(frontend, "recommend-test.cjs"));
compiled.filename = path.join(frontend, "recommend-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { profile, aggregate, recommendViews, ChartView, drawableViews } = compiled.exports;
globalThis.localStorage = { getItem: () => null, setItem() {} };

// Transit: rides between stations, one row per segment per day.
const LINES = { Red: ["Howard", 42.019, -87.6728], Blue: ["O'Hare", 41.9773, -87.9045] };
const TRIPS = {
  columns: ["trip_date", "line", "from_stop", "to_stop", "from_lat", "from_lon", "to_lat", "to_lon", "riders"],
  rows: [],
};
for (const day of ["2026-10-01", "2026-10-02", "2026-10-03"]) {
  for (const [line, [stop, lat, lon]] of Object.entries(LINES)) {
    TRIPS.rows.push([day, line, stop, "Clark/Lake", lat, lon, 41.8857, -87.6309, line === "Red" ? 100 : 50]);
    TRIPS.rows.push([day, line, "Clark/Lake", stop, 41.8857, -87.6309, lat, lon, 10]);
  }
}
const byKey = (views) => Object.fromEntries(views.map((v) => [v.key, v]));

test("each column is read for what it is", () => {
  assert.deepEqual(profile(TRIPS.columns, TRIPS.rows).map((p) => p.kind),
                   ["date", "category", "category", "category", "coord", "coord", "coord", "coord", "measure"]);
  assert.deepEqual(profile(["order_id", "amount"], [[1, 5]]).map((p) => p.kind), ["id", "measure"]);
  assert.deepEqual(aggregate([["a", 1], ["b", 2], ["a", 3.333]], [0], 1), [["a", 4.33], ["b", 2]]);
  assert.deepEqual(aggregate([["a"], ["a"], ["b"]], [0], null), [["a", 2], ["b", 1]]);   // a count
  assert.deepEqual(aggregate([[1], [2]], [], 0), [[3]]);                                // a total
});

test("trips become every view their data makes, each from its own summary", () => {
  const views = byKey(recommendViews(TRIPS.columns, TRIPS.rows));
  for (const key of ["route_map", "point_map", "line", "area", "stacked_area", "calendar", "bar:line",
                     "bar:from_stop", "donut", "network", "heatmap", "histogram", "boxplot", "kpi", "table"]) {
    assert.ok(views[key], key);
  }
  // A comparison is a total per category, biggest first — not a bar per row.
  assert.deepEqual(views["bar:line"].rows, [["Red", 330], ["Blue", 180]]);
  assert.equal(views["bar:line"].title, "riders by line");
  // A trend is a total per day, in order.
  assert.deepEqual(views.line.rows, [["2026-10-01", 170], ["2026-10-02", 170], ["2026-10-03", 170]]);
  // The lines split the trend.
  assert.deepEqual(views.stacked_area.columns, ["trip_date", "Red", "Blue"]);
  assert.deepEqual(views.stacked_area.rows[0], ["2026-10-01", 110, 60]);
  // Flows from one end to the other.
  assert.deepEqual(views.network.spec, { type: "network", title: "riders from from_stop to to_stop",
                                         x: "from_stop", y: ["to_stop", "riders"] });
  assert.deepEqual(views.kpi.rows, [[510]]);
  assert.equal(views.boxplot.title, "spread of riders per line");
  // Every view draws.
  assert.equal(drawableViews(Object.values(views)).length, Object.keys(views).length);
});

test("other scenarios: sales by region and month; no measure; nothing to draw", () => {
  const sales = byKey(recommendViews(["month", "region", "revenue", "orders"],
    [["2026-01", "West", 10, 2], ["2026-01", "East", 5, 1], ["2026-02", "West", 7, 3]]));
  assert.ok(!sales.route_map && !sales.point_map);                       // no places
  assert.ok(sales.scatter && sales.scatter.spec.x === "orders");          // two measures
  assert.deepEqual(sales["bar:region"].rows, [["West", 17], ["East", 5]]);
  // No measure: counts.
  const counted = byKey(recommendViews(["status"], [["open"], ["open"], ["closed"]]));
  assert.deepEqual(counted["bar:status"].rows, [["open", 2], ["closed", 1]]);
  assert.equal(counted["bar:status"].title, "rows by status");
  assert.deepEqual(recommendViews([], []), []);
  // Biggest first, whatever order the rows came in; a trend in date order.
  const shuffled = byKey(recommendViews(["day", "kind", "n"],
    [["2026-03-02", "small", 1], ["2026-03-01", "big", 9], ["2026-03-03", "small", 2]]));
  assert.deepEqual(shuffled["bar:kind"].rows, [["big", 9], ["small", 3]]);
  assert.deepEqual(shuffled.line.rows.map((r) => r[0]), ["2026-03-01", "2026-03-02", "2026-03-03"]);
  // However few fit, the rows themselves are always one of them.
  const few = recommendViews(TRIPS.columns, TRIPS.rows, 4);
  assert.deepEqual(few.map((v) => v.key), ["route_map", "point_map", "line", "table"]);
});

// ── In the D3 window ────────────────────────────────────────────────────

function find(node, predicate) {
  if (Array.isArray(node)) {
    for (const c of node) { const hit = find(c, predicate); if (hit) return hit; }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  return predicate(node) ? node : find(node.props.children, predicate);
}

function held(props) {
  const values = [];
  let cursor = 0;
  const own = {
    useState(initial) {
      const i = cursor++;
      if (!(i in values)) values[i] = typeof initial === "function" ? initial() : initial;
      return [values[i], (next) => { values[i] = typeof next === "function" ? next(values[i]) : next; }];
    },
    useRef(initial) { const i = cursor++; if (!(i in values)) values[i] = { current: initial }; return values[i]; },
    useMemo(factory) { cursor++; return factory(); },
    useEffect() {},
  };
  return () => { cursor = 0; hooks = own; try { return ChartView(props); } finally { hooks = null; } };
}

test("picking a view draws it as the answer's chart; back returns to the answer's", () => {
  const chart = { type: "route_map", x: "line", y: ["riders"], title: "Trips" };
  const render = held({ columns: TRIPS.columns, rows: TRIPS.rows, chart, d3Only: true });
  const drawn = () => find(render(), (n) => n.type?.name === "D3View")?.props;
  const gallery = () => find(render(), (n) => n.type?.name === "ViewGallery");
  assert.equal(drawn().spec.type, "route_map");
  const views = gallery().props.ways;
  gallery().props.onPick(views.find((v) => v.key === "bar:line"));
  assert.deepEqual([drawn().spec.type, drawn().columns, drawn().rows],
                   ["bar", ["line", "riders"], [["Red", 330], ["Blue", 180]]]);
  assert.equal(gallery().props.picked.key, "bar:line");
  gallery().props.onBack();
  assert.equal(drawn().spec.type, "route_map");
  assert.equal(drawn().rows.length, TRIPS.rows.length);
  // Outside the D3 window, no gallery.
  const plain = held({ columns: TRIPS.columns, rows: TRIPS.rows, chart });
  assert.equal(find(plain(), (n) => n.type?.name === "ViewGallery"), null);
});
