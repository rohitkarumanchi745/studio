// The "Run on a platform" panel under the chat composer, drawn by React into a
// real (jsdom) document with /jobs/platforms answered: one sentence when no
// platform is connected; otherwise only connected platforms can be picked.
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
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: { contents: `export {PlatformComposer} from "./src/components/ChatPlatformRun.jsx";`,
           resolveDir: frontend, loader: "jsx" },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "react-dom"],
});
const compiled = new Module(path.join(frontend, "platform-panel-test.cjs"));
compiled.filename = path.join(frontend, "platform-panel-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { PlatformComposer } = compiled.exports;
const React = require("react");
const { act } = require("react");
const { createRoot } = require("react-dom/client");

const ALL = [["airflow", "Apache Airflow"], ["databricks_jobs", "Databricks Jobs"], ["dbt_cloud", "dbt Cloud"],
             ["k8s_spark", "Spark on Kubernetes"], ["ray", "Ray Jobs"]];

async function mount(connected) {
  const platforms = ALL.map(([name, label]) => ({ name, label, configured: connected.includes(name) }));
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => platforms });
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const sent = [];
  await act(async () => {
    root.render(React.createElement(PlatformComposer, { busy: false, onClose() {},
      onSubmit: (...a) => sent.push(a) }));
  });
  await act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); });
  return { host, root, sent };
}

test("with nothing connected the panel is one sentence — no error, no empty form", async () => {
  const { host, root } = await mount([]);
  const text = host.querySelector(".chat-platform-none").textContent.replace(/\s+/g, " ");
  assert.equal(text, "No platform is connected yet — an administrator connects Apache Airflow, Databricks Jobs, "
    + "dbt Cloud, Spark on Kubernetes and Ray Jobs on the server. Then ask here, or just in the chat: "
    + "“kick off the daily sales pipeline on Airflow”.");
  assert.equal(host.querySelector("select, textarea, .chat-platform-submit, .error"), null);
  await act(async () => root.unmount());
});

test("with one connected, it is picked, and the others cannot be", async () => {
  const { host, root, sent } = await mount(["k8s_spark"]);
  const select = host.querySelector("select");
  assert.equal(select.value, "k8s_spark");
  const options = [...select.options].map((o) => [o.value, o.disabled, o.textContent]);
  assert.deepEqual(options[0], ["airflow", true, "Apache Airflow — not connected"]);
  assert.deepEqual(options[3], ["k8s_spark", false, "Spark on Kubernetes"]);
  assert.equal(host.querySelector(".chat-platform-none"), null);
  const submit = host.querySelector(".chat-platform-submit");
  assert.equal(submit.disabled, true);                         // nothing typed yet
  const box = host.querySelector("textarea");
  assert.equal(box.rows, 2);
  assert.match(box.placeholder, /"main_file":"local:\/\/\/opt\/jobs\/etl.py"/);
  // Type a job and submit it.
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
  await act(async () => {
    setter.call(box, '{"main_file":"local:///opt/jobs/etl.py","image":"x/y:1"}');
    box.dispatchEvent(new window.Event("input", { bubbles: true }));
  });
  assert.equal(submit.disabled, false);
  await act(async () => submit.click());
  assert.deepEqual(sent, [[{ platform_action: "submit", platform_target: "k8s_spark",
    platform_payload: { main_file: "local:///opt/jobs/etl.py", image: "x/y:1" } },
    "Submit this Spark on Kubernetes job for approval"]]);
  await act(async () => root.unmount());
});
