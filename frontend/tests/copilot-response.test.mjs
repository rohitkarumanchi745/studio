// Answers, Copilot-style: who answered and from where on one quiet line, the
// answer, its result as a card that opens the canvas, numbered sources, one
// row of actions (export in a menu), and follow-up questions read off the
// answer's own columns under the latest answer.
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
    contents: `export {AssistantMessage, followUps} from "./src/components/Chat.jsx";`,
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
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatWorkflow|ChatPlatformRun|ChartView)$/ },
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
const compiled = new Module(path.join(frontend, "copilot-response-test.cjs"));
compiled.filename = path.join(frontend, "copilot-response-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AssistantMessage, followUps } = compiled.exports;

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

async function withNetwork(answer, run) {
  const original = { fetch: globalThis.fetch, localStorage: globalThis.localStorage };
  const sent = [];
  globalThis.localStorage = { getItem() { return null; } };
  globalThis.fetch = async (url, options = {}) => {
    sent.push({ url, method: options.method || "GET", body: options.body && JSON.parse(options.body) });
    return { ok: true, status: 200, json: async () => answer(url) };
  };
  try { await run(sent); } finally { Object.assign(globalThis, original); }
}


globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };

const ANSWER = {
  text: "**West** leads with 2.18M.", source: "shop", table: "*", model: "claude", trace_id: "tr1",
  sql: "SELECT region, SUM(amount) AS revenue FROM orders GROUP BY region",
  columns: ["region", "segment", "revenue"],
  rows: [["West", "smb", 2180000], ["East", "ent", 2070000]],
  chart: { type: "bar", x: "region", y: ["revenue"], title: "Revenue by region" },
  source_meta: { source_type: "sql", engine: "sqlite" },
  inputs: { tables: ["orders"], columns: ["region", "amount"], row_count: 2 },
  citations: [{ source: "pricing.pdf", page: 3 }],
};

test("follow-ups are questions this data can go on to answer", () => {
  assert.deepEqual(followUps(["region", "segment", "revenue"], [["West", "smb", 2], ["East", "ent", 3]]),
                   ["Show the trend of revenue over time", "Top 5 region by revenue",
                    "Break revenue down by segment"]);
  assert.deepEqual(followUps(["order_date", "region", "amount"], [["2026-10-01", "EU", 5]]),
                   ["Show amount over time by region", "Top 5 region by amount",
                    "Compare amount with the previous period"]);
  assert.deepEqual(followUps(["customer_id", "name"], [[1, "Ana"]]), []);   // nothing to measure
  assert.deepEqual(followUps([], []), []);
});

test("an answer reads like Copilot: who answered, the answer, its result, its sources", () => {
  const html = harness(AssistantMessage, { m: ANSWER, onOpenCanvas() {}, onFollowUp() {} }).markup();
  assert.match(html, /class="assistant-avatar"[^>]*>◆<\/span><span class="assistant-name">Studio<\/span>/);
  assert.match(html, /<b>shop<\/b><span class="source-badge-type">Any SQL database · SQLite/);
  assert.match(html, /<strong>West<\/strong> leads with 2\.18M\./);
  // The result is a card that opens the canvas.
  assert.match(html, /class="result-card"[\s\S]*<b>Revenue by region<\/b>[\s\S]*2 rows · 3 columns[\s\S]*⤢ chart in canvas/);
  // Sources, numbered: the table the SQL read, then the document.
  assert.match(html, /references-label">Sources</);
  assert.match(html, /<button title="read by this answer&#x27;s SQL — click for its latest rows" class="chip chip-input reference"><span class="ref-num">1<\/span>▦ orders/);
  assert.match(html, /ref-num">2<\/span>📄 pricing\.pdf p\.3/);
  // One row of actions; downloads and email sit in the Export menu.
  assert.match(html, /role="toolbar" aria-label="Answer actions"/);
  for (const label of ["👍", "👎", "⧉ Copy", "‹/› SQL", "⟳ Refresh", "⏱ Automate"]) {
    assert.ok(html.includes(label), label);
  }
  const menu = html.slice(html.indexOf('class="export-menu"'), html.indexOf("</details>"));
  for (const label of ["⬇ CSV", "⬇ Excel", "⬇ Report", "✉ email report"]) {
    assert.ok(menu.includes(label), label);
  }
  assert.match(html, /AI-generated — check the SQL and sources/);
  // Follow-ups lead on from this answer.
  assert.match(html, /aria-label="Suggested follow-ups"/);
  assert.match(html, /<button class="followup">Top 5 region by revenue<\/button>/);
});

test("follow-ups appear only under the latest answer, and ask when clicked", () => {
  const older = harness(AssistantMessage, { m: ANSWER, onOpenCanvas() {} }).markup();
  assert.doesNotMatch(older, /followup/);
  const asked = [];
  const h = harness(AssistantMessage, { m: ANSWER, onOpenCanvas() {}, onFollowUp: (q) => asked.push(q) });
  button(h.tree(), "Break revenue down by segment").props.onClick();
  assert.deepEqual(asked, ["Break revenue down by segment"]);
  // A clarification or a proposed agent is answered first, not followed up.
  const clarify = harness(AssistantMessage, { m: { ...ANSWER, clarify: { prompt: "q", options: [] } },
                                              onOpenCanvas() {}, onFollowUp() {}, onClarify() {} }).markup();
  assert.doesNotMatch(clarify, /followup/);
});

test("copy puts the answer's text on the clipboard", async () => {
  const copied = [];
  const nav = globalThis.navigator;
  Object.defineProperty(globalThis, "navigator", { configurable: true,
    value: { clipboard: { writeText: async (t) => { copied.push(t); } } } });
  try {
    const h = harness(AssistantMessage, { m: ANSWER, onOpenCanvas() {} });
    await button(h.tree(), "⧉ Copy").props.onClick();
    assert.deepEqual(copied, ["**West** leads with 2.18M."]);
    assert.match(h.markup(), /copied/);
  } finally {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: nav });
  }
});
