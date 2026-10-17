// The D3 & artifacts window, made interactive like a Claude artifact: a filter
// bar above every visual (dates, categories, top N), charts that move rather
// than jump, and custom visuals that build their own controls and take new
// rows without reloading.
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM, VirtualConsole } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = { getItem: () => null, setItem() {} };

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
let hooks = null;
const bundle = await build({
  stdin: {
    contents: `export * from "./src/components/visualFilters.js";
      export {default as ChartView} from "./src/components/ChartView.jsx";
      export {default as VisualControls} from "./src/components/VisualControls.jsx";
      export {default as D3View, draw} from "./src/components/renderers/D3View.jsx";
      export {default as ArtifactView, buildArtifactDoc} from "./src/components/renderers/ArtifactView.jsx";
      export {animate, captureBars} from "./src/components/renderers/motion.js";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  plugins: [{
    name: "url-stub",
    setup(b) {
      b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
      b.onLoad({ filter: /.*/, namespace: "url-stub" },
               () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
    },
  }],
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
});
const compiled = new Module(path.join(frontend, "visual-controls-test.cjs"));
compiled.filename = path.join(frontend, "visual-controls-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const F = compiled.exports;
const { ChartView, VisualControls, D3View, draw, animate, captureBars, buildArtifactDoc } = F;
const d3 = await import("d3");

const DAILY = {
  columns: ["order_date", "region", "revenue"],
  rows: [["2026-01-03", "West", 120], ["2026-02-10", "East", 90], ["2026-03-28", "West", 60],
         ["2026-04-15", "North", 30], ["2026-06-30", "East", 45]],
};
const MONTHLY = { columns: ["month", "revenue"],
                  rows: [["2026-01", 10], ["2026-02", 14], ["2026-03", 9], ["2026-04", 12]] };

// ── What the bar offers, and what it keeps ──────────────────────────────

test("a date column becomes a range with presets anchored on the data, not today", () => {
  const c = F.detectControls(DAILY.columns, DAILY.rows, { type: "bar", x: "region", y: ["revenue"] });
  assert.deepEqual(c.date, { column: "order_date", min: "2026-01-03", max: "2026-06-30", monthly: false });
  const p = Object.fromEntries(F.datePresets(c.date).map((x) => [x.key, x.from]));
  assert.equal(p["30d"], "2026-06-01");                 // 30 days ending at the data's last day
  assert.equal(p.ytd, "2026-01-03");                    // clamped to where the data starts
  const m = F.detectControls(MONTHLY.columns, MONTHLY.rows, { type: "line" });
  assert.equal(m.date.monthly, true);
  assert.equal(F.datePresets(m.date).find((x) => x.key === "3m").from, "2026-02-01");
});

test("categories come from text columns with a handful of values, the chart's own first", () => {
  const c = F.detectControls(DAILY.columns, DAILY.rows, { type: "bar", x: "region", y: ["revenue"] });
  assert.deepEqual(c.categories, [{ column: "region", values: ["East", "North", "West"] }]);
  // Numbers and ids are not filters.
  const ids = { columns: ["id", "n"], rows: Array.from({ length: 40 }, (_, i) => [`id-${i}`, i]) };
  assert.deepEqual(F.detectControls(ids.columns, ids.rows, {}).categories, []);
});

test("filters keep what they say: a date range, picked categories, the top N", () => {
  const c = F.detectControls(DAILY.columns, DAILY.rows, { type: "bar", x: "region", y: ["revenue"] });
  const keep = (state) => F.applyControls(DAILY.columns, DAILY.rows, c, { ...F.EMPTY_STATE, ...state });
  assert.equal(keep({}), DAILY.rows);                                     // nothing narrowed
  assert.deepEqual(keep({ from: "2026-02-01", to: "2026-04-15" }).map((r) => r[0]),
                   ["2026-02-10", "2026-03-28", "2026-04-15"]);            // inclusive
  assert.deepEqual(keep({ cats: { region: ["East"] } }).map((r) => r[2]), [90, 45]);
  assert.deepEqual(keep({ cats: { region: [] } }), []);
  const wide = { columns: ["product", "sales"],
                 rows: Array.from({ length: 14 }, (_, i) => [`p${i}`, i * 10]) };
  const wc = F.detectControls(wide.columns, wide.rows, { type: "hbar", x: "product", y: ["sales"] });
  assert.deepEqual(wc.topN, { column: "product", by: "sales" });
  assert.deepEqual(F.applyControls(wide.columns, wide.rows, wc, { ...F.EMPTY_STATE, top: 3 })
                     .map((r) => r[0]), ["p11", "p12", "p13"]);
});

// ── The bar on a chart in the D3 window ─────────────────────────────────

function harness(props) {
  const values = [];
  let cursor = 0;
  const stateHooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in values)) values[i] = typeof initial === "function" ? initial() : initial;
      return [values[i], (next) => { values[i] = typeof next === "function" ? next(values[i]) : next; }];
    },
    useRef(initial) {
      const i = cursor++;
      if (!(i in values)) values[i] = { current: initial };
      return values[i];
    },
    useMemo(factory) { return factory(); },
    useEffect() {},
  };
  return () => { cursor = 0; hooks = stateHooks; try { return ChartView(props); } finally { hooks = null; } };
}

function find(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const hit = find(child, predicate);
      if (hit) return hit;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  if (predicate(node)) return node;
  return find(node.props.children, predicate);
}

const BAR = { type: "bar", x: "region", y: ["revenue"], title: "Revenue" };

test("the D3 window shows the bar, and the chart draws what it keeps, with motion", () => {
  const view = harness({ columns: DAILY.columns, rows: DAILY.rows, chart: BAR, d3Only: true });
  let tree = view();
  const bar = find(tree, (n) => n.type === VisualControls);
  assert.ok(bar, "filter bar shown");
  let chart = find(tree, (n) => n.type === D3View);
  assert.equal(chart.props.rows.length, 5);
  assert.equal(chart.props.motion, true);
  bar.props.onChange({ ...F.EMPTY_STATE, preset: "custom", from: "2026-03-01", to: null });
  tree = view();
  chart = find(tree, (n) => n.type === D3View);
  assert.deepEqual(chart.props.rows.map((r) => r[0]), ["2026-03-28", "2026-04-15", "2026-06-30"]);
  const html = renderToStaticMarkup(find(tree, (n) => n.type === VisualControls));
  assert.match(html, /<input type="date" aria-label="From"/);
  assert.match(html, /3 of 5 rows/);
  assert.match(html, /<option value="30d">Last 30 days<\/option>/);
  // Nothing left: a note, not an empty chart.
  find(view(), (n) => n.type === VisualControls).props.onChange(
    { ...F.EMPTY_STATE, cats: { region: [] } });
  assert.match(renderToStaticMarkup(view()), /Nothing in this range/);
  // Reset brings everything back.
  find(view(), (n) => n.type === VisualControls).props.onChange(null);
  assert.equal(find(view(), (n) => n.type === D3View).props.rows.length, 5);
});

test("outside the D3 window charts are untouched", () => {
  const tree = harness({ columns: DAILY.columns, rows: DAILY.rows, chart: BAR })();
  assert.equal(find(tree, (n) => n.type === VisualControls), null);
});

test("a custom visual gets the same bar and the filtered rows", () => {
  const visual = { title: "Rings", code: "/* draws */" };
  const view = harness({ columns: DAILY.columns, rows: DAILY.rows, chart: BAR, d3Only: true, visual });
  find(view(), (n) => n.type === VisualControls).props.onChange(
    { ...F.EMPTY_STATE, cats: { region: ["West"] } });
  const frame = find(view(), (n) => n.props?.visual === visual && n.props?.rows);
  assert.deepEqual(frame.props.rows.map((r) => r[2]), [120, 60]);
});

// ── Motion ───────────────────────────────────────────────────────────────

const tick = (ms) => new Promise((r) => setTimeout(r, ms));

function drawBars(rows, el = document.createElement("div")) {
  el.replaceChildren();
  draw(d3, el, ["region", "revenue"], rows, BAR, { w: 480, h: 240 });
  return el;
}
const heights = (el) => [...el.querySelectorAll("svg rect")].map((r) => Number(r.getAttribute("height")));

test("bars grow from their axis on first draw and settle where they belong", async () => {
  const el = drawBars([["West", 120], ["East", 90]]);
  const final = heights(el);
  assert.ok(animate(d3, el, BAR) >= 2);
  assert.deepEqual(heights(el), [0, 0]);                    // start on the axis
  await tick(1100);
  assert.deepEqual(heights(el).map(Math.round), final.map(Math.round));
});

test("on a filter change a bar glides from where it was", async () => {
  const el = drawBars([["West", 120], ["East", 90]]);
  const before = captureBars(el);
  assert.equal(before.size, 2);
  drawBars([["West", 120], ["East", 30]], el);
  const final = heights(el);
  animate(d3, el, BAR, before);
  const west = before.get([...before.keys()].find((k) => k.endsWith("West")));
  assert.equal(Number(el.querySelector("svg rect").getAttribute("height")), west.height); // not 0
  await tick(800);
  assert.deepEqual(heights(el).map(Math.round), final.map(Math.round));
});

test("reduced motion means no animation at all", () => {
  const el = drawBars([["West", 120], ["East", 90]]);
  const final = heights(el);
  const original = globalThis.matchMedia;
  globalThis.matchMedia = () => ({ matches: true });
  try {
    assert.equal(animate(d3, el, BAR), 0);
    assert.deepEqual(heights(el), final);
  } finally {
    globalThis.matchMedia = original;
  }
});

// ── Custom visuals: their own controls, and new rows without a reload ────

const d3Source = readFileSync(path.join(frontend, "node_modules/d3/dist/d3.min.js"), "utf8");

test("a custom visual builds controls that keep their value, and takes new rows in place", async () => {
  const code = `
    var bar = ui.bar(el);
    var metric = ui.select(bar, {key: "m", label: "Metric", options: ["revenue", "cost"], value: "revenue"});
    var n = ui.slider(bar, {key: "n", label: "Top", min: 1, max: 5, value: 3});
    ui.toggle(bar, {key: "pct", label: "as %", value: false});
    d3.select(el).append("div").attr("class", "out").text(metric + "|" + n + "|" + data.length);`;
  const doc = buildArtifactDoc({ code, columns: ["region", "revenue", "cost"],
                                 rows: [["West", 1, 2], ["East", 3, 4]], nonce: "c1", inlineD3: d3Source });
  const frame = new JSDOM(doc, { runScripts: "dangerously", pretendToBeVisual: true,
                                 virtualConsole: new VirtualConsole() });
  const w = frame.window;
  await tick(30);
  const out = () => w.document.querySelector(".out").textContent;
  assert.equal(out(), "revenue|3|2");
  assert.equal(w.document.querySelectorAll(".sv-bar select, .sv-bar input").length, 3);
  // The user switches the metric; a redraw (here: new rows) keeps the choice.
  const select = w.document.querySelector(".sv-bar select");
  select.value = "cost";
  select.dispatchEvent(new w.Event("change"));
  w.dispatchEvent(new w.MessageEvent("message", {
    source: w, data: { studioRows: { columns: ["region", "revenue", "cost"], rows: [["West", 1, 2]] } } }));
  await tick(10);
  assert.equal(out(), "cost|3|1");                         // same frame, new rows, choice kept
  // Rows from anywhere but the embedding page are ignored.
  w.dispatchEvent(new w.MessageEvent("message", { source: null,
    data: { studioRows: { columns: ["region"], rows: [] } } }));
  await tick(10);
  assert.equal(out(), "cost|3|1");
  frame.window.close();
});
