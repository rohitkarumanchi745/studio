// Maps drawn with D3: routes between places (transit, shipments, trips),
// places as dots, and countries / US states shaded by a value.
//
// Pinned here: which columns are places (coordinate pairs by name, countries
// and US states by name or code — offline, no geocoding), what each map draws
// from a result, when a map is offered and why not, and — with the real d3,
// the real base maps and a jsdom document — that each one actually draws.
import assert from "node:assert/strict";
import { test } from "node:test";
import { createRequire, Module } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { build } from "esbuild";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = { getItem: () => null, setItem() {} };

const frontend = fileURLToPath(new URL("../", import.meta.url));
const require = createRequire(import.meta.url);
const bundle = await build({
  stdin: {
    contents: `export * from "./src/components/renderers/geo.js";
      export {draw, mainCentroid, D3_ONLY, D3_TYPES} from "./src/components/renderers/D3View.jsx";
      export {computeFit, LABELS} from "./src/components/ChartView.jsx";`,
    resolveDir: frontend, loader: "jsx",
  },
  bundle: true, write: false, format: "cjs", platform: "node", jsx: "automatic",
  loader: { ".json": "json" },
  plugins: [{
    name: "url-stub",
    setup(b) {
      b.onResolve({ filter: /\?url$/ }, (a) => ({ path: a.path, namespace: "url-stub" }));
      b.onLoad({ filter: /.*/, namespace: "url-stub" },
               () => ({ contents: 'export default "/assets/d3.min.js";', loader: "js" }));
    },
  }],
  external: ["react", "react/jsx-runtime", "echarts", "d3", "vega-embed", "plotly.js-dist-min"],
});
const compiled = new Module(path.join(frontend, "maps-test.cjs"));
compiled.filename = path.join(frontend, "maps-test.cjs");
compiled.paths = Module._nodeModulePaths(frontend);
compiled.require = (id) => (id === "echarts" ? { init() { return {}; } } : require(id));
compiled._compile(bundle.outputFiles[0].text, compiled.filename);
const {
  coordinatePairs, placeColumn, placeKey, routeMapData, pointMapData, choroplethData, indexOf,
  allInUS, featureKey, loadGeo, US_STATES, MAP_TYPES, MAP_LIMITS,
  draw, mainCentroid, D3_ONLY, D3_TYPES, computeFit, LABELS,
} = compiled.exports;
const d3 = await import("d3");
const geo = await loadGeo();

// Transit: bus trips between stops, with their coordinates.
const TRIPS = {
  columns: ["from_stop", "to_stop", "from_lat", "from_lon", "to_lat", "to_lon", "riders"],
  rows: [
    ["Union Station", "Navy Pier", 41.8786, -87.6403, 41.8917, -87.6086, 120],
    ["Union Station", "Navy Pier", 41.8786, -87.6403, 41.8917, -87.6086, 30],     // same route
    ["Navy Pier", "Midway", 41.8917, -87.6086, 41.7868, -87.7522, 45],
    ["Midway", "O'Hare", 41.7868, -87.7522, 41.9742, -87.9073, 80],
  ],
};
const FLIGHTS = {
  columns: ["origin", "destination", "passengers"],
  rows: [["United States", "United Kingdom", 900], ["US", "France", 500], ["USA", "Japan", 300],
         ["Germany", "Côte d’Ivoire", 50], ["Atlantis", "France", 5]],
};

// ── Which columns are places ────────────────────────────────────────────

