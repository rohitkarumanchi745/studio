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
    contents: `export {default as Recovery, recoveryBlocksJobDecision, recoveryIsActive} from "./src/components/PipelineRecovery.jsx";
      export {default as ChatWorkflow} from "./src/components/ChatWorkflow.jsx";
      export {default as ChatPipeline} from "./src/components/ChatPipeline.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "recovery-test.cjs"));
compiled.filename = path.join(frontend, "recovery-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...args) => hooks ? hooks.useState(...args) : React.useState(...args),
  useEffect: (...args) => hooks ? hooks.useEffect(...args) : React.useEffect(...args),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Recovery, recoveryBlocksJobDecision, recoveryIsActive, ChatWorkflow, ChatPipeline } = compiled.exports;
const html = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const step = {name: "Read sales", sql: "SELECT revenue FROM sales", source: "demo", verified: true};
const sql = {id: "p", source: "demo", status: "ready", steps: [step], run: {
  id: "r", status: "failed", error: "connection lost", steps_result: [{...step, ok: false}],
}};
const dag = {name: "Daily revenue", status: "failed", job_id: "parent", tasks: [{...step, id: "read", depends_on: []}]};
const recovery = (state, extra = {}) => ({state, attempt: 1, max_attempts: 2, reason: "Connection was unavailable", ...extra});

test("recovery states distinguish active work from stopped recovery", () => {
  for (const state of ["pending", "diagnosing", "retrying", "awaiting_approval"]) {
    assert.equal(recoveryIsActive(recovery(state)), true);
    assert.match(html(Recovery, {recovery: recovery(state)}), /attempt 1 of 2/);
  }
  for (const state of ["succeeded", "escalated", "exhausted", "unrecognized"]) {
    assert.equal(recoveryIsActive(recovery(state)), false);
  }
  assert.equal(html(Recovery, {}), "");
});

test("a successful correction never relabels its failed SQL parent", () => {
  const rendered = html(ChatPipeline, {pipeline: {...sql, run: {...sql.run,
    recovery: recovery("succeeded", {child_run_id: "corrected-run", repairs_run_id: "r"}),
  }}, messageId: "m", onRun() {}});
  assert.match(rendered, /Run failed/);
  assert.match(rendered, /Agent correction succeeded/);
  assert.match(rendered, /original failed run remains recorded/);
  assert.match(rendered, /Correction linked to failed run: r/);
  assert.match(rendered, /Run original pipeline again/);
  assert.doesNotMatch(rendered, /Run completed/);
});

test("Airflow correction success remains separate from the original failed DAG", () => {
  const rendered = html(ChatWorkflow, {pipeline: {...dag, live: {state: "failed", job: {
    recovery: recovery("succeeded", {child_job_id: "corrected-job", child_state: "succeeded"}),
  }}}, messageId: "m", onRun() {}});
  assert.match(rendered, /<span role="status">failed<\/span>/);
  assert.match(rendered, /Agent correction succeeded/);
  assert.match(rendered, /Submit original for approval/);
});

test("active recovery disables manual duplicate runs on both pipeline cards", () => {
  for (const state of ["pending", "diagnosing", "retrying", "awaiting_approval"]) {
    const sqlHtml = html(ChatPipeline, {pipeline: {...sql, run: {...sql.run, recovery: recovery(state)}}, messageId: "m", onRun() {}});
    assert.match(sqlHtml, /disabled=""[^>]*>Agent handling recovery/);
    const dagHtml = html(ChatWorkflow, {pipeline: {...dag, live: {state: "failed", job: {recovery: recovery(state)}}}, messageId: "m", onRun() {}});
    assert.match(dagHtml, /disabled=""[^>]*>Agent handling recovery/);
    assert.doesNotMatch(dagHtml, /Ask “repair this pipeline”/);
  }
});

test("write correction requires approval and stopped recovery does not promise another attempt", () => {
  const rendered = html(Recovery, {recovery: recovery("awaiting_approval", {child_job_id: "child", child_state: "awaiting_approval"})});
  assert.match(rendered, /Review and approve the correction in Jobs/);
  assert.match(rendered, /No corrected write is authorized by the earlier approval/);
  assert.match(rendered, /href="\/jobs#job-child"/);
  for (const state of ["escalated", "exhausted"]) {
    assert.match(html(Recovery, {recovery: recovery(state)}), /Automatic recovery has stopped/);
  }
});

test("only the correction job can receive approval while recovery is active", () => {
  const waiting = recovery("awaiting_approval", {child_job_id: "child"});
  assert.equal(recoveryBlocksJobDecision(waiting, "parent"), true);
  assert.equal(recoveryBlocksJobDecision(waiting, "child"), false);
  assert.equal(recoveryBlocksJobDecision(null, "ordinary-job"), false);
});

test("server details are escaped, and links cannot become javascript URLs", () => {
  const rendered = html(Recovery, {recovery: recovery("escalated", {
    reason: '<script>alert("bad")</script>', child_job_id: 'javascript:alert("bad")',
  })});
  assert.doesNotMatch(rendered, /<script>/);
  assert.match(rendered, /&lt;script&gt;/);
  assert.match(rendered, /href="\/jobs#job-javascript%3A/);
});

// A tiny effect harness exercises real component polling without a browser or
// additional dependencies. No HTTP request leaves this process.
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
      cursor = 0;
      effects.length = 0;
      hooks = stateHooks;
      let tree;
      try { tree = component(props); } finally { hooks = null; }
      return renderToStaticMarkup(tree);
    },
    start() { return effects.map((effect) => effect()).filter(Boolean); },
  };
}

async function withPolling(data, work) {
  const originals = {fetch: globalThis.fetch, localStorage: globalThis.localStorage,
    setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout};
  const requests = [], timers = [];
  globalThis.localStorage = {getItem() { return null; }};
  globalThis.fetch = async (url) => {
    requests.push(url);
    return {status: data.status || 200, ok: !data.status || data.status === 200,
      json: async () => data.body || data};
  };
  globalThis.setTimeout = (fn, delay) => { timers.push({fn, delay}); return timers.length; };
  globalThis.clearTimeout = () => {};
  try { await work({requests, timers, flush: async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); }}); }
  finally { Object.assign(globalThis, originals); }
}

test("failed Airflow parent keeps polling while its agent recovery is active", async () => {
  await withPolling({state: "failed", job: {recovery: recovery("diagnosing")}}, async ({timers, flush}) => {
    const panel = harness(ChatWorkflow, {pipeline: dag, messageId: "m", onRun() {}});
    panel.render();
    const cleanup = panel.start();
    await flush();
    assert.ok(timers.some(({delay}) => delay === 5000));
    assert.match(panel.render(), /Agent diagnosing the failure/);
    cleanup.forEach((fn) => fn());
  });
});

test("SQL polling uses the owned run endpoint and exposes recovery without changing parent results", async () => {
  await withPolling({recovery: recovery("retrying", {child_run_id: "child"})}, async ({requests, timers, flush}) => {
    const panel = harness(ChatPipeline, {pipeline: sql, messageId: "m", onRun() {}});
    panel.render();
    const cleanup = panel.start();
    await flush();
    assert.deepEqual(requests, ["/api/pipelines/p/runs/r/recovery"]);
    assert.ok(timers.some(({delay}) => delay === 5000));
    const rendered = panel.render();
    assert.match(rendered, /Run failed/);
    assert.match(rendered, /Correction run: child/);
    cleanup.forEach((fn) => fn());
  });
});

test("a revoked SQL run clears the cached recipe and recovery details", async () => {
  await withPolling({status: 403, body: {detail: "revoked"}}, async ({flush}) => {
    const panel = harness(ChatPipeline, {pipeline: {...sql, run: {...sql.run, recovery: recovery("pending")}}, messageId: "m", onRun() {}});
    panel.render();
    const cleanup = panel.start();
    await flush();
    const rendered = panel.render();
    assert.match(rendered, /no longer available to your account/);
    assert.doesNotMatch(rendered, /SELECT|Connection was unavailable/);
    cleanup.forEach((fn) => fn());
  });
});

test("a SQL recovery polling error disables rerunning stale state", async () => {
  await withPolling({status: 503, body: {detail: "unavailable"}}, async ({flush}) => {
    const panel = harness(ChatPipeline, {pipeline: sql, messageId: "m", onRun() {}});
    panel.render();
    const cleanup = panel.start();
    await flush();
    const rendered = panel.render();
    assert.match(rendered, /Could not refresh agent recovery/);
    assert.match(rendered, /disabled=""[^>]*>Run pipeline again/);
    cleanup.forEach((fn) => fn());
  });
});
