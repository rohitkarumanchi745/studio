"""Spark on Kubernetes across clusters: AKS, GKE and a local one.

An administrator connects each in Integrations › Compute for Spark
(clusters.py); a Spark job runs on the cluster it names, else on the first
connected of AKS, GKE, the server's own K8S_API_URL, local — so with no cloud
cluster connected, Spark runs locally. Locked in, against mock Kubernetes API
servers (one over TLS with its own CA and client-certificate sign-in):
- a pasted kubeconfig fills the form: its current context's server, CA and
  sign-in, with a note for what Studio cannot use (exec plugins, files,
  loopback from Docker); bad input is refused, not guessed at;
- the form is checked before anything signs in (https, no metadata host,
  a sign-in, PEMs that load, DNS names);
- a test says what is wrong in one fixed sentence (token, RBAC, operator
  missing, CA, unreachable) and never echoes the server;
- a cluster is saved only once it signs in, encrypted, never read back;
  only administrators reach any of it;
- the order and the fallback to local; a named cluster or none; run refs
  that say where a run went, so status and logs ask that cluster; the
  server's own settings as before;
- TLS: the cluster's CA is used and verified, the client certificate signs
  in, and an unverifiable server is refused;
- chat: "on AKS / on GKE / locally" picks one, the queued payload names the
  cluster (and its image), a cluster that is not connected is said so.

Run from the backend directory:
    python -m pytest tests/test_compute_clusters.py -q
"""
import base64
import datetime
import ipaddress
import json
import os
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import HTTPException

from app import chat_platforms, clusters, db, platforms, supervisor

ADMIN = {"id": "u-clu-admin", "email": "clu-admin@studio.test", "role": "admin", "name": "A"}
ANALYST = {"id": "u-clu-ana", "email": "clu-ana@studio.test", "role": "analyst", "name": "B"}
GROUP = "/apis/sparkoperator.k8s.io/v1beta2"


# ── Certificates: a CA, a server certificate for 127.0.0.1, a client one ─

def _pem(cert):
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def _authority(cn):
    now = datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return cert, key


def _leaf(ca, ca_key, cn, *, server):
    now = datetime.datetime.now(datetime.timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
               .issuer_name(ca.subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(days=1))
               .not_valid_after(now + datetime.timedelta(days=1))
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                              critical=False)
               .add_extension(x509.ExtendedKeyUsage(
                   [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]),
                   critical=False))
    if server:
        builder = builder.add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]),
            critical=False)
    return builder.sign(ca_key, hashes.SHA256()), key


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    ca, ca_key = _authority("studio-test-ca")
    other_ca, _ = _authority("someone-else-ca")
    server, server_key = _leaf(ca, ca_key, "kube-apiserver", server=True)
    client, client_key = _leaf(ca, ca_key, "studio-test-client", server=False)
    folder = tmp_path_factory.mktemp("pki")
    (folder / "server.pem").write_text(_pem(server))
    (folder / "server.key").write_text(_key_pem(server_key))
    return {"ca": _pem(ca), "other_ca": _pem(other_ca), "client_cert": _pem(client),
            "client_key": _key_pem(client_key), "dir": folder}


# ── Mock Kubernetes API servers ─────────────────────────────────────────

class Kube(BaseHTTPRequestHandler):
    """A Kubernetes API with the operator's CRD. Each server has a tag (which
    cluster answered), a token it accepts, and a scripted probe answer."""

    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        body = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _signed_in(self):
        srv = self.server
        peer = getattr(self.connection, "getpeercert", lambda: None)()
        cn = ""
        if peer:
            cn = dict(x[0] for x in peer["subject"]).get("commonName", "")
        srv.seen.append({"auth": self.headers.get("Authorization"), "client_cn": cn,
                         "path": self.path, "method": self.command})
        return self.headers.get("Authorization") == f"Bearer {srv.token}" or cn == "studio-test-client"

    def do_GET(self):
        if not self._signed_in():
            return self._send(401, {"kind": "Status", "message": "Unauthorized"})
        path = urlsplit(self.path).path
        if path.startswith(GROUP) and path.endswith("/sparkapplications"):
            code = self.server.probe
            return self._send(code, {"items": []} if code == 200 else
                              {"kind": "Status", "message": f"secret detail {self.server.tag}"})
        if path.startswith(GROUP):
            return self._send(200, {"status": {"applicationState": {"state": "RUNNING"},
                                               "sparkApplicationId": f"app-{self.server.tag}",
                                               "driverInfo": {"podName": "drv"}}})
        if path.endswith("/log"):
            return self._send(200, f"driver log from {self.server.tag}")
        return self._send(404, {})

    def do_POST(self):
        if not self._signed_in():
            return self._send(401, {"kind": "Status", "message": "Unauthorized"})
        n = int(self.headers.get("Content-Length") or 0)
        self.server.created.append({"path": self.path, "body": json.loads(self.rfile.read(n))})
        return self._send(201, {})


