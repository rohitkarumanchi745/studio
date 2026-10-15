"""The Databricks connect form: caught at once, and never stuck on "connecting…".

A token pasted as the hostname and a browser address as the HTTP path used to
go straight to the driver, which retried for up to 15 minutes. Locked in here:
- the usual mix-ups are refused before any sign-in, per field, with where the
  right value lives — and a message never repeats what was typed (the
  "hostname" may be a secret pasted in the wrong box);
- copying from the browser just works: a workspace URL becomes its hostname, a
  full warehouse URL its /sql/… path — and that tidy form is what gets stored;
- a host that does not resolve fails on a DNS lookup, before the driver;
- a connect-screen sign-in is bounded (about a minute, one attempt) while a
  saved source keeps the driver's defaults for long queries;
- a failed sign-in is named from the HTTP status / exception type (rejected
  token, missing warehouse permission, wrong path, timeout), never echoing a
  submitted value.
"""
import importlib
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app import connections
from app.connectors import databricks_conn

TOKEN_IN_HOST = "dosee96b8fbb7eb4b2516c8e9e81f40f0fba"
SETTINGS_URL = "https://dbc-ff359528-b118.cloud.databricks.com/settings/workspace"
GOOD = {"server_hostname": "dbc-ff359528-b118.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123", "access_token": "dapi-test-token",
        "catalog": "main", "schema": "sales"}


@pytest.fixture()
def no_signin(monkeypatch):
    """Fail the test if anything reaches the connector; DNS always resolves."""
    def build(name, cfg):
        raise AssertionError("a refused form must not reach the driver")
    monkeypatch.setitem(connections.TYPES["databricks"], "build", build)
    monkeypatch.setattr(connections, "_resolves", lambda host, wait=5.0: True)


def _probe(**changes):
    return connections._probe("databricks", {**GOOD, **changes})


def test_the_screenshot_is_refused_at_once_field_by_field(no_signin):
    out = _probe(server_hostname=TOKEN_IN_HOST, http_path=SETTINGS_URL)
    assert out["ok"] is False and set(out["fields"]) == {"server_hostname", "http_path"}
    host, path = out["fields"]["server_hostname"], out["fields"]["http_path"]
    assert host.startswith("This looks like a token or secret, not a host name.")
    assert "Connection details" in host
    # The workspace the pasted settings URL points at is the likely hostname.
    assert "dbc-ff359528-b118.cloud.databricks.com" in host
    assert path.startswith("That is a browser address") and "/sql/1.0/warehouses/" in path
    assert TOKEN_IN_HOST not in json.dumps(out)              # never echoed back

    browsed = connections._browse("databricks", {**GOOD, "server_hostname": TOKEN_IN_HOST})
    assert browsed["ok"] is False and browsed["namespaces"] == []
    assert set(browsed["fields"]) == {"server_hostname"}


@pytest.mark.parametrize("token,says", [
    ("dose0123456789abcdef", "OAuth secret"),
    ("0f8fad5b-d9cb-469f-a165-70867728950e", "client) ID"),
    ("https://dbc-1.cloud.databricks.com", "host or URL"),
    ("dbc-1.cloud.databricks.com", "host or URL"),
])
def test_the_wrong_thing_in_the_token_field_is_named(no_signin, token, says):
    out = _probe(access_token=token)
    assert list(out["fields"]) == ["access_token"] and says in out["fields"]["access_token"]
    assert token not in json.dumps(out)


@pytest.mark.parametrize("host", ["dbc-1", "not a host", "dbc_1.cloud.databricks.com"])
def test_a_non_hostname_is_refused(no_signin, host):
    out = _probe(server_hostname=host)
    assert list(out["fields"]) == ["server_hostname"]
    assert out["fields"]["server_hostname"].startswith("This is not a host name.")


@pytest.mark.parametrize("raw,host,path", [
    ("https://dbc-1a.cloud.databricks.com/", "dbc-1a.cloud.databricks.com", GOOD["http_path"]),
    ("DBC-1A.cloud.databricks.com:443", "dbc-1a.cloud.databricks.com", GOOD["http_path"]),
    ("dbc-1a.cloud.databricks.com/browse?o=1", "dbc-1a.cloud.databricks.com", GOOD["http_path"]),
])
def test_a_pasted_address_becomes_the_hostname(raw, host, path):
    cfg, problems = connections._check_databricks({**GOOD, "server_hostname": raw})
    assert problems == {} and cfg["server_hostname"] == host and cfg["http_path"] == path


