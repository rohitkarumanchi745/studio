# Studio on Kubernetes

This directory is the production-oriented, cloud-neutral deployment path for
Studio's natural-language pipeline runtime. It uses only OCI images, standard
Kubernetes resources and the official Apache Airflow Helm chart. It works on a
conforming managed Kubernetes service; the cloud-specific pieces are the image
registry, ingress controller, secret manager, managed databases and storage
classes.

## Runtime boundary

```text
public ingress -> Studio web -> PostgreSQL / Redis
                      |
                durable job queue
                      v
                Studio worker --RW--> studio-airflow-dags (RWX)
                      |                         |
                      |                         +--RO--> Airflow DAG processor
                      |                         +--RO--> Airflow scheduler/tasks
                      +--> Airflow API (private ClusterIP)
                      +--> Agent Lightning server (private, one replica)
                                      |
                           local controller -> recovery model bridge
                                                    |
                                               hosted/private model

                Studio web/worker -> optional BitNet SQL/chat model
```

Only `studio-web` has an ingress template. Airflow, Agent Lightning, the model
bridge and BitNet use private `ClusterIP` services; Airflow's chart ingress is
disabled, and explicit ingress policies protect both the Studio-owned services
and the `airflow` Helm release. The worker is the only pod with write access to
the generated DAG claim. Component-specific official-chart `extraVolumes`
mount that same claim read-only in the API server, scheduler/LocalExecutor and
DAG processors; the chart's built-in writable DAG persistence mount is
intentionally disabled.

Before the worker can consume a queue row, an init container performs real
`SELECT 1` checks against Studio PostgreSQL and the configured PostgreSQL
source, pings Redis, authenticates to Airflow, requires healthy metadata,
scheduler, and DAG-processor states, calls Lightning and its controller
`/readyz` endpoints, and proves create, fsync, hard-link, read, and cleanup on
the shared DAG claim. The same check remains its readiness probe, while the
worker process independently checks the controller before it claims a queue
row. Studio web uses `/readyz` for database-backed readiness and keeps
`/health` as a non-destructive liveness endpoint.

Provision a SELECT-only warehouse identity for Studio's catalog/planning URL
and a separate, narrowly scoped Airflow identity that can SELECT inputs and
CREATE/INSERT only in the approved pipeline output schema. The example Secrets
use different role names to make that authority split visible. The base sets
`STUDIO_AIRFLOW_OUTPUT_SCHEMA=pipeline_output`; create that schema, grant the
Airflow role write authority there only, and retain the example connection's
`options=-csearch_path=public,pipeline_output` setting. Generated destinations
and dependency reads are schema-qualified, while source inputs resolve from
`public` first, so stale outputs cannot shadow them. Studio's `POSTGRES_DSN`
role should retain SELECT-only access to `public`.
Retain the example connection's 900-second `statement_timeout` and 30-second
`lock_timeout`; generated operators independently enforce the configured
`STUDIO_AIRFLOW_TASK_TIMEOUT_SECONDS=900`. Revoke access to unreviewed
user-defined functions from the Airflow writer (including default `PUBLIC`
function grants in PostgreSQL schemas) and explicitly grant only vetted ones.

`studio-web` has neither the Agent Lightning bearer token nor a NetworkPolicy
route to the Lightning server. The private worker alone submits Studio trace
and recovery rollouts. The server then admits only inert, UUID-bound Studio
trace payloads or the exact deterministic `PipelineRecoveryAgent` class,
environment mapping, metadata digest and typed recovery input. Agent
Lightning's generic local/Kubernetes runner fields are rejected before they
reach its store or controller. Its HTTP model-registry mutation routes are
disabled as well; startup alone registers the fixed recovery endpoint.

## Prerequisites and invariants

- Kubernetes with a CNI that implements and enforces
  `networking.k8s.io/v1` NetworkPolicy. This is a security boundary: the
  recovery bridge spends a provider credential and intentionally has no second
  application token. Prove an unrelated test Pod cannot connect before using
  production credentials.
