// The pipeline screens, in plain language: Pipeline flow draws its symbols
// (not "→"), says what a status means and why a run was rejected;
// Pipelines lets you leave a drafted step out; Jobs says what each job ran.
import assert from "node:assert/strict";
import { test } from "node:test";
import { readdirSync, readFileSync } from "node:fs";
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
    contents: `export * from "./src/components/pipelineText.js";
               export {PipelineGraph} from "./src/components/Flow.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "pipeline-screens-test.cjs"));
compiled.filename = path.join(frontend, "pipeline-screens-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const P = compiled.exports;

test("no component writes a \\u escape as JSX text, where React shows it literally", () => {
  const dir = path.join(frontend, "src/components");
  const offenders = [];
  for (const f of readdirSync(dir).filter((n) => n.endsWith(".jsx"))) {
    const src = readFileSync(path.join(dir, f), "utf8");
    // Text between tags: >…→…<  (inside {…} or quotes it is fine).
    for (const m of src.matchAll(/>([^<>{}"'`]*\\u[0-9a-fA-F]{4}[^<>{}"'`]*)</g)) offenders.push(`${f}: ${m[1].trim()}`);
  }
  assert.deepEqual(offenders, []);
});

test("the pipeline graph draws its symbols", () => {
  const html = renderToStaticMarkup(React.createElement(P.PipelineGraph, { pv: {
    source: "demo", executor: "Demo executor", kind: "sql_script", risk: "read",
    run_status: "succeeded_sql_only",
    steps: [{ name: "Extract sales", table: "sales", status: "ok", sql: "SELECT 1" }] } }));
  for (const s of ["◆ demo", "→", "▦ sales", "▷ Demo executor", "Succeeded — the SQL ran"]) {
    assert.ok(html.includes(s), s);
  }
  assert.doesNotMatch(html, /\\u[0-9a-f]{4}/i);
  assert.match(html, /flow-status-ok/);                   // a success, not a warning
});

test("statuses read as sentences, and a rejection says why", () => {
  assert.equal(P.flowStatusLabel("succeeded_sql_only"), "Succeeded — the SQL ran");
  assert.equal(P.flowStatusLabel("awaiting_approval"), "Waiting for an administrator's approval");
  assert.equal(P.flowStatusLabel("brand_new_state"), "Brand new state");
  assert.match(P.flowStatusNote({ status: "succeeded_sql_only" }), /generated Python is a download/);
  assert.equal(P.flowStatusNote({ status: "rejected", deployment: { reasons: [
    "Spark jobs run on Databricks only.", "demo is not a Spark target."] } }),
    "Why: Spark jobs run on Databricks only. · demo is not a Spark target.");
  assert.equal(P.flowStatusNote({ status: "rejected" }), "The approval step rejected this run.");
  assert.equal(P.flowStatusNote({ status: "succeeded" }), "");
});

test("leaving a drafted step out takes its table and source with it, and renumbers", () => {
  const lineage = {
    sources: [{ id: "s:demo", label: "demo" }],
    tables: [{ id: "t:demo.sales", label: "sales" }, { id: "t:demo.ecommerce_orders", label: "ecommerce_orders" },
             { id: "t:demo.ga4", label: "ga4" }],
    steps: [{ id: "step:0", index: 0, label: "Extract sales" },
            { id: "step:1", index: 1, label: "Extract ecommerce_orders" },
            { id: "step:2", index: 2, label: "Join sales to ga4" }],
    edges: [{ from: "s:demo", to: "t:demo.sales" }, { from: "s:demo", to: "t:demo.ecommerce_orders" },
            { from: "s:demo", to: "t:demo.ga4" },
            { from: "t:demo.sales", to: "step:0" }, { from: "t:demo.ecommerce_orders", to: "step:1" },
            { from: "t:demo.sales", to: "step:2" }, { from: "t:demo.ga4", to: "step:2" }],
  };
  const out = P.withoutStep(lineage, 1);
  assert.deepEqual(out.steps.map((s) => [s.id, s.index, s.label]),
                   [["step:0", 0, "Extract sales"], ["step:1", 1, "Join sales to ga4"]]);
  assert.deepEqual(out.tables.map((t) => t.label), ["sales", "ga4"]);     // ecommerce_orders gone
  assert.ok(out.edges.every((e) => e.to !== "t:demo.ecommerce_orders" && e.from !== "t:demo.ecommerce_orders"));
  assert.ok(out.edges.some((e) => e.from === "t:demo.sales" && e.to === "step:0"));
  // A table another step still reads stays.
  assert.deepEqual(P.withoutStep(lineage, 0).tables.map((t) => t.label), ["sales", "ecommerce_orders", "ga4"]);
  // Nothing left: no source either.
  let all = lineage;
  for (let i = 0; i < 3; i++) all = P.withoutStep(all, 0);
  assert.deepEqual([all.steps, all.tables, all.sources], [[], [], []]);
});

test("a job says what it ran, in one line", () => {
  assert.equal(P.jobSummary({ script: "-- weekly refresh\n\nSELECT region, SUM(revenue)\nFROM sales" }),
               "SELECT region, SUM(revenue)");
  assert.equal(P.jobSummary({ script: JSON.stringify({ run_name: "nightly sales", tasks: [] }) }), "nightly sales");
  assert.equal(P.jobSummary({ script: JSON.stringify({ dag_id: "daily_sales" }) }), "daily_sales");
  assert.equal(P.jobSummary({ script: JSON.stringify({ tasks: [{}, {}] }) }), "2 tasks");
  assert.equal(P.jobSummary({ script: "SELECT " + "x, ".repeat(60) }).length, 90);
  assert.equal(P.jobSummary({}), "");
  assert.equal(P.ago(1000, 1030), "just now");
  assert.equal(P.ago(1000, 1000 + 600), "10 min ago");
  assert.equal(P.ago(1000, 1000 + 7200), "2 h ago");
});
