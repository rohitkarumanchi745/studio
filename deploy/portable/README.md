# Portable Studio runtime

This directory packages the runtime needed for Studio's natural-language
pipeline path: Studio web and worker, Airflow 3, an Agent Lightning 1.0.1
server and local controller, a separately configured recovery model, and an
optional self-hosted BitNet SQL/chat service. The assets are cloud-neutral OCI/container
configuration. They do not prove that a particular cloud deployment is live.
Run all commands below from the repository root.

Use the Compose stack for development, evaluation, and a single durable host.
Use the Kubernetes manifests and the official Airflow Helm chart for a
production cluster. Apache's own [Docker Compose
guide](https://airflow.apache.org/docs/apache-airflow/stable/howto/docker-compose/index.html)
describes Compose as a quick-start rather than production infrastructure.

## What runs

| Component | Network exposure and authority |
|---|---|
| `studio-web` | The only public application service. It reads/writes Studio's application database and submits approved work. |
| `studio-worker` | Private and most privileged: owns durable jobs, writes compiled DAG files, talks to Airflow and Agent Lightning, and monitors terminal outcomes. Run at least one; use fenced job claims when scaling it. |
| Studio PostgreSQL | Durable users, conversations, jobs, pipeline plans/runs, `agent_traces`, successful recipes, and Agent Lightning delivery state. Private. |
| Redis | Cache and coordination data, not the system of record. Private. |
| Airflow API server, scheduler, and DAG processor | Airflow 3 control plane. The API is loopback-only in Compose and cluster-internal in Kubernetes. LocalExecutor tasks run with the scheduler. |
| Airflow PostgreSQL | Airflow metadata database. Keep it separate from Studio's database. Private. |
| DAG filesystem | Compiled, immutable DAG Python. Studio worker has read/write access; Airflow runtime components get read-only access after initialization. |
| `lightning-server` | Private Agent Lightning API/model proxy. Exactly one server process owns its state. Its admission layer accepts only inert Studio trace records or the fixed `PipelineRecoveryAgent` contract; the bearer token is not a general Python-runner capability. |
| `lightning-controller` | One trusted local controller. It imports `app.recovery_planner` but receives no Studio database or warehouse credential. |
| `recovery-model` | Private OpenAI-compatible bridge. It has only the selected provider/upstream credential and no database or execution tools. |
| `bitnet-serving` | Optional CPU inference service used for Studio's attested, learned SQL/chat model path. Its authenticated API stays private. Lightning recovery remains on the separately configured recovery model. |
| `warehouse` | Disposable PostgreSQL source included by Compose for smoke tests. Point production at an operator-managed warehouse instead. |

The recovery agent recommends `retry`, a typed `repair`, or `escalate`; it
does not execute SQL. Studio remains the authority for retry budgets, current
RBAC, SQL/DAG validation, approval inheritance, trigger idempotency, and child
run creation. Write-capable repairs still require a new administrator approval.
The public `studio-web` process has the Lightning URL so it can enqueue durable
delivery work, but it does not receive the Lightning bearer token. Only the
private worker performs trace/recovery mutations; the controller and server
hold the same token for their bounded runtime roles.

## Platform contract

The stack can run on any provider that supplies all of the following:

- OCI image builds and long-running services/jobs with stable private DNS;
- PostgreSQL for Studio and Airflow, plus Redis;
- a POSIX filesystem with hard-link support shared across services for generated DAG delivery;
- a private persistent volume for Agent Lightning state;
- secrets injection, TLS at public ingress, health probes, and restart policies;
- outbound HTTPS to the chosen recovery/frontier model and, when selected,
  enough CPU and disk for BitNet.

The DAG filesystem requirement is not optional in the current implementation.
The Studio worker must mount the same filesystem read/write at
`/opt/airflow/dags`; Airflow's scheduler and DAG processor must mount it
read-only at that exact path. In a multi-node Kubernetes cluster this means a
`ReadWriteMany` POSIX/NFS volume (for example EFS, Filestore, or Azure Files
mounted through its NFS offering, not Azure Files SMB), with
compatible UID/GID and permissions. Object storage, two same-named local
volumes, or separate per-service PaaS volumes are not the same filesystem.
Platforms that cannot provide this can still trigger an already-deployed DAG,
but cannot use Studio's current prompt-to-new-DAG publication path without an
additional DAG synchronization/deployment mechanism.

Use two warehouse identities for the same source: Studio's `POSTGRES_DSN`
should be catalog/SELECT-only, while `AIRFLOW_CONN_STUDIO_POSTGRES` should be a
different narrowly scoped principal allowed to SELECT approved inputs and
CREATE/INSERT only in the pipeline output schema. Giving the public application
the Airflow writer credential defeats the approval boundary; giving Airflow the
reader credential makes every approved materializing DAG fail.
The supplied stack sets `STUDIO_AIRFLOW_OUTPUT_SCHEMA=pipeline_output`, so
planning rejects unqualified or wrong-schema write destinations and requires
dependency-output reads to use that same qualifier. Its writer connection pins
`search_path=public,pipeline_output`, preventing a stale output from shadowing
an authorized input. The bootstrap role default provides the same setting, but the connection URI carries it too so a
restored or pre-existing volume cannot silently change the resolution order.
Generated operators also have a 900-second execution timeout, while the
PostgreSQL connection pins a 900-second statement timeout and 30-second lock
timeout. Publication waits at most 900 seconds for Airflow registration, and
Studio escalates a still-running or unobservable Airflow run after 24 hours;
that boundary never cancels or blindly retries an external run. Before CTAS or
recovery, the PostgreSQL connector probes only `pg_catalog` for the proposed
output identity, so a partial prior output is detected without granting the
Studio reader access to its rows. The demo bootstrap revokes public-schema function execution from
`PUBLIC`; on a managed warehouse, revoke unreviewed user-defined-function
execution from the Airflow principal and grant back only an operator-reviewed
allowlist. Studio additionally rejects known blocking/state-changing built-ins
and schema-qualified functions in external pipeline SQL.

The Agent Lightning snapshot requires private, persistent `ReadWriteOnce`
storage and exactly one server replica. The controller must also have exactly
one replica. A serverless platform that suspends workers/controllers or offers
only ephemeral filesystems does not meet this runtime contract.
The controller writes a private heartbeat only after its reconciler completes a
real rollout-queue poll. The worker's application-level claim gate checks that
heartbeat before schedulers run or another durable job is claimed; Compose and
Kubernetes probes check it too. A controller outage therefore leaves work
queued instead of silently accumulating unreconciled recovery rollouts.

## Single-host Compose

Prerequisites are Docker Engine, Docker Compose v2, enough disk for the images
and databases, and a configured model for Studio's prompt planner. The default
uses a hosted provider credential. The BitNet override additionally wires the
verified SQL/tool adapter into Studio's learned-scope chat and planning path.
Lightning recovery stays on its independently configured hosted model: the
current SQL adapter is not a recovery-policy adapter. Keep the hosted frontier
configured for new or out-of-scope prompts and safe fallback. No container
image was built or deployed merely by adding these files.

Generate a non-overwriting mode-0600 environment file:

```sh
python3 deploy/portable/generate_env.py
```

The generator refuses to replace an existing `deploy/portable/.env`. Keep that
file out of source control and back it up through your secret manager. Before
starting the default stack, edit it and set `ANTHROPIC_API_KEY`. The defaults
use `anthropic:claude-sonnet-5` for both Studio chat and Lightning recovery. To
use another provider, set all three of `STUDIO_LLM`,
`STUDIO_RECOVERY_UPSTREAM_MODEL`, and its provider credential. The recovery
bridge gives an upstream completion 240 seconds inside Lightning's
300-second rollout deadline, leaving time to validate and persist the decision;
keep `STUDIO_RECOVERY_UPSTREAM_TIMEOUT_S` below
`STUDIO_AGL_RECOVERY_TIMEOUT_S` if you override either value. For production,
keep the generated `STUDIO_DEMO_MODE=0`, use an externally managed warehouse,
and place the generated credentials in a backed-up secret manager. Do not
"rotate" a running database by editing `.env`: PostgreSQL image variables are
first-volume bootstrap inputs, and Airflow's create-user job does not reset an
existing password. Use the coordinated procedure below. Compose
binds Studio to `127.0.0.1` by default. Set `STUDIO_BIND_ADDRESS=0.0.0.0` only
behind an authenticated TLS reverse proxy or equivalent private ingress.

Render the final configuration before creating anything:

```sh
docker compose \
  --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  config --quiet
```

Then build and start the single-host stack:

```sh
docker compose \
  --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  up --build --detach
```

The default host endpoints are Studio at `http://127.0.0.1:8000` and Airflow
at `http://127.0.0.1:8080`; both bind only on loopback. The one-shot
`airflow-init` container should finish successfully; it is not expected to stay
running.

`AIRFLOW_URL` is the private control-plane address used by Studio;
`AIRFLOW_PUBLIC_URL` is used only for links returned to a browser. Compose's
generated value is loopback. In Kubernetes it is intentionally unset, so no
private cluster hostname leaks into the UI; set it only if an authenticated
operator-facing Airflow URL really exists.

### Rotate credentials without breaking the running stack

For an existing volume or managed database, rotate server-side identities
before restarting clients. Take a backup, create the new value in the secret
manager, and apply the changes in this order:

1. As a database administrator, run `ALTER ROLE ... PASSWORD ...` for the
   Studio database role, Airflow metadata role, warehouse reader, and warehouse
   pipeline writer. Quote identifiers/literals through the database driver;
   do not paste passwords into shared shell history. The Compose warehouse
   bootstrap script performs this setup only when the volume is first created.
2. In a private Airflow one-shot container/Pod, run `airflow users
   reset-password --username <AIRFLOW_USERNAME> --password <new secret>`.
   Confirm a token can be issued with the new value before retiring the old
   secret.
3. Update the matching client Secrets/`.env` values, roll Airflow and Studio,
   then run the authenticated Airflow smoke and a `SELECT 1` through both
   warehouse principals. Rotate the Agent Lightning, model-provider, Fernet,
   JWT, API, and Studio signing secrets according to their own documented
   overlap/re-encryption requirements; do not change all of them in one blind
   restart.

To stop containers while retaining named volumes:

```sh
docker compose \
  --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  down
```

Do not add `--volumes` unless deletion of Studio data, Airflow history, the
demo warehouse, Lightning trajectories, and any BitNet model/adapter files is
intentional and backed up.

### Use self-hosted BitNet for learned SQL/chat scope

The override connects Studio directly to the private, authenticated BitNet
gateway. It does not repoint Agent Lightning's recovery model. The adapter
produced by `scripts/train_online.py` learns Studio's single-query SQL/tool
contract; applying those weights to the unrelated retry/repair/escalate JSON
contract would not be evidence-based recovery.

Before rendering the override, set `STUDIO_BITNET_ADAPTER_URL` in the private
`.env` to the published, trained `tool_call.gguf` artifact and set
`STUDIO_BITNET_ADAPTER_VERSION` to its immutable release version. Compute the
artifact digest with `sha256sum tool_call.gguf` (or `shasum -a 256` on macOS)
and set `STUDIO_BITNET_ADAPTER_SHA256` to all 64 hexadecimal characters. The override
intentionally refuses an empty adapter URL: base-only BitNet is not accepted as
the learned SQL policy. The URI becomes provenance recorded in serving
state and model requests, so use a stable private-network/artifact identity and
do not embed a reusable credential in it.

Start the same stack with the BitNet override:

```sh
docker compose \
  --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  -f deploy/portable/compose.bitnet.yaml \
  config --quiet

docker compose \
  --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  -f deploy/portable/compose.bitnet.yaml \
  up --build --detach
```

The BitNet image currently builds the pinned `bitnet.cpp` engine for a modern
`linux/amd64` CPU and downloads the approximately 1.1 GB base GGUF onto the
`bitnet-data` volume on first boot. The configured adapter is also fetched onto
that volume and its URI, release version, and verified SHA-256 are recorded in
an atomic identity sidecar. Existing bytes are rehashed before reuse, and a
download with the wrong digest never replaces the prior file. Startup/readiness
fails until that exact identity is mounted and llama-server's
`GET /lora-adapters` confirms id `0`, the expected path, and scale `1.0`; an
arbitrary mounted file or a successful scale POST alone is not enough. Follow
[`serving/SELFHOST.md`](../../serving/SELFHOST.md) for adapter training,
conversion, publication, and provenance.

The override configures `STUDIO_LLM_BASE_URL`,
`STUDIO_BITNET_LLM`, and the bootstrapped adapter identity on Studio web and
worker. It also rejects a later global `tool_call` registry publication unless
that release includes its SHA-256, so an unattested training result cannot
silently supersede the working adapter. That makes trained BitNet eligible for
ordinary learned-scope chat/SQL work; it does not force every prompt through
BitNet. A dependency-aware Airflow DAG uses a different output contract, so a
failed/invalid BitNet planning attempt is retried with the configured frontier
model. The frontier `STUDIO_LLM` remains authoritative for new/out-of-scope
work and fallback.

## Kubernetes production

The files under `deploy/portable/kubernetes/` deploy Studio web/worker, the
recovery bridge, and the single-server/single-controller Agent Lightning
runtime. Airflow is installed from its official Helm chart with
`deploy/portable/kubernetes/airflow-values.yaml`, using the custom Airflow image
built from `deploy/airflow/Dockerfile`. The complete
[Kubernetes operator guide](kubernetes/README.md) is authoritative for the
manifest layout and post-deploy commands.

Treat the manifests as an operator-owned overlay, not a one-command universal
cloud installer. Before applying them:

1. Build and push immutable, vulnerability-scanned `linux/amd64` images for the
   root `Dockerfile` and `deploy/airflow/Dockerfile`; replace every example
   repository/tag or pin by digest.
2. Provision Studio PostgreSQL, Airflow PostgreSQL, Redis, and the warehouse;
   use TLS/private endpoints and least-privilege accounts.
3. Create the referenced secrets out of band. Never apply the example secret
   document unchanged or commit rendered secrets.
4. Bind `studio-airflow-dags` and `airflow-logs` to a real RWX POSIX storage
   class (or configure remote Airflow logging), and bind
   `agent-lightning-state` to encrypted RWO storage. Verify the same
   `/opt/airflow/dags` mount and security context on Studio worker and Airflow
   pods.
5. Install Airflow first, wait for metadata migration and all three required
   Airflow 3 components, then apply the Studio runtime. Expose only Studio
   through your ingress/gateway; keep Airflow, Lightning, the recovery model,
   databases, and Redis private.

The Kubernetes network policy also denies `studio-web` direct ingress to the
Lightning server. A compromised public web process therefore has neither its
credential nor a permitted network path. The server independently rejects
unknown local/Kubernetes runners, alternate Python classes, additional
environment mappings, and recovery payloads whose deterministic identity or
digest does not match Studio's contract. HTTP model-registry mutations are
also disabled: the one configured proxy model is registered directly during
server startup and cannot be repointed or deleted through the bearer API.

The required Secrets and keys are enumerated in
`kubernetes/secrets.example.yaml`; create them through a cloud secret operator
or from a private copy, never by applying that placeholder document. Hosted
recovery needs `studio-app-secrets`, `agent-lightning-auth`,
`recovery-model-secrets`, `airflow-api-user`, `airflow-metadata`,
`airflow-fernet-key`, `airflow-api-secret`, `airflow-jwt-secret`, and
`airflow-connections`. The BitNet overlay additionally needs `bitnet-auth` and
the operator must replace the credential-free immutable artifact `url` and
`version` in its `bitnet-adapter-config` ConfigMap. That identity is forwarded
and persisted as rollout/provenance metadata; never put an embedded or signed
credential in it. A gated Hugging Face token can instead live separately in
`bitnet-auth`.

After replacing the `studio:portable`, `studio-bitnet:portable`, and
`registry.example.invalid/studio-airflow` image placeholders with pushed
digests, the order is:

```sh
kubectl apply -f deploy/portable/kubernetes/base/namespace.yaml
# Create all required Secrets in studio-system through the chosen secret manager.
kubectl apply -n studio-system \
  -f deploy/portable/kubernetes/base/storage.yaml

helm repo add apache-airflow https://airflow.apache.org
helm repo update
helm upgrade --install airflow apache-airflow/airflow \
  --version 1.22.0 \
  --namespace studio-system \
  --values deploy/portable/kubernetes/airflow-values.yaml \
  --wait --timeout 15m

# Hosted recovery, with the configured frontier model:
kubectl apply -k deploy/portable/kubernetes/base
# Or add the attested BitNet SQL/chat path while retaining hosted recovery:
# kubectl apply -k deploy/portable/kubernetes/overlays/bitnet
```

Adapt `kubernetes/ingress.example.yaml` for the cluster's ingress class,
hostname, and TLS issuer, save it outside Git, and apply that private copy last.
A cloud's managed PostgreSQL, Redis, load balancer, secret manager, and RWX CSI
driver can replace local components; the service names, environment contract,
and trust boundaries stay the same. The base keeps Studio web at one replica
because the 60-second SSO handoff is process-local; scale it only with session
affinity or after externalizing that map.

Airflow's official chart documentation covers [production installation](https://airflow.apache.org/docs/helm-chart/stable/index.html)
and [DAG filesystem options](https://airflow.apache.org/docs/helm-chart/stable/manage-dag-files.html).
Do not deploy a second Lightning server/controller replica as an availability
shortcut: Agent Lightning 1.0.1 uses a process-local store, and Studio's wrapper
deliberately rejects a second writer to prevent split-brain history.

## Readiness and smoke tests

Container readiness is necessary but not sufficient. Run these checks after
every deployment and after changing a model or adapter.

For Compose, keep the same `--env-file` and `-f` arguments used to start it
(include `compose.bitnet.yaml` for BitNet):

```sh
docker compose --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml ps --all

curl --fail --silent --show-error http://127.0.0.1:8000/readyz

docker compose --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  exec lightning-server python scripts/run_agent_lightning.py check

docker compose --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  exec lightning-controller python scripts/run_agent_lightning.py controller-check

docker compose --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  exec studio-worker test -w /opt/airflow/dags

docker compose --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  exec airflow-dag-processor test -r /opt/airflow/dags/.studio-airflow-shared-v1
```

Run Airflow's authenticated cluster smoke with the credentials copied from the
private `.env` file (do not paste them into logs or shell history on a shared
host):

```sh
docker compose --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  exec \
  -e AIRFLOW_URL=http://127.0.0.1:8080 \
  -e AIRFLOW_USERNAME='<value from deploy/portable/.env>' \
  -e AIRFLOW_PASSWORD='<value from deploy/portable/.env>' \
  airflow-api python /opt/studio-airflow/smoke.py
```

That script authenticates through Airflow 3's token endpoint and requires the
metadata database, scheduler, and DAG processor to report healthy. Set
`AIRFLOW_SMOKE_DAG_ID` in the one-off environment to additionally require one
specific, unpaused, import-clean DAG.

For any recovery backend, Compose now makes a real non-streaming completion
through the private `recovery-model` bridge before starting the Lightning
controller. The checked-in smoke uses the same bounded system contract as the
recovery agent, contains no data or credentials, and fails unless the model
returns Studio's typed decision JSON. Re-run it directly when diagnosing or
after rotating the model or adapter:

```sh
docker compose --env-file deploy/portable/.env \
  -f deploy/portable/compose.yaml \
  exec lightning-server python scripts/smoke_recovery_model.py
```

Passing this smoke proves only one bounded recovery-contract response. Repeat
the test set after changing the separately configured recovery model. The
pinned `bitnet.cpp` server does not provide Agent Lightning's vLLM token-ID
extension, so the portable BitNet path is inference-only and does not claim
online weight optimization.

For Kubernetes, wait for every deployment listed in the
[operator smoke sequence](kubernetes/README.md#5-prove-the-live-chain), then run
the same checked-in checks inside the private Lightning pod:

```sh
kubectl -n studio-system exec deployment/lightning-server -- \
  python scripts/run_agent_lightning.py check
kubectl -n studio-system exec deployment/lightning-server -- \
  python scripts/smoke_recovery_model.py
```

Use a local port-forward and `deploy/airflow/smoke.py` with the `airflow-api-user`
Secret to validate Airflow without creating public Airflow ingress.

Finally, prove the user-visible path in a disposable staging tenant:

1. submit a natural-language Airflow DAG plan, inspect the typed tasks and SQL,
   submit it, and approve it as an administrator;
2. verify an immutable DAG appears on the shared volume, imports without error,
   is triggered exactly once, reaches a terminal state, and records the same
   outcome in Studio and Airflow;
3. use a controlled read-only test connector failure, then verify one Lightning
   diagnosis, the bounded retry/repair policy, current-permission revalidation,
   the failed parent and separate child run, and the final reward on the same
   diagnosis rollout;
4. confirm that an uncertain trigger or write-capable correction escalates or
   requests fresh approval instead of replaying automatically.

Do not manufacture a failure against production data to perform this test.

## Data, durability, and learning semantics

| Data | Durable location | Operational note |
|---|---|---|
| Prompts, pipeline actions, execution outcomes, rewards, successful recipes | Studio PostgreSQL, especially `agent_traces` and pipeline/job tables | This is the reusable application record and the source for prompt-to-pipeline memory. Back up and retain under your data policy. |
| Cumulative BitNet trainer replay and unreleased cursor batch | Private trainer volume: `STUDIO_TRAIN_OUTPUT_DIR/.training_replay.json` and `.train_cursor.json` | Mode 0600, bounded, and deliberately outside the web/serving containers. It can contain prompts; isolate it to one trusted organizational deployment. |
| Lightning rollouts, events, decisions, and reward events | `STUDIO_AGL_STATE_PATH` (`/var/lib/agent-lightning/state.json` in Compose) | Studio's wrapper atomically snapshots Agent Lightning 1.0.1's in-memory store after mutations and restores it on boot. |
| Published DAG source | Shared DAG filesystem | Immutable files; back up together with Studio/Airflow metadata if audit retention requires them. |
| Airflow run/task history | Airflow PostgreSQL | Use Airflow's own cleanup and backup policy. |
| BitNet base model and trained SQL/tool adapter | `bitnet-data` or operator-provided model/adapter volume | The portable override requires the exact configured adapter. Preserve the adapter, provenance sidecar, and registry version as one release. It is not the Lightning recovery trajectory store. |

The Lightning snapshot is restart-durable, not a horizontally scalable event
store. It serializes the complete in-memory history on each mutation, so write
cost and snapshot size grow with history. The default hard limit is 256 MiB
(`STUDIO_AGL_MAX_STATE_BYTES`); exceeding it makes mutations/readiness fail
closed. Define a retention/export policy before that point. Run one server and
one local controller, take encrypted volume snapshots/backups, test restores,
and restrict the state directory/file to its service account (the wrapper sets
the file to mode 0600). File permissions are not encryption: require
provider-managed encryption at rest and encrypted backups. Do not log or copy
the JSON to general-purpose diagnostics because it contains prompts and events.

Generated DAG files are content-addressed and never overwritten. Alert before
the DAG claim reaches 70% capacity. Periodically archive/delete only revisions
whose Studio workflow records are terminal, whose Airflow runs are outside the
audit-retention window, and whose DAG is paused; take a metadata/filesystem
snapshot first and confirm the DAG processor is import-clean afterward. Airflow
database cleanup does not remove these Python files.

Recording a prompt, decision, outcome, and reward does **not** fine-tune a
model, publish an adapter, or activate one. The current pipeline-recovery loop
improves immediate reuse through successful recipe retrieval and creates
training material. A separate, explicitly operated training job must select an
eligible corpus, train/validate weights, convert and publish the adapter, record
its provenance, and pass the completion/evaluation gate before serving it.
Structured DAG actions are not automatically fed into the existing
single-query online trainer.

For the strict GGUF path, `scripts/train_online.py --defer-publish` retains its
cursor batch until conversion and evaluation finish. After publishing the final
GGUF, `--ack-published-release` verifies the active URI, version, and SHA-256
before clearing that batch; a mismatch retains it. The trainer is deliberately
not part of this always-on runtime and should run as a separately permissioned
GPU/ML job.

In hosted `langchain` mode the recovery bridge deliberately returns ordinary
OpenAI-compatible completions without prompt/completion token IDs. Decisions
and observed rewards remain durable, but that path cannot by itself provide the
token-level trajectory required for Agent Lightning weight training. The
checked-in `bitnet.cpp` runtime also lacks Agent Lightning's vLLM token-ID
extension. `passthrough` mode can preserve such fields from a different,
compatible upstream, but no optimizer is included here; verify the exact engine
and trainer rather than inferring training readiness from configuration.
The bridge's steady-state `/readyz` proves configuration, while the controller
startup smoke proves one real completion. A provider can fail later; monitor
recovery failures and schedule the checked-in smoke at an interval acceptable
for its latency/cost. Worker claim gating proves the controller, not perpetual
third-party provider availability.

## Troubleshooting

- **Studio readiness is green but no pipeline advances:** confirm
  `studio-worker` is running with `STUDIO_WORKER_MODE=external`, shares the same
  Studio database, and can reach Airflow and Lightning.
- **Airflow scheduler/DAG processor refuses to start:** run and inspect the
  one-shot `airflow-init` job on the same DAG claim. A missing or forged
  `.studio-airflow-shared-v1` sentinel fails closed.
- **DAG never appears in Airflow:** compare the actual volume identity and mount
  path, not just the path string. Verify Studio worker write access, Airflow
  read access, UID/GID compatibility, DAG processor health, and import errors.
- **Lightning reports `model_upstream_unavailable`:** check the private
  `recovery-model` service, its `/readyz` and `/v1/models`, the configured alias
  `studio-recovery`, and the provider/upstream credential. Keep the endpoint
  private; do not put credentials in its URL.
- **A second Lightning replica crashes:** expected safety behavior. Its
  exclusive state lock prevents two process-local stores from overwriting the
  same snapshot.
- **BitNet is healthy but an Airflow plan is invalid:** byte attestation proves
  identity, not instruction-following. Studio falls back to the configured
  frontier planner; keep the SQL/tool adapter and recovery model contracts
  separate and run the live prompt acceptance test before promotion.
- **Rewards exist but model behavior does not improve:** expected unless a
  separate trainer has produced, validated, published, and activated new
  weights. Check Studio's `agent_traces`, Lightning state, trainer eligibility,
  adapter registry/provenance, and serving health as distinct stages.

## Verification boundary

The repository's unit/integration tests validate the launchers, configuration
guards, persistence wrapper, bridge, Airflow client contracts, DAG compiler,
and recovery supervisor with stubs. They do not establish that these images
build on your target architecture, that a CSI volume has the required sharing
semantics, that a cloud network policy is correct, that real provider imports
work, or that a deployed BitNet adapter follows the repair contract. Image
builds, target-cloud deployment, and the post-deploy smoke/evaluation sequence
remain operator acceptance gates.
