#!/usr/bin/env bash
#
# Start the whole stack (redis + postgres + feeder + worker + syncer) on the
# server, using the production compose overlay.
#
#   ./start.sh            # start with 1 worker
#   ./start.sh 3          # start with 3 workers (the one scalable service)
#
# Idempotent: re-running rebuilds changed images and recreates only what changed.
# Override the compose files if your layout differs:
#   COMPOSE_FILES="-f docker-compose.yml" ./start.sh

set -euo pipefail

# Run from the script's own directory, where the compose + .env files live.
cd "$(dirname "$0")"

COMPOSE_FILES=${COMPOSE_FILES:-"-f docker-compose.yml -f docker-compose.prod.yml"}
WORKER_SCALE=${1:-${WORKER_SCALE:-1}}

if [ -t 1 ]; then G=$'\e[32m'; R=$'\e[31m'; B=$'\e[1m'; N=$'\e[0m'; else G=; R=; B=; N=; fi
say()  { echo "${B}==>${N} $*"; }
die()  { echo "${R}error:${N} $*" >&2; exit 1; }

dc() { docker compose $COMPOSE_FILES "$@"; }

# --- preflight -------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "docker not found"
docker compose version >/dev/null 2>&1 || die "docker compose plugin not found"
[ -f .env ] || die ".env not found in $(pwd) — copy .env.example and fill it in"

# Required secrets must be present and non-empty in .env.
for key in ANTHROPIC_API_KEY VOYAGE_API_KEY POSTGRES_PASSWORD; do
  val=$(grep -E "^${key}=" .env | tail -n1 | cut -d= -f2-)
  [ -n "$val" ] || die "$key is missing or empty in .env"
done

case "$WORKER_SCALE" in
  ''|*[!0-9]*) die "worker count must be a positive integer, got: $WORKER_SCALE" ;;
esac

# --- build & start ---------------------------------------------------------
say "Building images..."
dc build

say "Starting stack (worker x${WORKER_SCALE})..."
dc up -d --scale worker="$WORKER_SCALE" --remove-orphans

# --- report ----------------------------------------------------------------
say "Current state:"
dc ps

echo
echo "${G}Stack is up.${N}"
echo "  Logs:    docker compose $COMPOSE_FILES logs -f"
echo "  Health:  ./healthcheck.sh"
echo "  Scale:   ./start.sh <N>   (or: docker compose $COMPOSE_FILES up -d --scale worker=N)"