test("coordinate columns pair by name, and say where a trip starts and ends", () => {
  const roles = (columns, row) => coordinatePairs(columns, [row]).map((p) => [p.key, p.role]);
  assert.deepEqual(roles(TRIPS.columns, TRIPS.rows[0]), [["from", "origin"], ["to", "dest"]]);
  assert.deepEqual(roles(["pickup_latitude", "pickup_longitude", "dropoff_latitude", "dropoff_longitude"],
                         [40.7, -74, 40.6, -73.9]), [["pickup", "origin"], ["dropoff", "dest"]]);
  assert.deepEqual(roles(["stop_name", "stop_lat", "stop_lon"], ["A", 41.9, -87.6]), [["stop", "point"]]);
  assert.deepEqual(roles(["Latitude", "Longitude"], [1, 2]), [["", "point"]]);
  assert.deepEqual(roles(["StartLat", "StartLng", "EndLat", "EndLng"], [1, 2, 3, 4]),
                   [["start", "origin"], ["end", "dest"]]);
  // Not degrees: not a place. Unpaired: not a place.
  assert.deepEqual(roles(["lat", "lon"], [400, 20]), []);
  assert.deepEqual(roles(["lat", "population"], [40, 20]), []);
});

test("countries and US states are found by name or code, offline", () => {
  assert.equal(placeKey("Côte d’Ivoire"), "cote divoire");
  const col = (name, values) => placeColumn([name], values.map((v) => [v]), 0);
  const world = col("country", ["US", "USA", "United States", "840", "GB", "Deutschland", "Japan"]);
  assert.equal(world.kind, "world");
  assert.deepEqual([...world.ids.values()].slice(0, 5), ["840", "840", "840", "840", "826"]);
  assert.deepEqual(world.unmatched, ["Deutschland"]);
  const states = col("state", ["CA", "Texas", "new york", "06", "DC"]);
  assert.equal(states.kind, "us");
  assert.deepEqual([...states.ids.values()], ["06", "48", "36", "06", "11"]);
  // "CA" is California and Canada: the column's name decides, then the count.
  assert.equal(col("ship_state", ["CA", "DE", "IN"]).kind, "us");
  assert.equal(col("ship_country", ["CA", "DE", "IN"]).kind, "world");
  assert.equal(col("code", ["CA", "TX", "NY"]).kind, "us");
  // A name two countries share goes to the one ISO gives it to.
  const congo = col("country", ["Congo", "Democratic Republic of the Congo", "DRC", "COD"]);
  assert.deepEqual([congo.ids.get("Congo"), congo.ids.get("Democratic Republic of the Congo"),
                    congo.ids.get("COD")], ["178", "180", "180"]);
  // Any other shared name is left unmatched rather than guessed.
  const shared = indexOf([["1", "Springfield", "Alpha"], ["2", "Springfield", "Beta"]]);
  assert.deepEqual([shared.get("springfield"), shared.get("alpha"), shared.get("beta")], [undefined, "1", "2"]);
  // Numbers are not places, however many look like FIPS codes.
  assert.equal(placeColumn(["id"], [[1], [2], [6]], 0), null);
  // Too few matches: not a place column.
  assert.equal(col("region", ["West", "East", "North", "France"]), null);
});

test("every US state and every country on the base map resolves to its own shape", () => {
  const states = JSON.parse(readFileSync(path.join(frontend, "node_modules/us-atlas/states-10m.json")));
  const byFips = new Map(states.objects.states.geometries.map((g) => [g.id, g.properties.name]));
  for (const [fips, code, name] of US_STATES) {
    assert.equal(byFips.get(fips), name, `${code} ${fips}`);
    assert.equal(placeColumn(["state"], [[code]], 0).ids.get(code), fips, code);
  }
  const world = JSON.parse(readFileSync(path.join(frontend, "node_modules/world-atlas/countries-110m.json")));
  const misses = world.objects.countries.geometries
    .filter((g) => placeColumn(["country"], [[g.properties.name]], 0)?.ids.get(g.properties.name)
                   !== featureKey(g))
    .map((g) => g.properties.name);
  assert.deepEqual(misses, []);
});

// ── What each map draws ─────────────────────────────────────────────────

