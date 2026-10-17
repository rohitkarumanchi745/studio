// Custom visuals: agent-written D3 code in a sandboxed frame.
//
// Two halves. The document Studio builds for the frame — its CSP, its escaping,
// the sandbox attribute — is checked as text. Then the same document is RUN in
// jsdom with the real d3 inlined, to prove the runner hands the panel's rows to
// the code, and reports a thrown error and even a syntax error back to Studio.
// (jsdom enforces neither CSP nor sandboxing; browsers do. What is proven here
// is that the policy is written, and that the runner logic works.)
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM, VirtualConsole } from "jsdom";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const urlStub = {
  name: "url-stub",
  setup(b) {
    b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
    b.onLoad({ filter: /.*/, namespace: "url-stub" },
             () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
  },
};
const bundle = await build({
  stdin: {
    contents: `export {default as ArtifactView, buildArtifactDoc, scriptSafe, dataJson}
        from "./src/components/renderers/ArtifactView.jsx";
      export {default as ChartView} from "./src/components/ChartView.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  plugins: [urlStub],
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
});
const compiled = new Module(path.join(frontend, "artifact-test.cjs"));
compiled.filename = path.join(frontend, "artifact-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => (id === "echarts" ? { init() { return {}; } } : require(id));
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { ArtifactView, buildArtifactDoc, scriptSafe, ChartView } = compiled.exports;

globalThis.localStorage = { getItem: () => null, setItem() {} };
globalThis.window = { location: { href: "https://studio.example/app" } };

const D3 = "https://studio.example/assets/d3.min.js";
const COLUMNS = ["region", "revenue"];
const ROWS = [["West", 120], ["East", 90]];
const DRAW = `d3.select(el).append("svg").selectAll("text").data(data).join("text")
  .attr("class", "label").text(d => d.region + ":" + d.revenue);`;

function scripts(html) {
  return (html.match(/<\/script>/g) || []).length;
}

test("the frame's policy allows no network, no frames and only Studio's d3", () => {
  const doc = buildArtifactDoc({ code: DRAW, columns: COLUMNS, rows: ROWS, nonce: "n1", d3Src: D3 });
  const policy = /http-equiv="Content-Security-Policy" content="([^"]+)"/.exec(doc)[1];
  for (const rule of ["default-src 'none'", "connect-src 'none'", "frame-src 'none'",
                      "worker-src 'none'", "form-action 'none'", "base-uri 'none'",
                      "img-src data: blob:", `script-src 'unsafe-inline' ${D3}`]) {
    assert.ok(policy.includes(rule), rule);
  }
  assert.doesNotMatch(policy, /unsafe-eval/);
  assert.equal(scripts(doc), 5);                       // guard, d3, ui helpers, data, runner
});

test("no data value or code can close the script element early", () => {
  const evil = "</script><script>alert(document.domain)</script>";
  const doc = buildArtifactDoc({ code: `/* ${evil} */ ${DRAW}`, columns: COLUMNS,
                                 rows: [[evil, 1]], nonce: "n2", d3Src: D3 });
  assert.equal(scripts(doc), 5);                       // still exactly our five
  // Parsed as a browser would: five script elements, and the hostile value is
  // back intact as data. (Its "<script>" survives only as inert text inside a
  // neutralised comment — inside a script element only "</script" ends one.)
  const parsed = new JSDOM(doc).window.document;
  assert.equal(parsed.querySelectorAll("script").length, 5);
  const data = JSON.parse(parsed.getElementById("studio-data").textContent);
  assert.equal(data.rows[0][0], evil);
  assert.equal(scriptSafe("a</SCRIPT>b<!--c"), "a<\\/SCRIPT>b<\\!--c");
});

test("a downloaded visual stands alone: d3 inlined, still no network", () => {
  const doc = buildArtifactDoc({ code: DRAW, columns: COLUMNS, rows: ROWS, nonce: "x",
                                 inlineD3: "window.d3 = {};", title: "Rings <b>" });
  assert.doesNotMatch(doc, /<script src=/);
  assert.match(doc, /script-src 'unsafe-inline';/);
  assert.match(doc, /<title>Rings b<\/title>/);
});

test("the frame is sandboxed to scripts only, without same-origin", () => {
  const html = renderToStaticMarkup(React.createElement(ArtifactView, {
    visual: { title: "Rings", code: DRAW }, columns: COLUMNS, rows: ROWS }));
  assert.match(html, /<iframe [^>]*sandbox="allow-scripts"/);
  assert.doesNotMatch(html, /allow-same-origin|allow-top-navigation|allow-popups|allow-forms/);
  assert.match(html, /referrerPolicy="no-referrer"|referrerpolicy="no-referrer"/i);
});

test("a panel with a visual shows it first, with code, download and chart view", () => {
  const html = renderToStaticMarkup(React.createElement(ChartView, {
    columns: COLUMNS, rows: ROWS, chart: { type: "table", title: "Rings" },
    visual: { title: "Rings", code: DRAW, explanation: "One ring per region." } }));
  assert.match(html, /<iframe/);
  assert.match(html, /✨ custom D3/);
  for (const label of ["‹/› code", "⬇ HTML", "chart view"]) assert.ok(html.includes(label), label);
  assert.match(html, /One ring per region\./);
});

// ── Running the document ────────────────────────────────────────────────

const d3Source = readFileSync(path.join(frontend, "node_modules/d3/dist/d3.min.js"), "utf8");

async function run(code, rows = ROWS) {
  const doc = buildArtifactDoc({ code, columns: COLUMNS, rows, nonce: "run1", inlineD3: d3Source });
  const messages = [];
  const dom = new JSDOM(doc, {
    runScripts: "dangerously", pretendToBeVisual: true,
    virtualConsole: new VirtualConsole(),              // the broken-code case is expected
    beforeParse(win) { win.addEventListener("message", (e) => messages.push(e.data)); },
  });
  for (let i = 0; i < 20 && !messages.length; i++) await new Promise((r) => setTimeout(r, 10));
  return { messages, document: dom.window.document, close: () => dom.window.close() };
}

test("the runner hands the panel's rows to the code and reports success", async () => {
  const evil = "</script><b>bold</b>";
  const { messages, document, close } = await run(DRAW, [["West", 120], [evil, 7]]);
  const labels = [...document.querySelectorAll("text.label")].map((t) => t.textContent);
  assert.deepEqual(labels, ["West:120", `${evil}:7`]);  // the value round-trips as text
  assert.equal(document.querySelectorAll("b").length, 0);
  assert.deepEqual(messages.map((m) => [m.studioVisual, m.kind]), [["run1", "ok"]]);
  close();
});

test("a thrown error and a syntax error both come back to Studio", async () => {
  const thrown = await run(`throw new Error("no such column: revenu");`);
  assert.equal(thrown.messages[0].kind, "error");
  assert.match(thrown.messages[0].detail, /no such column: revenu/);
  thrown.close();
  const broken = await run(`this is not javascript {`);
  assert.equal(broken.messages[0].kind, "error");
  broken.close();
});

// A model sometimes writes the whole function — `function draw(d3, data, el,
// size, ui) { … }` — instead of its body. Wrapped by the runner, that only
// declared a nested draw that nothing called: a blank frame and no error.
test("a visual written as the whole draw function still draws", async () => {
  const body = `d3.select(el).append("svg").attr("class", "drawn");`;
  for (const code of [
    body,
    `function draw(d3, data, el, size, ui) {\n  ${body}\n}`,
    `const draw = (d3, data, el, size, ui) => {\n  ${body}\n};`,
  ]) {
    const { messages, document, close } = await run(code);
    assert.equal(document.querySelectorAll("svg.drawn").length, 1, code.slice(0, 40));
    assert.equal(messages[0].kind, "ok");
    close();
  }
  // A body with its own helper named draw, already called, is not drawn twice.
  const { document, close } = await run(`function draw() { ${body} }\ndraw();`);
  assert.equal(document.querySelectorAll("svg.drawn").length, 1);
  close();
});
