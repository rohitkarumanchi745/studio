"""Connector interface every data source implements."""


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
