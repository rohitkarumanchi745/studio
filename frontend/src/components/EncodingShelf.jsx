// Data Formulator-style encoding shelf: a Fields rail + visual-channel wells.
// Assigning a field to X / Y / Color re-encodes the chart CLIENT-SIDE from the
// data already on screen — no round-trip to the agent — which is exactly the
// Data Formulator interaction. Deriving a NEW field (an aggregate, a computed
// column) still goes through the agent via the "+ new field" box.
import { useMemo, useState } from "react";
import { LABELS, computeFit } from "./ChartView";

const TYPE_ICON = { num: "#", cat: "A", time: "T" };

export function fieldKind(columns, rows, i) {
  const row = rows.find((r) => r[i] != null) || [];
  const v = row[i];
  if (typeof v === "number") return "num";
  const name = String(columns[i] || "").toLowerCase();
  if (/(^|_)(date|time|day|month|year|quarter|week|dt|ts)(_|$)/.test(name)) return "time";
  if (typeof v === "string" && /^\d{4}-\d{2}/.test(v)) return "time";
  return "cat";
}

// When Color is set, pivot (x, color, measure) into wide form so each color
// value becomes its own series — the chart engine draws multi-series from
// multiple y columns. Capped so a high-cardinality field can't explode.
const MAX_SERIES = 12;
export function deriveRenderData(columns, rows, spec) {
  if (!spec || !spec.color || !columns.includes(spec.color)) return { columns, rows, spec };
  const xi = columns.indexOf(spec.x);
  const ci = columns.indexOf(spec.color);
  const measure = (spec.y || []).find((c) => columns.includes(c) && c !== spec.color);
  const mi = measure != null ? columns.indexOf(measure) : -1;
  if (xi < 0 || ci < 0 || mi < 0 || xi === ci) return { columns, rows, spec };

  const xs = [], xseen = new Set(), colorVals = [], cseen = new Set(), acc = new Map();
  for (const r of rows) {
    const xv = r[xi], cv = String(r[ci]);
    if (!xseen.has(String(xv))) { xseen.add(String(xv)); xs.push(xv); }
    if (!cseen.has(cv) && colorVals.length < MAX_SERIES) { cseen.add(cv); colorVals.push(cv); }
    if (!cseen.has(cv)) continue; // beyond the cap
    const k = String(xv) + "" + cv;
    acc.set(k, (acc.get(k) || 0) + (typeof r[mi] === "number" ? r[mi] : 0));
  }
  const cols = [spec.x, ...colorVals];
  const outRows = xs.map((xv) => [xv, ...colorVals.map((cv) => acc.get(String(xv) + "" + cv) ?? 0)]);
  return { columns: cols, rows: outRows, spec: { ...spec, x: spec.x, y: colorVals } };
}

function Field({ name, kind }) {
  return (
    <div className="df-field" draggable
      onDragStart={(e) => { e.dataTransfer.setData("text/df-field", name); e.dataTransfer.effectAllowed = "copy"; }}
      title={`${name} · ${kind === "num" ? "quantitative" : kind === "time" ? "temporal" : "nominal"}`}>
      <span className={"df-ftype " + kind}>{TYPE_ICON[kind]}</span>
      <span className="df-fname">{name}</span>
    </div>
  );
}

// A single-value channel well (X, Color): drop or click-to-pick.
function Well({ label, value, eligible, onSet, onClear }) {
  const [open, setOpen] = useState(false);
  const [over, setOver] = useState(false);
  return (
    <div className="df-channel">
      <span className="df-channel-label">{label}</span>
      <div style={{ position: "relative" }}>
        <div
          className={"df-well" + (value ? " df-filled" : "") + (over ? " df-over" : "")}
          onClick={() => setOpen((o) => !o)}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault(); setOver(false);
            const f = e.dataTransfer.getData("text/df-field");
            if (f && eligible.includes(f)) onSet(f);
          }}>
          {value
            ? <>{value}<span className="df-chip-x" onClick={(e) => { e.stopPropagation(); onClear(); }}>{"✕"}</span></>
            : <span className="df-well-placeholder">drop field</span>}
        </div>
        {open && (
          <div className="df-pick" onMouseLeave={() => setOpen(false)}>
            {eligible.length === 0 && <div className="df-pick-none">no eligible fields</div>}
            {eligible.map((f) => (
              <div key={f} className="df-pick-item" onClick={() => { onSet(f); setOpen(false); }}>{f}</div>
            ))}
            {value && <div className="df-pick-item" onClick={() => { onClear(); setOpen(false); }} style={{ color: "var(--bad)" }}>{"✕ clear"}</div>}
          </div>
        )}
      </div>
    </div>
  );
}

