// The D3 & artifacts window: the chat in visual mode. Every answer is drawn
// right in the thread with D3 alone, or as a sandboxed custom visual.
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
    contents: `export {default as Chat, AssistantMessage, isVisualConversation}
                 from "./src/components/Chat.jsx";
               export {default as ChartView} from "./src/components/ChartView.jsx";
               export {D3_TYPES, VISUAL_TYPES} from "./src/components/renderers/D3View.jsx";`,
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
      // The canvas and the pipeline/platform panels are not under test; each
      // stub is named after what it replaces so a test can look for it.
      name: "stub-chat-children",
      setup(b) {
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatPipeline|ChatWorkflow|ChatPlatformRun)$/ },
                    ({ path: p }) => ({ path: p.slice(2), namespace: "chat-child" }));
        b.onLoad({ filter: /.*/, namespace: "chat-child" }, ({ path: name }) => ({
          contents: `export default function ${name}() { return null; }
                     export function PlatformComposer() { return null; }`,
          loader: "jsx",
        }));
      },
    },
  ],
});
const compiled = new Module(path.join(frontend, "visual-chat-test.cjs"));
compiled.filename = path.join(frontend, "visual-chat-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Chat, AssistantMessage, isVisualConversation, ChartView, D3_TYPES, VISUAL_TYPES } =
  compiled.exports;

globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const html = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const offered = (markup) => [...markup.matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]);

const COLUMNS = ["region", "revenue"];
const ROWS = [["West", 120], ["East", 90], ["North", 60]];
const BAR = { type: "bar", x: "region", y: ["revenue"], title: "By region" };

test("a D3-only chart has no engine picker and offers D3 without the line chart", () => {
  const markup = html(ChartView, { columns: COLUMNS, rows: ROWS, chart: BAR, d3Only: true });
  assert.match(markup, /class="echart d3-chart"/);
  assert.match(markup, /class="artifact-badge"[^>]*>D3</);
  assert.doesNotMatch(markup, /ECharts|Plotly|Altair/);
  assert.deepEqual(new Set(offered(markup)), VISUAL_TYPES);
  assert.deepEqual([...D3_TYPES].filter((t) => !VISUAL_TYPES.has(t)), ["line"]);

  // The ordinary chart keeps its engine picker and the whole catalog.
  const ordinary = html(ChartView, { columns: COLUMNS, rows: ROWS, chart: BAR });
  for (const engine of ["echarts", "plotly", "vega", "d3"]) assert.ok(offered(ordinary).includes(engine));
  for (const type of ["treemap", "sankey", "table", "kpi", "line"]) assert.ok(offered(ordinary).includes(type));
});

test("a line chart opens as a D3 area; a type D3 lacks as a D3 bar", () => {
  const line = html(ChartView, { columns: COLUMNS, rows: ROWS, d3Only: true,
                                 chart: { ...BAR, type: "line" } });
  assert.match(line, /<option value="area" selected=""/);
  assert.doesNotMatch(line, /<option value="line"/);
  assert.match(line, /d3-chart/);

  for (const type of ["table", "treemap", "kpi", "sankey"]) {
    const markup = html(ChartView, { columns: COLUMNS, rows: ROWS, d3Only: true,
                                     chart: { ...BAR, type } });
    assert.match(markup, /<option value="bar" selected=""/, type);
    assert.match(markup, /d3-chart/, type);
    assert.doesNotMatch(markup, /<table/, type);
  }
});

test("data no D3 chart fits gets a note, not a table", () => {
  const text = { columns: ["region", "segment"], rows: [["West", "A"], ["East", "B"]] };
  const markup = html(ChartView, { ...text, chart: BAR, d3Only: true });
  assert.match(markup, /doesn’t fit this data/);
  assert.match(markup, /Pick another D3 chart above, or ask for a custom visual/);
  assert.doesNotMatch(markup, /<table|d3-chart/);

  // Outside the window the table still stands in.
  assert.match(html(ChartView, { ...text, chart: BAR }), /<table/);
});

