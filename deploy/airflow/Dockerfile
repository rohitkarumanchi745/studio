# syntax=docker/dockerfile:1.7

# One immutable image is used for every Airflow component. Deploy it once per
# AIRFLOW_ROLE: init (one-shot), api-server, scheduler, and dag-processor.
ARG AIRFLOW_VERSION=3.3.1
FROM apache/airflow:slim-${AIRFLOW_VERSION}-python3.12

ARG AIRFLOW_VERSION

USER root
RUN install -d -o airflow -g root -m 0770 /opt/airflow/dags /opt/studio-airflow
USER airflow

COPY --chown=airflow:root deploy/airflow/requirements.txt /opt/studio-airflow/requirements.txt
RUN pip install --no-cache-dir \
      "apache-airflow==${AIRFLOW_VERSION}" \
      -r /opt/studio-airflow/requirements.txt \
      --constraint "${HOME}/constraints.txt" \
    && pip check

COPY --chown=airflow:root --chmod=0555 \
  deploy/airflow/runtime.py \
  deploy/airflow/healthcheck.py \
  deploy/airflow/smoke.py \
  /opt/studio-airflow/

# Airflow 3 requires an API server, scheduler, and standalone DAG processor.
# LocalExecutor keeps the minimal portable topology free of a broker/worker;
# task subprocesses run beside the scheduler and therefore share its DAG mount.
# DagProcessor heartbeats can be 30-60 seconds apart; 120 seconds keeps the
# documented jobs-check probe from declaring a healthy processor stale.
ENV AIRFLOW__CORE__EXECUTOR=LocalExecutor \
    AIRFLOW__CORE__AUTH_MANAGER=airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager \
    AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    AIRFLOW__API__HOST=0.0.0.0 \
    AIRFLOW__SCHEDULER__ENABLE_HEALTH_CHECK=True \
    AIRFLOW__DAG_PROCESSOR__BUNDLE_REFRESH_CHECK_INTERVAL=5 \
    AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL=10 \
    AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL=10 \
    AIRFLOW__DAG_PROCESSOR__HEALTH_CHECK_THRESHOLD=120 \
    AIRFLOW__DAG_PROCESSOR__DAG_VERSION_INFLATION_CHECK_LEVEL=error \
    STUDIO_AIRFLOW_REQUIRE_SHARED_DAGS_SENTINEL=1

# The upstream image keeps its signal-forwarding /entrypoint. This command is
# its child and execs the selected Airflow service after a fail-closed preflight.
CMD ["python", "/opt/studio-airflow/runtime.py", "run"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
  CMD ["python", "/opt/studio-airflow/healthcheck.py"]
