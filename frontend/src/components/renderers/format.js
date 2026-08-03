// FORMAT — pixels, never data. One implementation of the chart spec's `format`
// section, shared by all three engines. Every export is pure and total: it
// deep-clones before mutating, returns a new object, and degrades (with a
// warning) on unknown keys, wrong types or partial specs rather than throwing.
// Data changes live server-side in viz.py; nothing here alters a value, a
// column or a row count. The only exception to "absent format ⇒ no-op" is
// ctx.highlight, which is always honoured (G9).
import {
  AXIS_LINE, BAD, GOOD, GRID_LINE, INK, SEQ, SERIES, SURFACE, TOTAL,
  resolveSpec,
} from "./common";

// ── tokens & vocabulary ─────────────────────────────────────────────────

export const COLOR_TOKENS = Object.freeze({
  GOOD, BAD, TOTAL,
  ACCENT: SERIES[0],
  INK: INK.primary,
  MUTED: INK.muted,
  OTHER: "#5a5a56",
  SERIES0: SERIES[0], SERIES1: SERIES[1], SERIES2: SERIES[2], SERIES3: SERIES[3],
  SERIES4: SERIES[4], SERIES5: SERIES[5], SERIES6: SERIES[6], SERIES7: SERIES[7],
});

export const NUMBER_DEFAULT = Object.freeze({
  style: "number", currency: "USD", decimals: null, compact: "auto",
  prefix: "", suffix: "", scale: 1, locale: null, negative: "minus", null_text: "—",
});

function deepFreeze(o) {
  for (const v of Object.values(o)) if (v && typeof v === "object") deepFreeze(v);
  return Object.freeze(o);
}

export const FORMAT_DEFAULTS = deepFreeze({
  number: {},
  date: {},
  labels: Object.freeze({
    show: false, position: "auto", series: null, from_col: null, format: null,
    max_points: 30, color: null, rotate: 0,
  }),
  axes: Object.freeze({
    x: Object.freeze({
      title: null, show: true, label_angle: 0, label_limit: null, grid: false,
      min: null, max: null, reverse: false, type: "auto", tick_count: null,
    }),
    y: Object.freeze({
      title: null, show: true, grid: true, min: null, max: null,
      type: "auto", zero: true, tick_count: null,
    }),
    y2: Object.freeze({
      title: null, show: true, min: null, max: null, type: "auto", zero: true,
    }),
  }),
  legend: Object.freeze({ show: "auto", position: "top", orient: "auto", max_items: 12 }),
  palette: Object.freeze({
    mode: "theme", colors: null, key_order: null, by_key: null,
    other_color: "#5a5a56", sequential: null, diverging: null,
  }),
  series: {},
  reference_lines: [],
  conditional_colors: null,
  annotations: [],
  title: Object.freeze({ show: true, subtitle: null, align: "left" }),
  tooltip: Object.freeze({ show: true, trigger: "auto", fields: [] }),
  data_zoom: Object.freeze({ show: false, start: 0, end: 100 }),
  totals: Object.freeze({ show: false, label: "Total" }),
  empty_text: "No data",
});

export const FORMAT_KEYS = Object.freeze(Object.keys(FORMAT_DEFAULTS));

const NUMBER_STYLES = ["number", "currency", "percent", "compact", "scientific", "bytes"];
const DATE_PRESETS = ["year", "quarter", "month", "day", "datetime", "time"];
const LABEL_POSITIONS = ["auto", "top", "inside", "outside", "end", "center", "right"];
const LEGEND_POSITIONS = ["top", "bottom", "left", "right"];
const AXIS_TYPES = ["auto", "category", "value", "time", "log"];
const SERIES_TYPES = ["bar", "line", "area", "scatter"];
const DASHES = ["solid", "dashed", "dotted"];
const REF_MODES = ["value", "average", "median", "min", "max", "percentile"];
const RULE_OPS = ["eq", "ne", "gt", "gte", "lt", "lte", "between", "contains", "ncontains",
                  "startswith", "endswith", "isnull", "notnull"];

// Axis-less types (G1) and flat types that own no chart surface at all.
const AXISLESS = new Set(["pie", "donut", "treemap", "funnel", "radar", "gauge", "sankey", "kpi", "table"]);
const FLAT = new Set(["kpi", "table"]);
// Types whose option carries exactly one series drawing the first measure.
const SINGLE_MEASURE = new Set(["pie", "donut", "treemap", "funnel", "gauge", "heatmap",
                                "sankey", "scatter", "bubble", "boxplot", "radar"]);
const CATEGORY_COLORED = new Set(["bar", "hbar", "pie", "donut", "treemap", "funnel"]);

// ── tiny utils ──────────────────────────────────────────────────────────

const isObj = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const arr = (v) => (Array.isArray(v) ? v : []);
const str = (v) => (v == null ? "" : String(v));
const oneOf = (v, list, dflt) => (list.includes(v) ? v : dflt);
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
function numOr(v, dflt) {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : dflt;
}
function pathGet(obj, path) {
  let cur = obj;
  for (const k of String(path).split(".")) {
    if (!cur || typeof cur !== "object") return undefined;
    cur = cur[k];
  }
  return cur;
}
// Structural clone that passes functions/Dates through — option objects carry
// formatter callbacks that structuredClone would reject.
function clone(v, depth = 0) {
  if (depth > 12) return v;
  if (Array.isArray(v)) return v.map((x) => clone(x, depth + 1));
  if (v && typeof v === "object") {
    const proto = Object.getPrototypeOf(v);
    if (proto !== Object.prototype && proto !== null) return v;
    const o = {};
    for (const k of Object.keys(v)) o[k] = clone(v[k], depth + 1);
    return o;
  }
  return v;
}

// ── colors ──────────────────────────────────────────────────────────────

export function resolveColor(token, fallbackHex = null) {
  if (typeof token !== "string") return fallbackHex;
  const t = token.trim();
  if (!t) return fallbackHex;
  if (/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(t)) return t;
  if (/^(rgb|hsl)a?\(/i.test(t)) return t;
  const key = t.toUpperCase();
  if (Object.prototype.hasOwnProperty.call(COLOR_TOKENS, key)) return COLOR_TOKENS[key];
  const m = /^SERIES(\d+)$/.exec(key);
  if (m) return SERIES[Number(m[1]) % SERIES.length];
  return fallbackHex;
}

function hexParts(hex) {
  const h = String(hex || "").replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h.slice(0, 6);
  const n = parseInt(full, 16);
  return Number.isFinite(n) && full.length === 6
    ? [(n >> 16) & 255, (n >> 8) & 255, n & 255]
    : null;
}
function mixHex(a, b, t) {
  const pa = hexParts(a), pb = hexParts(b);
  if (!pa || !pb) return a;
  const f = clamp(numOr(t, 0), 0, 1);
  const c = pa.map((v, i) => Math.round(v + (pb[i] - v) * f));
  return "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");
}
function rampColor(t, ramp) {
  const stops = arr(ramp).map((c) => resolveColor(c, null)).filter(Boolean);
  if (stops.length === 0) return SEQ[SEQ.length - 1];
  if (stops.length === 1 || !Number.isFinite(t)) return stops[0];
  const x = clamp(t, 0, 1) * (stops.length - 1);
  const i = Math.floor(x);
  return i >= stops.length - 1 ? stops[stops.length - 1] : mixHex(stops[i], stops[i + 1], x - i);
}

// ── number formatting ───────────────────────────────────────────────────

function normNumber(nf, warnings, where) {
  const s = isObj(nf) ? nf : {};
  const style = oneOf(s.style, NUMBER_STYLES, NUMBER_DEFAULT.style);
  if (s.style != null && style !== s.style && warnings) {
    warnings.push(`${where}.style "${str(s.style)}" is unknown — using "number"`);
  }
  let decimals = s.decimals == null ? null : Math.round(clamp(numOr(s.decimals, 0), 0, 6));
  if (s.decimals != null && !Number.isFinite(numOr(s.decimals, NaN))) decimals = null;
  const compact = s.compact === true || s.compact === false ? s.compact
    : s.compact === "auto" || s.compact == null ? "auto" : "auto";
  return {
    style, currency: typeof s.currency === "string" && s.currency ? s.currency : NUMBER_DEFAULT.currency,
    decimals, compact,
    prefix: typeof s.prefix === "string" ? s.prefix : "",
    suffix: typeof s.suffix === "string" ? s.suffix : "",
    scale: numOr(s.scale, 1) || 1,
    locale: typeof s.locale === "string" && s.locale ? s.locale : null,
    negative: oneOf(s.negative, ["minus", "paren"], "minus"),
    null_text: typeof s.null_text === "string" ? s.null_text : NUMBER_DEFAULT.null_text,
  };
}

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

export function makeFormatter(numberFormat) {
  const f = normNumber(numberFormat, null, "number");
  const wantCompact = (n) =>
    f.style === "compact" || f.compact === true ||
    (f.compact === "auto" && Math.abs(n) >= 10000);

  return (v) => {
    if (v == null || v === "") return f.null_text;
    if (typeof v === "boolean") return String(v);
    const raw = typeof v === "number" ? v : Number(String(v).replace(/[\s,]/g, ""));
    if (!Number.isFinite(raw)) return typeof v === "number" ? f.null_text : String(v);
    let n = raw * f.scale;
    if (!Number.isFinite(n)) return f.null_text;

    const neg = n < 0 && f.negative === "paren";
    if (neg) n = -n;
    let body;
    if (f.style === "bytes") {
      let u = 0, x = Math.abs(n);
      while (x >= 1024 && u < BYTE_UNITS.length - 1) { x /= 1024; u++; }
      const d = f.decimals == null ? (u === 0 ? 0 : 1) : f.decimals;
      body = (n < 0 ? "-" : "") + x.toFixed(d) + " " + BYTE_UNITS[u];
    } else {
      const opts = {};
      if (f.style === "currency") { opts.style = "currency"; opts.currency = f.currency; }
      else if (f.style === "percent") opts.style = "percent";
      else if (f.style === "scientific") opts.notation = "scientific";
      const compacting = f.style !== "scientific" && wantCompact(n);
      if (compacting) {
        // decimals govern the full-precision form; the compact mantissa keeps
        // one digit so 1234567 reads "1.2M", never "1M".
        opts.notation = "compact"; opts.compactDisplay = "short";
        opts.minimumFractionDigits = 0; opts.maximumFractionDigits = 1;
      } else if (f.decimals == null) {
        // The |n| ≥ 1000 rule trims digit noise off plain numbers; a scientific
        // mantissa is always < 10, so magnitude must not reach it.
        opts.minimumFractionDigits = 0;
        opts.maximumFractionDigits = f.style !== "scientific" && Math.abs(n) >= 1000 ? 0 : 2;
      } else {
        opts.minimumFractionDigits = f.decimals; opts.maximumFractionDigits = f.decimals;
      }
      body = fmtIntl(n, f.locale, opts);
    }
    const out = f.prefix + body + f.suffix;
    return neg ? "(" + out + ")" : out;
  };
}

