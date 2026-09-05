"""Governance converges across processes, not just inside the one that applied it.

`PUT /api/governance` writes the document to the shared store and reloads the
process that handled the request. Every OTHER replica — a second uvicorn
worker, the job worker — caches its own parsed copy in module state, so before
the freshness check it kept enforcing the older, more permissive document
indefinitely: tightening a policy simply did not take effect fleet-wide, and a
stale replica went on serving dashboard tiles cached under it.

These tests run two genuinely independent copies of app.governance in one
interpreter — separate _STATE, _FRESH and _ON_CHANGE, one shared database —
which is exactly the shape of two processes. They prove:

  * a document applied by one is enforced by the other after one refresh, in
    both directions, with on_change firing in the process that reloads;
  * version() moves with it, so caches keyed on it (dashboard tiles) miss;
  * the TTL really throttles: no store probe inside it, exactly one after;
  * clearing propagates the same way, back to built-in RBAC;
  * no document anywhere still means built-in RBAC, and a MALFORMED stored
    document still fails closed to built-in RBAC — once, not every TTL;
  * an unreadable store keeps the document already in hand instead of falling
    open.

The semantic layer stores its document exactly the same way and had exactly
the same hole (two replicas compiling "revenue" from two different
definitions), so the last test pins its copy of the fix too.

Run from the backend directory:
    python -m pytest tests/test_governance_propagation.py -q
"""
import importlib.util
import os
import tempfile
import time
import uuid
import warnings

# Throwaway SQLite BEFORE app modules compute their paths.
_TMP = tempfile.mkdtemp(prefix="studio-gov-prop-test-")
os.environ["STUDIO_DB_PATH"] = os.path.join(_TMP, "studio.db")

import pytest

from app import db, governance, rbac

warnings.filterwarnings("ignore")

ADMIN = {"id": "u-gov-prop", "email": "gov-prop@studio.test", "role": "admin"}

TIGHTEN = """
version: 1
roles:
  admin: { sources: "*" }
  analyst: { sources: { demo: "*" } }
  viewer: { sources: { demo: [sales] } }
compliance:
  demo:
    customers:
      mask_columns: [lifetime_value]
"""

LOOSEN = TIGHTEN.replace("viewer: { sources: { demo: [sales] } }",
                         'viewer: { sources: { demo: "*" } }')

MALFORMED = "roles: [this is not a mapping\n  and: not even valid YAML: ["


def _replica():
    """A second, independent copy of app.governance — its own _STATE, _FRESH
    and _ON_CHANGE over the SAME database. That is what a second uvicorn
    worker or the job worker is; module state is the only thing a process
    does not share."""
    spec = importlib.util.find_spec("app.governance")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.load()
    return mod


def _age(mod):
    """Move a process past its refresh TTL without sleeping through it."""
    mod._FRESH["at"] -= 3600


def _wipe():
    c = db._conn()
    c.execute("DELETE FROM governance_docs")
    c.commit()
    c.close()


def _insert(text):
    """Write a document straight to the store — an apply by a process that
    never tells this one, which is the whole problem being fixed."""
    c = db._conn()
    c.execute("INSERT INTO governance_docs (id, yaml, applied_by, applied_at) VALUES (?,?,?,?)",
              (uuid.uuid4().hex, text, "elsewhere@studio.test", time.time()))
    c.commit()
    c.close()


@pytest.fixture(scope="module", autouse=True)
def _store():
    mp = pytest.MonkeyPatch()
    # Another test module may have imported app.db first with its own path.
    mp.setattr(db, "DB_PATH", os.environ["STUDIO_DB_PATH"])
    db.init_db()
    governance.init_tables()
    before_state, before_fresh = dict(governance._STATE), dict(governance._FRESH)
    yield
    # Leave the shared module exactly as it was found: the suite runs every
    # module in one interpreter, and this one points app.db somewhere else.
    _wipe()
    governance._STATE.update(before_state)
    governance._FRESH.update(before_fresh)
    mp.undo()


@pytest.fixture(autouse=True)
def _two_fresh_processes(monkeypatch):
    """Both processes boot on an empty store, on the default 5s TTL."""
    monkeypatch.setenv("STUDIO_GOVERNANCE_REFRESH_S", "5")
    monkeypatch.delenv("STUDIO_GOVERNANCE", raising=False)
    _wipe()
    governance.load()
    yield
    _wipe()
    governance.load()


