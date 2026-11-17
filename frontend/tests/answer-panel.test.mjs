// The chat's right-hand panel: an answer's steps (tool calls), the sources it
// read with the tables and columns under each, what it produced, and the
// tools it used — live while a run works. Helpers are checked directly; the
// panel is drawn by React into a real (jsdom) document against a fetch double.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: {
    contents: `export {default as AnswerPanel, stepRows, panelSources, panelOutputs, panelTools}
                 from "./src/components/AnswerPanel.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "react-dom"],
});
const compiled = new Module(path.join(frontend, "answer-panel-test.cjs"));
compiled.filename = path.join(frontend, "answer-panel-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AnswerPanel, stepRows, panelSources, panelOutputs, panelTools } = compiled.exports;
const React = require("react");
const { createRoot } = require("react-dom/client");
const { act } = require("react");

globalThis.localStorage = { getItem: () => "tok", setItem() {}, removeItem() {} };

const STEPS = [
  { t: 1, label: "grounded in the metadata graph: sales, customers", kind: "route", source: "shop",
    source_type: "sql", engine: "postgresql", tables: [{ name: "sales", why: ["revenue"] },
                                                       { name: "regions", bridge: true }] },
  { t: 2, label: "Shop agent: reading the question and the schema", kind: "schema", agent: "Shop agent",
    source: "shop", source_type: "sql", engine: "postgresql" },
  { t: 3, label: "Shop agent: running SQL on shop", kind: "query", agent: "Shop agent", source: "shop",
    source_type: "sql", engine: "postgresql" },
  { t: 4, label: "Shop agent: rendering a bar chart", kind: "chart", agent: "Shop agent" },
  { t: 5, label: "finalizing the answer", kind: "done" },
];
const ANSWER = {
  message_id: "m1", activity_task_id: "task-1", text: "West leads.", source: "shop",
  source_meta: { source_type: "sql", engine: "postgresql" }, model: "anthropic:claude-sonnet-5",
  sql: "SELECT s.region, SUM(s.revenue) FROM sales s JOIN customers c ON c.id = s.customer_id GROUP BY 1",
  columns: ["region", "revenue"], rows: [["West", 10], ["East", 5]],
  chart: { type: "bar", title: "Revenue by region", x: "region", y: "revenue" },
  uses: [{ source: "shop", source_type: "sql", engine: "postgresql", tables: [
    { name: "sales", columns: ["region", "revenue", "customer_id"] },
    { name: "customers", columns: ["id"] }] }],
  citations: [{ source: "pricing.pdf", page: 3 }, { source: "pricing.pdf", page: 4 }],
};

// ── Helpers ─────────────────────────────────────────────────────────────

test("steps are done, the newest of a running task in progress, problems marked", () => {
  assert.deepEqual(stepRows(STEPS).map((r) => r.state), ["done", "done", "done", "done", "done"]);
  assert.deepEqual(stepRows(STEPS.slice(0, 3), true).map((r) => r.state), ["done", "done", "running"]);
  const failed = stepRows([{ label: "attempt failed (TimeoutError); retrying" }, { label: "x" }]);
  assert.deepEqual(failed.map((r) => r.state), ["error", "done"]);
  assert.deepEqual(stepRows([null, { kind: "query" }, { label: "ok" }]).map((r) => r.label), ["ok"]);
});

test("sources come from the answer's uses: each source, its tables, their columns; documents last", () => {
  // What a step grounded on (a guess, before the SQL ran) yields to what the SQL read.
  const guessed = { label: "grounded in the metadata graph: returns", kind: "route", source: "other",
                    source_type: "sql", engine: "mysql", tables: [{ name: "returns" }] };
  assert.deepEqual(panelSources(ANSWER, [...STEPS, guessed]), panelSources(ANSWER, STEPS));
  assert.deepEqual(panelSources(ANSWER, STEPS), [
    { key: "s:shop", name: "shop", type: "sql", engine: "postgresql", tables: [
      { name: "sales", columns: ["region", "revenue", "customer_id"], all: false },
      { name: "customers", columns: ["id"], all: false }] },
    { key: "docs", docs: ["pricing.pdf"] },
  ]);
  // Several sources, a table read whole, the same table twice merged.
  const many = panelSources({ uses: [
    { source: "snow", source_type: "snowflake", tables: [{ name: "ORDERS", columns: ["AMOUNT"] }] },
    { source: "pg", source_type: "postgres", tables: [{ name: "sales", columns: [], all_columns: true }] },
    { source: "snow", tables: [{ name: "orders", columns: ["DAY"] }] },
  ] });
  assert.deepEqual(many.map((s) => [s.name, s.tables.map((t) => [t.name, t.columns, t.all])]), [
    ["snow", [["ORDERS", ["AMOUNT", "DAY"], false]]], ["pg", [["sales", [], true]]]]);
});

test("an older answer falls back to its SQL's tables; a live run shows what its steps grounded on", () => {
  const old = { source: "demo", source_meta: { source_type: "demo", engine: "sqlite" },
                inputs: { tables: ["sales"] } };
  assert.deepEqual(panelSources(old), [{ key: "s:demo", name: "demo", type: "demo", engine: "sqlite",
                                         tables: [{ name: "sales", columns: [], all: false }] }]);
  assert.deepEqual(panelSources({ source: "*", inputs: { tables: ["sales"] } }), []);
  const live = panelSources(null, STEPS);
  assert.deepEqual(live, [{ key: "s:shop", name: "shop", type: "sql", engine: "postgresql",
                            tables: [{ name: "sales", columns: [], all: false }] }]);   // no bridge
});

test("outputs: each chart, a result table, a visual, a job; expired rows said", () => {
  assert.deepEqual(panelOutputs(ANSWER), [
    { key: "c0", icon: "📊", label: "Revenue by region", meta: "bar chart · 2 rows · 2 columns", chart: true }]);
  const panels = panelOutputs({ panels: [
    { chart: { type: "line", title: "Trend" }, columns: ["d", "v"], rows: [[1, 2]] },
    { visual: { title: "Sankey" }, columns: ["a"], rows: [] },
    { columns: ["a", "b", "c"], rows: [[1, 2, 3], [4, 5, 6], [7, 8, 9]] }], rows_purged: true,
    platform_run: { label: "Spark on Kubernetes", status: "awaiting_approval" } });
  assert.deepEqual(panels.map((o) => [o.label, o.meta]), [
    ["Trend", "line chart · 1 rows · 2 columns"], ["Sankey", "D3 visual"], ["Result table", "3 rows · 3 columns"],
    ["Result rows expired", "the SQL and chart settings are kept"], ["Spark on Kubernetes", "awaiting approval"]]);
  assert.deepEqual(panelOutputs({ text: "hello" }), []);
  assert.deepEqual(panelOutputs(null), []);
});

test("tools: from what the steps did and what the answer has, each once, and the model", () => {
  assert.deepEqual(panelTools(ANSWER, STEPS).map((t) => t.label), [
    "Metadata graph", "Schema reader", "SQL", "Charts", "Knowledge search", "anthropic:claude-sonnet-5"]);
  // Without the steps (not the asker), read off the answer alone.
  assert.deepEqual(panelTools(ANSWER).map((t) => t.label), [
    "SQL", "Charts", "Knowledge search", "anthropic:claude-sonnet-5"]);
  assert.deepEqual(panelTools({ served_by: "bitnet", model: "x", verified_sql: { title: "v" },
                                mode: "cached", platform_run: { target: "ray", label: "Ray Jobs" } })
    .map((t) => t.label), ["Verified queries", "Plan cache", "Ray Jobs", "BitNet"]);
});

// ── The panel ───────────────────────────────────────────────────────────

function server(answers) {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    const [status, body] = answers[url] || [404, { detail: "no" }];
    return { status, ok: status < 400, json: async () => body };
  };
  return calls;
}

async function mount(props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => { root.render(React.createElement(AnswerPanel, props)); });
  for (let i = 0; i < 4; i++) await act(async () => {});
  return { host, root };
}
const sectionText = (host, title) => [...host.querySelectorAll(".ap-section")]
  .find((s) => s.querySelector(".ap-section-title").textContent === title);

test("a finished answer: its steps fetched once for the asker, sources as a tree, outputs, tools", async () => {
  const calls = server({ "/api/tasks/task-1": [200, { steps: STEPS }] });
  const saved = [];
  let opened = 0, closed = 0;
  const { host, root } = await mount({ answer: ANSWER, question: "revenue by region",
    onSteps: (id, s) => saved.push([id, s.length]), onOpen: () => { opened += 1; },
    onClose: () => { closed += 1; } });
  assert.deepEqual(calls, ["/api/tasks/task-1"]);
  assert.deepEqual(saved, [["task-1", 5]]);
  assert.match(host.querySelector(".ap-head").textContent, /Answer details.*revenue by region/);
  const steps = sectionText(host, "Steps");
  assert.equal(steps.querySelector(".ap-count").textContent, "5 / 5");
  assert.equal(steps.querySelectorAll(".ap-step.is-done").length, 5);
  assert.match(steps.textContent, /running SQL on shop/);
  assert.match(steps.textContent, /▦ regions ⇄/);
  const sources = sectionText(host, "Sources");
  const tree = [...sources.querySelectorAll(".ap-source")].map((s) => [
    s.querySelector(".ap-source-head b").textContent,
    [...s.querySelectorAll(".ap-table")].map((t) => [t.querySelector(".ap-table-name").textContent,
      [...t.querySelectorAll(".ap-chip")].map((c) => c.textContent)])]);
  assert.deepEqual(tree, [
    ["shop", [["▦ sales", ["region", "revenue", "customer_id"]], ["▦ customers", ["id"]]]],
    ["Your documents", [["pricing.pdf", []]]]]);
  assert.equal(sources.querySelector(".ap-source-type").textContent, "PostgreSQL");
  const output = sectionText(host, "Output").querySelector("button.ap-output");
  assert.match(output.textContent, /Revenue by region.*bar chart/);
  await act(async () => output.click());
  assert.equal(opened, 1);
  assert.deepEqual([...sectionText(host, "Tools").querySelectorAll(".ap-tool")].map((t) => t.textContent.trim()),
                   ["🧭 Metadata graph", "▦ Schema reader", "🛢 SQL", "📊 Charts", "📄 Knowledge search",
                    "🧠 anthropic:claude-sonnet-5"]);
  await act(async () => host.querySelector(".ap-close").click());
  assert.equal(closed, 1);
  await act(async () => root.unmount());
});

test("steps already in hand are not fetched; someone else's are said to be the asker's", async () => {
  const calls = server({});
  const known = await mount({ answer: ANSWER, steps: STEPS.slice(0, 2), onClose() {} });
  assert.deepEqual(calls, []);
  assert.equal(known.host.querySelector(".ap-count").textContent, "2 / 2");
  await act(async () => known.root.unmount());
  const theirs = await mount({ answer: ANSWER, onClose() {} });               // 404 for this reader
  assert.match(sectionText(theirs.host, "Steps").textContent, /kept for the person who asked/);
  // Sources and tools still come from the answer itself.
  assert.equal(sectionText(theirs.host, "Sources").querySelectorAll(".ap-source").length, 2);
  await act(async () => theirs.root.unmount());
  const none = await mount({ answer: { text: "hi" }, onClose() {} });
  assert.match(sectionText(none.host, "Steps").textContent, /No steps were recorded/);
  assert.match(sectionText(none.host, "Sources").textContent, /read no data source/);
  assert.match(sectionText(none.host, "Output").textContent, /Text only/);
  await act(async () => none.root.unmount());
});

test("a run in progress: its live steps, the newest spinning, sources it has grounded on", async () => {
  const calls = server({});
  const { host, root } = await mount({ running: true, steps: STEPS.slice(0, 3), question: "revenue?",
                                       onClose() {} });
  assert.deepEqual(calls, []);
  assert.match(host.querySelector(".ap-head strong").textContent, /Working on it/);
  assert.equal(host.querySelector(".ap-count").textContent, "2 / 3");
  const last = [...host.querySelectorAll(".ap-step")].at(-1);
  assert.ok(last.classList.contains("is-running"));
  assert.match(last.textContent, /in progress/);
  assert.match(sectionText(host, "Sources").textContent, /shop.*▦ sales/);
  assert.match(sectionText(host, "Output").textContent, /Nothing yet/);
  await act(async () => root.unmount());
  const empty = await mount({ running: true, steps: [], onClose() {} });
  assert.match(sectionText(empty.host, "Steps").textContent, /Waiting for the first step/);
  await act(async () => empty.root.unmount());
});

test("web pages an answer used are a source of their own, with links, and web search is a tool", () => {
  const m = { text: "12°C", mode: "general", model: "anthropic:claude-sonnet-5", citations: [
    { source: "NYC weather", url: "https://weather.example/nyc", kind: "web" },
    { source: "NYC weather again", url: "https://weather.example/nyc", kind: "web" },
    { source: "evil", url: "javascript:alert(1)", kind: "web" },
    { source: "pricing.pdf", page: 2 }] };
  assert.deepEqual(panelSources(m), [
    { key: "docs", docs: ["pricing.pdf"] },
    { key: "web", pages: [{ url: "https://weather.example/nyc", title: "NYC weather" }], bing: [] }]);
  assert.deepEqual(panelTools(m).map((t) => t.label),
                   ["Knowledge search", "Web search", "anthropic:claude-sonnet-5"]);
  assert.deepEqual(panelTools({ citations: [{ kind: "web", url: "https://a.example" }] }).map((t) => t.label),
                   ["Web search"]);
  const steps = [{ label: "Studio assistant: checking the time in America/New_York", kind: "clock" },
                 { label: "Studio assistant: searched the web for “x”", kind: "web" }];
  assert.deepEqual(panelTools(null, steps).map((t) => t.label), ["Clock", "Web search"]);
});

test("the panel links each web page, opening a new tab", async () => {
  server({});
  const { host, root } = await mount({ answer: { text: "12°C", citations: [
    { source: "NYC weather", url: "https://weather.example/nyc", kind: "web" }] }, onClose() {} });
  const link = sectionText(host, "Sources").querySelector("a.ap-link");
  assert.equal(link.getAttribute("href"), "https://weather.example/nyc");
  assert.equal(link.getAttribute("target"), "_blank");
  assert.equal(link.getAttribute("rel"), "noopener noreferrer");
  assert.match(sectionText(host, "Sources").textContent, /The web.*NYC weather.*weather\.example/);
  await act(async () => root.unmount());
});

test("Bing-grounded answers carry the Bing results link; files from the shared folder are listed", async () => {
  const m = { text: "12°C", mode: "general",
    web_grounding: { provider: "bing", queries: [
      { query: "NYC weather today", url: "https://www.bing.com/search?q=NYC+weather+today" },
      { query: "forged", url: "https://evil.example/search?q=x" }] },
    citations: [{ source: "NYC weather", url: "https://weather.example/nyc", kind: "web" },
                { source: "notes/q3.md", path: "notes/q3.md", kind: "file" }] };
  assert.deepEqual(panelSources(m), [
    { key: "files", files: ["notes/q3.md"] },
    { key: "web", pages: [{ url: "https://weather.example/nyc", title: "NYC weather" }],
      bing: [{ query: "NYC weather today", url: "https://www.bing.com/search?q=NYC+weather+today" }] }]);
  assert.deepEqual(panelTools(m).map((t) => t.label), ["Web search", "Your files"]);
  server({});
  const { host, root } = await mount({ answer: m, onClose() {} });
  const sources = sectionText(host, "Sources");
  const bing = [...sources.querySelectorAll("a.ap-link")].map((a) => [a.textContent, a.getAttribute("href")]);
  assert.deepEqual(bing, [["🔎 Searched Bing: NYC weather today", "https://www.bing.com/search?q=NYC+weather+today"],
                          ["NYC weather", "https://weather.example/nyc"]]);
  assert.match(sources.textContent, /Bing web search/);
  assert.match(sources.textContent, /Your files.*notes\/q3\.md/);
  await act(async () => root.unmount());
});

test("a Bing search with no page cited still shows its Bing results link", () => {
  const m = { text: "No clear answer.", web_grounding: { provider: "bing", queries: [
    { query: "obscure thing", url: "https://www.bing.com/search?q=obscure+thing" }] } };
  assert.deepEqual(panelSources(m), [{ key: "web", pages: [],
    bing: [{ query: "obscure thing", url: "https://www.bing.com/search?q=obscure+thing" }] }]);
});

test("a finished answer that ran no query lists no data source, whatever its steps read", () => {
  const steps = [{ label: "Demo agent: reading the question and the schema", kind: "schema",
                   source: "demo", source_type: "demo", engine: "sqlite" },
                 { label: "Demo agent: checking the time in America/New_York", kind: "clock" }];
  assert.deepEqual(panelSources({ text: "4 PM in New York." }, steps), []);
  assert.deepEqual(panelSources({ text: "x", sql: "SELECT 1" }, steps).map((s) => s.name), ["demo"]);
  assert.deepEqual(panelSources(null, steps).map((s) => s.name), ["demo"]);     // a live run
});
