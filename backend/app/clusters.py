"""Kubernetes clusters that Spark jobs run on: AKS, GKE and a local cluster.

The Spark on Kubernetes platform (platforms.K8sSparkPlatform) submits
SparkApplication resources to the Spark operator over the Kubernetes API.
Its clusters are set up here by an administrator, one of each kind:

    aks    Azure Kubernetes Service
    gke    Google Kubernetes Engine
    local  a Kubernetes on your own machine or network — kind, minikube,
           Docker Desktop, k3d

…plus the deployment's own K8S_API_URL / K8S_TOKEN ("default"), as before.
A job runs on the cluster it names; one that names none runs on the first
connected of aks, gke, default, local — so with no cloud cluster connected,
Spark runs on the local one.

Sign-in is what works without a cloud CLI inside Studio: a service-account
token (any cluster; deploy/spark-k8s/studio-access.yaml makes one) or a
client certificate (the kubeconfigs kind, minikube and Docker Desktop
write). TLS is verified against the cluster's own CA; skipping verification
is a per-cluster choice. Credentials are stored encrypted and never returned.
"""
import base64
import binascii
import functools
import hashlib
import json
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db
from .auth import current_user

router = APIRouter(prefix="/clusters", tags=["clusters"])

KINDS = {"aks": "Azure Kubernetes Service", "gke": "Google Kubernetes Engine",
         "local": "Local Kubernetes"}
#: Which cluster a job that names none runs on: the first connected.
ORDER = ("aks", "gke", "default", "local")
DEFAULT_NAMESPACE = "spark-jobs"
DEFAULT_SERVICE_ACCOUNT = "spark-operator-spark"
_SALT = b"studio-compute-clusters-v1"
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
_DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_GROUP = "/apis/sparkoperator.k8s.io/v1beta2"


def init_tables():
    with db.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS compute_clusters (
            kind TEXT PRIMARY KEY, config TEXT NOT NULL, updated_by TEXT, updated_at REAL)""")
        c.commit()


# ── Storage (encrypted) ─────────────────────────────────────────────────

@functools.lru_cache(maxsize=2)
def _fernet_for(secret):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=_SALT, iterations=200_000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret.encode())))


def _fernet():
    from . import bootstrap
    secret = bootstrap.jwt_secret() or ""
    if not secret:
        raise RuntimeError("STUDIO_SECRET is not set; cannot derive the encryption key")
    return _fernet_for(secret)


def stored():
    """{kind: config} for the clusters set up here whose config decrypts."""
    try:
        with db.connect() as c:
            rows = c.execute("SELECT kind, config FROM compute_clusters").fetchall()
    except Exception:
        return {}                       # no table yet: nothing set up
    out = {}
    for r in rows:
        try:
            out[r["kind"]] = json.loads(_fernet().decrypt(r["config"].encode()).decode())
        except (InvalidToken, RuntimeError, ValueError):
            continue                    # a rotated STUDIO_SECRET: as if not set up
    return out


def store(kind, cfg, user_id):
    """Keep a checked config, encrypted (the route checks; this only writes)."""
    token = _fernet().encrypt(json.dumps(cfg).encode()).decode()
    with db.connect() as c:
        c.execute("INSERT INTO compute_clusters (kind, config, updated_by, updated_at) VALUES (?,?,?,?) "
                  "ON CONFLICT(kind) DO UPDATE SET config=excluded.config, updated_by=excluded.updated_by, "
                  "updated_at=excluded.updated_at", (kind, token, user_id, time.time()))
        c.commit()


def env_cluster():
    """The deployment's own cluster (K8S_API_URL / K8S_TOKEN), or None."""
    base = os.getenv("K8S_API_URL", "").rstrip("/")
    token = os.getenv("K8S_TOKEN", "")
    if not (base and token):
        return None
    return {"base": base, "token": token, "namespace": os.getenv("K8S_NAMESPACE", "default"),
            "verify": os.getenv("K8S_VERIFY_SSL", "true").lower() not in ("false", "0", "no"),
            "ca_cert": _pem(os.getenv("K8S_CA_CERT", "")) or "", "client_cert": "", "client_key": "",
            "service_account": os.getenv("K8S_SERVICE_ACCOUNT", DEFAULT_SERVICE_ACCOUNT),
            "image": "", "label": "Kubernetes (server settings)"}


def all_clusters():
    """[(name, config)] connected, in ORDER — a job naming none runs on the first."""
    found = {k: _runtime(k, v) for k, v in stored().items() if k in KINDS}
    env = env_cluster()
    if env:
        found["default"] = env
    return [(name, found[name]) for name in ORDER if name in found]


