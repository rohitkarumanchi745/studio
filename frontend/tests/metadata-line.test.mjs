// The metadata line under each connected source: what discovery found, said
// plainly, and a Rediscover button only where the caller may run discovery.
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
    contents: `export {default as MetadataLine, metadataText} from "./src/components/MetadataLine.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "metadata-line-test.cjs"));
compiled.filename = path.join(frontend, "metadata-line-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { MetadataLine, metadataText } = compiled.exports;

const NOW = 1_800_000_000;

test("a discovered source reads as tables, views and when", () => {
  assert.equal(metadataText({ status: "ok", kinds: { table: 7, view: 1 }, discovered_at: NOW - 180 }, NOW),
               "Metadata: 7 tables · 1 view · discovered 3 min ago");
  assert.equal(metadataText({ status: "ok", kinds: { table: 1 }, discovered_at: NOW - 10 }, NOW),
               "Metadata: 1 table · discovered just now");
  assert.equal(metadataText({ status: "ok", kinds: { unknown: 8 }, discovered_at: NOW - 7200 }, NOW),
               "Metadata: 8 objects · discovered 2 h ago");
  assert.equal(metadataText({ status: "ok", kinds: {}, discovered_at: NOW }, NOW),
               "Metadata: nothing found · discovered just now");
});

test("every other state says what happened", () => {
  assert.equal(metadataText(undefined), "Metadata: not discovered yet");
  assert.equal(metadataText({ status: "not_discovered" }), "Metadata: not discovered yet");
  assert.equal(metadataText({ status: "running" }), "Metadata: discovering…");
  assert.equal(metadataText({ status: "queued" }), "Metadata: discovery queued…");
  assert.match(metadataText({ status: "running", kinds: { table: 7 }, discovered_at: NOW }, NOW),
               /^Metadata: 7 tables · discovered just now · rediscovering…$/);
  assert.equal(metadataText({ status: "failed" }), "Metadata: discovery failed");
  assert.match(metadataText({ status: "failed", kinds: { table: 7 }, discovered_at: NOW }, NOW),
               /7 tables · discovered just now · the last discovery failed$/);
  assert.match(metadataText({ status: "partial", kinds: { table: 2 }, discovered_at: NOW }, NOW),
               /some columns could not be read$/);
  assert.match(metadataText({ status: "ok", kinds: { table: 2 }, discovered_at: NOW,
                              mirror_status: "failed" }, NOW), /Neo4j mirror failed$/);
  assert.match(metadataText({ status: "held", kinds: { table: 8 }, discovered_at: NOW, held: 6 }, NOW),
               /^Metadata: 8 tables · discovered just now · 6 removals waiting to be confirmed$/);
  assert.match(metadataText({ status: "held", kinds: { table: 2 }, discovered_at: NOW }, NOW),
               /removals waiting to be confirmed$/);   // a non-admin sees no count
});

test("Rediscover appears only where the caller may run discovery", () => {
  const summary = { status: "ok", kinds: { table: 7, view: 1 }, discovered_at: NOW,
                    error: "boom <script>" };
  const admin = renderToStaticMarkup(React.createElement(MetadataLine,
    { name: "databricks-3", summary, canRediscover: true, reload: async () => ({}) }));
  assert.match(admin, /7 tables · 1 view/);
  assert.match(admin, /↻ Rediscover/);
  assert.match(admin, /title="boom &lt;script&gt;"/);          // escaped, never markup
  const other = renderToStaticMarkup(React.createElement(MetadataLine,
    { name: "databricks-3", summary, canRediscover: false, reload: async () => ({}) }));
  assert.doesNotMatch(other, /Rediscover/);
});
