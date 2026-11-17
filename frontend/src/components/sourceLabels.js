// How a source and an activity step are named: the icon tile that stands for
// a source (its connection type, else the engine it speaks), "Any SQL
// database · SQLite"-style labels, and what kind of work a step is. Shared by
// the chat (Chat.jsx) and its side panel (AnswerPanel.jsx).
import { knownSource, metaFor } from "./sourceCatalog";

export const ENGINE_ICON = {
  postgresql: "postgres", postgres: "postgres", mysql: "mysql", mariadb: "mariadb",
  mssql: "mssql", oracle: "oracle", snowflake: "snowflake", databricks: "databricks",
  bigquery: "bigquery", cypher: "neo4j", cql: "cassandra", trino: "trino",
  clickhouse: "clickhouse",
};

/** The tile whose icon stands for a source: its type when this bundle knows
 *  it, else the engine it speaks, else the generic database. */
export function sourceIconId(type, engine) {
  if (type && type !== "sql" && knownSource(type)) return type;
  const byEngine = ENGINE_ICON[String(engine || "").toLowerCase()];
  return byEngine && knownSource(byEngine) ? byEngine : "sql";
}

/** A step's kind — recorded, or read from its words for older steps. */
export function stepKind(step) {
  if (step?.kind) return step.kind;
  const label = String(step?.label || "");
  const rules = [
    [/running SQL|querying/i, "query"], [/reading the (question|columns|schema)/i, "schema"],
    [/custom visual|with D3/i, "visual"], [/rendering .* chart/i, "chart"],
    [/certified|semantic layer|semantic (metric|dimension)/i, "semantic"],
    [/verified query/i, "verified"], [/cached plan/i, "cache"], [/knowledge/i, "docs"],
    [/Aggregator/, "aggregate"], [/planner|planned/i, "plan"],
    [/fanning out|agent\(s\)|child agent/i, "agents"], [/BitNet|frontier/i, "model"],
    [/MCP|tools/i, "tools"], [/DAG|pipeline|platform job/i, "pipeline"],
    [/failed|error|retrying|skipped|abandoned/i, "error"], [/grounded|which source/i, "route"],
    [/finalizing/i, "done"], [/finished/i, "agent"],
  ];
  return rules.find(([re]) => re.test(label))?.[1] || null;
}

const ENGINE_LABEL = {
  sqlite: "SQLite", postgresql: "PostgreSQL", postgres: "PostgreSQL", mysql: "MySQL",
  mariadb: "MariaDB", mssql: "SQL Server", oracle: "Oracle", duckdb: "DuckDB",
  snowflake: "Snowflake", databricks: "Databricks", bigquery: "BigQuery", trino: "Trino",
  clickhouse: "ClickHouse", cql: "CQL", cypher: "Cypher",
};

/** "Any SQL database · SQLite", "Snowflake", "LanceDB": what kind of source this is. */
export function sourceTypeLabel(type, engine) {
  const id = sourceIconId(type, engine);
  const label = metaFor(id).label;
  if (id !== "sql") return label;
  const e = String(engine || "").toLowerCase();
  return e ? `${label} · ${ENGINE_LABEL[e] || e}` : label;
}
