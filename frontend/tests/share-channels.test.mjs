// Send an answer to Microsoft Teams or Slack.
//
// Pins down: every answer (with text or SQL) has a ↗ Share menu naming both;
// picking one opens the send panel for that kind only, listing the channels the
// user may use; Send posts the answer — only the rows the message shows, with
// the result's size, SQL only when asked — and says where it went or why not;
// with no channel it says who adds one. On the connections screen Teams and
// Slack are tiles on their own shelf, listing channels (never as connected
// sources), and only an admin gets the add form.
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
    contents: `export {AssistantMessage} from "./src/components/Chat.jsx";
      export {default as SharePanel, sendBody, PREVIEW_ROWS} from "./src/components/SharePanel.jsx";
      export {default as DestinationsPanel, rolesText} from "./src/components/DestinationsPanel.jsx";
      export {buildTiles, ConnectedSources, SEND_TO} from "./src/components/DataConnections.jsx";
      export {CATEGORIES, metaFor} from "./src/components/sourceCatalog.jsx";`,
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
      name: "stub-children",
      setup(b) {
        b.onResolve({ filter: /^\.\/(Canvas|SqlLab|ChatWorkflow|ChatPlatformRun|ChartView)$/ },
                    ({ path: p }) => ({ path: p.slice(2), namespace: "child" }));
        b.onLoad({ filter: /.*/, namespace: "child" }, ({ path: name }) => ({
          contents: `export default function ${name}() { return null; }
                     export function PlatformComposer() { return null; } export function chartImage() { return null; }`,
          loader: "jsx",
        }));
      },
    },
  ],
});
const compiled = new Module(path.join(frontend, "share-channels-test.cjs"));
compiled.filename = path.join(frontend, "share-channels-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
  useCallback: (...a) => (hooks ? hooks.useCallback(...a) : React.useCallback(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { AssistantMessage, SharePanel, sendBody, PREVIEW_ROWS, DestinationsPanel, rolesText,
        buildTiles, ConnectedSources, SEND_TO, CATEGORIES, metaFor } = compiled.exports;

function harness(component, props) {
  const values = [];
  const effects = [];
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
    useCallback(fn) { return fn; },
    useEffect(effect) { effects.push(effect); },
  };
  return {
    tree() {
      cursor = 0;
      effects.length = 0;
      hooks = stateHooks;
      try { return component(props); } finally { hooks = null; }
    },
    markup() { return renderToStaticMarkup(this.tree()); },
    start() { this.tree(); effects.forEach((e) => e()); },
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
const text = (n) => (Array.isArray(n) ? n.map(text).join("") : typeof n === "string" ? n
  : React.isValidElement(n) ? text(n.props.children) : "");
const button = (tree, label) => find(tree, (n) => n.type === "button" && text(n.props.children).includes(label));
const flush = async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); };

/** fetch and the signed-in user stood in for; `answer(url, body)` → [status, json]. */
async function withNetwork(user, answer, run) {
  const original = { fetch: globalThis.fetch, localStorage: globalThis.localStorage,
                     confirm: globalThis.confirm };
  const sent = [];
  const store = new Map([["studio_user", JSON.stringify(user)]]);
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  globalThis.confirm = () => true;
  globalThis.fetch = async (url, options = {}) => {
    const body = options.body && JSON.parse(options.body);
    sent.push({ url, method: options.method || "GET", body });
    const [status, json] = answer(url, body, options.method || "GET");
    return { ok: status < 400, status, json: async () => json };
  };
  try { await run(sent); } finally { Object.assign(globalThis, original); }
}

const ANALYST = { id: "u2", role: "analyst", email: "analyst@studio.local" };
const ADMIN = { id: "u1", role: "admin", email: "admin@studio.local" };
const CHANNELS = [
  { id: "d1", name: "#sales-weekly", kind: "slack", roles: ["admin", "analyst"] },
  { id: "d2", name: "Data team · General", kind: "teams", roles: ["admin", "analyst", "viewer"] },
  { id: "d3", name: "#exec", kind: "slack", roles: ["admin", "analyst"] },
  { id: "d4", name: "Data team · Alerts", kind: "teams", roles: ["admin", "analyst"] },
];
const ROWS = Array.from({ length: 40 }, (_, i) => [`R${i}`, i * 10]);
const ANSWER = {
  text: "**West** leads.", source: "shop", table: "*", model: "claude", trace_id: "tr1",
  sql: "SELECT region, SUM(amount) AS revenue FROM orders GROUP BY region",
  columns: ["region", "revenue"], rows: ROWS,
  chart: { type: "bar", x: "region", y: ["revenue"], title: "Revenue by region" },
};

test("every answer has a Share menu naming Teams and Slack", () => {
  const html = harness(AssistantMessage, { m: ANSWER, onOpenCanvas() {} }).markup();
  const menu = html.slice(html.indexOf("share-menu"));
  assert.match(menu, /↗ Share/);
  assert.ok(menu.includes("Microsoft Teams") && menu.includes("Slack"));
  assert.equal((menu.match(/class="srcicon"/g) || []).length, 2);   // each with its mark
  // A text-only answer (a document answer, no SQL) can be sent too…
  const doc = harness(AssistantMessage, { m: { text: "Policy says 30 days.", mode: "rag" } }).markup();
  assert.match(doc, /↗ Share/);
  // …but a stopped reply has nothing to send.
  const stopped = harness(AssistantMessage, { m: { text: "Stopped.", mode: "stopped" } }).markup();
  assert.doesNotMatch(stopped, /↗ Share/);
});

test("picking Slack opens the send panel for this answer under it, and closes the menu", () => {
  const h = harness(AssistantMessage, { m: ANSWER, requirement: "Revenue by region",
                                        conversationId: "c1", onOpenCanvas() {} });
  let closed = false;
  const details = { removeAttribute: (a) => { closed = a === "open"; } };
  button(h.tree(), "Slack").props.onClick({ currentTarget: { closest: () => details } });
  assert.ok(closed);
  const panel = find(h.tree(), (n) => n.type === SharePanel);
  assert.equal(panel.props.kind, "slack");
  assert.equal(panel.props.answer.title, "Revenue by region");
  assert.equal(panel.props.answer.conversationId, "c1");
  assert.equal(panel.props.answer.rows.length, 40);
  assert.equal(panel.props.answer.sql, ANSWER.sql);
  panel.props.onClose();
  assert.equal(find(h.tree(), (n) => n.type === SharePanel), null);
  // Rows that expired under retention are not sent.
  const old = harness(AssistantMessage, { m: { ...ANSWER, rows_purged: true }, onOpenCanvas() {} });
  button(old.tree(), "Microsoft Teams").props.onClick();
  const p2 = find(old.tree(), (n) => n.type === SharePanel);
  assert.equal(p2.props.kind, "teams");
  assert.deepEqual([p2.props.answer.columns, p2.props.answer.rows], [[], []]);
});

test("the request carries only the rows the message shows, the size, and SQL when asked", () => {
  const answer = { title: "Revenue", text: "West leads.", columns: ["region", "revenue"], rows: ROWS,
                   sql: "SELECT 1", source: "shop", conversationId: "c1" };
  const body = sendBody({ destination: "d1", note: "  For Monday  ", includeRows: true, includeSql: false },
                        answer);
  assert.equal(PREVIEW_ROWS, 15);
  assert.equal(body.rows.length, 15);
  assert.equal(body.row_count, 40);
  assert.deepEqual([body.destination_id, body.note, body.include_sql, body.include_rows, body.conversation_id],
                   ["d1", "For Monday", false, true, "c1"]);
  // SQL only when asked and there is some; a blank note is no note.
  assert.equal(sendBody({ destination: "d1", note: " ", includeRows: false, includeSql: true }, answer).include_sql,
               true);
  const noSql = sendBody({ destination: "d1", note: " ", includeRows: false, includeSql: true },
                         { ...answer, sql: null });
  assert.deepEqual([noSql.include_sql, noSql.note, noSql.include_rows], [false, null, false]);
});

test("Send lists only this kind's channels and says where the answer went", async () => {
  const answer = { title: "Revenue", text: "West leads.", columns: ["region", "revenue"], rows: ROWS,
                   sql: "SELECT 1", source: "shop", conversationId: "c1" };
  await withNetwork(ANALYST, (url, body) => url === "/api/share/destinations"
    ? [200, { destinations: CHANNELS }]
    : [200, { sent: true, destination: "#exec", kind: "slack" }], async (sent) => {
    localStorage.setItem("studio_share_last:teams", "d4");        // Teams' own last pick
    const h = harness(SharePanel, { kind: "slack", answer, onClose() {} });
    h.start();
    await flush();
    const html = h.markup();
    assert.match(html, /Send to Slack/);
    assert.ok(html.includes("#sales-weekly") && html.includes("#exec"));
    assert.ok(!html.includes("Data team"));                      // a Teams channel
    assert.match(html, /the table \(first 15 of 40 rows\)/);
    assert.equal(find(h.tree(), (n) => n.type === "select").props.value, "d1");
    assert.match(html, /the SQL/);
    // Pick #exec, add a note, send.
    const tree = h.tree();
    find(tree, (n) => n.type === "select").props.onChange({ target: { value: "d3" } });
    find(h.tree(), (n) => n.type === "textarea").props.onChange({ target: { value: "FYI" } });
    await button(h.tree(), "Send to Slack").props.onClick();
    const post = sent.find((r) => r.url === "/api/share/send");
    assert.equal(post.method, "POST");
    assert.deepEqual([post.body.destination_id, post.body.note, post.body.rows.length, post.body.row_count,
                      post.body.include_sql], ["d3", "FYI", 15, 40, false]);
    assert.match(h.markup(), /✓ Sent to #exec/);
    // Next time, Slack starts on the channel last sent to; Teams is unaffected.
    const again = harness(SharePanel, { kind: "slack", answer, onClose() {} });
    again.start();
    await flush();
    assert.equal(find(again.tree(), (n) => n.type === "select").props.value, "d3");
    const teams = harness(SharePanel, { kind: "teams", answer, onClose() {} });
    teams.start();
    await flush();
    assert.equal(find(teams.tree(), (n) => n.type === "select").props.value, "d4");
  });
});

test("a refused send says why, and no channel says who adds one", async () => {
  const answer = { title: "Revenue", text: "x", columns: [], rows: [], sql: null };
  await withNetwork(ANALYST, (url) => url === "/api/share/destinations"
    ? [200, { destinations: CHANNELS }]
    : [502, { detail: "The channel refused the message (403)." }], async () => {
    const h = harness(SharePanel, { kind: "teams", answer, onClose() {} });
    h.start();
    await flush();
    const html = h.markup();
    assert.doesNotMatch(html, /the table|the SQL/);               // nothing to include
    await button(h.tree(), "Send to Microsoft Teams").props.onClick();
    assert.match(h.markup(), /Not sent: The channel refused the message \(403\)\./);
  });
  for (const [user, said] of [[ANALYST, /Ask an administrator to add one/],
                              [ADMIN, /Integrations → Slack[\s\S]*hooks\.slack\.com/]]) {
    await withNetwork(user, () => [200, { destinations: CHANNELS.filter((c) => c.kind === "teams") }],
      async () => {
        const h = harness(SharePanel, { kind: "slack", answer, onClose() {} });
        h.start();
        await flush();
        const html = h.markup();
        assert.match(html, /No Slack channel is set up for you yet/);
        assert.match(html, said);
        assert.equal(button(h.tree(), "Send to Slack"), null);
      });
  }
});

test("Teams and Slack are tiles on their own shelf, listing channels, never connected sources", () => {
  assert.deepEqual(SEND_TO, ["teams", "slack"]);
  assert.ok(CATEGORIES.some((c) => c.key === "messaging"));
  assert.deepEqual([metaFor("teams").label, metaFor("slack").label, metaFor("teams").category],
                   ["Microsoft Teams", "Slack", "messaging"]);
  const tiles = Object.fromEntries(buildTiles({ channels: [...CHANNELS, { id: "x", kind: "discord", name: "z" }] })
    .map((t) => [t.id, t]));
  assert.deepEqual(tiles.slack.instances.map((i) => i.name), ["#sales-weekly", "#exec"]);
  assert.deepEqual(tiles.teams.instances.map((i) => [i.name, i.id, i.kind]),
                   [["Data team · General", "d2", "channel"], ["Data team · Alerts", "d4", "channel"]]);
  assert.equal(tiles.discord, undefined);                          // not a kind Studio sends to
  assert.equal(tiles.slack.connectable, false);
  // Connected sources lists what Studio reads; a channel is not one.
  const out = ConnectedSources({ tiles: Object.values(tiles), scans: {}, rescanning: [],
                                 onRediscover() {}, onPick() {} });
  assert.equal(out, null);
  assert.deepEqual(buildTiles({ q: "slack" }).map((t) => t.id), ["slack"]);
});

test("only an admin adds a channel: roles chosen, URL sent once, a test message by default", async () => {
  const notes = [], errors = [];
  let reloaded = 0;
  await withNetwork(ADMIN, (url, body, method) => method === "POST"
    ? [201, { id: "d9", name: body.name, kind: body.kind, roles: body.roles }]
    : [200, { removed: true }], async (sent) => {
    const props = { kind: "slack", channels: [{ id: "d1", name: "#sales-weekly", roles: ["admin", "analyst"] }],
                    isAdmin: true, reload: () => reloaded++, onNote: (n) => notes.push(n),
                    onError: (e) => errors.push(e) };
    const h = harness(DestinationsPanel, props);
    const html = h.markup();
    assert.match(html, /#sales-weekly<\/b><span class="meta">sent to by admins and analysts/);
    assert.match(html, /Add a Slack channel/);
    assert.match(html, /type="password"[^>]*aria-label="Webhook URL"|aria-label="Webhook URL"[^>]*type="password"/);
    assert.equal(button(h.tree(), "Add channel").props.disabled, true);   // nothing typed yet
    const input = (label) => find(h.tree(), (n) => n.type === "input" && n.props["aria-label"] === label);
    input("Channel name").props.onChange({ target: { type: "text", value: "#exec" } });
    input("Webhook URL").props.onChange({ target: { type: "password", value: "https://hooks.slack.com/services/T/B/X" } });
    find(h.tree(), (n) => n.type === "input" && n.props.type === "checkbox" && n.props.checked === false
      && n.props.onChange).props.onChange({ target: { type: "checkbox", checked: true } });   // viewers too
    await button(h.tree(), "Add channel").props.onClick();
    const post = sent.find((r) => r.method === "POST");
    assert.equal(post.url, "/api/share/destinations");
    assert.deepEqual(post.body, { name: "#exec", kind: "slack", url: "https://hooks.slack.com/services/T/B/X",
                                  roles: ["admin", "analyst", "viewer"], test: true });
    assert.match(notes.at(-1), /“#exec” added — a test message was posted there\./);
    assert.equal(reloaded, 1);
    assert.equal(input("Webhook URL").props.value, "");            // not kept on screen
    await button(h.tree(), "Remove").props.onClick();
    assert.deepEqual(sent.at(-1), { url: "/api/share/destinations/d1", method: "DELETE", body: undefined });
    assert.deepEqual(errors.filter(Boolean), []);
  });
  // Everyone else sees the channels, no form, no remove.
  const viewer = harness(DestinationsPanel, { kind: "teams", channels: [], isAdmin: false,
                                              reload() {}, onNote() {}, onError() {} }).markup();
  assert.match(viewer, /No Microsoft Teams channel yet\. An administrator adds them\./);
  assert.doesNotMatch(viewer, /Webhook URL|Remove/);
  assert.equal(rolesText(["viewer", "admin", "analyst"]), "admins, analysts and viewers");
  assert.equal(rolesText(["admin"]), "admins");
});
