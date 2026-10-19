"""One account, connected once.

Proves, through the real API with the warehouses stood in for:
  - two connections are "the same sign-in" when they sign in to the same place
    as the same identity — the schema each reads, the warehouse it runs on and
    a rotated password do not make a different one — and the fingerprint the
    listing shows reveals no credential;
  - a second connection with a sign-in already here is refused (409) when it
    would read any of the same data, before anything signs in; one that reads
    everything blocks every other; a different schema of a type that cannot
    read everything from one source (Postgres) is not a duplicate;
  - signing in says what the credential is already connected as;
  - "Combine into one" makes one connection read everything and removes the
    others with that sign-in — only once the wider sign-in works, never for a
    type that cannot span, and leaving other sign-ins alone;
  - a legacy duplicate cannot be reconnected.

Run from the backend directory:
    python -m pytest tests/test_one_sign_in.py -q
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from app import connections

HOST = "dbc-ff359528-b118.cloud.databricks.com"
DBX = {"server_hostname": HOST, "http_path": "/sql/1.0/warehouses/abc123",
       "access_token": "dapi-first-token"}
EVERYTHING = {**DBX, "catalog": "", "schema": "*"}
PG = "postgresql://ana:secret@db.example.com:5432/shop"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "one_sign_in.db"))
    monkeypatch.setenv("STUDIO_SECRET", "one-sign-in-secret")
    monkeypatch.setenv("STUDIO_GRAPH_SYNC_TICKER", "0")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    monkeypatch.setenv("STUDIO_WORKER_MODE", "off")
    from app import db as _db
    importlib.reload(_db)
    connections._CACHE.clear()
    import app.main as main
    importlib.reload(main)
    from app import db
    signed_in = []
    fail = {"on": False}

    class Warehouse:
        """Lists a table per schema it reads; everything lists full names."""
        def __init__(self, name, cfg):
            self.cfg = cfg

        def configured(self):
            return True

        def list_tables(self):
            signed_in.append(dict(self.cfg))
            if fail["on"]:
                raise PermissionError("403 Forbidden")
            schema = self.cfg.get("schema") or "public"
            return ["main.sales.orders", "main.finance.ledger"] if schema == "*" else ["orders"]

        def list_namespaces(self):
            signed_in.append(dict(self.cfg))
            if fail["on"]:
                raise PermissionError("403 Forbidden")
            return [{"database": "main", "schema": "sales"}, {"database": "main", "schema": "finance"}]

        def close(self):
            pass

    monkeypatch.setitem(connections.TYPES["databricks"], "build", Warehouse)
    monkeypatch.setitem(connections.TYPES["postgres"], "build", Warehouse)
    monkeypatch.setattr(connections, "_resolves", lambda host, wait=5.0: True)
    with TestClient(main.app) as c:
        tok = c.post("/api/auth/login", json={"email": "admin@studio.local",
                                              "password": "admin123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        ns = type("Env", (), {})()
        ns.c, ns.db, ns.signed_in, ns.fail = c, db, signed_in, fail

        def connect(name, cfg, ctype="databricks", status=201):
            r = c.post("/api/connections", json={"name": name, "ctype": ctype, "config": cfg})
            assert r.status_code == status, r.text
            return r.json()
        ns.connect = connect
        yield ns
    connections._CACHE.clear()


def test_the_same_sign_in_is_where_and_who_not_what_or_how(env):
    key = lambda cfg, ctype="databricks": connections.account_key(ctype, cfg)
    base = key({**DBX, "catalog": "main", "schema": "sales"})
    # What it reads, and the warehouse it runs on, are not the sign-in.
    assert key(EVERYTHING) == base
    assert key({**DBX, "http_path": "/sql/1.0/warehouses/other", "schema": "x"}) == base
    assert key({**DBX, "server_hostname": HOST.upper()}) == base
    # Another token, another workspace: another sign-in.
    assert key({**DBX, "access_token": "dapi-second-token"}) != base
    assert key({**DBX, "server_hostname": "dbc-other.cloud.databricks.com"}) != base
    # Postgres: a rotated password is the same login; another database is not.
    pg = key({"dsn": PG}, "postgres")
    assert key({"dsn": PG.replace("secret", "rotated"), "schema": "sales"}, "postgres") == pg
    assert key({"dsn": PG.replace("/shop", "/crm")}, "postgres") != pg
    assert key({"dsn": PG + "?sslmode=require"}, "postgres") == pg      # an option, not who
    assert key({"dsn": PG.replace("ana:", "bob:")}, "postgres") != pg
    # Safe to show: no credential in it, and meaningless under another secret.
    assert "dapi" not in base and len(base) == 20
    import os
    os.environ["STUDIO_SECRET"] = "another-deployment"
    try:
        assert key({**DBX}) != base
    finally:
        os.environ["STUDIO_SECRET"] = "one-sign-in-secret"


def test_one_that_reads_everything_blocks_every_other_before_signing_in(env):
    made = env.connect("lakehouse", EVERYTHING)
    assert made["everything"] is True and made["namespace"] == "all catalogs"
    assert made["account"] and "dapi" not in json.dumps(made)
    calls = len(env.signed_in)
    for cfg in ({**DBX, "catalog": "main", "schema": "sales"}, EVERYTHING,
                {**EVERYTHING, "http_path": "/sql/1.0/warehouses/other"}):
        r = env.c.post("/api/connections", json={"name": "again", "ctype": "databricks",
                                                 "config": cfg})
        assert r.status_code == 409
        assert r.json()["detail"] == {
            "error": "This sign-in is already connected as “lakehouse”, which reads everything "
                     "it can see — use that source.",
            "existing": "lakehouse", "stage": "duplicate"}
    assert len(env.signed_in) == calls                     # refused before any sign-in
    # Another token is another account.
    env.connect("databricks-ops", {**EVERYTHING, "access_token": "dapi-second-token"})


def test_pinned_sources_of_one_sign_in_may_not_overlap(env):
    env.connect("sales", {**DBX, "catalog": "main", "schema": "sales"})
    r = env.c.post("/api/connections", json={"name": "sales-2", "ctype": "databricks",
                                             "config": {**DBX, "catalog": "main", "schema": "sales"}})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == ("This sign-in already reads main.sales as “sales” "
                                           "— use that source.")
    # Everything would cover it: say how to get there instead.
    r = env.c.post("/api/connections", json={"name": "all", "ctype": "databricks",
                                             "config": EVERYTHING})
    assert r.status_code == 409 and "Combine into one" in r.json()["detail"]["error"]
    # All of the catalog covers it too; another schema does not.
    assert env.c.post("/api/connections", json={"name": "main-all", "ctype": "databricks", "config": {
        **DBX, "catalog": "main", "schema": "*"}}).status_code == 409
    env.connect("finance", {**DBX, "catalog": "main", "schema": "finance"})
    # A disconnected one still counts, and says how to bring it back.
    sales = next(c for c in env.c.get("/api/connections").json() if c["name"] == "sales")
    env.c.post(f"/api/connections/{sales['id']}/disconnect")
    r = env.c.post("/api/connections", json={"name": "sales-3", "ctype": "databricks",
                                             "config": {**DBX, "catalog": "main", "schema": "sales"}})
    assert r.status_code == 409 and r.json()["detail"]["error"].endswith(
        "(It is disconnected: reconnect it.)")
    # Reconnecting it is not a duplicate of itself.
    assert env.c.post(f"/api/connections/{sales['id']}/reconnect").json()["enabled"] is True


def test_postgres_reads_one_schema_per_source_so_only_the_same_schema_is_a_duplicate(env):
    env.connect("shop", {"dsn": PG}, "postgres")
    env.connect("shop-sales", {"dsn": PG, "schema": "sales"}, "postgres")
    r = env.c.post("/api/connections", json={"name": "shop-2", "ctype": "postgres", "config": {
        "dsn": PG.replace("secret", "rotated"), "schema": "public"}})
    assert r.status_code == 409 and "“shop”" in r.json()["detail"]["error"]
    env.connect("crm", {"dsn": PG.replace("/shop", "/crm")}, "postgres")
    # A Postgres source cannot be widened to everything.
    shop = next(c for c in env.c.get("/api/connections").json() if c["name"] == "shop")
    r = env.c.post(f"/api/connections/{shop['id']}/everything")
    assert r.status_code == 400 and "one schema per source" in r.json()["detail"]


def test_signing_in_says_what_the_credential_is_already_connected_as(env):
    env.connect("sales", {**DBX, "catalog": "main", "schema": "sales"})
    out = env.c.post("/api/connections/browse", json={"ctype": "databricks", "config": DBX}).json()
    assert out["ok"] is True
    assert out["connected_as"] == [{"name": "sales", "database": "main", "schema": "sales",
                                    "namespace": "main.sales", "everything": False,
                                    "disconnected": False, "env": False}]
    other = env.c.post("/api/connections/browse", json={
        "ctype": "databricks", "config": {**DBX, "access_token": "dapi-second-token"}}).json()
    assert other["connected_as"] == []
    # Nothing is said for a credential that does not sign in.
    env.fail["on"] = True
    out = env.c.post("/api/connections/browse", json={"ctype": "databricks", "config": DBX}).json()
    assert out["ok"] is False and "connected_as" not in out


def test_combine_into_one_reads_everything_and_removes_the_rest_of_that_sign_in(env):
    # What production had: one sign-in connected three times, one schema each.
    from app import connections as conns
    real = conns._refuse_duplicate
    conns._refuse_duplicate = lambda *a, **k: None            # how the copies came about
    try:
        for name, schema in (("databricks-2-1", "sales"), ("databricks-2-2", "finance"),
                             ("databricks-3", "sales")):
            env.connect(name, {**DBX, "catalog": "main", "schema": schema})
    finally:
        conns._refuse_duplicate = real
    env.connect("ops", {**EVERYTHING, "access_token": "dapi-second-token"})
    listing = {c["name"]: c for c in env.c.get("/api/connections").json()}
    assert len({listing[n]["account"] for n in ("databricks-2-1", "databricks-2-2",
                                                "databricks-3")}) == 1
    assert listing["ops"]["account"] != listing["databricks-3"]["account"]

    # The wider sign-in fails: nothing changes.
    keep = listing["databricks-3"]["id"]
    env.fail["on"] = True
    r = env.c.post(f"/api/connections/{keep}/everything")
    assert r.status_code == 400 and r.json()["detail"].startswith("connection test failed")
    assert len(env.c.get("/api/connections").json()) == 4
    env.fail["on"] = False

    r = env.c.post(f"/api/connections/{keep}/everything")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "databricks-3" and out["everything"] is True
    assert sorted(out["removed"]) == ["databricks-2-1", "databricks-2-2"]
    assert env.signed_in[-1]["schema"] == "*" and env.signed_in[-1]["catalog"] == ""
    assert sorted(c["name"] for c in env.c.get("/api/connections").json()) == ["databricks-3", "ops"]
    stored = connections._decrypt(connections._row_by_name("databricks-3")["config"])
    assert stored == {**EVERYTHING}                          # same token, every catalog
    # Rediscovered whole; the removed ones are gone from the record; audited.
    with env.db.connect() as c:
        jobs = [json.loads(r["payload"]) for r in c.execute(
            "SELECT payload FROM background_jobs WHERE kind='metagraph_discover'").fetchall()]
        assert {"source": "databricks-3", "trigger": "connection_saved"} in jobs
    actions = [(a["action"], a["prompt"]) for a in env.db.list_activity()]
    assert ("connection_read_everything", "databricks-3") in actions
    assert ("connection_delete", "databricks-2-1") in actions
    # And now nothing more can be connected with that sign-in.
    assert env.c.post("/api/connections", json={"name": "again", "ctype": "databricks", "config": {
        **DBX, "catalog": "main", "schema": "finance"}}).status_code == 409


def test_a_legacy_copy_cannot_be_reconnected_and_a_parked_one_cannot_be_widened(env):
    from app import connections as conns
    real = conns._refuse_duplicate
    conns._refuse_duplicate = lambda *a, **k: None
    try:
        env.connect("first", {**DBX, "catalog": "main", "schema": "sales"})
        env.connect("second", {**DBX, "catalog": "main", "schema": "sales"})
    finally:
        conns._refuse_duplicate = real
    b = next(c for c in env.c.get("/api/connections").json() if c["name"] == "second")
    env.c.post(f"/api/connections/{b['id']}/disconnect")
    r = env.c.post(f"/api/connections/{b['id']}/reconnect")
    assert r.status_code == 409 and r.json()["detail"]["existing"] == "first"
    r = env.c.post(f"/api/connections/{b['id']}/everything")
    assert r.status_code == 400 and "reconnect it first" in r.json()["detail"]
    assert env.c.post("/api/connections/nope/everything").status_code == 404


def test_only_an_admin_combines(env):
    made = env.connect("lakehouse", EVERYTHING)
    tok = env.c.post("/api/auth/login", json={"email": "analyst@studio.local",
                                              "password": "analyst123"}).json()["access_token"]
    r = env.c.post(f"/api/connections/{made['id']}/everything",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
