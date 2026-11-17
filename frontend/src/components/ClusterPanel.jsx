// A Kubernetes cluster Spark jobs run on — AKS, GKE or a local one — set up
// by an administrator on the Integrations screen: paste the cluster's
// kubeconfig (or type its server and a token), test that Studio can sign in
// and the Spark operator answers, then save. The server encrypts what is
// saved and never sends a credential back, so replacing one means entering
// it again.
import { useState } from "react";
import { api } from "../api";

/** Integrations tile id → the server's cluster kind. */
export const CLUSTER_TILES = { aks: "aks", gke: "gke", k8s_local: "local" };

export const ORDER_TEXT = "A Spark job that names no cluster runs on AKS, then GKE, then the "
  + "server’s own settings, then Local Kubernetes — so with no cloud cluster connected, Spark "
  + "runs on the local one.";

const ACCESS = ["Give Studio an account that may run Spark jobs — and nothing else",
                "kubectl apply -f deploy/spark-k8s/studio-access.yaml"];
const OPERATOR = ["Install the Spark operator, watching that namespace",
                  "helm repo add spark-operator https://kubeflow.github.io/spark-operator\n"
                  + "helm install spark-operator spark-operator/spark-operator -n spark-operator "
                  + "--create-namespace --set \"spark.jobNamespaces={spark-jobs}\" --wait"];
const TOKEN = ["Copy the account's token into the token field below",
               "kubectl -n spark-jobs get secret studio-submitter-token "
               + "-o jsonpath='{.data.token}' | base64 -d"];
const PASTE = ["Paste the kubeconfig below — Studio reads the server and its CA certificate",
               "kubectl config view --minify --raw"];

/** The steps for each kind of cluster: [what, command]. Exported for tests. */
export const STEPS = {
  aks: [["Point kubectl at the cluster", "az aks get-credentials -g <resource-group> -n <cluster>"],
        ACCESS, OPERATOR, PASTE, TOKEN],
  gke: [["Point kubectl at the cluster",
         "gcloud container clusters get-credentials <cluster> --region <region>"],
        ACCESS, OPERATOR, PASTE, TOKEN],
  local: [["Make one with kind, the Spark operator and Studio's access in one go (needs Docker, "
           + "kind, kubectl and Helm) — or run the steps for AKS on minikube or Docker Desktop",
           "deploy/spark-k8s/local-up.sh"],
          ["Paste its kubeconfig below — it signs in with a client certificate",
           "kind get kubeconfig --name studio"]],
};

const FIELDS = [
  { key: "server", label: "API server", placeholder: "https://…", wide: true, required: true },
  { key: "token", label: "Service-account token", secret: true, wide: true },
  { key: "ca_cert", label: "CA certificate (PEM, or the kubeconfig's certificate-authority-data)",
    area: true, wide: true },
  { key: "namespace", label: "Namespace for Spark jobs", placeholder: "spark-jobs" },
  { key: "service_account", label: "Spark service account", placeholder: "spark-operator-spark" },
  { key: "image", label: "Default Spark image (optional)", placeholder: "apache/spark:4.0.4",
    wide: true },
];

// Same reason as the connect form: keep browsers and password managers out.
const NO_AUTOFILL = { "data-1p-ignore": "true", "data-lpignore": "true",
                      "data-bwignore": "true", "data-form-type": "other" };

const EMPTY = { server: "", token: "", ca_cert: "", client_cert: "", client_key: "",
                namespace: "", service_account: "", image: "", insecure: false };

/** True once the form has a server and a way to sign in. Exported for tests. */
export function clusterReady(cfg) {
  return Boolean(cfg.server.trim()
    && (cfg.token.trim() || (cfg.client_cert.trim() && cfg.client_key.trim())));
}

/** The form after a kubeconfig is read: what it had, with what the kubeconfig
 * says filled in (an empty field in the kubeconfig never clears a typed one). */
