#!/usr/bin/env bash
# A local Kubernetes for Spark jobs from Studio, in one go: a kind cluster, the
# Spark operator watching the spark-jobs namespace, and Studio's access to it
# (studio-access.yaml). Safe to run again: each step is skipped or upgraded.
#
#   deploy/spark-k8s/local-up.sh            # cluster "studio"
#   CLUSTER=spark deploy/spark-k8s/local-up.sh
#
# Needs Docker (running), kind, kubectl and Helm.
set -euo pipefail

CLUSTER="${CLUSTER:-studio}"
here="$(cd "$(dirname "$0")" && pwd)"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Install $1 first — $2" >&2; exit 1; }
}
need docker "https://docs.docker.com/get-docker/"
need kind "https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
need kubectl "https://kubernetes.io/docs/tasks/tools/"
need helm "https://helm.sh/docs/intro/install/"
docker info >/dev/null 2>&1 || { echo "Start Docker first." >&2; exit 1; }

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  # The API server's certificate also names host.docker.internal, so a Studio
  # running in Docker on this machine can reach it with TLS verified.
  kind create cluster --name "$CLUSTER" --config - <<'KIND'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
kubeadmConfigPatches:
  - |
    kind: ClusterConfiguration
    apiServer:
      certSANs: ["localhost", "127.0.0.1", "host.docker.internal"]
KIND
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

kubectl apply -f "$here/studio-access.yaml"

helm repo add spark-operator https://kubeflow.github.io/spark-operator >/dev/null 2>&1 || true
helm repo update spark-operator >/dev/null
helm upgrade --install spark-operator spark-operator/spark-operator \
  --namespace spark-operator --create-namespace \
  --set "spark.jobNamespaces={spark-jobs}" --wait

server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
cat <<DONE

Local Kubernetes is ready for Spark jobs.

In Studio: Integrations › Compute for Spark › Local Kubernetes. Paste the
output of

  kind get kubeconfig --name $CLUSTER

and press "Fill in from kubeconfig", then Test connection and Connect.
(Or type the server, $server, and the token from:
  kubectl -n spark-jobs get secret studio-submitter-token -o jsonpath='{.data.token}' | base64 -d )

Studio running in Docker on this machine: use https://host.docker.internal:${server##*:}
as the server instead.
DONE
