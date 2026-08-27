"""Prompt-cache layout tests — stable content first, volatile content last.

Proves: the system prompt splits into a STABLE half (role, skill file, rules,
learned rules) and a VOLATILE half (per-user memory, the source's recent
failures); the stable half is byte-identical across users and unchanged by a
new failure on the source (so the provider's cached prefix keeps matching);
the volatile facts still reach the model, after the cache breakpoint; the
Anthropic SystemMessage carries the breakpoint on the first block only; other
providers get one plain string with both halves.

Run from the backend directory:
    python -m pytest tests/test_prompt_cache.py -q
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="studio-prompt-cache-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import agent, db


class _Conn:
    name = "demo"
    dialect = "sqlite"


SCHEMAS = {"sales": [{"name": "region", "type": "TEXT"}, {"name": "revenue", "type": "REAL"}]}


@pytest.fixture(autouse=True)
def _failures(monkeypatch):
    """Deterministic 'recent failures' so the test controls the volatile half."""
    state = {"errors": ["no such column: revenu"]}
    monkeypatch.setattr(db, "recent_failures", lambda source, limit=5: list(state["errors"]))
    return state


def _blocks(memory=("prefers bar charts",)):
    return agent._system_blocks(_Conn(), "*", ["sales"], SCHEMAS, list(memory))


def test_volatile_facts_live_only_in_the_tail():
    stable, volatile = _blocks()
    assert "prefers bar charts" in volatile and "prefers bar charts" not in stable
    assert "no such column: revenu" in volatile and "no such column: revenu" not in stable
    # The big, cache-worthy content stays in the stable half.
    assert "You are Studio" in stable and "Rules:" in stable and "sales" in stable


def test_stable_half_identical_across_users_and_after_new_failures(_failures):
    stable_a, _ = _blocks(memory=("likes West region",))
    stable_b, _ = _blocks(memory=("hates pie charts", "wants EUR"))
    assert stable_a == stable_b                          # different users → same cached prefix

    _failures["errors"].append("syntax error near GROUP")   # a new failure on the source
    stable_c, volatile_c = _blocks(memory=("likes West region",))
    assert stable_c == stable_a                          # prefix unchanged → cache still hits
    assert "syntax error near GROUP" in volatile_c        # ...but the model still learns of it


def test_system_prompt_string_is_stable_plus_volatile():
    stable, volatile = _blocks()
    whole = agent._system_prompt(_Conn(), "*", ["sales"], SCHEMAS, ["prefers bar charts"])
    assert whole == f"{stable}\n\n{volatile}"


def test_breakpoint_marks_only_the_stable_block():
    blocks = agent._cache_blocks("STABLE", "VOLATILE")
    assert [b["text"] for b in blocks] == ["STABLE", "VOLATILE"]
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]
    # No volatile tail → a single cached block, as before.
    assert len(agent._cache_blocks("STABLE")) == 1


def test_anthropic_system_message_carries_the_blocks(monkeypatch):
    pytest.importorskip("langchain_core")   # best-effort path needs LangChain
    monkeypatch.setenv("STUDIO_PROMPT_CACHE", "1")
    msg = agent._apply_prompt_cache("STABLE", "anthropic:claude-sonnet-5", volatile="VOLATILE")
    assert [b["text"] for b in msg.content] == ["STABLE", "VOLATILE"]
    assert msg.content[0]["cache_control"] == {"type": "ephemeral"}


def test_other_providers_and_disabled_cache_get_plain_string(monkeypatch):
    monkeypatch.setenv("STUDIO_PROMPT_CACHE", "1")
    assert agent._apply_prompt_cache("STABLE", "openai:gpt-4o", volatile="VOLATILE") is None
    monkeypatch.setenv("STUDIO_PROMPT_CACHE", "0")
    assert agent._apply_prompt_cache("STABLE", "anthropic:claude-sonnet-5", volatile="VOLATILE") is None

    # _graph then hands the plain concatenation to the graph builder.
    seen = {}
    monkeypatch.setattr(agent, "_build_graph", lambda llm, tools, system: seen.setdefault("system", system))
    agent._graph(None, [], "STABLE", "openai:gpt-4o", volatile="VOLATILE")
    assert seen["system"] == "STABLE\n\nVOLATILE"
