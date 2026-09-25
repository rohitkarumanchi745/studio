// The Learned rules review screen: what is live is shown apart from what is
// waiting, and only a waiting draft can be approved. A real render, no browser.
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
    contents: `export {RulesView} from "./src/components/LearnedRules.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "learned-rules-test.cjs"));
compiled.filename = path.join(frontend, "learned-rules-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { RulesView } = compiled.exports;

const noop = () => {};
const render = (data, draftText = "") => renderToStaticMarkup(React.createElement(RulesView, {
  data, draftText, onDraftText: noop, onApprove: noop, onReject: noop, onRetire: noop, busy: false,
}));
const row = (over) => ({ id: "r", rules: "- a rule", status: "proposed", evidence_count: 12,
                         created_at: 1_700_000_000, decided_at: null, ...over });

test("a waiting draft is editable and can be approved or rejected", () => {
  const html = render({ active: null, history: [row({ id: "p1" })], min_failures: 10 },
                      "- Aggregate before joining.");
  assert.match(html, /No learned rules are active/);
  assert.match(html, /from 12 failed runs/);
  assert.match(html, /<textarea[^>]*>- Aggregate before joining.<\/textarea>/);
  assert.match(html, /✓ approve/);
  assert.match(html, /✕ reject/);
  assert.doesNotMatch(html, />retire</);
});

test("live rules can be retired, and history is shown without approve buttons", () => {
  const html = render({
    active: row({ id: "a1", status: "active", rules: "- Live rule.", decided_at: 1_700_000_500 }),
    history: [row({ id: "old", status: "rejected", rules: "- Rejected rule." })],
    min_failures: 10,
  });
  assert.match(html, /- Live rule\./);
  assert.match(html, />retire</);
  assert.match(html, /No draft yet. The worker drafts one after 10 new failed runs/);
  assert.match(html, /- Rejected rule\./);
  assert.doesNotMatch(html, /✓ approve/);
});
