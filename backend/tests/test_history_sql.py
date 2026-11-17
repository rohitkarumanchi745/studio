"""An earlier answer's SQL rides along in the history the next model reads.

The model can change between turns (the composer's model picker), and the
history is all the next model knows of what came before. With only an
answer's text, "now break that down by month" had to be re-derived from
prose; with the SQL the answer ran, any model can build on the exact query.

Locked in:
- each view's SQL, once, in order — named by its source when an answer
  spans several — trimmed to STUDIO_HISTORY_SQL_CHARS;
- the query, never its rows: no number from an answer's data enters history;
- only answers carry it, never questions;
- an answer the reader's role may not see is left out whole — text and SQL —
  exactly as before;
- the agent is told to start a follow-up from that SQL and re-run it.

Run from the backend directory:
    python -m pytest tests/test_history_sql.py -q
"""
import pytest

from app import agent, chat, db, governance

ANA = {"id": "u-hsql-ana", "email": "hsql-ana@studio.test", "role": "analyst", "name": "Ana"}
VIEW = {"id": "u-hsql-view", "email": "hsql-view@studio.test", "role": "viewer", "name": "Vi"}


@pytest.fixture(scope="module", autouse=True)
def _tables():
    db.init_db()
    chat.init_tables()
    governance.init_tables()
    yield


def _answer(**content):
    return {"text": "Revenue is highest in the West.", "source": "demo", "table": "sales",
            "author_role": "analyst", **content}


# ── The SQL an answer ran ───────────────────────────────────────────────

def test_one_query():
    assert chat._answer_sql(_answer(sql="SELECT region, SUM(revenue) FROM sales GROUP BY 1")) == \
        "SELECT region, SUM(revenue) FROM sales GROUP BY 1"
    assert chat._answer_sql(_answer(sql=None)) == ""
    assert chat._answer_sql({"text": "hi"}) == ""


def test_every_view_once_and_named_by_source_when_several():
    one_source = _answer(sql="SELECT 2", panels=[{"sql": "SELECT 1", "source": "demo"},
                                                  {"sql": "SELECT 2", "source": "demo"},
                                                  {"sql": "SELECT 2", "source": "demo"}])
    assert chat._answer_sql(one_source) == "SELECT 1\nSELECT 2"
    across = _answer(source="*", sql="SELECT b FROM t",
                     panels=[{"sql": "SELECT a FROM sales", "source": "demo"},
                             {"sql": "SELECT b FROM t", "source": "snowflake"},
                             {"chart": {"type": "bar"}}])
    assert chat._answer_sql(across) == "[demo] SELECT a FROM sales\n[snowflake] SELECT b FROM t"


def test_long_sql_is_trimmed(monkeypatch):
    monkeypatch.setattr(chat, "HISTORY_SQL_CHARS", 40)
    out = chat._answer_sql(_answer(sql="SELECT " + ", ".join(f"col_{i}" for i in range(50))
                                   + " FROM sales"))
    assert out.endswith(" …") and len(out) <= 42


# ── In the history the next model reads ─────────────────────────────────

def _chat(owner, *answers):
    cid = db.create_conversation(owner["id"], "t")
    for question, answer in answers:
        db.add_message(cid, "user", {"text": question, "source": "demo", "table": "sales",
                                     "author_role": owner["role"]})
        db.add_message(cid, "assistant", answer)
    return cid


def test_the_next_model_sees_the_query_but_not_the_rows():
    cid = _chat(ANA, ("revenue by region", _answer(
        sql="SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region",
        columns=["region", "revenue"], rows=[["West", 98765.43], ["East", 12345.67]],
        model="openai:gpt-4o")))
    history = chat._conversation(cid, ANA, "now by month")[1]
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert history[0]["text"] == "revenue by region"                 # questions carry no SQL
    assert history[1]["text"] == (
        "Revenue is highest in the West.\nSQL this answer ran:\n"
        "SELECT region, SUM(revenue) AS revenue FROM sales GROUP BY region")
    joined = " ".join(h["text"] for h in history)
    assert "98765" not in joined and "12345" not in joined           # never the data
    # A question that happens to carry SQL (a re-ask with its query) still reads as asked.
    cid = db.create_conversation(ANA["id"], "t")
    db.add_message(cid, "user", {"text": "same as before", "source": "demo", "table": "sales",
                                 "author_role": "analyst", "sql": "SELECT region FROM sales"})
    assert chat._conversation(cid, ANA, "next")[1] == [{"role": "user", "text": "same as before"}]


def test_an_answer_the_reader_may_not_see_stays_out_whole():
    secret_sql = "SELECT name, lifetime_value FROM customers ORDER BY 2 DESC"
    cid = _chat(ANA, ("top customers", {"text": "Ada tops it.", "source": "demo",
                                        "table": "customers", "author_role": "analyst",
                                        "sql": secret_sql, "rows": [["Ada", 4200]]}))
    db.share_conversation(cid, VIEW["id"], "edit")
    history = chat._conversation(cid, VIEW, "and?")[1]
    viewer = " ".join(h["text"] for h in history)
    # The question stays (questions carry no data); the answer goes whole.
    assert [h["role"] for h in history] == ["user"]
    assert secret_sql not in viewer and "SQL this answer ran" not in viewer and "Ada" not in viewer
    owner = " ".join(h["text"] for h in chat._conversation(cid, ANA, "and?")[1])
    assert secret_sql in owner


def test_the_agent_is_told_to_build_on_it():
    source = open(agent.__file__).read()
    assert "an earlier answer in this conversation may end with the SQL it\n  ran" in source
    assert "start from that SQL and run it\n  again; never reuse its numbers." in source
