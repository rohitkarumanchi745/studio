# Run the whole Studio stack locally (Docker & Kubernetes)

Studio needs three things running together: the **app** (API + built frontend, one
image), **Postgres**, and **Redis**. Everything else — LLM keys, BitNet serving,
Harrier embeddings, M365/Azure, the trainer — is optional and degrades gracefully
when unset. Prereq: **Docker Desktop running**.

Demo logins after boot: `admin@studio.local / admin123` (also analyst/viewer `…123`).

---

## Option A — Docker Compose (fastest)

```bash
docker compose -f deploy/local/docker-compose.yml up --build
# open http://localhost:8000
```
Verify:
```bash
curl -s localhost:8000/api/health        # {"status":"ok","store":"postgres","tile_cache":"redis",...}
```
Tear down (add `-v` to also drop the Postgres volume):
```bash
docker compose -f deploy/local/docker-compose.yml down
```

Turn on more features by adding to the `app.environment` block:
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (real agent), `STUDIO_LLM_BASE_URL` +
`HARRIER_EMBED_URL` (BitNet routing + KAG embeddings), `AZURE_*` (M365 extraction).

---

## Option B — Local Kubernetes

Use Docker Desktop's built-in Kubernetes (Settings → Kubernetes → Enable), or `kind`.

```bash
# 1. Build the image locally
docker build -t studio:local .

# 2. Make the image visible to the cluster
#    Docker Desktop k8s: nothing to do (uses local images; manifest sets imagePullPolicy: IfNotPresent)
#    kind:      kind load docker-image studio:local
#    minikube:  minikube image load studio:local

# 3. Deploy app + Postgres + Redis
kubectl apply -f deploy/local/k8s/studio.yaml

# 4. Watch it come up
kubectl -n studio get pods -w        # wait for studio pod = Running/Ready

# 5. Reach it
kubectl -n studio port-forward svc/studio 8000:8000
# open http://localhost:8000
```
Debug / tear down:
```bash
kubectl -n studio logs deploy/studio
kubectl delete namespace studio
```
Edit env (secret `studio-env`) for keys/creds, then `kubectl -n studio rollout restart deploy/studio`.

---

## Run the test suite in a container
```bash
docker build -t studio:local .
docker run --rm studio:local sh -c "pip install -q pytest && python -m pytest -q -p no:cacheprovider"
# (recovery-planner tests also need: pip install agentlightning)
```

## Optional components (not needed for a functional stack)
- **CPU trainer** — `scripts/Dockerfile.trainer` (reward-filtered SFT + DPO); needs `STUDIO_LLM_BASE_URL`.
- **BitNet serving** — `serving/` (`docker-compose.yml`, `Dockerfile.gateway`); set `STUDIO_LLM_BASE_URL` on the app to point at it.
- **Tool sandbox** — `scripts/Dockerfile.toolrunner`, used by the approved-MCP-tool path.
- **k8s_spark** — the Airflow-DAG / `platform_run` feature submits `SparkApplication`s to a Spark-on-K8s operator; configure `STUDIO_AIRFLOW_*` / platform creds to exercise it.