function fmtIntl(n, locale, opts) {
  try {
    return new Intl.NumberFormat(locale || undefined, opts).format(n);
  } catch {
    try { return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(n); }
    catch { return String(n); }
  }
}

const _fmtCache = new Map();
export function formatValue(v, numberFormat) {
  let key;
  try { key = numberFormat ? JSON.stringify(numberFormat) : "_"; } catch { key = null; }
  if (key == null) return makeFormatter(numberFormat)(v);
  let f = _fmtCache.get(key);
  if (!f) {
    if (_fmtCache.size > 64) _fmtCache.clear();
    f = makeFormatter(numberFormat);
    _fmtCache.set(key, f);
  }
  return f(v);
}

// d3-format string for Vega/Plotly, which format on their own side.
export function toD3Format(numberFormat) {
  const f = normNumber(numberFormat, null, "number");
  const d = f.decimals;
  if (f.style === "percent") return `,.${d == null ? 1 : d}%`;
  if (f.style === "scientific") return `.${d == null ? 2 : d}e`;
  if (f.style === "bytes" || f.style === "compact" || f.compact === true) return "~s";
  if (f.style === "currency") return `$,.${d == null ? 0 : d}f`;
  return d == null ? "," : `,.${d}f`;
}

// ── date formatting ─────────────────────────────────────────────────────

const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const DATE_TOKENS = /yyyy|yy|MMMM|MMM|MM|M|dd|d|EEEE|EEE|HH|mm|ss|Q/g;

// Accepts Date, epoch ms, ISO strings and viz.py's sortable bin labels
// ("2026", "2026-Q1", "2026-03", "2026-03-04 14"). null ⇒ unparseable.
function parseDateish(v) {
  if (v instanceof Date) return Number.isFinite(v.getTime()) ? v : null;
  if (typeof v === "number") {
    if (Number.isInteger(v) && v >= 1000 && v <= 9999) return new Date(Date.UTC(v, 0, 1));
    const d = new Date(v);
    return Number.isFinite(d.getTime()) ? d : null;
  }
  const s = str(v).trim();
  if (!s) return null;
  let m;
  if ((m = /^(\d{4})$/.exec(s))) return new Date(Date.UTC(+m[1], 0, 1));
  if ((m = /^(\d{4})-Q([1-4])$/i.exec(s))) return new Date(Date.UTC(+m[1], (+m[2] - 1) * 3, 1));
  if ((m = /^(\d{4})-(\d{2})$/.exec(s))) return new Date(Date.UTC(+m[1], +m[2] - 1, 1));
  if ((m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2})$/.exec(s)))
    return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4]));
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(s + "T00:00:00Z");
  const t = Date.parse(s);
  return Number.isFinite(t) ? new Date(t) : null;
}

function formatPattern(d, pattern) {
  const y = d.getUTCFullYear(), mo = d.getUTCMonth(), day = d.getUTCDate();
  const h = d.getUTCHours(), mi = d.getUTCMinutes(), se = d.getUTCSeconds();
  const two = (n) => String(n).padStart(2, "0");
  return String(pattern).replace(DATE_TOKENS, (tok) => {
    switch (tok) {
      case "yyyy": return String(y);
      case "yy": return two(y % 100);
      case "MMMM": return MONTHS[mo];
      case "MMM": return MONTHS[mo].slice(0, 3);
      case "MM": return two(mo + 1);
      case "M": return String(mo + 1);
      case "dd": return two(day);
      case "d": return String(day);
      case "EEEE": return DAYS[d.getUTCDay()];
      case "EEE": return DAYS[d.getUTCDay()].slice(0, 3);
      case "HH": return two(h);
      case "mm": return two(mi);
      case "ss": return two(se);
      case "Q": return String(Math.floor(mo / 3) + 1);
      default: return tok;
    }
  });
}

const PRESET_PATTERNS = {
  year: "yyyy", month: "MMM yyyy", day: "MMM d",
  datetime: "MMM d, HH:mm", time: "HH:mm:ss",
};

export function makeDateFormatter(dateFormat) {
  const s = isObj(dateFormat) ? dateFormat : {};
  const pattern = typeof s.pattern === "string" && s.pattern ? s.pattern : null;
  const preset = oneOf(s.preset, DATE_PRESETS, null);
  return (v) => {
    if (v == null || v === "") return "";
    const d = parseDateish(v);
    if (!d) return str(v);                       // unparseable ⇒ identity
    if (pattern) return formatPattern(d, pattern);
    if (preset === "quarter") return "Q" + formatPattern(d, "Q yyyy");
    if (preset) return formatPattern(d, PRESET_PATTERNS[preset] || "yyyy-MM-dd");
    return str(v);
  };
}

// ── palette identity ────────────────────────────────────────────────────

function normPalette(p, warnings) {
  const s = isObj(p) ? p : {};
  const mode = oneOf(s.mode, ["theme", "custom", "by_key"], "theme");
  if (s.mode != null && mode !== s.mode && warnings) {
    warnings.push(`format.palette.mode "${str(s.mode)}" is unknown — using "theme"`);
  }
  const colors = Array.isArray(s.colors)
    ? s.colors.map((c) => resolveColor(c, null)).filter(Boolean)
    : null;
  const byKey = isObj(s.by_key)
    ? Object.fromEntries(Object.entries(s.by_key)
        .map(([k, v]) => [String(k), resolveColor(v, null)]).filter(([, v]) => v))
    : null;
  return {
    mode,
    colors: colors && colors.length ? colors : null,
    key_order: Array.isArray(s.key_order) ? s.key_order.map(String) : null,
    by_key: byKey && Object.keys(byKey).length ? byKey : null,
    other_color: resolveColor(s.other_color, COLOR_TOKENS.OTHER),
    sequential: Array.isArray(s.sequential)
      ? s.sequential.map((c) => resolveColor(c, null)).filter(Boolean) : null,
    diverging: Array.isArray(s.diverging)
      ? s.diverging.map((c) => resolveColor(c, null)).filter(Boolean) : null,
  };
}

// `other_color` is reserved for the top_n roll-up bucket, which only exists
// when the transform built one — a genuine category named "Other" is not that
// bucket and keeps its palette hue. An author-supplied other_color is taken as
// a deliberate pin on the name, since nothing generates that key on its own.
function otherKeyTest(spec) {
  const s = spec || {};
  const other = pathGet(s, "transform.top_n.other");
  if (isObj(other) && other.enabled === true) {
    const label = typeof other.label === "string" && other.label ? other.label : "Other";
    return (k) => k === label;
  }
  if (pathGet(s, "format.palette.other_color") != null) return (k) => /^other$/i.test(k);
  return () => false;
}

export function paletteFor(spec, keys) {
  const p = normPalette(pathGet(spec || {}, "format.palette"), null);
  const colors = p.colors && p.colors.length ? p.colors : SERIES;
  const byKey = p.by_key || {};
  const order = p.key_order;
  const local = arr(keys).map(String);
  const isOther = otherKeyTest(spec);
  const colorOf = (key) => {
    const k = String(key);
    if (byKey[k]) return byKey[k];
    if (isOther(k)) return p.other_color;
    if (order && order.length) {
      const i = order.indexOf(k);
      if (i >= 0) return colors[i % colors.length];
    }
    const j = local.indexOf(k);
    return colors[(j >= 0 ? j : 0) % colors.length];
  };
  return { colors, byKey, otherColor: p.other_color, colorOf };
}

// Canonical category order, captured at pin time so filtering never repaints.
export function captureKeyOrder(columns, rows, spec) {
  const cols = arr(columns);
  const key = pathGet(spec || {}, "x") || pathGet(spec || {}, "fields.dimensions.0.col");
  const i = key == null ? -1 : cols.indexOf(key);
  if (i < 0) return [];
  const seen = new Set(), out = [];
  for (const r of arr(rows)) {
    const v = String(Array.isArray(r) ? r[i] : r);
    if (!seen.has(v)) { seen.add(v); out.push(v); }
  }
  return out;
}

// ── normalization ───────────────────────────────────────────────────────

