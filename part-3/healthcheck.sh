#!/usr/bin/env bash
#
# Health check for the deployed stack. Run it on the server (from anywhere):
#
#   ./healthcheck.sh
#
# Works outward in layers: containers up -> datastores reachable -> data flowing.
# Prints a green/red line per check and exits non-zero if anything failed, so it
# also works as a cron probe:
#
#   */15 * * * * /path/to/part-3/healthcheck.sh >> /var/log/pythia-health.log 2>&1 || echo "UNHEALTHY"
#
# Override the compose files if your layout differs:
#   COMPOSE_FILES="-f docker-compose.yml" ./healthcheck.sh

set -uo pipefail

# Run from the script's own directory, where the compose files live.
cd "$(dirname "$0")" || exit 2

COMPOSE_FILES=${COMPOSE_FILES:-"-f docker-compose.yml -f docker-compose.prod.yml"}
SERVICES="redis postgres feeder worker syncer"

dc() { docker compose $COMPOSE_FILES "$@"; }

# Colors, disabled when output isn't a terminal (e.g. cron).
if [ -t 1 ]; then
  G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else
  G=; R=; Y=; B=; N=
fi

fails=0
ok()   { echo "  ${G}OK${N}  $*"; }
bad()  { echo "  ${R}FAIL${N} $*"; fails=$((fails + 1)); }
warn() { echo "  ${Y}WARN${N} $*"; }
hdr()  { echo; echo "${B}== $* ==${N}"; }

command -v docker >/dev/null 2>&1 || { echo "docker not found"; exit 2; }

# --- 1. Containers ---------------------------------------------------------
hdr "Containers"
for svc in $SERVICES; do
  ids=$(dc ps -q "$svc" 2>/dev/null)
  if [ -z "$ids" ]; then
    bad "$svc: no container (not started?)"
    continue
  fi
  running=0 total=0 note=""
  for id in $ids; do
    total=$((total + 1))
    state=$(docker inspect -f '{{.State.Status}}' "$id" 2>/dev/null)
    health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id" 2>/dev/null)
    restarts=$(docker inspect -f '{{.RestartCount}}' "$id" 2>/dev/null)
    if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "none" ]; }; then
      running=$((running + 1))
    fi
    [ "${restarts:-0}" -gt 3 ] 2>/dev/null && note="$note (restarts=$restarts!)"
    [ "$health" = "unhealthy" ] && note="$note (unhealthy!)"
  done
  if [ "$running" -eq "$total" ] && [ -z "$note" ]; then
    ok "$svc: $running/$total running"
  elif [ "$running" -eq "$total" ]; then
    warn "$svc: $running/$total running$note"
  else
    bad "$svc: only $running/$total running$note"
  fi
done

# --- 2. Redis --------------------------------------------------------------
hdr "Redis"
if dc exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
  ok "ping"
else
  bad "ping failed"
fi
news1=$(dc exec -T redis redis-cli LLEN news_queue 2>/dev/null | tr -d '\r')
beliefs=$(dc exec -T redis redis-cli LLEN belief_updates 2>/dev/null | tr -d '\r')
echo "    news_queue=${news1:-?}  belief_updates=${beliefs:-?}"
# Sample the news queue again to see if the workers are keeping up.
sleep 3
news2=$(dc exec -T redis redis-cli LLEN news_queue 2>/dev/null | tr -d '\r')
if [ -n "${news1:-}" ] && [ -n "${news2:-}" ]; then
  if [ "$news2" -gt "$news1" ] 2>/dev/null && [ "$news2" -gt 50 ] 2>/dev/null; then
    warn "news_queue climbing ($news1 -> $news2) and deep — consider: dc up -d --scale worker=N"
  else
    ok "news_queue not backing up ($news1 -> $news2)"
  fi
fi

# --- 3. Postgres -----------------------------------------------------------
hdr "Postgres"
if dc exec -T postgres pg_isready -U pm -d pm >/dev/null 2>&1; then
  ok "accepting connections"
else
  bad "pg_isready failed"
fi
q() { dc exec -T postgres psql -U pm -d pm -tAc "$1" 2>/dev/null | tr -d '\r'; }
[ "$(q "SELECT 1 FROM pg_extension WHERE extname='vector'")" = "1" ] \
  && ok "pgvector extension present" || bad "pgvector extension missing"
open=$(q "SELECT count(*) FROM markets WHERE NOT closed")
resolved=$(q "SELECT count(*) FROM markets WHERE closed")
bu=$(q "SELECT count(*) FROM belief_updates")
last=$(q "SELECT COALESCE(to_char(max(ts) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI')||' UTC','never') FROM belief_updates")
echo "    markets: open=${open:-?} resolved=${resolved:-?}  belief_updates=${bu:-?} (last: ${last:-?})"
if [ -n "${open:-}" ] && [ "$open" -gt 0 ] 2>/dev/null; then
  ok "markets populated (syncer working)"
else
  bad "no open markets — has the syncer run? (dc exec syncer python -m services.syncer.main --once)"
fi

# --- 4. Worker audit log ---------------------------------------------------
hdr "Worker audit trail"
alines=$(dc exec -T worker sh -c 'wc -l < /data/belief_updates.jsonl 2>/dev/null' 2>/dev/null | tr -d '\r')
if [ -n "${alines:-}" ]; then
  ok "belief_updates.jsonl has ${alines} entries"
else
  warn "audit log empty or missing (no belief updates yet — normal on a fresh deploy)"
fi

# --- 5. Host resources -----------------------------------------------------
hdr "Host"
disk=$(df -hP / | awk 'NR==2{print $5" used ("$4" free)"}')
echo "    disk /: $disk"

# --- Summary ---------------------------------------------------------------
hdr "Summary"
if [ "$fails" -eq 0 ]; then
  echo "${G}All checks passed.${N}"
  exit 0
else
  echo "${R}${fails} check(s) failed.${N}"
  exit 1
fi
