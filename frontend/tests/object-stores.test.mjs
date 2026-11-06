// S3, Azure Blob and GCS set up on the Integrations screen: the connect form
// (keys plus the first files), a store's Add files panel, and the Governance
// datasets panel listing the stores connected here.
//
// What is pinned down: the format is a choice and follows the files' extension
// until the admin picks one; the name follows the bucket until the admin types
// one; only live store rows offer Add files; adding files asks the server to
// read them first and shows a refusal where it was typed; only the registry's
// rows can be removed.
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
    contents: `export {ConnectForm, Instances, StoreFiles, STORES, guessFormat, nameFromPath, storeTables, fileFormProblem}
      from "./src/components/DataConnections.jsx";
      export {storeSources} from "./src/components/Governance.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "object-stores-test.cjs"));
compiled.filename = path.join(frontend, "object-stores-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...args) => hooks ? hooks.useState(...args) : React.useState(...args),
  useEffect: (...args) => hooks ? hooks.useEffect(...args) : React.useEffect(...args),
  useMemo: (...args) => hooks ? hooks.useMemo(...args) : React.useMemo(...args),
  useRef: (...args) => hooks ? hooks.useRef(...args) : React.useRef(...args),
  useCallback: (...args) => hooks ? hooks.useCallback(...args) : React.useCallback(...args),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { ConnectForm, Instances, StoreFiles, STORES, guessFormat, nameFromPath, storeTables,
        fileFormProblem, storeSources } = compiled.exports;

// ── Harness: index-based hooks, a fake fetch ────────────────────────────

function harness(component, props) {
  const values = [], effects = [], memos = [];
  let cursor = 0, memoCursor = 0;
  const stateHooks = {
    useState(initial) {
      const index = cursor++;
      if (index >= values.length) values.push(typeof initial === "function" ? initial() : initial);
      return [values[index], (next) => { values[index] = typeof next === "function" ? next(values[index]) : next; }];
    },
    useEffect(effect) { effects.push(effect); },
    useMemo(factory) { memos[memoCursor++] = factory(); return memos[memoCursor - 1]; },
    useRef(initial) {
      const index = cursor++;
      if (index >= values.length) values.push({ current: initial });
      return values[index];
    },
    useCallback(fn) { return fn; },
  };
  return {
    render() {
      cursor = 0; memoCursor = 0; effects.length = 0;
      hooks = stateHooks;
      try { return renderToStaticMarkup(component(props)); } finally { hooks = null; }
    },
    start() { return effects.map((e) => e()).filter(Boolean); },
    tree() {
      cursor = 0; memoCursor = 0; effects.length = 0;
      hooks = stateHooks;
      try { return component(props); } finally { hooks = null; }
    },
  };
}

function findAll(node, predicate, out = []) {
  if (Array.isArray(node)) node.forEach((child) => findAll(child, predicate, out));
  else if (node && typeof node === "object") {
    if (predicate(node)) out.push(node);
    findAll(node.props?.children, predicate, out);
  }
  return out;
}
const find = (node, predicate) => findAll(node, predicate)[0] || null;
const text = (node) => Array.isArray(node) ? node.map(text).join("")
  : node && typeof node === "object" ? text(node.props?.children)
  : node === null || node === undefined || node === false ? "" : String(node);

/** Fake /api: `routes["METHOD /path"]` is a body, or {status, body}. */
async function withApi(routes, work) {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage,
                      window: globalThis.window, confirm: globalThis.confirm };
  const calls = [];
  globalThis.localStorage = { getItem: () => null };
  globalThis.window = { location: { search: "", pathname: "/", hash: "" },
                        history: { replaceState() {} } };
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || "GET";
    calls.push({ url, method, body: options.body ? JSON.parse(options.body) : null });
    const hit = routes[`${method} ${url.replace(/^\/api/, "")}`];
    const reply = hit && typeof hit === "object" && "status" in hit ? hit : { status: 200, body: hit ?? {} };
    return { status: reply.status, ok: reply.status < 400, json: async () => reply.body };
  };
  try {
    await work({ calls, flush: async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); } });
  } finally { Object.assign(globalThis, originals); }
}

// What /connections/types serves for S3 (connections.TYPES["s3"]).
const S3_TYPE = {
  ctype: "s3", label: "Amazon S3", dialect: "duckdb", ns: {}, spans: false,
  fields: [
    { key: "access_key_id", label: "Access key ID", required: true, secret: false, default: "", placeholder: "AKIA…" },
    { key: "secret_access_key", label: "Secret access key", required: true, secret: true, default: "", placeholder: "" },
    { key: "region", label: "Region", required: false, secret: false, default: "us-east-1", placeholder: "" },
    { key: "endpoint", label: "Endpoint", required: false, secret: false, default: "", placeholder: "" },
    { key: "session_token", label: "Session token", required: false, secret: true, default: "", placeholder: "" },
    { key: "path", label: "Files", required: true, secret: false, default: "", placeholder: "s3://bucket/sales/*.parquet" },
    { key: "dataset", label: "Name them as a table", required: false, secret: false, default: "", placeholder: "" },
    { key: "format", label: "Format", required: false, secret: false, default: "parquet", placeholder: "",
      options: ["parquet", "csv", "json"] },
  ],
};
const s3Tile = (instances = []) => ({ id: "s3", meta: { label: "Amazon S3" }, connectable: true,
                                      fields: S3_TYPE.fields, ns: {}, spans: false, instances });

function s3Form(extra = {}) {
  const done = [], errors = [];
  const panel = harness(ConnectForm, { tile: s3Tile(extra.instances), defaultName: "s3-2",
    takenNames: new Set(["s3", "azure_blob", "gcs", ...(extra.taken || [])]),
    onDone: (m) => done.push(m), onError: (m) => errors.push(m), onRemoved() {} });
  return { panel, done, errors };
}
const input = (tree, key) => find(tree, (n) => (n.type === "input" || n.type === "select")
  && n.props.name === `conn-s3-${key}`);

// ── Pure helpers ────────────────────────────────────────────────────────

test("a path's extension says its format; anything else says nothing", () => {
  assert.equal(guessFormat("s3://trips/rides/*.parquet"), "parquet");
  assert.equal(guessFormat("s3://trips/rides/2026-10.pq"), "parquet");
  assert.equal(guessFormat("gs://b/stations/*.CSV"), "csv");
  assert.equal(guessFormat("az://c/x/data.tsv"), "csv");
  assert.equal(guessFormat("s3://b/events/*.jsonl"), "json");
  assert.equal(guessFormat("s3://b/events/day.ndjson.gz"), "json");
  assert.equal(guessFormat("s3://b/events/day.csv.gz"), "csv");
  assert.equal(guessFormat("s3://b/events/*"), "");
  assert.equal(guessFormat("s3://b.parquet/events/"), "");   // a bucket's dots are not an extension
  assert.equal(guessFormat(""), "");
  assert.equal(guessFormat(undefined), "");
});

test("a store is named after its bucket, numbered past the names taken", () => {
  const taken = new Set(["s3", "trips"]);
  assert.equal(nameFromPath("s3://rides/2026/*.parquet", taken), "rides");
  assert.equal(nameFromPath("s3://trips/rides/*.parquet", taken), "trips-2");
  assert.equal(nameFromPath("s3://Acme.Data_Lake/x.csv", taken), "acme-data-lake");
  assert.equal(nameFromPath("az://container/x.csv", new Set()), "container");
  assert.equal(nameFromPath("s3://a/x.csv", taken), "");          // too short to be a name
  assert.equal(nameFromPath("s3://", taken), "");
  assert.equal(nameFromPath("trips/rides.csv", taken), "");      // no scheme, no bucket
  const long = nameFromPath(`s3://${"b".repeat(60)}/x.csv`, new Set());
  assert.ok(long.length <= 31 && /^[a-z0-9][a-z0-9_-]{1,30}$/.test(long), long);
});

