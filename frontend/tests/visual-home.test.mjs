// The D3 & artifacts window's home: a greeting, one question, the composer in
// the middle, quick starts that fill it (with the part to type selected),
// three bigger starts, ideas from the data and recent visuals. Only a new
// conversation in that window opens on it.
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
    contents: `export {default as Chat} from "./src/components/Chat.jsx";
      export * from "./src/components/VisualHome.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
  plugins: [
    {
      name: "url-stub",
      setup(b) {
        b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
        b.onLoad({ filter: /.*/, namespace: "url-stub" },
                 () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
      },
    },
    {
      name: "stub-chat-children",
      setup(b) {
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatWorkflow|ChatPlatformRun)$/ },
                    ({ path: p }) => ({ path: p.slice(2), namespace: "chat-child" }));
        b.onLoad({ filter: /.*/, namespace: "chat-child" }, ({ path: name }) => ({
          contents: `export default function ${name}() { return null; }
                     export function PlatformComposer() { return null; }`,
          loader: "jsx",
        }));
      },
    },
  ],
});
const compiled = new Module(path.join(frontend, "visual-home-test.cjs"));
compiled.filename = path.join(frontend, "visual-home-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Chat, greeting, firstName, fillStart, glyphFor, recentVisuals, ago, QUICK_STARTS, BIG_STARTS,
        VisualHomeHeader, VisualHomeStarts } = compiled.exports;

const stored = { studio_user: JSON.stringify({ id: "u1", name: "Rohit Karumanchi", role: "admin" }) };
globalThis.localStorage = {
  getItem: (k) => (k in stored ? stored[k] : null),
  setItem: (k, v) => { stored[k] = String(v); },
  removeItem: (k) => { delete stored[k]; },
};
const html = (component, props) => renderToStaticMarkup(React.createElement(component, props));

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
const text = (n) => (Array.isArray(n) ? n.map(text).join("") : typeof n === "string" ? n
  : React.isValidElement(n) ? text(n.props.children) : "");

const CONVS = [
  { id: "c1", title: "🎨 revenue share by region", created_at: Date.now() / 1000 - 120 },
  { id: "c2", title: "🎨 sales by country map", created_at: Date.now() / 1000 - 7200 },
  { id: "c3", title: "🎨 orders trend by month" },
  { id: "c4", title: "🎨 top products as a table" },
  { id: "c5", title: "🎨 an older one" },
];

// ── The pieces ──────────────────────────────────────────────────────────

test("the greeting follows the clock and the user's first name", () => {
  assert.deepEqual([greeting(8), greeting(13), greeting(20)],
                   ["Good morning", "Good afternoon", "Good evening"]);
  assert.equal(firstName({ name: "Rohit Karumanchi" }), "Rohit");
  assert.equal(firstName({ email: "ana.lopez@acme.com" }), "Ana");
  assert.equal(firstName({}), "");
  const head = html(VisualHomeHeader, { user: { name: "Rohit K" }, now: new Date(2026, 9, 26, 20) });
  assert.match(head, /class="vh-hello">Good evening, Rohit</);
  assert.match(head, /<h1 class="vh-title">What would you like to visualize today\?<\/h1>/);
  assert.match(html(VisualHomeHeader, { user: null, now: new Date(2026, 9, 26, 9) }),
               /class="vh-hello">Good morning</);
});

test("a start fills the composer with the part to type selected", () => {
  assert.deepEqual(fillStart("map"), { text: "Show … on a map", select: [5, 6] });
  assert.equal(fillStart("automate").text, "Every week, chart …");
  assert.deepEqual(fillStart("table"), { text: "Show … as a table", select: [5, 6] });
  assert.equal(fillStart("nope"), null);
  // Every start has a "…" to type over, and every big start is a quick start too.
  for (const s of QUICK_STARTS) assert.ok(s.text.includes("…"), s.key);
  for (const s of BIG_STARTS) assert.ok(fillStart(s.key), s.key);
});

test("recent visuals: newest first, four, without their 🎨, each with a fitting thumbnail", () => {
  const shown = recentVisuals(CONVS);
  assert.deepEqual(shown.map((c) => [c.id, c.title, c.glyph]), [
    ["c1", "revenue share by region", "donut"], ["c2", "sales by country map", "map"],
    ["c3", "orders trend by month", "line"], ["c4", "top products as a table", "table"]]);
  assert.equal(glyphFor("revenue by country"), "map");
  assert.equal(glyphFor("riders between stops on a route map"), "map");
  assert.equal(glyphFor("sales dashboard"), "grid");
  assert.equal(glyphFor("revenue by region"), "bars");
  assert.equal(recentVisuals([{ id: "x", title: "🎨 " }])[0].title, "Untitled visual");
  // The same question asked twice is one card (the newest), and the list
  // still fills to four from what is left.
  const twice = recentVisuals([{ id: "a", title: "🎨 How has impressions trended by month?" },
                               { id: "b", title: "🎨 how has impressions trended by month? " },
                               ...CONVS]);
  assert.deepEqual(twice.map((c) => c.id), ["a", "c1", "c2", "c3"]);
  assert.deepEqual([ago(100, 130), ago(0, 600), ago(0, 7300), ago(0, 200000)],
                   ["just now", "10 min ago", "2 h ago", "2 d ago"]);
});

test("the starts: quick chips, three cards, ideas and recent — each doing what it says", () => {
  const calls = [];
  const props = { onStart: (k) => calls.push(["start", k]), recent: CONVS, onOpen: (id) => calls.push(["open", id]),
                  onOpenCharts: () => calls.push(["charts"]), ideas: ["Top 10 platform by impressions"],
                  onIdea: (q) => calls.push(["idea", q]) };
  const markup = html(VisualHomeStarts, props);
  assert.equal((markup.match(/class="vh-chip"/g) || []).length, 6);
  assert.equal((markup.match(/class="vh-card"/g) || []).length, 3);
  assert.match(markup, /aria-label="Try one from your data"/);
  // Recent comes before the ideas, as the reference lays it out.
  assert.ok(markup.indexOf('aria-label="Recent"') < markup.indexOf("Try one from your data"));
  assert.equal((markup.match(/class="vh-recent-card"/g) || []).length, 4);
  assert.match(markup, /2 min ago/);
  const tree = VisualHomeStarts(props);
  const button = (label) => find(tree, (n) => n.type === "button" && text(n.props.children).includes(label));
  button("Map").props.onClick();
  find(tree, (n) => n.props?.className === "vh-card" && text(n.props.children).includes("Custom visual"))
    .props.onClick();
  button("Top 10 platform").props.onClick();
  button("sales by country map").props.onClick();
  button("See all charts").props.onClick();
  assert.deepEqual(calls, [["start", "map"], ["start", "custom"], ["idea", "Top 10 platform by impressions"],
                           ["open", "c2"], ["charts"]]);
  // A card draws its conversation's real latest chart when there is one;
  // the sketch only when there is nothing to draw.
  const withPreview = VisualHomeStarts({ ...props, previews: {
    c1: { columns: ["region", "revenue"], rows: [["West", 1]], chart: { type: "donut", x: "region", y: ["revenue"] } } } });
  const cards = [];
  (function walk(n) {
    if (Array.isArray(n)) return n.forEach(walk);
    if (!React.isValidElement(n)) return;
    if (n.props.className === "vh-recent-card") cards.push(n);
    walk(n.props.children);
  })(withPreview);
  const mini = (card) => find(card, (n) => n.type?.name === "MiniChart");
  assert.equal(mini(cards[0]).props.chart.type, "donut");
  assert.deepEqual(mini(cards[0]).props.rows, [["West", 1]]);
  assert.equal(mini(cards[1]), null);                              // c2: no preview — a sketch
  // Nothing to list: no empty sections.
  const bare = html(VisualHomeStarts, { onStart() {} });
  assert.doesNotMatch(bare, /from your data|Recent/);
});

// ── In the chat window ──────────────────────────────────────────────────

function chatHarness(props) {
  const values = [];
  let cursor = 0;
  const own = {
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
  return () => {
    cursor = 0;
    hooks = own;
    try { return Chat(props); } finally { hooks = null; }
  };
}

test("a new conversation in the window opens on the home; a chosen start fills the composer", () => {
  const render = chatHarness({ visualMode: true, conversationId: null, recent: CONVS,
                               onOpenConversation() {}, onOpenCharts() {} });
  const tree = render();
  const markup = renderToStaticMarkup(tree);
  assert.match(markup, /class="chatpane visual-chat visual-home"/);
  assert.match(markup, /Good (morning|afternoon|evening), Rohit/);
  assert.match(markup, /class="composer chat-composer is-home"/);
  assert.match(markup, /placeholder="Describe the visual you want to create…"/);
  assert.match(markup, /class="composer-box"><span class="vh-prompt-mark">/);   // the slim bar's mark
  assert.doesNotMatch(markup, /class="empty"/);                // not the old empty state
  const starts = find(tree, (n) => n.type === VisualHomeStarts);
  assert.equal(starts.props.recent, CONVS);
  starts.props.onStart("map");
  const input = find(render(), (n) => n.type === "textarea");
  assert.equal(input.props.value, "Show … on a map");
});

test("no home in an existing conversation, or in the ordinary chat", () => {
  const open = renderToStaticMarkup(chatHarness({ visualMode: true, conversationId: "c1", recent: CONVS })());
  assert.doesNotMatch(open, /vh-title|visual-home|is-home|vh-prompt-mark/);
  const ordinary = renderToStaticMarkup(chatHarness({ conversationId: null })());
  assert.doesNotMatch(ordinary, /vh-title|visual-home|is-home/);
  assert.match(ordinary, /class="empty"/);                       // its own empty state stays
});

test("with Auto on, ideas come from the user's own data, and say from where", async () => {
  const sources = [
    { name: "demo", configured: true, allowed: true },
    // Not usable: disconnected, or not this role's — passed over.
    { name: "parked", ctype: "snowflake", configured: false, allowed: true },
    { name: "theirs", ctype: "bigquery", configured: true, allowed: false },
    { name: "postgres", configured: true, allowed: true },
    { name: "lake", ctype: "databricks", configured: true, allowed: true },
  ];
  const asked = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url) => {
    asked.push(url);
    const body = url === "/api/catalog/sources" ? sources
      : url.includes("/suggestions") ? { suggestions: [{ question: "Revenue by month?", table: "sales" }] } : [];
    return { ok: true, status: 200, json: async () => body };
  };
  try {
    const values = [];
    const effects = [];
    let cursor = 0;
    const own = {
      useState(initial) {
        const i = cursor++;
        if (!(i in values)) values[i] = typeof initial === "function" ? initial() : initial;
        return [values[i], (next) => { values[i] = typeof next === "function" ? next(values[i]) : next; }];
      },
      useRef(initial) { const i = cursor++; if (!(i in values)) values[i] = { current: initial }; return values[i]; },
      useMemo(factory) { return factory(); },
      useEffect(effect) { effects.push(effect); },
    };
    const render = () => { cursor = 0; effects.length = 0; hooks = own;
                           try { return Chat({ visualMode: true, conversationId: null }); } finally { hooks = null; } };
    const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
    render(); effects.forEach((e) => e()); await settle();        // sources load
    render(); effects.forEach((e) => e()); await settle();        // ideas load from the real source
    const urls = asked.filter((u) => u.includes("/suggestions"));
    assert.ok(urls.at(-1).startsWith("/api/catalog/sources/lake/suggestions"), urls.join(" | "));
    const starts = find(render(), (n) => n.type === VisualHomeStarts);
    assert.equal(starts.props.ideasFrom, "lake");
    assert.deepEqual(starts.props.ideas, ["Revenue by month?"]);
    assert.match(renderToStaticMarkup(React.createElement(VisualHomeStarts, starts.props)),
                 /<span>Try one from lake<\/span>/);
    // Nothing of their own yet: the demo data, and it says so.
    assert.match(renderToStaticMarkup(React.createElement(VisualHomeStarts,
      { ...starts.props, ideasFrom: "demo" })), /Try one from the demo data/);
  } finally {
    globalThis.fetch = original;
  }
});
