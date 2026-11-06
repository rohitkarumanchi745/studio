// The source catalog behind the Tableau-style connect grid: one tile per data
// source or pipeline-planning context Studio knows about, with its mark,
// display name and shelf.
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
  { key: "database", label: "Databases" },
  { key: "warehouse", label: "Data warehouses and lakehouses" },
  { key: "nosql", label: "NoSQL, vector and graph" },
  { key: "work", label: "Work management and collaboration" },
  { key: "automation", label: "Automation and workflows" },
  { key: "messaging", label: "Send answers to" },
  { key: "bi", label: "BI and semantic models" },
  { key: "storage", label: "Cloud file storage" },
  { key: "marketing", label: "Marketing and analytics" },
  { key: "apps", label: "Business apps" },
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
  gitbranch: (c) => (
    <>
      <path d="M12 9v18c0 3.2 2.4 5.5 5.7 5.5h5.6c3.3 0 5.7-2.3 5.7-5.5V15"
        fill="none" stroke={c} strokeWidth="2.8" strokeLinecap="round" />
      <circle cx="12" cy="8" r="4" fill={c} />
      <circle cx="29" cy="13" r="4" fill={c} />
      <circle cx="29" cy="29" r="4" fill={c} />
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
  eye: (c) => (
    <>
      <path d="M5 20c4.5-7 9.5-10.5 15-10.5S30.5 13 35 20c-4.5 7-9.5 10.5-15 10.5S9.5 27 5 20z"
        fill={c} opacity=".2" />
      <path d="M5 20c4.5-7 9.5-10.5 15-10.5S30.5 13 35 20c-4.5 7-9.5 10.5-15 10.5S9.5 27 5 20z"
        fill="none" stroke={c} strokeWidth="2.4" strokeLinejoin="round" />
      <circle cx="20" cy="20" r="5.6" fill={c} />
      <circle cx="22" cy="18" r="1.6" fill="#fff" />
    </>
  ),
  diamond: (c) => (
    <g fill={c}>
      <path d="M20 4.5 35.5 20 20 35.5l-5-5L25.5 20 15 9.5z" />
      <path d="M14 10.5 23.5 20 14 29.5 4.5 20z" opacity=".55" />
    </g>
  ),
  wave: (c) => (
    <g fill="none" stroke={c} strokeWidth="2.8" strokeLinecap="round">
      <path d="M6 14c3.5-4 6.5-4 10 0s6.5 4 10 0 5-4 8-1" />
      <path d="M6 21c3.5-4 6.5-4 10 0s6.5 4 10 0 5-4 8-1" opacity=".7" />
      <path d="M6 28c3.5-4 6.5-4 10 0s6.5 4 10 0 5-4 8-1" opacity=".45" />
    </g>
  ),
  stack: (c) => (
    <g fill={c}>
      <rect x="8" y="7" width="24" height="7" rx="3" />
      <rect x="8" y="16.5" width="24" height="7" rx="3" opacity=".7" />
      <rect x="8" y="26" width="24" height="7" rx="3" opacity=".45" />
    </g>
  ),
  oval: (c) => (
    <ellipse cx="20" cy="20" rx="14" ry="9" fill="none" stroke={c} strokeWidth="5" />
  ),
  leaf: (c) => (
    <>
      <path d="M20 4c7 7 9 13 9 18a9 9 0 0 1-18 0c0-5 2-11 9-18z" fill={c} />
      <path d="M20 9v27" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" opacity=".8" />
    </>
  ),
  bolt: (c) => (
    <path d="M23 4 9 23h9l-2 13 15-20h-9z" fill={c} />
  ),
  cloudsq: (c) => (
    <>
      <path d="M12 29a6 6 0 0 1-.6-12 8.5 8.5 0 0 1 16.4-2A6.5 6.5 0 0 1 28 29z" fill={c} opacity=".25" />
      <path d="M12 29a6 6 0 0 1-.6-12 8.5 8.5 0 0 1 16.4-2A6.5 6.5 0 0 1 28 29z" fill="none"
        stroke={c} strokeWidth="2.4" strokeLinejoin="round" />
    </>
  ),
  nodes: (c) => (
    <>
      <path d="M9 20h22M20 20l7-8M20 20l7 8" stroke={c} strokeWidth="2.4" strokeLinecap="round" />
      <circle cx="8" cy="20" r="4.2" fill={c} />
      <circle cx="20" cy="20" r="3.6" fill={c} opacity=".75" />
      <circle cx="29" cy="11" r="3.6" fill={c} opacity=".6" />
      <circle cx="29" cy="29" r="3.6" fill={c} opacity=".6" />
    </>
  ),
  bubble: (c) => (
    <>
      <path d="M8 11.5A3.5 3.5 0 0 1 11.5 8h17a3.5 3.5 0 0 1 3.5 3.5v11a3.5 3.5 0 0 1-3.5 3.5H17l-6 5v-5h.5A3.5 3.5 0 0 1 8 22.5z"
        fill={c} opacity=".22" stroke={c} strokeWidth="2.4" strokeLinejoin="round" />
      <path d="M14 14h12M20 14v8" stroke={c} strokeWidth="2.6" strokeLinecap="round" />
    </>
  ),
  hash: (c) => (
    <>
      <rect x="7" y="7" width="26" height="26" rx="7" fill={c} opacity=".18" />
      <path d="M17 11l-2.4 18M25.4 11 23 29M11.5 17h18M10.5 23h18" stroke={c} strokeWidth="2.8"
        strokeLinecap="round" />
    </>
  ),
  lance: (c) => (
    <>
      <path d="M8 32L25.5 14.5" stroke={c} strokeWidth="3" strokeLinecap="round" />
      <path d="M21.5 10.5L33 7l-3.5 11.5z" fill={c} />
      <circle cx="10" cy="14" r="2.6" fill={c} opacity=".45" />
      <circle cx="16" cy="9" r="2" fill={c} opacity=".3" />
      <circle cx="26" cy="30" r="2.6" fill={c} opacity=".45" />
      <circle cx="31" cy="24" r="2" fill={c} opacity=".3" />
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
  postgres: { label: "PostgreSQL", color: "#336791", mark: "cylinder", category: "database",
              blurb: "Any Postgres-wire database — RDS, Cloud SQL, Neon, Supabase, Timescale." },
  snowflake: { label: "Snowflake", color: "#29b5e8", mark: "snowflake", category: "warehouse",
               blurb: "Warehouse, database and schema on a Snowflake account." },
  databricks: { label: "Databricks SQL", color: "#ff3621", mark: "bricks", category: "warehouse",
                blurb: "A SQL warehouse on a Databricks workspace, over Unity Catalog." },
  bigquery: { label: "BigQuery", color: "#4285f4", mark: "hexcloud", category: "warehouse",
              blurb: "A BigQuery dataset, read with a service account." },
  neo4j: { label: "Neo4j", color: "#018bff", mark: "graph", category: "nosql",
           blurb: "A property graph queried with Cypher." },
  sql: { label: "Any SQL database", color: "#5b6b7a", mark: "cylinder", category: "database",
         blurb: "Anything with a SQLAlchemy driver installed — MySQL, MariaDB, SQL Server, Oracle, and more — from one connection URL." },
  powerbi: { label: "Power BI", color: "#c8a415", mark: "panel", category: "bi",
             blurb: "Semantic models in a Power BI or Fabric workspace, read with a service principal." },

  // Connected through their own flow rather than a credential form.
  m365: { label: "Microsoft 365", color: "#0f6cbd", mark: "tiles", category: "work",
          blurb: "OneDrive, SharePoint and Outlook, synced into your private knowledge collection." },
  github: { label: "GitHub", color: "#24292f", mark: "gitbranch", category: "work",
            blurb: "Register repositories as read-only planning context for pipeline drafts; repository code is not executed." },

  // Where answers can be sent, not sources Studio reads.
  teams: { label: "Microsoft Teams", color: "#5059c9", mark: "bubble", category: "messaging",
           blurb: "Send an answer — its text, a table of its first rows and a link back — to a Teams channel." },
  slack: { label: "Slack", color: "#4a154b", mark: "hash", category: "messaging",
           blurb: "Send an answer — its text, a table of its first rows and a link back — to a Slack channel." },

  // Object storage: set up on the Integrations screen (keys plus the files to
  // read as a table) or by the deployment's environment.
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
  dynamic_yield: { label: "Dynamic Yield", color: "#00a3b5", mark: "chevrons", category: "marketing",
                   blurb: "Experiment and personalisation results." },

  demo: { label: "Demo data", color: "#79766f", mark: "beaker", category: "other",
          blurb: "The built-in sample warehouse — always available, safe to explore." },

  // Native connectors added with the marketplace.
  cassandra: { label: "Apache Cassandra", color: "#1287b1", mark: "eye", category: "nosql",
               blurb: "A keyspace's tables, read with CQL — also ScyllaDB and Amazon Keyspaces." },
  lancedb: { label: "LanceDB", color: "#6d5dfc", mark: "lance", category: "nosql",
             blurb: "Tables beside your embeddings — ids, text, sources, dates — queried with SQL, on object storage or LanceDB Cloud." },
  jira: { label: "Atlassian Jira", color: "#0052cc", mark: "diamond", category: "work",
          blurb: "Issues and projects from Jira Cloud or Data Center, queryable as tables." },

  // ── The marketplace: integrations that connect through a native type ────
  mysql: { label: "MySQL", color: "#00758f", mark: "wave", category: "database",
           blurb: "MySQL 5.7+ and 8 — self-hosted, Amazon RDS, Aurora MySQL, Cloud SQL." },
  mariadb: { label: "MariaDB", color: "#003545", mark: "wave", category: "database",
             blurb: "MariaDB Server and SkySQL, over the MySQL protocol." },
  mssql: { label: "Microsoft SQL Server", color: "#cc2927", mark: "stack", category: "database",
           blurb: "SQL Server 2014+ — on premises or on a VM." },
  azure_sql: { label: "Azure SQL Database", color: "#0078d4", mark: "stack", category: "database",
               blurb: "Azure SQL Database and Managed Instance." },
  oracle: { label: "Oracle Database", color: "#c74634", mark: "oval", category: "database",
            blurb: "Oracle 12c+ and Autonomous Database — thin mode, no Oracle client needed." },
  singlestore: { label: "SingleStore", color: "#aa00ff", mark: "bolt", category: "database",
                 blurb: "SingleStore (MemSQL), over the MySQL protocol." },
  tidb: { label: "TiDB", color: "#e30c34", mark: "stack", category: "database",
          blurb: "TiDB and TiDB Cloud, over the MySQL protocol." },
  planetscale: { label: "PlanetScale", color: "#222222", mark: "orbit", category: "database",
                 blurb: "PlanetScale MySQL databases." },
  supabase: { label: "Supabase", color: "#3ecf8e", mark: "bolt", category: "database",
              blurb: "A Supabase project's Postgres database." },
  neon: { label: "Neon", color: "#00b386", mark: "cylinder", category: "database",
          blurb: "Neon serverless Postgres." },
  cockroachdb: { label: "CockroachDB", color: "#6933ff", mark: "cylinder", category: "database",
                 blurb: "CockroachDB, over the Postgres wire protocol." },
  timescale: { label: "TimescaleDB", color: "#e8a300", mark: "cylinder", category: "database",
               blurb: "Timescale's time-series Postgres." },
  yugabyte: { label: "YugabyteDB", color: "#ff6e42", mark: "cylinder", category: "database",
              blurb: "YugabyteDB YSQL, over the Postgres wire protocol." },
  aurora_pg: { label: "Amazon Aurora PostgreSQL", color: "#ff9900", mark: "cylinder",
               category: "database", blurb: "Aurora and RDS for PostgreSQL." },
  cloudsql_pg: { label: "Google Cloud SQL / AlloyDB", color: "#4285f4", mark: "cylinder",
                 category: "database", blurb: "Cloud SQL for PostgreSQL and AlloyDB." },
  redshift: { label: "Amazon Redshift", color: "#8c4fff", mark: "hexcloud", category: "warehouse",
              blurb: "A Redshift cluster or Serverless workgroup, over the Postgres protocol." },
  clickhouse: { label: "ClickHouse", color: "#d4b800", mark: "bars", category: "warehouse",
                blurb: "ClickHouse and ClickHouse Cloud." },
  trino: { label: "Trino / Starburst", color: "#dd00a1", mark: "orbit", category: "warehouse",
           blurb: "A Trino or Starburst catalog and schema." },
  teradata: { label: "Teradata", color: "#f37440", mark: "stack", category: "warehouse",
              blurb: "Teradata Vantage." },
  db2: { label: "IBM Db2", color: "#0f62fe", mark: "stack", category: "warehouse",
         blurb: "Db2 for Linux, UNIX and Windows, and Db2 on Cloud." },
  hana: { label: "SAP HANA", color: "#0070f2", mark: "hexcloud", category: "warehouse",
          blurb: "SAP HANA and HANA Cloud." },
  vertica: { label: "Vertica", color: "#1c3d5a", mark: "chevrons", category: "warehouse",
             blurb: "Vertica analytic databases." },
  scylladb: { label: "ScyllaDB", color: "#2bb7d6", mark: "eye", category: "nosql",
              blurb: "ScyllaDB and Scylla Cloud — Cassandra-compatible CQL." },
  keyspaces: { label: "Amazon Keyspaces", color: "#ff9900", mark: "eye", category: "nosql",
               blurb: "Amazon Keyspaces (for Apache Cassandra), over TLS on port 9142." },
  jira_sm: { label: "Jira Service Management", color: "#2684ff", mark: "diamond", category: "work",
             blurb: "Service desk requests — the same Jira site and token as Jira." },

  // ── Not available yet: on the map, honestly marked ──────────────────────
  mongodb: { label: "MongoDB", color: "#47a248", mark: "leaf", category: "nosql",
             blurb: "Not available yet." },
  dynamodb: { label: "Amazon DynamoDB", color: "#4053d6", mark: "bolt", category: "nosql",
              blurb: "Not available yet." },
  elasticsearch: { label: "Elasticsearch", color: "#00a3a3", mark: "rings", category: "nosql",
                   blurb: "Not available yet." },
  salesforce: { label: "Salesforce", color: "#00a1e0", mark: "cloudsq", category: "apps",
                blurb: "Not available yet." },
  hubspot: { label: "HubSpot", color: "#ff7a59", mark: "orbit", category: "apps",
             blurb: "Not available yet." },
  stripe: { label: "Stripe", color: "#635bff", mark: "stack", category: "apps",
            blurb: "Not available yet." },
  zendesk: { label: "Zendesk", color: "#17494d", mark: "diamond", category: "apps",
             blurb: "Not available yet." },
  servicenow: { label: "ServiceNow", color: "#3f9a5f", mark: "orbit", category: "work",
                blurb: "Incidents, changes, problems and requests from a ServiceNow instance, queryable as tables." },
  n8n: { label: "n8n", color: "#ea4b71", mark: "nodes", category: "automation",
         blurb: "Workflows and their executions from n8n Cloud or self-hosted n8n — what ran, what failed, how long it took." },
  shopify: { label: "Shopify", color: "#5e8e3e", mark: "bucket", category: "apps",
             blurb: "Not available yet." },
  google_sheets: { label: "Google Sheets", color: "#0f9d58", mark: "panel", category: "apps",
                   blurb: "Not available yet." },
  tableau: { label: "Tableau", color: "#e97627", mark: "petals", category: "bi",
             blurb: "Not available yet." },
};

/**
 * How each marketplace integration connects. `ctype` is the native connection
 * type whose form it opens, `preset` what that form starts with, `dialect` the
 * SQLAlchemy dialect an Any-SQL tile needs a driver for (and the one its
 * connections are listed under), `driver` the package that provides it. No
 * `ctype` means not available yet.
 */
/** Shown first, on their own shelf, until the user searches. */
export const FEATURED = ["databricks", "powerbi", "servicenow", "jira", "n8n", "lancedb"];

export const MARKETPLACE = {
  mysql: { ctype: "sql", dialect: "mysql", driver: "pymysql",
           preset: { url: "mysql+pymysql://user:password@host:3306/database" } },
  mariadb: { ctype: "sql", dialect: "mysql", driver: "pymysql",
             preset: { url: "mysql+pymysql://user:password@host:3306/database" } },
  mssql: { ctype: "sql", dialect: "mssql", driver: "pymssql",
           preset: { url: "mssql+pymssql://user:password@host:1433/database", schema: "dbo" } },
  azure_sql: { ctype: "sql", dialect: "mssql", driver: "pymssql",
               preset: { url: "mssql+pymssql://user:password@server.database.windows.net:1433/database",
                         schema: "dbo" } },
  oracle: { ctype: "sql", dialect: "oracle", driver: "oracledb",
            preset: { url: "oracle+oracledb://user:password@host:1521/?service_name=ORCLPDB1" } },
  singlestore: { ctype: "sql", dialect: "mysql", driver: "pymysql",
                 preset: { url: "mysql+pymysql://user:password@host:3306/database" } },
  tidb: { ctype: "sql", dialect: "mysql", driver: "pymysql",
          preset: { url: "mysql+pymysql://user:password@host:4000/database" } },
  planetscale: { ctype: "sql", dialect: "mysql", driver: "pymysql",
                 preset: { url: "mysql+pymysql://user:password@aws.connect.psdb.cloud:3306/database" } },
  supabase: { ctype: "postgres",
              preset: { dsn: "postgresql://postgres:password@db.project.supabase.co:5432/postgres" } },
  neon: { ctype: "postgres",
          preset: { dsn: "postgresql://user:password@ep-name.region.aws.neon.tech/neondb?sslmode=require" } },
  cockroachdb: { ctype: "postgres",
                 preset: { dsn: "postgresql://user:password@host:26257/defaultdb?sslmode=require" } },
  timescale: { ctype: "postgres",
               preset: { dsn: "postgresql://tsdbadmin:password@host:5432/tsdb?sslmode=require" } },
  yugabyte: { ctype: "postgres", preset: { dsn: "postgresql://yugabyte:password@host:5433/yugabyte" } },
  aurora_pg: { ctype: "postgres",
               preset: { dsn: "postgresql://user:password@cluster.region.rds.amazonaws.com:5432/postgres" } },
  cloudsql_pg: { ctype: "postgres", preset: { dsn: "postgresql://user:password@host:5432/postgres" } },
  redshift: { ctype: "postgres",
              preset: { dsn: "postgresql://user:password@cluster.region.redshift.amazonaws.com:5439/dev" } },
  clickhouse: { ctype: "sql", dialect: "clickhouse", driver: "clickhouse-sqlalchemy",
                preset: { url: "clickhouse+native://user:password@host:9000/default" } },
  trino: { ctype: "sql", dialect: "trino", driver: "trino",
           preset: { url: "trino://user@host:443/catalog/schema" } },
  teradata: { ctype: "sql", dialect: "teradatasql", driver: "teradatasqlalchemy",
              preset: { url: "teradatasql://user:password@host" } },
  db2: { ctype: "sql", dialect: "db2", driver: "ibm_db_sa",
         preset: { url: "db2+ibm_db://user:password@host:50000/database" } },
  hana: { ctype: "sql", dialect: "hana", driver: "sqlalchemy-hana",
          preset: { url: "hana://user:password@host:30015" } },
  vertica: { ctype: "sql", dialect: "vertica", driver: "sqlalchemy-vertica-python",
             preset: { url: "vertica+vertica_python://user:password@host:5433/database" } },
  scylladb: { ctype: "cassandra", preset: { port: "9042" } },
  keyspaces: { ctype: "cassandra",
               preset: { contact_points: "cassandra.us-east-1.amazonaws.com", port: "9142",
                         ssl: "true" } },
  jira_sm: { ctype: "jira",
             preset: { jql: 'projectType = "service_desk" ORDER BY updated DESC' } },
  mongodb: {}, dynamodb: {}, elasticsearch: {}, salesforce: {}, hubspot: {}, stripe: {},
  zendesk: {}, shopify: {}, google_sheets: {}, tableau: {},
};

function humanize(id) {
  return String(id || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (m) => m.toUpperCase());
}

/** Display metadata for a ctype or source name; never throws, never returns null. */
/** True for a source this bundle has a tile (and an icon) for. */
export function knownSource(id) {
  return Boolean(id && META[id]);
}

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
