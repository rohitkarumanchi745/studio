// A conversation that is gone — deleted, unshared, or never in this database —
// must not trap the user. Opening its link used to show an empty chat, and every
// question sent there failed with "Conversation not found". Now the chat moves
// to a new one, says why, and keeps what the user asked or had typed.
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
    contents: `export {default as Chat} from "./src/components/Chat.jsx";`,
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
const compiled = new Module(path.join(frontend, "missing-conversation-test.cjs"));
compiled.filename = path.join(frontend, "missing-conversation-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...a) => (hooks ? hooks.useState(...a) : React.useState(...a)),
  useRef: (...a) => (hooks ? hooks.useRef(...a) : React.useRef(...a)),
  useEffect: (...a) => (hooks ? hooks.useEffect(...a) : React.useEffect(...a)),
  useMemo: (...a) => (hooks ? hooks.useMemo(...a) : React.useMemo(...a)),
} : id === "echarts" ? { init() { return {}; } } : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Chat } = compiled.exports;

const GONE = { status: 404, body: { detail: "Conversation not found" } };
const ok = (body) => ({ status: 200, body });

// The fake network holds its answers until settle() releases them: in the
// browser, the renders and effects a change sets off finish long before any
// response arrives, and the draft effects rely on that.
let inflight = [];

// Chat with hooks that keep state and run an effect only when its deps change,
// in declaration order — as React does — so the draft and load effects
// interleave the way they do in the browser. Each settle() round renders and
// runs the due effects; once a render leaves none due, the network answers.
function mount(props) {
  const values = [];
  let cursor = 0;
  let due = [];
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
    useEffect(effect, deps) {
      const i = cursor++;
      const prev = values[i];
      if (!prev || !deps || deps.some((d, k) => !Object.is(d, prev.deps[k]))) due.push(effect);
      values[i] = { deps };
    },
  };
  const render = () => {
    cursor = 0;
    due = [];
    hooks = stateHooks;
    try { return Chat(props); } finally { hooks = null; }
  };
  return {
    props,
    async settle() {
      for (let round = 0; round < 16; round++) {
        render();
        const run = due;
        run.forEach((effect) => effect());
        if (!run.length) inflight.splice(0).forEach((answer) => answer());
        for (let i = 0; i < 20; i++) await Promise.resolve();
      }
      const tree = render();
      return { tree, markup: renderToStaticMarkup(tree) };
    },
    tree: render,
  };
}

function findElement(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  return predicate(node) ? node : findElement(node.props.children, predicate);
}

// A fake server: `routes(method, url)` answers {status, body}; anything it
// leaves out is an empty 200. Records every request; a fresh localStorage.
async function withServer(routes, run) {
  const original = { fetch: globalThis.fetch, setTimeout: globalThis.setTimeout,
                     localStorage: globalThis.localStorage };
  const requests = [];
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    requests.push({ method, url, body: options.body ? JSON.parse(options.body) : undefined });
    const { status, body } = routes(method, url) || ok([]);
    return new Promise((resolve) => inflight.push(
      () => resolve({ ok: status < 400, status, json: async () => body })));
  };
  globalThis.setTimeout = () => 0;                 // no task polling
  inflight = [];
  try { await run({ requests, store }); }
  finally { Object.assign(globalThis, original); }
}

// Type and send; settle() then lets the send (and anything after it) answer.
async function ask(chat, text) {
  const input = findElement(chat.tree(), (el) => el.type === "input" && el.props.className === "pill-input");
  input.props.onChange({ target: { value: text } });
  const form = findElement(chat.tree(), (el) => el.type === "form" && el.props.className === "composer chat-composer");
  form.props.onSubmit({ preventDefault() {} });
  return chat.settle();
}

