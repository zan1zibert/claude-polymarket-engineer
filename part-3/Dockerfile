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

# ---- worker / signal stages will be added here as we build them ----
