// The filter bar above a visual in the D3 & artifacts window (logic in
// visualFilters.js): a date range with presets, category pickers, top N.
// Changing anything re-draws the visual from the rows it already has — with
// motion, not a jump — so trying a different window or slice is instant.
import { TOP_N_CHOICES, datePresets, isFiltered } from "./visualFilters";

function CategoryPicker({ control, keep, onChange }) {
  const all = !Array.isArray(keep);
  const chosen = new Set(all ? control.values : keep);
  const toggle = (v) => {
    const next = new Set(chosen);
    if (next.has(v)) next.delete(v); else next.add(v);
    onChange(next.size === control.values.length ? null : control.values.filter((x) => next.has(x)));
  };
  const label = all ? "All" : chosen.size === 0 ? "None"
    : chosen.size === 1 ? [...chosen][0] : `${chosen.size} of ${control.values.length}`;
  return (
    <details className="vc-pick">
      <summary title={`Filter by ${control.column}`}>
        <span className="vc-label">{control.column}</span> {label} ▾
      </summary>
      <div className="vc-menu">
        <div className="vc-menu-actions">
          <button type="button" className="chip" onClick={() => onChange(null)}>all</button>
          <button type="button" className="chip" onClick={() => onChange([])}>none</button>
        </div>
        {control.values.map((v) => (
          <label key={v} className="vc-option">
            <input type="checkbox" checked={chosen.has(v)} onChange={() => toggle(v)} /> {v}
          </label>
        ))}
      </div>
    </details>
  );
}

export default function VisualControls({ controls, state, onChange, shown, total }) {
  const { date, categories, topN } = controls;
  if (!date && !categories.length && !topN) return null;
  const set = (patch) => onChange({ ...state, ...patch });
  const presets = datePresets(date);
  const kind = date?.monthly ? "month" : "date";
  // Month pickers speak "YYYY-MM"; the state keeps whole days either way.
  const show = (d) => (d && kind === "month" ? d.slice(0, 7) : d || "");
  const take = (v, end) => (!v ? null : kind === "month"
    ? (end ? endOfMonth(v) : `${v}-01`) : v);

  return (
    <div className="vc-bar" role="group" aria-label="Filter this visual">
      {date && (
        <span className="vc-dates">
          <span className="vc-label">{date.column}</span>
          <select className="vc-preset" value={state.preset || "custom"} aria-label="Date range"
                  onChange={(e) => {
                    const p = presets.find((x) => x.key === e.target.value);
                    set(e.target.value === "all" ? { preset: "all", from: null, to: null }
                      : p ? { preset: p.key, from: p.from, to: date.max } : { preset: "custom" });
                  }}>
            <option value="all">All dates</option>
            {presets.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
            <option value="custom">Custom…</option>
          </select>
          <input type={kind} aria-label="From" min={show(date.min)} max={show(date.max)}
                 value={show(state.from || date.min)}
                 onChange={(e) => set({ preset: "custom", from: take(e.target.value, false) })} />
          <span className="vc-dash">–</span>
          <input type={kind} aria-label="To" min={show(date.min)} max={show(date.max)}
                 value={show(state.to || date.max)}
                 onChange={(e) => set({ preset: "custom", to: take(e.target.value, true) })} />
        </span>
      )}
      {categories.map((c) => (
        <CategoryPicker key={c.column} control={c} keep={state.cats?.[c.column]}
                        onChange={(keep) => set({ cats: { ...state.cats, [c.column]: keep } })} />
      ))}
      {topN && (
        <select className="vc-preset" aria-label={`Top ${topN.column}`} value={state.top || ""}
                onChange={(e) => set({ top: Number(e.target.value) || null })}>
          <option value="">All {topN.column}</option>
          {TOP_N_CHOICES.map((n) => <option key={n} value={n}>Top {n} by {topN.by}</option>)}
        </select>
      )}
      {isFiltered(state) && (
        <>
          <span className="meta vc-count">{shown} of {total} rows</span>
          <button type="button" className="chip" onClick={() => onChange(null)}>reset</button>
        </>
      )}
    </div>
  );
}

function endOfMonth(ym) {
  const [y, m] = ym.split("-").map(Number);
  const last = new Date(Date.UTC(y, m, 0)).getUTCDate();
  return `${ym}-${String(last).padStart(2, "0")}`;
}