export function withKubeconfig(cfg, fields) {
  const out = { ...cfg };
  for (const [k, v] of Object.entries(fields || {})) {
    if (k === "insecure") out.insecure = Boolean(v);
    else if (k in EMPTY && v) out[k] = String(v);
  }
  return out;
}

export default function ClusterPanel({ kind, label, state, defaultKind, isAdmin, reload,
                                       onNote, onError }) {
  const [cfg, setCfg] = useState({ ...EMPTY });
  const [kubeconfig, setKubeconfig] = useState("");
  const [notes, setNotes] = useState([]);
  const [busy, setBusy] = useState("");
  const [test, setTest] = useState(null);
  const [problems, setProblems] = useState({});
  const connected = Boolean(state?.connected);

  if (!isAdmin) {
    return (
      <div className="conn-detail">
        <div className="meta">
          An administrator connects Spark clusters. Once one is connected, ask in the chat — “run
          etl.py on Spark on Kubernetes”, or name the cluster: “…on AKS”, “…on GKE”, “…locally”.
        </div>
        <div className="meta">{ORDER_TEXT}</div>
      </div>
    );
  }

  const set = (key, value) => {
    setCfg((c) => ({ ...c, [key]: value }));
    setProblems((p) => { const { [key]: _, ...rest } = p; return rest; });
    setTest(null);
  };

  async function read() {
    setBusy("read");
    onError("");
    try {
      const d = await api("/clusters/kubeconfig", { method: "POST",
                                                     body: JSON.stringify({ text: kubeconfig }) });
      setCfg((c) => withKubeconfig(c, d.fields));
      setNotes(d.notes || []);
      setKubeconfig("");        // read once; the fields below carry it on
      setTest(null);
      setProblems({});
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function runTest() {
    setBusy("test");
    onError("");
    try {
      const d = await api(`/clusters/test?kind=${encodeURIComponent(kind)}`,
                          { method: "POST", body: JSON.stringify({ config: cfg }) });
      setTest(d);
      setProblems(d.fields || {});
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function save() {
    setBusy("save");
    onError("");
    try {
      const d = await api(`/clusters/${encodeURIComponent(kind)}`,
                          { method: "PUT", body: JSON.stringify({ config: cfg }) });
      setCfg({ ...EMPTY });
      setNotes([]);
      setTest(null);
      setProblems({});
      onNote(`${label} connected — Spark jobs run in “${d.namespace}” on ${d.server_host}.`);
      reload();
    } catch (e) {
      setProblems(e.detail?.fields || {});
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function remove() {
    if (!confirm(`Remove ${label}? Spark jobs that name it will not run until it is connected `
                 + "again; running jobs keep running on the cluster.")) return;
    setBusy("remove");
    onError("");
    try {
      await api(`/clusters/${encodeURIComponent(kind)}`, { method: "DELETE" });
      onNote(`${label} removed.`);
      reload();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  }

  const field = (f) => (
    <label key={f.key} className={`conn-field${f.wide ? " conn-field-wide" : ""}`}>
      <span className="conn-label">
        {f.label}{f.required && <b className="conn-req"> *</b>}
      </span>
      {f.area ? (
        <textarea className={`cluster-pem${problems[f.key] ? " conn-input-bad" : ""}`}
          aria-invalid={problems[f.key] ? "true" : undefined} rows={3} spellCheck={false}
          value={cfg[f.key]} onChange={(e) => set(f.key, e.target.value)} disabled={!!busy}
          {...NO_AUTOFILL} />
      ) : (
        <input type={f.secret ? "password" : "text"} name={`cluster-${kind}-${f.key}`}
          className={problems[f.key] ? "conn-input-bad" : undefined}
          aria-invalid={problems[f.key] ? "true" : undefined}
          placeholder={f.placeholder || ""} value={cfg[f.key]}
          onChange={(e) => set(f.key, e.target.value)} disabled={!!busy}
          {...NO_AUTOFILL} autoComplete={f.secret ? "new-password" : "off"} />
      )}
      {problems[f.key] && <span className="conn-field-error">{problems[f.key]}</span>}
    </label>
  );

  return (
    <div className="conn-detail cluster-panel">
      {connected ? (
        <div className="cluster-status" role="status">
          <span className="m365-badge m365-badge-on">connected</span>
          <span>
            <b>{state.server_host}</b> · namespace <b>{state.namespace}</b> · signs in with a{" "}
            {state.auth === "token" ? "token" : "client certificate"}
            {state.verified_tls ? "" : " · TLS not verified"}
            {state.image ? <> · image <code>{state.image}</code></> : null}
          </span>
          <button className="chip" onClick={remove} disabled={!!busy}>
            {busy === "remove" ? "removing…" : "✕ Remove"}
          </button>
        </div>
      ) : null}
      <div className="meta">
        {defaultKind === kind
          ? "Spark jobs that name no cluster run here. "
          : ""}
        {ORDER_TEXT}
      </div>
      {kind === "local" && (
        <div className="meta conn-warn">
          Studio has to reach the cluster: a cluster on your laptop works with a Studio running on
          that laptop (or your network), not with a Studio hosted in the cloud.
        </div>
      )}

      <details className="cluster-steps" open={!connected}>
        <summary>Set up the cluster</summary>
        <ol>
          {STEPS[kind].map(([what, command]) => (
            <li key={command}>{what}<pre><code>{command}</code></pre></li>
          ))}
        </ol>
      </details>

      <div className="conn-field conn-field-wide">
        <span className="conn-label">
          {connected ? "Replace it — paste a kubeconfig" : "Paste a kubeconfig"}
        </span>
        <textarea className="cluster-pem" rows={4} spellCheck={false} value={kubeconfig}
          aria-label="Kubeconfig" placeholder="apiVersion: v1&#10;clusters: …"
          onChange={(e) => setKubeconfig(e.target.value)} disabled={!!busy} {...NO_AUTOFILL} />
        <div className="m365-actions">
          <button className="chip" onClick={read} disabled={!!busy || !kubeconfig.trim()}>
            {busy === "read" ? "reading…" : "Fill in from kubeconfig"}
          </button>
        </div>
        {notes.map((n) => <span key={n} className="meta cluster-note">{n}</span>)}
      </div>

      <div className="conn-form">
        {FIELDS.map(field)}
        <details className="conn-field conn-field-wide cluster-cert"
          open={Boolean(cfg.client_cert || cfg.client_key)}>
          <summary>Sign in with a client certificate instead</summary>
          {field({ key: "client_cert", label: "Client certificate (PEM)", area: true, wide: true })}
          {field({ key: "client_key", label: "Client key (PEM)", area: true, wide: true })}
        </details>
        <label className="conn-field conn-field-wide cluster-check">
          <span>
            <input type="checkbox" checked={cfg.insecure} disabled={!!busy}
              onChange={(e) => set("insecure", e.target.checked)} />{" "}
            Skip TLS verification — only for a local cluster whose certificate does not name this
            address
          </span>
        </label>
      </div>

      {test && (test.ok
        ? <div className="meta share-notice">✓ {test.said}</div>
        : <div className="error">{test.said}</div>)}

      <div className="m365-actions">
        <button className="chip" onClick={runTest} disabled={!!busy || !clusterReady(cfg)}>
          {busy === "test" ? "signing in…" : "⚡ Test connection"}
        </button>
        <button className="primary" onClick={save} disabled={!!busy || !clusterReady(cfg)}>
          {busy === "save" ? "connecting…" : connected ? "✓ Replace" : "✓ Connect"}
        </button>
      </div>
      <div className="meta">
        Saved only once Studio signs in and the Spark operator answers. Credentials are encrypted
        at rest and never displayed again; every Spark job still needs an administrator's
        approval before it runs.
      </div>
    </div>
  );
}
