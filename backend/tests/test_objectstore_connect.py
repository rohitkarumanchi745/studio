"""S3, Azure Blob and GCS set up on the Integrations screen (connections.py
TYPES "s3" / "azure_blob" / "gcs", read through connectors/objectstore.py).

Locked in here:
- the form check tidies what has one answer (the format from the files'
  extension, a name, an endpoint's scheme) and marks, per field, what is
  wrong — before anything signs in, and never pointing the server at a cloud
  metadata address (an S3 endpoint, an Azure connection string's endpoints);
- a failed read says a FIXED sentence, on the field it points at — the
  driver's own text (which may hold a key or a signed URL) never comes back;
- a connection carries its own keys and first dataset: it reads those, not
  the deployment's environment, and the environment's bootstrap datasets stay
  the built-ins';
- the keys are a sign-in: the same keys twice is refused, the deployment's
  own keys included, with the way to add files to the source already there;
- more files are added to a store from its row, read with its keys first
  (check=true); only registry rows can be removed, and removing the connection
  removes them;
- the built-in s3 source can be set up from the same form.

No network: reading files is stubbed at ObjectStoreConnector.get_schema
(DuckDB's binder is what reads them). test_live_s3_round_trip runs the real
DuckDB read against an S3 emulator when STUDIO_TEST_S3_ENDPOINT names one
(e.g. `moto_server -p 5055`, then STUDIO_TEST_S3_ENDPOINT=127.0.0.1:5055).
"""
import importlib
import json
import os
import urllib.request

import pytest
from fastapi.testclient import TestClient

from app.connectors import objectstore