// A multi-value channel well (Y): a set of chips + an add picker.
function MultiWell({ label, values, eligible, onChange }) {
  const [open, setOpen] = useState(false);
  const [over, setOver] = useState(false);
  const add = (f) => { if (f && !values.includes(f)) onChange([...values, f]); };
  const remaining = eligible.filter((c) => !values.includes(c));
  return (
    <div className="df-channel">
      <span className="df-channel-label">{label}</span>
      <div style={{ position: "relative" }}>
        <div
          className={"df-well df-well-multi" + (values.length ? " df-filled" : "") + (over ? " df-over" : "")}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault(); setOver(false);
            const f = e.dataTransfer.getData("text/df-field");
            if (f && eligible.includes(f)) add(f);
          }}
          onClick={() => setOpen((o) => !o)}>
          {values.length === 0 && <span className="df-well-placeholder">drop fields</span>}
          {values.map((v) => (
            <span key={v} className="df-filled" style={{ padding: "1px 4px", borderRadius: 5 }}>
              {v}<span className="df-chip-x" onClick={(e) => { e.stopPropagation(); onChange(values.filter((x) => x !== v)); }}>{"✕"}</span>
            </span>
          ))}
        </div>
        {open && (
          <div className="df-pick" onMouseLeave={() => setOpen(false)}>
            {remaining.length === 0 && <div className="df-pick-none">no more fields</div>}
            {remaining.map((f) => (
              <div key={f} className="df-pick-item" onClick={() => { add(f); setOpen(false); }}>{f}</div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default function EncodingShelf({ columns, rows, spec, onSpec, onDeriveField, busy, children }) {
  const kinds = useMemo(() => columns.map((_, i) => fieldKind(columns, rows, i)), [columns, rows]);
  const numeric = columns.filter((_, i) => kinds[i] === "num");
  const categorical = columns.filter((_, i) => kinds[i] !== "num");
  const [newField, setNewField] = useState("");

  // Chart types that fit the current data — the type menu offers these first.
  const fit = useMemo(() => computeFit(columns, rows, spec || {}), [columns, rows, spec]);
  const types = Object.keys(LABELS).filter((t) => t === "table" || fit[t]?.ok);

  const s = spec || { type: "bar", x: columns[0], y: numeric.slice(0, 1) };
  const patch = (p) => onSpec({ ...s, ...p });

  return (
    <div className="df">
      <aside className="df-fields">
        <div className="df-fields-head">Fields</div>
        <div className="df-field-list">
          {columns.map((c, i) => <Field key={c} name={c} kind={kinds[i]} />)}
        </div>
        <form className="df-newfield" onSubmit={(e) => { e.preventDefault(); if (newField.trim()) { onDeriveField(newField.trim()); setNewField(""); } }}>
          <input value={newField} onChange={(e) => setNewField(e.target.value)}
            placeholder="+ new field (e.g. profit = revenue - cost)" disabled={busy} />
          <button className="primary" style={{ width: 34, borderRadius: 8 }} disabled={busy || !newField.trim()}>{"→"}</button>
        </form>
      </aside>

      <div className="df-main">
        <div className="df-shelf">
          <div className="df-channel">
            <span className="df-channel-label">Chart</span>
            <select value={s.type} onChange={(e) => patch({ type: e.target.value })}>
              {types.map((t) => <option key={t} value={t}>{LABELS[t]}</option>)}
            </select>
          </div>
          <div className="df-shelf-sep" />
          <Well label="X" value={s.x} eligible={columns}
            onSet={(f) => patch({ x: f })} onClear={() => patch({ x: undefined })} />
          <MultiWell label="Y" values={(s.y || []).filter((c) => columns.includes(c))} eligible={numeric}
            onChange={(v) => patch({ y: v })} />
          <Well label="Color" value={s.color} eligible={categorical.filter((c) => c !== s.x)}
            onSet={(f) => patch({ color: f })} onClear={() => patch({ color: undefined })} />
        </div>
        {children}
      </div>
    </div>
  );
}