function normNumberMap(m, warnings) {
  const out = {};
  if (!isObj(m)) return out;
  for (const [col, nf] of Object.entries(m)) out[col] = normNumber(nf, warnings, `format.number.${col}`);
  return out;
}
function normDateMap(m, warnings) {
  const out = {};
  if (!isObj(m)) return out;
  for (const [col, df] of Object.entries(m)) {
    const s = isObj(df) ? df : {};
    const preset = oneOf(s.preset, DATE_PRESETS, null);
    if (s.preset != null && preset == null && warnings) {
      warnings.push(`format.date.${col}.preset "${str(s.preset)}" is unknown — ignored`);
    }
    out[col] = { preset, pattern: typeof s.pattern === "string" ? s.pattern : null };
  }
  return out;
}
function normAxis(a, dflt) {
  const s = isObj(a) ? a : {};
  const o = { ...dflt };
  if ("title" in s) o.title = s.title == null ? null : str(s.title);
  if ("show" in s) o.show = s.show !== false;
  if ("grid" in s && "grid" in dflt) o.grid = s.grid === true;
  if ("reverse" in s && "reverse" in dflt) o.reverse = s.reverse === true;
  if ("zero" in s && "zero" in dflt) o.zero = s.zero !== false;
  if ("label_angle" in s && "label_angle" in dflt) o.label_angle = clamp(numOr(s.label_angle, 0), -90, 90);
  if ("label_limit" in s && "label_limit" in dflt) {
    o.label_limit = s.label_limit == null ? null : Math.max(10, numOr(s.label_limit, 100));
  }
  if ("min" in s) o.min = Number.isFinite(numOr(s.min, NaN)) ? numOr(s.min, 0) : null;
  if ("max" in s) o.max = Number.isFinite(numOr(s.max, NaN)) ? numOr(s.max, 0) : null;
  if ("type" in s) o.type = oneOf(s.type, AXIS_TYPES, "auto");
  if ("tick_count" in s && "tick_count" in dflt) {
    o.tick_count = s.tick_count == null ? null : Math.round(clamp(numOr(s.tick_count, 5), 2, 20));
  }
  return o;
}
function normSeriesMap(m, warnings) {
  const out = {};
  if (!isObj(m)) return out;
  for (const [col, cfg] of Object.entries(m)) {
    const s = isObj(cfg) ? cfg : {};
    out[col] = {
      type: oneOf(s.type, SERIES_TYPES, null),
      axis: oneOf(s.axis, ["y", "y2"], "y"),
      color: resolveColor(s.color, null),
      dash: oneOf(s.dash, DASHES, "solid"),
      width: clamp(numOr(s.width, 2), 0, 12),
      smooth: s.smooth === true,
      show_symbol: s.show_symbol === true,
      stack: s.stack == null ? null : str(s.stack),
      opacity: s.opacity == null ? null : clamp(numOr(s.opacity, 1), 0, 1),
      show: s.show !== false,
      _keys: Object.keys(s),
    };
    if (s.type != null && out[col].type == null && warnings) {
      warnings.push(`format.series.${col}.type "${str(s.type)}" is unknown — ignored`);
    }
  }
  return out;
}
function normRefLines(list, warnings) {
  return arr(list).map((r, i) => {
    const s = isObj(r) ? r : {};
    const mode = oneOf(s.mode, REF_MODES, "value");
    if (s.mode != null && mode !== s.mode && warnings) {
      warnings.push(`format.reference_lines[${i}].mode "${str(s.mode)}" is unknown — using "value"`);
    }
    return {
      axis: oneOf(s.axis, ["x", "y", "y2"], "y"),
      mode,
      value: Number.isFinite(numOr(s.value, NaN)) ? numOr(s.value, 0) : null,
      of: s.of == null ? null : str(s.of),
      percentile: clamp(numOr(s.percentile, 90), 0, 100),
      to: Number.isFinite(numOr(s.to, NaN)) ? numOr(s.to, 0) : null,
      label: s.label == null ? null : str(s.label),
      label_position: oneOf(s.label_position, ["start", "middle", "end"], "end"),
      color: resolveColor(s.color, null),
      dash: oneOf(s.dash, DASHES, "dashed"),
      width: clamp(numOr(s.width, 1), 0, 8),
      opacity: clamp(numOr(s.opacity, 0.12), 0, 1),
    };
  }).filter((r) => r.mode !== "value" || r.value != null);
}
function normConditional(cc, warnings) {
  if (!isObj(cc)) return null;
  const rules = arr(cc.rules).map((r) => {
    const s = isObj(r) ? r : {};
    const op = oneOf(s.op, RULE_OPS, null);
    if (op == null && warnings) warnings.push(`format.conditional_colors rule op "${str(s.op)}" is unknown — dropped`);
    return op == null ? null : {
      op, value: s.value, lo: s.lo, hi: s.hi,
      color: resolveColor(s.color, null), label: s.label == null ? null : str(s.label),
    };
  }).filter((r) => r && r.color);
  const sc = isObj(cc.scale) ? cc.scale : {};
  return {
    target: cc.target == null ? null : str(cc.target),
    by: cc.by == null ? null : str(cc.by),
    mode: oneOf(cc.mode, ["rules", "scale"], "rules"),
    rules,
    default: resolveColor(cc.default, null),
    scale: {
      ramp: Array.isArray(sc.ramp) ? sc.ramp.map((c) => resolveColor(c, null)).filter(Boolean) : null,
      min: Number.isFinite(numOr(sc.min, NaN)) ? numOr(sc.min, 0) : null,
      mid: Number.isFinite(numOr(sc.mid, NaN)) ? numOr(sc.mid, 0) : null,
      max: Number.isFinite(numOr(sc.max, NaN)) ? numOr(sc.max, 0) : null,
      diverging: sc.diverging === true,
    },
  };
}

export function normalizeFormat(format, ctx = {}) {
  const warnings = [];
  const c = isObj(ctx) ? ctx : {};
  const spec = isObj(c.spec) ? c.spec : {};
  const src = isObj(format) ? format : {};
  for (const k of Object.keys(src)) {
    if (!FORMAT_KEYS.includes(k)) warnings.push(`format.${k} is not a known key — dropped`);
  }
  const out = {
    number: normNumberMap(src.number, warnings),
    date: normDateMap(src.date, warnings),
    labels: { ...FORMAT_DEFAULTS.labels },
    axes: {
      x: normAxis(pathGet(src, "axes.x"), FORMAT_DEFAULTS.axes.x),
      y: normAxis(pathGet(src, "axes.y"), FORMAT_DEFAULTS.axes.y),
      y2: normAxis(pathGet(src, "axes.y2"), FORMAT_DEFAULTS.axes.y2),
    },
    legend: { ...FORMAT_DEFAULTS.legend },
    palette: normPalette(src.palette, warnings),
    series: normSeriesMap(src.series, warnings),
    reference_lines: normRefLines(src.reference_lines, warnings),
    conditional_colors: normConditional(src.conditional_colors, warnings),
    annotations: arr(src.annotations).filter(isObj).map((a) => ({
      x: a.x, y: Number.isFinite(numOr(a.y, NaN)) ? numOr(a.y, 0) : null,
      series: a.series == null ? null : str(a.series),
      text: str(a.text), arrow: a.arrow !== false,
      color: resolveColor(a.color, null),
      position: typeof a.position === "string" ? a.position : "top",
    })),
    title: { ...FORMAT_DEFAULTS.title },
    tooltip: { ...FORMAT_DEFAULTS.tooltip },
    data_zoom: { ...FORMAT_DEFAULTS.data_zoom },
    totals: { ...FORMAT_DEFAULTS.totals },
    empty_text: typeof src.empty_text === "string" ? src.empty_text : FORMAT_DEFAULTS.empty_text,
  };

  const l = isObj(src.labels) ? src.labels : {};
  out.labels = {
    show: l.show === true,
    position: oneOf(l.position, LABEL_POSITIONS, "auto"),
    series: Array.isArray(l.series) ? l.series.map(String) : null,
    from_col: l.from_col == null ? null : str(l.from_col),
    format: isObj(l.format) ? normNumber(l.format, warnings, "format.labels.format") : null,
    max_points: Math.max(0, Math.round(numOr(l.max_points, 30))),
    color: resolveColor(l.color, null),
    rotate: clamp(numOr(l.rotate, 0), -90, 90),
  };
  if (l.position != null && out.labels.position !== l.position) {
    warnings.push(`format.labels.position "${str(l.position)}" is unknown — using "auto"`);
  }

  const lg = isObj(src.legend) ? src.legend : {};
  out.legend = {
    show: lg.show === true || lg.show === false ? lg.show : "auto",
    position: oneOf(lg.position, LEGEND_POSITIONS, "top"),
    orient: oneOf(lg.orient, ["auto", "horizontal", "vertical"], "auto"),
    max_items: Math.max(1, Math.round(numOr(lg.max_items, 12))),
  };

  const t = isObj(src.title) ? src.title : {};
  out.title = {
    show: t.show !== false,
    subtitle: t.subtitle == null ? null : str(t.subtitle),
    align: oneOf(t.align, ["left", "center", "right"], "left"),
  };

  const tt = isObj(src.tooltip) ? src.tooltip : {};
  out.tooltip = {
    show: tt.show !== false,
    trigger: oneOf(tt.trigger, ["auto", "item", "axis", "none"], "auto"),
    fields: arr(tt.fields).map(String),
  };

  const dz = isObj(src.data_zoom) ? src.data_zoom : {};
  out.data_zoom = {
    show: dz.show === true,
    start: clamp(numOr(dz.start, 0), 0, 100),
    end: clamp(numOr(dz.end, 100), 0, 100),
  };

  const to = isObj(src.totals) ? src.totals : {};
  out.totals = { show: to.show === true, label: typeof to.label === "string" ? to.label : "Total" };

  // fields.measures[].format is shorthand for format.number[col].style.
  for (const m of arr(pathGet(spec, "fields.measures"))) {
    if (!isObj(m) || !m.col || !m.format) continue;
    if (!out.number[m.col] && NUMBER_STYLES.includes(m.format)) {
      out.number[m.col] = normNumber({ style: m.format }, null, `fields.measures.${m.col}`);
    }
  }
  return { format: out, warnings };
}

const PERCENT_NAME = /(^|_)(pct|percent|rate|share|margin)(_|$)/i;
const CURRENCY_NAME = /(^|_)(revenue|sales|amount|cost|price|spend)(_|$)/i;
const DATE_NAME = /(^|_)(date|time|day|month|quarter|week|year)(_|$)/i;

// Zero-config defaults so v1 specs (no format at all) still read well. The
// caller decides whether to use this — applyFormat itself stays a no-op.
export function inferFormat(columns, rows, spec) {
  const cols = arr(columns);
  const sample = arr(rows).slice(0, 50);
  const number = {}, date = {};
  cols.forEach((c, i) => {
    const name = str(c);
    if (DATE_NAME.test(name)) date[name] = { preset: null, pattern: null };
    const numeric = sample.some((r) => typeof (Array.isArray(r) ? r[i] : undefined) === "number");
    if (!numeric) return;
    if (PERCENT_NAME.test(name)) number[name] = { style: "percent", decimals: 1 };
    else if (CURRENCY_NAME.test(name)) number[name] = { style: "currency", currency: "USD", compact: "auto" };
    else number[name] = { style: "number", compact: "auto" };
  });
  const out = {
    number, labels: { show: false }, legend: { show: "auto" }, palette: { mode: "theme" },
  };
  if (Object.keys(date).length) out.date = date;
  const measures = arr(pathGet(spec || {}, "fields.measures"));
  for (const m of measures) {
    if (isObj(m) && m.col && NUMBER_STYLES.includes(m.format)) number[String(m.col)] = { style: m.format };
  }
  return out;
}

// Lookup order: format.number[col] → format.number["*"] → measure shorthand → default.
export function resolveNumberFormat(spec, col) {
  const s = isObj(spec) ? spec : {};
  const map = isObj(pathGet(s, "format.number")) ? s.format.number : {};
  const key = col == null ? null : String(col);
  if (key != null && isObj(map[key])) return normNumber(map[key], null, "number");
  if (isObj(map["*"])) return normNumber(map["*"], null, "number");
  if (key != null) {
    for (const m of arr(pathGet(s, "fields.measures"))) {
      if (isObj(m) && String(m.col) === key && NUMBER_STYLES.includes(m.format)) {
        return normNumber({ style: m.format }, null, "number");
      }
    }
  }
  return { ...NUMBER_DEFAULT };
}