def _hooked(mod):
    """Register an on_change counter and hand back (list, unregister)."""
    fired = []

    def hook():
        fired.append(1)

    mod.on_change(hook)
    return fired, lambda: mod._ON_CHANGE.remove(hook)


# ── The fix: a policy applied over there is enforced over here ───────────

def test_a_policy_applied_by_one_process_is_enforced_by_the_other_after_a_refresh():
    web, worker = governance, _replica()
    assert web.policies() is None and worker.policies() is None      # built-in RBAC
    assert web.version() == worker.version() == "builtin"
    assert not rbac.can_access("viewer", "demo", "customers")        # built-in: no PII
    fired, unhook = _hooked(web)
    try:
        # A GRANT applied elsewhere reaches this process...
        ok, errors = worker.apply_yaml(LOOSEN, ADMIN)
        assert ok, errors
        assert web.policies() is None and not fired                  # inside its TTL
        _age(web)                                    # one refresh interval later
        assert web.policies()["viewer"] == {"demo": "*"}
        assert rbac.can_access("viewer", "demo", "customers")
        assert web.loaded() and web.active_source() == "database"
        # version() moves with the document, so every cache keyed on it (the
        # dashboard tile cache) misses on THIS replica too, not just the
        # applying one.
        assert web.version() == worker.version() != "builtin"
        assert fired                                 # hooks fire where it reloaded
        loose_version = web.version()

        # ...and so does the TIGHTENING, which is the case that matters: this
        # is the replica that used to serve the permissive document forever.
        ok, errors = worker.apply_yaml(TIGHTEN, ADMIN)
        assert ok, errors
        assert rbac.can_access("viewer", "demo", "customers")   # the bounded window
        _age(web)
        assert not rbac.can_access("viewer", "demo", "customers")
        assert web.version() == worker.version() != loose_version

        # Compliance travelled with it: the mask applies here now.
        cols, rows = web.filter_result("demo", "SELECT name, lifetime_value FROM customers",
                                       ["name", "lifetime_value"], [["Ada", 4200.0]])
        assert rows == [["Ada", "***"]]
    finally:
        unhook()


def test_propagation_runs_the_other_way_too():
    """Symmetry matters: the replica that handled the PUT is not special."""
    web, worker = governance, _replica()
    ok, errors = web.apply_yaml(TIGHTEN, ADMIN)
    assert ok, errors
    assert worker.policies() is None                 # inside the worker's TTL
    _age(worker)
    assert worker.policies()["viewer"] == {"demo": {"sales"}}

    # And a SECOND apply is picked up as well — identity is (id, applied_at),
    # so a later document is never mistaken for the one already loaded.
    ok, errors = web.apply_yaml(LOOSEN, ADMIN)
    assert ok, errors
    _age(worker)
    assert worker.policies()["viewer"] == {"demo": "*"}
    assert worker.version() == web.version()


def test_clearing_the_document_propagates_back_to_builtin_rbac():
    web, worker = governance, _replica()
    assert web.apply_yaml(TIGHTEN, ADMIN)[0]
    _age(worker)
    assert worker.policies() is not None and worker.loaded()

    _wipe()                                          # what DELETE /governance does
    web.reload()
    assert worker.policies() is not None              # still inside the worker's TTL
    _age(worker)
    assert worker.policies() is None and not worker.loaded()
    assert worker.version() == "builtin"


# ── The cost: one indexed single-row read per process per TTL ───────────

def test_the_ttl_throttles_the_store_probe(monkeypatch):
    calls = []
    real_conn = db._conn

    def counting_conn():
        calls.append(1)
        return real_conn()

    monkeypatch.setattr(db, "_conn", counting_conn)

    def hammer(n=40):
        for _ in range(n):
            governance.policies()
            governance.version()
            governance.column_rules("demo", "customers")
            governance.filter_result("demo", "SELECT region FROM sales", ["region"], [["EU"]])

    hammer()
    assert calls == []                       # inside the TTL: not one query

    _age(governance)
    governance.policies()
    assert len(calls) == 1                   # exactly one probe...
    hammer()
    assert len(calls) == 1                   # ...and the next TTL's worth is free

    # A probe that finds nothing new does not reload (no second connection).
    _age(governance)
    governance.version()
    assert len(calls) == 2

    # A probe that DOES find something new pays for the reload as well.
    _insert(TIGHTEN)
    probes = len(calls)
    _age(governance)
    assert governance.policies()["viewer"] == {"demo": {"sales"}}
    assert len(calls) == probes + 2          # the probe, then the reload
    hammer()
    assert len(calls) == probes + 2


