// Agent activity, with icons: a step on a database shows that database's own
// icon (its connection type, or the engine an "Any SQL database" URL names);
// every other kind of work — a chart, D3, a certified metric, documents, the
// aggregator — has its own glyph. Steps recorded before steps had a kind are
// read from their wording.
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
    contents: `export {AgentActivity, AssistantMessage, StepIcon, sourceIconId, sourceTypeLabel, stepKind}
                 from "./src/components/Chat.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
  plugins: [{
    name: "stubs",
    setup(b) {
      b.onResolve({ filter: /\\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
      b.onLoad({ filter: /.*/, namespace: "url-stub" },
               () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
    },
  }],
});
const compiled = new Module(path.join(frontend, "activity-icons-test.cjs"));
compiled.filename = path.join(frontend, "activity-icons-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AgentActivity, AssistantMessage, StepIcon, sourceIconId, sourceTypeLabel, stepKind } =
  compiled.exports;
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const html = (c, p) => renderToStaticMarkup(React.createElement(c, p));

test("a source's icon is its type, else its engine, else a database", () => {
  assert.equal(sourceIconId("snowflake", "snowflake"), "snowflake");
  assert.equal(sourceIconId("lancedb", "duckdb"), "lancedb");
  assert.equal(sourceIconId("demo", "sqlite"), "demo");
  assert.equal(sourceIconId("sql", "mysql"), "mysql");             // Any SQL database → its engine
  assert.equal(sourceIconId("sql", "postgresql"), "postgres");
  assert.equal(sourceIconId("sql", "mssql"), "mssql");
  assert.equal(sourceIconId("sql", "sqlite"), "sql");              // no better icon: the database
  assert.equal(sourceIconId("my_custom_thing", "weird"), "sql");
  assert.equal(sourceIconId(undefined, "cql"), "cassandra");
});

test("a step's kind is recorded, or read from its words", () => {
  assert.equal(stepKind({ kind: "docs", label: "anything" }), "docs");
  const said = {
    "Demo agent: running SQL on demo": "query",
    "Demo agent: reading the columns of sales": "schema",
    "Demo agent: rendering a bar chart": "chart",
    "drawing the answer with D3": "visual",
    "answered from a certified metric": "semantic",
    "answered with the verified query “Visits”": "verified",
    "reusing a cached plan for an equivalent question": "cache",
    "searching your knowledge collections": "docs",
    "Aggregator: synthesizing one answer from 2 agents' results": "aggregate",
    "fanning out to Demo agent, Snowflake agent": "agents",
    "BitNet couldn't answer — escalating to the frontier model": "model",
    "Snowflake agent: failed": "error",
    "finalizing the answer": "done",
  };
  for (const [label, kind] of Object.entries(said)) assert.equal(stepKind({ label }), kind, label);
  assert.equal(stepKind({ label: "hmm" }), null);
});

test("a database step shows the database; other work shows its glyph", () => {
  const db = html(StepIcon, { step: { kind: "query", label: "running SQL", source: "sales_pg",
                                      source_type: "sql", engine: "postgresql" } });
  assert.match(db, /class="srcicon"[^>]*aria-label="PostgreSQL"/);
  assert.match(db, /title="Querying · sales_pg"/);
  const lance = html(StepIcon, { step: { kind: "schema", source: "vectors", source_type: "lancedb",
                                         engine: "duckdb" } });
  assert.match(lance, /aria-label="LanceDB"/);
  assert.match(html(StepIcon, { step: { kind: "visual" } }), />🎨</);
  assert.match(html(StepIcon, { step: { kind: "semantic" } }), />▣</);
  assert.match(html(StepIcon, { step: { kind: "docs" } }), />📄</);
  assert.match(html(StepIcon, { step: { kind: "aggregate" } }), />∑</);
  // A query step whose source could not be resolved: the generic glyph.
  assert.match(html(StepIcon, { step: { kind: "query", source: "gone" } }), />🛢</);
  assert.match(html(StepIcon, { step: { label: "something new" } }), />•</);
});

test("the activity panel shows an icon on every step, and on what it is doing now", () => {
  const steps = [
    { t: 1, label: "grounded in the metadata graph: sales", kind: "route" },
    { t: 2, label: "Demo agent: running SQL on demo", kind: "query", source: "demo",
      source_type: "demo", engine: "sqlite", agent: "Demo agent" },
    { t: 3, label: "Demo agent: rendering a bar chart", kind: "chart", agent: "Demo agent" },
  ];
  const out = html(AgentActivity, { steps, running: true, startedAt: Date.now() });
  assert.equal(out.match(/class="activity-icon/g)?.length, 4);      // 3 steps + "now"
  assert.match(out, /aria-label="Demo data"/);
  assert.match(out, /🧭/);
  const now = out.slice(out.indexOf('class="activity-current"'), out.indexOf("</span></span>"));
  assert.match(now, /📊/);                                         // drawing the chart now
});

test("a source is named with its type: which database, which engine", () => {
  assert.equal(sourceTypeLabel("sql", "sqlite"), "Any SQL database · SQLite");
  assert.equal(sourceTypeLabel("sql", "mysql"), "MySQL");
  assert.equal(sourceTypeLabel("snowflake", "snowflake"), "Snowflake");
  assert.equal(sourceTypeLabel("lancedb", "duckdb"), "LanceDB");
  assert.equal(sourceTypeLabel("demo", "sqlite"), "Demo data");
  assert.equal(sourceTypeLabel("shop", "sqlite"), "Any SQL database · SQLite");
});

test("a step names its database, and grounding names its tables", () => {
  const steps = [
    { t: 1, label: "grounded in the metadata graph: orders (joined through customers)", kind: "route",
      source: "shop", source_type: "sql", engine: "sqlite",
      tables: [{ name: "orders", why: ["amount", "region"] }, { name: "customers", bridge: true }] },
    { t: 2, label: "Snowflake agent: running SQL on sales_sf", kind: "query", source: "sales_sf",
      source_type: "snowflake", engine: "snowflake" },
  ];
  const out = html(AgentActivity, { steps, running: false });
  assert.match(out, /class="source-badge" title="shop — Any SQL database · SQLite"/);
  assert.match(out, /<b>shop<\/b><span class="source-badge-type">Any SQL database · SQLite<\/span>/);
  assert.match(out, /<b>sales_sf<\/b><span class="source-badge-type">Snowflake<\/span>/);
  assert.match(out, /title="matched amount, region">▦ orders</);
  assert.match(out, /chip-bridge" title="joins the matched tables">▦ customers ⇄</);
  // A step on no source names none.
  assert.doesNotMatch(html(AgentActivity, { steps: [{ t: 1, label: "drawing the answer with D3",
                                                      kind: "visual" }] }), /source-badge/);
});

test("an answer names the database it came from, and Auto shows its icon", () => {
  const m = { text: "EU leads.", source: "shop", table: "*", model: "x", columns: [], rows: [],
              source_meta: { source_type: "sql", engine: "sqlite" },
              auto_route: { mode: "single", source: "shop", tables: ["orders"], terms: [],
                            types: { shop: { source_type: "sql", engine: "sqlite" } } } };
  const out = html(AssistantMessage, { m, onOpenCanvas() {} });
  assert.match(out, /<b>shop<\/b><span class="source-badge-type">Any SQL database · SQLite<\/span><\/span> · all tables/);
  assert.match(out, /class="auto-route-icon" title="shop — Any SQL database · SQLite"><svg class="srcicon"/);
  // An older answer without source_meta keeps its plain name.
  const old = html(AssistantMessage, { m: { ...m, source_meta: undefined, auto_route: undefined },
                                       onOpenCanvas() {} });
  assert.match(old, / · shop · all tables/);
});
