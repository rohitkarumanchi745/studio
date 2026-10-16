"""The agent can read every table on a source, not just the first ten.

A whole-source chat used to load the columns of the first ten tables in list
order and show every other table as "schema not loaded — inspect before use",
with no way to inspect it: on a wide source the agent guessed column names
for the very table a question (or a join) needed. Locked in here:
- describe_table: the agent reads any table this source lists for the user —
  RBAC-checked, governed exactly like the catalog's schema endpoint, never a
  guessed name (that would step outside the source's pinned namespace), at
  most ten per call, cached for the turn;
- focus tables: a question also brings in the columns of the tables it names
  or shares terms with, and a follow-up those its earlier SQL used — picked
  by name, so no warehouse read for tables with no evidence;
- they go in the volatile half of the prompt: the cached prefix (the skill
  file) is identical whatever the question;
- the prompt and the skill file say which tables have no columns listed, and
  to call describe_table rather than guess.
"""
import json
import os
import tempfile
import types

_TMP = tempfile.mkdtemp(prefix="studio-schema-beyond-ten-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import agent, chat, connections, db, governance, matching, qcache, semantic, skills
from app import router as model_router
from app.connectors.demo import DemoConnector, seed

ANA = {"id": "u-sb-ana", "email": "sb-ana@studio.test", "role": "analyst", "name": "Ana"}
ADMIN = {"id": "u-sb-admin", "email": "sb-admin@studio.test", "role": "admin", "name": "Ad"}
VIEWER = {"id": "u-sb-view", "email": "sb-view@studio.test", "role": "viewer", "name": "Vi"}
SALES_SQL = "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY 1"
GOV_YAML = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*" } }
  viewer: { sources: { demo: [sales, web_traffic] } }
compliance:
  demo:
    customers:
      deny_columns: [name]
    qualtrics_nps:
      deny_columns: [difficulty_driver]
  dbx_all:
    studio_dev.studio_qa.dim_customers:
      deny_columns: [email]
"""


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    governance.init_tables()
    seed()
    yield


@pytest.fixture(autouse=True)
def _governance():
    governance._set(GOV_YAML, "test")
    yield
    governance._STATE.update(doc=None, yaml="", source=None)


@pytest.fixture()
def reads(monkeypatch):
    """Every column read the demo connector serves, in order."""
    seen = []
    original = DemoConnector.get_schema

    def get_schema(self, table):
        seen.append(table)
        return original(self, table)

    monkeypatch.setattr(DemoConnector, "get_schema", get_schema)
    return seen


TABLES = DemoConnector().list_tables()
FIRST_TEN = TABLES[:10]
LATER = TABLES[10:]          # qualtrics_nps, sales, sprinklr_social, web_traffic, …


def test_the_demo_source_already_has_tables_past_the_first_ten():
    assert len(TABLES) > 10
    assert "sales" in LATER and "web_traffic" in LATER


# ── focus_tables: picked by name, never by guesswork ──────────────────────

def test_a_question_brings_in_the_tables_it_names_or_shares_terms_with():
    tables = ["studio_dev.studio_qa.dim_customers", "studio_dev.studio_qa.fact_payments",
              "studio_dev.retail_qa.retail_orders", "studio_dev.studio_qa.fact_events"]
    # Named outright, by its last part or in full.
    assert matching.focus_tables("count rows in fact_events", tables) == \
        ["studio_dev.studio_qa.fact_events"]
    assert matching.focus_tables("join studio_qa.fact_payments to customers", tables)[0] == \
        "studio_dev.studio_qa.fact_payments"
    # Shares a term with the table's own name ("payments" → fact_payments).
    assert matching.focus_tables("payments by method and customer", tables) == \
        ["studio_dev.studio_qa.dim_customers", "studio_dev.studio_qa.fact_payments"]
    # No evidence, nothing loaded; the limit holds.
    assert matching.focus_tables("top 10 by amount", tables) == []
    assert len(matching.focus_tables("customers payments orders events", tables, limit=2)) == 2


def test_a_follow_up_keeps_the_tables_its_earlier_sql_used_but_sql_words_do_not_count():
    tables = ["ecommerce_orders", "sales", "web_traffic"]
    earlier = "SELECT region, SUM(revenue) FROM sales GROUP BY 1 ORDER BY 2 DESC"
    assert matching.focus_tables("and by month?", tables, context=earlier) == ["sales"]
    # ORDER BY in the SQL is not a mention of the orders table.
    assert "ecommerce_orders" not in matching.focus_tables("and by month?", tables,
                                                           context=earlier)
    # The question outranks the history; ties keep the given order.
    assert matching.focus_tables("web_traffic visits", tables, context=earlier)[0] == "web_traffic"


# ── describe_for: the catalog's rules, nothing more ──────────────────────

def test_a_listed_table_is_described_with_governed_columns(reads):
    conn = DemoConnector()
    out = agent.describe_for(ANA, conn, TABLES, "QUALTRICS_NPS")     # any case
    assert out["table"] == "qualtrics_nps"                           # the listed spelling
    names = [c["name"] for c in out["columns"]]
    assert "nps" in names and "difficulty_driver" not in names       # denied by governance
    assert reads == ["qualtrics_nps"]


def test_a_name_the_source_does_not_list_never_reaches_the_warehouse(reads):
    conn = DemoConnector()
    for guess in ("other_schema.sales", "information_schema.tables", "sales; DROP TABLE x"):
        assert "error" in agent.describe_for(ANA, conn, TABLES, guess)
    # A viewer's list is their RBAC list: customers is not on it.
    viewer_tables = ["sales", "web_traffic"]
    assert "error" in agent.describe_for(VIEWER, conn, viewer_tables, "customers")
    # Even a stale list cannot widen RBAC.
    assert agent.describe_for(VIEWER, conn, TABLES, "customers") == {"error": "no access"}
    assert reads == []


def test_a_very_wide_table_is_cut_short_with_a_note():
    class Wide(DemoConnector):
        def get_schema(self, table):
            return [{"name": f"c{i}", "type": "INTEGER"} for i in range(450)]
    out = agent.describe_for(ANA, Wide(), ["sales"], "sales")
    assert len(out["columns"]) == agent.DESCRIBE_MAX_COLUMNS
    assert out["note"] == f"first {agent.DESCRIBE_MAX_COLUMNS} of 450 columns"


# ── the describe_table tool inside a turn ────────────────────────────────

class _Msg:
    type = "ai"

    def __init__(self, text):
        self.content = text


def _turn_with_tool(monkeypatch, calls, schemas, focus=None, connector=None, tables=TABLES,
                    user=ANA):
    """run_agent with a scripted graph that only calls describe_table."""
    monkeypatch.setattr(agent, "llm_available", lambda *a, **k: True)
    monkeypatch.setattr(agent, "make_llm", lambda spec, user=None, **kw: object())
    monkeypatch.setattr(agent, "mcp_servers", lambda user=None: {})
    seen = {}

    def fake_graph(llm, tools, system, spec, volatile=None):
        seen["tools"] = [getattr(t, "name", getattr(t, "__name__", "")) for t in tools]
        seen["system"], seen["volatile"] = system, volatile
        describe = next(t for t in tools if getattr(t, "name", "") == "describe_table")

        class G:
            def invoke(self, state, config=None):
                seen["replies"] = [json.loads(describe.invoke({"tables": c})) for c in calls]
                return {"messages": [_Msg("done")]}
        return G()

    monkeypatch.setattr(agent, "_graph", fake_graph)
    agent.run_agent("revenue by region", connector or DemoConnector(), "*", tables, schemas, [],
                    user, focus_schemas=focus)
    return seen


def test_the_agent_can_read_any_listed_table_and_each_is_read_once(monkeypatch, reads):
    conn = DemoConnector()
    schemas = {t: conn.get_schema(t) for t in FIRST_TEN}
    reads.clear()
    seen = _turn_with_tool(monkeypatch, [["sales", "ads_performance"], ["SALES"],
                                         ["nope"], [f"t{i}" for i in range(12)]], schemas)
    assert "describe_table" in seen["tools"]
    first, again, nope, many = seen["replies"]
    assert [c["name"] for c in first["tables"][0]["columns"]][:3] == \
        ["order_id", "order_date", "region"]
    assert first["tables"][1]["table"] == "ads_performance"            # already in the prompt
    assert again["tables"][0]["table"] == "sales"
    assert reads == ["sales"]                                          # one read for the turn
    assert "error" in nope["tables"][0]
    assert len(many["tables"]) == agent.DESCRIBE_MAX_TABLES and "call again" in many["note"]


def test_a_focus_table_is_in_the_volatile_prompt_only_and_needs_no_read(monkeypatch, reads):
    conn = DemoConnector()
    schemas = {t: conn.get_schema(t) for t in FIRST_TEN}
    focus = {"sales": conn.get_schema("sales")}
    reads.clear()
    seen = _turn_with_tool(monkeypatch, [["sales"]], schemas, focus)
    assert "sales: order_id" in seen["volatile"]
    assert "sales: order_id" not in seen["system"]
    assert seen["replies"][0]["tables"][0]["table"] == "sales" and reads == []


def test_the_cached_prefix_is_the_same_whatever_the_question_brings_in():
    conn = DemoConnector()
    schemas = {t: conn.get_schema(t) for t in FIRST_TEN}
    a = agent._system_blocks(conn, "*", TABLES, schemas, [], None,
                             {"sales": conn.get_schema("sales")})
    b = agent._system_blocks(conn, "*", TABLES, schemas, [], None,
                             {"web_traffic": conn.get_schema("web_traffic")})
    assert a[0] == b[0]                                   # stable half: byte for byte
    assert "sales: order_id" in a[1] and "web_traffic: day" in b[1]
    assert f"Columns are listed for 10 of {len(TABLES)} tables: call describe_table" in a[0]
    assert "Never guess a column name" in a[0]


def test_the_skill_file_points_at_describe_table_and_was_rebuilt():
    conn = DemoConnector()
    schemas = {t: conn.get_schema(t) for t in FIRST_TEN}
    md = skills._build(conn, "analyst", TABLES, schemas, "fp")
    assert "### sales\n  (columns not listed — call describe_table before using it)" in md
    assert "schema not loaded" not in md and "call describe_table first" in md
    # The template is part of the fingerprint, so files cached with the old
    # wording are rebuilt rather than served.
    before = skills.fingerprint("sqlite", TABLES, schemas)
    skills.TEMPLATE_VERSION += 1
    try:
        assert skills.fingerprint("sqlite", TABLES, schemas) != before
    finally:
        skills.TEMPLATE_VERSION -= 1


# ── through a whole chat turn ────────────────────────────────────────────

ANSWER = {"text": "West leads.", "sql": SALES_SQL, "columns": ["region", "revenue"],
          "rows": [["West", 120]], "chart": {"type": "bar", "x": "region", "y": ["revenue"]},
          "mode": "agent", "model": "anthropic:test", "email": None, "errors": [],
          "panels": [{"sql": SALES_SQL, "columns": ["region", "revenue"], "rows": [["West", 120]],
                      "chart": {"type": "bar", "x": "region", "y": ["revenue"]}}]}


@pytest.fixture()
def turns(monkeypatch):
    calls = []

    def fake_run_agent(**kw):
        calls.append(kw)
        return {**ANSWER, "panels": [dict(p) for p in ANSWER["panels"]]}

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    monkeypatch.setattr(semantic, "answer", lambda *a, **k: None)
    monkeypatch.setattr(qcache, "lookup", lambda *a, **k: None)
    monkeypatch.setattr(qcache, "store", lambda *a, **k: None)
    monkeypatch.setattr(model_router, "choose", lambda *a, **k: ("frontier", None))
    return calls


def test_a_whole_source_question_about_a_later_table_gets_its_columns(turns, reads):
    out = chat.ask(chat.Ask(prompt="revenue by region from sales", source="demo", table="*"),
                   user=ANA)
    kw = turns[0]
    assert "sales" not in kw["schemas"] and set(kw["schemas"]) == set(FIRST_TEN)
    assert list(kw["focus_schemas"]) == ["sales"]
    assert [c["name"] for c in kw["focus_schemas"]["sales"]][:3] == \
        ["order_id", "order_date", "region"]
    # A follow-up that names no table keeps the one its earlier SQL used.
    chat.ask(chat.Ask(prompt="and by month?", source="demo", table="*",
                      conversation_id=out["conversation_id"]), user=ANA)
    assert list(turns[1]["focus_schemas"]) == ["sales"]
    # The cached prefix did not move between the two questions.
    assert turns[0]["skill_md"] == turns[1]["skill_md"]


def test_focus_columns_are_governed_and_nothing_is_read_without_evidence(
        turns, reads, monkeypatch):
    chat.ask(chat.Ask(prompt="nps by survey in qualtrics", source="demo", table="*"), user=ANA)
    focus = turns[0]["focus_schemas"]
    assert list(focus) == ["qualtrics_nps"]
    assert "difficulty_driver" not in [c["name"] for c in focus["qualtrics_nps"]]
    # What was read before the agent ran (the matched-tables chips read more
    # afterwards, as they always have).
    before_agent = []
    fake = agent.run_agent
    monkeypatch.setattr(agent, "run_agent", lambda **kw: (before_agent.extend(reads), fake(**kw))[1])
    reads.clear()
    chat.ask(chat.Ask(prompt="what changed last week?", source="demo", table="*"), user=ANA)
    assert turns[1]["focus_schemas"] == {}
    assert sorted(before_agent) == sorted(FIRST_TEN)     # the usual ten, no more


def test_the_preloaded_columns_are_governed_too(turns):
    chat.ask(chat.Ask(prompt="list customers", source="demo", table="customers"), user=ANA)
    assert "name" not in [c["name"] for c in turns[0]["schemas"]["customers"]]


def test_focus_can_be_switched_off(turns, monkeypatch):
    monkeypatch.setattr(chat, "FOCUS_TABLES", 0)
    chat.ask(chat.Ask(prompt="revenue from sales", source="demo", table="*"), user=ANA)
    assert turns[0]["focus_schemas"] == {}


# ── Databricks, every schema: full names, as on a real workspace ─────────
# The real connector code against a scripted warehouse: twelve tables in one
# schema sort ahead of the ones the questions are about.

DBX_TABLES = ([("studio_dev", "analytics", f"kpi_{i:02d}") for i in range(1, 13)]
              + [("studio_dev", "retail_qa", "retail_orders"),
                 ("studio_dev", "studio_qa", "dim_customers"),
                 ("studio_dev", "studio_qa", "fact_payments")])
DBX_COLUMNS = {
    "`studio_dev`.`studio_qa`.`fact_payments`": [("payment_id", "bigint"), ("customer_id", "bigint"),
                                                 ("method", "string"), ("amount", "double")],
    "`studio_dev`.`studio_qa`.`dim_customers`": [("customer_id", "bigint"), ("segment", "string"),
                                                 ("email", "string")],
}


class _Cur:
    def __init__(self):
        self.rows, self.sql = [], []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if "information_schema.tables" in sql:
            self.rows = list(DBX_TABLES)
        elif sql.startswith("DESCRIBE TABLE "):
            self.rows = DBX_COLUMNS.get(sql[len("DESCRIBE TABLE "):], [("id", "bigint")])
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchall(self):
        return self.rows

    def described(self):
        return [s[len("DESCRIBE TABLE "):] for s in self.sql if s.startswith("DESCRIBE TABLE ")]


def _databricks():
    conn = connections._DynDatabricks("dbx_all", {
        "server_hostname": "dbc-1.cloud.databricks.com", "http_path": "/sql/1.0/warehouses/a",
        "access_token": "dapi-x", "catalog": "studio_dev", "schema": "*"})
    cur = _Cur()
    conn._conn = lambda: types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)
    return conn, cur


def test_databricks_tables_past_the_first_ten_are_read_by_full_name(monkeypatch):
    conn, cur = _databricks()
    tables = conn.list_tables()
    assert len(tables) == 15 and "studio_dev.studio_qa.fact_payments" not in tables[:10]
    schemas = {t: [] for t in tables[:10]}
    seen = _turn_with_tool(monkeypatch, [["studio_dev.studio_qa.fact_payments"],
                                         ["fact_payments"], ["studio_dev.other.fact_payments"],
                                         ["studio_dev.studio_qa.dim_customers"]],
                           schemas, connector=conn, tables=tables, user=ADMIN)
    full, short, other, customers = seen["replies"]
    assert [c["name"] for c in full["tables"][0]["columns"]] == \
        ["payment_id", "customer_id", "method", "amount"]
    # A short name, or a schema the source does not list, is never sent.
    assert "error" in short["tables"][0] and "error" in other["tables"][0]
    assert cur.described() == ["`studio_dev`.`studio_qa`.`fact_payments`",
                               "`studio_dev`.`studio_qa`.`dim_customers`"]
    # Governance applies to full names too.
    assert [c["name"] for c in customers["tables"][0]["columns"]] == ["customer_id", "segment"]


def test_a_databricks_question_brings_in_its_tables_through_a_whole_turn(turns, monkeypatch):
    conn, cur = _databricks()
    monkeypatch.setattr(chat, "connector_or_400", lambda source: conn)
    chat.ask(chat.Ask(prompt="payments by method and customer segment", source="dbx_all",
                      table="*"), user=ADMIN)
    kw = turns[0]
    assert set(kw["focus_schemas"]) == {"studio_dev.studio_qa.dim_customers",
                                        "studio_dev.studio_qa.fact_payments"}
    assert [c["name"] for c in kw["focus_schemas"]["studio_dev.studio_qa.dim_customers"]] == \
        ["customer_id", "segment"]                                     # email denied
    assert "write EVERY table" in kw["skill_md"]
    assert ("### studio_dev.studio_qa.fact_payments\n  (columns not listed — call describe_table"
            in kw["skill_md"])
