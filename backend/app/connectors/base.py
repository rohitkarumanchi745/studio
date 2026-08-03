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
