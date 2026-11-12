// Build Python → Run on Ray Jobs: the drafted module is submitted as a Ray
// Jobs platform run (a script payload), which an administrator approves in
// Jobs. The button exists only for a role that can submit platform runs, and
// only works once Ray Jobs is configured on the server.
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
    contents: `export {default as PyBuild, rayRunPayload} from "./src/components/PyBuild.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "pybuild-ray-test.cjs"));
compiled.filename = path.join(frontend, "pybuild-ray-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...args) => hooks ? hooks.useState(...args) : React.useState(...args),
  useEffect: (...args) => hooks ? hooks.useEffect(...args) : React.useEffect(...args),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { PyBuild, rayRunPayload } = compiled.exports;

function harness(component, props) {
  const values = [], effects = [];
  let cursor = 0;
  const stateHooks = {
    useState(initial) {
      const index = cursor++;
      if (index >= values.length) values.push(typeof initial === "function" ? initial() : initial);
      return [values[index], (next) => { values[index] = typeof next === "function" ? next(values[index]) : next; }];
    },
    useEffect(effect) { effects.push(effect); },
  };
  return {
    render() {
      cursor = 0; effects.length = 0;
      hooks = stateHooks;
      try { return renderToStaticMarkup(component(props)); } finally { hooks = null; }
    },
    start() { return effects.map((e) => e()).filter(Boolean); },
    set(index, value) { values[index] = value; },
    tree() {
      cursor = 0; effects.length = 0;
      hooks = stateHooks;
      try { return component(props); } finally { hooks = null; }
    },
  };
}

function find(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = find(child, predicate);
      if (found) return found;
    }
  } else if (node && typeof node === "object") {
    if (predicate(node)) return node;
    return find(node.props?.children, predicate);
  }
  return null;
}
const text = (node) => Array.isArray(node) ? node.map(text).join("")
  : node && typeof node === "object" ? text(node.props?.children)
  : node === null || node === undefined || node === false ? "" : String(node);

async function withApi(routes, work) {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage, window: globalThis.window };
  const calls = [];
  globalThis.localStorage = { getItem: () => null };
  globalThis.window = { location: { search: "", pathname: "/", hash: "" }, history: { replaceState() {} } };
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    calls.push({ url, method, body: options.body ? JSON.parse(options.body) : null });
    const hit = routes[`${method} ${url.replace(/^\/api/, "")}`];
    const reply = hit && typeof hit === "object" && "status" in hit ? hit : { status: 200, body: hit ?? {} };
    return { status: reply.status, ok: reply.status < 400, json: async () => reply.body };
  };
  try {
    await work({ calls, flush: async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); } });
  } finally { Object.assign(globalThis, originals); }
}

// State slots in PyBuild: prompt, context, busy, result, error, ray, submitted.
const RESULT = 3;
const PLATFORMS = [{ name: "airflow", label: "Apache Airflow", configured: true },
                   { name: "ray", label: "Ray Jobs", configured: true }];
const MODULE = { python: "import ray\nprint('hi')\n", mode: "agent", mcp_servers: [] };
const runButton = (panel) => find(panel.tree(), (n) => n.type === "button" && text(n).includes("Run on Ray Jobs"));

async function screen(routes) {
  const panel = harness(PyBuild, { onClose() {} });
  panel.render();
  panel.start();
  await routes.flush();
  return panel;
}

test("the module is submitted as a Ray Jobs script run, for an administrator to approve", async () => {
  await withApi({ "GET /jobs/platforms": PLATFORMS,
                  "POST /jobs": { status: 201, body: { id: "abcdef12-3456", status: "awaiting_approval" } } },
    async (api) => {
      const panel = await screen(api);
      assert.equal(runButton(panel), null, "nothing to run before a module is built");
      panel.set(RESULT, MODULE);
      const button = runButton(panel);
      assert.equal(button.props.disabled, false);
      await button.props.onClick();
      const post = api.calls.find((c) => c.method === "POST" && c.url === "/api/jobs");
      assert.deepEqual(post.body, rayRunPayload(MODULE.python));
      assert.deepEqual(JSON.parse(post.body.script), { script: MODULE.python, metadata: { source: "build_python" } });
      assert.equal(post.body.kind, "platform_run");
      assert.equal(post.body.target, "ray");
      assert.match(panel.render(), /Submitted to Ray Jobs as job abcdef12 — an administrator approves it in Jobs/);
    });
});

test("an unconfigured Ray Jobs leaves the button off and says what to set", async () => {
  await withApi({ "GET /jobs/platforms": [{ name: "ray", label: "Ray Jobs", configured: false }] },
    async (api) => {
      const panel = await screen(api);
      panel.set(RESULT, MODULE);
      const button = runButton(panel);
      assert.equal(button.props.disabled, true);
      assert.match(panel.render(), /set RAY_ADDRESS to a Ray head/);
      assert.equal(api.calls.some((c) => c.method === "POST"), false);
    });
});

test("a role that cannot submit platform runs sees no Ray button", async () => {
  await withApi({ "GET /jobs/platforms": { status: 403, body: { detail: "Your role cannot run pipelines" } } },
    async (api) => {
      const panel = await screen(api);
      panel.set(RESULT, MODULE);
      assert.equal(runButton(panel), null);
      assert.doesNotMatch(panel.render(), /Run on Ray Jobs|RAY_ADDRESS/);
    });
});

test("a refused submission is shown as the error, not as a success", async () => {
  await withApi({ "GET /jobs/platforms": PLATFORMS,
                  "POST /jobs": { status: 400, body: { detail: "platform_run script must be a JSON payload" } } },
    async (api) => {
      const panel = await screen(api);
      panel.set(RESULT, MODULE);
      await runButton(panel).props.onClick();
      const html = panel.render();
      assert.match(html, /class="error">platform_run script must be a JSON payload/);
      assert.doesNotMatch(html, /Submitted to Ray Jobs/);
    });
});
