"""Migration CLI — the explicit release step when STUDIO_AUTO_MIGRATE=0.

    python -m app.migrate status   # current version + what is pending
    python -m app.migrate up       # apply pending migrations, one per line

Reads the same environment as the app (DATABASE_URL / STUDIO_DB_PATH) and
loads backend/.env first, exactly like main.py, so a shell that can run the
server can run this. It touches ONLY schema_migrations and the columns the
migrations add — it never creates the baseline tables; those come from the
app's own startup (init_tables), which is why an old database must have
booted at least once before `up` has anything to do.
"""
import sys
from pathlib import Path

from dotenv import load_dotenv

# Same .env as main.py, and BEFORE importing db, which reads DATABASE_URL and
# STUDIO_DB_PATH at import time.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import db, migrations  # noqa: E402

USAGE = "usage: python -m app.migrate status|up"


def _store():
    return "postgres" if db.IS_PG else f"sqlite ({db.DB_PATH})"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd not in {"status", "up"}:
        print(USAGE, file=sys.stderr)
        return 2
    st = migrations.status()
    print(f"store:   {_store()}")
    print(f"current: {st['current']}")
    if cmd == "status":
        if st["pending"]:
            print("pending:")
            for v, n in st["pending"]:
                print(f"  {v}  {n}")
        else:
            print("pending: none")
        return 0
    applied = migrations.apply_pending()
    if not applied:
        print("nothing to apply")
    for v, n in applied:
        print(f"applied {v}  {n}")
    print(f"current: {migrations.status()['current']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