// ── capability probe ────────────────────────────────────────────────────

export function formatCapabilities(type) {
  const t = str(type);
  const flat = FLAT.has(t);
  const axes = !flat && !AXISLESS.has(t);
  return {
    axes,
    legend: !flat && t !== "gauge",
    labels: !flat,
    dualAxis: axes && ["bar", "hbar", "stacked_bar", "combo", "line", "area", "stacked_area",
                       "scatter", "bubble"].includes(t),
    referenceLines: axes,
    palette: !flat,
    conditionalColors: !flat && !["heatmap", "sankey", "gauge"].includes(t),
    annotations: axes,
    dataZoom: axes,
  };
}

export function formatSummary(format) {
  const { format: f } = normalizeFormat(format, {});
  const out = [];
  for (const [col, nf] of Object.entries(f.number)) {
    if (nf.style === "currency") out.push(`$ ${col}`);
    else if (nf.style === "percent") out.push(`% ${col}`);
    else if (nf.style === "bytes") out.push(`bytes ${col}`);
  }
  if (f.labels.show) out.push("labels on");
  for (const r of f.reference_lines) {
    const v = r.mode === "value" ? formatValue(r.value, { compact: true }) : r.mode;
    out.push(`${r.label || "line"} ${v}`);
  }
  if (f.conditional_colors) out.push("conditional colors");
  if (f.legend.show === false) out.push("legend off");
  else if (isObj(format) && isObj(format.legend) && format.legend.position) out.push(`legend ${f.legend.position}`);
  if (Object.values(f.series).some((s) => s.axis === "y2")) out.push("dual axis");
  if (f.data_zoom.show) out.push("zoom");
  return out;
}

// ── ECharts pass ────────────────────────────────────────────────────────

const DUAL_AXIS_WARNING =
  "Dual axis charts invent correlations — consider two panels, or index both measures to 100.";

function plottedCols(columns, rows, spec) {
  try {
    return resolveSpec(arr(columns), arr(rows), isObj(spec) ? spec : {}).yCols;
  } catch {
    return arr(pathGet(spec || {}, "y")).map(String);
  }
}

// option.series[i] -> the measure column it draws (null when it draws none).
function seriesCols(type, seriesList, yCols) {
  if (type === "waterfall") return seriesList.map((_, i) => (i === 0 ? null : yCols[0] ?? null));
  if (type === "histogram") return seriesList.map(() => null);
  if (SINGLE_MEASURE.has(type)) return seriesList.map(() => yCols[0] ?? null);
  return seriesList.map((_, i) => yCols[i] ?? null);
}

function axisKeys(type) {
  // hbar swaps the roles: format.axes.x is always the CATEGORY axis.
  return type === "hbar"
    ? { cat: "yAxis", val: "xAxis" }
    : { cat: "xAxis", val: "yAxis" };
}
// The y2 measure rides the second VALUE axis, which on hbar is x — binding it
// to yAxisIndex would leave it on the shared scale under a cloned category axis.
function valueAxisBinding(type) {
  const { val } = axisKeys(type);
  return val === "xAxis"
    ? { key: "xAxis", index: "xAxisIndex", position: "top" }
    : { key: "yAxis", index: "yAxisIndex", position: "right" };
}
function axisAt(option, key, index = 0) {
  const a = option[key];
  if (Array.isArray(a)) return isObj(a[index]) ? a[index] : null;
  return index === 0 && isObj(a) ? a : null;
}
function ensure(obj, key, dflt) {
  if (!isObj(obj[key])) obj[key] = clone(dflt);
  return obj[key];
}

function labelPosition(type, pos) {
  if (type === "hbar") {
    return { auto: "right", outside: "right", end: "right", top: "top",
             inside: "insideRight", center: "inside", right: "right" }[pos] || "right";
  }
  if (type === "pie" || type === "donut") {
    return { auto: "outside", outside: "outside", inside: "inside", center: "center",
             top: "outside", end: "outside", right: "outside" }[pos] || "outside";
  }
  if (type === "funnel" || type === "treemap" || type === "radar") return "inside";
  return { auto: "top", outside: "top", end: "top", top: "top",
           inside: "inside", center: "inside", right: "right" }[pos] || "top";
}

function itemOf(d) {
  if (isObj(d)) return { ...d };
  return { value: d };
}
function itemValue(d) {
  if (isObj(d)) return d.value;
  return d;
}
function pointValue(p) {
  const v = p && p.value;
  if (Array.isArray(v)) return v.length >= 3 ? v[2] : v[v.length - 1];
  if (isObj(v)) return v.value;
  return v;
}

function matchRule(v, rule) {
  const op = rule.op;
  if (op === "isnull") return v == null || v === "";
  if (op === "notnull") return !(v == null || v === "");
  if (v == null) return op === "ne" || op === "ncontains";
  const a = Number(v), b = Number(rule.value);
  const numeric = Number.isFinite(a) && Number.isFinite(b);
  switch (op) {
    case "eq": return numeric ? a === b : str(v).toLowerCase() === str(rule.value).toLowerCase();
    case "ne": return numeric ? a !== b : str(v).toLowerCase() !== str(rule.value).toLowerCase();
    case "gt": return numeric ? a > b : str(v) > str(rule.value);
    case "gte": return numeric ? a >= b : str(v) >= str(rule.value);
    case "lt": return numeric ? a < b : str(v) < str(rule.value);
    case "lte": return numeric ? a <= b : str(v) <= str(rule.value);
    case "between": {
      const lo = Number(rule.lo), hi = Number(rule.hi);
      if (Number.isFinite(a) && Number.isFinite(lo) && Number.isFinite(hi)) return a >= lo && a <= hi;
      return str(v) >= str(rule.lo) && str(v) <= str(rule.hi);
    }
    case "contains": return str(v).toLowerCase().includes(str(rule.value).toLowerCase());
    case "ncontains": return !str(v).toLowerCase().includes(str(rule.value).toLowerCase());
    case "startswith": return str(v).toLowerCase().startsWith(str(rule.value).toLowerCase());
    case "endswith": return str(v).toLowerCase().endsWith(str(rule.value).toLowerCase());
    default: return false;
  }
}

function statOf(values, mode, percentile) {
  const nums = values.map(Number).filter(Number.isFinite).sort((x, y) => x - y);
  if (!nums.length) return null;
  switch (mode) {
    case "min": return nums[0];
    case "max": return nums[nums.length - 1];
    case "average": return nums.reduce((a, b) => a + b, 0) / nums.length;
    case "median": return quantile(nums, 0.5);
    case "percentile": return quantile(nums, clamp(numOr(percentile, 90), 0, 100) / 100);
    default: return null;
  }
}
function quantile(sorted, p) {
  const i = (sorted.length - 1) * p;
  const lo = Math.floor(i), hi = Math.ceil(i);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
}

export function applyFormat(option, spec, ctx = {}) {
  if (!option || typeof option !== "object" || Array.isArray(option)) {
    return { option: option == null ? null : option, warnings: [] };
  }
  const warnings = [];
  let out;
  try {
    const s = isObj(spec) ? spec : {};
    const c = isObj(ctx) ? ctx : {};
    const type = str(s.type);
    const cap = formatCapabilities(type);
    const columns = arr(c.columns);
    const rows = arr(c.rows).filter(Array.isArray);
    const rawFmt = isObj(s.format) ? s.format : {};
    const { format: f, warnings: nw } = normalizeFormat(rawFmt, { ...c, spec: s });
    warnings.push(...nw);
    const has = (p) => pathGet(rawFmt, p) !== undefined;

    out = clone(option);
    if (!Array.isArray(out.series)) out.series = isObj(out.series) ? [out.series] : [];
    const yCols = plottedCols(columns, rows, s);
    let cols = seriesCols(type, out.series, yCols);

    // series.show === false drops the series outright; do it first so every
    // later index (reference lines, conditional colors) lines up.
    if (has("series")) {
      const keep = out.series.map((_, i) => {
        const cfg = cols[i] ? f.series[cols[i]] : null;
        return !(cfg && cfg.show === false);
      });
      if (keep.some((k) => !k) && keep.some((k) => k)) {
        out.series = out.series.filter((_, i) => keep[i]);
        cols = cols.filter((_, i) => keep[i]);
      }
    }

    const keys = out.series.length && CATEGORY_COLORED.has(type)
      ? categoryKeys(out, columns, rows, s)
      : [];

    if (cap.palette && has("palette")) applyPalette(out, s, f, type, keys);
    if (has("series")) applySeries(out, f, cols, type, cap);
    if (cap.axes) applyAxes(out, f, type, has, warnings);
    if (cap.legend && has("legend")) applyLegend(out, f, yCols);
    if (has("number") || has("date")) applyValueFormats(out, s, f, cols, type);
    if (cap.labels && has("labels")) applyLabels(out, s, f, cols, type, columns, rows);
    if (cap.conditionalColors && has("conditional_colors")) {
      applyConditional(out, f, cols, type, columns, rows, warnings);
    }
    if (cap.referenceLines && has("reference_lines")) applyRefLines(out, f, cols, type, columns, rows);
    if (cap.annotations && has("annotations")) applyAnnotations(out, f, type);
    if (has("tooltip")) applyTooltip(out, f);
    if (cap.dataZoom && has("data_zoom") && f.data_zoom.show) {
      out.dataZoom = [
        { type: "inside", start: f.data_zoom.start, end: f.data_zoom.end },
        { type: "slider", start: f.data_zoom.start, end: f.data_zoom.end,
          height: 16, bottom: 0, borderColor: AXIS_LINE,
          fillerColor: "rgba(57,135,229,0.15)", handleStyle: { color: AXIS_LINE },
          textStyle: { color: INK.muted } },
      ];
      const grid = ensure(out, "grid", {});
      grid.bottom = Math.max(numOr(grid.bottom, 8), 28);
    }
    if (has("title")) applyTitle(out, s, f);
    if (has("totals") && f.totals.show) applyTotals(out, s, f, cols, type);

    // G9 — highlight rides in ctx, not in the spec, and is always honoured.
    applyHighlight(out, c.highlight, columns, rows);

    if (Object.values(f.series).some((cfg) => cfg.axis === "y2" && cfg._keys.includes("axis"))
        && cap.dualAxis) {
      warnings.push(DUAL_AXIS_WARNING);
    }
  } catch (e) {
    return { option, warnings: [...warnings, "format could not be applied: " + str(e && e.message)] };
  }
  return { option: out, warnings };
}

