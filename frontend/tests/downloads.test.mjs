// Downloads: an answer's rows as CSV or Excel, an answer, the canvas or a
// dashboard as one HTML report, and a chart as a PNG. The report's charts are
// drawn by ECharts' own server-side renderer (chartImage) — real SVG, here in
// Node — so a report has its charts even when none is open on screen.
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
const bundle = await build({
  stdin: {
    contents: `export {AssistantMessage, answerReport} from "./src/components/Chat.jsx";
               export {canvasReport} from "./src/components/Canvas.jsx";
               export {dashboardReport} from "./src/components/Dashboard.jsx";
               export {chartImage} from "./src/components/ChartView.jsx";
               export {attachmentName, base64Text, downloadTable, downloadReport}
                 from "./src/exporting.js";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
  plugins: [
    {
      name: "url-stub",
      setup(b) {
        b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
        b.onLoad({ filter: /.*/, namespace: "url-stub" },
                 () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
      },
    },
    {
      name: "stub-children",
      setup(b) {
        b.onResolve({ filter: /^\.\/(SqlLab|ChatWorkflow|ChatPlatformRun)$/ },
                    ({ path: p }) => ({ path: p.slice(2), namespace: "child" }));
        b.onLoad({ filter: /.*/, namespace: "child" }, ({ path: name }) => ({
          contents: `export default function ${name}() { return null; }
                     export function PlatformComposer() { return null; } export function chartImage() { return null; }`,
          loader: "jsx",
        }));
      },
    },
  ],
});
const compiled = new Module(path.join(frontend, "downloads-test.cjs"));
compiled.filename = path.join(frontend, "downloads-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
  useCallback: (fn) => fn,
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AssistantMessage, answerReport, canvasReport, dashboardReport, chartImage,
        attachmentName, base64Text, downloadTable, downloadReport } = compiled.exports;

function harness(component, props) {
  const values = [];
  const effects = [];
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
    useEffect(effect) { effects.push(effect); },
  };
  return {
    tree() {
      cursor = 0;
      effects.length = 0;
      hooks = stateHooks;
      try { return component(props); } finally { hooks = null; }
    },
    markup() { return renderToStaticMarkup(this.tree()); },
    effects() { this.tree(); effects.forEach((e) => e()); },
  };
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
  return predicate(node) ? node : find(node.props.children, predicate);
}
const text = (n) => (Array.isArray(n) ? n.map(text).join("") : typeof n === "string" ? n
  : React.isValidElement(n) ? text(n.props.children) : "");
const button = (tree, label) => find(tree, (n) => n.type === "button" && text(n.props.children).includes(label));


const svgOf = (url) => Buffer.from(url.replace(/^data:image\/svg\+xml;base64,/, ""), "base64").toString("utf8");

const COLUMNS = ["region", "revenue"];
const ROWS = [["EU", 120], ["US", 80], ["APAC", 40]];
const BAR = { type: "bar", x: "region", y: ["revenue"], title: "Revenue by region" };

test("a report's chart is real SVG, drawn with no screen", () => {
  const url = chartImage(COLUMNS, ROWS, BAR);
  assert.match(url, /^data:image\/svg\+xml;base64,/);
  const svg = svgOf(url);
  assert.match(svg, /^<svg[^>]*width="900"[^>]*height="420"/);
  for (const label of ["EU", "US", "APAC"]) assert.ok(svg.includes(`>${label}<`), label);
  // A table, a KPI card, a misfit or no data draw no image.
  assert.equal(chartImage(COLUMNS, ROWS, { ...BAR, type: "table" }), null);
  assert.equal(chartImage(COLUMNS, ROWS, { ...BAR, type: "kpi" }), null);
  // Only D3 draws these; ECharts' rendering of them would not be the chart.
  for (const type of ["network", "beeswarm", "calendar", "circle_pack"]) {
    assert.equal(chartImage(["a", "b", "n"], [["x", "y", 1], ["y", "z", 2]], { ...BAR, type,
      source: "a", target: "b", value: "n" }), null, type);
  }
  assert.equal(chartImage(COLUMNS, [], BAR), null);
  assert.equal(chartImage(COLUMNS, ROWS, null), null);
  assert.equal(svgOf(chartImage(["d", "n"], [["Zürich ✓", 1]], { type: "bar", x: "d", y: ["n"] }))
    .includes("Zürich ✓"), true);                                   // UTF-8 survives base64
});

test("an answer's report has a section per view, the text first", () => {
  const m = { text: "EU leads.", source: "shop", sql: "SELECT 1", chart: BAR, columns: COLUMNS,
              rows: ROWS };
  const one = answerReport(m, { title: "revenue by region", columns: COLUMNS, rows: ROWS });
  assert.equal(one.title, "revenue by region");
  assert.equal(one.subtitle, "shop");
  assert.equal(one.sections.length, 1);
  const [s] = one.sections;
  assert.equal(s.title, "Revenue by region");
  assert.equal(s.text, "EU leads.");
  assert.match(s.image, /^data:image\/svg\+xml;base64,/);
  assert.deepEqual([s.columns, s.rows, s.sql, s.source], [COLUMNS, ROWS, "SELECT 1", "shop"]);

  const panels = [{ columns: COLUMNS, rows: ROWS, chart: BAR, sql: "SELECT a", source: "eu_dw" },
                  { columns: COLUMNS, rows: ROWS, chart: { ...BAR, type: "table", title: "" } }];
  const many = answerReport({ ...m, source: "*", panels }, { title: "q", columns: COLUMNS, rows: ROWS });
  assert.equal(many.subtitle, undefined);                          // "all sources" is no source
  assert.deepEqual(many.sections.map((x) => x.title), ["Revenue by region", "View 2"]);
  assert.deepEqual(many.sections.map((x) => x.text), ["EU leads.", null]);
  assert.deepEqual(many.sections.map((x) => x.source), ["eu_dw", null]);
  assert.equal(many.sections[1].image, null);                       // a table draws no image
});

test("the canvas and a dashboard become reports too", () => {
  const canvas = canvasReport({ source: "shop", panels: [{ columns: COLUMNS, rows: ROWS, chart: BAR,
                                                          sql: "SELECT 2" }] }, "Revenue");
  assert.equal(canvas.sections.length, 1);
  assert.match(canvas.sections[0].image, /^data:image\/svg/);
  assert.equal(canvas.sections[0].sql, "SELECT 2");
  const doc = { title: "Weekly", description: "the numbers", tiles: [
    { id: "t1", title: "Revenue", sql: "SELECT r", source: "shop", spec: BAR },
    { id: "t2", title: "Secret" }, { id: "t3", title: "Broken" }, { id: "t4", title: "Not loaded" }] };
  const data = { t1: { columns: COLUMNS, rows: ROWS }, t2: { denied: true }, t3: { error: "boom" } };
  const report = dashboardReport(doc, data);
  assert.equal(report.title, "Weekly");
  assert.equal(report.subtitle, "the numbers");
  assert.deepEqual(report.sections.map((x) => x.title), ["Revenue"]);   // denied / failed left out
  assert.match(report.sections[0].image, /^data:image\/svg/);
  assert.deepEqual([report.sections[0].sql, report.sections[0].source], ["SELECT r", "shop"]);
});

async function withDownloads(run) {
  const original = { fetch: globalThis.fetch, localStorage: globalThis.localStorage,
                     document: globalThis.document, URL: globalThis.URL };
  const sent = [], saved = [];
  globalThis.localStorage = { getItem: (k) => (k === "studio_token" ? "tok" : null) };
  globalThis.fetch = async (url, options = {}) => {
    sent.push({ url, method: options.method, auth: options.headers?.Authorization,
                body: JSON.parse(options.body) });
    return { ok: true, status: 200,
             headers: { get: (h) => h === "content-disposition" ? 'attachment; filename="Revenue.csv"' : null },
             blob: async () => ({ size: 3 }), json: async () => ({}) };
  };
  globalThis.URL = { createObjectURL: () => "blob:x", revokeObjectURL() {} };
  globalThis.document = {
    createElement: () => ({ click() { saved.push(this.download); }, remove() {} }),
    body: { appendChild() {} },
  };
  try { await run(sent, saved); } finally { Object.assign(globalThis, original); }
}

test("a download posts what is shown and saves the server's file name", async () => {
  await withDownloads(async (sent, saved) => {
    const name = await downloadTable("csv", { title: "Revenue", columns: COLUMNS, rows: ROWS });
    assert.equal(name, "Revenue.csv");
    assert.deepEqual(saved, ["Revenue.csv"]);
    assert.equal(sent[0].url, "/api/export/csv");
    assert.equal(sent[0].method, "POST");
    assert.equal(sent[0].auth, "Bearer tok");
    assert.deepEqual(sent[0].body.rows, ROWS);
    await downloadReport({ title: "R", sections: [] });
    assert.equal(sent[1].url, "/api/export/report");
  });
  assert.equal(attachmentName('attachment; filename="a.xlsx"', "x"), "a.xlsx");
  assert.equal(attachmentName(null, "fallback.csv"), "fallback.csv");
  assert.equal(Buffer.from(base64Text("Zürich ✓"), "base64").toString("utf8"), "Zürich ✓");
});

test("an answer offers CSV, Excel and Report only when it has rows to give", async () => {
  const m = { text: "EU leads.", source: "shop", table: "orders", sql: "SELECT region, revenue",
              chart: BAR, columns: COLUMNS, rows: ROWS, model: "x" };
  const html = harness(AssistantMessage, { m, requirement: "revenue by region", onOpenCanvas() {} }).markup();
  for (const label of ["⬇ CSV", "⬇ Excel", "⬇ Report"]) assert.ok(html.includes(label), label);
  for (const gone of [{ ...m, rows: [] }, { ...m, rows_purged: true, rows: [] }]) {
    const none = harness(AssistantMessage, { m: gone, onOpenCanvas() {} }).markup();
    assert.doesNotMatch(none, /⬇ CSV|⬇ Excel|⬇ Report/);
  }
  await withDownloads(async (sent) => {
    const h = harness(AssistantMessage, { m, requirement: "revenue by region", onOpenCanvas() {} });
    await button(h.tree(), "⬇ Excel").props.onClick();
    assert.equal(sent[0].url, "/api/export/xlsx");
    assert.deepEqual(sent[0].body, { title: "revenue by region", columns: COLUMNS, rows: ROWS,
                                     sql: "SELECT region, revenue", text: "EU leads.", source: "shop" });
    assert.match(h.markup(), /saved Revenue\.csv/);                 // the name the server gave
    await button(h.tree(), "⬇ Report").props.onClick();
    const report = sent[1].body;
    assert.equal(sent[1].url, "/api/export/report");
    assert.equal(report.title, "revenue by region");
    assert.match(report.sections[0].image, /^data:image\/svg\+xml;base64,/);
  });
});
