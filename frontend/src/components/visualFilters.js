// The filter bar over every visual in the D3 & artifacts window: pick a date
// range, narrow categories, keep the top N — without asking again. The
// controls are read off the rows the visual already has (no warehouse round
// trip), so they work the same for a built-in D3 chart and for a custom
// visual the agent wrote. Pure functions, so they are tested on their own.

const DATE = /^(\d{4})-(\d{2})(?:-(\d{2}))?(?:[T ].*)?$/;
const MAX_CATEGORIES = 30;          // more distinct values than this is not a filter
const MAX_CATEGORY_CONTROLS = 2;
const TOP_N_TYPES = new Set(["bar", "hbar", "stacked_bar", "pie", "donut", "treemap",
                             "funnel", "circle_pack", "combo"]);
export const TOP_N_CHOICES = [5, 10, 20];

/** "2026-03-14T…" → "2026-03-14"; "2026-03" → "2026-03-01"; else null. */
export function dayOf(value) {
  if (value == null) return null;
  const m = DATE.exec(String(value).trim());
  if (!m) return null;
  return `${m[1]}-${m[2]}-${m[3] || "01"}`;
}

const isNumber = (v) => v !== null && v !== "" && !Number.isNaN(Number(v));

/** What the bar offers for this data: a date range, category filters, top-N. */
export function detectControls(columns, rows, spec) {
  const out = { date: null, categories: [], topN: null };
  if (!columns?.length || !rows?.length) return out;
  const values = (i) => rows.map((r) => r[i]).filter((v) => v !== null && v !== undefined && v !== "");

  // A date column: most values read as dates. Monthly data (every day "01")
  // gets month pickers instead of day pickers.
  for (let i = 0; i < columns.length && !out.date; i++) {
    const vs = values(i);
    const days = vs.map(dayOf).filter(Boolean);
    if (vs.length && days.length / vs.length >= 0.8 && !vs.every(isNumber)) {
      const sorted = [...days].sort();
      out.date = {
        column: columns[i], min: sorted[0], max: sorted[sorted.length - 1],
        monthly: days.every((d) => d.endsWith("-01")) && new Set(days).size > 1,
      };
    }
  }

  // Category filters: text columns with a handful of distinct values. The
  // chart's own category (x, or a series column) first.
  const preferred = [spec?.x, ...(spec?.y || []).slice(1)].filter(Boolean);
  const order = [...preferred.map((c) => columns.indexOf(c)).filter((i) => i >= 0),
                 ...columns.map((_, i) => i)];
  for (const i of [...new Set(order)]) {
    if (out.categories.length >= MAX_CATEGORY_CONTROLS) break;
    if (columns[i] === out.date?.column) continue;
    const vs = values(i);
    if (!vs.length || vs.every(isNumber)) continue;
    const distinct = [...new Set(vs.map(String))];
    if (distinct.length >= 2 && distinct.length <= MAX_CATEGORIES) {
      out.categories.push({ column: columns[i], values: distinct.sort((a, b) => a.localeCompare(b)) });
    }
  }

  // Top N: a ranking chart with more categories than fit comfortably.
  const xi = columns.indexOf(spec?.x);
  const yi = columns.indexOf(spec?.y?.[0]);
  if (TOP_N_TYPES.has(spec?.type) && xi >= 0 && yi >= 0
      && new Set(rows.map((r) => String(r[xi]))).size > TOP_N_CHOICES[1]) {
    out.topN = { column: spec.x, by: spec.y[0] };
  }
  return out;
}

const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;

/** The presets a date range offers, each {key, label, from} — anchored on the
 *  data's own latest date, not today's: the data may end months ago. */
export function datePresets(date) {
  if (!date) return [];
  const max = new Date(`${date.max}T00:00:00Z`);
  const back = (days, months = 0) => {
    const d = new Date(max);
    d.setUTCMonth(d.getUTCMonth() - months);
    d.setUTCDate(d.getUTCDate() - days);
    return iso(d);
  };
  const presets = date.monthly
    ? [["3m", "Last 3 months", back(0, 2)], ["6m", "Last 6 months", back(0, 5)],
       ["12m", "Last 12 months", back(0, 11)]]
    : [["7d", "Last 7 days", back(6)], ["30d", "Last 30 days", back(29)],
       ["90d", "Last 90 days", back(89)], ["12m", "Last 12 months", back(-1, 12)]];
  presets.push(["ytd", "Year to date", `${date.max.slice(0, 4)}-01-01`]);
  return presets.map(([key, label, from]) => ({ key, label, from: from < date.min ? date.min : from }));
}

export const EMPTY_STATE = Object.freeze({ from: null, to: null, preset: "all", cats: {}, top: null });

/** True when the state narrows anything. */
export function isFiltered(state) {
  return Boolean(state && (state.from || state.to || state.top
    || Object.values(state.cats || {}).some((v) => Array.isArray(v))));
}

/** The rows the visual draws under `state`. */
export function applyControls(columns, rows, controls, state) {
  if (!rows?.length || !isFiltered(state)) return rows || [];
  let out = rows;
  const { date, topN } = controls || {};
  if (date && (state.from || state.to)) {
    const di = columns.indexOf(date.column);
    out = out.filter((r) => {
      const d = dayOf(r[di]);
      return d && (!state.from || d >= state.from) && (!state.to || d <= state.to);
    });
  }
  for (const [col, keep] of Object.entries(state.cats || {})) {
    if (!Array.isArray(keep)) continue;                // null = every value
    const ci = columns.indexOf(col);
    if (ci < 0) continue;
    const allowed = new Set(keep);
    out = out.filter((r) => allowed.has(String(r[ci])));
  }
  if (topN && state.top) {
    const xi = columns.indexOf(topN.column);
    const yi = columns.indexOf(topN.by);
    const totals = new Map();
    for (const r of out) totals.set(String(r[xi]), (totals.get(String(r[xi])) || 0) + (Number(r[yi]) || 0));
    const top = new Set([...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, state.top).map(([k]) => k));
    out = out.filter((r) => top.has(String(r[xi])));
  }
  return out;
}
