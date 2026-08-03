// FilterBar — dashboard slicers (global filters) + cross-filter pills, in ONE
// row above the grid (never floating over charts). Every control emits the same
// Filter shape the server evaluates; there is no predicate logic on the client.
// Props:
//   fields   Field[]        — from POST /dashboards/{id}/data → fields. Computed from
//                             UNFILTERED base rows, so options never vanish on select.
//   value    FilterContext  — {slicers: Filter[], cross: Selection[]}
//   onChange (next: FilterContext) => void
//   onClear  () => void     — drop every slicer and cross-selection
//   pending  bool           — a refetch is in flight
//   compact  bool           — denser row for narrow layouts
import { useEffect, useRef, useState } from "react";

export const EMPTY_CONTEXT = { slicers: [], cross: [] };

// ≤ this many distinct values renders as a chip list; more gets a search box.
const CHIP_LIMIT = 12;
const OP_WORDS = {
  eq: "is", ne: "is not", in: "is", nin: "is not", gt: ">", gte: "≥", lt: "<", lte: "≤",
  between: "between", contains: "contains", ncontains: "excludes",
  startswith: "starts with", endswith: "ends with", isnull: "is blank",
  notnull: "is set", last_n: "last",
};

function isBlank(v) {
  return v === null || v === undefined || v === "";
}

function fmtVal(v) {
  if (isBlank(v)) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)));
  return String(v);
}

// Which control a field gets. Unknown dtype degrades to a value list.
function slicerKind(field) {
  const dtype = field?.dtype;
  const role = field?.role;
  if (role === "id") return "text";
  if (dtype === "number" && role !== "category") return "range";
  if (dtype === "date" || role === "time") return "range";
  if (dtype === "string" || dtype === "bool" || !dtype) {
    return Array.isArray(field?.values) && field.values.length ? "list" : "text";
  }
  return "list";
}

// A UI slicer draft -> the wire Filter. Empty draft ⇒ null (no predicate).
export function slicerToFilter(slicer) {
  if (!slicer || isBlank(slicer.col)) return null;
  const col = String(slicer.col);
  const kind = slicer.kind || slicerKind(slicer);
  const numeric = slicer.dtype === "number";
  const cast = (v) => (numeric && v !== "" && !isNaN(Number(v)) ? Number(v) : v);

  if (kind === "range") {
    const lo = isBlank(slicer.lo) ? null : cast(slicer.lo);
    const hi = isBlank(slicer.hi) ? null : cast(slicer.hi);
    if (lo === null && hi === null) return null;
    if (lo !== null && hi !== null) return { col, op: "between", lo, hi, origin: "slicer" };
    return lo !== null
      ? { col, op: "gte", value: lo, origin: "slicer" }
      : { col, op: "lte", value: hi, origin: "slicer" };
  }
  if (kind === "text") {
    const text = String(slicer.text ?? "").trim();
    return text ? { col, op: "contains", value: text, origin: "slicer" } : null;
  }
  const values = (Array.isArray(slicer.values) ? slicer.values : []).filter((v) => !isBlank(v));
  return values.length ? { col, op: "in", values, origin: "slicer" } : null;
}

// "Region is East or West". Total: an unknown op falls back to its own name.
export function filterLabel(filter, fields) {
  if (!filter || isBlank(filter.col)) return "";
  const f = (Array.isArray(fields) ? fields : []).find((x) => x && x.col === filter.col);
  const name = f?.label || filter.col;
  const op = filter.op || "eq";
  if (op === "isnull") return `${name} is blank`;
  if (op === "notnull") return `${name} is set`;
  if (op === "between") return `${name} ${fmtVal(filter.lo)} – ${fmtVal(filter.hi)}`;
  if (op === "last_n") return `${name} last ${fmtVal(filter.value)} ${filter.unit || "day"}s`;
  if (op === "in" || op === "nin") {
    const vs = (Array.isArray(filter.values) ? filter.values : []).map(fmtVal);
    if (!vs.length) return name;
    const joined = vs.length > 3 ? `${vs.slice(0, 3).join(", ")} +${vs.length - 3}` : vs.join(" or ");
    return `${name} ${OP_WORDS[op]} ${joined}`;
  }
  return `${name} ${OP_WORDS[op] || op} ${fmtVal(filter.value)}`;
}

