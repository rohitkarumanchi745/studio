// The composer's ＋ menu closes once an option is picked, on a click anywhere
// else and on Escape — left open, it covered the text box and the
// suggestions above. Drawn by React into a real (jsdom) document.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body><p id='outside'>elsewhere</p></body></html>",
                      { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: { contents: `export {default as ComposerPlus} from "./src/components/ComposerPlus.jsx";`,
           resolveDir: frontend, loader: "jsx" },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "react-dom"],
});
const compiled = new Module(path.join(frontend, "composer-plus-test.cjs"));
compiled.filename = path.join(frontend, "composer-plus-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { ComposerPlus } = compiled.exports;
const React = require("react");
const { createRoot } = require("react-dom/client");
const { act } = require("react");

async function mount(props = {}) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const calls = { build: [], platform: 0 };
  await act(async () => {
    root.render(React.createElement(ComposerPlus, {
      buildAirflow: false, busy: false,
      onBuildAirflow: (v) => calls.build.push(v), onPlatform: () => { calls.platform += 1; },
      ...props,
    }));
  });
  const menu = host.querySelector("details.composer-plus");
  return { host, root, menu, calls, open: () => { menu.open = true; } };
}

const mousedown = (el) => el.dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true }));

test("picking the Airflow DAG option reports it and closes the menu", async () => {
  const m = await mount();
  m.open();
  await act(async () => { m.menu.querySelector("input[type=checkbox]").click(); });
  assert.deepEqual(m.calls.build, [true]);
  assert.equal(m.menu.open, false);
  await act(async () => m.root.unmount());
});

test("opening the platform panel closes the menu too", async () => {
  const m = await mount();
  m.open();
  await act(async () => { m.menu.querySelector("button.menu-item").click(); });
  assert.equal(m.calls.platform, 1);
  assert.equal(m.menu.open, false);
  await act(async () => m.root.unmount());
});

test("a click elsewhere closes it; a click inside does not", async () => {
  const m = await mount();
  m.open();
  mousedown(m.menu.querySelector(".composer-menu"));
  assert.equal(m.menu.open, true);
  mousedown(document.getElementById("outside"));
  assert.equal(m.menu.open, false);
  const removed = [];
  const remove = document.removeEventListener.bind(document);
  document.removeEventListener = (type, fn, ...rest) => { removed.push(type); return remove(type, fn, ...rest); };
  try { await act(async () => m.root.unmount()); } finally { document.removeEventListener = remove; }
  // Unmounted: the listeners are gone with it.
  assert.deepEqual(removed.sort(), ["keydown", "mousedown"]);
  m.menu.open = true;
  mousedown(document.getElementById("outside"));
  assert.equal(m.menu.open, true);
});

test("Escape closes it and puts focus back on ＋", async () => {
  const m = await mount();
  m.open();
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(m.menu.open, false);
  assert.equal(document.activeElement, m.menu.querySelector("summary"));
  // Escape with the menu shut changes nothing.
  document.getElementById("outside").focus?.();
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.equal(m.menu.open, false);
  await act(async () => m.root.unmount());
});