const ANSWER = {
  text: "West leads.", source: "demo", table: "orders", sql: "SELECT region, revenue FROM orders",
  columns: COLUMNS, rows: ROWS, chart: BAR, visual_mode: true,
  panels: [
    { sql: "SELECT region, revenue FROM orders", columns: COLUMNS, rows: ROWS, engine: "d3",
      chart: { ...BAR, type: "circle_pack", title: "Share" } },
    { sql: "SELECT region, revenue FROM orders", columns: COLUMNS, rows: ROWS, engine: "d3",
      chart: { type: "bar", title: "Rings" },
      visual: { title: "Rings", code: "root.append('svg')", explanation: "One ring per region." } },
  ],
};

test("the window draws every panel in the thread, with D3 or a sandboxed visual", () => {
  const markup = html(AssistantMessage, { m: ANSWER, visualMode: true, onOpenCanvas() {} });
  assert.equal(markup.match(/class="msg-visual"/g)?.length, 2);
  assert.match(markup, /class="echart d3-chart echart-tall"/);
  assert.match(markup, /<iframe[^>]*sandbox="allow-scripts"/);
  assert.match(markup, /One ring per region/);
  assert.doesNotMatch(markup, /in canvas|⤢ canvas|ECharts|Plotly|Altair/);
  // The visual is the answer: right under its text, above the SQL and the rest.
  assert.ok(markup.indexOf("West leads.") < markup.indexOf('class="msg-visual"'));
  assert.ok(markup.indexOf('class="msg-visual"') < markup.indexOf("edit / verify / save SQL"));

  // A single-chart answer (no panels) is drawn too.
  const single = html(AssistantMessage, { m: { ...ANSWER, panels: [] }, visualMode: true });
  assert.equal(single.match(/class="msg-visual"/g)?.length, 1);
  assert.match(single, /d3-chart/);
});

test("the ordinary chat still sends charts to the canvas", () => {
  const markup = html(AssistantMessage, { m: ANSWER, onOpenCanvas() {} });
  assert.match(markup, /⤢ 2 charts in canvas/);
  assert.doesNotMatch(markup, /msg-visual|d3-chart|<iframe/);
});

test("a conversation is a D3 & artifacts one by its 🎨 title", () => {
  assert.equal(isVisualConversation({ title: "🎨 revenue by region" }), true);
  assert.equal(isVisualConversation({ title: "revenue by region" }), false);
  assert.equal(isVisualConversation({ title: "Platform pipeline" }), false);
  assert.equal(isVisualConversation(undefined), false);
  assert.equal(isVisualConversation({ title: null }), false);
});

// ── The window itself ────────────────────────────────────────────────────

function chatHarness(props) {
  const values = [];
  const effects = [];
  let cursor = 0;
  const stateHooks = {
    useState(initial) {
      const index = cursor++;
      if (!(index in values)) values[index] = typeof initial === "function" ? initial() : initial;
      return [values[index], (next) => { values[index] = typeof next === "function" ? next(values[index]) : next; }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in values)) values[index] = { current: initial };
      return values[index];
    },
    useMemo(factory) { return factory(); },
    useEffect(effect) { effects.push(effect); },
  };
  return {
    render() {
      cursor = 0;
      effects.length = 0;
      hooks = stateHooks;
      let tree;
      try { tree = Chat(props); } finally { hooks = null; }
      return { tree, markup: renderToStaticMarkup(tree) };
    },
    startEffects() { effects.forEach((effect) => effect()); },
  };
}

function findElement(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  return predicate(node) ? node : findElement(node.props.children, predicate);
}

async function flushPromises() {
  for (let i = 0; i < 12; i++) await Promise.resolve();
}

async function withChatNetwork(responseFor, run) {
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : undefined });
    return { ok: true, status: 200, json: async () => responseFor(url) };
  };
  globalThis.setTimeout = () => 0;            // no task polling here
  try { await run(requests); }
  finally {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
  }
}

async function ask(chat, text) {
  const input = findElement(chat.render().tree,
    (el) => el.type === "input" && el.props.className === "pill-input");
  input.props.onChange({ target: { value: text } });
  const form = findElement(chat.render().tree,
    (el) => el.type === "form" && el.props.className === "composer chat-composer");
  await form.props.onSubmit({ preventDefault() {} });
}

