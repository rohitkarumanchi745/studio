// The platform card in the regular chat: when a DAG named in words matches
// several, the card offers them as buttons, and a button submits that DAG
// with the card's parameters. The composer's Submit button keeps its size.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import path from "node:path";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
let hooks = null;
const bundle = await build({
  stdin: {
    contents: `export {default as ChatPlatformRun, PlatformComposer} from "./src/components/ChatPlatformRun.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "chat-platform-run-test.cjs"));
compiled.filename = path.join(frontend, "chat-platform-run-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { ChatPlatformRun, PlatformComposer } = compiled.exports;

function render(component, props) {
  const values = [];
  let cursor = 0;
  hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in values)) values[i] = typeof initial === "function" ? initial() : initial;
      return [values[i], (next) => { values[i] = typeof next === "function" ? next(values[i]) : next; }];
    },
    useEffect() {},
  };
  try { return component(props); } finally { hooks = null; }
}

function findAll(node, predicate, out = []) {
  if (Array.isArray(node)) node.forEach((c) => findAll(c, predicate, out));
  else if (node && typeof node === "object") {
    if (predicate(node)) out.push(node);
    findAll(node.props?.children, predicate, out);
  }
  return out;
}
const text = (n) => Array.isArray(n) ? n.map(text).join("")
  : n && typeof n === "object" ? text(n.props?.children) : n == null || n === false ? "" : String(n);

const CARD = {
  execution_mode: "platform_run", target: "airflow", label: "Apache Airflow", status: "needs_input",
  payload: { conf: { day: "2026-11-01" } },
  missing: ["dag_id: more than one Airflow DAG matches “sales” — pick one: daily_sales, daily_sales_backfill"],
  choices: ["daily_sales", "daily_sales_backfill"],
};

test("a card that matched several DAGs offers each as a button that submits it", () => {
  const sent = [];
  const tree = render(ChatPlatformRun, { artifact: CARD, busy: false, messageId: "m1",
                                         onAction: (...a) => sent.push(a) });
  const group = findAll(tree, (n) => n.props?.role === "group")[0];
  assert.equal(group.props["aria-label"], "Pick the DAG");
  const buttons = findAll(group, (n) => n.type === "button");
  assert.deepEqual(buttons.map(text), ["daily_sales", "daily_sales_backfill"]);
  buttons[1].props.onClick();
  assert.deepEqual(sent, [["submit", { payload: { conf: { day: "2026-11-01" }, dag_id: "daily_sales_backfill" },
                                       text: "Run Airflow DAG daily_sales_backfill" }]]);
  // The reason is listed too.
  assert.match(renderToStaticMarkup(tree), /more than one Airflow DAG matches “sales”/);
});

test("no buttons once the run is submitted, or without a message to act on", () => {
  const queued = render(ChatPlatformRun, { artifact: { ...CARD, status: "awaiting_approval", job_id: "j1" },
                                           busy: false, messageId: "m1", onAction() {} });
  assert.equal(findAll(queued, (n) => n.props?.role === "group").length, 0);
  const orphan = render(ChatPlatformRun, { artifact: CARD, busy: false, messageId: null, onAction() {} });
  assert.equal(findAll(orphan, (n) => n.props?.role === "group").length, 0);
  const busy = render(ChatPlatformRun, { artifact: CARD, busy: true, messageId: "m1", onAction() {} });
  assert.ok(findAll(busy, (n) => n.type === "button" && text(n) === "daily_sales")[0].props.disabled);
});

test("the panel points at plain English, and its Submit button is not the round send button", () => {
  const tree = render(PlatformComposer, { busy: false, onSubmit() {}, onClose() {} });
  const html = renderToStaticMarkup(tree);
  assert.match(html, /Or just ask in the chat: “kick off the daily sales pipeline on Airflow”/);
  assert.match(html, /<div class="chat-platform-row"><select aria-label="Execution platform">/);
  assert.match(html, /class="primary chat-platform-submit"/);
  assert.match(html, /<textarea class="chat-platform-payload" aria-label="Platform job parameters" rows="2"/);
  const css = readFileSync(path.join(frontend, "src/styles.css"), "utf8");
  assert.match(css, /\.composer \.chat-platform-submit, \.chat-platform-submit \{ width: auto;/);
  assert.match(css, /\.chat-platform-payload \{\n  display: block; width: 100%; box-sizing: border-box; min-height: 52px;/);
});

test("a Spark card says which cluster it runs on, and nothing for other platforms", () => {
  const spark = { execution_mode: "platform_run", target: "k8s_spark", label: "Spark on Kubernetes",
                  status: "awaiting_approval", job_id: "j1",
                  payload: { main_file: "local:///opt/jobs/etl.py", image: "a:1", cluster: "local" } };
  const html = (artifact) => renderToStaticMarkup(render(ChatPlatformRun, { artifact, busy: false,
                                                                            messageId: "m1", onAction() {} }));
  assert.match(html(spark), /<p class="meta chat-platform-cluster">Runs on Local Kubernetes<\/p>/);
  assert.match(html({ ...spark, payload: { ...spark.payload, cluster: "aks" } }),
               /Runs on Azure Kubernetes Service/);
  assert.doesNotMatch(html({ ...spark, payload: { main_file: "x" } }), /Runs on/);
  assert.doesNotMatch(html({ ...spark, payload: { ...spark.payload, cluster: "<b>x" } }), /Runs on/);
  assert.doesNotMatch(html({ ...CARD, payload: { dag_id: "d", cluster: "local" } }), /Runs on/);
  // Not yet queued (a cluster not connected, say): the missing list speaks instead.
  const waiting = html({ ...spark, status: "needs_input", job_id: undefined,
                         payload: { ...spark.payload, cluster: "aks" },
                         missing: ["cluster: AKS is not connected"] });
  assert.doesNotMatch(waiting, /Runs on/);
  assert.match(waiting, /cluster: AKS is not connected/);
});