test("routes add up per origin → destination, and name their ends", () => {
  const data = routeMapData(TRIPS.columns, TRIPS.rows, { x: "from_stop", y: ["to_stop", "riders"] });
  assert.equal(data.kind, "us");                                   // Chicago: the US map
  assert.equal(data.valueName, "riders");
  assert.deepEqual(data.routes.map((r) => [r.fromLabel, r.toLabel, r.value]),
                   [["Union Station", "Navy Pier", 150], ["Midway", "O'Hare", 80], ["Navy Pier", "Midway", 45]]);
  // No value column: count the trips.
  const counted = routeMapData(TRIPS.columns.slice(0, 6), TRIPS.rows.map((r) => r.slice(0, 6)),
                               { x: "from_stop", y: [] });
  assert.equal(counted.valueName, "trips");
  assert.equal(counted.routes[0].value, 2);
  // Across the world by country name; an unknown place is reported, not drawn.
  const flights = routeMapData(FLIGHTS.columns, FLIGHTS.rows, { x: "origin", y: ["destination", "passengers"] });
  assert.equal(flights.kind, "world");
  assert.deepEqual(flights.routes.map((r) => [r.from.id, r.to.id, r.value]),
                   [["840", "826", 900], ["840", "250", 500], ["840", "392", 300], ["276", "384", 50]]);
  assert.deepEqual(flights.unmatched, ["Atlantis"]);
  // A spec that names only one end (the choropleth's): the ends are found
  // by their names, and the value is any number the spec asks for.
  const named = routeMapData(["origin_country", "destination_country", "passengers"],
                             [["US", "GB", 9], ["DE", "US", 4]], { x: "origin_country", y: ["passengers"] });
  assert.deepEqual(named.routes.map((r) => [r.from.id, r.to.id, r.value]), [["840", "826", 9], ["276", "840", 4]]);
  assert.equal(named.valueName, "passengers");
  // More than two place columns: only their names say which end is which.
  const three = routeMapData(["home_country", "origin_country", "destination_country", "n"],
                             [["France", "US", "GB", 3]], { x: "home_country", y: ["n"] });
  assert.deepEqual(three.routes.map((r) => [r.from.id, r.to.id]), [["840", "826"]]);
  // A trip that ends where it began is not a route.
  const loop = routeMapData(TRIPS.columns, [...TRIPS.rows, ["Midway", "Midway", 41.7868, -87.7522,
                                                            41.7868, -87.7522, 999]],
                            { x: "from_stop", y: ["riders"] });
  assert.ok(loop.routes.every((r) => r.fromLabel !== r.toLabel));
  // Coordinates: each end is named by the column named for it.
  const stops = routeMapData(TRIPS.columns, TRIPS.rows, { x: "from_stop", y: ["riders"] });
  assert.deepEqual([stops.routes[0].fromLabel, stops.routes[0].toLabel], ["Union Station", "Navy Pier"]);
  // Nothing to route: no map.
  assert.equal(routeMapData(["region", "revenue"], [["West", 1]], { x: "region", y: ["revenue"] }), null);
  // Busiest routes first, capped.
  const many = Array.from({ length: MAP_LIMITS.routes + 50 }, (_, i) => [0, i / 100, 10, i / 100, i]);
  const capped = routeMapData(["o_lat", "o_lon", "d_lat", "d_lon", "n"], many, { y: ["n"] });
  assert.equal(capped.routes.length, MAP_LIMITS.routes);
  assert.equal(capped.total, MAP_LIMITS.routes + 50);
  assert.equal(capped.routes[0].value, MAP_LIMITS.routes + 49);
});