- A CSI driver that really supports `ReadWriteMany` and POSIX hard links for
  `studio-airflow-dags` and `airflow-logs` (NFS, EFS, Filestore, or Azure Files
  NFS—not SMB), honors pod `fsGroup` on RWX mounts, and permits gid 0 write/read
  bits without squashing them. The worker uses `fsGroupChangePolicy: Always` so
  a root:root claim receives group-write permission; verify this behavior for
  the chosen driver. Two unrelated per-service disks with the same path do not
  work.
- One PostgreSQL database for Studio, one PostgreSQL database for Airflow
  metadata, Redis for Studio's cache, and at least one data warehouse/source.
  Managed services are expected; database Pods are intentionally absent.
- Helm 3.19+ for official Airflow chart `1.22.0`, and a registry that can serve
  immutable image digests to the cluster.
- `agent-lightning-state` must remain `ReadWriteOnce`. `lightning-server` and
  `lightning-controller` must each remain at one replica. Agent Lightning 1.0.1
  stores live state in-process; Studio's wrapper adds restart durability through
  a locked snapshot, not active/active high availability.
- The Studio web deployment remains one replica because its 60-second SSO
  handoff is process-local. Its deployment uses `Recreate`, so even an upgrade
  never creates two handoff maps. Scale it only after externalizing that state.

The default resources are starting points, not capacity promises. Measure
warehouse query concurrency, DAG parse latency and model latency before scaling.

## 1. Build and publish immutable images

From the repository root:

```sh
docker build -t REGISTRY/STUDIO:VERSION .
docker build -f deploy/airflow/Dockerfile -t REGISTRY/STUDIO-AIRFLOW:3.3.1-VERSION .
docker push REGISTRY/STUDIO:VERSION
docker push REGISTRY/STUDIO-AIRFLOW:3.3.1-VERSION
```

Resolve the pushed digests, then replace the `studio` image entry in
`base/kustomization.yaml`. In `airflow-values.yaml`, replace the deliberately
non-existent `images.airflow.digest` value with the registry-reported
`sha256:...` (the chart then renders `repository@digest`; do not put the digest
in `tag`). If using the BitNet overlay, also build and pin it:

```sh
docker build -f serving/Dockerfile.railway -t REGISTRY/STUDIO-BITNET:VERSION serving
docker push REGISTRY/STUDIO-BITNET:VERSION
```

Replace both image entries in `overlays/bitnet/kustomization.yaml`. The supplied
BitNet image targets `amd64`; build a separately tested ARM image before changing
its node selector.

## 2. Provision storage and secrets

Set the intended storage classes in `base/storage.yaml` if the cluster's default
classes do not satisfy RWX/RWO requirements. Then create the namespace and PVCs:

```sh
kubectl apply -f deploy/portable/kubernetes/base/namespace.yaml
kubectl apply -n studio-system -f deploy/portable/kubernetes/base/storage.yaml
kubectl -n studio-system wait --for=jsonpath='{.status.phase}'=Bound pvc/studio-airflow-dags --timeout=5m
kubectl -n studio-system wait --for=jsonpath='{.status.phase}'=Bound pvc/agent-lightning-state --timeout=5m
```

Some CSI drivers bind a PVC only after a consuming Pod is scheduled. In that
case `Pending` at this stage is expected; verify it binds during Helm/deployment.

`secrets.example.yaml` documents exact names and keys but is deliberately not
applied by Kustomize. Create these Secrets through your cloud secret operator or
copy the example outside Git and replace every placeholder:

- `studio-app-secrets`
- `agent-lightning-auth`
- `recovery-model-secrets`
- `airflow-api-user`
- `airflow-metadata`
- `airflow-fernet-key`
- `airflow-api-secret`
- `airflow-jwt-secret`
- `airflow-connections`
- `bitnet-auth` (BitNet overlay only)

Apply the private copy before installing Airflow:

```sh
kubectl apply -n studio-system -f /path/to/your/private/studio-secrets.yaml
```