function categoryKeys(option, columns, rows, spec) {
  const xi = columns.indexOf(pathGet(spec, "x"));
  if (xi >= 0 && rows.length) return rows.map((r) => String(r[xi]));
  const data = arr(pathGet(option, "series.0.data"));
  return data.map((d) => (isObj(d) ? String(d.name ?? "") : ""));
}

function applyPalette(out, spec, f, type, keys) {
  const p = f.palette;
  // G5 — heatmap colours through visualMap, never option.color.
  if (type === "heatmap") {
    const ramp = p.sequential || (p.mode !== "theme" ? p.colors : null);
    if (ramp && ramp.length) {
      const vm = Array.isArray(out.visualMap) ? out.visualMap[0] : out.visualMap;
      if (isObj(vm)) ensure(vm, "inRange", {}).color = ramp;
    }
    return;
  }
  if (p.mode !== "theme" && p.colors) out.color = p.colors;
  if (p.sequential && p.sequential.length) {
    const vm = Array.isArray(out.visualMap) ? out.visualMap[0] : out.visualMap;
    if (isObj(vm)) ensure(vm, "inRange", {}).color = p.sequential;
  }
  const pinned = p.mode === "by_key" || p.by_key;
  if (!pinned || !CATEGORY_COLORED.has(type) || !keys.length) return;
  const pal = paletteFor(spec, keys);
  const target = out.series[0];
  if (!isObj(target) || !Array.isArray(target.data)) return;
  target.colorBy = "series";                       // G4 — before per-point itemStyle
  target.data = target.data.map((d, i) => {
    const key = isObj(d) && d.name != null ? String(d.name) : keys[i];
    if (key == null) return d;
    const item = itemOf(d);
    item.itemStyle = { ...(item.itemStyle || {}), color: pal.colorOf(key) };
    return item;
  });
}

function applySeries(out, f, cols, type, cap) {
  const v2 = valueAxisBinding(type);
  let dual = false;
  out.series.forEach((sr, i) => {
    const col = cols[i];
    const cfg = col ? f.series[col] : null;
    if (!isObj(sr) || !cfg) return;
    const set = new Set(cfg._keys);
    // A mark type only swaps between cartesian marks — retyping a pie/funnel/
    // sankey series to "bar" would ask for an axis the chart does not have.
    const cartesian = cap.axes && ["bar", "line", "scatter"].includes(sr.type);
    if (set.has("type") && cfg.type && cartesian) {
      if (cfg.type === "area") { sr.type = "line"; sr.areaStyle = { ...(sr.areaStyle || {}), opacity: 0.18 }; }
      else if (cfg.type === "scatter") sr.type = "scatter";
      else { sr.type = cfg.type; if (cfg.type === "bar") delete sr.areaStyle; }
    }
    if (set.has("color") && cfg.color) {
      sr.itemStyle = { ...(sr.itemStyle || {}), color: cfg.color };
      if (sr.type === "line") sr.lineStyle = { ...(sr.lineStyle || {}), color: cfg.color };
      if (sr.colorBy === "data") sr.colorBy = "series";
    }
    if (set.has("dash")) {
      sr.lineStyle = { ...(sr.lineStyle || {}), type: cfg.dash === "solid" ? "solid" : cfg.dash };
    }
    if (set.has("width")) sr.lineStyle = { ...(sr.lineStyle || {}), width: cfg.width };
    if (set.has("smooth")) sr.smooth = cfg.smooth;
    if (set.has("show_symbol")) sr.showSymbol = cfg.show_symbol;
    if (set.has("stack") && cartesian) sr.stack = cfg.stack || undefined;
    if (set.has("opacity") && cfg.opacity != null) {
      sr.itemStyle = { ...(sr.itemStyle || {}), opacity: cfg.opacity };
      if (sr.lineStyle) sr.lineStyle = { ...sr.lineStyle, opacity: cfg.opacity };
      if (sr.areaStyle) sr.areaStyle = { ...sr.areaStyle, opacity: cfg.opacity };
    }
    if (set.has("axis") && cfg.axis === "y2" && cap.dualAxis) { sr[v2.index] = 1; dual = true; }
  });
  if (!dual) return;
  const base = axisAt(out, v2.key, 0);
  if (!base) return;
  const second = { ...clone(base), position: v2.position, splitLine: { show: false } };
  delete second.name;
  out[v2.key] = [base, second];
}

function applyAxes(out, f, type, has, warnings) {
  const { cat, val } = axisKeys(type);
  const catAx = axisAt(out, cat, 0);
  const valAx = axisAt(out, val, 0);
  const val2 = axisAt(out, val, 1);

  if (catAx && has("axes.x")) patchAxis(catAx, f.axes.x, "category", has, "axes.x", warnings);
  if (valAx && has("axes.y")) patchAxis(valAx, f.axes.y, "value", has, "axes.y", warnings);
  if (val2 && has("axes.y2")) patchAxis(val2, f.axes.y2, "value", has, "axes.y2", warnings);

  if (has("axes.x.title") && catAx && f.axes.x.title != null) {
    const grid = ensure(out, "grid", {});
    if (type === "hbar") grid.left = Math.max(numOr(grid.left, 8), 8);
    else grid.bottom = Math.max(numOr(grid.bottom, 8), 24);
  }
}

function patchAxis(ax, cfg, kind, has, base, warnings) {
  const p = (k) => has(`${base}.${k}`);
  // Scale settings key off what the axis IS, not where it sits: a heatmap's y
  // is a category axis, and retyping it breaks the render outright.
  const isCategory = ax.type === "category" || Array.isArray(ax.data);
  if (p("title") && cfg.title != null) {
    ax.name = cfg.title;
    ax.nameLocation = "middle";
    ax.nameGap = kind === "category" ? 28 : 36;
    ax.nameTextStyle = { ...(ax.nameTextStyle || {}), color: INK.muted };
  }
  if (p("show") && cfg.show === false) {
    ax.axisLabel = { ...(ax.axisLabel || {}), show: false };
    ax.axisLine = { ...(ax.axisLine || {}), show: false };
    ax.splitLine = { ...(ax.splitLine || {}), show: false };
  }
  if (p("label_angle")) ax.axisLabel = { ...(ax.axisLabel || {}), rotate: cfg.label_angle };
  if (p("label_limit") && cfg.label_limit != null) {
    ax.axisLabel = { ...(ax.axisLabel || {}), width: cfg.label_limit, overflow: "truncate" };
  }
  if (p("grid")) ax.splitLine = { ...(ax.splitLine || {}), show: cfg.grid === true, lineStyle: { color: GRID_LINE } };
  if (p("reverse") && cfg.reverse) ax.inverse = true;
  if (!isCategory) {
    if (p("min") && cfg.min != null) ax.min = cfg.min;
    if (p("max") && cfg.max != null) ax.max = cfg.max;
    if (p("zero") && cfg.zero === false) ax.scale = true;
    if (p("tick_count") && cfg.tick_count != null) ax.splitNumber = cfg.tick_count;
  }
  if (p("type") && cfg.type && cfg.type !== "auto" && cfg.type !== ax.type) {
    if (isCategory) {
      warnings.push(`${base}.type "${cfg.type}" is not supported on a category axis — ignored`);
    } else if (cfg.type === "log") {
      ax.type = "log";
      if (ax.min != null && ax.min <= 0) delete ax.min;
    } else if (cfg.type === "value") {
      ax.type = "value";
    } else {
      warnings.push(`${base}.type "${cfg.type}" is not supported by this renderer — ignored`);
    }
  }
}

function applyLegend(out, f, yCols) {
  const lg = f.legend;
  const show = lg.show === "auto" ? yCols.length > 1 : lg.show === true;
  const legend = isObj(out.legend) ? out.legend : {};
  const next = {
    ...legend, show,
    textStyle: { ...(legend.textStyle || {}), color: INK.secondary },
    icon: legend.icon || "circle",
    type: "scroll",
  };
  delete next.top; delete next.bottom; delete next.left; delete next.right;
  const vertical = lg.orient === "vertical" || (lg.orient === "auto" && (lg.position === "left" || lg.position === "right"));
  next.orient = vertical ? "vertical" : "horizontal";
  if (lg.position === "top") next.top = 0;
  else if (lg.position === "bottom") next.bottom = 0;
  else if (lg.position === "left") { next.left = 0; next.top = "middle"; }
  else { next.right = 0; next.top = "middle"; }
  out.legend = next;

  // The grid has to move with it or the legend sits on top of the plot.
  const grid = ensure(out, "grid", {});
  grid.top = show && lg.position === "top" ? 32 : 16;
  grid.bottom = show && lg.position === "bottom" ? 32 : Math.max(numOr(grid.bottom, 8), 8);
  grid.left = show && lg.position === "left" ? 96 : 8;
  grid.right = show && lg.position === "right" ? 112 : 16;
}

function applyValueFormats(out, spec, f, cols, type) {
  const { cat, val } = axisKeys(type);
  const v2 = valueAxisBinding(type);
  const measure = cols.find(Boolean) ?? null;
  const nf = resolveNumberFormat(spec, measure);
  const fmt = makeFormatter(nf);
  const valAx = axisAt(out, val, 0);
  if (valAx && valAx.type !== "category") {
    valAx.axisLabel = { ...(valAx.axisLabel || {}), formatter: (v) => fmt(v) };
  }
  const val2 = axisAt(out, val, 1);
  if (val2 && val2.type !== "category") {
    const nf2 = resolveNumberFormat(spec, cols.find((c, i) => c && out.series[i]?.[v2.index] === 1) ?? measure);
    const fmt2 = makeFormatter(nf2);
    val2.axisLabel = { ...(val2.axisLabel || {}), formatter: (v) => fmt2(v) };
  }
  // Category axis: date preset first, then an explicit number format on x.
  const xcol = str(pathGet(spec, "x"));
  const catAx = axisAt(out, cat, 0);
  if (catAx) {
    if (f.date[xcol]) {
      const dfmt = makeDateFormatter(f.date[xcol]);
      catAx.axisLabel = { ...(catAx.axisLabel || {}), formatter: (v) => dfmt(v) };
    } else if (f.number[xcol]) {
      const xfmt = makeFormatter(f.number[xcol]);
      catAx.axisLabel = { ...(catAx.axisLabel || {}), formatter: (v) => xfmt(v) };
    }
  }
  // G2 — a type that owns tooltip.formatter (waterfall) keeps it.
  const tip = ensure(out, "tooltip", {});
  if (typeof tip.formatter !== "function" && typeof tip.formatter !== "string") {
    tip.valueFormatter = (v) => fmt(v);
  }
  const vm = Array.isArray(out.visualMap) ? out.visualMap[0] : out.visualMap;
  if (isObj(vm)) vm.formatter = (v) => fmt(v);
}

