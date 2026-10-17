// How many agents a task is using: live while it runs (from the agents its
// progress steps name), and on the finished answer (from its saved roster).
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
const bundle = await build({
  stdin: {
    contents: `export {AgentActivity, AssistantMessage, agentSummary} from "./src/components/Chat.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
  plugins: [{
    name: "stub-chat-children",
    setup(b) {
      b.onResolve({ filter: /^\.\/(Canvas|ChartView|SqlLab|ChatPipeline|ChatWorkflow|ChatPlatformRun)$/ },
                  ({ path: p }) => ({ path: p.slice(2), namespace: "chat-child" }));
      b.onLoad({ filter: /.*/, namespace: "chat-child" }, ({ path: name }) => ({
        contents: `export default function ${name}() { return null; }
                   export function PlatformComposer() { return null; }`,
        loader: "jsx",
      }));
    },
  }],
});
const compiled = new Module(path.join(frontend, "agent-count-test.cjs"));
compiled.filename = path.join(frontend, "agent-count-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AgentActivity, AssistantMessage, agentSummary } = compiled.exports;
globalThis.localStorage = { getItem: () => null, setItem() {} };
const html = (c, props) => renderToStaticMarkup(React.createElement(c, props));

const CREW = [
  { name: "Source Mapper", role: "planner" }, { name: "Dependency Planner", role: "planner" },
  { name: "Minimal Graph Planner", role: "planner" },
  { name: "Databricks-3 agent", role: "worker" }, { name: "Snowflake agent", role: "worker" },
  { name: "Aggregator", role: "aggregator" },
];

test("the summary counts agents and breaks them down by role", () => {
  assert.deepEqual(agentSummary([{ name: "Demo agent", role: "worker" }]),
                   { total: 1, names: ["Demo agent"], text: "1 agent", detail: "" });
  const s = agentSummary(CREW);
  assert.equal(s.text, "6 agents");
  assert.equal(s.detail, "3 planners · 2 workers · Aggregator");
  // Live: the agents its steps name, each once, in order of first appearance.
  const live = agentSummary([], [{ label: "x", agent: "Source Mapper" }, { label: "y" },
                                 { label: "z", agent: "Snowflake agent" }, { label: "w", agent: "Source Mapper" }]);
  assert.deepEqual(live.names, ["Source Mapper", "Snowflake agent"]);
  assert.equal(agentSummary([], [{ label: "no agent named" }]).total, 0);
});

test("a running task shows how many agents are working, and which", () => {
  const out = html(AgentActivity, { running: true, steps: [
    { t: 1, label: "Source Mapper: proposed a graph", agent: "Source Mapper" },
    { t: 2, label: "Snowflake agent: running SQL on snowflake", agent: "Snowflake agent" },
    { t: 3, label: "Databricks-3 agent: running SQL", agent: "Databricks-3 agent" }] });
  assert.match(out, /👥 3 agents working/);
  assert.match(out, /Agents · 3 agents/);
  for (const name of ["Source Mapper", "Snowflake agent", "Databricks-3 agent"]) assert.ok(out.includes(name));
});

test("a finished answer shows its whole crew in the header and the activity panel", () => {
  const m = { text: "EU leads.", source: "*", table: "*", mode: "orchestrated", agents: CREW,
              activity_task_id: "t9" };
  const out = html(AssistantMessage, { m, activitySteps: [] });
  assert.match(out, /👥 6 agents \(3 planners · 2 workers · Aggregator\)/);
  assert.match(out, /title="Source Mapper, Dependency Planner, Minimal Graph Planner, Databricks-3 agent, Snowflake agent, Aggregator"/);
  assert.match(out, /Agents · 6 agents \(3 planners · 2 workers · Aggregator\)/);
});

test("a single-source answer says one agent; an answer without agents says nothing", () => {
  const one = html(AssistantMessage, { m: { text: "x", source: "demo", table: "sales",
                                            agents: [{ name: "Demo agent", role: "worker" }] } });
  assert.match(one, /👥 1 agent</);
  const none = html(AssistantMessage, { m: { text: "x", source: "demo", table: "sales" } });
  assert.doesNotMatch(none, /👥/);
});
