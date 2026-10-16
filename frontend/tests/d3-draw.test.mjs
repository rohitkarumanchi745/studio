// D3 actually drawing: every type D3View supports, rendered with the real d3
// into a jsdom document, so a broken scale or a mis-wired layout fails here
// rather than as a blank tile. jsdom has no layout engine — sizes are passed
// in, as the component does from its container — so this proves the SVG is
// built correctly, not how it looks.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: {
    contents: `export {draw, D3_TYPES} from "./src/components/renderers/D3View.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  external: ["react", "react/jsx-runtime", "d3"],
});
const compiled = new Module(path.join(frontend, "d3-draw-test.cjs"));
compiled.filename = path.join(frontend, "d3-draw-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => require(id);
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const { draw, D3_TYPES } = compiled.exports;
const d3 = await import("d3");

const SIZE = { w: 640, h: 320 };

function render(type, columns, rows, spec = {}) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  const out = draw(d3, el, columns, rows, { type, title: type, ...spec }, SIZE);
  if (typeof out === "function") out();                 // stop an animated layout
  return { el, out, count: (sel) => el.querySelectorAll(sel).length };
}

const REGIONS = { columns: ["region", "revenue", "cost"],
                  rows: [["West", 120, 80], ["East", 90, 70], ["North", 60, 20], ["South", 30, 25]] };
const MONTHS = { columns: ["month", "revenue", "cost"],
                 rows: [["2024-01-01", 10, 6], ["2024-02-01", 14, 8], ["2024-03-01", 9, 7]] };

test("bars: one per category, grouped per series, stacked as segments", () => {
  const one = render("bar", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue"] });
  assert.equal(one.out, null);
  assert.equal(one.count("svg"), 1);
  assert.equal(one.count("g > rect[rx]"), 4);
  assert.equal(render("bar", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue", "cost"] })
    .count("rect[rx='3']"), 8);
  assert.equal(render("stacked_bar", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue", "cost"] })
    .count("rect[rx='3']"), 8);
  assert.equal(render("hbar", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue"] })
    .count("rect[rx='3']"), 4);
});

test("lines and areas draw one path per series over a time axis", () => {
  const line = render("line", MONTHS.columns, MONTHS.rows, { x: "month", y: ["revenue", "cost"] });
  const paths = [...line.el.querySelectorAll("path")].filter((p) => p.getAttribute("stroke-width") === "2");
  assert.equal(paths.length, 2);
  assert.ok(paths.every((p) => /^M[\d.]/.test(p.getAttribute("d"))));
  const stacked = render("stacked_area", MONTHS.columns, MONTHS.rows, { x: "month", y: ["revenue", "cost"] });
  assert.equal([...stacked.el.querySelectorAll("path")].filter((p) => p.getAttribute("opacity") === "0.35").length, 2);
});

test("pie, donut, scatter, bubble and heatmap draw their marks", () => {
  assert.equal(render("pie", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue"] })
    .count("path"), 4);
  assert.equal(render("donut", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue"] })
    .count("path"), 4);
  assert.equal(render("scatter", ["cost", "revenue"], [[80, 120], [70, 90], [20, 60]], { x: "cost", y: ["revenue"] })
    .count("circle"), 3);
  const bubble = render("bubble", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue", "cost"] });
  const radii = [...bubble.el.querySelectorAll("circle")].map((c) => +c.getAttribute("r"));
  assert.equal(radii.length, 4);
  assert.ok(Math.max(...radii) > Math.min(...radii));   // sized by the second y
  const heat = render("heatmap", ["day", "hour", "n"], [["Mon", "9", 3], ["Mon", "10", 5], ["Tue", "9", 1]],
                      { x: "day", y: ["hour", "n"] });
  assert.equal(heat.count("rect[rx='2']"), 3);
});

test("network: a node per entity, a line per link, a draggable layout to stop", () => {
  const el = document.createElement("div");
  const stop = draw(d3, el, ["from", "to", "calls"],
                    [["api", "db", 5], ["web", "api", 4], ["web", "cdn", 1]],
                    { type: "network", x: "from", y: ["to", "calls"] }, SIZE);
  assert.equal(typeof stop, "function");               // the force layout is live until stopped
  assert.equal(el.querySelectorAll("circle").length, 4);
  assert.equal(el.querySelectorAll("line").length, 3);
  stop();
});

test("beeswarm, calendar and circle packing draw every datum", () => {
  const swarm = render("beeswarm", ["region", "amount"],
                       Array.from({ length: 40 }, (_, i) => [i % 2 ? "West" : "East", i]),
                       { x: "region", y: ["amount"] });
  assert.equal(swarm.count("circle"), 40);
  const cal = render("calendar", ["day", "orders"], [["2024-01-01", 3], ["2024-06-15", 9]],
                     { x: "day", y: ["orders"] });
  assert.equal(cal.count("rect"), 366);                  // every day of leap-year 2024
  const filled = [...cal.el.querySelectorAll("rect")].filter((r) => r.getAttribute("fill") !== "#eceae7");
  assert.equal(filled.length, 2);
  const pack = render("circle_pack", ["product", "revenue", "category"],
                      [["Lamp", 30, "Home"], ["Desk", 50, "Office"], ["Rug", 20, "Home"]],
                      { x: "product", y: ["revenue", "category"] });
  assert.equal(pack.count("circle"), 5);                 // 2 groups + 3 leaves
});

test("data that does not fit is refused, never half-drawn", () => {
  // Only a self-link: no edge to draw. (A numeric "target" is turned away
  // earlier, by ChartView's fit rule, before a network is ever offered.)
  assert.equal(render("network", ["a", "b"], [["x", "x"]], { x: "a", y: ["b"] }).out, false);
  assert.equal(render("calendar", ["region", "v"], [["West", 1]], { x: "region", y: ["v"] }).out, false);
  assert.equal(render("bar", ["region"], [["West"]], { x: "nope", y: [] }).out, false);
});

test("every type D3 claims draws without throwing", () => {
  const fixtures = {
    network: [["from", "to"], [["a", "b"], ["b", "c"]], { x: "from", y: ["to"] }],
    calendar: [["day", "v"], [["2024-01-01", 1]], { x: "day", y: ["v"] }],
    beeswarm: [["g", "v"], [["a", 1], ["b", 2]], { x: "g", y: ["v"] }],
    circle_pack: [["g", "v"], [["a", 1], ["b", 2]], { x: "g", y: ["v"] }],
    heatmap: [["a", "b", "v"], [["x", "y", 1]], { x: "a", y: ["b", "v"] }],
    ...MORE,
  };
  for (const type of D3_TYPES) {
    const [columns, rows, spec] = fixtures[type] || [REGIONS.columns, REGIONS.rows,
                                                     { x: "region", y: ["revenue", "cost"] }];
    const { out, count } = render(type, columns, rows, spec);
    assert.notEqual(out, false, type);
    assert.equal(count(type === "table" ? "table" : "svg"), 1, type);
  }
});

// The rest of the catalog, drawn with D3 for the D3 & artifacts window.
const FLOWS = [["source", "target", "visits"],
               [["Ads", "Landing", 60], ["Search", "Landing", 40], ["Landing", "Signup", 30],
                ["Landing", "Bounce", 70], ["Signup", "Paid", 12]],
               { x: "source", y: ["target", "visits"] }];
const SPREAD = [["team", "hours"],
                [...Array.from({ length: 9 }, (_, i) => ["A", 10 + i]), ["A", 60],
                 ...Array.from({ length: 6 }, (_, i) => ["B", 20 + i * 2])],
                { x: "team", y: ["hours"] }];
const BRIDGE = [["step", "delta"], [["Start", 100], ["Price", 20], ["Churn", -35], ["Upsell", 15]],
                { x: "step", y: ["delta"] }];
const MORE = {
  sankey: FLOWS, boxplot: SPREAD, waterfall: BRIDGE,
  kpi: [["revenue"], [[1234.5]], { x: "revenue", y: ["revenue"] }],
  gauge: [["score"], [[72]], { x: "score", y: ["score"] }],
  histogram: [["amount"], Array.from({ length: 50 }, (_, i) => [i * 3]), { x: "amount", y: ["amount"] }],
};

test("the rest of the catalog draws every datum it is given", () => {
  const tree = render("treemap", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue"] });
  assert.equal(tree.count("rect"), 4);
  assert.equal(render("funnel", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue"] })
    .count("path"), 4);
  const radar = render("radar", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue", "cost"] });
  assert.equal(radar.count("circle"), 8);                // 2 series × 4 axes
  const combo = render("combo", REGIONS.columns, REGIONS.rows, { x: "region", y: ["revenue", "cost"] });
  assert.equal(combo.count("rect[rx='3']"), 4);          // bars for revenue
  assert.equal(combo.count("circle"), 4);                // a line (with points) for cost
  const hist = render("histogram", ...MORE.histogram);
  assert.equal(hist.count("rect[rx='2']"), 7);           // ≈√50 bins
  const box = render("boxplot", ...SPREAD);
  assert.equal(box.count("rect[rx='3']"), 2);            // one box per team
  assert.equal(box.count("circle"), 1);                  // the 60-hour outlier
  const fall = render("waterfall", ...BRIDGE);
  const fills = [...fall.el.querySelectorAll("rect[rx='3']")].map((r) => r.getAttribute("fill"));
  assert.deepEqual(fills, ["#008300", "#008300", "#e66767", "#008300", "#3987e5"]); // + total
  const flow = render("sankey", ...FLOWS);
  assert.equal(flow.count("rect"), 6);                   // six nodes
  assert.equal(flow.count("path"), 5);                   // five flows
  assert.match(render("kpi", ...MORE.kpi).el.textContent, /1,234\.5/);
  assert.equal(render("gauge", ...MORE.gauge).count("path"), 2);
  const table = render("table", REGIONS.columns, REGIONS.rows);
  assert.equal(table.count("tbody tr"), 4);
  assert.equal(table.count("th"), 3);
});

test("a flow that loops back, or a box with no groups, is refused", () => {
  assert.equal(render("sankey", ["a", "b", "v"], [["x", "y", 1], ["y", "x", 1]],
                      { x: "a", y: ["b", "v"] }).out, false);
  assert.equal(render("boxplot", ["v"], [[1]], { x: "nope", y: ["v"] }).out, false);
  assert.equal(render("radar", ["a", "v"], [["x", 1], ["y", 2]], { x: "a", y: ["v"] }).out, false);
});

test("table cells are text, never markup", () => {
  const { el } = render("table", ["name"], [["<img src=x onerror=alert(1)>"]]);
  assert.equal(el.querySelectorAll("img").length, 0);
  assert.match(el.querySelector("td").textContent, /<img/);
});

test("a tooltip shows data as text, never as markup", () => {
  const { el } = render("bar", ["region", "revenue"], [["<img src=x onerror=alert(1)>", 5]],
                        { x: "region", y: ["revenue"] });
  const bar = el.querySelector("rect[rx='3']");
  bar.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true, clientX: 5, clientY: 5 }));
  const tip = el.querySelector(".d3-tip");
  assert.match(tip.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(el.querySelectorAll("img").length, 0);
});
