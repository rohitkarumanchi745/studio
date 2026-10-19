// Answers render their markdown (never as raw **stars** or backticks, never as
// HTML), and a 👍/👎 can be taken back or switched.
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
    contents: `export {default as Markdown, parseMarkdown} from "./src/components/Markdown.jsx";
               export {AssistantMessage} from "./src/components/Chat.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
  plugins: [{
    name: "stub-chat-children",
    setup(b) {
      b.onResolve({ filter: /^\.\/(Canvas|ChartView|SqlLab|ChatWorkflow|ChatPlatformRun)$/ },
                  ({ path: p }) => ({ path: p.slice(2), namespace: "chat-child" }));
      b.onLoad({ filter: /.*/, namespace: "chat-child" }, ({ path: name }) => ({
        contents: `export default function ${name}() { return null; }
                   export function PlatformComposer() { return null; } export function chartImage() { return null; }`,
        loader: "jsx",
      }));
    },
  }],
});
const compiled = new Module(path.join(frontend, "md-votes-test.cjs"));
compiled.filename = path.join(frontend, "md-votes-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Markdown, parseMarkdown, AssistantMessage } = compiled.exports;
const md = (text) => renderToStaticMarkup(React.createElement(Markdown, { text }));

// The answer from the screenshot that showed raw markdown.
const SCREENSHOT = `The table \`studio_dev.retail_qa.retail_orders\` contains the following columns:

1. **order_id** (bigint): A unique identifier for each order.
2. **customer_id** (string): The identifier for the customer who placed the order.
3. **store_id** (string): The identifier for the store where the order was made.

This table is essential for analyzing order-related data.`;

test("the screenshot's answer renders as text, a numbered list and bold names", () => {
  const html = md(SCREENSHOT);
  assert.match(html, /<code>studio_dev\.retail_qa\.retail_orders<\/code>/);
  assert.match(html, /<ol start="1"><li><strong>order_id<\/strong> \(bigint\): A unique/);
  assert.equal((html.match(/<li>/g) || []).length, 3);
  assert.doesNotMatch(html, /\*\*|`/);                 // no raw markdown left
  assert.equal((html.match(/<p>/g) || []).length, 2);
});

test("snake_case words stay as they are; real emphasis still works", () => {
  const html = md("order_id and store_id, *italic*, _also italic_, __bold__");
  assert.match(html, /order_id and store_id/);
  assert.match(html, /<em>italic<\/em>, <em>also italic<\/em>, <strong>bold<\/strong>/);
});

test("nothing in an answer becomes markup or a script link", () => {
  const html = md('<script>alert(1)</script> [click](javascript:alert(1)) [docs](https://example.com/x)\n'
                  + '<img src=x onerror=alert(1)>');
  assert.doesNotMatch(html, /<script|<img|href="javascript/i);
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /\[click\]\(javascript:alert\(1\)\)/);         // left as text
  assert.match(html, /<a href="https:\/\/example\.com\/x" target="_blank" rel="noopener noreferrer">docs<\/a>/);
});

test("tables, code blocks, headings and bullets", () => {
  const html = md("## Revenue\n\n| region | revenue |\n|---|---:|\n| EU | 125 |\n| US | 90 |\n\n"
                  + "```sql\nSELECT 1\n```\n\n- one\n- two");
  assert.match(html, /<h4>Revenue<\/h4>/);
  assert.match(html, /<th>region<\/th><th>revenue<\/th>/);
  assert.match(html, /<td>EU<\/td><td>125<\/td>/);
  assert.match(html, /<pre><code>SELECT 1<\/code><\/pre>/);
  assert.match(html, /<ul><li>one<\/li><li>two<\/li><\/ul>/);
});

test("a list split by blank lines is still one list", () => {
  const blocks = parseMarkdown("1. a\n\n2. b\n\n3. c\n\nAfter.");
  assert.deepEqual(blocks.map((b) => b.type), ["ol", "p"]);
  assert.deepEqual(blocks[0].items, ["a", "b", "c"]);
  assert.deepEqual(parseMarkdown("5. five\n6. six")[0].start, 5);
});

// ── Votes ───────────────────────────────────────────────────────────────

function messageHarness(props) {
  const values = [];
  let cursor = 0;
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
    useEffect() {},
  };
  return {
    tree() {
      cursor = 0;
      hooks = stateHooks;
      try { return AssistantMessage(props); } finally { hooks = null; }
    },
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
  return predicate(node) ? node : find(node.props.children, predicate);
}

test("a vote can be taken back and switched", async () => {
  const sent = [];
  const original = { fetch: globalThis.fetch, localStorage: globalThis.localStorage };
  globalThis.localStorage = { getItem() { return null; } };
  globalThis.fetch = async (url, options) => {
    sent.push({ url, body: JSON.parse(options.body) });
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  try {
    const msg = messageHarness({ m: { text: "EU leads.", trace_id: "t1", source: "demo", table: "*" } });
    const button = (label) => find(msg.tree(), (n) => n.type === "button" && n.props.children === label);
    const pressed = () => [button("👍").props["aria-pressed"], button("👎").props["aria-pressed"]];

    await button("👍").props.onClick();
    assert.deepEqual(pressed(), [true, false]);
    await button("👍").props.onClick();                       // again: take it back
    assert.deepEqual(pressed(), [false, false]);
    await button("👎").props.onClick();                       // the other one
    await button("👍").props.onClick();                       // switch
    assert.deepEqual(sent.map((r) => [r.url, r.body.score]), [
      ["/api/feedback", 1], ["/api/feedback", 0], ["/api/feedback", -1], ["/api/feedback", 1]]);
    assert.equal(button("👍").props.disabled, undefined);      // never locked
    assert.equal(button("👍").props.title, "Take this back");
  } finally {
    Object.assign(globalThis, original);
  }
});