def _runtime(kind, cfg):
    """A stored config in the shape the platform reads."""
    return {"base": cfg["server"], "token": cfg.get("token", ""),
            "namespace": cfg.get("namespace") or DEFAULT_NAMESPACE,
            "verify": not cfg.get("insecure"), "ca_cert": cfg.get("ca_cert", ""),
            "client_cert": cfg.get("client_cert", ""), "client_key": cfg.get("client_key", ""),
            "service_account": cfg.get("service_account") or DEFAULT_SERVICE_ACCOUNT,
            "image": cfg.get("image", ""), "label": KINDS[kind]}


# ── TLS and sign-in ─────────────────────────────────────────────────────

def _pem(value):
    """PEM text from PEM or from base64 of PEM (a kubeconfig's *-data)."""
    value = str(value or "").strip()
    if not value or "-----BEGIN" in value:
        return value
    try:
        decoded = base64.b64decode(value, validate=True).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return value
    return decoded.strip() if "-----BEGIN" in decoded else value


_CONTEXTS = {}


def ssl_context(cfg):
    """The TLS context a cluster is reached with: its own CA (or the system's),
    verification off only when chosen, and its client certificate if it signs
    in with one. Built once per distinct config."""
    key = hashlib.sha256(json.dumps([cfg.get("verify", True), cfg.get("ca_cert", ""),
                                     cfg.get("client_cert", ""), cfg.get("client_key", "")])
                         .encode()).hexdigest()
    if key in _CONTEXTS:
        return _CONTEXTS[key]
    if not cfg.get("verify", True):
        ctx = ssl._create_unverified_context()
    elif cfg.get("ca_cert"):
        ctx = ssl.create_default_context(cadata=cfg["ca_cert"])
    else:
        ctx = ssl.create_default_context()
    if cfg.get("client_cert") and cfg.get("client_key"):
        # load_cert_chain reads files: a private directory, gone right after.
        with tempfile.TemporaryDirectory(prefix="studio-k8s-") as folder:
            cert, keyfile = os.path.join(folder, "c.pem"), os.path.join(folder, "k.pem")
            for path, text in ((cert, cfg["client_cert"]), (keyfile, cfg["client_key"])):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(text)
            ctx.load_cert_chain(cert, keyfile)
    if len(_CONTEXTS) > 32:
        _CONTEXTS.clear()
    _CONTEXTS[key] = ctx
    return ctx


def headers(cfg):
    return {"Authorization": f"Bearer {cfg['token']}"} if cfg.get("token") else {}


# ── The form ────────────────────────────────────────────────────────────

def _loopback(host):
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def normalize(kind, raw):
    """(config as stored, {field: problem}) — checked before anything signs in."""
    if kind not in KINDS:
        raise HTTPException(404, f"Unknown cluster '{kind}'")
    raw = {k: (v.strip() if isinstance(v, str) else v) for k, v in (raw or {}).items()}
    cfg = {"server": str(raw.get("server") or "").rstrip("/"),
           "token": str(raw.get("token") or ""),
           "ca_cert": _pem(raw.get("ca_cert")),
           "client_cert": _pem(raw.get("client_cert")),
           "client_key": _pem(raw.get("client_key")),
           "insecure": str(raw.get("insecure", "")).lower() in ("1", "true", "yes", "on"),
           "namespace": str(raw.get("namespace") or DEFAULT_NAMESPACE),
           "service_account": str(raw.get("service_account") or DEFAULT_SERVICE_ACCOUNT),
           "image": str(raw.get("image") or "")}
    problems = {}
    url = urlsplit(cfg["server"])
    if url.scheme not in ("https", "http") or not url.hostname:
        problems["server"] = "The API server's address — https://… (kubectl config view shows it)."
    elif url.scheme == "http" and not (kind == "local" and _loopback(url.hostname)):
        problems["server"] = "Use https:// — plain http is only for a local `kubectl proxy`."
    else:
        from .connectors.objectstore import _metadata_host
        if _metadata_host(url.hostname):
            problems["server"] = "That is a cloud metadata address, not a Kubernetes API server."
    if bool(cfg["client_cert"]) != bool(cfg["client_key"]):
        problems["client_key" if cfg["client_cert"] else "client_cert"] = (
            "A client certificate needs its key, and a key its certificate.")
    elif not cfg["token"] and not cfg["client_cert"]:
        problems["token"] = ("Sign in with a service-account token — or, for a local cluster, the "
                             "client certificate and key from its kubeconfig.")
    if cfg["ca_cert"]:
        try:
            ssl.create_default_context(cadata=cfg["ca_cert"])
        except (ssl.SSLError, ValueError):
            problems["ca_cert"] = ("Not a certificate — paste the PEM, or the kubeconfig's "
                                   "certificate-authority-data.")
    if cfg["client_cert"] and cfg["client_key"] and "client_cert" not in problems:
        try:
            ssl_context({"verify": False, "client_cert": cfg["client_cert"],
                         "client_key": cfg["client_key"]})
        except (ssl.SSLError, ValueError, OSError):
            problems["client_cert"] = "That certificate and key do not load as a pair."
    if not _DNS_LABEL.match(cfg["namespace"]):
        problems["namespace"] = "A namespace is lowercase letters, digits and -, up to 63."
    if not _DNS_NAME.match(cfg["service_account"]):
        problems["service_account"] = "A service account name is lowercase letters, digits, - and ."
    if re.search(r"\s", cfg["image"]):
        problems["image"] = "An image reference has no spaces — e.g. apache/spark:3.5.3."
    return cfg, problems


