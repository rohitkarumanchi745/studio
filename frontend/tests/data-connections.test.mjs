// The "Connect to data" grid: tile model + a real render, no browser.
//
// The grid is the only place a user picks a source, so the two things worth
// pinning down are that every source lands on exactly one tile with an honest
// status, and that a non-admin never sees a credential form.
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
    contents: `export {default as DataConnections, buildTiles, suggestName, planConnections, connectionReadiness, Namespaces, namespaceKey, remainingPickedNamespaces, GithubPanel, githubRegistrationReady, ConnectForm, instanceBadge, envActions}
      from "./src/components/DataConnections.jsx";
      export {metaFor} from "./src/components/sourceCatalog.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "connections-test.cjs"));
compiled.filename = path.join(frontend, "connections-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...args) => hooks ? hooks.useState(...args) : React.useState(...args),
  useEffect: (...args) => hooks ? hooks.useEffect(...args) : React.useEffect(...args),
  useMemo: (...args) => hooks ? hooks.useMemo(...args) : React.useMemo(...args),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { DataConnections, buildTiles, suggestName, planConnections, connectionReadiness,
        Namespaces, namespaceKey, remainingPickedNamespaces, GithubPanel,
        githubRegistrationReady, metaFor, ConnectForm, instanceBadge, envActions } = compiled.exports;

// What the four endpoints answer on a deployment with Postgres configured from
// the environment, one user-added Snowflake, and S3 keys that were never set.
const TYPES = [
  { ctype: "postgres", label: "PostgreSQL", dialect: "postgres",
    ns: { schema: "schema", database: "" },
    fields: [{ key: "dsn", label: "Connection string (DSN)", required: true, secret: true,
               default: "", placeholder: "postgresql://…" },
             { key: "schema", label: "Schema", required: false, secret: false, default: "public", placeholder: "" }] },
  { ctype: "snowflake", label: "Snowflake", dialect: "snowflake",
    ns: { schema: "schema", database: "database" },
    fields: [{ key: "account", label: "Account", required: true, secret: false, default: "", placeholder: "org-account" }] },
];
const CONNS = [{ id: "c1", name: "sales-sf", ctype: "snowflake", type_label: "Snowflake",
                 hint: "acme-prod", configured: true }];
const SOURCES = [
  { name: "demo", dialect: "sqlite", configured: true, allowed: true },
  { name: "postgres", dialect: "postgres", configured: true, allowed: true },
  { name: "snowflake", dialect: "snowflake", configured: false, allowed: true },
  { name: "s3", dialect: "duckdb", configured: false, allowed: false },
  { name: "sales-sf", dialect: "snowflake", configured: true, allowed: true },
];
const REPOS = [{ id: "r1", name: "sales-pipelines", url: "https://github.com/acme/sales-pipelines",
                 default_branch: "main", description: "Curated SQL examples" }];

const byId = (tiles) => Object.fromEntries(tiles.map((t) => [t.id, t]));

test("every source lands on exactly one tile, and user connections fold into their type", () => {
  const tiles = byId(buildTiles({ types: TYPES, conns: CONNS, sources: SOURCES }));

  // The user's Snowflake connection is an instance of the Snowflake tile, not
  // a tile of its own — otherwise the grid grows a tile per credential.
  assert.equal(tiles["sales-sf"], undefined);
  assert.deepEqual(tiles.snowflake.instances.map((i) => [i.name, i.kind, i.configured]),
                   [["snowflake", "env", false], ["sales-sf", "user", true]]);
  assert.equal(tiles.snowflake.instances[1].connId, "c1");

  // A type with no live source still gets a tile — that is the point of the
  // grid — and an env-only source gets one without becoming connectable.
  assert.equal(tiles.postgres.connectable, true);
  assert.equal(tiles.s3.connectable, false);
  assert.equal(tiles.s3.instances.length, 1);
  assert.equal(tiles.m365.instances.length, 0);
  assert.equal(tiles.github.instances.length, 0);
  assert.equal(tiles.github.connectable, false); // not a warehouse connection

  // Sources the catalog serves but this bundle has never heard of still render.
  const unknown = byId(buildTiles({ sources: [{ name: "duck_lake", configured: true }] }));
  assert.equal(unknown.duck_lake.meta.label, "Duck Lake");
});

test("search matches the display name, not just the backend identifier", () => {
  const hit = buildTiles({ types: TYPES, conns: CONNS, sources: SOURCES, q: "postgre" });
  assert.deepEqual(hit.map((t) => t.id), ["postgres"]);
  // "Microsoft 365" is findable even though its id is "m365".
  assert.deepEqual(buildTiles({ types: TYPES, sources: [], q: "microsoft" }).map((t) => t.id), ["m365"]);
  assert.deepEqual(buildTiles({ repos: REPOS, q: "github" }).map((t) => t.id), ["github"]);
  assert.equal(buildTiles({ repos: REPOS }).find((t) => t.id === "github").instances[0].name,
               "sales-pipelines");
  assert.deepEqual(buildTiles({ types: TYPES, sources: SOURCES, q: "zzz" }), []);
});

test("a new connection never collides with an existing source name", () => {
  const taken = new Set(SOURCES.map((s) => s.name));
  assert.equal(suggestName("bigquery", taken), "bigquery");
  assert.equal(suggestName("postgres", taken), "postgres-2");
  // Underscored ctypes become legal source names (the backend requires [a-z0-9_-]).
  assert.equal(suggestName("azure_blob", taken), "azure-blob");
});

test("Power BI sits on the warehouse shelf as a connectable source", () => {
  // It began as a marketing-shelf placeholder (powerbi_sap) that only ever
  // raised a setup message; the real connector is the `powerbi` ctype.
  const m = metaFor("powerbi");
  assert.equal(m.label, "Power BI");
  assert.equal(m.category, "warehouse");
  const tiles = buildTiles({
    types: [{ ctype: "powerbi", label: "Power BI", dialect: "sqlite", fields: [],
              ns: { schema: "dataset", database: "workspace" } }],
    sources: [{ name: "powerbi", dialect: "sqlite", configured: true }],
  });
  const tile = tiles.find((t) => t.id === "powerbi");
  assert.ok(tile.connectable);
  assert.equal(tile.instances.length, 1);          // the env source lands on the same tile
  assert.equal(tile.instances[0].kind, "env");
  assert.equal(metaFor("powerbi_sap").category, "other");    // the old id is just unknown now
});

test("metaFor never returns a hole", () => {
  for (const id of ["postgres", "m365", "github", "s3", "", null, "something_new"]) {
    const m = metaFor(id);
    assert.ok(m.label, `no label for ${id}`);
    assert.match(m.color, /^#[0-9a-f]{6}$/);
  }
  assert.equal(metaFor("github").label, "GitHub");
});

test("GitHub registration only enables canonical GitHub repository URLs and new names", () => {
  const form = { name: "sales-pipelines", url: "https://github.com/acme/pipelines", default_branch: "main" };
  assert.equal(githubRegistrationReady(form), true);
  assert.equal(githubRegistrationReady({ ...form, url: "https://github.com/acme/pipelines.git" }), true);
  assert.equal(githubRegistrationReady(form, REPOS), false); // POST would replace its ID
  for (const url of ["http://github.com/acme/pipelines", "https://github.com.evil.test/acme/pipelines",
                     "https://github.com/acme/pipelines?token=x", "https://github.com/acme/..",
                     "https://github.com/acme/.git", "https://github.com/acme/pipelines/subdir"]) {
    assert.equal(githubRegistrationReady({ ...form, url }), false, url);
  }
  assert.equal(githubRegistrationReady({ ...form, default_branch: "../main" }), false);
});

// ── Rendering ───────────────────────────────────────────────────────────

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
    // Recompute every render: these memos are pure, and the test wants the
    // value that matches the state it just set.
    useMemo(factory) { memos[memoCursor++] = factory(); return memos[memoCursor - 1]; },
  };
  return {
    render() {
      cursor = 0; memoCursor = 0; effects.length = 0;
      hooks = stateHooks;
      try { return renderToStaticMarkup(component(props)); } finally { hooks = null; }
    },
    start() { return effects.map((e) => e()).filter(Boolean); },
    set(index, value) { values[index] = value; },
    tree() {
      cursor = 0; memoCursor = 0;
      hooks = stateHooks;
      try { return component(props); } finally { hooks = null; }
    },
  };
}

function findNode(node, predicate) {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findNode(child, predicate);
      if (found) return found;
    }
  } else if (node && typeof node === "object") {
    if (predicate(node)) return node;
    return findNode(node.props?.children, predicate);
  }
  return null;
}

// State slot order in DataConnections: sources, types, conns, envs, m365, picked, …, repos.
const S = { sources: 0, types: 1, conns: 2, envs: 3, m365: 4, picked: 5, repos: 9 };

async function withApi(routes, work) {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage,
                      window: globalThis.window, confirm: globalThis.confirm };
  const requests = [], calls = [];
  globalThis.localStorage = { getItem: (k) => k === "studio_user" ? routes.__user : null };
  globalThis.window = { location: { search: "", pathname: "/", hash: "" },
                        history: { replaceState() {} } };
  globalThis.fetch = async (url, options = {}) => {
    requests.push(url);
    calls.push({ url, ...options });
    const key = Object.keys(routes).find((r) => url === `/api${r}`);
    return { status: 200, ok: true, json: async () => routes[key] ?? [] };
  };
  try {
    await work({ requests, calls, flush: async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); } });
  } finally { Object.assign(globalThis, originals); }
}

const ADMIN = JSON.stringify({ id: "u1", role: "admin" });
const ANALYST = JSON.stringify({ id: "u2", role: "analyst" });
const VIEWER = JSON.stringify({ id: "u3", role: "viewer" });

test("the grid renders a tile per source with an honest status", async () => {
  await withApi({ __user: ADMIN, "/catalog/sources": SOURCES, "/connections/types": TYPES,
                  "/connections": CONNS, "/m365/status": { configured: true, connected: false },
                  "/settings/repos": { repos: REPOS } },
    async ({ requests, flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render();
      panel.start();
      await flush();
      const html = panel.render();

      // /connections/env: how each built-in source is set (admin only).
      assert.deepEqual(requests.sort(), ["/api/catalog/sources", "/api/connections",
                                         "/api/connections/env", "/api/connections/types",
                                         "/api/m365/status", "/api/settings/repos"]);
      // Section headings, so the grid reads as a map rather than a flat wall.
      assert.match(html, /Databases and warehouses/);
      assert.match(html, /Cloud file storage/);
      // Snowflake has one live source (the user's) out of two instances.
      assert.match(html, /Snowflake<\/span><span class="conn-tile-status">1 connected/);
      // BigQuery is offered even though nothing is configured for it…
      assert.match(html, /PostgreSQL<\/span><span class="conn-tile-status">1 connected/);
      // …and S3, which this screen cannot set up, says so instead of vanishing.
      assert.match(html, /Amazon S3<\/span><span class="conn-tile-status">Not configured/);
      assert.match(html, /GitHub<\/span><span class="conn-tile-status">1 registered/);
      assert.match(html, /conn-tile-off/);
      // Tiles are icon-led: one inline mark each, no missing-image holes.
      assert.equal((html.match(/class="srcicon"/g) || []).length,
                   buildTiles({ types: TYPES, conns: CONNS, sources: SOURCES, repos: REPOS }).length);
    });
});

test("picking a source asks for that source's fields, and only that source's", async () => {
  await withApi({ __user: ADMIN, "/catalog/sources": SOURCES, "/connections/types": TYPES,
                  "/connections": CONNS, "/m365/status": { configured: true, connected: false },
                  "/settings/repos": { repos: REPOS } },
    async ({ flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render();
      panel.start();
      await flush();

      panel.set(S.picked, "postgres");
      const html = panel.render();
      assert.match(html, /Connection string \(DSN\)/);
      assert.match(html, /type="password"/);            // the DSN carries a password
      assert.match(html, /value="public"/);             // field defaults are prefilled
      assert.match(html, /value="postgres-2"/);         // and the name cannot collide
      // A type with namespaces offers to list them in the same press.
      assert.match(html, /Sign in and list schemas/);
      // Nothing from another connector leaks into this form.
      assert.doesNotMatch(html, /Account|Warehouse|HTTP path/);

      // A connector with no namespaces (Neo4j) skips the schema step entirely.
      panel.set(S.types, [...TYPES, { ctype: "neo4j", label: "Neo4j", dialect: "cypher",
        ns: {}, fields: [{ key: "uri", label: "URI", required: true, secret: false,
                           default: "", placeholder: "bolt://host:7687" }] }]);
      panel.set(S.picked, "neo4j");
      const neo = panel.render();
      assert.match(neo, /Test connection/);
      assert.doesNotMatch(neo, /list schemas/);

      // Microsoft 365 is an OAuth grant, not a credential form.
      panel.set(S.picked, "m365");
      const m365 = panel.render();
      assert.match(m365, /Connect Microsoft 365/);
      assert.doesNotMatch(m365, /Test connection/);
    });
});

test("GitHub has a distinct admin registration panel, not warehouse credentials", async () => {
  await withApi({ __user: ADMIN, "/catalog/sources": SOURCES, "/connections/types": TYPES,
                  "/connections": CONNS, "/m365/status": { configured: false },
                  "/settings/repos": { repos: REPOS } },
    async ({ flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render(); panel.start(); await flush();
      panel.set(S.picked, "github");
      const html = panel.render();
      assert.match(html, /Register repository/);
      assert.match(html, /GITHUB_TOKEN/);
      assert.match(html, /not warehouse data connections/);
      assert.match(html, /Studio analysts and administrators/);
      assert.doesNotMatch(html, /Test connection|Connection string \(DSN\)/);
    });
});

test("a non-admin sees the map of sources but never a credential form", async () => {
  await withApi({ __user: ANALYST, "/catalog/sources": SOURCES,
                  "/m365/status": { configured: true, connected: false },
                  "/repos": { repos: REPOS } },
    async ({ requests, flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render();
      panel.start();
      await flush();

      // The admin-only endpoints are not even called.
      assert.ok(!requests.includes("/api/connections/types"));
      assert.ok(!requests.includes("/api/connections"));
      assert.ok(!requests.includes("/api/settings/repos"));
      assert.ok(requests.includes("/api/repos"));

      const html = panel.render();
      assert.match(html, /PostgreSQL/);                 // still sees what exists
      assert.doesNotMatch(html, /conn-tile-status">Connect</);

      panel.set(S.picked, "postgres");
      const detail = panel.render();
      assert.doesNotMatch(detail, /Connection string|Test connection/);
      assert.match(detail, /An administrator connects sources/);

      panel.set(S.picked, "github");
      const github = panel.render();
      assert.match(github, /sales-pipelines/);
      assert.doesNotMatch(github, /Register repository|GITHUB_TOKEN/);
    });
});

test("viewers see the GitHub tile but no repository metadata or registration call", async () => {
  await withApi({ __user: VIEWER, "/catalog/sources": SOURCES,
                  "/m365/status": { configured: true, connected: false } },
    async ({ requests, flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render(); panel.start(); await flush();
      assert.ok(!requests.includes("/api/repos"));
      assert.ok(!requests.includes("/api/settings/repos"));
      const grid = panel.render();
      assert.match(grid, /GitHub<\/span><span class="conn-tile-status">Ask an admin/);
      panel.set(S.picked, "github");
      const detail = panel.render();
      assert.match(detail, /An administrator registers repositories/);
      assert.doesNotMatch(detail, /sales-pipelines|GITHUB_TOKEN|Register repository/);
    });
});

test("the GitHub panel registers through the admin API without sending a token", async () => {
  await withApi({ __user: ADMIN, "/settings/repos": { repos: [] } }, async ({ calls }) => {
    const notes = [];
    const panel = harness(GithubPanel, {
      repos: [], isAdmin: true, reload() {}, onNote: (note) => notes.push(note), onError() {},
    });
    for (const [placeholder, value] of [["sales-pipelines", "orders"],
                                        ["https://github.com/org/repo", "https://github.com/acme/orders"],
                                        ["What pipeline examples live here?", "Curated DAG examples"]]) {
      const input = findNode(panel.tree(), (n) => n.type === "input" && n.props.placeholder === placeholder);
      input.props.onChange({ target: { value } });
    }
    const button = findNode(panel.tree(), (n) => n.type === "button" &&
      n.props.children === "＋ Register repository");
    assert.equal(button.props.disabled, false);
    await button.props.onClick();
    const post = calls.find((call) => call.url === "/api/settings/repos" && call.method === "POST");
    assert.deepEqual(JSON.parse(post.body), {
      name: "orders", url: "https://github.com/acme/orders", description: "Curated DAG examples",
      default_branch: "main",
    });
    assert.doesNotMatch(JSON.stringify(post), /GITHUB_TOKEN|github_pat_|ghp_/);
    assert.ok(notes.some((note) => note.includes("registered")));
  });
});

test("the GitHub panel removes a registered name using its encoded API path", async () => {
  await withApi({ __user: ADMIN }, async ({ calls }) => {
    globalThis.confirm = () => true;
    const panel = harness(GithubPanel, {
      repos: [{ ...REPOS[0], name: "sales pipelines" }], isAdmin: true,
      reload() {}, onNote() {}, onError() {},
    });
    const button = findNode(panel.tree(), (n) => n.type === "button" &&
      n.props["aria-label"] === "Remove sales pipelines");
    await button.props.onClick();
    assert.ok(calls.some((call) => call.url === "/api/settings/repos/sales%20pipelines" &&
      call.method === "DELETE"));
  });
});

test("non-admin GitHub panel has no registration or removal mutation controls", async () => {
  await withApi({ __user: ANALYST }, async ({ calls }) => {
    const panel = harness(GithubPanel, {
      repos: REPOS, isAdmin: false, reload() {}, onNote() {}, onError() {},
    });
    const tree = panel.tree();
    assert.equal(findNode(tree, (n) => n.type === "input"), null);
    assert.equal(findNode(tree, (n) => n.type === "button"), null);
    assert.equal(calls.length, 0);
  });
});


// ── The schema step ─────────────────────────────────────────────────────
// A Studio source is pinned to ONE namespace: qualifiers() refuses any other,
// and allowed_tables() matches bare table names, so a source spanning two
// schemas would let a grant for `orders` admit the other schema's `orders`.
// Picking N schemas therefore has to produce N sources, not one wide one.

const SF_NS = { schema: "schema", database: "database" };
const FOUND = [{ database: "acme", schema: "public" },
                { database: "acme", schema: "analytics" },
                { database: "warehouse", schema: "raw" }];

test("picking three schemas plans three sources, each pinned to one namespace", () => {
  const plan = planConnections({
    cfg: { account: "acme-prod", password: "pw", schema: "public" },
    ns: SF_NS, name: "sales-sf", chosen: FOUND, takenNames: new Set(["demo"]),
  });

  assert.deepEqual(plan.map((p) => [p.name, p.config.database, p.config.schema]), [
    ["sales-sf-1", "acme", "public"],
    ["sales-sf-2", "acme", "analytics"],
    ["sales-sf-3", "warehouse", "raw"],
  ]);
  // Source names are catalog-visible even to roles without access. They must
  // not repeat namespace names that the API deliberately redacts.
  assert.ok(plan.every((p) => !p.name.includes(p.config.schema.toLowerCase())));
  // The credential rides along unchanged; only the namespace differs.
  assert.ok(plan.every((p) => p.config.account === "acme-prod" && p.config.password === "pw"));
  // No plan is a superset of another — that is the whole safety property.
  assert.equal(new Set(plan.map((p) => p.config.schema)).size, 3);
});

test("BigQuery's namespace lands on dataset/project, not schema/database", () => {
  const plan = planConnections({
    cfg: { project: "old-proj", dataset: "old", credentials_json: "{}" },
    ns: { schema: "dataset", database: "project" }, name: "bq",
    chosen: [{ database: "analytics-prod", schema: "Events" }],
  });
  assert.equal(plan.length, 1);
  assert.equal(plan[0].config.dataset, "Events");   // case preserved — BigQuery is case-sensitive
  assert.equal(plan[0].config.project, "analytics-prod");
  assert.equal(plan[0].config.schema, undefined);
});

test("Postgres keeps its database in the DSN and only rewrites the schema", () => {
  const plan = planConnections({
    cfg: { dsn: "postgresql://u:p@h:5432/acme", schema: "public" },
    ns: { schema: "schema", database: "" }, name: "pg",
    chosen: [{ database: "acme", schema: "analytics" }],
  });
  assert.equal(plan[0].config.schema, "analytics");
  assert.equal(plan[0].config.dsn, "postgresql://u:p@h:5432/acme");
  assert.equal(plan[0].config[""], undefined);      // no empty-string key written
});

test("planned names dodge existing sources and each other", () => {
  const plan = planConnections({
    cfg: {}, ns: SF_NS, name: "sf", chosen: FOUND,
    takenNames: new Set(["sf-1", "sf-2"]),
  });
  assert.deepEqual(plan.map((p) => p.name), ["sf-3", "sf-4", "sf-5"]);
  assert.equal(new Set(plan.map((p) => p.name)).size, 3);
  // Source names are [a-z0-9_-] and at most 31 characters, server-side.
  for (const p of plan) assert.match(p.name, /^[a-z0-9][a-z0-9_-]{1,30}$/);
});

test("one picked namespace dodges a collision on the typed base name", () => {
  const plan = planConnections({
    cfg: {}, ns: SF_NS, name: "warehouse",
    chosen: [{ database: "acme", schema: "analytics" }],
    takenNames: new Set(["warehouse", "warehouse-1"]),
  });
  assert.equal(plan[0].name, "warehouse-2");
  assert.equal(plan[0].config.database, "acme");
  assert.equal(plan[0].config.schema, "analytics");
});

test("long generated names retain their uniqueness suffix", () => {
  const base = "warehouse-connection-prefix";
  const first = planConnections({
    cfg: {}, ns: SF_NS, name: base,
    chosen: [{ database: "acme", schema: "schema-name-one" },
             { database: "acme", schema: "schema-name-two" }],
  });
  assert.ok(first[0].name.length <= 31);
  assert.ok(first[1].name.length <= 31);
  assert.notEqual(first[0].name, first[1].name);
  assert.match(first[0].name, /-1$/);
  assert.match(first[1].name, /-2$/);

  const again = planConnections({
    cfg: {}, ns: SF_NS, name: base,
    chosen: [{ database: "acme", schema: "schema-name-one" }],
    takenNames: new Set([base, first[0].name, first[1].name]),
  });
  assert.match(again[0].name, /-3$/);
  assert.ok(again[0].name.length <= 31);
});

test("schema spelling never enters a public generated source name", () => {
  const plan = planConnections({
    cfg: {}, ns: SF_NS, name: "dw",
    chosen: [{ database: "d", schema: "Raw.Events_2024" },
             { database: "d", schema: "a-very-long-schema-name-that-will-be-cut-off" }],
  });
  for (const p of plan) assert.match(p.name, /^[a-z0-9][a-z0-9_-]{1,30}$/);
  assert.deepEqual(plan.map((p) => p.name), ["dw-1", "dw-2"]);
  assert.ok(plan.every((p) => !p.name.includes("raw") && !p.name.includes("schema")));
});

test("partial batch retry drops successful namespaces and reserves their names", () => {
  const named = planConnections({
    cfg: {}, ns: SF_NS, name: "dw", chosen: FOUND,
  });
  assert.deepEqual(named.map((p) => p.name), ["dw-1", "dw-2", "dw-3"]);

  const initiallyPicked = FOUND.map(namespaceKey);
  const remaining = remainingPickedNamespaces(initiallyPicked, named, ["dw-1"]);
  assert.deepEqual(remaining, FOUND.slice(1).map(namespaceKey));

  const retry = planConnections({
    cfg: {}, ns: SF_NS, name: "dw", chosen: FOUND.slice(1),
    takenNames: new Set(["dw-1"]),
  });
  assert.deepEqual(retry.map((p) => p.name), ["dw-2", "dw-3"]);
  assert.ok(!retry.some((p) => p.config.schema === "public"));
});

test("picking nothing plans the single source the form already describes", () => {
  const plan = planConnections({
    cfg: { dsn: "postgresql://…", schema: "public" },
    ns: { schema: "schema", database: "" }, name: "sales-pg", chosen: [],
  });
  assert.deepEqual(plan, [{ ns: null, name: "sales-pg",
                            config: { dsn: "postgresql://…", schema: "public" } }]);
});

test("credentials can browse before required namespace fields are selected", () => {
  const fields = [
    { key: "account", required: true },
    { key: "user", required: true },
    { key: "password", required: true },
    { key: "database", required: true },
    { key: "schema", required: true },
  ];
  const cfg = { account: "acct", user: "analyst", password: "secret" };
  assert.deepEqual(connectionReadiness({ fields, ns: SF_NS, cfg }), {
    credentialsFilled: true,
    saveFilled: false,
  });
  assert.deepEqual(connectionReadiness({
    fields, ns: SF_NS, cfg,
    chosen: [{ database: "ACME", schema: "PUBLIC" }],
  }), { credentialsFilled: true, saveFilled: true });
  assert.equal(connectionReadiness({
    fields, ns: SF_NS, cfg: { account: "acct", user: "analyst" },
  }).credentialsFilled, false);
});

// ── The checklist ───────────────────────────────────────────────────────

const keyOf = (n) => `${n.database || ""}\u0000${n.schema}`;
const renderNs = (props) => renderToStaticMarkup(
  React.createElement(Namespaces, { picked: [], setPicked() {}, keyOf, ...props }));

test("the checklist groups schemas under their database", () => {
  const html = renderNs({ browse: { ok: true, namespaces: FOUND } });
  assert.match(html, /Schemas this account can see/);
  assert.match(html, /conn-ns-db">acme</);
  assert.match(html, /conn-ns-db">warehouse</);
  for (const schema of ["public", "analytics", "raw"]) {
    assert.match(html, new RegExp(`<span>${schema}</span>`));
  }
  // Selection is visible, and only on what was picked.
  const picked = renderNs({ browse: { ok: true, namespaces: FOUND },
                            picked: [keyOf(FOUND[1])] });
  assert.equal((picked.match(/conn-ns-on/g) || []).length, 1);
  assert.match(picked, /1 selected/);
});

test("a listing the warehouse refused does not block connecting", () => {
  const html = renderNs({ browse: { ok: false, error: "SHOW SCHEMAS denied", namespaces: [] } });
  assert.match(html, /SHOW SCHEMAS denied/);
  assert.match(html, /only affects the picker, not the connection/);
  assert.doesNotMatch(html, /type="checkbox"/);

  // And an account that simply sees nothing says so rather than looking broken.
  assert.match(renderNs({ browse: { ok: true, namespaces: [] } }),
               /No schemas visible to this account/);
});

test("a connection form never invites the browser to fill the Studio login", async () => {
  // A password input reads as a login to the browser, which then fills the
  // saved Studio email into the field before it and the Studio password into
  // the secret. autocomplete="off" is ignored there; "new-password" is not.
  await withApi({ __user: ADMIN, "/catalog/sources": SOURCES, "/connections/types": TYPES,
                  "/connections": CONNS, "/m365/status": { configured: true, connected: false },
                  "/settings/repos": { repos: REPOS } },
    async ({ flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render();
      panel.start();
      await flush();
      panel.set(S.picked, "postgres");
      const inputs = (panel.render().match(/<input[^>]*>/g) || [])
        .filter((tag) => /name="conn-postgres-/.test(tag));
      assert.equal(inputs.length, 2);                           // dsn + schema
      for (const tag of inputs) {
        assert.match(tag, /data-1p-ignore="true"/);
        assert.match(tag, /data-lpignore="true"/);
        assert.match(tag, /data-bwignore="true"/);
      }
      const [dsn, schema] = inputs;
      assert.match(dsn, /type="password"/);
      assert.match(dsn, /autocomplete="new-password"/i);   // HTML attribute names are case-insensitive
      assert.match(schema, /autocomplete="off"/i);
    });
});


// ── Built-in sources, managed from the screen ───────────────────────────

const ENVS = [{ name: "postgres", mode: "environment", hint: "" },
              { name: "snowflake", mode: "environment", hint: "" }];

test("a built-in source's row offers what an admin can do to it", () => {
  const tiles = byId(buildTiles({ types: TYPES, conns: CONNS, sources: SOURCES, envs: ENVS }));
  const pg = tiles.postgres.instances.find((i) => i.name === "postgres");
  assert.equal(pg.mode, "environment");
  assert.equal(instanceBadge(pg), "configured by environment");
  assert.deepEqual(envActions(pg), ["change", "disconnect"]);
  // Not configured by the environment: it can be set up here instead.
  const sf = tiles.snowflake.instances.find((i) => i.name === "snowflake");
  assert.equal(instanceBadge(sf), "not configured");
  assert.deepEqual(envActions(sf), ["setup"]);
  // A user connection keeps its own remove and gets none of these.
  const own = tiles.snowflake.instances.find((i) => i.name === "sales-sf");
  assert.equal(own.mode, undefined);
  assert.deepEqual(envActions(own), []);
  const replaced = { kind: "env", mode: "replaced", configured: true };
  assert.equal(instanceBadge(replaced), "set up here");
  assert.deepEqual(envActions(replaced), ["change", "disconnect", "revert"]);
  const off = { kind: "env", mode: "disconnected", configured: false };
  assert.equal(instanceBadge(off), "disconnected here");
  assert.deepEqual(envActions(off), ["reconnect", "revert"]);
  // Non-admins never load /connections/env: no mode, so no actions.
  const plain = byId(buildTiles({ types: TYPES, conns: CONNS, sources: SOURCES }));
  assert.deepEqual(envActions(plain.postgres.instances.find((i) => i.name === "postgres")), []);
});

test("the built-in row shows its actions on the connect screen", async () => {
  await withApi({ __user: ADMIN, "/catalog/sources": SOURCES, "/connections/types": TYPES,
                  "/connections": CONNS, "/connections/env": ENVS,
                  "/m365/status": { configured: true, connected: false },
                  "/settings/repos": { repos: REPOS } },
    async ({ flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render();
      panel.start();
      await flush();
      panel.set(S.picked, "postgres");
      const html = panel.render();
      assert.match(html, /configured by environment/);
      assert.match(html, /✎ Change/);
      assert.match(html, /⏸ Disconnect/);
    });
});

function connectForm(done = []) {
  const tile = byId(buildTiles({ types: TYPES, sources: SOURCES, envs: ENVS })).postgres;
  return harness(ConnectForm, { tile, defaultName: "postgres-2", takenNames: new Set(["postgres"]),
                                onDone: (m) => done.push(m), onError() {}, onRemoved() {} });
}

test("Change re-points the built-in source under its own name", async () => {
  await withApi({ __user: ADMIN }, async ({ calls }) => {
    const done = [];
    const panel = connectForm(done);
    const rows = findNode(panel.tree(), (n) => n.type?.name === "Instances");
    rows.props.onManage({ name: "postgres", kind: "env", mode: "environment", configured: true },
                        "change");
    let tree = panel.tree();
    // It stays `postgres`: there is no name to choose.
    assert.equal(findNode(tree, (n) => n.type === "input" && n.props.value === "postgres-2"), null);
    findNode(tree, (n) => n.type === "input" && n.props.name === "conn-postgres-dsn")
      .props.onChange({ target: { value: "postgresql://u:p@db.example/sales" } });
    tree = panel.tree();
    const save = findNode(tree, (n) => n.type === "button" && n.props.className === "primary");
    assert.equal(save.props.disabled, false);
    await save.props.onClick();
    const put = calls.find((c) => c.url === "/api/connections/env/postgres" && c.method === "PUT");
    assert.deepEqual(JSON.parse(put.body),
                     { config: { dsn: "postgresql://u:p@db.example/sales", schema: "public" } });
    assert.equal(calls.some((c) => c.url === "/api/connections" && c.method === "POST"), false);
    assert.ok(done[0].includes("“postgres” now reads"));
  });
});

test("Disconnect and Back to environment act on the built-in source at once", async () => {
  await withApi({ __user: ADMIN }, async ({ calls }) => {
    globalThis.confirm = () => true;
    const panel = connectForm();
    const rows = findNode(panel.tree(), (n) => n.type?.name === "Instances");
    const pg = { name: "postgres", kind: "env", mode: "replaced", configured: true };
    await rows.props.onManage(pg, "disconnect");
    await rows.props.onManage(pg, "revert");
    assert.ok(calls.some((c) => c.url === "/api/connections/env/postgres/disconnect" &&
                                c.method === "POST"));
    assert.ok(calls.some((c) => c.url === "/api/connections/env/postgres" &&
                                c.method === "DELETE"));
  });
});

// ── The Databricks form: wrong values are marked where they were typed ──

const DBX = { ctype: "databricks", label: "Databricks SQL", dialect: "databricks",
  ns: { schema: "schema", database: "catalog" },
  fields: [
    { key: "server_hostname", label: "Server hostname", required: true, secret: false, default: "", placeholder: "" },
    { key: "http_path", label: "HTTP path", required: true, secret: false, default: "", placeholder: "" },
    { key: "access_token", label: "Access token", required: true, secret: true, default: "", placeholder: "" },
    { key: "catalog", label: "Catalog", required: false, secret: false, default: "", placeholder: "" },
    { key: "schema", label: "Schema", required: false, secret: false, default: "default", placeholder: "" },
  ] };
const HOST_SAYS = "This looks like a token or secret, not a host name.";
const PATH_SAYS = "That is a browser address, not the warehouse's HTTP path.";

function dbxForm(errors) {
  const tile = byId(buildTiles({
    types: [DBX], sources: [{ name: "databricks", dialect: "databricks", configured: true, allowed: true }],
    envs: [{ name: "databricks", mode: "environment", hint: "" }] })).databricks;
  const panel = harness(ConnectForm, { tile, defaultName: "databricks-2",
    takenNames: new Set(["databricks"]), onDone() {}, onError: (m) => errors.push(m), onRemoved() {} });
  const type = (key, value) => findNode(panel.tree(),
    (n) => n.type === "input" && n.props.name === `conn-databricks-${key}`)
    .props.onChange({ target: { value } });
  type("server_hostname", "dose0123456789abcdef0123");
  type("http_path", "https://dbc-1.cloud.databricks.com/settings/workspace");
  type("access_token", "dapi-token");
  return { panel, type };
}

test("a refused sign-in marks each wrong field and says nothing was sent", async () => {
  const refused = { ok: false, namespaces: [], error: `${HOST_SAYS} ${PATH_SAYS}`,
                    fields: { server_hostname: HOST_SAYS, http_path: PATH_SAYS } };
  await withApi({ __user: ADMIN, "/connections/browse": refused }, async () => {
    const errors = [];
    const { panel, type } = dbxForm(errors);
    const signIn = findNode(panel.tree(), (n) => n.type === "button"
      && String(n.props.children).includes("Sign in and list schemas"));
    await signIn.props.onClick();
    let html = panel.render();
    assert.equal((html.match(/class="conn-input-bad"/g) || []).length, 2);
    assert.equal((html.match(/aria-invalid="true"/g) || []).length, 2);
    assert.ok(html.includes(HOST_SAYS) && html.includes("That is a browser address"));
    assert.match(html, /Fix the marked fields — nothing\s+was sent to Databricks SQL yet/);
    // Not the picker's "couldn't list schemas — only affects the picker" note.
    assert.doesNotMatch(html, /list schemas \(/);
    // Editing a field clears the marks: the next sign-in decides again.
    type("server_hostname", "dbc-1.cloud.databricks.com");
    html = panel.render();
    assert.doesNotMatch(html, /conn-input-bad|Fix the marked/);
  });
});

test("a save refused for its fields marks them instead of a banner", async () => {
  await withApi({ __user: ADMIN }, async () => {
    const puts = [];
    globalThis.fetch = async (url, options = {}) => {
      if (url === "/api/connections/env/databricks" && options.method === "PUT") {
        puts.push(JSON.parse(options.body));
        return { status: 400, ok: false, json: async () => ({ detail: {
          error: `connection test failed: ${PATH_SAYS}`, fields: { http_path: PATH_SAYS } } }) };
      }
      return { status: 200, ok: true, json: async () => [] };
    };
    const errors = [];
    const { panel, type } = dbxForm(errors);
    findNode(panel.tree(), (n) => n.type?.name === "Instances").props.onManage(
      { name: "databricks", kind: "env", mode: "environment", configured: true }, "change");
    // "Change" starts from a blank form; fill it with one wrong field.
    type("server_hostname", "dbc-1.cloud.databricks.com");
    type("http_path", "https://dbc-1.cloud.databricks.com/settings/workspace");
    type("access_token", "dapi-token");
    const save = findNode(panel.tree(), (n) => n.type === "button" && n.props.className === "primary");
    await save.props.onClick();
    assert.equal(puts.length, 1);
    const html = panel.render();
    assert.equal((html.match(/class="conn-input-bad"/g) || []).length, 1);
    assert.ok(html.includes("That is a browser address"));
    assert.match(html, /Fix the marked field — nothing/);
    assert.ok(errors.every((e) => e === ""), errors.join(" | "));
  });
});

test("a save refused after signing in says so, and marks the schema", async () => {
  await withApi({ __user: ADMIN }, async () => {
    const said = "Signed in, but this schema has no tables the credential can see.";
    globalThis.fetch = async (url, options = {}) => {
      if (url === "/api/connections/env/databricks" && options.method === "PUT") {
        return { status: 400, ok: false, json: async () => ({ detail: {
          error: `connection test failed: ${said}`, fields: { schema: said }, stage: "signed_in" } }) };
      }
      return { status: 200, ok: true, json: async () => [] };
    };
    const errors = [];
    const { panel, type } = dbxForm(errors);
    findNode(panel.tree(), (n) => n.type?.name === "Instances").props.onManage(
      { name: "databricks", kind: "env", mode: "environment", configured: true }, "change");
    type("server_hostname", "dbc-1.cloud.databricks.com");
    type("http_path", "/sql/1.0/warehouses/abc");
    type("access_token", "dapi-token");
    await findNode(panel.tree(), (n) => n.type === "button" && n.props.className === "primary")
      .props.onClick();
    const html = panel.render();
    assert.match(html, /name="conn-databricks-schema" class="conn-input-bad"/);
    assert.ok(html.includes(said));
    assert.match(html, /Signed in, but not saved — fix the marked field\./);
    assert.doesNotMatch(html, /nothing\s+was sent/);
  });
});

test("a sign-in the workspace refused marks the field and says it was refused", async () => {
  const said = "There is no SQL warehouse at that HTTP path — copy it again from the warehouse's Connection details tab.";
  await withApi({ __user: ADMIN, "/connections/browse": {
    ok: false, error: said, fields: { http_path: said }, stage: "sign_in", namespaces: [] } }, async () => {
    const { panel } = dbxForm([]);
    await findNode(panel.tree(), (n) => n.type === "button"
      && String(n.props.children).includes("Sign in and list schemas")).props.onClick();
    const html = panel.render();
    assert.match(html, /name="conn-databricks-http_path" class="conn-input-bad"/);
    assert.match(html, /Databricks SQL refused the sign-in — fix the marked field\./);
    assert.doesNotMatch(html, /nothing\s+was sent|Signed in, but not saved/);
  });
});

// ── The picker shows which schemas hold tables; saving skips empty ones ─

const LISTED = { ok: true, truncated: false, namespaces: [
  { database: "main", schema: "default", tables: 0 },
  { database: "main", schema: "sales", tables: 3 },
  { database: "main", schema: "ops", tables: 1 },
] };

test("the picker shows table counts and will not pick an empty schema", () => {
  const html = harness(Namespaces, { browse: LISTED, picked: [], setPicked() {}, keyOf: namespaceKey }).render();
  assert.match(html, /<span>sales<\/span><span class="meta">3 tables<\/span>/);
  assert.match(html, /<span>ops<\/span><span class="meta">1 table<\/span>/);
  assert.match(html, /conn-ns-empty[^>]*>.*?disabled="".*?<span>default<\/span><span class="meta">empty<\/span>/s);
  assert.doesNotMatch(html, /no tables in any of them/);
  const allEmpty = { ...LISTED, namespaces: LISTED.namespaces.map((n) => ({ ...n, tables: 0 })) };
  assert.match(harness(Namespaces, { browse: allEmpty, picked: [], setPicked() {}, keyOf: namespaceKey }).render(),
               /no tables in any of them\. It most likely\s+needs USE CATALOG, USE SCHEMA and SELECT/);
  // Counts unknown (a Hive metastore): nothing is greyed out.
  const unknown = { ...LISTED, namespaces: LISTED.namespaces.map(({ tables, ...n }) => n) };
  assert.doesNotMatch(harness(Namespaces, { browse: unknown, picked: [], setPicked() {}, keyOf: namespaceKey }).render(),
                      /conn-ns-empty|disabled=""/);
});

async function saveTwo(emptyNames) {
  const posts = [], done = [], errors = [];
  await withApi({ __user: ADMIN, "/connections/browse": LISTED }, async () => {
    const original = globalThis.fetch;
    globalThis.fetch = async (url, options = {}) => {
      if (url === "/api/connections" && options.method === "POST") {
        const body = JSON.parse(options.body);
        posts.push(body.config.schema);
        if (emptyNames.includes(body.config.schema)) {
          const said = "Signed in, but this schema has no tables the credential can see.";
          return { status: 400, ok: false, json: async () => ({ detail: {
            error: `connection test failed: ${said}`, fields: { schema: said }, stage: "signed_in" } }) };
        }
        return { status: 201, ok: true, json: async () => ({ name: body.name }) };
      }
      return original(url, options);
    };
    const tile = byId(buildTiles({ types: [DBX], sources: [], envs: [] })).databricks;
    const panel = harness(ConnectForm, { tile, defaultName: "databricks-2", takenNames: new Set(),
      onDone: (m) => done.push(m), onError: (m) => errors.push(m), onRemoved() {} });
    const type = (key, value) => findNode(panel.tree(),
      (n) => n.type === "input" && n.props.name === `conn-databricks-${key}`).props.onChange({ target: { value } });
    type("server_hostname", "dbc-1.cloud.databricks.com");
    type("http_path", "/sql/1.0/warehouses/abc");
    type("access_token", "dapi-token");
    await findNode(panel.tree(), (n) => n.type === "button"
      && String(n.props.children).includes("Sign in and list schemas")).props.onClick();
    // Tick sales and ops (the picker would not let "default" be ticked).
    const ns = findNode(panel.tree(), (n) => n.type?.name === "Namespaces");
    ns.props.setPicked([namespaceKey(LISTED.namespaces[1]), namespaceKey(LISTED.namespaces[2])]);
    await findNode(panel.tree(), (n) => n.type === "button" && n.props.className === "primary").props.onClick();
  });
  return { posts, done, errors };
}

test("saving several schemas skips an empty one and connects the rest", async () => {
  const { posts, done, errors } = await saveTwo(["ops"]);
  assert.deepEqual(posts, ["sales", "ops"]);
  assert.equal(done.length, 1);
  assert.match(done[0], /“databricks-2-1” is now a source/);
  assert.match(done[0], /Skipped main\.ops — no tables this token can see\./);
  assert.ok(errors.every((e) => e === ""), errors.join(" | "));
});

test("when every picked schema is empty, it says which and what to grant", async () => {
  const { done, errors } = await saveTwo(["sales", "ops"]);
  assert.equal(done.length, 0);
  assert.match(errors.at(-1), /None of the picked schemas has tables this token can see \(main\.sales, main\.ops\)/);
  assert.match(errors.at(-1), /USE CATALOG, USE SCHEMA and SELECT/);
});
