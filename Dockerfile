# ── Stage 1: build the frontend ─────────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ── Stage 2: backend runtime, serving API + built frontend ──────────────
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# DuckDB's object-store readers (S3/GCS, Azure) are extensions it otherwise
# downloads on first use; baked in here, connecting a bucket never waits on
# extensions.duckdb.org. Not fatal: without them, the first use downloads.
RUN python -c "import duckdb; c = duckdb.connect(); [c.execute(f'INSTALL {e}') for e in ('httpfs', 'azure')]" \
    || echo "duckdb extensions not pre-installed; they download on first use"
COPY backend/ ./
COPY --from=frontend /fe/dist ./static
ENV STUDIO_STATIC_DIR=/app/static
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