The Fernet key must be a valid URL-safe encoded 32-byte key. The API and JWT
secrets must be different strong random values. The value at
`agent-lightning-auth/token` is referenced as `AGL_KEY` by the private
server/controller and as `STUDIO_AGL_TOKEN` by the private worker/controller.
Where a workload receives both names they must match, preventing split-brain
authentication configuration. The public web workload receives neither name.

For credential rotation, first alter the real PostgreSQL/warehouse roles, then
run an Airflow one-shot Pod with `airflow users reset-password`, verify token
issuance, update the referenced Secrets, and roll workloads. Changing a Secret
alone does not update a database role or an existing Airflow user. The full
ordering is in [`../README.md`](../README.md#rotate-credentials-without-breaking-the-running-stack).

## 3. Install private Airflow

The values pin Airflow 3.3.1 and use the custom image from
`deploy/airflow/Dockerfile`. They select LocalExecutor, a standalone DAG
processor and API server, persistent logs, external PostgreSQL and no public
Airflow ingress. The official chart owns metadata migration and the initial API
user creation; the latter reads `airflow-api-user`, which Studio also uses.

```sh
helm repo add apache-airflow https://airflow.apache.org
helm repo update
helm upgrade --install airflow apache-airflow/airflow \
  --version 1.22.0 \
  --namespace studio-system \
  --values deploy/portable/kubernetes/airflow-values.yaml \
  --wait --timeout 15m
```

Keep the release name `airflow`. If it changes, update both `AIRFLOW_URL` in
`base/config.yaml` and the `release` selectors in `base/network-policy.yaml`.
Do not enable `ingress.apiServer`: Studio is the public control plane and
obtains a short-lived Airflow v2 token over the private service.

## 4. Deploy Studio and recovery

Hosted/LangChain recovery model:

```sh
kubectl apply -k deploy/portable/kubernetes/base
```

Attested private BitNet for learned SQL/chat scope, while retaining the hosted
recovery model:

```sh
kubectl apply -k deploy/portable/kubernetes/overlays/bitnet
```

The overlay gives `studio-web` and `studio-worker` the private
`STUDIO_LLM_BASE_URL`, the same gateway key, `STUDIO_BITNET_LLM=openai:bitnet`,
and the operator-pinned bootstrap adapter URI/version/SHA-256. That makes the trained
BitNet model eligible for normal learned-scope chat/SQL work. Its adapter is not
used as Lightning's recovery policy: the current trainer's SQL tool-call target
and recovery's retry/repair/escalate target are different contracts. The
configured frontier `STUDIO_LLM` remains the fallback for prompts outside
BitNet's learned scope and for invalid self-hosted planning responses.

Before using BitNet, replace `url`, `version`, and `sha256` in the overlay's
`bitnet-adapter-config` ConfigMap. The URL is forwarded with the model request
and stored in rollout/provenance metadata, so it must be stable and contain no
embedded or signed credentials. Put a gated Hugging Face token in
`bitnet-auth/hugging_face_token`, or expose another credential-free artifact URL
only on the private network. Generate the pinned value from the final GGUF with
`sha256sum tool_call.gguf` (or `shasum -a 256` on macOS), after conversion and
before upload.

On first BitNet boot the engine downloads roughly 1.1 GB to `bitnet-data`, then
downloads the trained GGUF named by `bitnet-adapter-config/url`. The supervisor
rehashes the bytes against `sha256` and atomically records URI, version, and
digest. The overlay sets strict adapter gates on serving and Studio's adapter
registry, so readiness stays false unless llama-server reports adapter id `0`
at the expected path with scale `1.0`, and the mounted/applied identities match
all three pinned fields. A base-only BitNet process therefore cannot receive
Studio model traffic. Training the
PEFT adapter and converting it to GGUF remain prerequisites; deploy readiness
does not claim that model weights have been trained. The Studio registry also
rejects later global tool-adapter releases without a SHA-256 while this overlay
is active, preventing an unattested publish from replacing the pinned route.

Finally adapt and apply `ingress.example.yaml`. It is the only public edge in
this bundle:

```sh
kubectl apply -f /path/to/your/private/studio-ingress.yaml
```

## 5. Prove the live chain

```sh
kubectl -n studio-system rollout status deployment/airflow-api-server --timeout=10m
kubectl -n studio-system rollout status statefulset/airflow-scheduler --timeout=10m
kubectl -n studio-system rollout status deployment/airflow-dag-processor --timeout=10m
kubectl -n studio-system rollout status deployment/recovery-model --timeout=10m
kubectl -n studio-system rollout status deployment/lightning-server --timeout=10m
kubectl -n studio-system rollout status deployment/lightning-controller --timeout=10m
kubectl -n studio-system rollout status deployment/studio-worker --timeout=10m
kubectl -n studio-system rollout status deployment/studio-web --timeout=10m
kubectl -n studio-system exec deployment/lightning-server -- \
  python scripts/run_agent_lightning.py check
kubectl -n studio-system exec deployment/lightning-controller -- \
  python scripts/run_agent_lightning.py controller-check
kubectl -n studio-system exec deployment/lightning-server -- \
  python scripts/smoke_recovery_model.py
kubectl -n studio-system get pods,pvc
```

That smoke generates one harmless diagnosis and validates the same bounded JSON
decision contract used by the recovery agent; it does not execute SQL. The
controller runs it as an init-container gate before its reconciler starts.
The worker's claim gate continuously requires
`http://lightning-controller:8082/readyz`; if the controller dies, queued work
stays queued. A later hosted-provider outage is detected when a recovery call
fails, not by the bridge's configuration-only readiness. Schedule the smoke at
an interval whose provider cost is acceptable and alert on failures.

Also verify NetworkPolicy enforcement from a disposable Pod that does not carry
an allowed component label. The first connection must fail; the second proves
ordinary namespace DNS/networking still works:

```sh
kubectl -n studio-system run policy-negative --rm -i --restart=Never \
  --image=curlimages/curl:8.16.0 -- \
  sh -ec '! curl --connect-timeout 3 --fail http://recovery-model:8081/readyz'
kubectl -n studio-system run policy-dns --rm -i --restart=Never \
  --image=busybox:1.37.0 -- nslookup recovery-model
```

For BitNet, also inspect the honest engine stage and exact adapter attestation;
`mounted_adapter` and `applied_adapter` must be identical:

```sh
kubectl -n studio-system exec deployment/studio-worker -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://bitnet-serving:9000/health', timeout=10).read().decode())"
```

Port-forward Studio for a private smoke test, sign in, submit a read-only natural
language pipeline, review its immutable DAG fingerprint, approve it as an admin,
and wait for the terminal Airflow state:

```sh
kubectl -n studio-system port-forward service/studio-web 8000:8000
curl --fail http://127.0.0.1:8000/readyz
```

The chain is operational only after that real prompt -> review -> approval -> DAG
publication -> Airflow run completes and a deliberately safe failing read-only
run produces a Lightning recovery decision and terminal reward.

## Data durability and learning semantics

- Studio PostgreSQL owns prompts, typed pipeline plans, runs, child recovery
  runs, rewards and `agent_traces`.
- `agent-lightning-state` owns Lightning rollout/event snapshots across restarts.
- `studio-airflow-dags` owns immutable generated DAG artifacts.
- Airflow PostgreSQL/log storage owns orchestration state and task logs.
- `bitnet-data` owns the BitNet base GGUF and any delivered GGUF adapter.

This runtime stores successful/failing experience and feeds it back to Agent
Lightning recovery. It does **not** silently retrain or replace BitNet weights.
Weight training, evaluation, PEFT-to-GGUF conversion, provenance publication and
adapter rollout remain a separately gated ML release process.

Content-addressed DAG revisions intentionally accumulate. Alert at 70% PVC
usage and archive/remove only terminal, retention-expired, paused DAG revisions
after snapshotting Studio and Airflow metadata. The Airflow database cleanup
job does not garbage-collect files from `studio-airflow-dags`.
