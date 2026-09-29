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
    contents: `export {planningContextFields, chatPipelineFields, usePlanningCatalog,
      PlanningContextPicker, PlanningSources} from "./src/components/Pipelines.jsx";`,
    resolveDir: frontend,
    loader: "jsx",
  },
  bundle: true,
  write: false,
  format: "cjs",
  platform: "node",
  jsx: "automatic",
  external: ["react", "react/jsx-runtime"],
});
const compiled = new Module(path.join(frontend, "planning-sources-test.cjs"));
compiled.filename = path.join(frontend, "planning-sources-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => id === "react" ? {
  ...React,
  useState: (...args) => hooks ? hooks.useState(...args) : React.useState(...args),
  useEffect: (...args) => hooks ? hooks.useEffect(...args) : React.useEffect(...args),
} : require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { planningContextFields, chatPipelineFields, usePlanningCatalog,
  PlanningContextPicker, PlanningSources } = compiled.exports;

const html = (component, props) =>
  renderToStaticMarkup(React.createElement(component, props));

test("chat includes planning-source IDs only for build turns", () => {
  assert.deepEqual(chatPipelineFields(false, false, "repo-1", ["page-1"]), {});
  assert.deepEqual(chatPipelineFields(true, false, " repo-1 ",
    ["page-1", "page-1", "", 2]), {
    pipeline_action: "build",
    pipeline_mode: "read_only_sql",
    repository_id: "repo-1",
    confluence_page_ids: ["page-1"],
  });
  assert.deepEqual(chatPipelineFields(false, true, "", []), {
    pipeline_action: "build",
    pipeline_mode: "airflow_dag",
    repository_id: null,
    confluence_page_ids: [],
  });
  assert.deepEqual(planningContextFields("", null), {
    repository_id: null,
    confluence_page_ids: [],
  });
  assert.deepEqual(planningContextFields("repo-1", ["a", "b", "c", "d"])
    .confluence_page_ids, ["a", "b", "c"]);
});

test("the picker exposes repositories and multi-page Confluence context honestly", () => {
  const rendered = html(PlanningContextPicker, {
    repositories: [{ id: "repo-1", name: "Data jobs", default_branch: "release" }],
    confluencePages: [
      { id: "page-1", title: "Revenue definitions", space_key: "FIN", version: 4 },
      { id: "page-2", title: "Runbook", space_key: "OPS", version: 2 },
    ],
    repositoryId: "repo-1",
    confluencePageIds: ["page-1", "page-2"],
    onRepositoryChange() {},
    onConfluenceChange() {},
  });
  assert.match(rendered, /Repository planning context/);
  assert.match(rendered, /Data jobs · release/);
  assert.match(rendered, /Confluence page planning context/);
  assert.match(rendered, /Revenue definitions · FIN · v4/);
  assert.match(rendered, /2 pages selected/);
  assert.match(rendered, /read-only planning context, not code execution/);
  assert.match(rendered, /review, approval, and run path/);
});

test("unconfigured Confluence is a non-blocking empty selector", () => {
  const rendered = html(PlanningContextPicker, {
    repositoryStatus: "ready",
    confluenceStatus: "unconfigured",
    repositories: [],
    confluencePages: [],
  });
  assert.match(rendered, /Confluence is not configured/);
  assert.match(rendered, /repository-only planning can continue/);
  assert.match(rendered, /aria-label="Confluence page planning context"[^>]*disabled/);
});

test("viewers do not receive planning-source selectors", () => {
  const rendered = html(PlanningContextPicker, { planningAllowed: false });
  assert.match(rendered, /available to analysts and administrators/);
  assert.doesNotMatch(rendered, /<select/);
});

test("draft provenance names exact selected sources and rejects unsafe links", () => {
  const rendered = html(PlanningSources, { sources: {
    github_repository: {
      repo: { id: "repo-1", name: "Warehouse", url: "javascript:alert(1)" },
      ref: "main@abc123", files: [{ path: "daily.sql" }],
    },
    confluence_pages: [{
      id: "page-1", title: "Metric contract", space_key: "FIN", version: 7,
      url: "https://wiki.example/pages/1", sha256: "a".repeat(64),
    }],
  }});
  assert.match(rendered, /Pipeline planning provenance/);
  assert.match(rendered, /Warehouse · main@abc123 · 1 referenced file/);
  assert.match(rendered, /Metric contract · FIN · v7/);
  assert.match(rendered, /none was executed as code/);
  assert.doesNotMatch(rendered, /javascript:/);
  assert.match(rendered, /href="https:\/\/wiki\.example\/pages\/1"/);
});

test("catalog requests the two exact GET endpoints and degrades Confluence", async () => {
  const values = [];
  const effects = [];
  let cursor = 0;
  hooks = {
    useState(initial) {
      const index = cursor++;
      if (index >= values.length) values.push(typeof initial === "function" ? initial() : initial);
      return [values[index], (next) => {
        values[index] = typeof next === "function" ? next(values[index]) : next;
      }];
    },
    useEffect(effect) { effects.push(effect); },
  };
  const calls = [];
  globalThis.localStorage = {
    getItem(key) {
      return key === "studio_user" ? JSON.stringify({ id: "u1", role: "analyst" }) : null;
    },
  };
  globalThis.fetch = async (url) => {
    calls.push(url);
    if (url === "/api/repos") return {
      ok: true, status: 200,
      async json() { return { repos: [{ id: "repo-1", name: "Jobs" }] }; },
    };
    return {
      ok: false, status: 503,
      async json() { return { detail: "Confluence is not configured" }; },
    };
  };
  usePlanningCatalog();
  const cleanup = effects[0]();
  await new Promise((resolve) => setTimeout(resolve, 0));
  cursor = 0;
  const catalog = usePlanningCatalog();
  cleanup();
  hooks = null;

  assert.deepEqual(calls, ["/api/repos", "/api/confluence/pages"]);
  assert.deepEqual(catalog.repositories, [{ id: "repo-1", name: "Jobs" }]);
  assert.equal(catalog.repositoryStatus, "ready");
  assert.deepEqual(catalog.confluencePages, []);
  assert.equal(catalog.confluenceStatus, "unconfigured");
});
