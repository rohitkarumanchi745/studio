// The reader sizes a visual in the D3 & artifacts window: presets, a drag
// handle (and keys), wide mode, full screen — remembered for the next one.
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
    contents: `export {default as VizFrame, SIZES, MIN_HEIGHT, MAX_HEIGHT, clampHeight, rememberedHeight}
                 from "./src/components/VizFrame.jsx";
               export {default as ChartView} from "./src/components/ChartView.jsx";
               export {default as D3View} from "./src/components/renderers/D3View.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
  plugins: [{
    name: "url-stub",
    setup(b) {
      b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
      b.onLoad({ filter: /.*/, namespace: "url-stub" },
               () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
    },
  }],
});
const compiled = new Module(path.join(frontend, "viz-frame-test.cjs"));
compiled.filename = path.join(frontend, "viz-frame-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { VizFrame, SIZES, MIN_HEIGHT, MAX_HEIGHT, clampHeight, ChartView, D3View } = compiled.exports;

// A tiny window: localStorage and the event listeners the frame uses.
const store = new Map();
const listeners = {};
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) };
globalThis.addEventListener = (type, fn) => { (listeners[type] ||= new Set()).add(fn); };
globalThis.removeEventListener = (type, fn) => { listeners[type]?.delete(fn); };
const fire = (type, event) => [...(listeners[type] || [])].forEach((fn) => fn(event));

function mount(component, props) {
  const values = [];
  let cursor = 0;
  let effects = [];
  const stateHooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in values)) values[i] = typeof initial === "function" ? initial() : initial;
      return [values[i], (next) => { values[i] = typeof next === "function" ? next(values[i]) : next; }];
    },
    useRef(initial) {
      const i = cursor++;
      if (!(i in values)) values[i] = { current: initial };
      return values[i];
    },
    useMemo(factory) { return factory(); },
    useEffect(effect) { effects.push(effect); },
  };
  return {
    tree() {
      cursor = 0;
      effects = [];
      hooks = stateHooks;
      try { return component(props); } finally { hooks = null; }
    },
    html() { return renderToStaticMarkup(this.tree()); },
    runEffects() { this.tree(); effects.forEach((e) => e()); },
  };
}

function find(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const hit = find(child, predicate);
      if (hit) return hit;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  if (predicate(node)) return node;
  return find(node.props.children, predicate);
}
const byClass = (tree, cls) => find(tree, (n) => String(n.props?.className || "").split(" ").includes(cls));
const button = (tree, label) => find(tree, (n) => n.type === "button" && [].concat(n.props.children).join("").includes(label));
const heightOf = (tree) => byClass(tree, "viz-body")?.props.style.height;
const child = React.createElement("div", { className: "chart-inside" });

test("presets size the visual and are remembered for the next one", () => {
  store.clear();
  const f = mount(VizFrame, { title: "Revenue", children: child });
  assert.equal(heightOf(f.tree()), 440);
  const html = f.html();
  for (const [label] of SIZES) assert.match(html, new RegExp(`>${label}</button>`));
  assert.match(html, /⇤⇥ wide/);
  assert.match(html, /⤢ full screen/);
  assert.match(html, /role="separator"[^>]*aria-valuenow="440"/);
  button(f.tree(), "L").props.onClick();
  assert.equal(heightOf(f.tree()), 520);
  assert.equal(button(f.tree(), "L").props["aria-pressed"], true);
  assert.equal(store.get("studio_viz_height"), "520");
  // The next visual starts at the reader's choice.
  assert.equal(heightOf(mount(VizFrame, { children: child }).tree()), 520);
});

test("dragging the handle resizes freely; keys and double-click work too; limits hold", () => {
  store.clear();
  const f = mount(VizFrame, { children: child });
  const handle = () => byClass(f.tree(), "viz-handle");
  handle().props.onPointerDown({ preventDefault() {}, clientY: 100 });
  fire("pointermove", { clientY: 160 });
  assert.equal(heightOf(f.tree()), 500);
  fire("pointermove", { clientY: -900 });
  assert.equal(heightOf(f.tree()), MIN_HEIGHT);               // never collapses
  fire("pointerup", {});
  assert.equal(store.get("studio_viz_height"), String(MIN_HEIGHT));
  assert.equal(listeners.pointermove.size, 0);                // the drag let go
  const key = (k, shiftKey = false) => handle().props.onKeyDown({ key: k, shiftKey, preventDefault() {} });
  key("ArrowDown");
  assert.equal(heightOf(f.tree()), MIN_HEIGHT + 20);
  key("ArrowDown", true);
  assert.equal(heightOf(f.tree()), MIN_HEIGHT + 120);
  key("End");
  assert.equal(heightOf(f.tree()), MAX_HEIGHT);
  handle().props.onDoubleClick();
  assert.equal(heightOf(f.tree()), 440);
  assert.equal(clampHeight("nonsense"), 440);
});

test("wide mode breaks out of the chat column", () => {
  const f = mount(VizFrame, { children: child });
  assert.doesNotMatch(f.html(), /viz-frame is-wide/);
  button(f.tree(), "wide").props.onClick();
  assert.match(f.html(), /class="viz-frame is-wide"/);
  assert.match(f.html(), /⇥ column/);
});

test("full screen shows the visual over everything, and Esc brings it back", () => {
  const f = mount(VizFrame, { title: "Revenue", children: child });
  button(f.tree(), "full screen").props.onClick();
  f.runEffects();
  let html = f.html();
  assert.match(html, /class="viz-full" role="dialog" aria-modal="true" aria-label="Revenue"/);
  assert.equal((html.match(/chart-inside/g) || []).length, 1);  // drawn once: in the overlay
  assert.match(html, /Shown full screen/);
  fire("keydown", { key: "Enter" });
  assert.match(f.html(), /viz-full/);                          // only Esc closes it
  fire("keydown", { key: "Escape" });
  html = f.html();
  assert.doesNotMatch(html, /viz-full/);
  assert.match(html, /class="viz-body"/);
});

test("the D3 window puts its charts in the frame, filling it; elsewhere charts are as they were", () => {
  const props = { columns: ["region", "revenue"], rows: [["West", 120], ["East", 90]],
                  chart: { type: "bar", x: "region", y: ["revenue"], title: "By region" } };
  const inWindow = mount(ChartView, { ...props, d3Only: true }).tree();
  const frame = find(inWindow, (n) => n.type === VizFrame);
  assert.ok(frame);
  const chart = find(frame.props.children, (n) => n.type === D3View);
  assert.equal(chart.props.fill, true);
  assert.equal(find(mount(ChartView, props).tree(), (n) => n.type === VizFrame), null);
});
