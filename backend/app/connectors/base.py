"""Connector interface every data source implements."""
import datetime
import decimal


def to_jsonable(v):
    """Coerce a warehouse cell value to something JSON-serializable, so results
    survive the tile cache (JSON round-trip) and the API response. Real
    warehouses (Databricks / Snowflake) return `date`/`datetime`/`Decimal`/
    `bytes` that FastAPI's encoder rejects; SQLite already returns str/num.
    Decimals become numbers (charts need numeric columns to stay numeric);
    dates/times become ISO strings."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        try:
            return bytes(v).decode("utf-8", "replace")
        except Exception:
            return str(v)
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): to_jsonable(x) for k, x in v.items()}
    return str(v)


def jsonify_rows(rows):
    """Apply to_jsonable across a result set (list of row lists)."""
    return [[to_jsonable(v) for v in r] for r in rows]


class Connector:
    name = "base"
    dialect = "ansi"

    def configured(self):
        """True when credentials/config for this source are present."""
        raise NotImplementedError

    def list_tables(self):
        """Return a list of table names."""
        raise NotImplementedError

    def get_schema(self, table):
        """Return [{"name": col, "type": sqltype}, ...] for a table."""
        raise NotImplementedError

    def run_query(self, sql):
        """Execute a (pre-validated) SELECT. Return (columns, rows)."""
        raise NotImplementedError

    # ── High-risk capabilities (gated by the supervisor + human-in-the-loop) ──
    # These break the read-only invariant, so they must never be reached
    # except through supervisor.py, which requires human approval first.

    def run_script(self, sql):
        """Execute a write/DDL statement against this environment. Default:
        unsupported — a connector must opt in."""
        raise NotImplementedError(f"{self.name} does not support writes")

    def submit_spark_job(self, config):
        """Submit a Spark / compute job. Return a run handle. Default:
        unsupported."""
        raise NotImplementedError(f"{self.name} does not support Spark jobs")

    def rollback(self, handle):
        """Undo a deployed run after a terminal failure (cancel the run, revert
        the target). Called only by the safe-production flow on a run that keeps
        failing. Default: unsupported — the flow then flags manual intervention."""
        raise NotImplementedError(f"{self.name} does not support rollback")
