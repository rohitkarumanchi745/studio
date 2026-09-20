// The source catalog behind the Tableau-style connect grid: one tile per data
// source Studio knows about, with its mark, its display name and which shelf it
// sits on.
//
// Why the presentation lives here and not in the backend: the backend is the
// source of truth for what a connector NEEDS (/connections/types serves the
// field spec, which is what the connect form renders). What a connector LOOKS
// like is pure chrome, and duplicating it into connections.py would put the
// same string in two places. Anything missing from this map still renders — it
// falls back to a lettermark and a humanised name — so a connector added to the
// backend appears in the grid immediately, just without its logo.
//
// The marks are drawn here rather than shipped as vendor logo files: a vendor's
// logo is their trademark, and an approximate geometric mark in the brand
// colour carries the recognition without redistributing their asset.

const CATEGORIES = [
  { key: "warehouse", label: "Databases and warehouses" },
  { key: "workspace", label: "Files and workspaces" },
  { key: "storage", label: "Cloud file storage" },
  { key: "marketing", label: "Marketing and analytics" },
  { key: "other", label: "Other" },
];

// ── Marks ───────────────────────────────────────────────────────────────
// Each takes the tile colour and returns the SVG body for a 40×40 viewBox.

const MARKS = {
  cylinder: (c) => (
    <>
      <path d="M8 11v18c0 2.8 5.4 5 12 5s12-2.2 12-5V11z" fill={c} opacity=".22" />
      <ellipse cx="20" cy="11" rx="12" ry="5" fill={c} />
      <path d="M8 11v18c0 2.8 5.4 5 12 5s12-2.2 12-5V11" fill="none" stroke={c} strokeWidth="2.4" />
      <path d="M8 20c0 2.8 5.4 5 12 5s12-2.2 12-5" fill="none" stroke={c} strokeWidth="2.4" />
    </>
  ),
  snowflake: (c) => (
    <g stroke={c} strokeWidth="2.6" strokeLinecap="round">
      <path d="M20 5v30M7 12.5l26 15M33 12.5l-26 15" />
      <path d="M20 11l-4-3M20 11l4-3M20 29l-4 3M20 29l4 3" strokeWidth="2.2" />
    </g>
  ),
  bricks: (c) => (
    <g fill={c}>
      <path d="M8 13.5 20 7l12 6.5-12 6.5z" opacity=".45" />
      <path d="M8 20 20 13.5 32 20l-12 6.5z" opacity=".72" />
      <path d="M8 26.5 20 20l12 6.5L20 33z" />
    </g>
  ),
  hexcloud: (c) => (
    <>
      <path d="M20 5.5 33 13v14L20 34.5 7 27V13z" fill={c} opacity=".18" />
      <path d="M20 5.5 33 13v14L20 34.5 7 27V13z" fill="none" stroke={c} strokeWidth="2.4" />
      <circle cx="20" cy="20" r="6" fill="none" stroke={c} strokeWidth="2.4" />
      <path d="M24.2 24.2 29 29" stroke={c} strokeWidth="2.6" strokeLinecap="round" />
    </>
  ),
  graph: (c) => (
    <>
      <g stroke={c} strokeWidth="2.2">
        <path d="M11 13.5 29 10M11 13.5 22 29M29 10 22 29" />
      </g>
      <circle cx="11" cy="13.5" r="4.5" fill={c} />
      <circle cx="29" cy="10" r="3.6" fill={c} opacity=".6" />
      <circle cx="22" cy="29" r="4" fill={c} opacity=".8" />
    </>
  ),
  bucket: (c) => (
    <>
      <path d="M8 12h24l-3 20a2 2 0 0 1-2 1.8H13a2 2 0 0 1-2-1.8z" fill={c} opacity=".22" />
      <path d="M8 12h24l-3 20a2 2 0 0 1-2 1.8H13a2 2 0 0 1-2-1.8z" fill="none" stroke={c} strokeWidth="2.4" />
      <path d="M6 12h28" stroke={c} strokeWidth="2.8" strokeLinecap="round" />
      <path d="M15 19v8M20 19v8M25 19v8" stroke={c} strokeWidth="1.8" opacity=".55" />
    </>
  ),
  blobs: (c) => (
    <>
      <path d="M16 7 6 30h9l3-7z" fill={c} opacity=".45" />
      <path d="M21 13 11 30h23l-5-9h-7l4-8z" fill={c} />
    </>
  ),
  tiles: () => (
    <>
      <rect x="7" y="7" width="12" height="12" rx="1.5" fill="#f25022" />
      <rect x="21" y="7" width="12" height="12" rx="1.5" fill="#7fba00" />
      <rect x="7" y="21" width="12" height="12" rx="1.5" fill="#00a4ef" />
      <rect x="21" y="21" width="12" height="12" rx="1.5" fill="#ffb900" />
    </>
  ),
  bars: (c) => (
    <g fill={c}>
      <rect x="8" y="22" width="6.5" height="12" rx="2.5" opacity=".45" />
      <rect x="16.75" y="15" width="6.5" height="19" rx="2.5" opacity=".72" />
      <rect x="25.5" y="6" width="6.5" height="28" rx="2.5" />
    </g>
  ),
  pin: (c) => (
    <>
      <path d="M13 32a5.4 5.4 0 0 1-4.7-8.1l9-15.6a5.4 5.4 0 0 1 9.4 5.4l-9 15.6A5.4 5.4 0 0 1 13 32z" fill={c} opacity=".5" />
      <path d="M27 32a5.4 5.4 0 0 0 4.7-8.1l-9-15.6a5.4 5.4 0 0 0-9.4 5.4l9 15.6A5.4 5.4 0 0 0 27 32z" fill={c} />
    </>
  ),
  orbit: (c) => (
    <>
      <circle cx="20" cy="20" r="11.5" fill="none" stroke={c} strokeWidth="2.4" opacity=".45" />
      <path d="M20 8.5a11.5 11.5 0 0 1 8.1 19.6" fill="none" stroke={c} strokeWidth="2.8" strokeLinecap="round" />
      <circle cx="20" cy="20" r="4.5" fill={c} />
    </>
  ),
  rings: (c) => (
    <>
      <circle cx="16" cy="20" r="9.5" fill="none" stroke={c} strokeWidth="2.4" opacity=".5" />
      <circle cx="24" cy="20" r="9.5" fill="none" stroke={c} strokeWidth="2.4" />
      <circle cx="20" cy="20" r="2.6" fill={c} />
    </>
  ),
  petals: (c) => (
    <g fill={c}>
      <circle cx="20" cy="10" r="4.4" opacity=".9" />
      <circle cx="28.7" cy="15" r="4.4" opacity=".72" />
      <circle cx="28.7" cy="25" r="4.4" opacity=".56" />
      <circle cx="20" cy="30" r="4.4" opacity=".44" />
      <circle cx="11.3" cy="25" r="4.4" opacity=".56" />
      <circle cx="11.3" cy="15" r="4.4" opacity=".72" />
    </g>
  ),
  chevrons: (c) => (
    <g fill="none" stroke={c} strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M11 11l8 9-8 9" opacity=".45" />
      <path d="M22 11l8 9-8 9" />
    </g>
  ),
  panel: (c) => (
    <>
      <rect x="6.5" y="8.5" width="27" height="23" rx="3.5" fill={c} opacity=".18" />
      <rect x="6.5" y="8.5" width="27" height="23" rx="3.5" fill="none" stroke={c} strokeWidth="2.4" />
      <g fill={c}>
        <rect x="11.5" y="21" width="4" height="6" rx="1.4" />
        <rect x="18" y="16" width="4" height="11" rx="1.4" />
        <rect x="24.5" y="12.5" width="4" height="14.5" rx="1.4" />
      </g>
    </>
  ),
  beaker: (c) => (
    <>
      <path d="M16 6v10L9 29a3 3 0 0 0 2.7 4.4h16.6A3 3 0 0 0 31 29l-7-13V6" fill="none" stroke={c} strokeWidth="2.4" strokeLinejoin="round" />
      <path d="M13.4 24h13.2l4.4 5A3 3 0 0 1 28.3 33.4H11.7A3 3 0 0 1 9 29z" fill={c} opacity=".35" />
      <path d="M14 6h12" stroke={c} strokeWidth="2.8" strokeLinecap="round" />
    </>
  ),
};

