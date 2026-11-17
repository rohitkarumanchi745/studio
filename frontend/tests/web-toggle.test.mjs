// The chat's web search control: a switch, and a provider dropdown limited to
// what the server offers; the choice is remembered and sent with each
// question. Drawn by React into a real (jsdom) document.
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
const stored = {};
globalThis.localStorage = { getItem: (k) => stored[k] ?? null, setItem: (k, v) => { stored[k] = String(v); },
                            removeItem: (k) => { delete stored[k]; } };

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: {
    contents: `export {default as WebToggle, readWebChoice, webForRequest} from "./src/components/WebToggle.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "react-dom", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
  plugins: [
    { name: "url-stub", setup(b) {
      b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
      b.onLoad({ filter: /.*/, namespace: "url-stub" }, () => ({ contents: 'export default "/d3.js";', loader: "js" }));
    } },
    { name: "stub-children", setup(b) {
      b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatWorkflow|ChatPlatformRun|ChartView)$/ },
                  ({ path: p }) => ({ path: p.slice(2), namespace: "child" }));
      b.onLoad({ filter: /.*/, namespace: "child" }, ({ path: name }) => ({
        contents: `export default function ${name}() { return null; }
                   export function PlatformComposer() { return null; } export function chartImage() { return null; }`,
        loader: "jsx" }));
    } },
  ],
});
const compiled = new Module(path.join(frontend, "web-toggle-test.cjs"));
compiled.filename = path.join(frontend, "web-toggle-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { WebToggle, readWebChoice, webForRequest } = compiled.exports;
const React = require("react");
const { createRoot } = require("react-dom/client");
const { act } = require("react");

const OFFER = { enabled: true, default: "google", providers: [
  { id: "google", label: "Google", verbatim: true }, { id: "brave", label: "Brave", verbatim: false }] };

async function mount(props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const seen = [];
  const render = async (value) => act(async () => {
    root.render(React.createElement(WebToggle, { offer: OFFER, onChange: (v) => { seen.push(v); }, ...props, value }));
  });
  await render(props.value);
  return { host, root, seen, render };
}

test("the switch turns the web off and on; the dropdown offers Auto and what is configured", async () => {
  delete stored.studio_web;
  assert.equal(readWebChoice(), "auto");
  const t = await mount({ value: "auto" });
  const sw = t.host.querySelector("input[role=switch]");
  const select = t.host.querySelector("select[aria-label='Web search provider']");
  assert.equal(sw.checked, true);
  assert.deepEqual([...select.options].map((o) => [o.value, o.textContent]),
                   [["auto", "Auto (Google)"], ["google", "Google"], ["brave", "Brave"]]);
  await act(async () => sw.click());
  assert.deepEqual(t.seen, ["off"]);
  assert.equal(stored.studio_web, "off");
  await t.render("off");
  assert.equal(t.host.querySelector("select"), null);                 // no provider while off
  assert.equal(t.host.querySelector("input[role=switch]").checked, false);
  await act(async () => t.host.querySelector("input[role=switch]").click());
  assert.deepEqual(t.seen, ["off", "auto"]);
  await t.render("auto");
  const pick = t.host.querySelector("select");
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, "value").set.call(pick, "brave");
    pick.dispatchEvent(new window.Event("change", { bubbles: true }));
  });
  assert.deepEqual(t.seen.at(-1), "brave");
  assert.equal(stored.studio_web, "brave");
  await act(async () => t.root.unmount());
});

test("hidden when the server turned the web off; a gone provider falls back to Auto", async () => {
  const off = await mount({ value: "auto", offer: { enabled: false, providers: [] } });
  assert.equal(off.host.innerHTML, "");
  await act(async () => off.root.unmount());
  assert.equal(webForRequest("brave", OFFER), "brave");
  assert.equal(webForRequest("bing", OFFER), "auto");                  // not offered (any more)
  assert.equal(webForRequest("off", OFFER), "off");
  assert.equal(webForRequest("auto", { enabled: false }), "off");
  assert.equal(webForRequest("auto", null), undefined);                 // not loaded: server's default
  assert.equal(webForRequest("off", null), "off");
});