function applyLabels(out, spec, f, cols, type, columns, rows) {
  const l = f.labels;
  if (!l.show) {
    out.series.forEach((sr) => { if (isObj(sr) && isObj(sr.label)) sr.label = { ...sr.label, show: false }; });
    return;
  }
  const wanted = out.series
    .map((sr, i) => ({ sr, i, col: cols[i] }))
    .filter(({ sr, i, col }) => isObj(sr) && !(type === "waterfall" && i === 0)
      && (!l.series || (col && l.series.includes(col))));
  if (!wanted.length) return;
  // G8 — too many marks: drop labels silently, it is a design decision.
  const marks = Math.max(rows.length, 1) * wanted.length;
  if (l.max_points > 0 && marks > l.max_points) return;

  const fromIdx = l.from_col ? columns.indexOf(l.from_col) : -1;
  const position = labelPosition(type, l.position);
  wanted.forEach(({ sr, col }) => {
    const nf = l.format || (fromIdx >= 0
      ? resolveNumberFormat(spec, l.from_col)
      : resolveNumberFormat(spec, col));
    const fmt = makeFormatter(nf);
    const formatter = fromIdx >= 0
      ? (p) => fmt((rows[p && p.dataIndex] || [])[fromIdx])
      : (p) => fmt(pointValue(p));
    sr.label = {
      ...(sr.label || {}), show: true, position,
      color: l.color || INK.secondary,
      rotate: l.rotate || undefined,
      formatter,
    };
    if (type === "pie" || type === "donut") {
      sr.labelLine = { ...(sr.labelLine || {}), show: position === "outside" };
    }
  });
}

function applyConditional(out, f, cols, type, columns, rows) {
  const cc = f.conditional_colors;
  if (!cc || !cc.target) return;
  let si = cols.indexOf(cc.target);
  if (type === "waterfall") si = out.series.length > 1 ? 1 : -1;
  if (si < 0) si = out.series.findIndex(isObj);
  const sr = out.series[si];
  if (!isObj(sr) || !Array.isArray(sr.data)) return;
  const byCol = cc.by || cc.target;
  const bi = columns.indexOf(byCol);

  const valuesOf = (j) => {
    if (bi >= 0 && rows[j]) return rows[j][bi];
    return itemValue(sr.data[j]);
  };
  let lo = cc.scale.min, hi = cc.scale.max;
  if (cc.mode === "scale" && (lo == null || hi == null)) {
    const nums = sr.data.map((_, j) => Number(valuesOf(j))).filter(Number.isFinite);
    if (nums.length) { lo = lo == null ? Math.min(...nums) : lo; hi = hi == null ? Math.max(...nums) : hi; }
  }
  const ramp = cc.scale.ramp && cc.scale.ramp.length
    ? cc.scale.ramp
    : cc.scale.diverging ? [BAD, INK.muted, GOOD] : SEQ;

  sr.colorBy = "series";                            // G4
  sr.data = sr.data.map((d, j) => {
    const v = valuesOf(j);
    let color = null;
    if (cc.mode === "scale") {
      const n = Number(v);
      const span = (hi ?? 1) - (lo ?? 0);
      color = rampColor(span === 0 ? 0.5 : (n - (lo ?? 0)) / span, ramp);
    } else {
      const hit = cc.rules.find((r) => matchRule(v, r));
      color = hit ? hit.color : cc.default;
    }
    if (!color) return d;
    const item = itemOf(d);
    item.itemStyle = { ...(item.itemStyle || {}), color };
    return item;
  });
}

function applyRefLines(out, f, cols, type, columns, rows) {
  if (!f.reference_lines.length) return;
  const { cat, val } = axisKeys(type);
  const v2 = valueAxisBinding(type);
  for (const rl of f.reference_lines) {
    const wantIdx = rl.axis === "y2" ? 1 : 0;
    let si = out.series.findIndex((sr, i) =>
      isObj(sr) && (numOr(sr[v2.index], 0) === wantIdx) && !(type === "waterfall" && i === 0));
    if (si < 0) si = type === "waterfall" && out.series.length > 1 ? 1 : out.series.findIndex(isObj);
    const sr = out.series[si];
    if (!isObj(sr)) continue;

    let value = rl.value;
    if (rl.mode !== "value") {
      const col = rl.of || cols[si] || cols.find(Boolean);
      const ci = col ? columns.indexOf(col) : -1;
      const vals = ci >= 0 ? rows.map((r) => r[ci]) : sr.data ? arr(sr.data).map(itemValue) : [];
      value = statOf(vals, rl.mode, rl.percentile);
    }
    if (value == null || !Number.isFinite(Number(value))) continue;
    const axisKey = rl.axis === "x" ? cat : val;
    const color = rl.color || INK.muted;

    if (rl.to != null) {
      const area = ensure(sr, "markArea", {});
      if (!Array.isArray(area.data)) area.data = [];
      area.data.push([
        { [axisKey]: Math.min(Number(value), rl.to), itemStyle: { color, opacity: rl.opacity },
          label: rl.label ? { show: true, formatter: rl.label, color: INK.muted, position: "insideTop" } : undefined },
        { [axisKey]: Math.max(Number(value), rl.to) },
      ]);
      continue;
    }
    // G3 — append, never replace an existing markLine.
    const ml = ensure(sr, "markLine", {});
    if (!Array.isArray(ml.data)) ml.data = [];
    ml.symbol = ml.symbol || "none";
    ml.data.push({
      [axisKey]: Number(value),
      lineStyle: { type: rl.dash === "solid" ? "solid" : rl.dash, color, width: rl.width },
      label: {
        show: !!rl.label, formatter: rl.label || "", color: INK.muted,
        position: rl.label_position === "start" ? "insideStartTop"
          : rl.label_position === "middle" ? "insideMiddleTop" : "insideEndTop",
      },
    });
  }
}

function applyAnnotations(out, f, type) {
  if (!f.annotations.length) return;
  const si = out.series.findIndex(isObj);
  const sr = out.series[si];
  if (!isObj(sr)) return;
  const mp = ensure(sr, "markPoint", {});
  if (!Array.isArray(mp.data)) mp.data = [];
  for (const a of f.annotations) {
    if (a.x == null && a.y == null) continue;
    mp.data.push({
      coord: type === "hbar" ? [a.y, a.x] : [a.x, a.y],
      value: a.text,
      symbol: a.arrow ? "pin" : "circle",
      symbolSize: a.arrow ? 36 : 8,
      itemStyle: { color: a.color || INK.muted },
      label: { show: !!a.text, formatter: a.text, color: INK.primary, position: a.position },
    });
  }
}

function applyTooltip(out, f) {
  const tip = ensure(out, "tooltip", {});
  if (!f.tooltip.show || f.tooltip.trigger === "none") { tip.show = false; return; }
  tip.show = true;
  if (f.tooltip.trigger !== "auto") tip.trigger = f.tooltip.trigger;
}

function applyTitle(out, spec, f) {
  if (f.title.show === false) { out.title = { ...(isObj(out.title) ? out.title : {}), show: false }; return; }
  if (!f.title.subtitle) return;
  // The chart title itself lives in the toolbar; only the subtitle is drawn here.
  out.title = {
    ...(isObj(out.title) ? out.title : {}),
    show: true, text: "", subtext: f.title.subtitle,
    left: f.title.align, top: 0,
    subtextStyle: { color: INK.muted, fontSize: 11 },
  };
  const grid = ensure(out, "grid", {});
  grid.top = Math.max(numOr(grid.top, 16), 34);
}

function applyTotals(out, spec, f, cols, type) {
  if (type !== "stacked_bar" && type !== "stacked_area") return;
  const stacks = out.series.filter((sr) => isObj(sr) && sr.stack);
  if (!stacks.length) return;
  const len = Math.max(...stacks.map((sr) => arr(sr.data).length));
  const totals = Array.from({ length: len }, (_, i) =>
    stacks.reduce((a, sr) => a + (Number(itemValue(arr(sr.data)[i])) || 0), 0));
  const nf = resolveNumberFormat(spec, cols.find(Boolean));
  const fmt = makeFormatter(nf);
  out.series.push({
    type: "bar", stack: stacks[0].stack, name: f.totals.label,
    data: totals.map(() => 0), silent: true, tooltip: { show: false },
    itemStyle: { color: "transparent" },
    label: { show: true, position: "top", color: INK.secondary,
             formatter: (p) => fmt(totals[p && p.dataIndex]) },
  });
}

// G9 — dim non-selected marks; rows are never removed, so the scale holds.
function applyHighlight(out, highlight, columns, rows) {
  if (!isObj(highlight) || !highlight.col) return;
  const values = arr(highlight.values ?? (highlight.value != null ? [highlight.value] : []));
  if (!values.length) return;
  const keep = new Set(values.map(String));
  const hi = columns.indexOf(highlight.col);
  const DIM = 0.22;

  out.series.forEach((sr) => {
    if (!isObj(sr) || !Array.isArray(sr.data)) return;
    const isLineish = sr.type === "line";
    let anyMatch = false;
    sr.data = sr.data.map((d, j) => {
      const key = isObj(d) && d.name != null
        ? String(d.name)
        : hi >= 0 && rows[j] ? String(rows[j][hi]) : null;
      const match = key != null && keep.has(key);
      if (match) { anyMatch = true; return d; }
      if (key == null) return d;
      const item = itemOf(d);
      item.itemStyle = { ...(item.itemStyle || {}), opacity: DIM };
      return item;
    });
    if (isLineish && !anyMatch) {
      sr.lineStyle = { ...(sr.lineStyle || {}), opacity: DIM };
      if (sr.areaStyle) sr.areaStyle = { ...sr.areaStyle, opacity: DIM * 0.5 };
    }
  });
}

// ── Plotly pass ─────────────────────────────────────────────────────────