// ── The catalog ─────────────────────────────────────────────────────────
// Keys are the backend's identifiers: a `ctype` from /connections/types for
// anything connectable here, otherwise the source `name` from /catalog/sources.

const META = {
  // Connectable from this screen — the field spec comes from the backend.
  postgres: { label: "PostgreSQL", color: "#336791", mark: "cylinder", category: "warehouse",
              blurb: "Any Postgres-wire database — RDS, Cloud SQL, Neon, Supabase, Timescale." },
  snowflake: { label: "Snowflake", color: "#29b5e8", mark: "snowflake", category: "warehouse",
               blurb: "Warehouse, database and schema on a Snowflake account." },
  databricks: { label: "Databricks SQL", color: "#ff3621", mark: "bricks", category: "warehouse",
                blurb: "A SQL warehouse on a Databricks workspace, over Unity Catalog." },
  bigquery: { label: "BigQuery", color: "#4285f4", mark: "hexcloud", category: "warehouse",
              blurb: "A BigQuery dataset, read with a service account." },
  neo4j: { label: "Neo4j", color: "#018bff", mark: "graph", category: "warehouse",
           blurb: "A property graph queried with Cypher." },

  // Connected through their own flow rather than a credential form.
  m365: { label: "Microsoft 365", color: "#0f6cbd", mark: "tiles", category: "workspace",
          blurb: "OneDrive, SharePoint and Outlook, synced into your private knowledge collection." },

  // Environment-configured sources. They appear so the grid is the whole map
  // of what Studio can read, not just what this screen can set up.
  s3: { label: "Amazon S3", color: "#569a31", mark: "bucket", category: "storage",
        blurb: "Parquet, CSV and JSON in S3 (or any S3-compatible store), read through DuckDB." },
  azure_blob: { label: "Azure Blob Storage", color: "#0078d4", mark: "blobs", category: "storage",
                blurb: "Blob and ADLS Gen2 containers, read through DuckDB." },
  gcs: { label: "Google Cloud Storage", color: "#ea4335", mark: "bucket", category: "storage",
         blurb: "GCS buckets over the S3-compatible endpoint, read through DuckDB." },

  ga4: { label: "Google Analytics 4", color: "#e37400", mark: "bars", category: "marketing",
         blurb: "GA4 reports as queryable tables." },
  google_ads: { label: "Google Ads", color: "#4285f4", mark: "pin", category: "marketing",
                blurb: "Campaign and keyword performance reports." },
  microsoft_ads: { label: "Microsoft Advertising", color: "#00a4ef", mark: "pin", category: "marketing",
                   blurb: "Campaign performance from Microsoft Advertising." },
  braze: { label: "Braze", color: "#ff9b3f", mark: "rings", category: "marketing",
           blurb: "Campaign and canvas engagement statistics." },
  algolia: { label: "Algolia", color: "#5468ff", mark: "orbit", category: "marketing",
             blurb: "Search analytics — top queries and no-result queries." },
  qualtrics: { label: "Qualtrics", color: "#00b4ef", mark: "rings", category: "marketing",
               blurb: "Survey responses as tables." },
  sprinklr: { label: "Sprinklr", color: "#0d9ddb", mark: "petals", category: "marketing",
              blurb: "Social listening and engagement reports." },
  powerbi_sap: { label: "Power BI / SAP", color: "#c8a415", mark: "panel", category: "marketing",
                 blurb: "Datasets published to a Power BI workspace." },
  dynamic_yield: { label: "Dynamic Yield", color: "#00a3b5", mark: "chevrons", category: "marketing",
                   blurb: "Experiment and personalisation results." },

  demo: { label: "Demo data", color: "#79766f", mark: "beaker", category: "other",
          blurb: "The built-in sample warehouse — always available, safe to explore." },
};