def _serve(tag, token="tok", tls=None):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Kube)
    srv.tag, srv.token, srv.probe, srv.seen, srv.created = tag, token, 200, [], []
    scheme = "http"
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls["dir"] / "server.pem", tls["dir"] / "server.key")
        ctx.load_verify_locations(cadata=tls["ca"])
        ctx.verify_mode = ssl.CERT_OPTIONAL
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"{scheme}://127.0.0.1:{srv.server_address[1]}"
    return srv


@pytest.fixture
def servers(pki):
    made = {}

    def make(tag, **kw):
        made[tag] = _serve(tag, **kw)
        return made[tag]
    yield make
    for srv in made.values():
        srv.shutdown()
        srv.server_close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    db.init_db()
    clusters.init_tables()
    supervisor.init_tables()
    for name in ("K8S_API_URL", "K8S_TOKEN", "K8S_NAMESPACE", "K8S_VERIFY_SSL", "K8S_CA_CERT",
                 "K8S_SERVICE_ACCOUNT", "K8S_SPARK_IMAGE", "K8S_SPARK_JOBS_DIR"):
        monkeypatch.delenv(name, raising=False)

    def wipe():
        with db.connect() as c:
            c.execute("DELETE FROM compute_clusters")
            c.commit()
    wipe()
    clusters._CONTEXTS.clear()
    yield
    wipe()


def _put(kind, srv, **extra):
    """A cluster as the route would keep it (route checks are tested apart)."""
    cfg = {"server": srv.url, "token": srv.token, "ca_cert": "", "client_cert": "", "client_key": "",
           "insecure": False, "namespace": "spark-jobs", "service_account": "spark-operator-spark",
           "image": "", **extra}
    clusters.store(kind, cfg, ADMIN["id"])


def _kubeconfig(**user):
    return yaml.safe_dump({
        "apiVersion": "v1", "kind": "Config", "current-context": "kind-studio",
        "clusters": [
            {"name": "other", "cluster": {"server": "https://other.example:6443"}},
            {"name": "kind-studio", "cluster": {
                "server": "https://127.0.0.1:52817",
                "certificate-authority-data": base64.b64encode(b"-----BEGIN CERTIFICATE-----\nAA==\n"
                                                               b"-----END CERTIFICATE-----").decode()}}],
        "contexts": [{"name": "other", "context": {"cluster": "other", "user": "other"}},
                     {"name": "kind-studio", "context": {"cluster": "kind-studio", "user": "kind-studio",
                                                         "namespace": "spark-jobs"}}],
        "users": [{"name": "other", "user": {"token": "not-this-one"}},
                  {"name": "kind-studio", "user": user}],
    })


# ── A pasted kubeconfig ─────────────────────────────────────────────────

def test_a_kind_kubeconfig_fills_the_form_from_its_current_context():
    cert = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nCC==\n-----END CERTIFICATE-----").decode()
    key = base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nKK==\n-----END PRIVATE KEY-----").decode()
    fields, notes = clusters.parse_kubeconfig(_kubeconfig(**{"client-certificate-data": cert,
                                                             "client-key-data": key}))
    assert fields == {"server": "https://127.0.0.1:52817",
                      "ca_cert": "-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----",
                      "insecure": False, "token": "",
                      "client_cert": "-----BEGIN CERTIFICATE-----\nCC==\n-----END CERTIFICATE-----",
                      "client_key": "-----BEGIN PRIVATE KEY-----\nKK==\n-----END PRIVATE KEY-----",
                      "namespace": "spark-jobs"}
    assert notes[0] == "Read the “kind-studio” context."
    assert "host.docker.internal" in notes[1] and len(notes) == 2


