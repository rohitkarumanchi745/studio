// Every chart a result's data can make — read from the data, not a fixed list.
//
// A result is profiled column by column (a measure, a date, a category, a
// label, a coordinate…) and each view is built for what it shows: a total per
// day for a trend, a total per category for a comparison, shares, a matrix of
// two categories, flows between the two ends of a trip, places on a map, the
// spread of a measure, two measures against each other, the headline total,
// the rows themselves. Each view carries its own (summarized) data, so a bar
// chart of 1,800 raw trips is 7 bars — riders per line — not 1,800 slivers.
// Pure: rows in, views out.
import { choroplethData, coordinatePairs, pointMapData, routeMapData, tokens } from "./renderers/geo";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}/;
const ID_LIKE = /(^id$|_id$|^id_|uuid)/i;
// A measure people mean when they ask "how much": tried first.
const PRIMARY = /revenue|sales|amount|total|riders|units|qty|quantity|orders|value|count|spend|cost/i;
const ORIGIN = new Set(["origin", "orig", "from", "src", "source", "start", "pickup"]);
const DEST = new Set(["dest", "destination", "to", "dst", "target", "end", "dropoff"]);
const MAX_RAW = 2000;          // a view of every row draws at most this many

/** Each column's kind: measure | date | category | label | coord | id. */
export function profile(columns, rows) {
  const sample = rows.slice(0, MAX_RAW);
  const coord = new Set(coordinatePairs(columns, sample).flatMap((p) => [p.lat, p.lon]));
  return columns.map((name, i) => {
    const vals = sample.map((r) => r[i]).filter((v) => v !== null && v !== undefined && v !== "");
    const distinct = new Set(vals.map(String)).size;
    const first = vals[0];
    let kind;
    if (coord.has(i)) kind = "coord";
    else if (typeof first === "number") kind = ID_LIKE.test(name) ? "id" : "measure";
    else if (vals.length && vals.slice(0, 50).every((v) => ISO_DATE.test(String(v)))) kind = "date";
    else kind = distinct <= 30 ? "category" : "label";
    return { name, i, kind, distinct };
  });
}

/** Rows summed by the key columns: [[...keys, total]] (a count with no measure). */
export function aggregate(rows, keys, vi) {
  const out = new Map();
  for (const r of rows) {
    const key = keys.map((i) => r[i]);
    const id = JSON.stringify(key);
    const v = vi === null || vi === undefined ? 1 : Number(r[vi]);
    const hit = out.get(id) || [...key, 0];
    hit[hit.length - 1] += Number.isFinite(v) ? v : 0;
    out.set(id, hit);
  }
  return [...out.values()].map((r) => {
    const last = r.length - 1;
    r[last] = Math.round(r[last] * 100) / 100;
    return r;
  });
}

const role = (name) => {
  const t = tokens(name);
  return t.some((x) => ORIGIN.has(x)) ? "origin" : t.some((x) => DEST.has(x)) ? "dest" : null;
};

/**
 * The views a result can be drawn as, most telling first, at most `max`:
 * [{key, type, title, columns, rows, spec}]. Whether each one actually draws
 * is the renderer's fit check's to say (ChartView.computeFit).
 */
