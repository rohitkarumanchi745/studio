// The composer, as ChatGPT draws it — a message that grows, Enter to send,
// Shift+Enter for a new line, ＋ for the Airflow and platform options, the
// model as a pill — and ■ Stop while an answer is being worked on.
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
    contents: `export {default as Chat, AssistantMessage} from "./src/components/Chat.jsx";`,
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
const compiled = new Module(path.join(frontend, "composer-stop-test.cjs"));
compiled.filename = path.join(frontend, "composer-stop-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Chat, AssistantMessage } = compiled.exports;

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
    (el) => el.type === "textarea" && el.props.className === "pill-input");
  input.props.onChange({ target: { value: text } });
  const form = findElement(chat.render().tree,
    (el) => el.type === "form" && el.props.className === "composer chat-composer");
  await form.props.onSubmit({ preventDefault() {} });
}

const TEXTAREA = (el) => el.type === "textarea" && el.props.className === "pill-input";

async function withServer(respond, run) {
  const original = { fetch: globalThis.fetch, setTimeout: globalThis.setTimeout };
  const sent = [];
  globalThis.fetch = async (url, options = {}) => {
    sent.push({ url, method: options.method || "GET", body: options.body && JSON.parse(options.body) });
    const answer = await respond(url, options);
    return { ok: true, status: 200, json: async () => answer };
  };
  globalThis.setTimeout = () => 0;                 // no polling here
  try { await run(sent); } finally { Object.assign(globalThis, original); }
}

test("the composer: a growing message, ＋ options, a model pill, a round send", () => {
  const { markup, tree } = chatHarness({ conversationId: null }).render();
  assert.match(markup, /<textarea class="pill-input" rows="1" aria-label="Message Studio"/);
  assert.match(markup, /class="composer-plus-btn" aria-label="More options"/);
  const menu = markup.slice(markup.indexOf('class="composer-menu"'), markup.indexOf("</details>"));
  assert.match(menu, /Build an approval-gated Airflow DAG from this prompt/);
  assert.match(menu, /Run on Airflow \/ another platform/);
  assert.match(markup, /class="model-mini" title="Which AI model answers" aria-label="Model"/);
  assert.match(markup, /aria-label="Send message" disabled="">↑</);     // nothing typed yet
  assert.match(markup, /Studio can make mistakes/);
  assert.doesNotMatch(markup, /aria-label="Stop"/);
  // The D3 window keeps the message and the model, without the pipeline options.
  const visual = chatHarness({ conversationId: null, visualMode: true }).render().markup;
  assert.doesNotMatch(visual, /composer-plus|Airflow/);
  assert.ok(findElement(tree, TEXTAREA));
});

test("Enter sends; Shift+Enter is a new line", async () => {
  await withServer((url) => url === "/api/chat/background" ? { task_id: "t1", conversation_id: "c1" } : [],
    async (sent) => {
      const chat = chatHarness({ conversationId: null, onConversationCreated() {} });
      findElement(chat.render().tree, TEXTAREA).props.onChange({ target: { value: "revenue by region" } });
      let prevented = 0;
      findElement(chat.render().tree, TEXTAREA).props.onKeyDown(
        { key: "Enter", shiftKey: true, preventDefault: () => { prevented++; } });
      assert.equal(prevented, 0);
      assert.equal(sent.filter((r) => r.url === "/api/chat/background").length, 0);
      await findElement(chat.render().tree, TEXTAREA).props.onKeyDown(
        { key: "Enter", shiftKey: false, preventDefault: () => { prevented++; } });
      await flushPromises();
      assert.ok(prevented >= 1);                        // Enter did not add a line
      assert.equal(sent.find((r) => r.url === "/api/chat/background").body.prompt, "revenue by region");
    });
});

test("the Airflow option shows as a chip in the composer, removable", () => {
  const chat = chatHarness({ conversationId: null });
  const option = findElement(chat.render().tree, (el) => el.type === "input" && el.props.type === "checkbox"
    && el.props.onChange && String(el.props.checked) === "false");
  option.props.onChange({ target: { checked: true } });
  const { markup, tree } = chat.render();
  assert.match(markup, /⚙ Airflow DAG/);
  assert.match(markup, /aria-label="Build Airflow DAG"/);
  findElement(tree, (el) => el.type === "button" && el.props["aria-label"] === "Don't build a DAG")
    .props.onClick();
  assert.doesNotMatch(chat.render().markup, /⚙ Airflow DAG/);
});

test("■ Stop replaces send while an answer is worked on, and stops it", async () => {
  await withServer((url) => url === "/api/chat/background" ? { task_id: "t7", conversation_id: "c7" }
    : url === "/api/tasks/t7/stop" ? { status: "stopped", task_id: "t7" } : [],
    async (sent) => {
      const chat = chatHarness({ conversationId: "c7", onConversationCreated() {} });
      await ask(chat, "revenue by region");
      const { markup, tree } = chat.render();
      assert.match(markup, /aria-label="Stop" title="Stop this answer"/);
      assert.doesNotMatch(markup, /aria-label="Send message"/);
      await findElement(tree, (el) => el.type === "button" && el.props["aria-label"] === "Stop").props.onClick();
      await flushPromises();
      assert.equal(sent.filter((r) => r.url === "/api/tasks/t7/stop" && r.method === "POST").length, 1);
      assert.ok(sent.some((r) => r.url === "/api/conversations/c7/messages"));   // the stopped reply
      const after = chat.render().markup;
      assert.doesNotMatch(after, /aria-label="Stop"/);
      assert.match(after, /aria-label="Send message"/);
    });
});

test("Stop pressed before the task has an id stops it as soon as it starts", async () => {
  let release;
  const pending = new Promise((r) => { release = r; });
  await withServer(async (url) => url === "/api/chat/background"
    ? (await pending, { task_id: "t8", conversation_id: "c8" })
    : url === "/api/tasks/t8/stop" ? { status: "stopped", task_id: "t8" } : [],
    async (sent) => {
      const chat = chatHarness({ conversationId: "c8", onConversationCreated() {} });
      findElement(chat.render().tree, TEXTAREA).props.onChange({ target: { value: "revenue" } });
      const form = findElement(chat.render().tree, (el) => el.type === "form");
      const sending = form.props.onSubmit({ preventDefault() {} });
      await flushPromises();
      await findElement(chat.render().tree, (el) => el.type === "button" && el.props["aria-label"] === "Stop")
        .props.onClick();
      assert.equal(sent.filter((r) => r.url.endsWith("/stop")).length, 0);   // no id yet
      release();
      await sending;
      await flushPromises();
      assert.equal(sent.filter((r) => r.url === "/api/tasks/t8/stop").length, 1);
    });
});

test("a stopped answer is a quiet line, not an answer", () => {
  const out = renderToStaticMarkup(React.createElement(AssistantMessage, { onOpenCanvas() {},
    m: { mode: "stopped", text: "You stopped this answer before it finished.", columns: [], rows: [] } }));
  assert.match(out, /class="answer-stopped" role="note">⏹ You stopped this answer before it finished\./);
  assert.doesNotMatch(out, /Answer actions|AI-generated|assistant-meta/);
});
