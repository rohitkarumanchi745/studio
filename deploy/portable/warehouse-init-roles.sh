#!/usr/bin/env bash
set -Eeuo pipefail

# Runs only during PostgreSQL's first-volume initialization. psql's format()
# performs identifier/literal quoting, so operator-selected role names and
# generated passwords are never interpolated as raw SQL.
: "${WAREHOUSE_READER_USER:?set WAREHOUSE_READER_USER}"
: "${WAREHOUSE_READER_PASSWORD:?set WAREHOUSE_READER_PASSWORD}"
: "${WAREHOUSE_PIPELINE_WRITER_USER:?set WAREHOUSE_PIPELINE_WRITER_USER}"
: "${WAREHOUSE_PIPELINE_WRITER_PASSWORD:?set WAREHOUSE_PIPELINE_WRITER_PASSWORD}"

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=reader_user="$WAREHOUSE_READER_USER" \
  --set=reader_password="$WAREHOUSE_READER_PASSWORD" \
  --set=writer_user="$WAREHOUSE_PIPELINE_WRITER_USER" \
  --set=writer_password="$WAREHOUSE_PIPELINE_WRITER_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'reader_user', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'reader_user') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'reader_user', :'reader_password') \gexec

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'writer_user', :'writer_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'writer_user') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'writer_user', :'writer_password') \gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'reader_user') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'reader_user') \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :'reader_user') \gexec
SELECT format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO %I', :'reader_user') \gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'writer_user') \gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'writer_user') \gexec
SELECT format('GRANT SELECT ON ALL TABLES IN SCHEMA public TO %I', :'writer_user') \gexec
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
SELECT format('CREATE SCHEMA IF NOT EXISTS pipeline_output AUTHORIZATION %I', :'writer_user') \gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA pipeline_output TO %I', :'writer_user') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET search_path TO public, pipeline_output',
              :'writer_user', current_database()) \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET statement_timeout TO %L',
              :'writer_user', current_database(), '900s') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET lock_timeout TO %L',
              :'writer_user', current_database(), '30s') \gexec
SQL