def test_a_cloud_kubeconfig_says_its_plugin_cannot_be_used():
    for user in ({"exec": {"command": "kubelogin", "args": ["get-token"]}},
                 {"exec": {"command": "gke-gcloud-auth-plugin"}}, {"auth-provider": {"name": "gcp"}}):
        fields, notes = clusters.parse_kubeconfig(_kubeconfig(**user))
        assert fields["token"] == "" and fields["client_cert"] == ""
        assert any("signs in through a plugin" in n and "studio-access.yaml" in n for n in notes)
    # Files on another machine are named, not read.
    _, notes = clusters.parse_kubeconfig(_kubeconfig(**{"client-certificate": "/home/u/.minikube/c.crt",
                                                        "client-key": "/home/u/.minikube/c.key"}))
    assert any("files on that machine" in n for n in notes)


@pytest.mark.parametrize("text,said", [
    ("clusters: [", "does not read as YAML"),
    ("just: words", "no clusters in it"),
    ("- a\n- b", "no clusters in it"),
    ("x" * 200_001, "too large"),
])
def test_what_is_not_a_kubeconfig_is_refused(text, said):
    with pytest.raises(HTTPException) as e:
        clusters.parse_kubeconfig(text)
    assert e.value.status_code == 400 and said in e.value.detail


# ── The form, checked before anything signs in ─────────────────────────

GOOD = {"server": "https://k8s.example:443", "token": "t"}


@pytest.mark.parametrize("kind,raw,field", [
    ("aks", {**GOOD, "server": "k8s.example"}, "server"),
    ("aks", {**GOOD, "server": "http://k8s.example"}, "server"),
    ("local", {**GOOD, "server": "http://10.0.0.5:8001"}, "server"),
    ("gke", {**GOOD, "server": "https://169.254.169.254"}, "server"),
    ("gke", {**GOOD, "server": "https://metadata.google.internal"}, "server"),
    ("aks", {"server": "https://k8s.example"}, "token"),
    ("local", {"server": "https://k8s.example", "client_cert": "-----BEGIN CERTIFICATE-----"}, "client_key"),
    ("aks", {**GOOD, "ca_cert": "not a certificate"}, "ca_cert"),
    ("aks", {**GOOD, "namespace": "Spark_Jobs"}, "namespace"),
    ("aks", {**GOOD, "service_account": "a b"}, "service_account"),
    ("aks", {**GOOD, "image": "my spark:1"}, "image"),
])
def test_the_form_is_checked_first(kind, raw, field):
    _, problems = clusters.normalize(kind, raw)
    assert list(problems) == [field]


def test_a_good_form_and_its_defaults(pki):
    cfg, problems = clusters.normalize("local", {"server": "http://localhost:8001/ ", "token": " t "})
    assert problems == {}
    assert cfg["server"] == "http://localhost:8001" and cfg["token"] == "t"
    assert cfg["namespace"] == "spark-jobs" and cfg["service_account"] == "spark-operator-spark"
    cfg, problems = clusters.normalize("aks", {**GOOD, "ca_cert": base64.b64encode(pki["ca"].encode()).decode(),
                                               "insecure": "true"})
    assert problems == {} and cfg["ca_cert"] == pki["ca"].strip() and cfg["insecure"] is True
    _, problems = clusters.normalize("local", {"server": "https://127.0.0.1:6443",
                                               "client_cert": pki["client_cert"], "client_key": pki["client_key"]})
    assert problems == {}
    _, problems = clusters.normalize("local", {"server": "https://127.0.0.1:6443",
                                               "client_cert": pki["client_cert"], "client_key": pki["ca"]})
    assert list(problems) == ["client_cert"]
    with pytest.raises(HTTPException) as e:
        clusters.normalize("eks", GOOD)
    assert e.value.status_code == 404


# ── Testing a cluster ───────────────────────────────────────────────────

