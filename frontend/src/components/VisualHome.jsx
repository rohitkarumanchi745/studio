// The D3 & artifacts window's home: what a new visual conversation opens on.
// A greeting, one question, the composer in the middle of the page, quick
// starts that fill the composer with a phrase to finish, three bigger starts,
// and the conversations you visualized lately — so the window says what it
// is for (charts, dashboards, maps, custom visuals) before you type a word.
//
// Pure helpers are exported for tests; the components only lay them out.

/** "Good morning" / "Good afternoon" / "Good evening" for a local hour. */
export function greeting(hour) {
  return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

/** The name to greet: the first word of the user's name, else their email's. */
export function firstName(user) {
  const name = String(user?.name || "").trim().split(/\s+/)[0];
  if (name) return name;
  const local = String(user?.email || "").split("@")[0].split(/[._+-]/)[0];
  return local ? local[0].toUpperCase() + local.slice(1) : "";
}

// What each quick start puts in the composer. "…" is where the reader types:
// it is selected, so typing replaces it.
export const QUICK_STARTS = [
  { key: "chart", label: "Chart", icon: "bars", text: "Show … as a bar chart" },
  { key: "table", label: "Table", icon: "table", text: "Show … as a table" },
  { key: "dashboard", label: "Dashboard", icon: "grid",
    text: "Build a dashboard of … with a view by day, by month and by year" },
  { key: "map", label: "Map", icon: "pin", text: "Map … on a route map" },
  { key: "custom", label: "Custom visual", icon: "spark",
    text: "Write a custom interactive D3 visual of …" },
  { key: "automate", label: "Automate", icon: "bolt", text: "Every Monday at 9, chart …" },
];

export const BIG_STARTS = [
  { key: "dashboard", title: "Build a dashboard", tone: "violet", icon: "grid",
    sub: "Several views of one question, side by side." },
  { key: "custom", title: "Write a custom visual", tone: "mint", icon: "spark",
    sub: "The AI writes interactive D3 you can save as a file." },
  { key: "map", title: "Map your data", tone: "peach", icon: "pin",
    sub: "Routes, places and regions — transit, shipments, sales by country." },
];

/** The composer's text for a start, and the span to select (the "…"). */
export function fillStart(key) {
  const start = QUICK_STARTS.find((s) => s.key === key);
  if (!start) return null;
  const at = start.text.indexOf("…");
  return { text: start.text, select: at < 0 ? [start.text.length, start.text.length] : [at, at + 1] };
}

/** A recent visual conversation's thumbnail, read from its title. */
export function glyphFor(title) {
  const t = String(title || "").toLowerCase();
  // Asked for by name first ("… as a map", "… share"), then what the words
  // suggest ("by region" is a map only when nothing else is asked for).
  if (/\b(map|maps|routes?|transit|trips?|flights?|shipments?)\b/.test(t)) return "map";
  if (/\b(table|list|rows)\b/.test(t)) return "table";
  if (/\b(dashboard|views|overview)\b/.test(t)) return "grid";
  if (/\b(share|mix|split|breakdown|percent|portion)/.test(t)) return "donut";
  if (/\b(trend|over time|by (day|week|month|year)|daily|monthly|growth)/.test(t)) return "line";
  if (/\b(countr|states?\b|cities|city|locations?)/.test(t)) return "map";
  return "bars";
}

/** Up to `n` recent visual conversations, titled without their 🎨 mark. */
export function recentVisuals(conversations, n = 4) {
  return (conversations || []).slice(0, n).map((c) => {
    const title = String(c.title || "").replace(/^🎨\s*/, "") || "Untitled visual";
    return { id: c.id, title, glyph: glyphFor(title), at: c.created_at };
  });
}

/** "just now" / "5 min ago" / "3 h ago" / "2 d ago" for epoch seconds. */
export function ago(seconds, now = Date.now() / 1000) {
  const d = Math.max(0, now - Number(seconds));
  if (!Number.isFinite(d)) return "";
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return `${Math.floor(d / 86400)} d ago`;
}

// ── Icons and thumbnails, drawn here (24×24 line icons) ─────────────────

const ICONS = {
  bars: <><path d="M5 19V11M10 19V6M15 19v-5M20 19V9" /></>,
  table: <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M4 10h16M10 10v9" /></>,
  grid: <><rect x="4" y="4" width="7" height="7" rx="1.5" /><rect x="13" y="4" width="7" height="7" rx="1.5" />
    <rect x="4" y="13" width="7" height="7" rx="1.5" /><rect x="13" y="13" width="7" height="7" rx="1.5" /></>,
  pin: <><path d="M12 21s-6-5.6-6-11a6 6 0 0 1 12 0c0 5.4-6 11-6 11z" /><circle cx="12" cy="10" r="2.2" /></>,
  spark: <><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18" /></>,
  bolt: <><path d="M13 3 5 13h6l-1 8 8-10h-6z" /></>,
};

export function Icon({ name, size = 18 }) {
  return (
    <svg className="vh-icon" width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true">
      {ICONS[name] || ICONS.bars}
    </svg>
  );
}

// The chart palette's first colors (renderers/common.js SERIES), so a
// thumbnail looks like the charts the window draws.
const THUMB = ["#3987e5", "#9085e9", "#199e70", "#c98500"];

function Thumb({ glyph }) {
  const body = {
    bars: [12, 20, 15, 26, 22, 32].map((h, i) => (
      <rect key={i} x={10 + i * 13} y={44 - h} width="8" height={h} rx="2"
            fill={THUMB[i % 2 ? 1 : 0]} opacity={0.55 + i * 0.07} />)),
    line: <><path d="M8 40 L26 30 L42 34 L60 20 L82 14" fill="none" stroke={THUMB[0]} strokeWidth="2.5"
                  strokeLinecap="round" strokeLinejoin="round" />
      <path d="M8 40 L26 30 L42 34 L60 20 L82 14 L82 46 L8 46z" fill={THUMB[0]} opacity=".12" /></>,
    donut: <><circle cx="30" cy="28" r="14" fill="none" stroke={THUMB[0]} strokeWidth="7"
                     strokeDasharray="56 88" transform="rotate(-90 30 28)" />
      <circle cx="30" cy="28" r="14" fill="none" stroke={THUMB[1]} strokeWidth="7" strokeDasharray="0 56 32 100"
              transform="rotate(-90 30 28)" />
      <rect x="54" y="18" width="28" height="4" rx="2" fill={THUMB[0]} opacity=".5" />
      <rect x="54" y="27" width="22" height="4" rx="2" fill={THUMB[1]} opacity=".5" />
      <rect x="54" y="36" width="16" height="4" rx="2" fill="#c8c6c4" /></>,
    grid: [[8, 8], [48, 8], [8, 30], [48, 30]].map(([x, y], i) => (
      <rect key={i} x={x} y={y} width="36" height="16" rx="3" fill={THUMB[i]} opacity=".45" />)),
    table: [10, 20, 30, 40].map((y, i) => (
      <rect key={i} x="10" y={y} width={i ? 72 : 72} height="5" rx="2"
            fill={i ? "#c8c6c4" : THUMB[0]} opacity={i ? 0.7 : 0.6} />)),
    map: <><path d="M10 38 C24 14, 44 44, 60 20 S 80 18, 84 12" fill="none" stroke={THUMB[0]} strokeWidth="2.5"
                  strokeDasharray="5 4" strokeLinecap="round" />
      <circle cx="10" cy="38" r="4" fill={THUMB[0]} /><circle cx="84" cy="12" r="4" fill={THUMB[2]} />
      <circle cx="60" cy="20" r="3" fill={THUMB[2]} /></>,
  }[glyph] || null;
  return (
    <svg className="vh-thumb" viewBox="0 0 92 50" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
      {body}
    </svg>
  );
}

// ── The home ────────────────────────────────────────────────────────────

/** Above the composer: who it is for, and the one question. */
export function VisualHomeHeader({ user, now = new Date() }) {
  const name = firstName(user);
  return (
    <div className="vh-head">
      <div className="vh-hello">{greeting(now.getHours())}{name ? `, ${name}` : ""}</div>
      <h1 className="vh-title">What would you like to visualize today?</h1>
      <div className="vh-sub">
        Every answer is drawn with D3 — a chart, a dashboard, a map, or a custom visual the AI
        writes for you.
      </div>
    </div>
  );
}

/** Below the composer: quick starts, bigger starts, ideas from the data, and
 *  recent visuals. `onStart(key)` fills the composer. */
export function VisualHomeStarts({ onStart, recent = [], onOpen, onOpenCharts, ideas = [], onIdea }) {
  const shown = recentVisuals(recent);
  return (
    <div className="vh-below">
      <div className="vh-chips" role="group" aria-label="Quick starts">
        {QUICK_STARTS.map((s) => (
          <button key={s.key} type="button" className="vh-chip" onClick={() => onStart(s.key)}
                  title={s.text}>
            <Icon name={s.icon} size={15} /> {s.label}
          </button>
        ))}
      </div>

      <div className="vh-cards">
        {BIG_STARTS.map((s) => (
          <button key={s.key} type="button" className="vh-card" onClick={() => onStart(s.key)}>
            <span className={`vh-tile vh-tile-${s.tone}`}><Icon name={s.icon} size={20} /></span>
            <span className="vh-card-text">
              <b>{s.title}</b>
              <span>{s.sub}</span>
            </span>
          </button>
        ))}
      </div>

      {ideas.length > 0 && (
        <section className="vh-section" aria-label="From your data">
          <div className="vh-section-head"><span>From your data</span></div>
          <div className="vh-ideas">
            {ideas.map((q) => (
              <button key={q} type="button" className="vh-idea" onClick={() => onIdea(q)}>{q}</button>
            ))}
          </div>
        </section>
      )}

      {shown.length > 0 && (
        <section className="vh-section" aria-label="Recent">
          <div className="vh-section-head">
            <span>Recent</span>
            {onOpenCharts && (
              <button type="button" className="vh-link" onClick={onOpenCharts}>Your charts →</button>
            )}
          </div>
          <div className="vh-recent">
            {shown.map((c) => (
              <button key={c.id} type="button" className="vh-recent-card" onClick={() => onOpen?.(c.id)}
                      title={c.title}>
                <span className="vh-recent-title">{c.title}</span>
                <Thumb glyph={c.glyph} />
                {c.at && <span className="vh-recent-at">{ago(c.at)}</span>}
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