test("opening a conversation that is gone moves to a new chat and says why", async () => {
  await withServer((method, url) => url === "/api/conversations/dead/messages" ? GONE : null,
    async ({ store }) => {
      store.set("studio_draft:dead", "revenue by method");     // typed there before
      const left = [];
      const chat = mount({ conversationId: "dead", onConversationMissing: (id) => left.push(id) });
      await chat.settle();
      assert.deepEqual(left, ["dead"]);
      // The router answers by opening "/" in the same Chat instance.
      chat.props.conversationId = null;
      const { tree, markup } = await chat.settle();
      assert.match(markup, /class="chat-notice" role="status">That conversation isn&#x27;t available any more/);
      assert.match(markup, /What you asked is in the box below/);
      assert.doesNotMatch(markup, /class="error"/);
      const input = findElement(tree, (el) => el.type === "input" && el.props.className === "pill-input");
      assert.equal(input.props.value, "revenue by method");     // the draft came along
      assert.equal(store.get("studio_draft:new"), "revenue by method");
      assert.equal(store.has("studio_draft:dead"), false);

      // Opening a real conversation clears the notice.
      chat.props.conversationId = "c2";
      assert.doesNotMatch((await chat.settle()).markup, /chat-notice/);
    });
});

test("only a 404 leaves: an outage or a server error stays put", async () => {
  for (const status of [500, 503]) {
    await withServer((method, url) => url === "/api/conversations/c1/messages"
      ? { status, body: { detail: "down" } } : null, async () => {
      const left = [];
      const chat = mount({ conversationId: "c1", onConversationMissing: (id) => left.push(id) });
      assert.doesNotMatch((await chat.settle()).markup, /chat-notice/);
      assert.deepEqual(left, []);
    });
  }
});

test("a question sent into a conversation that vanished is kept for a new chat", async () => {
  let deleted = false;
  await withServer((method, url) => {
    if (url === "/api/conversations/c1/messages") return deleted ? GONE : ok([]);
    if (url === "/api/chat/background") return GONE;
    return null;
  }, async ({ requests, store }) => {
    const left = [];
    const chat = mount({ conversationId: "c1", onConversationMissing: (id) => left.push(id) });
    await chat.settle();
    deleted = true;                                // e.g. deleted in another tab
    await ask(chat, "Top 10 method by amount — bar chart");
    assert.deepEqual(left, ["c1"]);
    // It asked the server whether the conversation is really gone.
    assert.deepEqual(requests.filter((r) => r.url === "/api/conversations/c1/messages").length, 2);
    assert.equal(store.get("studio_draft:new"), "Top 10 method by amount — bar chart");
    chat.props.conversationId = null;
    const { tree, markup } = await chat.settle();
    assert.match(markup, /chat-notice/);
    assert.doesNotMatch(markup, /class="error"|Conversation not found/);
    const input = findElement(tree, (el) => el.type === "input" && el.props.className === "pill-input");
    assert.equal(input.props.value, "Top 10 method by amount — bar chart");
    assert.equal(findElement(tree, (el) => el.props?.className === "bubble-user"), null);
  });
});

test("a 404 for something else, or no way to leave, still shows the error", async () => {
  // The conversation is still there: the 404 was about something else.
  await withServer((method, url) => url === "/api/chat/background"
    ? { status: 404, body: { detail: "Source not found" } } : null, async () => {
    const left = [];
    const chat = mount({ conversationId: "c1", onConversationMissing: (id) => left.push(id) });
    await chat.settle();
    await ask(chat, "anything");
    assert.deepEqual(left, []);
    assert.match((await chat.settle()).markup, /class="error">Source not found</);
  });
  // A Chat mounted without a way to leave keeps the old behaviour.
  await withServer((method, url) => url === "/api/chat/background" ? GONE : null, async ({ requests }) => {
    const chat = mount({ conversationId: "c1" });
    await chat.settle();
    await ask(chat, "anything");
    assert.match((await chat.settle()).markup, /class="error">Conversation not found</);
    assert.equal(requests.filter((r) => r.url === "/api/conversations/c1/messages").length, 1);
  });
});

test("a new chat whose first question fails is not mistaken for a missing one", async () => {
  await withServer((method, url) => url === "/api/chat/background"
    ? { status: 404, body: { detail: "Source not found" } } : null, async ({ requests }) => {
    const left = [];
    const chat = mount({ conversationId: null, onConversationMissing: (id) => left.push(id) });
    await chat.settle();
    await ask(chat, "anything");
    assert.deepEqual(left, []);
    assert.equal(requests.some((r) => /\/api\/conversations\/.+\/messages/.test(r.url)), false);
    assert.match((await chat.settle()).markup, /class="error">Source not found</);
  });
});