@pytest.mark.parametrize("code,field,said", [
    (403, "namespace", "apply studio-access.yaml"),
    (404, None, "Spark operator is not installed"),
    (500, None, "answered unexpectedly (HTTP 500)"),
])
def test_a_test_says_what_is_wrong_in_its_own_words(servers, code, field, said):
    srv = servers("aks")
    srv.probe = code
    out = clusters.probe("local", {"server": srv.url, "token": "tok", "namespace": "spark-jobs"})
    assert out["ok"] is False and out["field"] == field and said in out["said"]
    assert "secret detail" not in json.dumps(out)
    assert srv.seen[-1]["path"] == f"{GROUP}/namespaces/spark-jobs/sparkapplications?limit=1"


def test_a_wrong_token_an_unreachable_server_and_success(servers):
    srv = servers("aks")
    out = clusters.probe("local", {"server": srv.url, "token": "wrong", "namespace": "spark-jobs"})
    assert out["field"] == "token" and "refused this sign-in" in out["said"]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    out = clusters.probe("local", {"server": f"http://127.0.0.1:{closed}", "token": "t", "namespace": "n"})
    assert out["field"] == "server" and "host.docker.internal" in out["said"]
    out = clusters.probe("aks", {"server": f"http://127.0.0.1:{closed}", "token": "t", "namespace": "n"})
    assert out["field"] == "server" and "host.docker.internal" not in out["said"]
    assert clusters.probe("local", {"server": srv.url, "token": "tok", "namespace": "spark-jobs"}) == {
        "ok": True, "said": "Signed in — the Spark operator is ready in “spark-jobs”."}


# ── TLS: the cluster's CA, a client certificate ─────────────────────────

def test_tls_verifies_against_the_clusters_ca_and_signs_in_with_a_certificate(servers, pki):
    srv = servers("local", token="none-accepted", tls=pki)
    base = {"server": srv.url, "namespace": "spark-jobs",
            "client_cert": pki["client_cert"], "client_key": pki["client_key"]}
    # Without its CA the server cannot be verified: refused, and said so.
    out = clusters.probe("local", base)
    assert out["field"] == "ca_cert" and "could not be verified" in out["said"]
    out = clusters.probe("local", {**base, "ca_cert": pki["other_ca"]})
    assert out["field"] == "ca_cert"
    # With it, the client certificate signs in.
    assert clusters.probe("local", {**base, "ca_cert": pki["ca"]})["ok"] is True
    assert srv.seen[-1]["client_cn"] == "studio-test-client" and srv.seen[-1]["auth"] is None
    # Skipping verification is a choice, and still signs in with the certificate.
    assert clusters.probe("local", {**base, "insecure": True})["ok"] is True


def test_a_tls_cluster_runs_a_job_end_to_end(servers, pki):
    srv = servers("local", token="none-accepted", tls=pki)
    _put("local", srv, token="", ca_cert=pki["ca"], client_cert=pki["client_cert"],
         client_key=pki["client_key"], image="registry.local/spark:3.5")
    k8s = platforms.PLATFORMS["k8s_spark"]
    assert k8s.configured() is True
    out = k8s.trigger({"main_file": "local:///opt/jobs/etl.py", "name": "etl"})
    assert out["run_ref"] == "local:spark-jobs/etl"
    made = srv.created[-1]
    assert made["path"] == f"{GROUP}/namespaces/spark-jobs/sparkapplications"
    assert made["body"]["spec"]["image"] == "registry.local/spark:3.5"      # the cluster's image
    assert "cluster" not in made["body"]
    assert k8s.status(out["run_ref"])["metrics"]["sparkApplicationId"] == "app-local"
    assert k8s.logs(out["run_ref"]) == "driver log from local"


# ── Which cluster a job runs on ─────────────────────────────────────────