def test_a_full_warehouse_url_becomes_its_path_and_fills_a_blank_host():
    url = "https://adb-12.3.azuredatabricks.net/sql/1.0/warehouses/9f8e?o=12"
    cfg, problems = connections._check_databricks(
        {**GOOD, "server_hostname": "", "http_path": url})
    assert problems == {}
    assert cfg["http_path"] == "/sql/1.0/warehouses/9f8e"
    assert cfg["server_hostname"] == "adb-12.3.azuredatabricks.net"
    # Tokens are not required to look like a PAT: Entra ID tokens sign in too.
    assert connections._check_databricks({**GOOD, "access_token": "eyJhbGciOi"})[1] == {}


def test_a_host_that_does_not_resolve_fails_before_the_driver(monkeypatch):
    def build(name, cfg):
        raise AssertionError("an unresolvable host must not reach the driver")
    monkeypatch.setitem(connections.TYPES["databricks"], "build", build)
    seen = []
    monkeypatch.setattr(connections, "_resolves", lambda host, wait=5.0: seen.append(host) or False)
    out = _probe(server_hostname="https://dbc-typo.cloud.databricks.com")
    assert seen == ["dbc-typo.cloud.databricks.com"]
    assert list(out["fields"]) == ["server_hostname"] and "does not resolve" in out["error"]


def test_the_dns_lookup_is_real_and_bounded():
    assert connections._resolves("localhost") is True
    assert connections._resolves("no-such-host.invalid") is False


# ── Bounded sign-in ─────────────────────────────────────────────────────

@pytest.fixture()
def fake_driver(monkeypatch):
    calls = []

    class _Conn:
        def close(self):
            pass

    def connect(**kw):
        calls.append(kw)
        if kw["server_hostname"].startswith("down"):
            raise TimeoutError("timed out")
        return _Conn()

    sql = types.ModuleType("databricks.sql")
    sql.connect = connect
    pkg = types.ModuleType("databricks")
    pkg.__path__ = []
    pkg.sql = sql
    monkeypatch.setitem(sys.modules, "databricks", pkg)
    monkeypatch.setitem(sys.modules, "databricks.sql", sql)
    return calls


def test_a_test_sign_in_is_bounded_and_a_saved_source_is_not(fake_driver):
    probe = connections._DynDatabricks("__probe__", GOOD)
    probe._conn()
    browse = connections._DynDatabricks("__browse__", GOOD)
    browse._conn()
    saved = connections._DynDatabricks("sales_dbx", GOOD)
    saved._conn()
    limits = databricks_conn.PROBE_LIMITS
    assert all(fake_driver[i][k] == v for i in (0, 1) for k, v in limits.items())
    assert limits["_retry_stop_after_attempts_duration"] <= 60
    # The driver waits at least _retry_delay_max between tries: well under a minute.
    assert limits["_retry_delay_max"] < limits["_retry_stop_after_attempts_duration"] / 3
    assert not any(k in fake_driver[2] for k in limits)        # long queries keep defaults


def test_a_test_sign_in_tries_once(fake_driver):
    probe = connections._DynDatabricks("__probe__", {**GOOD, "server_hostname": "down.example.com"})
    with pytest.raises(TimeoutError):
        probe._execute(lambda con: None)
    assert len(fake_driver) == 1


# ── Naming a failed sign-in ─────────────────────────────────────────────

class _RequestError(Exception):
    def __init__(self, msg, code=None, original=None):
        super().__init__(msg)
        self.context = {"http-code": code, "original-exception": original}


@pytest.mark.parametrize("code,says", [
    (401, "rejected the access token"),
    (403, "Can Use"),
    (404, "no SQL warehouse at that HTTP path"),
    (503, "starting"),
])
def test_an_http_status_is_named(code, says):
    exc = _RequestError(f"Error during request: dapi-test-token {code}", code=code)
    assert says in databricks_conn.describe_error(exc)