test("places as dots: coordinates, or a country's centre", () => {
  const stops = pointMapData(["stop_name", "stop_lat", "stop_lon", "boardings"],
                             [["Clark/Lake", 41.8857, -87.6309, 9000], ["Howard", 42.019, -87.6728, 4000],
                              ["bad", null, null, 1]], { x: "stop_name", y: ["boardings"] });
  assert.equal(stops.kind, "us");
  assert.deepEqual(stops.points.map((p) => [p.label, p.value]), [["Clark/Lake", 9000], ["Howard", 4000]]);
  const world = pointMapData(["city", "lat", "lon"], [["Paris", 48.85, 2.35], ["Chicago", 41.88, -87.63]],
                             { x: "city", y: [] });
  assert.equal(world.kind, "world");
  const byCountry = pointMapData(["country", "sales"], [["France", 3], ["Japan", 5]], { x: "country", y: ["sales"] });
  assert.deepEqual(byCountry.points.map((p) => [p.id, p.value]), [["250", 3], ["392", 5]]);
  assert.equal(allInUS([[-87.6, 41.9], [-157.8, 21.3], [-149.9, 61.2]]), true);    // Chicago, Honolulu, Anchorage
  assert.equal(allInUS([[-87.6, 41.9], [2.35, 48.85]]), false);
});

test("a choropleth shades countries or states by a value — or by how many rows each has", () => {
  const sales = choroplethData(["state", "sales"], [["CA", 10], ["California", 5], ["TX", 7], ["Narnia", 1]],
                               { x: "state", y: ["sales"] });
  assert.equal(sales.kind, "us");
  assert.deepEqual(sales.regions.map((r) => [r.id, r.value]), [["06", 15], ["48", 7]]);
  assert.deepEqual(sales.unmatched, ["Narnia"]);
  const counted = choroplethData(["shipment_id", "dest_country"], [[1, "France"], [2, "France"], [3, "Spain"]],
                                 { x: "shipment_id", y: [] });              // x is not a place: the first that is
  assert.equal(counted.placeName, "dest_country");
  assert.equal(counted.valueName, "rows");
  assert.deepEqual(counted.regions.map((r) => [r.label, r.value]), [["France", 2], ["Spain", 1]]);
});

// ── When a map is offered ───────────────────────────────────────────────

test("maps are D3 types with their own shelf, offered only when the data has places", () => {
  for (const t of MAP_TYPES) assert.ok(D3_ONLY.has(t) && D3_TYPES.has(t) && LABELS[t], t);
  const fit = computeFit(TRIPS.columns, TRIPS.rows, { type: "bar", x: "from_stop", y: ["riders"] });
  assert.equal(fit.route_map.ok, true);
  assert.equal(fit.point_map.ok, true);
  assert.equal(fit.choropleth.ok, false);
  const none = computeFit(["region", "revenue"], [["West", 1]], { type: "bar", x: "region", y: ["revenue"] });
  assert.match(none.route_map.reason, /origin and destination latitude\/longitude/);
  assert.match(none.point_map.reason, /latitude\/longitude columns, or a column of countries/);
  assert.match(none.choropleth.reason, /countries or US states/);
});

// ── Drawing ─────────────────────────────────────────────────────────────

const SIZE = { w: 720, h: 400 };
function render(type, columns, rows, spec = {}) {
  const el = document.createElement("div");
  document.body.appendChild(el);
  const out = draw(d3, el, columns, rows, { type, title: type, ...spec }, SIZE, geo);
  return { el, out, all: (sel) => [...el.querySelectorAll(sel)] };
}

