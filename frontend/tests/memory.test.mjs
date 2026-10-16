// The Memory page's list: every saved note is shown with a way to forget it,
// and an empty memory says how notes get there. A real render, no browser.
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
    contents: `export {NotesList} from "./src/components/Memory.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "memory-test.cjs"));
compiled.filename = path.join(frontend, "memory-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { NotesList } = compiled.exports;

const render = (props) => renderToStaticMarkup(
  React.createElement(NotesList, { onDelete: () => {}, busy: false, ...props }));

test("each note is listed with its own forget button", () => {
  const html = render({ notes: [
    { id: "n1", note: "prefers bar charts", updated_at: 1_700_000_000 },
    { id: "n2", note: "cares about the West region", updated_at: 1_700_000_100 },
  ] });
  assert.match(html, /prefers bar charts/);
  assert.match(html, /cares about the West region/);
  assert.equal(html.match(/✕ forget/g).length, 2);
  assert.match(html, /aria-label="Forget: prefers bar charts"/);
});

test("an empty memory explains how notes get saved", () => {
  const html = render({ notes: [] });
  assert.match(html, /Nothing remembered yet/);
  assert.doesNotMatch(html, /✕ forget/);
});

test("buttons are disabled while a delete is in flight", () => {
  const html = render({ busy: true, notes: [{ id: "n1", note: "x", updated_at: 0 }] });
  assert.match(html, /<button[^>]*disabled=""/);
});