// A cross-filter Selection reads like a filter in the pill row.
function crossLabel(sel, fields) {
  if (!sel || isBlank(sel.field)) return "";
  const f = (Array.isArray(fields) ? fields : []).find((x) => x && x.col === sel.field);
  const name = f?.label || sel.field;
  if (Array.isArray(sel.range) && sel.range.length === 2) {
    return `${name} ${fmtVal(sel.range[0])} – ${fmtVal(sel.range[1])}`;
  }
  const vs = Array.isArray(sel.values) && sel.values.length ? sel.values : [sel.value];
  const shown = vs.map(fmtVal);
  const joined = shown.length > 2 ? `${shown.slice(0, 2).join(", ")} +${shown.length - 2}` : shown.join(" or ");
  return `${name} = ${joined}`;
}

export default function FilterBar({ fields, value, onChange, onClear, pending, compact }) {
  const ctx = { slicers: [], cross: [], ...(value && typeof value === "object" ? value : {}) };
  const slicers = Array.isArray(ctx.slicers) ? ctx.slicers.filter(Boolean) : [];
  const cross = Array.isArray(ctx.cross) ? ctx.cross.filter(Boolean) : [];
  const list = (Array.isArray(fields) ? fields : []).filter((f) => f && f.col);
  // Measures are aggregated away in most tiles — slice on dimensions and time.
  const slicerFields = list.filter((f) => f.kind !== "measure");

  const [open, setOpen] = useState(null);
  const barRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const away = (e) => {
      if (barRef.current && !barRef.current.contains(e.target)) setOpen(null);
    };
    window.addEventListener("mousedown", away);
    return () => window.removeEventListener("mousedown", away);
  }, [open]);

  function setSlicer(col, filter) {
    const rest = slicers.filter((f) => f.col !== col);
    onChange?.({ ...ctx, slicers: filter ? [...rest, filter] : rest });
  }

  function removeCross(i) {
    onChange?.({ ...ctx, cross: cross.filter((_, j) => j !== i) });
  }

  const active = slicers.length + cross.length;

  return (
    <div className={"filter-bar" + (compact ? " filter-bar-compact" : "")} ref={barRef}>
      <span className="meta">Filters</span>
      {slicerFields.map((f) => (
        <Slicer
          key={f.col}
          field={f}
          filter={slicers.find((s) => s.col === f.col) || null}
          open={open === f.col}
          onOpen={(v) => setOpen(v)}
          onSet={setSlicer}
        />
      ))}
      {slicerFields.length === 0 && <span className="meta">No filterable fields yet.</span>}

      <span className="filter-spacer" />

      {cross.map((s, i) => (
        <span key={`${s.field}-${i}`} className="cross-pill" title="Cross-filter from a chart click">
          {crossLabel(s, list)}
          <button className="cross-pill-x" onClick={() => removeCross(i)} title="Clear this selection">
            ✕
          </button>
        </span>
      ))}
      {pending && <span className="meta">updating…</span>}
      {active > 0 && (
        <button className="chip" onClick={onClear} title="Clear every slicer and selection">
          ✕ clear all
        </button>
      )}
    </div>
  );
}

function Slicer({ field, filter, open, onOpen, onSet }) {
  const kind = slicerKind(field);
  const label = filter ? filterLabel(filter, [field]) : field.label || field.col;
  const tiles = Array.isArray(field.tiles) ? field.tiles.length : 0;
  return (
    <span className="filter-slicer">
      <button
        className={"filter-chip" + (filter ? " chip-on" : "")}
        onClick={() => onOpen(open ? null : field.col)}
        title={
          `${field.label || field.col} · ${field.dtype || "string"}` +
          (tiles ? ` · in ${tiles} tile${tiles > 1 ? "s" : ""}` : "")
        }
      >
        {label} ▾
      </button>
      {open && (
        <div className="filter-pop">
          {kind === "range" ? (
            <RangePop field={field} filter={filter} onSet={onSet} />
          ) : kind === "text" ? (
            <TextPop field={field} filter={filter} onSet={onSet} />
          ) : (
            <ListPop field={field} filter={filter} onSet={onSet} />
          )}
          <div className="filter-pop-foot">
            <button className="chip" onClick={() => onSet(field.col, null)}>clear</button>
            <button className="chip" onClick={() => onOpen(null)}>done</button>
          </div>
        </div>
      )}
    </span>
  );
}

