"""Suite-wide environment: run the app in demo mode.

Seed accounts (admin@studio.local etc.) and the ephemeral JWT secret exist only
under STUDIO_DEMO_MODE=1; the suite logs in with those seeds, so the flag is
set before any app module is imported. setdefault keeps an explicit shell
override working, and test_bootstrap.py toggles production mode per test with
monkeypatch.
"""
import os

os.environ.setdefault("STUDIO_DEMO_MODE", "1")