def test_no_cloud_cluster_means_local_and_the_order_is_aks_gke_server_local(servers, monkeypatch):
    local, env, gke, aks = (servers(t) for t in ("local", "env", "gke", "aks"))
    k8s = platforms.PLATFORMS["k8s_spark"]
    assert k8s.configured() is False
    _put("local", local)
    assert k8s.pick()[0] == "local"
    monkeypatch.setenv("K8S_API_URL", env.url)
    monkeypatch.setenv("K8S_TOKEN", "tok")
    monkeypatch.setenv("K8S_NAMESPACE", "ns1")
    assert k8s.pick()[0] == "default"
    _put("gke", gke)
    assert k8s.pick()[0] == "gke"
    _put("aks", aks)
    assert k8s.pick()[0] == "aks"
    assert [c["kind"] for c in clusters.list_clusters(ADMIN)["clusters"] if c["connected"]] == [
        "aks", "gke", "local"]
    assert clusters.list_clusters(ADMIN)["default"] == "aks"
    # A job runs where it says, whatever else is connected.
    refs = {name: k8s.trigger({"main_file": "local:///x.py", "cluster": name, "name": f"j-{name}"})["run_ref"]
            for name in ("aks", "gke", "local", "default")}
    assert refs == {"aks": "aks:spark-jobs/j-aks", "gke": "gke:spark-jobs/j-gke",
                    "local": "local:spark-jobs/j-local", "default": "ns1/j-default"}
    for name, srv in (("aks", aks), ("gke", gke), ("local", local), ("default", env)):
        assert len(srv.created) == 1
        assert srv.created[0]["body"]["metadata"]["name"] == f"j-{name}"
        # Status and logs ask the cluster the run went to.
        assert k8s.status(refs[name])["metrics"]["sparkApplicationId"] == f"app-{srv.tag}"
        assert k8s.logs(refs[name]) == f"driver log from {srv.tag}"
    # None named: the first connected.
    assert k8s.trigger({"main_file": "local:///x.py"})["run_ref"].startswith("aks:")


def test_a_cluster_named_but_not_connected_is_refused(servers):
    _put("local", servers("local"))
    k8s = platforms.PLATFORMS["k8s_spark"]
    with pytest.raises(RuntimeError, match="Azure Kubernetes Service is not connected"):
        k8s.trigger({"main_file": "local:///x.py", "cluster": "aks"})
    with pytest.raises(RuntimeError, match="Unknown cluster 'eks'"):
        k8s.trigger({"main_file": "local:///x.py", "cluster": "eks"})
    # A run on a cluster since removed: its status says so, its logs are empty.
    state = k8s.status("gke:spark-jobs/old")
    assert state["state"] == "unknown" and "Google Kubernetes Engine is not connected" in state["detail"]
    assert k8s.logs("gke:spark-jobs/old") == ""


def test_the_manifest_keeps_the_clusters_namespace_and_account(servers):
    srv = servers("gke")
    _put("gke", srv, namespace="team-a", service_account="team-a-spark")
    k8s = platforms.PLATFORMS["k8s_spark"]
    out = k8s.trigger({"apiVersion": "sparkoperator.k8s.io/v1beta2", "kind": "SparkApplication",
                       "cluster": "gke", "metadata": {"name": "full", "namespace": "kube-system"},
                       "spec": {"image": "x:1", "mainApplicationFile": "local:///x.py"}})
    assert out["run_ref"] == "gke:team-a/full"
    body = srv.created[-1]["body"]
    assert body["metadata"]["namespace"] == "team-a" and "cluster" not in body
    k8s.trigger({"main_file": "local:///x.py"})
    assert srv.created[-1]["body"]["spec"]["driver"]["serviceAccount"] == "team-a-spark"


def test_a_rotated_secret_reads_as_not_connected(servers, monkeypatch):
    _put("local", servers("local"))
    assert platforms.PLATFORMS["k8s_spark"].configured() is True
    from app import bootstrap
    monkeypatch.setattr(bootstrap, "jwt_secret", lambda: "a-different-secret")
    assert clusters.stored() == {}
    assert platforms.PLATFORMS["k8s_spark"].configured() is False


# ── The routes: admin only; saved once it signs in; never read back ─────

