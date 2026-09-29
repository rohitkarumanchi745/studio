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
    contents: 'export {AgentActivity, AssistantMessage} from "./src/components/Chat.jsx";',
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
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AgentActivity, AssistantMessage } = compiled.exports;
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