export function applyFormatPlotly(figure, spec, ctx = {}) {
  if (!isObj(figure) || !Array.isArray(figure.data)) {
    return { figure: figure == null ? null : figure, warnings: [] };
  }
  const warnings = [];
  let out;
  try {
    const s = isObj(spec) ? spec : {};
    const c = isObj(ctx) ? ctx : {};
    const type = str(s.type);
    const cap = formatCapabilities(type);
    const columns = arr(c.columns);
    const rows = arr(c.rows).filter(Array.isArray);
    const rawFmt = isObj(s.format) ? s.format : {};
    const { format: f, warnings: nw } = normalizeFormat(rawFmt, { ...c, spec: s });
    warnings.push(...nw);
    const has = (p) => pathGet(rawFmt, p) !== undefined;

    out = { data: clone(figure.data), layout: clone(figure.layout) || {} };
    const yCols = plottedCols(columns, rows, s);
    const cols = seriesCols(type, out.data, yCols);
    const layout = out.layout;
    const horiz = type === "hbar";
    const valAxisKey = horiz ? "xaxis" : "yaxis";
    const catAxisKey = horiz ? "yaxis" : "xaxis";

    if (has("number") || has("date")) {
      const nf = resolveNumberFormat(s, cols.find(Boolean));
      ensure(layout, valAxisKey, {}).tickformat = toD3Format(nf);
      const xcol = str(pathGet(s, "x"));
      if (f.number[xcol]) ensure(layout, catAxisKey, {}).tickformat = toD3Format(f.number[xcol]);
    }
    if (cap.axes && has("axes")) {
      const ax = ensure(layout, catAxisKey, {}), ay = ensure(layout, valAxisKey, {});
      if (has("axes.x.title") && f.axes.x.title != null) ax.title = { text: f.axes.x.title };
      if (has("axes.y.title") && f.axes.y.title != null) ay.title = { text: f.axes.y.title };
      if (has("axes.x.show") && f.axes.x.show === false) ax.visible = false;
      if (has("axes.y.show") && f.axes.y.show === false) ay.visible = false;
      if (has("axes.x.label_angle")) ax.tickangle = f.axes.x.label_angle;
      if (has("axes.x.grid")) ax.showgrid = f.axes.x.grid === true;
      if (has("axes.y.grid")) ay.showgrid = f.axes.y.grid === true;
      if (has("axes.x.reverse") && f.axes.x.reverse) ax.autorange = "reversed";
      if (f.axes.y.min != null || f.axes.y.max != null) {
        ay.range = [f.axes.y.min ?? undefined, f.axes.y.max ?? undefined];
      }
      if (has("axes.y.type") && f.axes.y.type === "log") ay.type = "log";
      if (has("axes.x.type") && f.axes.x.type === "log") ax.type = "log";
    }
    if (cap.legend && has("legend")) {
      const show = f.legend.show === "auto" ? yCols.length > 1 : f.legend.show === true;
      layout.showlegend = show;
      const p = f.legend.position;
      layout.legend = {
        ...(isObj(layout.legend) ? layout.legend : {}),
        orientation: p === "left" || p === "right" || f.legend.orient === "vertical" ? "v" : "h",
        x: p === "left" ? -0.12 : p === "right" ? 1.02 : 0,
        y: p === "bottom" ? -0.2 : p === "top" ? 1.15 : 1,
        font: { color: INK.secondary },
      };
    }
    if (cap.palette && has("palette")) {
      const pal = paletteFor(s, categoryKeysFromRows(columns, rows, s));
      if (f.palette.mode !== "theme" && f.palette.colors) layout.colorway = f.palette.colors;
      if ((f.palette.mode === "by_key" || f.palette.by_key) && CATEGORY_COLORED.has(type)) {
        const keys = categoryKeysFromRows(columns, rows, s);
        if (keys.length && out.data[0]) {
          const colorsByKey = keys.map((k) => pal.colorOf(k));
          const tr = out.data[0];
          if (type === "pie" || type === "donut") ensure(tr, "marker", {}).colors = colorsByKey;
          else ensure(tr, "marker", {}).color = colorsByKey;
        }
      }
      if (f.palette.sequential && out.data[0] && out.data[0].type === "heatmap") {
        const seq = f.palette.sequential;
        out.data[0].colorscale = seq.map((c, i) => [i / Math.max(seq.length - 1, 1), c]);
      }
    }
    if (has("series")) {
      let dual = false;
      out.data.forEach((tr, i) => {
        const cfg = cols[i] ? f.series[cols[i]] : null;
        if (!isObj(tr) || !cfg) return;
        const set = new Set(cfg._keys);
        if (set.has("show") && cfg.show === false) tr.visible = false;
        if (set.has("type") && cfg.type) {
          if (cfg.type === "bar") tr.type = "bar";
          else { tr.type = "scatter"; tr.mode = cfg.type === "scatter" ? "markers" : "lines"; }
          if (cfg.type === "area") tr.fill = "tozeroy";
        }
        if (set.has("color") && cfg.color) {
          ensure(tr, "line", {}).color = cfg.color;
          ensure(tr, "marker", {}).color = cfg.color;
        }
        if (set.has("dash")) ensure(tr, "line", {}).dash = cfg.dash === "solid" ? "solid" : cfg.dash;
        if (set.has("width")) ensure(tr, "line", {}).width = cfg.width;
        if (set.has("smooth")) ensure(tr, "line", {}).shape = cfg.smooth ? "spline" : "linear";
        if (set.has("stack")) tr.stackgroup = cfg.stack || undefined;
        if (set.has("opacity") && cfg.opacity != null) tr.opacity = cfg.opacity;
        if (set.has("axis") && cfg.axis === "y2" && cap.dualAxis) {
          if (horiz) tr.xaxis = "x2"; else tr.yaxis = "y2";
          dual = true;
        }
      });
      if (dual) {
        // The overlay goes on the VALUE axis, which on hbar is x.
        const key = horiz ? "xaxis2" : "yaxis2";
        layout[key] = {
          ...(isObj(layout[key]) ? layout[key] : {}),
          overlaying: horiz ? "x" : "y", side: horiz ? "top" : "right",
          gridcolor: "transparent",
          tickfont: { color: INK.muted },
          title: f.axes.y2.title ? { text: f.axes.y2.title } : undefined,
        };
        warnings.push(DUAL_AXIS_WARNING);
      }
    }
    if (cap.labels && has("labels") && f.labels.show) {
      const fromIdx = f.labels.from_col ? columns.indexOf(f.labels.from_col) : -1;
      const marks = Math.max(rows.length, 1) * out.data.length;
      if (!(f.labels.max_points > 0 && marks > f.labels.max_points)) {
        out.data.forEach((tr, i) => {
          if (!isObj(tr)) return;
          const nf = f.labels.format || resolveNumberFormat(s, fromIdx >= 0 ? f.labels.from_col : cols[i]);
          const fmt = makeFormatter(nf);
          const vals = fromIdx >= 0
            ? rows.map((r) => r[fromIdx])
            : arr(horiz ? tr.x : tr.y);
          tr.text = vals.map((v) => fmt(v));
          tr.textposition = f.labels.position === "inside" ? "inside" : "outside";
          if (tr.type === "scatter") tr.mode = str(tr.mode).includes("text") ? tr.mode : (tr.mode || "lines") + "+text";
          tr.textfont = { color: f.labels.color || INK.secondary };
        });
      }
    }
    if (cap.conditionalColors && has("conditional_colors") && f.conditional_colors) {
      const cc = f.conditional_colors;
      const ti = Math.max(cols.indexOf(cc.target), 0);
      const tr = out.data[ti];
      const bi = columns.indexOf(cc.by || cc.target);
      if (isObj(tr) && rows.length) {
        const ramp = cc.scale.ramp && cc.scale.ramp.length ? cc.scale.ramp : SEQ;
        const nums = bi >= 0 ? rows.map((r) => Number(r[bi])).filter(Number.isFinite) : [];
        const lo = cc.scale.min ?? (nums.length ? Math.min(...nums) : 0);
        const hi = cc.scale.max ?? (nums.length ? Math.max(...nums) : 1);
        ensure(tr, "marker", {}).color = rows.map((r) => {
          const v = bi >= 0 ? r[bi] : null;
          if (cc.mode === "scale") return rampColor(hi === lo ? 0.5 : (Number(v) - lo) / (hi - lo), ramp);
          const hit = cc.rules.find((rule) => matchRule(v, rule));
          return hit ? hit.color : (cc.default || SERIES[0]);
        });
      }
    }
    if (cap.referenceLines && has("reference_lines")) {
      const shapes = arr(layout.shapes).slice();
      const anns = arr(layout.annotations).slice();
      for (const rl of f.reference_lines) {
        let v = rl.value;
        if (rl.mode !== "value") {
          const col = rl.of || cols.find(Boolean);
          const ci = col ? columns.indexOf(col) : -1;
          v = statOf(ci >= 0 ? rows.map((r) => r[ci]) : [], rl.mode, rl.percentile);
        }
        if (v == null || !Number.isFinite(Number(v))) continue;
        const across = horiz || rl.axis === "x" ? "x" : "y";
        const base = { line: { color: rl.color || INK.muted, width: rl.width, dash: rl.dash },
                       xref: "paper", yref: "paper" };
        if (rl.to != null) {
          shapes.push(across === "y"
            ? { ...base, type: "rect", xref: "paper", x0: 0, x1: 1, yref: "y", y0: Math.min(v, rl.to), y1: Math.max(v, rl.to), fillcolor: rl.color || INK.muted, opacity: rl.opacity, line: { width: 0 } }
            : { ...base, type: "rect", yref: "paper", y0: 0, y1: 1, xref: "x", x0: Math.min(v, rl.to), x1: Math.max(v, rl.to), fillcolor: rl.color || INK.muted, opacity: rl.opacity, line: { width: 0 } });
        } else {
          shapes.push(across === "y"
            ? { ...base, type: "line", x0: 0, x1: 1, yref: "y", y0: v, y1: v }
            : { ...base, type: "line", y0: 0, y1: 1, xref: "x", x0: v, x1: v });
        }
        if (rl.label) {
          anns.push(across === "y"
            ? { xref: "paper", x: 1, yref: "y", y: v, text: rl.label, showarrow: false,
                font: { color: INK.muted, size: 11 }, xanchor: "right", yanchor: "bottom" }
            : { yref: "paper", y: 1, xref: "x", x: v, text: rl.label, showarrow: false,
                font: { color: INK.muted, size: 11 } });
        }
      }
      if (shapes.length) layout.shapes = shapes;
      if (anns.length) layout.annotations = anns;
    }
    if (cap.annotations && has("annotations") && f.annotations.length) {
      layout.annotations = arr(layout.annotations).concat(f.annotations.map((a) => ({
        x: a.x, y: a.y, text: a.text, showarrow: a.arrow !== false,
        arrowcolor: a.color || INK.muted, font: { color: a.color || INK.primary, size: 11 },
      })));
    }
    if (cap.dataZoom && has("data_zoom") && f.data_zoom.show) {
      ensure(layout, catAxisKey, {}).rangeslider = { visible: true, thickness: 0.08 };
    }
    if (has("title") && f.title.subtitle) {
      layout.title = { text: f.title.subtitle, font: { color: INK.muted, size: 12 }, x: 0 };
    }
    if (has("tooltip") && (!f.tooltip.show || f.tooltip.trigger === "none")) layout.hovermode = false;

    applyHighlightPlotly(out, c.highlight, columns, rows, horiz);
  } catch (e) {
    return { figure, warnings: [...warnings, "format could not be applied: " + str(e && e.message)] };
  }
  return { figure: out, warnings };
}

