# Spark on Kubernetes: AKS, GKE or a local cluster

Studio runs Spark jobs as `SparkApplication` resources through the
[Spark operator](https://github.com/kubeflow/spark-operator). An administrator
connects up to three clusters in **Integrations › Compute for Spark**:

| Cluster | What it is |
|---|---|
| Azure Kubernetes Service | an AKS cluster |
| Google Kubernetes Engine | a GKE cluster |
| Local Kubernetes | kind, minikube, Docker Desktop or k3d on your own machine or network |

The deployment's own `K8S_API_URL` / `K8S_TOKEN` settings still work, as a
fourth cluster ("the server's settings").

**Where a job runs.** A job that names a cluster runs there: "run etl.py on
AKS", "…on GKE", "…locally", "…on minikube". A job that names none runs on
the first connected of **AKS → GKE → the server's settings → Local**. With no
cloud cluster connected, Spark runs on the local one. The card sent for
approval says which cluster it is, and the job goes there even if another
cluster is connected before an administrator approves it.

Every cluster needs the same two things: the Spark operator, and an account
for Studio that may run Spark jobs in one namespace and nothing else
([`studio-access.yaml`](studio-access.yaml)).

## AKS

```sh
az aks get-credentials -g <resource-group> -n <cluster>
kubectl apply -f deploy/spark-k8s/studio-access.yaml
helm repo add spark-operator https://kubeflow.github.io/spark-operator
helm install spark-operator spark-operator/spark-operator -n spark-operator \
  --create-namespace --set "spark.jobNamespaces={spark-jobs}" --wait
kubectl config view --minify --raw        # paste into Studio
kubectl -n spark-jobs get secret studio-submitter-token \
  -o jsonpath='{.data.token}' | base64 -d # paste as the token
```

Studio reads the API server address and its CA certificate from the pasted
kubeconfig. The kubeconfig's own sign-in goes through `kubelogin`, which
Studio cannot run, so Studio signs in with the token instead.

## GKE

```sh
gcloud container clusters get-credentials <cluster> --region <region>
```

Then follow the same steps as for AKS. The kubeconfig signs in through
`gke-gcloud-auth-plugin`, so here too Studio uses the token.

## Local

```sh
deploy/spark-k8s/local-up.sh              # needs Docker, kind, kubectl, Helm
kind get kubeconfig --name studio         # paste into Studio
```

`local-up.sh` creates a kind cluster named `studio`, installs the operator and
applies `studio-access.yaml`. You can run it again safely. A kind kubeconfig
signs in with a client certificate, and Studio uses that certificate as it is.
For minikube or Docker Desktop, run the AKS steps from `kubectl apply` on.

**Studio has to reach the cluster.** A cluster on your laptop works with a
Studio that runs on that laptop or on your network. A Studio hosted in the
cloud cannot reach it, so use AKS or GKE there.

If Studio runs in Docker on the same machine, `127.0.0.1` points at the
container, not at your machine. Use one of these:

- Use `https://host.docker.internal:<port>` as the server (Docker Desktop).
  `local-up.sh` makes a cluster whose certificate names that host, so TLS
  stays verified.
- Join Studio's container to kind's network with
  `docker network connect kind <studio-container>`, then paste
  `kind get kubeconfig --name studio --internal` (Linux).

## The job

A job's file must be where the driver can read it: inside the image
(`local:///opt/jobs/etl.py`) or in object storage the image can read
(`s3a://…`, `abfss://…`, `gs://…`). The image comes from the first of these
that is set:

1. the image the request names ("with image my/spark:3.5");
2. the cluster's default image, set on its tile;
3. `K8S_SPARK_IMAGE`.

If none is set, Studio asks for an image.

The operator's `spark-operator-spark` account runs the driver; set another one
on the cluster's tile if you use your own.