KEYS = {"access_key_id": "AKIASTUDIOTEST", "secret_access_key": "wJalr-very-secret-key"}
RIDES = {**KEYS, "path": "s3://trips/rides/*.parquet", "dataset": "rides"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_DB_PATH", str(tmp_path / "stores.db"))
    monkeypatch.setenv("STUDIO_SECRET", "objectstore-connect-secret")
    monkeypatch.setenv("STUDIO_GRAPH_SYNC_TICKER", "0")
    monkeypatch.setenv("STUDIO_AUTOPILOT_TICKER", "0")
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_REGION",
              "STUDIO_S3_ENDPOINT", "STUDIO_OBJECT_DATASETS", "GCS_HMAC_KEY_ID", "GCS_HMAC_SECRET",
              "AZURE_STORAGE_CONNECTION_STRING", "AZURE_STORAGE_ACCOUNT",
              "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "STUDIO_LLM", "STUDIO_LLM_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import db as _db
    importlib.reload(_db)
    from app import connections
    connections._CACHE.clear()
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c
    connections._CACHE.clear()


def _login(c, email="admin@studio.local", password="admin123"):
    tok = c.post("/api/auth/login",
                 json={"email": email, "password": password}).json()["access_token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


@pytest.fixture()
def reads(monkeypatch):
    """Stub the read: every get_schema call is recorded, and the files' path
    decides the outcome, worded the way DuckDB words it."""
    seen = []

    def get_schema(self, table):
        d = next(x for x in self._datasets() if x["name"] == table)
        seen.append((self.name, self.kind, d["uri"], d["format"]))
        uri = d["uri"]
        if "ghost" in uri:
            raise RuntimeError(f'IO Error: No files found that match the pattern "{uri}"')
        if "private" in uri:
            raise RuntimeError("HTTP Error: HTTP GET error on 'https://private.s3.amazonaws.com/x' "
                               "(HTTP 403) AccessDenied X-Amz-Signature=abc123SIGNED")
        if d["format"] == "parquet" and uri.endswith(".csv"):
            raise RuntimeError("Invalid Input Error: No magic bytes found at end of file "
                               f"'{uri}'")
        return [{"name": "line", "type": "VARCHAR"}, {"name": "riders", "type": "BIGINT"}]

    monkeypatch.setattr(objectstore.ObjectStoreConnector, "get_schema", get_schema)
    return seen


# ── The form check: before anything signs in ─────────────────────────────

def test_check_fills_the_format_and_name_from_the_files():
    cfg, problems = objectstore.check_config("s3", {**KEYS, "path": " s3://trips/stations/*.csv "})
    assert problems == {}
    assert (cfg["path"], cfg["format"], cfg["dataset"]) == ("s3://trips/stations/*.csv", "csv", "data")
    cfg, _ = objectstore.check_config("gcs", {"path": "gs://b/events/day.jsonl.gz"})
    assert cfg["format"] == "json"
    cfg, _ = objectstore.check_config("s3", {"path": "s3://b/x/*"})
    assert cfg["format"] == "parquet"                  # nothing to go on: the common case
    cfg, _ = objectstore.check_config("s3", {"path": "s3://b/x.csv", "format": "JSON"})
    assert cfg["format"] == "json"                     # a format picked wins over the extension


@pytest.mark.parametrize("kind, path, says", [
    ("s3", "gs://trips/x.parquet", "s3://"),
    ("s3", "trips/x.parquet", "for example s3://bucket/sales/*.parquet"),
    ("gcs", "s3://trips/x.parquet", "gs://"),
    ("azure_blob", "s3://trips/x.parquet", "az://"),
    ("s3", "s3://trips/x.parquet?s3_access_key_id=AKIA", "query string"),
    ("s3", "s3://trips/my files/*.csv", "spaces"),
    ("s3", "s3://trips/x'.csv", "quotes"),
])
def test_a_path_from_elsewhere_is_refused_by_field(kind, path, says):
    _, problems = objectstore.check_config(kind, {"path": path, "connection_string": "AccountName=a;"})
    assert set(problems) == {"path"}
    assert says in problems["path"]


def test_a_table_name_and_format_are_checked():
    _, problems = objectstore.check_config("s3", {"path": "s3://b/x.csv", "dataset": "my-table",
                                                  "format": "xlsx"})
    assert set(problems) == {"dataset", "format"}
    assert "xlsx" not in problems["format"]           # the choices, not the typo
    assert "csv, json, parquet" in problems["format"]


def test_an_s3_endpoint_is_tidied_to_host_and_port():
    cfg, problems = objectstore.check_config("s3", {**RIDES, "endpoint": "http://minio.local:9000/"})
    assert problems == {} and cfg["endpoint"] == "minio.local:9000" and cfg["use_ssl"] == "false"
    cfg, problems = objectstore.check_config("s3", {**RIDES, "endpoint": "HTTPS://r2.example.com"})
    assert problems == {} and cfg["endpoint"] == "r2.example.com" and "use_ssl" not in cfg
    _, problems = objectstore.check_config("s3", {**RIDES, "endpoint": "minio.local:9000/bucket"})
    assert "host and port" in problems["endpoint"]


@pytest.mark.parametrize("endpoint", [
    "169.254.169.254", "http://169.254.169.254:80", "[fe80::1]:9000", "fe80::1",
    "[::ffff:169.254.169.254]:80", "fd00:ec2::254", "100.100.100.200", "[::ffff:100.100.100.200]:80",
    "metadata.google.internal", "Metadata.Google.Internal.", "metadata",
])
def test_an_s3_endpoint_never_points_at_a_metadata_service(endpoint):
    _, problems = objectstore.check_config("s3", {**RIDES, "endpoint": endpoint})
    assert problems == {"endpoint": "That is a cloud metadata address, not a storage endpoint."}


def test_local_and_named_endpoints_are_fine():
    for endpoint in ("127.0.0.1:9000", "localhost:5055", "minio:9000", "s3.eu-west-1.amazonaws.com"):
        _, problems = objectstore.check_config("s3", {**RIDES, "endpoint": endpoint})
        assert problems == {}, endpoint


def test_azure_needs_a_connection_string_or_a_whole_service_principal():
    path = {"path": "az://container/sales/*.parquet"}
    _, problems = objectstore.check_config("azure_blob", path)
    assert set(problems) == {"connection_string"} and "service-principal" in problems["connection_string"]
    _, problems = objectstore.check_config("azure_blob", {**path, "account_name": "acct", "client_id": "c"})
    assert set(problems) == {"connection_string"}     # half a principal is no principal
    _, problems = objectstore.check_config("azure_blob", {**path, "connection_string": "AccountKey=k=="})
    assert "AccountName=" in problems["connection_string"]
    _, problems = objectstore.check_config("azure_blob", {
        **path, "account_name": "acct", "tenant_id": "t", "client_id": "c", "client_secret": "s"})
    assert problems == {}
    _, problems = objectstore.check_config("azure_blob", {
        **path, "connection_string": "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=k==;"
                                     "EndpointSuffix=core.windows.net"})
    assert problems == {}
    # Azurite, the local emulator, names its own endpoint — that is fine…
    _, problems = objectstore.check_config("azure_blob", {
        **path, "connection_string": "AccountName=devstoreaccount1;AccountKey=k==;"
                                     "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"})
    assert problems == {}
    # …a metadata service is not.
    _, problems = objectstore.check_config("azure_blob", {
        **path, "connection_string": "AccountName=a;AccountKey=k==;BlobEndpoint=http://169.254.169.254/"})
    assert "metadata" in problems["connection_string"]


# ── A failed read: a fixed sentence, on its field ────────────────────────

@pytest.mark.parametrize("kind, text, field, stage", [
    ("s3", "HTTP Error: HTTP GET error (HTTP 403) AccessDenied", "secret_access_key", "sign_in"),
    ("s3", "InvalidAccessKeyId: The AWS Access Key Id you provided does not exist", "secret_access_key", "sign_in"),
    ("s3", "SignatureDoesNotMatch", "secret_access_key", "sign_in"),
    ("gcs", "HTTP 403 Forbidden", "hmac_secret", "sign_in"),
    ("azure_blob", "AuthenticationFailed: Server failed to authenticate", "connection_string", "sign_in"),
    ("s3", 'IO Error: No files found that match the pattern "s3://b/x/*.parquet"', "path", "signed_in"),
    ("s3", "HTTP 404 NoSuchBucket", "path", "signed_in"),
    ("azure_blob", "ContainerNotFound", "path", "signed_in"),
    ("s3", "Could not resolve hostname minio.nowhere", "endpoint", "sign_in"),
    ("gcs", "Connection timed out", None, "sign_in"),
    ("s3", "Invalid Input Error: No magic bytes found at end of file 's3://b/x.csv'", "format", "signed_in"),
    ("s3", "Invalid Input Error: Error when sniffing file", "format", "signed_in"),
    ("s3", "Invalid Input Error: Malformed JSON in file", "format", "signed_in"),
    ("s3", "IO Error: Failed to download extension httpfs", None, "check"),
])
def test_read_failures_are_named_by_field(kind, text, field, stage):
    found = objectstore.classify_error(RuntimeError(text), kind)
    assert (found["field"], found["stage"]) == (field, stage)
    assert text not in found["said"]


def test_the_cause_chain_is_read_but_never_repeated():
    try:
        try:
            raise OSError("HTTP 403 AccessDenied for X-Amz-Credential=AKIASTUDIOTEST/sig")
        except OSError as inner:
            raise RuntimeError("binder failed") from inner
    except RuntimeError as outer:
        found = objectstore.classify_error(outer, "s3")
    assert found["field"] == "secret_access_key"
    assert "AKIA" not in found["said"] and "sig" not in found["said"]
    assert objectstore.classify_error(RuntimeError("something else entirely"), "s3") is None


# ── A connection's own keys and datasets ─────────────────────────────────

def test_a_connection_reads_its_own_keys_not_the_environment(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAENVIRONMENT")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
    own = objectstore._credentials("s3", {**KEYS, "endpoint": "minio:9000", "use_ssl": "false"})
    assert own["present"]
    assert "AKIASTUDIOTEST" in own["secret"] and "AKIAENVIRONMENT" not in own["secret"]
    assert "ENDPOINT 'minio:9000'" in own["secret"] and "USE_SSL false" in own["secret"]
    # A connection without keys has none — the environment's are not borrowed.
    assert not objectstore._credentials("s3", {"path": "s3://b/x.csv"})["present"]
    # A built-in reads the environment.
    assert "AKIAENVIRONMENT" in objectstore._credentials("s3")["secret"]
    gcs = objectstore._credentials("gcs", {"hmac_key_id": "GOOG1E", "hmac_secret": "h"})
    assert gcs["present"] and "KEY_ID 'GOOG1E'" in gcs["secret"]
    az = objectstore._credentials("azure_blob", {"connection_string": "AccountName=a;AccountKey=k"})
    assert az["present"] and "CONNECTION_STRING 'AccountName=a;AccountKey=k'" in az["secret"]


def test_a_quote_in_a_key_cannot_break_out_of_the_secret():
    sql = objectstore._credentials("s3", {**KEYS, "secret_access_key": "x', ENDPOINT 'evil"})["secret"]
    assert "SECRET 'x'', ENDPOINT ''evil'" in sql


def test_the_environment_bootstrap_stays_with_the_built_ins(client, monkeypatch):
    monkeypatch.setenv("STUDIO_OBJECT_DATASETS", json.dumps(
        {"s3": [{"name": "orders", "uri": "s3://env/orders/*.parquet"}],
         "trips": [{"name": "leak", "uri": "s3://other/*.parquet"}]}))
    assert [d["name"] for d in objectstore.datasets("s3")] == ["orders"]
    names = [d["name"] for d in objectstore.datasets("trips", "s3", RIDES)]
    assert names == ["rides"]
    # A connection's own dataset is validated like any other.
    assert objectstore.datasets("trips", "s3", {**RIDES, "path": "gs://b/x.parquet"}) == []
    objectstore._upsert_dataset("trips", "rides", "s3://trips/rides-v2/*.parquet", "parquet")
    assert [d["uri"] for d in objectstore.datasets("trips", "s3", RIDES)] == \
        ["s3://trips/rides-v2/*.parquet"]                 # the registry wins a collision


def test_only_pins_a_probe_to_the_files_given():
    d = {"name": "stations", "uri": "s3://trips/stations/*.csv", "format": "csv"}
    conn = objectstore.ObjectStoreConnector("trips", "s3", RIDES, only=[d])
    assert conn.list_tables() == ["stations"]
    bad = objectstore.ObjectStoreConnector("trips", "s3", RIDES, only=[{**d, "uri": "gs://x/y.csv"}])
    assert bad.list_tables() == []


# ── Connecting from the Integrations screen ─────────────────────────────

def test_the_store_types_are_served_with_their_forms(client):
    _login(client)
    types = {t["ctype"]: t for t in client.get("/api/connections/types").json()}
    for kind in ("s3", "azure_blob", "gcs"):
        keys = [f["key"] for f in types[kind]["fields"]]
        assert keys[-3:] == ["path", "dataset", "format"], kind
        fmt = types[kind]["fields"][-1]
        assert fmt["options"] == ["parquet", "csv", "json"] and fmt["default"] == "parquet"
        assert types[kind]["dialect"] == "duckdb"
    secret = {f["key"] for t in types.values() for f in t["fields"] if f["secret"]}
    assert {"secret_access_key", "session_token", "hmac_secret", "connection_string",
            "client_secret"} <= secret
    # Other types' fields carry no options key.
    assert all("options" not in f for f in types["postgres"]["fields"])


def test_connect_reads_the_files_and_stores_the_keys_encrypted(client, reads):
    _login(client)
    r = client.post("/api/connections", json={"name": "trips", "ctype": "s3",
                                              "config": {**RIDES, "endpoint": "http://minio:9000"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["hint"] == "s3://trips/rides/*.parquet" and body["namespace"] == ""
    assert "wJalr" not in r.text and "AKIASTUDIOTEST" not in r.text
    assert reads == [("__probe__", "s3", "s3://trips/rides/*.parquet", "parquet")]
    from app import db
    with db.connect() as c:
        raw = c.execute("SELECT config FROM data_connections WHERE name='trips'").fetchone()["config"]
    assert "wJalr" not in raw and "AKIA" not in raw
    from app import connections
    stored = connections._config(connections._row_by_name("trips"))
    assert stored["endpoint"] == "minio:9000" and stored["use_ssl"] == "false"
    sources = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert sources["trips"]["configured"] and sources["trips"]["dialect"] == "duckdb"
    assert client.get("/api/catalog/sources/trips/tables").json() == ["rides"]


def test_a_wrong_path_or_format_is_marked_and_nothing_is_saved(client, reads):
    _login(client)
    for cfg, field in (({**RIDES, "path": "s3://trips/ghost/*.parquet"}, "path"),
                       ({**RIDES, "path": "s3://trips/stations.csv", "format": "parquet"}, "format")):
        r = client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": cfg})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert list(detail["fields"]) == [field] and detail["stage"] == "signed_in"
    assert "trips" not in {c["name"] for c in client.get("/api/connections").json()}


def test_refused_keys_say_so_without_repeating_the_driver(client, reads):
    _login(client)
    r = client.post("/api/connections/test", json={"ctype": "s3", "config": {
        **RIDES, "path": "s3://private/x.parquet"}})
    out = r.json()
    assert out["ok"] is False and out["stage"] == "sign_in"
    assert list(out["fields"]) == ["secret_access_key"]
    assert "SIGNED" not in r.text and "amazonaws" not in r.text


def test_a_form_problem_is_refused_before_any_read(client, reads):
    _login(client)
    out = client.post("/api/connections/test", json={"ctype": "s3", "config": {
        **RIDES, "endpoint": "169.254.169.254"}}).json()
    assert out["stage"] == "check" and list(out["fields"]) == ["endpoint"]
    out = client.post("/api/connections/test", json={"ctype": "gcs", "config": {
        "hmac_key_id": "GOOG1E", "hmac_secret": "h", "path": "s3://wrong/x.csv"}}).json()
    assert out["stage"] == "check" and list(out["fields"]) == ["path"]
    assert reads == []


def test_the_same_keys_twice_point_to_the_source_already_there(client, reads):
    _login(client)
    assert client.post("/api/connections", json={"name": "trips", "ctype": "s3",
                                                  "config": RIDES}).status_code == 201
    # Other files, another region, the same keys: still the same sign-in.
    r = client.post("/api/connections", json={"name": "trips-2", "ctype": "s3", "config": {
        **RIDES, "path": "s3://trips/stations/*.csv", "region": "eu-west-1"}})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["existing"] == "trips" and detail["stage"] == "duplicate"
    assert "add these files to it instead" in detail["error"]
    # Other keys are another sign-in; so is the same key at another endpoint.
    assert client.post("/api/connections", json={"name": "lake", "ctype": "s3", "config": {
        **RIDES, "access_key_id": "AKIAOTHER"}}).status_code == 201
    assert client.post("/api/connections", json={"name": "minio", "ctype": "s3", "config": {
        **RIDES, "endpoint": "minio:9000"}}).status_code == 201
    # …and the endpoint is compared as typed-any-way.
    r = client.post("/api/connections", json={"name": "minio-2", "ctype": "s3", "config": {
        **RIDES, "endpoint": "HTTP://MINIO:9000/"}})
    assert r.status_code == 409 and r.json()["detail"]["existing"] == "minio"


def test_azure_sign_in_is_the_account(client, reads):
    _login(client)
    first = {"connection_string": "DefaultEndpointsProtocol=https;AccountName=Acme;AccountKey=k1==",
             "path": "az://sales/*.parquet"}
    assert client.post("/api/connections", json={"name": "acme", "ctype": "azure_blob",
                                                  "config": first}).status_code == 201
    again = {**first, "connection_string": "AccountName=acme;AccountKey=k2==;", "path": "az://hr/*.csv"}
    r = client.post("/api/connections", json={"name": "acme-2", "ctype": "azure_blob", "config": again})
    assert r.status_code == 409 and r.json()["detail"]["existing"] == "acme"


def test_the_deployments_own_keys_are_a_sign_in_too(client, reads, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", KEYS["access_key_id"])
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", KEYS["secret_access_key"])
    monkeypatch.setenv("STUDIO_OBJECT_DATASETS", json.dumps(
        {"s3": [{"name": "orders", "uri": "s3://env/orders/*.parquet"}]}))
    _login(client)
    r = client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    assert r.status_code == 409 and r.json()["detail"]["existing"] == "s3"


def test_the_built_in_s3_source_can_be_set_up_here(client, reads):
    _login(client)
    env = {e["name"]: e for e in client.get("/api/connections/env").json()}
    assert env["s3"]["mode"] == "environment" and not env["s3"]["env_configured"]
    r = client.put("/api/connections/env/s3", json={"config": RIDES})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "replaced" and r.json()["hint"] == "s3://trips/rides/*.parquet"
    sources = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert sources["s3"]["configured"]
    assert client.get("/api/catalog/sources/s3/tables").json() == ["rides"]
    # And a connection with the same keys is then a duplicate of it.
    r = client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    assert r.status_code == 409 and r.json()["detail"]["existing"] == "s3"


# ── Adding files to a store ──────────────────────────────────────────────

def _add(c, **body):
    return c.post("/api/settings/objectstore", json={"source": "trips", "check": True, **body})


def test_files_are_read_with_the_stores_keys_before_they_are_added(client, reads):
    _login(client)
    client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    reads.clear()
    r = _add(client, name="stations", uri="s3://trips/stations/*.csv", format="csv")
    assert r.status_code == 201, r.text
    assert reads == [("trips", "s3", "s3://trips/stations/*.csv", "csv")]
    assert client.get("/api/catalog/sources/trips/tables").json() == ["rides", "stations"]

    r = _add(client, name="ghost", uri="s3://trips/ghost/*.parquet")
    detail = r.json()["detail"]
    assert r.status_code == 400 and list(detail["fields"]) == ["uri"]
    assert detail["error"].startswith("Signed in, but there are no files at that path")
    r = _add(client, name="wrong", uri="s3://trips/stations.csv", format="parquet")
    assert list(r.json()["detail"]["fields"]) == ["format"]
    r = _add(client, name="locked", uri="s3://private/x.parquet")
    detail = r.json()["detail"]
    assert r.status_code == 400 and detail["fields"] == {} and detail["stage"] == "sign_in"
    assert "SIGNED" not in r.text
    names = {(d["source"], d["name"]) for d in client.get("/api/settings/objectstore").json()["datasets"]}
    assert names == {("trips", "stations")}


def test_files_with_no_format_named_are_read_as_their_extension_says(client, reads):
    _login(client)
    client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    reads.clear()
    assert _add(client, name="stations", uri="s3://trips/stations/*.csv").status_code == 201
    assert _add(client, name="events", uri="s3://trips/events/*.ndjson.gz").status_code == 201
    assert _add(client, name="more", uri="s3://trips/more/*").status_code == 201
    assert [r[3] for r in reads] == ["csv", "json", "parquet"]


def test_without_check_a_dataset_is_registered_as_before(client, reads):
    _login(client)
    r = client.post("/api/settings/objectstore", json={"source": "s3", "name": "orders",
                                                       "uri": "s3://env/orders/*.parquet"})
    assert r.status_code == 201 and reads == []


def test_a_store_with_no_keys_or_switched_off_cannot_read_files(client, reads):
    _login(client)
    r = _add(client, source="s3", name="orders", uri="s3://env/orders/*.parquet")
    assert r.status_code == 400 and "has no keys yet" in r.json()["detail"]["error"]
    client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    cid = next(c["id"] for c in client.get("/api/connections").json() if c["name"] == "trips")
    client.post(f"/api/connections/{cid}/disconnect")
    r = _add(client, name="stations", uri="s3://trips/stations/*.csv")
    assert r.status_code == 400 and "not an object-store source" in r.json()["detail"]
    r = _add(client, source="postgres", name="x", uri="s3://a/b.csv")
    assert r.status_code == 400


def test_the_listing_shows_what_each_store_reads_now(client, reads):
    _login(client)
    client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    client.post("/api/connections", json={"name": "lake", "ctype": "gcs", "config": {
        "hmac_key_id": "GOOG1E", "hmac_secret": "h", "path": "gs://lake/events/*.json"}})
    _add(client, name="stations", uri="s3://trips/stations/*.csv", format="csv")
    out = client.get("/api/settings/objectstore").json()
    assert out["kinds"] == {"s3": "s3", "azure_blob": "azure_blob", "gcs": "gcs",
                            "trips": "s3", "lake": "gcs"}
    assert out["sources"]["trips"] == ["s3://"] and out["sources"]["lake"] == ["gs://"]
    assert [(d["name"], d["format"]) for d in out["live"]["trips"]] == [("rides", "parquet"),
                                                                       ("stations", "csv")]
    assert [(d["name"], d["format"]) for d in out["live"]["lake"]] == [("data", "json")]
    assert out["live"]["s3"] == []
    assert "GOOG1E" not in json.dumps(out) and "wJalr" not in json.dumps(out)


def test_removing_a_store_takes_its_files_with_it(client, reads):
    _login(client)
    client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    _add(client, name="stations", uri="s3://trips/stations/*.csv", format="csv")
    objectstore._upsert_dataset("s3", "orders", "s3://env/orders/*.parquet", "parquet")
    cid = next(c["id"] for c in client.get("/api/connections").json() if c["name"] == "trips")
    assert client.delete(f"/api/connections/{cid}").status_code == 200
    rows = {(d["source"], d["name"]) for d in client.get("/api/settings/objectstore").json()["datasets"]}
    assert rows == {("s3", "orders")}             # the built-in's stay
    # A new source given the same name starts with only its own files.
    client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": {
        **RIDES, "access_key_id": "AKIANEW"}})
    assert client.get("/api/catalog/sources/trips/tables").json() == ["rides"]


def test_files_are_admin_only(client, reads):
    _login(client)
    client.post("/api/connections", json={"name": "trips", "ctype": "s3", "config": RIDES})
    client.headers.pop("Authorization")
    _login(client, "analyst@studio.local", "analyst123")
    assert client.get("/api/settings/objectstore").status_code == 403
    assert _add(client, name="stations", uri="s3://trips/stations/*.csv").status_code == 403
    # And the new source is the admin's until governance grants it (fail closed).
    sources = {s["name"]: s for s in client.get("/api/catalog/sources").json()}
    assert sources.get("trips", {}).get("allowed") in (None, False)


# ── The real read, against an S3 emulator ───────────────────────────────

LIVE = os.getenv("STUDIO_TEST_S3_ENDPOINT", "")


@pytest.mark.skipif(not LIVE, reason="set STUDIO_TEST_S3_ENDPOINT to an S3 emulator (moto_server)")
def test_live_s3_round_trip(client):
    duckdb = pytest.importorskip("duckdb")
    bucket = "studio-live-test"
    # moto accepts an unsigned bucket create; DuckDB writes the files.
    req = urllib.request.Request(f"http://{LIVE}/{bucket}", method="PUT")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
    keys = {"access_key_id": "studio-test", "secret_access_key": "studio-secret",
            "endpoint": f"http://{LIVE}"}
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs")
    con.execute(f"CREATE SECRET (TYPE s3, KEY_ID 'studio-test', SECRET 'studio-secret', "
                f"ENDPOINT '{LIVE}', URL_STYLE 'path', USE_SSL false, REGION 'us-east-1')")
    con.execute(f"COPY (SELECT * FROM (VALUES ('Red', 270), ('Blue', 175)) t(line, riders)) "
                f"TO 's3://{bucket}/rides/part-1.parquet' (FORMAT parquet)")
    con.execute(f"COPY (SELECT * FROM (VALUES ('Union', 'Red')) t(stop, line)) "
                f"TO 's3://{bucket}/stops/stops.csv' (FORMAT csv, HEADER)")
    con.close()

    _login(client)
    good = {**keys, "path": f"s3://{bucket}/rides/*.parquet", "dataset": "rides"}
    out = client.post("/api/connections/test", json={"ctype": "s3", "config": good}).json()
    assert out == {"ok": True, "tables": 1, "sample": ["rides"]}
    out = client.post("/api/connections/test", json={"ctype": "s3", "config": {
        **good, "path": f"s3://{bucket}/nothing/*.parquet"}}).json()
    assert out["fields"] == {"path": out["error"]} and out["stage"] == "signed_in"
    out = client.post("/api/connections/test", json={"ctype": "s3", "config": {
        **good, "path": f"s3://{bucket}/stops/*.csv", "format": "parquet"}}).json()
    assert list(out["fields"]) == ["format"]

    assert client.post("/api/connections", json={"name": "live", "ctype": "s3",
                                                  "config": good}).status_code == 201
    r = client.post("/api/settings/objectstore", json={
        "source": "live", "name": "stops", "uri": f"s3://{bucket}/stops/*.csv", "check": True})
    assert r.status_code == 201, r.text
    r = client.post("/api/queries/verify", json={
        "source": "live", "table": "rides",
        "sql": "SELECT line, SUM(riders) AS riders FROM rides GROUP BY line ORDER BY line"})
    assert r.json()["ok"], r.text
    r = client.post("/api/queries/verify", json={
        "source": "live", "table": "rides",
        "sql": f"SELECT * FROM read_parquet('s3://{bucket}/rides/*.parquet')"})
    assert not r.json()["ok"] and "query guard" in r.json()["error"]