function ListPop({ field, filter, onSet }) {
  const [q, setQ] = useState("");
  const all = Array.isArray(field.values) ? field.values : [];
  const picked = filter?.op === "in" && Array.isArray(filter.values) ? filter.values : [];
  const pickedKeys = new Set(picked.map(String));
  const needle = q.trim().toLowerCase();
  const shown = (needle ? all.filter((v) => String(v).toLowerCase().includes(needle)) : all).slice(0, 300);

  function toggle(v) {
    const key = String(v);
    const next = pickedKeys.has(key) ? picked.filter((p) => String(p) !== key) : [...picked, v];
    onSet(field.col, slicerToFilter({ col: field.col, dtype: field.dtype, kind: "list", values: next }));
  }

  if (!all.length) {
    return (
      <div className="filter-pop-body">
        <div className="meta">
          {field.value_count > 0
            ? `${field.value_count} distinct values — too many to list.`
            : "No values available."}
        </div>
        <TextPop field={field} filter={filter} onSet={onSet} />
      </div>
    );
  }

  return (
    <div className="filter-pop-body">
      {all.length > CHIP_LIMIT && (
        <input
          className="filter-search"
          placeholder={`Search ${field.label || field.col}…`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      )}
      <div className={all.length > CHIP_LIMIT ? "filter-values filter-values-scroll" : "filter-values"}>
        {shown.map((v, i) => (
          <button
            key={`${String(v)}-${i}`}
            className={"chip" + (pickedKeys.has(String(v)) ? " chip-active" : "")}
            onClick={() => toggle(v)}
          >
            {fmtVal(v)}
          </button>
        ))}
        {shown.length === 0 && <span className="meta">No match.</span>}
      </div>
    </div>
  );
}

function RangePop({ field, filter, onSet }) {
  const seed = (() => {
    if (filter?.op === "between") return [filter.lo ?? "", filter.hi ?? ""];
    if (filter?.op === "gte") return [filter.value ?? "", ""];
    if (filter?.op === "lte") return ["", filter.value ?? ""];
    return ["", ""];
  })();
  const [lo, setLo] = useState(seed[0]);
  const [hi, setHi] = useState(seed[1]);
  const first = useRef(true);

  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const h = setTimeout(
      () => onSet(field.col, slicerToFilter({ col: field.col, dtype: field.dtype, kind: "range", lo, hi })),
      200
    );
    return () => clearTimeout(h);
  }, [lo, hi]);

  // Text inputs, not type="date": bins emit sortable strings ("2026-03",
  // "2026-W10") that a native date picker cannot represent.
  return (
    <div className="filter-pop-body filter-range">
      <input
        className="filter-input"
        placeholder={isBlank(field.min) ? "from" : `from ${fmtVal(field.min)}`}
        value={lo}
        onChange={(e) => setLo(e.target.value)}
      />
      <span className="meta">to</span>
      <input
        className="filter-input"
        placeholder={isBlank(field.max) ? "to" : `to ${fmtVal(field.max)}`}
        value={hi}
        onChange={(e) => setHi(e.target.value)}
      />
    </div>
  );
}

function TextPop({ field, filter, onSet }) {
  const [text, setText] = useState(filter?.op === "contains" ? String(filter.value ?? "") : "");
  const first = useRef(true);

  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const h = setTimeout(
      () => onSet(field.col, slicerToFilter({ col: field.col, dtype: field.dtype, kind: "text", text })),
      250
    );
    return () => clearTimeout(h);
  }, [text]);

  return (
    <div className="filter-pop-body">
      <input
        className="filter-input"
        placeholder={`${field.label || field.col} contains…`}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
    </div>
  );
}