function humanize(id) {
  return String(id || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (m) => m.toUpperCase());
}

/** Display metadata for a ctype or source name; never throws, never returns null. */
export function metaFor(id) {
  const m = META[id];
  if (m) return { id, ...m };
  // A source the backend serves that this bundle predates. It still gets a
  // tile — a lettermark and a humanised name — so the grid never has a hole.
  // The label must be non-empty: SourceIcon takes its first character.
  return { id, label: humanize(id) || "Unknown source", color: "#79766f",
           mark: null, category: "other", blurb: "" };
}

export { CATEGORIES };

/** The tile mark. Unknown sources get a lettermark so the grid never has a hole. */
export function SourceIcon({ id, size = 40 }) {
  const m = metaFor(id);
  const draw = m.mark && MARKS[m.mark];
  return (
    <svg
      className="srcicon"
      width={size}
      height={size}
      viewBox="0 0 40 40"
      role="img"
      aria-label={m.label}
      style={{ "--mark": m.color }}
    >
      {draw ? (
        draw(m.color)
      ) : (
        <>
          <rect x="4" y="4" width="32" height="32" rx="9" fill={m.color} opacity=".16" />
          <text
            x="20"
            y="20"
            textAnchor="middle"
            dominantBaseline="central"
            fontSize="17"
            fontWeight="700"
            fill={m.color}
          >
            {m.label.slice(0, 1).toUpperCase()}
          </text>
        </>
      )}
    </svg>
  );
}
