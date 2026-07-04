# syntax=docker/dockerfile:1
#
# Multi-stage build: one shared `base` stage holds common deps + the `lib/`
# package; each service is a thin stage on top selected via `target:` in
# docker-compose. This gives separate per-service images that share the base
# layer — "monorepo, several images" without duplicating shared code.

FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lib/ ./lib/

# ---- feeder (singleton) ----
FROM base AS feeder
COPY services/feeder/ ./services/feeder/
CMD ["python", "-m", "services.feeder.main"]

# ---- worker (scalable: docker compose up --scale worker=N) ----
FROM base AS worker
COPY services/worker/ ./services/worker/
COPY prompts/ ./prompts/
CMD ["python", "-m", "services.worker.main"]

# ---- syncer (singleton: market ingestion) ----
FROM base AS syncer
COPY services/syncer/ ./services/syncer/
CMD ["python", "-m", "services.syncer.main"]

# ---- signal stage will be added here as we build it ----