def test_saving_needs_a_working_sign_in_and_keeps_it_encrypted(servers, pki):
    srv = servers("aks", token="good-token", tls=pki)
    config = {"server": srv.url.replace("127.0.0.1", "localhost"), "token": "wrong", "ca_cert": pki["ca"]}
    with pytest.raises(HTTPException) as e:
        clusters.save_cluster("aks", clusters.ClusterIn(config=config), ADMIN)
    assert e.value.status_code == 400 and list(e.value.detail["fields"]) == ["token"]
    assert clusters.stored() == {}
    with pytest.raises(HTTPException) as e:
        clusters.save_cluster("aks", clusters.ClusterIn(config={**config, "server": "nope"}), ADMIN)
    assert e.value.detail["stage"] == "check" and srv.seen[-1]["auth"] == "Bearer wrong"   # never sent
    saved = clusters.save_cluster("aks", clusters.ClusterIn(config={**config, "token": "good-token"}), ADMIN)
    assert saved == {"kind": "aks", "label": "Azure Kubernetes Service", "connected": True,
                     "server_host": "localhost", "namespace": "spark-jobs", "auth": "token",
                     "verified_tls": True, "image": ""}
    with db.connect() as c:
        raw = c.execute("SELECT config FROM compute_clusters WHERE kind='aks'").fetchone()["config"]
    assert "good-token" not in raw and "BEGIN CERTIFICATE" not in raw
    listed = json.dumps(clusters.list_clusters(ADMIN))
    assert "good-token" not in listed and "BEGIN" not in listed
    assert clusters.remove_cluster("aks", ADMIN) == {"kind": "aks", "connected": False}
    assert clusters.stored() == {}


def test_a_test_reports_field_problems_without_signing_in(servers):
    srv = servers("gke")
    out = clusters.test_cluster(clusters.ClusterIn(config={"server": srv.url, "token": "tok"}), "gke", ADMIN)
    assert out["ok"] is False and list(out["fields"]) == ["server"] and srv.seen == []
    out = clusters.test_cluster(clusters.ClusterIn(config={"server": srv.url, "token": "tok"}), "local", ADMIN)
    assert out["ok"] is True


def test_only_administrators_reach_any_of_it():
    from fastapi.testclient import TestClient
    import app.main as main
    with TestClient(main.app) as c:
        def login(email, password):
            tok = c.post("/api/auth/login", json={"email": email, "password": password}).json()["access_token"]
            return {"Authorization": f"Bearer {tok}"}
        analyst = login("analyst@studio.local", "analyst123")
        admin = login("admin@studio.local", "admin123")
        calls = [("get", "/api/clusters", None), ("post", "/api/clusters/kubeconfig", {"text": "x"}),
                 ("post", "/api/clusters/test?kind=local", {"config": {}}),
                 ("put", "/api/clusters/local", {"config": {}}), ("delete", "/api/clusters/local", None)]
        for method, path, body in calls:
            r = getattr(c, method)(path, headers=analyst, **({"json": body} if body is not None else {}))
            assert r.status_code == 403, (method, path)
            assert getattr(c, method)(path, **({"json": body} if body is not None else {})).status_code in (401, 403)
        r = c.get("/api/clusters", headers=admin)
        assert r.status_code == 200
        assert r.json() == {"clusters": [{"kind": k, "label": clusters.KINDS[k], "connected": False}
                                         for k in ("aks", "gke", "local")],
                            "server_settings": False, "default": None,
                            "order": ["aks", "gke", "default", "local"]}
        r = c.put("/api/clusters/local", headers=admin, json={"config": {"server": "ftp://x"}})
        assert r.status_code == 400 and set(r.json()["detail"]["fields"]) == {"server", "token"}


# ── Chat: "on AKS", "locally" ───────────────────────────────────────────

@pytest.fixture
def queued(monkeypatch):
    seen = []

    def submit(kind, target, payload, user, **kw):
        seen.append(json.loads(payload))
        return {"id": kw["job_id"], "status": "awaiting_approval", "kind": kind, "target": target}
    monkeypatch.setattr(supervisor, "submit", submit)
    return seen


def test_with_only_a_local_cluster_a_job_naming_none_is_queued_for_it(servers, queued):
    _put("local", servers("local"))
    request = chat_platforms.intent("run /opt/jobs/etl.py on spark on kubernetes with image a/b:1")
    assert request["payload"] == {"main_file": "local:///opt/jobs/etl.py", "image": "a/b:1"}
    card = chat_platforms.submit(request, ANALYST, request_id="r-local")
    assert card["status"] == "awaiting_approval"
    assert queued == [{"main_file": "local:///opt/jobs/etl.py", "image": "a/b:1", "cluster": "local"}]
    assert card["payload"]["cluster"] == "local"            # the card says where