test("the window opens as a prompt for a visual, without the pipeline controls", () => {
  const { markup } = chatHarness({ conversationId: null, visualMode: true, onOpenCharts() {} }).render();
  assert.match(markup, /🎨 What do you want to see\?/);
  assert.match(markup, /Every answer is drawn\s+with D3/);
  assert.match(markup, /🎨 D3 &amp; artifacts/);
  assert.match(markup, /⊞ your charts/);
  assert.match(markup, /Describe the visualization/);
  assert.doesNotMatch(markup, /Run on Airflow|composer-pipeline-option/);
  // Any source can be picked, all sources included.
  assert.match(markup, /<option value="\*">✳ all sources — multi-agent<\/option>/);

  const ordinary = chatHarness({ conversationId: null }).render().markup;
  assert.match(ordinary, /What do you want to know\?/);
  assert.match(ordinary, /Run on Airflow/);
  assert.doesNotMatch(ordinary, /D3 &amp; artifacts|your charts/);
});

test("a question asked in the window is sent as a visual turn", async () => {
  await withChatNetwork((url) => url === "/api/chat/background"
    ? { task_id: "t1", conversation_id: "c1" } : [], async (requests) => {
    const created = [];
    const chat = chatHarness({ conversationId: null, visualMode: true,
                               onConversationCreated: (id) => created.push(id) });
    await ask(chat, "revenue by region as circle packing");
    const sent = requests.find((r) => r.url === "/api/chat/background");
    assert.equal(sent.body.prompt, "revenue by region as circle packing");
    assert.equal(sent.body.visual, true);
    assert.equal(sent.body.pipeline_action, undefined);
    assert.deepEqual(created, ["c1"]);
  });

  await withChatNetwork((url) => url === "/api/chat/background"
    ? { task_id: "t2", conversation_id: "c2" } : [], async (requests) => {
    const chat = chatHarness({ conversationId: null, onConversationCreated() {} });
    await ask(chat, "revenue by region");
    const sent = requests.find((r) => r.url === "/api/chat/background");
    assert.equal(sent.body.visual, undefined);
  });
});

test("choosing a source for an ambiguous question stays a visual turn", async () => {
  const stored = [
    { id: "u1", role: "user", content: { text: "revenue by region" } },
    { id: "a1", role: "assistant", content: { mode: "clarify", text: "Which source?" } },
  ];
  await withChatNetwork((url) => url === "/api/conversations/c3/messages" ? stored
    : url === "/api/conversations/c3/task" ? { status: "done" }
    : url === "/api/chat/background" ? { task_id: "t3", conversation_id: "c3" } : [],
  async (requests) => {
    const chat = chatHarness({ conversationId: "c3", visualMode: true, onConversationCreated() {} });
    chat.render();
    chat.startEffects();
    await flushPromises();
    const clarify = findElement(chat.render().tree, (el) => el.type === AssistantMessage);
    await clarify.props.onClarify("revenue by region", { source: "demo" });
    const sent = requests.find((r) => r.url === "/api/chat/background");
    assert.equal(sent.body.source, "demo");
    assert.equal(sent.body.visual, true);
  });
});

test("reopening a window conversation keeps its charts in the thread, not the canvas", async () => {
  const stored = [
    { id: "u1", role: "user", content: { text: "revenue by region" } },
    { id: "a1", role: "assistant", content: ANSWER },
  ];
  for (const visualMode of [true, false]) {
    await withChatNetwork((url) => url === "/api/conversations/c9/messages" ? stored
      : url === "/api/conversations/c9/task" ? { status: "done" } : [], async () => {
      const chat = chatHarness({ conversationId: "c9", visualMode, onConversationCreated() {} });
      chat.render();
      chat.startEffects();
      await flushPromises();
      const { tree, markup } = chat.render();
      const canvas = findElement(tree, (el) => typeof el.type === "function" && "onNewVersion" in el.props);
      if (visualMode) {
        assert.equal(canvas, null);
        assert.doesNotMatch(markup, /with-canvas/);
        assert.match(markup, /class="msg-visual"/);
      } else {
        assert.ok(canvas, "the ordinary chat restores the canvas");
        assert.match(markup, /with-canvas/);
      }
    });
  }
});