def parse_kubeconfig(text):
    """(form fields, notes) from a pasted kubeconfig — its current context.
    Nothing is kept: the fields go back to the form, which sends them on."""
    import yaml
    if len(text or "") > 200_000:
        raise HTTPException(400, "That kubeconfig is too large to be one cluster's")
    try:
        doc = yaml.safe_load(text or "")
    except yaml.YAMLError:
        raise HTTPException(400, "That is not a kubeconfig (it does not read as YAML)")
    if not isinstance(doc, dict) or not doc.get("clusters"):
        raise HTTPException(400, "That is not a kubeconfig (no clusters in it)")
    named = lambda key: {e.get("name"): e.get(key[:-1]) or {} for e in doc.get(key) or []
                         if isinstance(e, dict)}
    contexts, clusters, users = named("contexts"), named("clusters"), named("users")
    current = doc.get("current-context") or next(iter(contexts), None)
    ctx = contexts.get(current) or {}
    cluster = clusters.get(ctx.get("cluster")) or next(iter(clusters.values()), {})
    user = users.get(ctx.get("user")) or {}
    notes = [f"Read the “{current}” context."] if current and len(contexts) > 1 else []
    fields = {"server": cluster.get("server", ""),
              "ca_cert": _pem(cluster.get("certificate-authority-data", "")),
              "insecure": bool(cluster.get("insecure-skip-tls-verify")),
              "token": user.get("token", ""),
              "client_cert": _pem(user.get("client-certificate-data", "")),
              "client_key": _pem(user.get("client-key-data", "")),
              "namespace": ctx.get("namespace") or ""}
    if cluster.get("certificate-authority") and not fields["ca_cert"]:
        notes.append("The CA certificate is a file on that machine — paste its contents.")
    if user.get("client-certificate") and not fields["client_cert"]:
        notes.append("The client certificate and key are files on that machine — paste their contents.")
    if user.get("exec") or user.get("auth-provider"):
        notes.append("This kubeconfig signs in through a plugin (gke-gcloud-auth-plugin, kubelogin) "
                     "Studio cannot run — sign in with a service-account token instead "
                     "(deploy/spark-k8s/studio-access.yaml makes one).")
    host = urlsplit(fields["server"]).hostname or ""
    if _loopback(host):
        notes.append("The server is on this machine’s loopback: if Studio runs in Docker, use "
                     "host.docker.internal instead (a cluster made by local-up.sh has a certificate "
                     "that names it; for another, tick “Skip TLS verification”).")
    return fields, notes


# ── Testing a cluster ───────────────────────────────────────────────────

def _get(url, runtime, context, timeout=15):
    """One GET to the cluster (platforms.py is the one that runs jobs; this
    module only asks whether it could). RuntimeError naming the HTTP status
    or the network's reason — never the server's words."""
    req = urllib.request.Request(url, headers=headers(runtime))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            r.read(1 << 20)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(str(e.reason))
    except (TimeoutError, OSError) as e:
        raise RuntimeError(f"unreachable: {type(e).__name__} {e}")


def probe(kind, cfg):
    """{ok, said?, field?, stage?} — can this sign-in list SparkApplications
    in the namespace? (signed in, allowed, operator installed, all at once).
    `said` is a fixed sentence: nothing the server answered is repeated."""
    runtime = _runtime(kind, cfg)
    url = f"{runtime['base']}{_GROUP}/namespaces/{runtime['namespace']}/sparkapplications?limit=1"
    try:
        context = ssl_context(runtime)
    except (ssl.SSLError, OSError, ValueError):
        return {"ok": False, "said": "Could not set up TLS with these certificates.",
                "field": "ca_cert", "stage": "check"}
    try:
        _get(url, runtime, context)
    except RuntimeError as exc:
        return {"ok": False, **classify(str(exc), kind, runtime)}
    return {"ok": True, "said": f"Signed in — the Spark operator is ready in “{runtime['namespace']}”."}