def test_the_servers_own_cluster_is_not_named_in_the_payload(servers, queued, monkeypatch):
    env = servers("env")
    monkeypatch.setenv("K8S_API_URL", env.url)
    monkeypatch.setenv("K8S_TOKEN", "tok")
    chat_platforms.submit(chat_platforms.intent("run /opt/x.py on k8s with image a:1"), ANALYST,
                          request_id="r-env")
    assert queued == [{"main_file": "local:///opt/x.py", "image": "a:1"}]


@pytest.mark.parametrize("prompt,cluster", [
    ("run /opt/x.py on AKS with image a:1", "aks"),
    ("launch /opt/x.py on Azure Kubernetes Service with image a:1", "aks"),
    ("run /opt/x.py on GKE with image a:1", "gke"),
    ("run /opt/x.py on google kubernetes engine with image a:1", "gke"),
    ("run /opt/x.py on spark on kubernetes locally with image a:1", "local"),
    ("run /opt/x.py on the local cluster on k8s with image a:1", "local"),
    ("run /opt/x.py on minikube with image a:1", "local"),
    ("kick off /opt/x.py on docker desktop with image a:1", "local"),
    ("run /opt/x.py on a kind cluster with image a:1", "local"),
])
def test_a_cluster_is_picked_in_words(servers, prompt, cluster):
    for kind in ("aks", "gke", "local"):
        _put(kind, servers(kind))
    request = chat_platforms.intent(prompt)
    assert request["target"] == "k8s_spark", prompt
    assert request["payload"] == {"main_file": "local:///opt/x.py", "image": "a:1", "cluster": cluster}
    assert request["missing"] == []


def test_cluster_names_inside_a_path_are_not_clusters(servers):
    _put("local", servers("local"))
    request = chat_platforms.intent("submit s3a://gke-lake/aks/agg.jar to spark on k8s with class "
                                    "com.acme.Agg using image my/spark:3.5")
    assert request["payload"]["main_file"] == "s3a://gke-lake/aks/agg.jar"
    assert "cluster" not in request["payload"] and request["missing"] == []


def test_a_cluster_not_connected_or_two_at_once_is_said(servers):
    _put("local", servers("local"))
    request = chat_platforms.intent("run /opt/x.py on AKS with image a:1")
    assert request["missing"] == ["cluster: AKS is not connected — an administrator connects it in "
                                  "Integrations › Compute for Spark"]
    request = chat_platforms.intent("run /opt/x.py on aks or gke with image a:1")
    assert request["missing"] == ["cluster: name one cluster — AKS, GKE or local"]


def test_the_clusters_image_is_the_default_and_a_card_takes_a_cluster(servers, queued):
    _put("local", servers("local"), image="registry.local/spark:3.5")
    request = chat_platforms.intent("run /opt/x.py on minikube")
    assert request["payload"] == {"main_file": "local:///opt/x.py", "cluster": "local",
                                  "image": "registry.local/spark:3.5"}
    assert request["missing"] == []
    # A card waiting for input takes "on GKE" / "locally" alone.
    card = chat_platforms._artifact("k8s_spark", {"main_file": "local:///opt/x.py", "image": "a:1",
                                                  "cluster": "gke"}, ANALYST,
                                    missing=["cluster: GKE is not connected"])
    request = chat_platforms.intent("locally", card)
    assert request["payload"] == {"main_file": "local:///opt/x.py", "image": "a:1", "cluster": "local"}
    assert request["missing"] == []
    # A panel payload without an image runs with the cluster's, which the approver sees.
    request = chat_platforms.intent(None, target="k8s_spark", payload={"main_file": "local:///y.py"})
    assert request["missing"] == []
    chat_platforms.submit(request, ANALYST, request_id="r-image")
    assert queued[-1] == {"main_file": "local:///y.py", "cluster": "local",
                          "image": "registry.local/spark:3.5"}