def test_a_zero_ttl_checks_every_decision(monkeypatch):
    """The knob is real: STUDIO_GOVERNANCE_REFRESH_S=0 trades the throttle for
    zero convergence lag, for a deployment that wants it."""
    monkeypatch.setenv("STUDIO_GOVERNANCE_REFRESH_S", "0")
    _insert(TIGHTEN)
    assert governance.policies()["viewer"] == {"demo": {"sales"}}   # no ageing needed


# ── Fail-closed behaviour is untouched ──────────────────────────────────

def test_no_document_anywhere_still_means_builtin_rbac():
    _age(governance)
    assert governance.policies() is None
    assert governance.version() == "builtin"
    assert not governance.loaded() and governance.active_source() is None
    assert rbac.can_access("analyst", "demo", "customers")
    # Nothing loaded means nothing filtered, exactly as before.
    assert governance.filter_result("demo", "SELECT name FROM customers",
                                    ["name"], [["Ada"]]) == (["name"], [["Ada"]])


def test_a_malformed_document_applied_elsewhere_still_fails_closed():
    """A broken document must not crash the refresh, must not be enforced, and
    must not be re-loaded (re-firing every cache-invalidation hook) once per
    TTL forever — hence the identity is recorded even when the YAML is not."""
    _insert(MALFORMED)
    _age(governance)
    assert governance.policies() is None              # built-in RBAC, not a crash
    assert not governance.loaded()
    assert governance.version() == "builtin"
    assert "invalid" in (governance.active_source() or "")
    assert rbac.can_access("analyst", "demo", "customers")

    fired, unhook = _hooked(governance)
    try:
        for _ in range(3):
            _age(governance)
            governance.policies()
        assert not fired                              # loaded once, not once per TTL
    finally:
        unhook()

    # A good document applied after it still lands.
    _insert(TIGHTEN)
    _age(governance)
    assert governance.policies()["viewer"] == {"demo": {"sales"}}


def test_an_unreadable_store_keeps_the_document_already_in_hand(monkeypatch):
    """A blip in the state store must never be read as "no document", which
    would fall OPEN to built-in RBAC on every replica at once."""
    worker = _replica()
    assert worker.apply_yaml(TIGHTEN, ADMIN)[0]
    _age(governance)
    assert governance.policies()["viewer"] == {"demo": {"sales"}}

    def down():
        raise RuntimeError("state store unreachable")

    real_conn = db._conn
    monkeypatch.setattr(db, "_conn", down)
    try:
        _age(governance)
        assert governance.policies()["viewer"] == {"demo": {"sales"}}   # held, not dropped
        assert governance.version() != "builtin"
    finally:
        # Restore before this test's fixtures tear down — they need the store.
        monkeypatch.setattr(db, "_conn", real_conn)


# ── The semantic layer stores its document the same way ─────────────────

def test_a_semantic_model_applied_elsewhere_reaches_the_other_process_too():
    """semantic.py mirrors this module's store, so it mirrors the fix. Two
    replicas compiling a metric from two different definitions is the exact
    "nobody can trust a number" failure the semantic layer exists to remove."""
    from app import semantic

    semantic.init_tables()
    before_state, before_fresh = dict(semantic._STATE), dict(semantic._FRESH)
    try:
        c = db._conn()
        c.execute("DELETE FROM semantic_models")
        c.commit()
        c.close()
        semantic.load()
        assert semantic.models_for("demo") == []

        c = db._conn()
        c.execute("INSERT INTO semantic_models (id, yaml, applied_by, applied_at) "
                  "VALUES (?,?,?,?)",
                  (uuid.uuid4().hex, semantic.TEMPLATE, "elsewhere@studio.test", time.time()))
        c.commit()
        c.close()

        assert semantic.models_for("demo") == []          # inside the refresh TTL
        semantic._FRESH["at"] -= 3600                     # one refresh interval later
        models = semantic.models_for("demo")
        assert models and "revenue" in models[0]["metrics"]
    finally:
        c = db._conn()
        c.execute("DELETE FROM semantic_models")
        c.commit()
        c.close()
        semantic._STATE.update(before_state)
        semantic._FRESH.update(before_fresh)