function categoryKeysFromRows(columns, rows, spec) {
  const xi = arr(columns).indexOf(pathGet(spec || {}, "x"));
  return xi >= 0 ? arr(rows).map((r) => String(r[xi])) : [];
}

function applyHighlightPlotly(out, highlight, columns, rows, horiz) {
  if (!isObj(highlight) || !highlight.col) return;
  const values = arr(highlight.values ?? (highlight.value != null ? [highlight.value] : []));
  if (!values.length) return;
  const hi = arr(columns).indexOf(highlight.col);
  if (hi < 0 || !rows.length) return;
  const keep = new Set(values.map(String));
  const opacity = rows.map((r) => (keep.has(String(r[hi])) ? 1 : 0.22));
  out.data.forEach((tr) => {
    if (!isObj(tr)) return;
    ensure(tr, "marker", {}).opacity = opacity;
  });
}

// ── Vega-Lite pass ──────────────────────────────────────────────────────

export function applyFormatVega(vlSpec, spec, ctx = {}) {
  if (!isObj(vlSpec)) return { spec: vlSpec == null ? null : vlSpec, warnings: [] };
  const warnings = [];
  let out;
  try {
    const s = isObj(spec) ? spec : {};
    const c = isObj(ctx) ? ctx : {};
    const type = str(s.type);
    const cap = formatCapabilities(type);
    const columns = arr(c.columns);
    const rows = arr(c.rows).filter(Array.isArray);
    const rawFmt = isObj(s.format) ? s.format : {};
    const { format: f, warnings: nw } = normalizeFormat(rawFmt, { ...c, spec: s });
    warnings.push(...nw);
    const has = (p) => pathGet(rawFmt, p) !== undefined;

    out = clone(vlSpec);
    const enc = isObj(out.encoding) ? out.encoding : null;
    const cfg = ensure(out, "config", {});
    const horiz = type === "hbar";
    const valKey = horiz ? "x" : "y";
    const catKey = horiz ? "y" : "x";
    const yCols = plottedCols(columns, rows, s);
    const measure = yCols[0] ?? null;

    if (enc && (has("number") || has("date"))) {
      const nf = resolveNumberFormat(s, measure);
      if (isObj(enc[valKey])) ensure(enc[valKey], "axis", {}).format = toD3Format(nf);
      const xcol = str(pathGet(s, "x"));
      if (isObj(enc[catKey]) && f.number[xcol]) {
        ensure(enc[catKey], "axis", {}).format = toD3Format(f.number[xcol]);
      }
    }
    if (enc && cap.axes && has("axes")) {
      if (isObj(enc[catKey])) {
        const ax = ensure(enc[catKey], "axis", {});
        if (has("axes.x.title") && f.axes.x.title != null) ax.title = f.axes.x.title || null;
        if (has("axes.x.label_angle")) ax.labelAngle = f.axes.x.label_angle;
        if (has("axes.x.label_limit") && f.axes.x.label_limit != null) ax.labelLimit = f.axes.x.label_limit;
        if (has("axes.x.grid")) ax.grid = f.axes.x.grid === true;
        if (has("axes.x.show") && f.axes.x.show === false) enc[catKey].axis = null;
      }
      if (isObj(enc[valKey])) {
        const ay = ensure(enc[valKey], "axis", {});
        if (has("axes.y.title") && f.axes.y.title != null) ay.title = f.axes.y.title || null;
        if (has("axes.y.grid")) ay.grid = f.axes.y.grid === true;
        if (has("axes.y.show") && f.axes.y.show === false) enc[valKey].axis = null;
        if (f.axes.y.min != null || f.axes.y.max != null) {
          const sc = ensure(enc[valKey], "scale", {});
          sc.domain = [f.axes.y.min ?? 0, f.axes.y.max ?? 0];
          if (f.axes.y.min == null || f.axes.y.max == null) delete sc.domain;
          if (f.axes.y.min != null && f.axes.y.max != null) sc.domain = [f.axes.y.min, f.axes.y.max];
        }
        if (has("axes.y.type") && f.axes.y.type === "log") ensure(enc[valKey], "scale", {}).type = "log";
        if (has("axes.y.zero") && f.axes.y.zero === false) ensure(enc[valKey], "scale", {}).zero = false;
      }
    }
    if (cap.legend && has("legend")) {
      const show = f.legend.show === "auto" ? yCols.length > 1 : f.legend.show === true;
      cfg.legend = { ...(isObj(cfg.legend) ? cfg.legend : {}), orient: f.legend.position };
      if (!show && enc && isObj(enc.color)) enc.color.legend = null;
    }
    if (cap.palette && has("palette") && f.palette.colors) {
      cfg.range = { ...(isObj(cfg.range) ? cfg.range : {}), category: f.palette.colors };
      if (enc && isObj(enc.color)) ensure(enc.color, "scale", {}).range = f.palette.colors;
    }
    if (cap.palette && f.palette.sequential && enc && isObj(enc.color)) {
      ensure(enc.color, "scale", {}).range = f.palette.sequential;
    }
    if (cap.conditionalColors && has("conditional_colors") && f.conditional_colors && enc) {
      const cc = f.conditional_colors;
      const field = cc.by || cc.target;
      const rule = cc.rules[0];
      if (field && rule && cc.mode === "rules") {
        enc.color = {
          condition: { test: vegaTest(field, rule), value: rule.color },
          value: cc.default || SERIES[0],
        };
      }
    }
    if (cap.labels && has("labels") && f.labels.show && enc) {
      const marks = Math.max(rows.length, 1);
      if (!(f.labels.max_points > 0 && marks > f.labels.max_points)) {
        const field = f.labels.from_col || measure;
        if (field) {
          const nf = f.labels.format || resolveNumberFormat(s, field);
          const textEnc = { ...clone(enc), text: { field, type: "quantitative", format: toD3Format(nf) } };
          delete textEnc.color;
          const markSpec = out.mark;
          out.layer = arr(out.layer).length
            ? out.layer.concat([{ mark: { type: "text", dy: horiz ? 0 : -8, dx: horiz ? 10 : 0,
                                          color: f.labels.color || INK.secondary }, encoding: textEnc }])
            : [{ mark: markSpec, encoding: clone(enc) },
               { mark: { type: "text", dy: horiz ? 0 : -8, dx: horiz ? 10 : 0,
                         color: f.labels.color || INK.secondary }, encoding: textEnc }];
          delete out.mark;
          delete out.encoding;
        }
      }
    }
    if (cap.referenceLines && has("reference_lines") && f.reference_lines.length) {
      const layers = arr(out.layer).length ? out.layer.slice() : [{ mark: out.mark, encoding: clone(out.encoding) }];
      for (const rl of f.reference_lines) {
        let v = rl.value;
        if (rl.mode !== "value") {
          const col = rl.of || measure;
          const ci = col ? columns.indexOf(col) : -1;
          v = statOf(ci >= 0 ? rows.map((r) => r[ci]) : [], rl.mode, rl.percentile);
        }
        if (v == null || !Number.isFinite(Number(v))) continue;
        layers.push({
          mark: { type: "rule", color: rl.color || INK.muted, strokeWidth: rl.width,
                  strokeDash: rl.dash === "dotted" ? [2, 3] : rl.dash === "dashed" ? [6, 4] : undefined },
          encoding: { [valKey]: { datum: Number(v), type: "quantitative" } },
        });
      }
      out.layer = layers;
      delete out.mark;
      delete out.encoding;
    }
    if (has("title") && f.title.subtitle) out.title = { text: "", subtitle: f.title.subtitle, anchor: "start" };

    // G9 — highlight via an opacity condition; rows stay in the data.
    if (isObj(c.highlight) && c.highlight.col) {
      const values = arr(c.highlight.values ?? (c.highlight.value != null ? [c.highlight.value] : []));
      if (values.length) {
        const test = values.map((v) => `datum[${JSON.stringify(String(c.highlight.col))}] === ${JSON.stringify(String(v))}`).join(" || ");
        const target = isObj(out.encoding) ? out.encoding : arr(out.layer)[0]?.encoding;
        if (isObj(target)) target.opacity = { condition: { test, value: 1 }, value: 0.22 };
      }
    }
  } catch (e) {
    return { spec: vlSpec, warnings: [...warnings, "format could not be applied: " + str(e && e.message)] };
  }
  return { spec: out, warnings };
}

function vegaTest(field, rule) {
  const d = `datum[${JSON.stringify(String(field))}]`;
  const v = JSON.stringify(rule.value ?? null);
  switch (rule.op) {
    case "eq": return `${d} === ${v}`;
    case "ne": return `${d} !== ${v}`;
    case "gt": return `${d} > ${v}`;
    case "gte": return `${d} >= ${v}`;
    case "lt": return `${d} < ${v}`;
    case "lte": return `${d} <= ${v}`;
    case "between": return `${d} >= ${JSON.stringify(rule.lo ?? null)} && ${d} <= ${JSON.stringify(rule.hi ?? null)}`;
    case "isnull": return `${d} == null`;
    case "notnull": return `${d} != null`;
    case "contains": return `indexof(lower(toString(${d})), lower(${v})) >= 0`;
    case "ncontains": return `indexof(lower(toString(${d})), lower(${v})) < 0`;
    case "startswith": return `indexof(lower(toString(${d})), lower(${v})) === 0`;
    case "endswith": return `endswith(lower(toString(${d})), lower(${v}))`;
    default: return "false";
  }
}