test("only the registry's own rows can be removed", () => {
  const state = {
    datasets: [{ source: "trips", name: "stations", uri: "s3://trips/stations/*.csv", format: "csv" },
               { source: "s3", name: "stations", uri: "s3://other/*.csv", format: "csv" }],
    live: { trips: [{ name: "rides", uri: "s3://trips/rides/*.parquet", format: "parquet" },
                    { name: "stations", uri: "s3://trips/stations/*.csv", format: "csv" }] },
  };
  assert.deepEqual(storeTables(state, "trips").map((t) => [t.name, t.removable]),
                   [["rides", false], ["stations", true]]);
  assert.deepEqual(storeTables(state, "nowhere"), []);
  assert.deepEqual(storeTables(null, "trips"), []);
});

test("the add-files form says what is wrong before anything is sent", () => {
  const tables = [{ name: "rides" }];
  const ok = { name: "stations", uri: "s3://trips/stations/*.csv" };
  assert.equal(fileFormProblem(ok, tables, ["s3://"]), "");
  assert.match(fileFormProblem({ ...ok, uri: "gs://trips/x.csv" }, tables, ["s3://"]), /start with s3:\/\//);
  assert.match(fileFormProblem({ ...ok, uri: "az://c/x" }, [], ["az://", "abfss://"]),
               /^$/);
  assert.match(fileFormProblem({ ...ok, name: "1st" }, tables, ["s3://"]), /letters, digits and _/);
  assert.match(fileFormProblem({ ...ok, name: "my-table" }, tables, ["s3://"]), /letters, digits and _/);
  assert.match(fileFormProblem({ ...ok, name: "rides" }, tables, ["s3://"]), /already a table here/);
  // Nothing typed yet is not a problem to shout about.
  assert.equal(fileFormProblem({ name: "", uri: "" }, tables, ["s3://"]), "");
});

test("Governance lists the built-in stores first, then the ones connected here", () => {
  assert.deepEqual(storeSources({ s3: "s3", azure_blob: "azure_blob", gcs: "gcs",
                                  trips: "s3", lake: "azure_blob" }),
                   [["s3", "s3"], ["azure_blob", "azure_blob"], ["gcs", "gcs"],
                    ["lake", "azure_blob"], ["trips", "s3"]]);
  assert.deepEqual(storeSources(undefined).map(([s]) => s), ["s3", "azure_blob", "gcs"]);
  assert.deepEqual(STORES, ["s3", "azure_blob", "gcs"]);
});

// ── The connect form ────────────────────────────────────────────────────

test("a store's form offers the format as a choice and the path full width", () => {
  const { panel } = s3Form();
  const html = panel.render();
  assert.match(html, /<select name="conn-s3-format"[^>]*><option value="parquet" selected="">parquet<\/option><option value="csv">csv<\/option><option value="json">json<\/option><\/select>/);
  assert.match(html, /conn-field conn-field-wide"><span class="conn-label">Files/);
  assert.match(html, /add more from the source&#x27;s row \(＋ Add files\)/);
  // Secrets stay password inputs; a select carries no password-manager flags.
  assert.match(html, /type="password" name="conn-s3-secret_access_key"/);
});

test("typing the files sets their format and a name from the bucket", () => {
  const { panel } = s3Form({ taken: ["trips"] });
  input(panel.tree(), "path").props.onChange({ target: { value: "s3://trips/stations/*.csv" } });
  const tree = panel.tree();
  assert.equal(input(tree, "format").props.value, "csv");
  assert.ok(find(tree, (n) => n.type === "input" && n.props.value === "trips-2"), "named after the bucket");
});

test("a format or name the admin chose is never overwritten by the path", () => {
  const { panel } = s3Form();
  input(panel.tree(), "format").props.onChange({ target: { value: "json" } });
  find(panel.tree(), (n) => n.type === "input" && n.props.value === "s3-2")
    .props.onChange({ target: { value: "lake" } });
  input(panel.tree(), "path").props.onChange({ target: { value: "s3://trips/stations/*.csv" } });
  const tree = panel.tree();
  assert.equal(input(tree, "format").props.value, "json");
  assert.ok(find(tree, (n) => n.type === "input" && n.props.value === "lake"));
  assert.equal(find(tree, (n) => n.type === "input" && n.props.value === "trips"), null);
});

test("a store's test reads the files, and Connect posts the store with its format", async () => {
  await withApi({ "POST /connections/test": { ok: true, tables: 1, sample: ["stations"] },
                  "POST /connections": { name: "trips" } }, async ({ calls }) => {
    const { panel, done } = s3Form();
    const set = (k, v) => input(panel.tree(), k).props.onChange({ target: { value: v } });
    set("access_key_id", "AKIAEXAMPLE");
    set("secret_access_key", "s3cr3t");
    set("path", "s3://trips/stations/*.csv");
    set("dataset", "stations");
    const testButton = find(panel.tree(), (n) => n.type === "button" && text(n).includes("Test connection"));
    assert.equal(testButton.props.disabled, false);
    await testButton.props.onClick();
    assert.match(panel.render(), /Signed in and read the files — they become the table “stations”/);
    const save = find(panel.tree(), (n) => n.type === "button" && n.props.className === "primary");
    await save.props.onClick();
    const post = calls.find((c) => c.url === "/api/connections" && c.method === "POST");
    assert.deepEqual(post.body, {
      name: "trips", ctype: "s3",
      config: { region: "us-east-1", format: "csv", access_key_id: "AKIAEXAMPLE",
                secret_access_key: "s3cr3t", path: "s3://trips/stations/*.csv", dataset: "stations" },
    });
    assert.match(done[0], /“trips” is now a source/);
  });
});

test("a refused read marks the field it points at", async () => {
  const said = "Signed in, but there are no files at that path — check the bucket and the pattern.";
  await withApi({ "POST /connections/test": { ok: false, error: said, fields: { path: said },
                                              stage: "signed_in" } }, async () => {
    const { panel } = s3Form();
    const set = (k, v) => input(panel.tree(), k).props.onChange({ target: { value: v } });
    set("access_key_id", "AKIAEXAMPLE");
    set("secret_access_key", "s3cr3t");
    set("path", "s3://trips/nothing/*.parquet");
    await find(panel.tree(), (n) => n.type === "button" && text(n).includes("Test connection")).props.onClick();
    const tree = panel.tree();
    assert.equal(input(tree, "path").props["aria-invalid"], "true");
    assert.equal(input(tree, "access_key_id").props["aria-invalid"], undefined);
    assert.match(panel.render(), /Signed in, but not saved — fix the marked field/);
  });
});

// ── Rows: Add files ─────────────────────────────────────────────────────

test("only a live store row offers Add files", () => {
  const rows = [
    { name: "s3", kind: "env", mode: "environment", configured: true },
    { name: "trips", kind: "user", connId: "c1", configured: true },
    { name: "parked", kind: "user", connId: "c2", configured: false, disconnected: true },
    { name: "unreadable", kind: "user", connId: "c3", configured: false },
  ];
  const rowsOf = (props) => renderToStaticMarkup(React.createElement(Instances, props));
  const html = rowsOf({ tile: s3Tile(rows), onAct() {}, onManage() {} });
  const rowsHtml = html.split('class="dbconn-row').slice(1);
  assert.deepEqual(rowsHtml.map((r) => r.includes("＋ Add files")), [true, true, false, false]);
  // Not on other kinds of source, and not for a viewer (no actions at all).
  const pg = { id: "postgres", meta: { label: "PostgreSQL" }, ns: { schema: "schema" }, instances: [rows[1]] };
  assert.doesNotMatch(rowsOf({ tile: pg, onAct() {} }), /Add files/);
  assert.doesNotMatch(rowsOf({ tile: s3Tile(rows) }), /Add files/);
});

test("Add files on a row opens that store's files under the rows", () => {
  const rows = [{ name: "trips", kind: "user", connId: "c1", configured: true }];
  const { panel } = s3Form({ instances: rows });
  const instances = find(panel.tree(), (n) => n.type?.name === "Instances");
  instances.props.onAct(rows[0], "files");
  const files = find(panel.tree(), (n) => n.type?.name === "StoreFiles");
  assert.equal(files.props.source, "trips");
  instances.props.onAct(rows[0], "files");      // the same button closes it
  assert.equal(find(panel.tree(), (n) => n.type?.name === "StoreFiles"), null);
});

// ── The files panel ─────────────────────────────────────────────────────

const LISTING = {
  datasets: [{ source: "trips", name: "stations", uri: "s3://trips/stations/*.csv", format: "csv" }],
  env_datasets: [],
  sources: { s3: ["s3://"], trips: ["s3://"] },
  kinds: { s3: "s3", azure_blob: "azure_blob", gcs: "gcs", trips: "s3" },
  live: { trips: [{ source: "trips", name: "rides", uri: "s3://trips/rides/*.parquet", format: "parquet" },
                  { source: "trips", name: "stations", uri: "s3://trips/stations/*.csv", format: "csv" }] },
  formats: ["csv", "json", "parquet"],
};

async function filesPanel(routes, work) {
  await withApi({ "GET /settings/objectstore": LISTING, ...routes }, async (api) => {
    const errors = [];
    const panel = harness(StoreFiles, { source: "trips", onError: (m) => errors.push(m), onClose() {} });
    panel.render();
    panel.start();
    await api.flush();
    await work({ ...api, panel, errors });
  });
}

test("the files panel lists each table, and only added ones can be removed", async () => {
  await filesPanel({}, async ({ panel }) => {
    const html = panel.render();
    assert.match(html, /Files trips reads, each as a table/);
    assert.match(html, /<b>rides<\/b><span class="query-tag">parquet<\/span><span class="meta conn-file-uri" title="s3:\/\/trips\/rides\/\*\.parquet">/);
    assert.match(html, /rides<\/b>.*?part of trips&#x27;s setup/);
    assert.match(html, /stations<\/b>.*?✕ Remove/);
    assert.match(html, /placeholder="s3:\/\/bucket\/sales\/\*\.parquet — a file, a folder\/\* or a pattern"/);
  });
});

test("adding files asks the server to read them first, then queues a scan", async () => {
  await filesPanel({ "POST /settings/objectstore": { status: 201, body: { datasets: [] } } },
    async ({ panel, calls, flush }) => {
      const field = (start) => find(panel.tree(), (n) => n.type === "input"
        && n.props.placeholder?.startsWith(start));
      field("s3://").props.onChange({ target: { value: "s3://trips/events/*.jsonl" } });
      field("the table").props.onChange({ target: { value: "events" } });
      // The extension picked the format.
      assert.equal(find(panel.tree(), (n) => n.type === "select").props.value, "json");
      const add = find(panel.tree(), (n) => n.type === "button" && n.props.className === "primary");
      assert.equal(add.props.disabled, false);
      await add.props.onClick();
      await flush();
      const post = calls.find((c) => c.method === "POST" && c.url === "/api/settings/objectstore");
      assert.deepEqual(post.body, { source: "trips", name: "events", uri: "s3://trips/events/*.jsonl",
                                    format: "json", check: true });
      assert.ok(calls.some((c) => c.method === "POST" && c.url === "/api/metagraph/sources/trips/discover"));
      // Reloaded after, and the form is clear for the next.
      assert.equal(calls.filter((c) => c.method === "GET").length, 2);
      assert.match(panel.render(), /✓ Read the files — “events” is a table on trips now/);
      assert.equal(find(panel.tree(), (n) => n.type === "input" && n.props.value === "events"), null);
    });
});

test("a name already used, or a path from another store, cannot be sent", async () => {
  await filesPanel({}, async ({ panel }) => {
    const inputs = () => findAll(panel.tree(), (n) => n.type === "input");
    inputs()[0].props.onChange({ target: { value: "gs://trips/x.csv" } });
    inputs()[1].props.onChange({ target: { value: "stations" } });
    const html = panel.render();
    assert.match(html, /Files here start with s3:\/\//);
    const add = find(panel.tree(), (n) => n.type === "button" && n.props.className === "primary");
    assert.equal(add.props.disabled, true);
    inputs()[0].props.onChange({ target: { value: "s3://trips/x.csv" } });
    assert.match(panel.render(), /“stations” is already a table here/);
  });
});

test("a refused read is shown on the field it points at; any other refusal under the form", async () => {
  const said = "Signed in, but there are no files at that path — check the bucket and the pattern.";
  await filesPanel({ "POST /settings/objectstore": { status: 400, body: { detail: {
      error: said, stage: "signed_in", fields: { uri: said } } } } },
    async ({ panel, calls }) => {
      const inputs = () => findAll(panel.tree(), (n) => n.type === "input");
      inputs()[0].props.onChange({ target: { value: "s3://trips/ghost/*.parquet" } });
      inputs()[1].props.onChange({ target: { value: "ghost" } });
      await find(panel.tree(), (n) => n.type === "button" && n.props.className === "primary").props.onClick();
      assert.equal(inputs()[0].props["aria-invalid"], "true");
      assert.match(panel.render(), /conn-field-error">Signed in, but there are no files at that path/);
      // No scan is queued for files that were not added.
      assert.equal(calls.some((c) => c.url.endsWith("/discover")), false);
      // Typing again clears the mark.
      inputs()[0].props.onChange({ target: { value: "s3://trips/rides/*.parquet" } });
      assert.equal(inputs()[0].props["aria-invalid"], undefined);
    });
  const refused = "The store refused these credentials — check them, and that they may read this bucket or container.";
  await filesPanel({ "POST /settings/objectstore": { status: 400, body: { detail: {
      error: refused, stage: "sign_in", fields: {} } } } },
    async ({ panel }) => {
      const inputs = () => findAll(panel.tree(), (n) => n.type === "input");
      inputs()[0].props.onChange({ target: { value: "s3://private/x.parquet" } });
      inputs()[1].props.onChange({ target: { value: "secret_stuff" } });
      await find(panel.tree(), (n) => n.type === "button" && n.props.className === "primary").props.onClick();
      assert.match(panel.render(), /<div class="error">The store refused these credentials/);
    });
});

test("removing a table asks first, then deletes only that registry row", async () => {
  await filesPanel({ "DELETE /settings/objectstore/trips/stations": { datasets: [] } },
    async ({ panel, calls }) => {
      globalThis.confirm = () => false;
      const remove = () => find(panel.tree(), (n) => n.type === "button" && text(n) === "✕ Remove");
      await remove().props.onClick();
      assert.equal(calls.some((c) => c.method === "DELETE"), false);
      globalThis.confirm = (q) => /Stop reading “stations” from trips\? The files stay where they are/.test(q);
      await remove().props.onClick();
      assert.deepEqual(calls.filter((c) => c.method === "DELETE").map((c) => c.url),
                       ["/api/settings/objectstore/trips/stations"]);
    });
});