export function recommendViews(columns, rows, max = 16) {
  if (!columns?.length || !rows?.length) return [];
  const prof = profile(columns, rows);
  const measures = prof.filter((p) => p.kind === "measure");
  const m = measures.find((p) => PRIMARY.test(p.name)) || measures[0] || null;
  const val = m ? m.name : "rows";
  const vi = m ? m.i : null;
  const dates = prof.filter((p) => p.kind === "date");
  const cats = prof.filter((p) => p.kind === "category" && p.distinct >= 2)
    .sort((a, b) => a.distinct - b.distinct);
  const labels = prof.filter((p) => p.kind === "label");
  const raw = rows.slice(0, MAX_RAW);
  const out = [];
  // The rows themselves always have a place: the last one, kept free for them.
  const add = (v) => {
    if ((out.length < max - 1 || v.key === "table") && !out.some((o) => o.key === v.key)) out.push(v);
  };
  const view = (key, type, title, cols, data, x, y) =>
    add({ key, type, title, columns: cols, rows: data, spec: { type, title, x, y } });

  // Places: when the data has them, a map is its own view.
  const firstText = (cats[0] || labels[0] || prof[0]).name;
  for (const [type, build, title] of [
    ["route_map", routeMapData, `${val} between places`],
    ["point_map", pointMapData, "places"],
    ["choropleth", choroplethData, `${val} by place`],
  ]) {
    const spec = { type, x: firstText, y: m ? [m.name] : [] };
    if (build(columns, raw, spec)) add({ key: type, type, title, columns, rows: raw, spec: { ...spec, title } });
  }

  // Over time: a total per date.
  if (dates.length) {
    const d = dates[0];
    const series = aggregate(rows, [d.i], vi).sort((a, b) => String(a[0]).localeCompare(String(b[0])));
    view("line", "line", `${val} over ${d.name}`, [d.name, val], series, d.name, [val]);
    view("area", "area", `${val} over ${d.name}, filled`, [d.name, val], series, d.name, [val]);
    const split = cats.find((c) => c.distinct <= 6);
    if (split) {
      const names = [...new Set(rows.map((r) => String(r[split.i])))];
      const byDate = new Map();
      for (const [day, cat, total] of aggregate(rows, [d.i, split.i], vi)) {
        const row = byDate.get(day) || [day, ...names.map(() => 0)];
        row[1 + names.indexOf(String(cat))] = total;
        byDate.set(day, row);
      }
      const pivot = [...byDate.values()].sort((a, b) => String(a[0]).localeCompare(String(b[0])));
      view("stacked_area", "stacked_area", `${val} over ${d.name} by ${split.name}`,
           [d.name, ...names], pivot, d.name, names);
    }
    view("calendar", "calendar", `${val} per day`, [d.name, val], series, d.name, [val]);
  }

  // Comparisons and shares: a total per category.
  for (const c of cats.slice(0, 2)) {
    const totals = aggregate(rows, [c.i], vi).sort((a, b) => b[1] - a[1]);
    view(`bar:${c.name}`, "bar", `${val} by ${c.name}`, [c.name, val], totals, c.name, [val]);
  }
  if (cats[0]) {
    const totals = aggregate(rows, [cats[0].i], vi).sort((a, b) => b[1] - a[1]);
    if (cats[0].distinct <= 8) {
      view("donut", "donut", `share of ${val} by ${cats[0].name}`, [cats[0].name, val], totals,
           cats[0].name, [val]);
    } else {
      view("treemap", "treemap", `share of ${val} by ${cats[0].name}`, [cats[0].name, val], totals,
           cats[0].name, [val]);
    }
  }
  if (labels[0]) {
    const top = aggregate(rows, [labels[0].i], vi).sort((a, b) => b[1] - a[1]).slice(0, 15);
    view("hbar", "hbar", `top ${labels[0].name} by ${val}`, [labels[0].name, val], top,
         labels[0].name, [val]);
  }

  // Flows: from one end to the other (a trip's stops, a shipment's countries).
  const ends = [...cats, ...labels];
  const from = ends.find((c) => role(c.name) === "origin");
  const to = ends.find((c) => role(c.name) === "dest");
  if (from && to) {
    view("network", "network", `${val} from ${from.name} to ${to.name}`, [from.name, to.name, val],
         aggregate(rows, [from.i, to.i], vi), from.name, [to.name, val]);
  }

  // Two categories at once.
  if (cats.length >= 2 && cats[0].distinct * cats[1].distinct <= 600) {
    view("heatmap", "heatmap", `${val} by ${cats[0].name} and ${cats[1].name}`,
         [cats[0].name, cats[1].name, val], aggregate(rows, [cats[0].i, cats[1].i], vi),
         cats[0].name, [cats[1].name, val]);
  }

  // The spread of a measure, and two measures against each other.
  if (m) {
    view("histogram", "histogram", `how ${val} is spread`, [val], raw.map((r) => [r[m.i]]), val, [val]);
    const group = cats.find((c) => c.distinct <= 20);
    if (group) {
      view("boxplot", "boxplot", `spread of ${val} per ${group.name}`, [group.name, val],
           raw.map((r) => [r[group.i], r[m.i]]), group.name, [val]);
    }
    const other = measures.find((p) => p !== m);
    if (other) {
      view("scatter", "scatter", `${val} against ${other.name}`, [other.name, val],
           raw.map((r) => [r[other.i], r[m.i]]), other.name, [val]);
    }
    const total = aggregate(rows, [], vi)[0][0];
    view("kpi", "kpi", `total ${val}`, [val], [[total]], val, [val]);
  }

  view("table", "table", "the rows", columns, raw, columns[0], columns.slice(1, 3));
  return out;
}
