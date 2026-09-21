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
    contents: `export {default as DataConnections, buildTiles, suggestName, planConnections, Namespaces}
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
const { DataConnections, buildTiles, suggestName, planConnections, Namespaces,
        metaFor } = compiled.exports;

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

  // Sources the catalog serves but this bundle has never heard of still render.
  const unknown = byId(buildTiles({ sources: [{ name: "duck_lake", configured: true }] }));
  assert.equal(unknown.duck_lake.meta.label, "Duck Lake");
});

test("search matches the display name, not just the backend identifier", () => {
  const hit = buildTiles({ types: TYPES, conns: CONNS, sources: SOURCES, q: "postgre" });
  assert.deepEqual(hit.map((t) => t.id), ["postgres"]);
  // "Microsoft 365" is findable even though its id is "m365".
  assert.deepEqual(buildTiles({ types: TYPES, sources: [], q: "microsoft" }).map((t) => t.id), ["m365"]);
  assert.deepEqual(buildTiles({ types: TYPES, sources: SOURCES, q: "zzz" }), []);
});

test("a new connection never collides with an existing source name", () => {
  const taken = new Set(SOURCES.map((s) => s.name));
  assert.equal(suggestName("bigquery", taken), "bigquery");
  assert.equal(suggestName("postgres", taken), "postgres-2");
  // Underscored ctypes become legal source names (the backend requires [a-z0-9_-]).
  assert.equal(suggestName("azure_blob", taken), "azure-blob");
});

test("metaFor never returns a hole", () => {
  for (const id of ["postgres", "m365", "s3", "", null, "something_new"]) {
    const m = metaFor(id);
    assert.ok(m.label, `no label for ${id}`);
    assert.match(m.color, /^#[0-9a-f]{6}$/);
  }
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
  };
}

// State slot order in DataConnections: sources, types, conns, m365, picked, …
const S = { sources: 0, types: 1, conns: 2, m365: 3, picked: 4 };

async function withApi(routes, work) {
  const originals = { fetch: globalThis.fetch, localStorage: globalThis.localStorage,
                      window: globalThis.window };
  const requests = [];
  globalThis.localStorage = { getItem: (k) => k === "studio_user" ? routes.__user : null };
  globalThis.window = { location: { search: "", pathname: "/", hash: "" },
                        history: { replaceState() {} } };
  globalThis.fetch = async (url) => {
    requests.push(url);
    const key = Object.keys(routes).find((r) => url === `/api${r}`);
    return { status: 200, ok: true, json: async () => routes[key] ?? [] };
  };
  try {
    await work({ requests, flush: async () => { for (let n = 0; n < 8; n++) await Promise.resolve(); } });
  } finally { Object.assign(globalThis, originals); }
}

const ADMIN = JSON.stringify({ id: "u1", role: "admin" });
const ANALYST = JSON.stringify({ id: "u2", role: "analyst" });

test("the grid renders a tile per source with an honest status", async () => {
  await withApi({ __user: ADMIN, "/catalog/sources": SOURCES, "/connections/types": TYPES,
                  "/connections": CONNS, "/m365/status": { configured: true, connected: false } },
    async ({ requests, flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render();
      panel.start();
      await flush();
      const html = panel.render();

      assert.deepEqual(requests.sort(), ["/api/catalog/sources", "/api/connections",
                                         "/api/connections/types", "/api/m365/status"]);
      // Section headings, so the grid reads as a map rather than a flat wall.
      assert.match(html, /Databases and warehouses/);
      assert.match(html, /Cloud file storage/);
      // Snowflake has one live source (the user's) out of two instances.
      assert.match(html, /Snowflake<\/span><span class="conn-tile-status">1 connected/);
      // BigQuery is offered even though nothing is configured for it…
      assert.match(html, /PostgreSQL<\/span><span class="conn-tile-status">1 connected/);
      // …and S3, which this screen cannot set up, says so instead of vanishing.
      assert.match(html, /Amazon S3<\/span><span class="conn-tile-status">Not configured/);
      assert.match(html, /conn-tile-off/);
      // Tiles are icon-led: one inline mark each, no missing-image holes.
      assert.equal((html.match(/class="srcicon"/g) || []).length,
                   buildTiles({ types: TYPES, conns: CONNS, sources: SOURCES }).length);
    });
});

test("picking a source asks for that source's fields, and only that source's", async () => {
  await withApi({ __user: ADMIN, "/catalog/sources": SOURCES, "/connections/types": TYPES,
                  "/connections": CONNS, "/m365/status": { configured: true, connected: false } },
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

test("a non-admin sees the map of sources but never a credential form", async () => {
  await withApi({ __user: ANALYST, "/catalog/sources": SOURCES,
                  "/m365/status": { configured: true, connected: false } },
    async ({ requests, flush }) => {
      const panel = harness(DataConnections, { onClose() {} });
      panel.render();
      panel.start();
      await flush();

      // The admin-only endpoints are not even called.
      assert.ok(!requests.includes("/api/connections/types"));
      assert.ok(!requests.includes("/api/connections"));

      const html = panel.render();
      assert.match(html, /PostgreSQL/);                 // still sees what exists
      assert.doesNotMatch(html, /conn-tile-status">Connect</);

      panel.set(S.picked, "postgres");
      const detail = panel.render();
      assert.doesNotMatch(detail, /Connection string|Test connection/);
      assert.match(detail, /An administrator connects sources/);
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
    ["sales-sf-public", "acme", "public"],
    ["sales-sf-analytics", "acme", "analytics"],
    ["sales-sf-raw", "warehouse", "raw"],
  ]);
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
    takenNames: new Set(["sf-public", "sf-public-2"]),
  });
  assert.deepEqual(plan.map((p) => p.name), ["sf-public-3", "sf-analytics", "sf-raw"]);
  assert.equal(new Set(plan.map((p) => p.name)).size, 3);
  // Source names are [a-z0-9_-] and at most 31 characters, server-side.
  for (const p of plan) assert.match(p.name, /^[a-z0-9][a-z0-9_-]{1,30}$/);
});

test("a schema with punctuation still yields a legal source name", () => {
  const plan = planConnections({
    cfg: {}, ns: SF_NS, name: "dw",
    chosen: [{ database: "d", schema: "Raw.Events_2024" },
             { database: "d", schema: "a-very-long-schema-name-that-will-be-cut-off" }],
  });
  for (const p of plan) assert.match(p.name, /^[a-z0-9][a-z0-9_-]{1,30}$/);
  assert.equal(plan[0].name, "dw-raw-events-2024");
});

test("picking nothing plans the single source the form already describes", () => {
  const plan = planConnections({
    cfg: { dsn: "postgresql://…", schema: "public" },
    ns: { schema: "schema", database: "" }, name: "sales-pg", chosen: [],
  });
  assert.deepEqual(plan, [{ ns: null, name: "sales-pg",
                            config: { dsn: "postgresql://…", schema: "public" } }]);
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