def test_network_failures_are_named_through_the_chain():
    class NameResolutionError(Exception):
        pass

    class MaxRetryError(Exception):
        def __init__(self, reason):
            super().__init__("pool")
            self.reason = reason

    assert "does not resolve" in databricks_conn.describe_error(
        MaxRetryError(NameResolutionError("x")))
    # The driver's retry policy reports only text: a refused status, or giving up.
    assert "rejected the access token" in databricks_conn.describe_error(_RequestError(
        "Error during request to server", original="Received 401 - UNAUTHORIZED. Confirm"))
    assert "Can Use" in databricks_conn.describe_error(_RequestError(
        "Error during request to server", original="403 codes are not retried"))
    gave_up = databricks_conn.describe_error(_RequestError(
        "Error during request to server. Retry request would exceed Retry policy max retry "
        "duration of 60.0 seconds", original="Retry request would exceed Retry policy max "
        "retry duration of 60.0 seconds"))
    assert gave_up.startswith("The workspace did not accept the sign-in in time")
    try:
        try:
            raise TimeoutError("read timed out")
        except TimeoutError as inner:
            raise _RequestError("wrapped") from inner
    except _RequestError as outer:
        assert "within a minute" in databricks_conn.describe_error(outer)
    assert databricks_conn.describe_error(ValueError("something else")) is None


def test_a_probe_shows_the_named_failure_never_the_token(monkeypatch):
    class _Failing:
        def __init__(self, name, cfg):
            pass

        def configured(self):
            return True

        def list_tables(self):
            raise _RequestError("401 for token dapi-test-token", code=401)

    monkeypatch.setitem(connections.TYPES["databricks"], "build", _Failing)
    monkeypatch.setattr(connections, "_resolves", lambda host, wait=5.0: True)
    out = _probe()
    assert out == {"ok": False, "error": databricks_conn._SAY_STATUS[401]}
    assert "dapi-test-token" not in out["error"]


# ── What gets stored ────────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "dbx_form.db"))
    monkeypatch.setenv("STUDIO_SECRET", "dbx-form-secret")
    monkeypatch.setenv("STUDIO_GRAPH_SYNC_TICKER", "0")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    from app import db as _db
    importlib.reload(_db)
    connections._CACHE.clear()
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        tok = c.post("/api/auth/login", json={"email": "admin@studio.local",
                                              "password": "admin123"}).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        yield c
    connections._CACHE.clear()


def test_the_tidy_config_is_what_gets_saved(client, monkeypatch):
    seen = []

    class _Ok:
        def __init__(self, name, cfg):
            seen.append(cfg)

        def configured(self):
            return True

        def list_tables(self):
            return ["orders"]

    monkeypatch.setitem(connections.TYPES["databricks"], "build", _Ok)
    monkeypatch.setattr(connections, "_resolves", lambda host, wait=5.0: True)
    pasted = {**GOOD, "server_hostname": "https://dbc-ff359528-b118.cloud.databricks.com/",
              "http_path": "https://dbc-ff359528-b118.cloud.databricks.com/sql/1.0/warehouses/abc123"}
    r = client.post("/api/connections", json={"name": "dbx_sales", "ctype": "databricks",
                                              "config": pasted})
    assert r.status_code == 201, r.text
    stored = connections._decrypt(connections._row_by_name("dbx_sales")["config"])
    assert stored["server_hostname"] == GOOD["server_hostname"]
    assert stored["http_path"] == GOOD["http_path"]
    assert seen[-1]["http_path"] == GOOD["http_path"]

    bad = client.post("/api/connections", json={"name": "dbx_bad", "ctype": "databricks",
                                                 "config": {**GOOD, "server_hostname": TOKEN_IN_HOST}})
    assert bad.status_code == 400
    detail = bad.json()["detail"]
    assert detail["error"].startswith("connection test failed: This looks like a token")
    assert list(detail["fields"]) == ["server_hostname"]
    assert TOKEN_IN_HOST not in bad.text
    # Replacing the built-in source refuses the same way, field by field.
    env = client.put("/api/connections/env/databricks",
                     json={"config": {**GOOD, "http_path": SETTINGS_URL}})
    assert env.status_code == 400 and list(env.json()["detail"]["fields"]) == ["http_path"]
    # The /test endpoint returns the per-field problems for the form.
    t = client.post("/api/connections/test", json={"ctype": "databricks",
                                                   "config": {**GOOD, "http_path": SETTINGS_URL}})
    assert t.status_code == 200 and list(t.json()["fields"]) == ["http_path"]
