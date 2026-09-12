"""Suite-wide environment: demo mode and disposable application data.

Seed accounts (admin@studio.local etc.) and the ephemeral JWT secret exist only
under STUDIO_DEMO_MODE=1; the suite logs in with those seeds, so the flag is
set before any app module is imported. setdefault keeps an explicit shell
override working, and test_bootstrap.py toggles production mode per test with
monkeypatch.
"""
import os
import tempfile

os.environ.setdefault("STUDIO_DEMO_MODE", "1")

# This must happen BEFORE test collection imports any app module. Module-local
# env assignments cannot protect a database module imported by an earlier test;
# fixture teardown would otherwise restore the real development DB_PATH.
# Never permit a developer's DATABASE_URL (including one in .env) to send unit
# test writes to Postgres. Postgres-specific tests can opt in with monkeypatch.
_SESSION_DATA = tempfile.mkdtemp(prefix="studio-pytest-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_SESSION_DATA, "studio.db")
os.environ["DATABASE_URL"] = ""
