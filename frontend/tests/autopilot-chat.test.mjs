// Autopilot from chat: a prompt that asks for something to happen again is a
// proposed agent card the user checks and creates; ⏱ Automate turns an answer
// into one; the agent's runs come back into the chat under an Autopilot line.
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
               export {default as AutopilotDraft, agentBody, createdFrom, draftFromAnswer}
                 from "./src/components/AutopilotDraft.jsx";`,
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
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatWorkflow|ChatPlatformRun)$/ },
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
const compiled = new Module(path.join(frontend, "autopilot-chat-test.cjs"));
compiled.filename = path.join(frontend, "autopilot-chat-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AssistantMessage, AutopilotDraft, agentBody, createdFrom, draftFromAnswer } = compiled.exports;
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };

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


const DRAFT = { trigger_type: "threshold", name: "Revenue alert", goal: "Report revenue and explain what changed",
  trigger_config: { metric_prompt: "revenue", op: "lt", value: 1000000, interval_seconds: 300 },
  source: "demo", tables: [], visual: false };

test("a proposed agent is a card, filled in from the request", () => {
  const m = { text: "That's a job for an Autopilot agent…", mode: "autopilot_draft",
              autopilot_draft: DRAFT, source: "demo", table: "Autopilot", columns: [], rows: [] };
  const html = harness(AssistantMessage, { m, conversationId: "c1", onOpenCanvas() {} }).markup();
  assert.match(html, /aria-label="Proposed Autopilot agent"/);
  assert.match(html, /value="Revenue alert"/);
  assert.match(html, />Report revenue and explain what changed</);
  assert.match(html, /value="revenue"/);
  assert.match(html, /<option value="lt" selected="">/);
  assert.match(html, /value="1000000"/);
  assert.match(html, /◆ demo/);
  assert.match(html, /✓ Create agent/);
  assert.doesNotMatch(html, /draws with D3/);
  // No source yet: the card asks for one; a D3 chat's agent says how it draws.
  const open = harness(AssistantMessage, { m: { ...m, autopilot_draft: { ...DRAFT, source: null, visual: true } },
                                           conversationId: "c1", onOpenCanvas() {} }).markup();
  assert.match(open, /choose a source…/);
  assert.match(open, /🎨 draws with D3/);
});

test("the card sends the agent it shows, posting to this chat", () => {
  const state = { name: " Revenue alert ", goal: "Report revenue", trigger: "threshold", interval: 86400,
                  metric: "revenue ", op: "lt", value: "1000000", table: "", source: "demo" };
  assert.deepEqual(agentBody(state, { ...DRAFT, visual: true }, "c1"), {
    name: "Revenue alert", goal: "Report revenue", trigger_type: "threshold",
    trigger_config: { metric_prompt: "revenue", op: "lt", value: 1000000, interval_seconds: 300 },
    source: "demo", tables: [], conversation_id: "c1", visual: true });
  assert.deepEqual(agentBody({ ...state, trigger: "schedule", interval: "3600" }, DRAFT, "c1").trigger_config,
                   { interval_seconds: 3600 });
  assert.deepEqual(agentBody({ ...state, trigger: "event", table: " web_traffic" }, DRAFT, null),
                   { ...agentBody({ ...state, trigger: "event", table: "web_traffic" }, DRAFT, null),
                     trigger_config: { table: "web_traffic", interval_seconds: 300 } });
  assert.equal(agentBody(state, DRAFT, null).conversation_id, undefined);
  // An agent this card already made is found again after a reload.
  const agents = [{ id: "a1", conversation_id: "c1", goal: DRAFT.goal, trigger_type: "threshold" },
                  { id: "a2", conversation_id: "c2", goal: DRAFT.goal, trigger_type: "threshold" }];
  assert.equal(createdFrom(agents, DRAFT, "c1").id, "a1");
  assert.equal(createdFrom(agents, DRAFT, "c3"), null);
});

test("Create makes the agent, runs it once, and says so", async () => {
  const sent = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options = {}) => {
    sent.push({ url, method: options.method || "GET", body: options.body && JSON.parse(options.body) });
    const answer = url === "/api/autopilot" && options.method === "POST"
      ? { id: "a9", name: "Revenue alert", visual: false } : url === "/api/autopilot" ? { agents: [] } : {};
    return { ok: true, status: 200, json: async () => answer };
  };
  try {
    const created = [];
    const card = harness(AutopilotDraft, { draft: DRAFT, conversationId: "c1",
                                           onCreated: (a) => created.push(a.id) });
    await button(card.tree(), "✓ Create agent").props.onClick();
    const posts = sent.filter((r) => r.method === "POST");
    assert.deepEqual(posts.map((r) => r.url), ["/api/autopilot", "/api/autopilot/a9/run"]);
    assert.equal(posts[0].body.conversation_id, "c1");
    assert.equal(posts[0].body.trigger_type, "threshold");
    assert.deepEqual(created, ["a9"]);
    assert.match(card.markup(), /✓ <b>Revenue alert<\/b> is on — each run posts to this chat/);
  } finally {
    globalThis.fetch = original;
  }
});

test("⏱ Automate turns an answer into a proposed agent; a posted run says where it came from", () => {
  const answer = { text: "West leads.", source: "demo", table: "*", sql: "SELECT region, SUM(revenue)",
                   columns: ["region", "revenue"], rows: [["West", 3]], chart: null, model: "x" };
  const h = harness(AssistantMessage, { m: answer, requirement: "revenue by region", conversationId: "c1",
                                        onOpenCanvas() {} });
  assert.match(h.markup(), /⏱ Automate/);
  button(h.tree(), "⏱ Automate").props.onClick();
  const html = h.markup();
  assert.match(html, /aria-label="Proposed Autopilot agent"/);
  assert.match(html, />revenue by region</);
  assert.match(html, /<option value="86400" selected="">daily<\/option>/);
  assert.deepEqual(draftFromAnswer({ ...answer, source: "*" }, "q", true).source, null);
  assert.equal(draftFromAnswer(answer, "revenue by region", true).visual, true);

  const posted = { ...answer, autopilot: { agent_name: "Daily revenue", trigger: "schedule",
                                           status: "done", summary: "Revenue by region." } };
  const out = harness(AssistantMessage, { m: posted, conversationId: "c1", onOpenCanvas() {} }).markup();
  assert.match(out, /✦ Autopilot · <b>Daily revenue<\/b> · on its schedule/);
  assert.doesNotMatch(out, /⏱ Automate/);                        // already an agent's answer
  const failed = harness(AssistantMessage, { m: { ...posted, autopilot: { ...posted.autopilot,
    status: "failed" } }, onOpenCanvas() {} }).markup();
  assert.match(failed, /class="ap-posted ap-posted-bad"/);
});