def classify(text, kind, runtime):
    low = text.lower()
    if re.search(r"http 401\b", low):
        return {"said": "The cluster refused this sign-in — check the token (or client certificate).",
                "field": "token", "stage": "sign_in"}
    if re.search(r"http 403\b", low):
        return {"said": f"Signed in, but this account may not use SparkApplications in "
                        f"“{runtime['namespace']}” — apply studio-access.yaml, which grants exactly that.",
                "field": "namespace", "stage": "signed_in"}
    if re.search(r"http 404\b", low):
        return {"said": "Signed in, but the Spark operator is not installed on this cluster — install "
                        "it with Helm (see the steps).", "field": None, "stage": "signed_in"}
    if "certificate verify failed" in low or "certificate_verify_failed" in low or "hostname mismatch" in low:
        return {"said": "The cluster’s certificate could not be verified — paste its CA certificate "
                        "(certificate-authority-data in the kubeconfig).",
                "field": "ca_cert", "stage": "sign_in"}
    if re.search(r"refused|timed out|timeout|not known|nodename|name resolution|unreachable|no route|"
                 r"reset|closed|eof", low):
        local = kind == "local" and _loopback(urlsplit(runtime["base"]).hostname or "")
        return {"said": "Could not reach the API server — check the address"
                        + (" (if Studio runs in Docker, the host is host.docker.internal)." if local else "."),
                "field": "server", "stage": "sign_in"}
    status = re.search(r"http (\d{3})\b", low)
    return {"said": f"The cluster answered unexpectedly{f' (HTTP {status.group(1)})' if status else ''}.",
            "field": None, "stage": "sign_in"}


# ── Routes (admin only) ─────────────────────────────────────────────────

def _admin(user):
    if (user or {}).get("role") != "admin":
        raise HTTPException(403, "Only administrators set up compute clusters")


class ClusterIn(BaseModel):
    config: dict


class KubeconfigIn(BaseModel):
    text: str


def _public(kind, cfg):
    url = urlsplit(cfg["server"])
    return {"kind": kind, "label": KINDS[kind], "connected": True, "server_host": url.hostname,
            "namespace": cfg.get("namespace") or DEFAULT_NAMESPACE,
            "auth": "token" if cfg.get("token") else "certificate", "verified_tls": not cfg.get("insecure"),
            "image": cfg.get("image", "")}


@router.get("")
def list_clusters(user=Depends(current_user)):
    """Each kind and whether it is connected (never its credentials), and
    which one a job that names none would run on."""
    _admin(user)
    have = stored()
    rows = [_public(k, have[k]) if k in have else {"kind": k, "label": KINDS[k], "connected": False}
            for k in KINDS]
    env = env_cluster()
    order = [name for name, _ in all_clusters()]
    return {"clusters": rows, "server_settings": bool(env), "default": order[0] if order else None,
            "order": list(ORDER)}


@router.post("/kubeconfig")
def read_kubeconfig(body: KubeconfigIn, user=Depends(current_user)):
    _admin(user)
    fields, notes = parse_kubeconfig(body.text)
    return {"fields": fields, "notes": notes}


@router.post("/test")
def test_cluster(body: ClusterIn, kind: str, user=Depends(current_user)):
    _admin(user)
    cfg, problems = normalize(kind, body.config)
    if problems:
        return {"ok": False, "said": " ".join(problems.values()), "fields": problems, "stage": "check"}
    out = probe(kind, cfg)
    if out.get("field"):
        out["fields"] = {out["field"]: out["said"]}
    return out


@router.put("/{kind}")
def save_cluster(kind: str, body: ClusterIn, user=Depends(current_user)):
    """Set a cluster up — only once it signs in and the operator answers."""
    _admin(user)
    cfg, problems = normalize(kind, body.config)
    if problems:
        raise HTTPException(400, {"error": " ".join(problems.values()), "fields": problems,
                                  "stage": "check"})
    out = probe(kind, cfg)
    if not out["ok"]:
        raise HTTPException(400, {"error": out["said"], "stage": out.get("stage"),
                                  "fields": {out["field"]: out["said"]} if out.get("field") else {}})
    store(kind, cfg, user["id"])
    db.log_activity(user, "cluster_save", prompt=kind, source="k8s_spark")
    return _public(kind, cfg)


@router.delete("/{kind}")
def remove_cluster(kind: str, user=Depends(current_user)):
    _admin(user)
    if kind not in KINDS:
        raise HTTPException(404, f"Unknown cluster '{kind}'")
    with db.connect() as c:
        c.execute("DELETE FROM compute_clusters WHERE kind=?", (kind,))
        c.commit()
    db.log_activity(user, "cluster_remove", prompt=kind, source="k8s_spark")
    return {"kind": kind, "connected": False}
