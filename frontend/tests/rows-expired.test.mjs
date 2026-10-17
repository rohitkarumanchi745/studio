// An answer whose stored result rows expired says so — instead of drawing an
// empty chart — and re-running its SQL brings current data back into view.
// Administrators see the first removal coming and can back it up first.
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
    contents: `export {AssistantMessage} from "./src/components/Chat.jsx";
               export {Retention} from "./src/components/Governance.jsx";`,
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
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatPipeline|ChatWorkflow|ChatPlatformRun|ChartView)$/ },
                    ({ path: p }) => ({ path: p.slice(2), namespace: "child" }));
        b.onLoad({ filter: /.*/, namespace: "child" }, ({ path: name }) => ({
          contents: `export default function ${name}() { return null; }
                     export function PlatformComposer() { return null; }`,
          loader: "jsx",
        }));
      },
    },
  ],
});
const compiled = new Module(path.join(frontend, "rows-expired-test.cjs"));
compiled.filename = path.join(frontend, "rows-expired-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AssistantMessage, Retention } = compiled.exports;

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

const EXPIRED = {
  text: "EU leads.", sql: "SELECT region, revenue FROM sales", source: "demo", table: "sales",
  columns: ["region", "revenue"], rows: [], chart: { type: "bar" }, message_id: "m1",
  panels: [{ sql: "SELECT 1", columns: ["region"], rows: [], chart: { type: "bar" } }],
  rows_purged: true,
};

test("an expired answer says so, and offers no empty chart", () => {
  const html = harness(AssistantMessage, { m: EXPIRED, onOpenCanvas() {} }).markup();
  assert.match(html, /class="rows-expired" role="note"/);
  assert.match(html, /Result rows expired\./);
  assert.match(html, /re-run for current data/);
  assert.doesNotMatch(html, /chart-open|⤢ canvas/);            // would open empty charts
  // A live answer is unchanged.
  const live = harness(AssistantMessage, { m: { ...EXPIRED, rows_purged: undefined,
                                               rows: [["EU", 125]] }, onOpenCanvas() {} }).markup();
  assert.doesNotMatch(live, /rows-expired/);
  assert.match(live, /chart-open/);
});

test("re-running it fetches current rows and opens them, not the stored message", async () => {
  await withNetwork(() => ({ columns: ["region", "revenue"], rows: [["EU", 130], ["US", 90]] }),
    async (sent) => {
      const opened = [];
      const msg = harness(AssistantMessage, { m: EXPIRED, onOpenCanvas: (v) => opened.push(v) });
      await button(msg.tree(), "re-run for current data").props.onClick();
      assert.deepEqual(sent.map((r) => [r.url, r.body]),
        [["/api/chat/rerun", { source: "demo", sql: EXPIRED.sql }]]);
      assert.equal(opened.length, 1);
      assert.deepEqual(opened[0].rows, [["EU", 130], ["US", 90]]);
      assert.equal(opened[0].panels, undefined);                 // not the purged panels
      assert.equal(opened[0].message_id, undefined);             // edits stay local
      const after = msg.markup();
      assert.doesNotMatch(after, /rows-expired/);
      assert.match(after, /chart-open/);
      // Reopening shows the fresh rows again, never the purged ones.
      button(msg.tree(), "chart in canvas").props.onClick();
      assert.deepEqual(opened[1].rows, [["EU", 130], ["US", 90]]);
      assert.equal(opened[1].panels, undefined);                 // the purged panels never return
      assert.equal(opened[1].message_id, undefined);
    });
});

test("in the D3 window nothing is drawn until it is re-run", async () => {
  const html = harness(AssistantMessage, { m: EXPIRED, visualMode: true }).markup();
  assert.doesNotMatch(html, /msg-visuals/);
  assert.match(html, /rows-expired/);
});

test("administrators see the first removal coming, and can back it up first", async () => {
  const status = {
    enabled: true, days: 90, tick_seconds: 3600, eligible: 12, oldest_eligible_at: 1700000000,
    first_purge_pending: true, first_purge_at: 1792800000, acknowledged_by: null,
    runs: [{ started_at: 1792195000, status: "notice", eligible: 12, purged: 0, error: null }],
  };
  await withNetwork((url) => (url.endsWith("/acknowledge")
    ? { ...status, acknowledged_by: "ops@studio.test" } : status), async (sent) => {
    const r = harness(Retention, {});
    r.effects();
    for (let i = 0; i < 20; i++) await Promise.resolve();
    const html = r.markup();
    assert.match(html, /Result rows of 12 answers<\/b>/);
    assert.match(html, /will be removed\s+on /);
    assert.match(html, /download these rows/);
    assert.match(html, /backup taken — start now/);
    assert.match(html, /postpone 7 days/);
    assert.match(html, /<td>notice<\/td>/);
    await button(r.tree(), "backup taken").props.onClick();
    assert.deepEqual(sent.at(-1), { url: "/api/admin/retention/acknowledge", method: "POST", body: {} });
    assert.match(r.markup(), /Backup confirmed by ops@studio\.test/);
    assert.doesNotMatch(r.markup(), /backup taken — start now/);
  });
});
