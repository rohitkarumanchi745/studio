// The Visuals screen: every chart, every way D3 can draw it, and a choice.
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
let hooks = null;
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
    contents: `export {default as Visuals, baseSpec, d3Options, groupCharts, nowDrawn}
      from "./src/components/Visuals.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  plugins: [urlStub],
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
});
const compiled = new Module(path.join(frontend, "visuals-test.cjs"));
compiled.filename = path.join(frontend, "visuals-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Visuals, baseSpec, d3Options, groupCharts, nowDrawn } = compiled.exports;

const COLUMNS = ["region", "revenue"];
const ROWS = [["West", 120], ["East", 90], ["North", 60]];
const CHARTS = [
  { conversation_id: "c1", conversation_title: "Revenue chat", message_id: "m2", panel_index: 0,
    title: "By region", type: "bar", engine: null, has_visual: false, source: "demo",
    columns: COLUMNS, row_count: 3, can_edit: true },
  { conversation_id: "c1", conversation_title: "Revenue chat", message_id: "m2", panel_index: 1,
    title: "Rings", type: "table", engine: null, has_visual: true, source: "demo",
    columns: COLUMNS, row_count: 3, can_edit: true },
  { conversation_id: "c2", conversation_title: "Traffic", message_id: "m9", panel_index: 0,
    title: "Sessions by day", type: "line", engine: "d3", has_visual: false, source: "demo",
    columns: ["day", "sessions"], row_count: 30, can_edit: false },
];
const CHART = { conversation_id: "c1", message_id: "m2", panel_index: 0, title: "By region",
                columns: COLUMNS, rows: ROWS, source: "demo", engine: null, visual: null,
                chart: { type: "bar", x: "region", y: ["revenue"], title: "By region" },
                can_edit: true };

test("a chart without x/y still gets columns to draw", () => {
  assert.deepEqual(baseSpec(COLUMNS, ROWS, { type: "table", title: "Rings" }),
                   { type: "table", title: "Rings", x: "region", y: ["revenue"] });
  assert.deepEqual(baseSpec(COLUMNS, ROWS, CHART.chart).y, ["revenue"]);
});

test("the D3 options are exactly the D3 types this data fits", () => {
  const opts = d3Options(COLUMNS, ROWS, CHART.chart);
  for (const t of ["bar", "hbar", "pie", "donut", "beeswarm", "circle_pack"]) assert.ok(opts.includes(t), t);
  for (const t of ["network", "calendar", "table", "kpi", "treemap"]) assert.ok(!opts.includes(t), t);
  const days = d3Options(["day", "sessions"], [["2024-01-01", 3], ["2024-01-02", 5]], null);
  assert.ok(days.includes("calendar") && days.includes("line"));
});

test("the list groups by conversation and searches title, source and type", () => {
  assert.deepEqual(groupCharts(CHARTS).map((g) => [g.title, g.items.length]),
                   [["Revenue chat", 2], ["Traffic", 1]]);
  assert.deepEqual(groupCharts(CHARTS, "sessions").map((g) => g.title), ["Traffic"]);
  assert.deepEqual(groupCharts(CHARTS, "nothing-like-this"), []);
});

test("how a chart is drawn now, in words", () => {
  assert.equal(nowDrawn(CHART), "Bar · your default engine");
  assert.equal(nowDrawn({ ...CHART, engine: "d3" }), "Bar · d3");
  assert.equal(nowDrawn({ ...CHART, visual: { code: "x" } }), "a custom visual");
  assert.equal(nowDrawn({ ...CHART, chart: { type: "network" } }), "Network (D3) · d3");
});

// ── Rendering and choosing ──────────────────────────────────────────────

function harness(component, props) {
  const values = [], effects = [], memos = [];
  let cursor = 0, memoCursor = 0;
  const stateHooks = {
    useState(initial) {
      const index = cursor++;
      if (index >= values.length) values.push(typeof initial === "function" ? initial() : initial);
      return [values[index], (next) => { values[index] = typeof next === "function" ? next(values[index]) : next; }];
    },
    useEffect(effect) { effects.push(effect); },
    useMemo(factory) { memos[memoCursor++] = factory(); return memos[memoCursor - 1]; },
  };
  return {
    render() {
      cursor = 0; memoCursor = 0; effects.length = 0;
      hooks = stateHooks;
      try { return renderToStaticMarkup(component(props)); } finally { hooks = null; }
    },
    start() { return effects.map((e) => e()).filter(Boolean); },
    set(index, value) { values[index] = value; },
    get(index) { return values[index]; },
    tree() {
      cursor = 0; memoCursor = 0;
      hooks = stateHooks;
      try { return component(props); } finally { hooks = null; }
    },
  };
}

function findAll(node, predicate, out = []) {
  if (Array.isArray(node)) node.forEach((n) => findAll(n, predicate, out));
  else if (node && typeof node === "object") {
    if (predicate(node)) out.push(node);
    findAll(node.props?.children, predicate, out);
  }
  return out;
}

async function withApi(routes, work) {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage, window: globalThis.window };
  const calls = [];
  globalThis.localStorage = { getItem: (k) => (k === "studio_user" ? JSON.stringify({ id: "u1", role: "analyst" }) : null) };
  globalThis.window = { location: { href: "https://studio.example/visuals", search: "", pathname: "/", hash: "" } };
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, ...options });
    const key = Object.keys(routes).find((r) => url.startsWith(`/api${r}`));
    const body = typeof routes[key] === "function" ? routes[key](options) : routes[key];
    return { status: 200, ok: true, json: async () => body ?? {} };
  };
  try {
    await work({ calls, flush: async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); } });
  } finally { Object.assign(globalThis, originals); }
}

// State slots in Visuals: charts, q, sel, data, drafts, request, busy, note, error.
const S = { charts: 0, sel: 2, data: 3 };

test("the screen lists charts, then every D3 option and the visual composer", async () => {
  await withApi({ "/visuals/charts": CHARTS, "/visuals/chart": CHART }, async ({ flush }) => {
    const screen = harness(Visuals, { onClose() {} });
    screen.render();
    screen.start();
    await flush();
    let html = screen.render();
    assert.match(html, /Revenue chat/);
    assert.match(html, /Sessions by day/);
    assert.match(html, /✨ custom visual/);               // the Rings panel
    assert.match(html, /view only/);                     // the shared Traffic chart
    assert.match(html, /Pick a chart/);

    screen.set(S.sel, CHARTS[0]);
    screen.set(S.data, CHART);
    html = screen.render();
    assert.match(html, /now drawn as <b>Bar · your default engine<\/b>/);
    const useButtons = (html.match(/>Use this</g) || []).length;
    assert.equal(useButtons, d3Options(COLUMNS, ROWS, CHART.chart).length);
    assert.match(html, /✨ Write it/);
    assert.match(html, /No custom visual yet/);
  });
});

test("Use this saves the chosen D3 chart for that panel", async () => {
  await withApi({ "/visuals/charts": CHARTS, "/visuals/chart": CHART,
                  "/visuals/apply": { message_id: "m3", panel_index: 0, message: {} } },
    async ({ calls, flush }) => {
      const screen = harness(Visuals, { onClose() {} });
      screen.render();
      screen.start();
      await flush();
      screen.set(S.sel, CHARTS[0]);
      screen.set(S.data, CHART);
      const tree = screen.tree();
      const cards = findAll(tree, (n) => n.props?.className === "panel visuals-card");
      const pie = cards.find((c) => findAll(c, (n) => n.type === "b" && n.props.children === "Pie").length);
      const button = findAll(pie, (n) => n.type === "button")[0];
      await button.props.onClick();
      const apply = calls.find((c) => c.url === "/api/visuals/apply");
      assert.deepEqual(JSON.parse(apply.body), { conversation_id: "c1", message_id: "m2",
        panel_index: 0, chart_type: "pie", engine: "d3" });
      // The screen follows the new version of the answer.
      assert.equal(screen.get(S.sel).message_id, "m3");
    });
});
