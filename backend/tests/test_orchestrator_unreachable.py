"""A cross-source answer names the sources it could not reach.

accessible_sources dropped a source whose listing failed, silently: the answer
across sources then read as complete with a whole database missing. Locked in
here:
- an unreachable source the user may query is off the roster (nothing queries
  it) but recorded, and the answer says it was left out — or, when nothing
  else is reachable, says that instead of "No accessible data sources.";
- a source the role cannot use, or one that is not configured, is never named:
  the note must not reveal sources the user has no access to;
- a plain list roster still works.
"""
import pytest

from app import agent, orchestrator, rbac, skills


class _Conn:
    def __init__(self, name, broken=False):
        self.name, self.dialect, self.broken = name, "ansi", broken

    def list_tables(self):
        if self.broken:
            raise ConnectionError("warehouse asleep")
        return [f"{self.name}_orders"]

    def get_schema(self, table):
        return [{"name": "id", "type": "INTEGER"}]


USER = {"id": "u-orch", "role": "analyst"}
CONNECTORS = {"postgres": _Conn("postgres"), "snowflake": _Conn("snowflake", broken=True),
              "databricks": _Conn("databricks", broken=True), "payroll": _Conn("payroll", broken=True),
              "spare": _Conn("spare", broken=True)}


@pytest.fixture(autouse=True)
def _sources(monkeypatch):
    metas = [{"name": n, "configured": n != "spare"} for n in CONNECTORS]
    monkeypatch.setattr(orchestrator, "all_sources", lambda: metas)
    monkeypatch.setattr(orchestrator, "get_connector", lambda name: CONNECTORS[name])
    # payroll exists but this role may not use it.
    monkeypatch.setattr(rbac, "allowed_sources",
                        lambda role: {"postgres", "snowflake", "databricks", "spare"})
    monkeypatch.setattr(rbac, "allowed_tables", lambda role, source, tables: list(tables))
    monkeypatch.setattr(skills, "get_skill", lambda *a, **k: "")
    monkeypatch.setenv("STUDIO_AGENT_GRAPH", "0")


def test_an_unreachable_source_is_off_the_roster_but_recorded():
    roster = orchestrator.accessible_sources(USER)
    assert [s["connector"].name for s in roster] == ["postgres"]
    assert roster.unreachable == ["databricks", "snowflake"]
    # Never named: a source the role cannot use (payroll), or one that is not
    # configured (spare) — even though both would fail to list too.
    assert "payroll" not in roster.unreachable and "spare" not in roster.unreachable


def test_the_answer_says_which_sources_it_left_out(monkeypatch):
    monkeypatch.setattr(agent, "run_agent", lambda *a, **k: {
        "text": "Postgres has 42 orders.", "sql": "SELECT 1", "columns": [], "rows": [],
        "chart": None, "panels": [], "errors": [], "mode": "agent"})
    out = orchestrator.run_orchestrated("orders everywhere", USER, [])
    assert out["text"] == ("Postgres has 42 orders.\n\nNote: couldn't reach databricks, "
                           "snowflake just now, so this answer leaves them out.")
    assert out["unreachable_sources"] == ["databricks", "snowflake"]
    assert out["agents_used"] == ["postgres"]


def test_with_nothing_reachable_it_says_so_instead_of_no_sources(monkeypatch):
    CONNECTORS["postgres"].broken = True
    try:
        out = orchestrator.run_orchestrated("orders everywhere", USER, [])
    finally:
        CONNECTORS["postgres"].broken = False
    assert out["text"].startswith("Couldn't reach databricks, postgres, snowflake just now")
    assert "No accessible data sources" not in out["text"]
    assert out["unreachable_sources"] == ["databricks", "postgres", "snowflake"]


def test_a_plain_list_roster_still_works(monkeypatch):
    monkeypatch.setattr(agent, "run_agent", lambda *a, **k: {
        "text": "ok", "sql": None, "columns": [], "rows": [], "chart": None,
        "panels": [], "errors": [], "mode": "agent"})
    roster = list(orchestrator.accessible_sources(USER))
    out = orchestrator.run_orchestrated("q", USER, [], sources=roster)
    assert out["text"] == "ok" and "unreachable_sources" not in out
    assert orchestrator.run_orchestrated("q", USER, [], sources=[])["text"] == \
        "No accessible data sources."
