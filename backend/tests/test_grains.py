"""Time grains: prompt word → grain → the dialect's bucket expression."""
from app import grains


def test_grain_detection_and_bucket_expressions():
    assert grains.detect("monthly revenue by region") == "month"
    assert grains.detect("annual spend") == "year"
    assert grains.detect("revenue by region") is None
    assert grains.bucket_expr("sqlite", "d", "month") == "strftime('%Y-%m', d)"
    assert grains.bucket_expr("postgres", "d", "week") == "DATE_TRUNC('week', d)"
    assert grains.bucket_expr("sqlite", "d", "nonsense") is None
    # sqlite has no strftime quarter — it is composed, and recognised again.
    q = grains.bucket_expr("sqlite", "d", "quarter")
    assert "strftime('%Y'" in q and grains.has_bucket(q, "quarter")


def test_has_bucket_does_not_confuse_a_finer_format_for_a_coarser_one():
    """The whole point of the intent check: '%Y-%m-%d' is daily, and matching
    it as a substring of the monthly '%Y-%m' would silence the warning."""
    assert grains.has_bucket("SELECT strftime('%Y-%m', d) FROM t", "month")
    assert not grains.has_bucket("SELECT strftime('%Y-%m-%d', d) FROM t", "month")
    assert grains.has_bucket("SELECT DATE_TRUNC('month', d) FROM t", "month")
    assert grains.has_bucket("SELECT DATE_TRUNC(d, MONTH) FROM t", "month")
    assert grains.has_bucket("SELECT EXTRACT(month FROM d) FROM t", "month")
    assert not grains.has_bucket("SELECT DATE_TRUNC('day', d) FROM t", "month")


def test_agent_still_reads_its_granularity_table_from_grains():
    """agent.py's keyless fallback buckets by the SAME table the grains leaf
    module defines."""
    from app import agent
    assert agent._GRAN_FMT is grains.SQLITE_FMT
    assert agent._GRAN_FMT["month"] == "%Y-%m"
