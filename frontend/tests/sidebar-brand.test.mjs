// The sidebar names the app: "Studio", with the ◆ badge its answers carry,
// at the top — above "+ New chat" and the Chats list.
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
    contents: `export {default as Sidebar} from "./src/components/Sidebar.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "react-dom"],
  loader: { ".json": "json" },
});
const compiled = new Module(path.join(frontend, "sidebar-brand-test.cjs"));
compiled.filename = path.join(frontend, "sidebar-brand-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { Sidebar } = compiled.exports;

function render(user) {
  const originals = { localStorage: globalThis.localStorage, window: globalThis.window };
  globalThis.localStorage = { getItem: (k) => (k === "studio_user" ? JSON.stringify(user) : null),
                              setItem() {}, removeItem() {} };
  globalThis.window = { location: { search: "", pathname: "/", hash: "" }, history: { replaceState() {} },
                        addEventListener() {}, removeEventListener() {} };
  try {
    return renderToStaticMarkup(React.createElement(Sidebar, {
      conversations: [{ id: "c1", title: "total sales by region", owned: true }],
      activeId: null, onSelect() {}, onNew() {}, onCollapse() {},
    }));
  } finally {
    Object.assign(globalThis, originals);
  }
}

for (const role of ["admin", "analyst", "viewer"]) {
  test(`the sidebar names the app above the chats (${role})`, () => {
    const html = render({ id: "u1", role, name: "Rohit" });
    assert.match(html, /<div class="sidebar-brand"><span class="assistant-avatar sidebar-brand-mark" aria-hidden="true">◆<\/span><span class="sidebar-brand-name">Studio<\/span><\/div>/);
    const brand = html.indexOf('class="sidebar-brand"');
    const newChat = html.indexOf("+ New chat");
    const chats = html.indexOf('class="conv-label">Chats<');
    assert.ok(brand >= 0 && newChat > brand && chats > newChat, "Studio, then + New chat, then Chats");
    // Named once: the badge is decoration, the word is the name.
    assert.equal((html.match(/sidebar-brand-name/g) || []).length, 1);
  });
}
