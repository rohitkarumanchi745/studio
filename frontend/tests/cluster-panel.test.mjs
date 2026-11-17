// Spark clusters on the Integrations screen: AKS, GKE and Local Kubernetes
// tiles on their own shelf, and the panel an administrator connects one with —
// paste a kubeconfig to fill the form, test, connect, remove. Drawn by React
// into a real (jsdom) document; the server is a fetch double that records
// what was sent.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: {
    contents: `export {default as ClusterPanel, CLUSTER_TILES, STEPS, ORDER_TEXT, clusterReady, withKubeconfig}
                 from "./src/components/ClusterPanel.jsx";
               export {buildTiles, signInGroups} from "./src/components/DataConnections.jsx";
               export {metaFor, CATEGORIES, SourceIcon} from "./src/components/sourceCatalog.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "react-dom"],
});
const compiled = new Module(path.join(frontend, "cluster-panel-test.cjs"));
compiled.filename = path.join(frontend, "cluster-panel-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { ClusterPanel, CLUSTER_TILES, STEPS, ORDER_TEXT, clusterReady, withKubeconfig, buildTiles,
        signInGroups, metaFor, CATEGORIES, SourceIcon } = compiled.exports;
const React = require("react");
const { createRoot } = require("react-dom/client");
const { act } = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const store = new Map([["studio_token", "tok"]]);
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem() {}, removeItem() {} };

/** A fetch double: answers by "METHOD /path", records every call. */
function server(answers) {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    const key = `${options.method || "GET"} ${url}`;
    calls.push({ key, body: options.body ? JSON.parse(options.body) : undefined });
    const [status, body] = answers[key] || [404, { detail: "no route" }];
    return { status, ok: status < 400, json: async () => body };
  };
  return calls;
}

async function mount(props) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const seen = { notes: [], errors: [], reloads: 0 };
  await act(async () => {
    root.render(React.createElement(ClusterPanel, {
      kind: "local", label: "Local Kubernetes", state: undefined, defaultKind: null, isAdmin: true,
      reload: () => { seen.reloads += 1; }, onNote: (n) => seen.notes.push(n),
      onError: (e) => { if (e) seen.errors.push(e); },
      ...props,
    }));
  });
  return { host, root, seen };
}

// Typing into a React-controlled field: set the value the way a user does.
function type(el, value) {
  const proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
  el.dispatchEvent(new window.Event("input", { bubbles: true }));
}
const button = (host, text) => [...host.querySelectorAll("button")]
  .find((b) => b.textContent.includes(text));
const input = (host, key) => host.querySelector(`input[name="cluster-local-${key}"]`);
const settle = async () => { for (let i = 0; i < 6; i++) await act(async () => {}); };

// ── Tiles ───────────────────────────────────────────────────────────────

test("three cluster tiles on a Compute for Spark shelf, each with its own mark", () => {
  assert.deepEqual(CLUSTER_TILES, { aks: "aks", gke: "gke", k8s_local: "local" });
  assert.ok(CATEGORIES.some((c) => c.key === "compute" && c.label === "Compute for Spark"));
  for (const id of Object.keys(CLUSTER_TILES)) {
    assert.equal(metaFor(id).category, "compute");
    const svg = renderToStaticMarkup(React.createElement(SourceIcon, { id }));
    assert.match(svg, /<rect x="15" y="7"/);               // the pods mark, not a lettermark
    assert.doesNotMatch(svg, /<text/);
  }
  assert.equal(metaFor("aks").label, "Azure Kubernetes Service");
  assert.equal(metaFor("gke").label, "Google Kubernetes Engine");
  assert.equal(metaFor("k8s_local").label, "Local Kubernetes");
});

test("a connected cluster is its tile's one instance, and never a data source", () => {
  const tiles = buildTiles({ clusters: [
    { kind: "local", label: "Local Kubernetes", connected: true, server_host: "127.0.0.1",
      namespace: "spark-jobs" },
    { kind: "aks", label: "Azure Kubernetes Service", connected: false },
  ] });
  const byId = Object.fromEntries(tiles.map((t) => [t.id, t]));
  assert.deepEqual(byId.k8s_local.instances, [{ name: "Local Kubernetes", configured: true,
                                                kind: "cluster", hint: "127.0.0.1 · spark-jobs" }]);
  assert.deepEqual(byId.aks.instances, []);
  assert.deepEqual(byId.gke.instances, []);
  // Connected sources (what Studio scans for data) leaves clusters out.
  assert.deepEqual(signInGroups(tiles), []);
});

// ── The panel ───────────────────────────────────────────────────────────

const KIND_KUBECONFIG = {
  fields: { server: "https://127.0.0.1:52817", ca_cert: "-----BEGIN CERTIFICATE-----\nCA\n",
            insecure: false, token: "", client_cert: "-----BEGIN CERTIFICATE-----\nC\n",
            client_key: "-----BEGIN PRIVATE KEY-----\nK\n", namespace: "spark-jobs" },
  notes: ["The server is on this machine’s loopback: if Studio runs in Docker, use host.docker.internal"],
};

test("a pasted kubeconfig fills the form; Test and Connect send it; the form clears", async () => {
  const calls = server({
    "POST /api/clusters/kubeconfig": [200, KIND_KUBECONFIG],
    "POST /api/clusters/test?kind=local": [200, { ok: true, said: "Signed in — the Spark operator is ready in “spark-jobs”." }],
    "PUT /api/clusters/local": [200, { kind: "local", namespace: "spark-jobs", server_host: "127.0.0.1" }],
  });
  const m = await mount({});
  assert.ok(button(m.host, "Test connection").disabled);       // nothing to sign in with yet
  const paste = m.host.querySelector("textarea[aria-label=Kubeconfig]");
  await act(async () => type(paste, "apiVersion: v1\nclusters: []"));
  await act(async () => button(m.host, "Fill in from kubeconfig").click());
  await settle();
  assert.deepEqual(calls[0], { key: "POST /api/clusters/kubeconfig",
                               body: { text: "apiVersion: v1\nclusters: []" } });
  assert.equal(paste.value, "");                                // read once, not kept
  assert.equal(input(m.host, "server").value, "https://127.0.0.1:52817");
  assert.equal(input(m.host, "namespace").value, "spark-jobs");
  assert.ok(m.host.querySelector("details.cluster-cert").open);  // it signs in with a certificate
  assert.match(m.host.textContent, /use host\.docker\.internal/);

  await act(async () => button(m.host, "Test connection").click());
  await settle();
  const sent = { server: "https://127.0.0.1:52817", token: "", ca_cert: "-----BEGIN CERTIFICATE-----\nCA\n",
                 client_cert: "-----BEGIN CERTIFICATE-----\nC\n", client_key: "-----BEGIN PRIVATE KEY-----\nK\n",
                 namespace: "spark-jobs", service_account: "", image: "", insecure: false };
  assert.deepEqual(calls[1], { key: "POST /api/clusters/test?kind=local", body: { config: sent } });
  assert.match(m.host.textContent, /✓ Signed in — the Spark operator is ready/);

  await act(async () => type(input(m.host, "image"), "registry.local/spark:3.5"));
  await act(async () => button(m.host, "Connect").click());
  await settle();
  assert.deepEqual(calls[2], { key: "PUT /api/clusters/local",
                               body: { config: { ...sent, image: "registry.local/spark:3.5" } } });
  assert.deepEqual(m.seen.notes, ["Local Kubernetes connected — Spark jobs run in “spark-jobs” on 127.0.0.1."]);
  assert.equal(m.seen.reloads, 1);
  assert.equal(input(m.host, "server").value, "");              // credentials leave the page
  await act(async () => m.root.unmount());
});

test("a refused save marks the field the server named", async () => {
  server({ "PUT /api/clusters/local": [400, { detail: {
    error: "The cluster refused this sign-in — check the token (or client certificate).",
    fields: { token: "The cluster refused this sign-in — check the token (or client certificate)." },
    stage: "sign_in" } }] });
  const m = await mount({});
  await act(async () => type(input(m.host, "server"), "https://k8s.example"));
  await act(async () => type(input(m.host, "token"), "wrong"));
  assert.equal(input(m.host, "token").type, "password");
  await act(async () => button(m.host, "Connect").click());
  await settle();
  assert.equal(input(m.host, "token").getAttribute("aria-invalid"), "true");
  assert.equal(input(m.host, "server").getAttribute("aria-invalid"), null);
  assert.deepEqual(m.seen.errors, ["The cluster refused this sign-in — check the token (or client certificate)."]);
  assert.equal(m.seen.reloads, 0);
  // Typing in the field clears its mark.
  await act(async () => type(input(m.host, "token"), "right"));
  assert.equal(input(m.host, "token").getAttribute("aria-invalid"), null);
  await act(async () => m.root.unmount());
});

test("a connected cluster shows where it runs and can be removed after a confirm", async () => {
  const calls = server({ "DELETE /api/clusters/aks": [200, { kind: "aks", connected: false }] });
  const asked = [];
  globalThis.confirm = (q) => { asked.push(q); return asked.length > 1; };
  const m = await mount({ kind: "aks", label: "Azure Kubernetes Service", defaultKind: "aks",
                          state: { kind: "aks", connected: true, server_host: "studio-abc.hcp.eastus.azmk8s.io",
                                   namespace: "spark-jobs", auth: "token", verified_tls: true, image: "" } });
  const text = m.host.textContent;
  assert.match(text, /studio-abc\.hcp\.eastus\.azmk8s\.io · namespace spark-jobs · signs in with a token/);
  assert.doesNotMatch(text, /TLS not verified/);
  assert.match(text, /Spark jobs that name no cluster run here\./);
  assert.ok(text.includes(ORDER_TEXT));
  assert.equal(m.host.querySelector("details.cluster-steps").open, false);   // set up already
  assert.ok(button(m.host, "Replace"));
  await act(async () => button(m.host, "Remove").click());                   // declined
  assert.equal(calls.length, 0);
  await act(async () => button(m.host, "Remove").click());
  await settle();
  assert.deepEqual(calls.map((c) => c.key), ["DELETE /api/clusters/aks"]);
  assert.deepEqual(m.seen.notes, ["Azure Kubernetes Service removed."]);
  assert.equal(m.seen.reloads, 1);
  await act(async () => m.root.unmount());
});

test("each kind has its own setup steps; local warns that Studio must reach it", async () => {
  server({});
  for (const [kind, first] of [["aks", "az aks get-credentials"], ["gke", "gcloud container clusters get-credentials"],
                               ["local", "deploy/spark-k8s/local-up.sh"]]) {
    const m = await mount({ kind });
    const steps = [...m.host.querySelectorAll(".cluster-steps code")].map((c) => c.textContent);
    assert.ok(steps[0].startsWith(first), kind);
    assert.deepEqual(steps, STEPS[kind].map(([, command]) => command));
    assert.ok(m.host.querySelector("details.cluster-steps").open);
    const warns = /not with a Studio hosted in the cloud/.test(m.host.textContent);
    assert.equal(warns, kind === "local", kind);
    await act(async () => m.root.unmount());
  }
  assert.ok(STEPS.aks.some(([, c]) => c === "kubectl apply -f deploy/spark-k8s/studio-access.yaml"));
  assert.ok(STEPS.gke.some(([, c]) => c.includes("spark.jobNamespaces={spark-jobs}")));
});

test("a non-admin is told who connects clusters, and sees no form", async () => {
  const calls = server({});
  const m = await mount({ isAdmin: false, kind: "gke" });
  assert.match(m.host.textContent, /An administrator connects Spark clusters/);
  assert.equal(m.host.querySelectorAll("input, textarea, button").length, 0);
  assert.equal(calls.length, 0);
  await act(async () => m.root.unmount());
});

test("ready only with a server and a way to sign in; a kubeconfig never clears a typed field", () => {
  const empty = { server: "", token: "", client_cert: "", client_key: "" };
  assert.equal(clusterReady(empty), false);
  assert.equal(clusterReady({ ...empty, server: "https://x" }), false);
  assert.equal(clusterReady({ ...empty, server: "https://x", token: " t " }), true);
  assert.equal(clusterReady({ ...empty, server: "https://x", client_cert: "c" }), false);
  assert.equal(clusterReady({ ...empty, server: "https://x", client_cert: "c", client_key: "k" }), true);
  const typed = { server: "", token: "typed", ca_cert: "", client_cert: "", client_key: "",
                  namespace: "", service_account: "", image: "mine:1", insecure: false };
  assert.deepEqual(withKubeconfig(typed, { server: "https://y", token: "", insecure: true, unknown: "x" }),
                   { ...typed, server: "https://y", insecure: true });
});
