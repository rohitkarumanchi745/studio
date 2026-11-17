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
    contents: `export {AssistantMessage, readPanelPref, panelAnswer} from "./src/components/Chat.jsx";`,
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
const { AssistantMessage, readPanelPref, panelAnswer } = compiled.exports;

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
