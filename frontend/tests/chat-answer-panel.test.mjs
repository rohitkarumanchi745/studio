// The chat's answer-details panel, from the chat's side: open by default on a
// wide screen and then as the reader left it; it follows the newest answer
// unless one was picked with its own "Details" button.
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
    contents: `export {AssistantMessage, readPanelPref, panelAnswer, webUrl, describeRoute, stepKind, bingQueries}
                 from "./src/components/Chat.jsx";`,
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
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatWorkflow|ChatPlatformRun|ChartView)$/ },
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
const compiled = new Module(path.join(frontend, "chat-answer-panel-test.cjs"));
compiled.filename = path.join(frontend, "chat-answer-panel-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AssistantMessage, readPanelPref, panelAnswer, webUrl, describeRoute, stepKind, bingQueries } = compiled.exports;

function withBrowser({ saved, wide, throws = false }, work) {
  const before = { localStorage: globalThis.localStorage, window: globalThis.window };
  globalThis.localStorage = { getItem: (k) => { if (throws) throw new Error("blocked");
                                                return k === "studio_answer_panel" ? saved ?? null : null; } };
  globalThis.window = { matchMedia: (q) => ({ matches: q === "(min-width: 1200px)" && wide }) };
  try { return work(); } finally { Object.assign(globalThis, before); }
}

test("open by default on a wide screen; then as the reader left it; storage may be blocked", () => {
  assert.equal(withBrowser({ wide: true }, readPanelPref), true);
  assert.equal(withBrowser({ wide: false }, readPanelPref), false);
  assert.equal(withBrowser({ wide: true, saved: "closed" }, readPanelPref), false);
  assert.equal(withBrowser({ wide: false, saved: "open" }, readPanelPref), true);
  assert.equal(withBrowser({ wide: true, saved: "junk" }, readPanelPref), true);
  assert.equal(withBrowser({ wide: true, throws: true }, readPanelPref), true);
});

test("the panel follows the newest answer, or the one picked, with the question it answered", () => {
  const messages = [
    { role: "user", text: "revenue by region" }, { role: "assistant", text: "A1" },
    { role: "user", text: "now by month" }, { role: "assistant", text: "A2" },
    { role: "user", text: "pending question" },
  ];
  assert.deepEqual(panelAnswer(messages, null), { m: messages[3], index: 3, question: "now by month" });
  assert.deepEqual(panelAnswer(messages, 1), { m: messages[1], index: 1, question: "revenue by region" });
  assert.equal(panelAnswer(messages, 2).index, 3);             // a question is not an answer
  assert.equal(panelAnswer(messages, 99).index, 3);
  assert.equal(panelAnswer([{ role: "user", text: "q" }], null), null);
  assert.equal(panelAnswer([{ role: "assistant", text: "hi" }], null).question, "");
});

test("each answer has a Details button that says when it is the one shown", () => {
  const m = { text: "West leads.", sql: "SELECT 1", source: "demo", table: "sales", trace_id: "t" };
  const html = (props) => renderToStaticMarkup(React.createElement(AssistantMessage,
    { m, onOpenCanvas() {}, busy: false, ...props }));
  assert.match(html({ onDetails() {} }),
               /<button class="icon-btn" aria-pressed="false" title="Its steps, the sources and columns it used, and the tools it called">◧ Details<\/button>/);
  assert.match(html({ onDetails() {}, detailed: true }), /class="icon-btn chip-on" aria-pressed="true"/);
  assert.doesNotMatch(html({}), /◧ Details/);
});

// ── Answers that are not about data ─────────────────────────────────────

test("a direct answer says so instead of a source, and lists the web pages it used as links", () => {
  const m = { text: "It is 12°C and cloudy.", mode: "general", source: "*", table: "general",
              model: "anthropic:claude-sonnet-5", trace_id: "t",
              auto_route: { mode: "general", why: "no connected source has what the question asks about" },
              citations: [{ source: "NYC weather", url: "https://weather.example/nyc", kind: "web" },
                          { source: "evil", url: "javascript:alert(1)", kind: "web" },
                          { source: "pricing.pdf", page: 3 }] };
  const html = renderToStaticMarkup(React.createElement(AssistantMessage, { m, onOpenCanvas() {}, busy: false }));
  assert.match(html, /anthropic:claude-sonnet-5 · answered directly/);
  assert.doesNotMatch(html, /· \* ·|· general</);
  assert.match(html, /<a class="chip chip-input reference reference-web" title="from the web · weather.example" href="https:\/\/weather.example\/nyc" target="_blank" rel="noopener noreferrer"><span class="ref-num">1<\/span>🌐 NYC weather<\/a>/);
  assert.doesNotMatch(html, /javascript:|evil/);
  assert.match(html, /📄 pricing.pdf p.3/);
  assert.match(html, /answered directly — no connected source has this/);
});

test("only http(s) links; route and step words for direct answers, the web and the clock", () => {
  assert.equal(webUrl("https://a.example/x?y=1"), "https://a.example/x?y=1");
  assert.equal(webUrl("http://a.example"), "http://a.example/");
  for (const bad of ["javascript:alert(1)", "data:text/html,x", "ftp://a.example", "", null, "not a url"]) {
    assert.equal(webUrl(bad), null, String(bad));
  }
  assert.equal(describeRoute({ mode: "general" }), "answered directly — no connected source has this");
  assert.equal(stepKind({ label: "Studio assistant: searched the web for “NYC weather”" }), "web");
  assert.equal(stepKind({ label: "Demo agent: read https://weather.example/nyc" }), "web");
  assert.equal(stepKind({ label: "Studio assistant: checking the time in America/New_York" }), "clock");
  assert.equal(stepKind({ label: "Demo agent: running SQL on demo" }), "query");
});

test("a Bing-grounded answer shows the Bing results link and says it came from a web search", () => {
  const m = { text: "It is 12°C.", mode: "general", source: "*", table: "general", model: "openai:gpt-5",
              web_grounding: { provider: "bing", queries: [
                { query: "NYC weather", url: "https://www.bing.com/search?q=NYC+weather" },
                { query: "spoof", url: "javascript:alert(1)" }] },
              citations: [{ source: "NYC weather", url: "https://weather.example/nyc", kind: "web" },
                          { source: "notes/q3.md", path: "notes/q3.md", kind: "file" }] };
  const html = renderToStaticMarkup(React.createElement(AssistantMessage, { m, onOpenCanvas() {}, busy: false }));
  assert.match(html, /<span class="web-search-note">From a Bing web search<\/span>/);
  assert.match(html, /href="https:\/\/www.bing.com\/search\?q=NYC\+weather"[^>]*><span class="ref-num">1<\/span>🔎 Bing: NYC weather<\/a>/);
  assert.match(html, /<span class="ref-num">2<\/span>🌐 NYC weather<\/a>/);
  assert.match(html, /<span class="ref-num">3<\/span>📁 notes\/q3.md<\/span>/);
  assert.doesNotMatch(html, /javascript:|spoof/);
  assert.deepEqual(bingQueries({ web_grounding: { provider: "anthropic", queries: [{ query: "x",
    url: "https://www.bing.com/search?q=x" }] } }), []);
  assert.equal(stepKind({ label: "Studio assistant: searched Bing for “x”" }), "web");
  assert.equal(stepKind({ label: "Demo agent: searched the shared folder for “q3”" }), "files");
  assert.equal(stepKind({ label: "Demo agent: read the file notes/q3.md" }), "files");
  // Not a Bing answer: no note.
  const plain = renderToStaticMarkup(React.createElement(AssistantMessage, {
    m: { ...m, web_grounding: undefined }, onOpenCanvas() {}, busy: false }));
  assert.doesNotMatch(plain, /From a Bing web search|🔎 Bing/);
});

test("tables that only matched the question's words are not sources of an answer that ran no query", () => {
  const m = { text: "It is 4 PM in New York.", source: "demo", table: "*", trace_id: "t",
              matched_tables: [{ table: "production", why: ["time"] }], inputs: { tables: [] } };
  const html = (x) => renderToStaticMarkup(React.createElement(AssistantMessage, { m: x, onOpenCanvas() {}, busy: false }));
  assert.doesNotMatch(html(m), /▦ production/);
  assert.match(html({ ...m, sql: "SELECT * FROM production" }), /▦ production/);
});
