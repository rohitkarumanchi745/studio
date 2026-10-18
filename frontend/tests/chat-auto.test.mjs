// Auto: Studio picks the source and tables from each question. The chat's
// toolbar switch (on by default, remembered per browser) hides the pickers and
// sends the question with auto: true; off, the pickers and today's request
// come back. An auto-routed answer says where it went.
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
    contents: `export {default as Chat, AssistantMessage, describeRoute, readAutoPref}
                 from "./src/components/Chat.jsx";`,
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
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatWorkflow|ChatPlatformRun)$/ },
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
const compiled = new Module(path.join(frontend, "chat-auto-test.cjs"));
compiled.filename = path.join(frontend, "chat-auto-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Chat, AssistantMessage, describeRoute, readAutoPref } = compiled.exports;

const html = (component, props) => renderToStaticMarkup(React.createElement(component, props));

const stored = {};
globalThis.localStorage = {
  getItem: (k) => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
  removeItem: (k) => { delete stored[k]; },
};

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

function toggle(chat, on) {
  const input = findElement(chat.render().tree,
    (el) => el.type === "input" && el.props.role === "switch");
  input.props.onChange({ target: { checked: on } });
}

const SOURCES = [{ name: "shop", configured: true, allowed: true },
                 { name: "support", configured: true, allowed: true }];
const respond = (url) => url === "/api/chat/background" ? { task_id: "t1", conversation_id: "c1" }
  : url === "/api/catalog/sources" ? SOURCES : url.endsWith("/tables") ? ["orders"] : [];

test("Auto is on by default: no pickers, and the question goes out to be routed", async () => {
  for (const k of Object.keys(stored)) delete stored[k];
  assert.equal(readAutoPref(), true);
  await withChatNetwork(respond, async (requests) => {
    const chat = chatHarness({ conversationId: null, onConversationCreated() {} });
    chat.render();
    chat.startEffects();
    await flushPromises();
    const { markup } = chat.render();
    assert.match(markup, /role="switch"[^>]*checked=""/);
    assert.match(markup, /Studio picks the source and tables from each question/);
    const toolbar = markup.slice(markup.indexOf('class="toolbar"'), markup.indexOf('class="messages"'));
    assert.doesNotMatch(toolbar, /<select|tpicker-btn/);
    assert.match(markup, /placeholder="Ask about your data — Studio finds the source and tables…"/);
    // Hidden pickers fetch no table list (and so report no error about one).
    assert.equal(requests.filter((r) => r.url.endsWith("/tables")).length, 0);
    await ask(chat, "total amount of orders by region");
    const sent = requests.find((r) => r.url === "/api/chat/background").body;
    assert.equal(sent.auto, true);
    assert.equal(sent.source, "*");
    assert.equal(sent.table, "*");
    assert.equal(sent.tables, undefined);
  });
});

test("Auto off brings the pickers back, sends the picked source, and is remembered", async () => {
  for (const k of Object.keys(stored)) delete stored[k];
  await withChatNetwork(respond, async (requests) => {
    const chat = chatHarness({ conversationId: null, onConversationCreated() {} });
    chat.render();
    toggle(chat, false);
    chat.render();
    chat.startEffects();
    await flushPromises();
    const { markup } = chat.render();
    assert.match(markup, /<option value="\*">✳ all sources — multi-agent<\/option>/);
    assert.match(markup, /<option value="shop">shop<\/option>/);
    assert.match(markup, /tpicker-btn/);
    assert.doesNotMatch(markup, /role="switch"[^>]*checked=""/);
    assert.equal(stored.studio_auto_source, "0");
    assert.equal(readAutoPref(), false);
    await ask(chat, "total amount of orders by region");
    const sent = requests.find((r) => r.url === "/api/chat/background").body;
    assert.equal(sent.auto, undefined);
    assert.equal(sent.source, "demo");
  });
  // A new chat opens the way it was left.
  assert.doesNotMatch(chatHarness({ conversationId: null }).render().markup,
                      /role="switch"[^>]*checked=""/);
  const chat = chatHarness({ conversationId: null });
  chat.render();
  toggle(chat, true);
  chat.render();
  chat.startEffects();
  assert.equal(stored.studio_auto_source, "1");
});

test("blocked storage leaves Auto on", () => {
  const saved = globalThis.localStorage;
  globalThis.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  try {
    assert.equal(readAutoPref(), true);
  } finally {
    globalThis.localStorage = saved;
  }
});

test("an auto-routed answer says where it went", () => {
  assert.equal(describeRoute({ mode: "single", source: "shop", tables: ["orders", "customers"],
                               terms: ["amount", "net_sale"] }),
               "picked shop · orders, customers — matched amount, net sale");
  assert.equal(describeRoute({ mode: "single", source: "support", tables: [], terms: [], kept: true }),
               "stayed with support");
  assert.equal(describeRoute({ mode: "multi", sources: ["shop", "support", "crm"], terms: ["ticket"] }),
               "asked shop, support and crm — matched ticket");
  assert.equal(describeRoute({ mode: "multi", sources: ["shop", "shop_eu"], terms: [] }),
               "asked shop and shop_eu");
  assert.match(describeRoute({ mode: "all" }), /^asked every source/);

  const m = { text: "EU leads.", source: "shop", table: "*", model: "x", columns: [], rows: [],
              panels: [], auto_route: { mode: "single", source: "shop", tables: ["orders"],
                                        terms: ["amount"], why: "shop has what the question asks for" } };
  const markup = html(AssistantMessage, { m, onOpenCanvas() {} });
  assert.match(markup, /class="auto-route-row" title="shop has what the question asks for"/);
  assert.match(markup, /✳ Auto<\/span> picked shop · orders — matched amount/);
  assert.doesNotMatch(html(AssistantMessage, { m: { ...m, auto_route: undefined }, onOpenCanvas() {} }),
                      /auto-route-row/);
});
