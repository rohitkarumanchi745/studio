// Jobs says what each job ran, by whom and when, without opening the row.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: {
    contents: `export * from "./src/components/jobText.js";`,
    resolveDir: frontend, loader: "js",
  },
  bundle: true, write: false, format: "cjs", platform: "node",
});
const compiled = new Module(path.join(frontend, "job-summary-test.cjs"));
compiled.filename = path.join(frontend, "job-summary-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { jobSummary, ago } = compiled.exports;

test("a job says what it ran, in one line", () => {
  assert.equal(jobSummary({ script: "-- weekly refresh\n\nSELECT region, SUM(revenue)\nFROM sales" }),
               "SELECT region, SUM(revenue)");
  assert.equal(jobSummary({ script: JSON.stringify({ run_name: "nightly sales", tasks: [] }) }), "nightly sales");
  assert.equal(jobSummary({ script: JSON.stringify({ dag_id: "daily_sales" }) }), "daily_sales");
  assert.equal(jobSummary({ script: JSON.stringify({ tasks: [{}, {}] }) }), "2 tasks");
  assert.equal(jobSummary({ script: "{not json" }), "{not json");
  assert.equal(jobSummary({ script: "SELECT " + "x, ".repeat(60) }).length, 90);
  assert.equal(jobSummary({}), "");
});

test("and when, relative to now", () => {
  assert.equal(ago(1000, 1030), "just now");
  assert.equal(ago(1000, 1000 + 600), "10 min ago");
  assert.equal(ago(1000, 1000 + 7200), "2 h ago");
  assert.equal(ago(0), "");
  assert.match(ago(1000, 1000 + 3 * 86400), /\d/);
});