test("a route map draws the base map, a great-circle route each, and its stops", () => {
  const m = render("route_map", TRIPS.columns, TRIPS.rows, { x: "from_stop", y: ["to_stop", "riders"] });
  assert.equal(m.out, null);
  assert.equal(m.all("path.map-region").length, 56);                 // the US: states, DC, territories
  const routes = m.all("path.map-route");
  assert.equal(routes.length, 3);
  assert.ok(routes.every((p) => /^M[\d.-]+,[\d.-]+/.test(p.getAttribute("d"))));
  assert.ok(routes.every((p) => p.classList.contains("map-flow")));   // moving dashes
  // Widest is the busiest.
  const widths = routes.map((p) => Number(p.getAttribute("stroke-width")));
  assert.equal(Math.max(...widths), widths.at(-1));
  assert.equal(m.all("circle.map-stop").length, 6);                   // 3 origins + 3 destinations
  assert.match(m.el.querySelector(".map-legend").textContent, /origin.*destination.*width = riders/);
  // A city's network is drawn close up, not as a dot on the whole country:
  // its stops spread across the map, each named once.
  const xs = m.all("circle.map-stop").map((c) => Number(c.getAttribute("cx")));
  assert.ok(Math.max(...xs) - Math.min(...xs) > SIZE.w / 3, `stops span ${Math.max(...xs) - Math.min(...xs)}px`);
  assert.deepEqual(m.all("text.map-label").map((t) => t.textContent).sort(),
                   ["Midway", "Navy Pier", "O'Hare", "Union Station"]);
  // The world, by country name, with what could not be placed.
  const w = render("route_map", FLIGHTS.columns, FLIGHTS.rows, { x: "origin", y: ["destination", "passengers"] });
  assert.equal(w.all("path.map-route").length, 4);
  assert.ok(w.all("path.map-region").length > 170);
  assert.equal(w.el.querySelector(".map-note").textContent, "Not on the map: Atlantis");
  // A country too small for the base map is named as missing, not dropped
  // as if it were the quietest route.
  const sg = render("route_map", FLIGHTS.columns, [...FLIGHTS.rows.slice(0, 3), ["Australia", "Singapore", 2100]],
                    { x: "origin", y: ["destination", "passengers"] });
  assert.equal(sg.all("path.map-route").length, 3);
  assert.equal(sg.el.querySelector(".map-note").textContent, "Not on the map: Singapore");
  assert.doesNotMatch(sg.el.querySelector(".map-legend").textContent, /busiest/);
});

test("a point map draws a dot per place, sized by its value", () => {
  const m = render("point_map", ["stop_name", "stop_lat", "stop_lon", "boardings"],
                   [["Clark/Lake", 41.8857, -87.6309, 9000], ["Howard", 42.019, -87.6728, 4000]],
                   { x: "stop_name", y: ["boardings"] });
  const dots = m.all("circle.map-point");
  assert.equal(dots.length, 2);
  assert.ok(Number(dots[0].getAttribute("r")) > Number(dots[1].getAttribute("r")));   // biggest first
  assert.ok(dots.every((c) => c.getAttribute("data-r")));             // keeps its size when zoomed
});

test("a choropleth shades the places it knows and keys its scale", () => {
  const m = render("choropleth", ["country", "revenue"], [["France", 10], ["Japan", 40], ["Brazil", 25]],
                   { x: "country", y: ["revenue"] });
  const shaded = m.all("path.map-shaded");
  assert.equal(shaded.length, 3);
  assert.equal(new Set(shaded.map((p) => p.getAttribute("fill"))).size, 3);
  assert.equal(m.all(".map-legend rect").length, 6);                  // 5 steps + no data
  assert.equal(m.el.querySelector(".map-legend").textContent, "1040no data · shade = revenue");
  const big = render("choropleth", ["country", "revenue"], [["France", 400000], ["Japan", 9100000]],
                     { x: "country", y: ["revenue"] });
  assert.match(big.el.querySelector(".map-legend").textContent, /^400k9\.1M/);
});

test("a map refuses data with no places, and centres a country on its main land", () => {
  assert.equal(render("route_map", ["region", "revenue"], [["West", 1]], { x: "region", y: ["revenue"] }).out, false);
  assert.equal(render("choropleth", ["region", "revenue"], [["West", 1]], { x: "region", y: ["revenue"] }).out, false);
  const countries = geo.topojson.feature(geo.world, geo.world.objects.countries).features;
  const france = mainCentroid(d3, countries.find((f) => f.id === "250"));
  assert.ok(france[0] > -5 && france[0] < 10 && france[1] > 41 && france[1] < 51, String(france));
  const usa = mainCentroid(d3, countries.find((f) => f.id === "840"));
  assert.ok(usa[0] > -125 && usa[0] < -67 && usa[1] > 25 && usa[1] < 50, String(usa));
});
