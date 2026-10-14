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
    contents: 'export {default as Chat, AgentActivity, AssistantMessage} from "./src/components/Chat.jsx";',
    resolveDir: frontend,
    loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
  plugins: [{
    name: "stub-chat-children",
    setup(bundler) {
      bundler.onResolve({filter: /^\.\/(Canvas|ChartView|SqlLab|ChatPipeline|ChatWorkflow|ChatPlatformRun)$/},
        ({path: specifier}) => ({path: specifier, namespace: "chat-child"}));
      bundler.onLoad({filter: /.*/, namespace: "chat-child"}, () => ({
        contents: "export default function Stub() { return null; } export function PlatformComposer() { return null; }",
        loader: "jsx",
      }));
    },
  }],
});
const compiled = new Module(path.join(frontend, "agent-activity-test.cjs"));
compiled.filename = path.join(frontend, "agent-activity-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...args) => hooks ? hooks.useState(...args) : React.useState(...args),
  useRef: (...args) => hooks ? hooks.useRef(...args) : React.useRef(...args),
  useEffect: (...args) => hooks ? hooks.useEffect(...args) : React.useEffect(...args),
  useMemo: (...args) => hooks ? hooks.useMemo(...args) : React.useMemo(...args),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Chat, AgentActivity, AssistantMessage } = compiled.exports;
const html = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const graph = {nodes: [
  {id: "sales", agent: "Sales agent", status: "ok", depends_on: []},
  {id: "spend", agent: "Spend agent", status: "failed", depends_on: ["sales"]},
  {id: "__reason__", agent: "Aggregator", status: "ok", depends_on: ["spend"]},
]};

function activityHarness(props) {
  const values = [];
  let cursor = 0;
  const stateHooks = {
    useState(initial) {
      const index = cursor++;
      if (!(index in values)) values[index] = typeof initial === "function" ? initial() : initial;
      return [values[index], (next) => { values[index] = typeof next === "function" ? next(values[index]) : next; }];
    },
    useRef(initial) {
      const index = cursor++;
      if (!(index in values)) values[index] = {current: initial};
      return values[index];
    },
    useEffect() {},
  };
  return {
    render() {
      cursor = 0;
      hooks = stateHooks;
      let tree;
      try { tree = AgentActivity(props); } finally { hooks = null; }
      return {tree, markup: renderToStaticMarkup(tree)};
    },
  };
}

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
      if (!(index in values)) values[index] = {current: initial};
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
      return {tree, markup: renderToStaticMarkup(tree)};
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
  const originalStorage = globalThis.localStorage;
  const originalSetTimeout = globalThis.setTimeout;
  const requests = [];
  const timers = [];
  globalThis.localStorage = {getItem() { return null; }, setItem() {}, removeItem() {}};
  globalThis.fetch = async (url, options) => {
    requests.push({url, options});
    const body = responseFor(url, options);
    return {ok: true, status: 200, json: async () => body};
  };
  globalThis.setTimeout = (fn, delay) => { timers.push({fn, delay}); return timers.length; };
  try { await run({requests, timers}); }
  finally {
    globalThis.fetch = originalFetch;
    globalThis.localStorage = originalStorage;
    globalThis.setTimeout = originalSetTimeout;
  }
}

test("live activity shows current and earlier recorded events with an elapsed timer", () => {
  const rendered = html(AgentActivity, {running: true, startedAt: Date.now() - 3200, steps: [
    {t: 10, label: "planning sources"}, {t: 12, label: "Sales agent: querying"},
  ]});
  assert.match(rendered, /<details[^>]*open=""/);
  assert.match(rendered, /Agent activity/);
  assert.match(rendered, /Sales agent: querying/);
  assert.match(rendered, /planning sources/);
  assert.match(rendered, /elapsed/);
  assert.doesNotMatch(rendered, /thought|reasoning/i);
});

test("a reopened answer loads owner-only events on expansion and shows graph dependencies", async () => {
  const originalFetch = globalThis.fetch;
  const originalStorage = globalThis.localStorage;
  const requests = [];
  globalThis.localStorage = {getItem() { return null; }};
  globalThis.fetch = async (url) => {
    requests.push(url);
    return {ok: true, status: 200, json: async () => ({steps: [
      {t: 100, label: "planned 2 agents"}, {t: 105, label: '<script>failed</script>'},
    ]})};
  };
  try {
    const panel = activityHarness({taskId: "task 1", graph});
    const before = panel.render();
    assert.match(before.markup, /View recorded steps/);
    assert.doesNotMatch(before.markup, /Sales agent|planned 2 agents/);
    assert.deepEqual(requests, []);
    await before.tree.props.onToggle({currentTarget: {open: true}});
    const after = panel.render().markup;
    assert.deepEqual(requests, ["/api/tasks/task%201"]);
    assert.match(after, /planned 2 agents/);
    assert.match(after, /Agent graph/);
    assert.match(after, /Spend agent/);
    assert.match(after, /After Sales agent/);
    assert.match(after, /Failed/);
    assert.doesNotMatch(after, /<script>/);
    assert.match(after, /&lt;script&gt;failed&lt;\/script&gt;/);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.localStorage = originalStorage;
  }
});

