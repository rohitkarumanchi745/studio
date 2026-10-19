// Autopilot on screen: what an agent's last check found (so a poll that did
// not fire is visible), a run in full (its text, charts, data and SQL, and
// any action that could not be proposed), unread alerts on top of the
// screen, and the sidebar bell that stays visible when Tools is collapsed.
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
    contents: `export {checkText, RunDetail, AlertsStrip} from "./src/components/Autopilot.jsx";
               export {default as AlertsBell} from "./src/components/AlertsBell.jsx";`,
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
const compiled = new Module(path.join(frontend, "autopilot-test.cjs"));
compiled.filename = path.join(frontend, "autopilot-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { checkText, RunDetail, AlertsStrip, AlertsBell } = compiled.exports;

globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
const html = (c, p) => renderToStaticMarkup(React.createElement(c, p));
const T = 1792358000;

test("an agent says what its last check found", () => {
  const threshold = { trigger_type: "threshold", last_checked_at: T };
  assert.equal(checkText({ ...threshold, last_checked_at: null }), "not checked yet");
  assert.match(checkText({ ...threshold, last_check: { status: "not met", value: 334200 } }),
               /^not met \(334,200\) · checked /);
  assert.match(checkText({ ...threshold, last_check: { status: "met", value: 12.345 } }),
               /^still met \(12\.35\) — fires again once it drops back/);
  assert.match(checkText({ ...threshold, last_check: { status: "error",
                                                       detail: "the threshold SQL returned no number" } }),
               /^⚠ couldn't check \(.+\): the threshold SQL returned no number$/);
  const event = { trigger_type: "event", last_checked_at: T };
  assert.match(checkText({ ...event, last_check: { status: "watching", value: "2026-10-18" } }),
               /^watching for rows newer than 2026-10-18/);
  assert.match(checkText({ ...event, last_check: { status: "no new data", value: "2026-10-18" } }),
               /^no new data since 2026-10-18/);
  // Schedules and manual agents are not polled: nothing to say.
  assert.equal(checkText({ trigger_type: "schedule", last_check: { status: "fired" } }), "");
  assert.equal(checkText({ trigger_type: "manual" }), "");
});

const RUN = {
  id: "r1", status: "done", trigger: "schedule", started_at: T, finished_at: T + 2.5,
  summary: "Revenue by region.", trace_id: "abcdef123456",
  result: { text: "**West** leads.", sql: "SELECT region, SUM(revenue) FROM sales GROUP BY region",
            columns: ["region", "revenue"], rows: [["West", 3], ["East", 2]],
            chart: { type: "table", title: "Revenue" }, served_by: "semantic", row_count: 900 },
};

test("a run opens in full: text, data, SQL and where the answer came from", () => {
  const out = html(RunDetail, { run: RUN, onOpenJobs() {} });
  assert.match(out, /▣ certified metric/);
  assert.match(out, /schedule · .+ · took 2\.5s/);
  assert.match(out, /<strong>West<\/strong> leads\./);              // the text as markdown
  assert.match(out, /<td[^>]*>West<\/td>/);                         // the data
  assert.match(out, /The first 2 of 900 rows/);
  assert.match(out, /<summary class="meta">SQL<\/summary>/);
  assert.match(out, /GROUP BY region/);
  assert.doesNotMatch(out, /could not be proposed|class="error/);
  const refused = html(RunDetail, { run: { ...RUN, proposed_job_id: null,
    result: { ...RUN.result, action_error: "supervisor down" } } });
  assert.match(refused, /The governed action could not be proposed: supervisor down/);
  const failed = html(RunDetail, { run: { ...RUN, status: "failed", result: null,
    summary: "the trigger could not be checked: no such table" } });
  assert.match(failed, /class="error ap-run-why">the trigger could not be checked: no such table/);
  const gated = html(RunDetail, { run: { ...RUN, proposed_job_id: "j1" }, onOpenJobs() {} });
  assert.match(gated, /action pending approval/);
});

test("unread alerts lead the screen, and only unread ones", () => {
  const alerts = [
    { id: "a1", agent_id: "g1", agent_name: "Revenue dip", status: "done", summary: "Crossed.",
      started_at: T, seen_at: null },
    { id: "a2", agent_id: "g2", agent_name: "Nightly", status: "failed", summary: "Broke.",
      started_at: T, seen_at: null },
    { id: "a3", agent_id: "g2", agent_name: "Old", status: "done", started_at: T, seen_at: T },
  ];
  const out = html(AlertsStrip, { alerts, onOpen() {}, onMarkAll() {} });
  assert.match(out, /🔔 2 new alerts/);
  assert.match(out, /Revenue dip/);
  assert.match(out, /Nightly/);
  assert.doesNotMatch(out, />Old</);
  assert.equal(html(AlertsStrip, { alerts: [alerts[2]], onOpen() {}, onMarkAll() {} }), "");
  const opened = [];
  const tree = AlertsStrip({ alerts, onOpen: (a) => opened.push(a.id), onMarkAll() {} });
  const rows = tree.props.children[1];
  rows[1].props.children.at(-1).props.onClick();
  assert.deepEqual(opened, ["a2"]);
});

test("the sidebar bell shows only with something unread", () => {
  assert.equal(html(AlertsBell, { count: 0, onOpen() {} }), "");
  assert.match(html(AlertsBell, { count: 1, onOpen() {} }), /🔔 1 new Autopilot alert</);
  assert.match(html(AlertsBell, { count: 3, onOpen() {} }), /🔔 3 new Autopilot alerts</);
});