test("shared messages without a task ID reveal neither steps nor the graph", () => {
  const owner = html(AssistantMessage, {
    m: {text: "An answer", source: "demo", table: "*", activity_task_id: "owned", graph},
  });
  assert.match(owner, /Agent activity/);
  assert.match(owner, /View recorded steps/);
  assert.doesNotMatch(owner, /Sales agent|Spend agent/);

  const rendered = html(AssistantMessage, {
    m: {text: "A shared answer", source: "demo", table: "*", graph},
  });
  assert.match(rendered, /A shared answer/);
  assert.doesNotMatch(rendered, /Agent activity|Sales agent|Spend agent/);
});

test("a denied task lookup keeps the graph hidden", async () => {
  const originalFetch = globalThis.fetch;
  const originalStorage = globalThis.localStorage;
  globalThis.localStorage = {getItem() { return null; }};
  globalThis.fetch = async () => ({ok: false, status: 404, json: async () => ({detail: "Not found"})});
  try {
    const panel = activityHarness({taskId: "private", graph});
    await panel.render().tree.props.onToggle({currentTarget: {open: true}});
    const rendered = panel.render().markup;
    assert.match(rendered, /Activity details aren&#x27;t available/);
    assert.doesNotMatch(rendered, /Sales agent|Spend agent/);
  } finally {
    globalThis.fetch = originalFetch;
    globalThis.localStorage = originalStorage;
  }
});

test("a failed task poll keeps recorded steps in an expandable panel above the error", async () => {
  const steps = [
    {t: 100, label: "Planning the query"},
    {t: 103, label: "Sales agent: query failed"},
  ];
  await withChatNetwork((url) => {
    if (url === "/api/chat/background") return {task_id: "failed-live", conversation_id: "conv-live"};
    if (url === "/api/tasks/failed-live") return {status: "failed", steps, error: "The warehouse rejected the query."};
    if (url === "/api/conversations/conv-live/messages") return [];
    return [];
  }, async ({requests, timers}) => {
    const chat = chatHarness({conversationId: "conv-live", onConversationCreated() {}});
    const input = findElement(chat.render().tree, (element) => element.type === "input" && element.props.className === "pill-input");
    input.props.onChange({target: {value: "Show sales"}});
    const form = findElement(chat.render().tree, (element) => element.type === "form" && element.props.className === "composer chat-composer");
    await form.props.onSubmit({preventDefault() {}});
    assert.deepEqual(timers.map(({delay}) => delay), [1500]);
    await timers[0].fn();
    await flushPromises();

    const {tree, markup} = chat.render();
    const activity = findElement(tree, (element) => element.type === AgentActivity && element.props.taskId === "failed-live");
    assert.ok(activity, "failed run has an activity panel");
    assert.equal(activity.props.failed, true);
    assert.deepEqual(activity.props.steps, steps);
    assert.equal(activity.props.graph, undefined);
    assert.match(markup, /Run failed · 2 events/);
    assert.match(markup, /Sales agent: query failed/);
    assert.match(markup, /The warehouse rejected the query/);
    assert.ok(markup.indexOf("Agent activity") < markup.indexOf('class="error"'));
    assert.doesNotMatch(markup, /Agent graph/);
    assert.equal(requests.filter(({url}) => url === "/api/tasks/failed-live").length, 1);
  });
});

test("reopening a failed chat fetches owner-only steps and places activity above the error", async () => {
  const steps = [
    {t: 200, label: "Planning two sources"},
    {t: 204, label: "Spend agent: permission denied"},
  ];
  await withChatNetwork((url) => {
    if (url === "/api/conversations/conv-reopen/messages") return [{id: "user-1", role: "user", content: {text: "Compare spend"}}];
    if (url === "/api/conversations/conv-reopen/task") return {id: "failed-reopen", status: "failed", error: "The run could not finish."};
    if (url === "/api/tasks/failed-reopen") return {status: "failed", steps, error: "The spend source denied access.", graph};
    return [];
  }, async ({requests}) => {
    const chat = chatHarness({conversationId: "conv-reopen", onConversationCreated() {}});
    chat.render();
    chat.startEffects();
    await flushPromises();

    const {tree, markup} = chat.render();
    const activity = findElement(tree, (element) => element.type === AgentActivity && element.props.taskId === "failed-reopen");
    assert.ok(activity, "reopened failed run has an activity panel");
    assert.equal(activity.props.failed, true);
    assert.deepEqual(activity.props.steps, steps);
    assert.equal(activity.props.graph, undefined);
    assert.deepEqual(requests.filter(({url}) => url.includes("conv-reopen") || url === "/api/tasks/failed-reopen").map(({url}) => url), [
      "/api/conversations/conv-reopen/messages",
      "/api/conversations/conv-reopen/task",
      "/api/tasks/failed-reopen",
    ]);
    assert.match(markup, /Spend agent: permission denied/);
    assert.match(markup, /The spend source denied access/);
    assert.ok(markup.indexOf("Agent activity") < markup.indexOf('class="error"'));
    assert.doesNotMatch(markup, /Agent graph|Sales agent/);

    await activityHarness(activity.props).render().tree.props.onToggle({currentTarget: {open: true}});
    assert.equal(requests.filter(({url}) => url === "/api/tasks/failed-reopen").length, 1);
  });
});
