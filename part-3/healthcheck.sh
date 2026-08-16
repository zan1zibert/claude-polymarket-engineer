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
SERVICES="redis postgres feeder worker syncer scorer signal"

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
# SCARD, not LLEN: the worker->signal hop is a SET of market ids (so repeat
# notifications for one market collapse), not the list it used to be. LLEN on the
# retired `belief_updates` key returned 0 whether the pipeline was healthy or
# dead, which is exactly the kind of silently-passing check this script exists to
# avoid. A non-zero depth here is normal — it just means the signal service hasn't
# drained the set yet.
dirty=$(dc exec -T redis redis-cli SCARD belief_dirty 2>/dev/null | tr -d '\r')
echo "    news_queue=${news1:-?}  belief_dirty=${dirty:-?}"
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

# --- 5. Scorer ---------------------------------------------------------
hdr "Scorer"
awaiting=$(q "SELECT count(*) FROM markets WHERE closed AND resolved_outcome IS NULL")
scored=$(q "SELECT count(*) FROM forecast_scores")
last_scored=$(q "SELECT COALESCE(to_char(max(scored_at) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI')||' UTC','never') FROM forecast_scores")
pending=$(q "SELECT count(*) FROM markets m LEFT JOIN forecast_scores s ON s.market_id = m.id
             WHERE m.closed AND m.resolved_outcome IS NOT NULL AND m.resolved_outcome != 0.5
               AND m.current_score IS NOT NULL AND s.market_id IS NULL")
echo "    forecast_scores=${scored:-?} (last: ${last_scored:-?})  awaiting_outcome=${awaiting:-?}  pending_scoring=${pending:-?}"
if [ -n "${pending:-}" ] && [ "$pending" -gt 0 ] 2>/dev/null; then
  warn "${pending} resolved market(s) waiting to be graded — scorer running? (dc logs scorer)"
else
  ok "no scoring backlog"
fi
if [ -n "${awaiting:-}" ] && [ "$awaiting" -gt 200 ] 2>/dev/null; then
  warn "awaiting_outcome is high (${awaiting}) — syncer backfill may be stuck (dc logs syncer)"
fi

# --- 6. Signal -------------------------------------------------------------
# Deliberately no FAIL in this section. A quiet signal service is the normal
# state, not a broken one: firing needs a belief inside a conviction band AND a
# 5-point edge against the live price AND a market resolving inside the horizon,
# so a fresh deploy can legitimately sit at zero for days. Same reasoning as the
# scorer above, which also only warns on a backlog. A cron probe that is
# permanently red gets ignored, and then it protects nothing.
hdr "Signal"
# Check the schema is actually there before reporting on it. `q` sends stderr to
# /dev/null, so a query against a missing table yields an empty string — and every
# "is it empty?" test below would then read as "nothing to report" and print a
# green OK for a service whose tables do not exist. That is the same
# silently-passing failure the retired LLEN check had. count(*) on an existing but
# empty table returns "0", so an EMPTY result reliably means the query itself
# failed, which makes this a sound discriminator rather than a guess.
sigs=$(q "SELECT count(*) FROM signals")
if [ -z "${sigs:-}" ]; then
  warn "signals/paper_positions tables not found — migrations applied? (dc run --rm migrate)"
else
  last_sig=$(q "SELECT COALESCE(to_char(max(ts) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI')||' UTC','never') FROM signals")
  pos_open=$(q "SELECT count(*) FROM paper_positions WHERE status = 'open'")
  pos_settled=$(q "SELECT count(*) FROM paper_positions WHERE status <> 'open'")
  echo "    signals=${sigs} (last: ${last_sig:-?})  positions: open=${pos_open:-?} settled=${pos_settled:-?}"

  # The liveness check: a position whose market has already resolved should have
  # been settled by the sweep. Anything sitting here means the sweep is not
  # running (the same shape as the scorer's pending_scoring backlog). The 0.5
  # exclusion matches lib/db.settle_positions and resolved_unscored_markets — an
  # undeterminable outcome is deliberately never settled, so counting it here
  # would manufacture a warning that can never be cleared.
  unsettled=$(q "SELECT count(*) FROM paper_positions p JOIN markets m ON m.id = p.market_id
                 WHERE p.status = 'open' AND m.resolved_outcome IS NOT NULL
                   AND m.resolved_outcome != 0.5")
  if [ "${unsettled:-0}" -gt 0 ] 2>/dev/null; then
    warn "${unsettled} resolved position(s) not settled — signal sweep running? (dc logs signal)"
  else
    ok "no settlement backlog"
  fi

  # Informational only, never a threshold. Whether we are MAKING money belongs to
  # the accuracy dashboard; this script only answers whether the machine runs. It
  # must never go red because the paper book is down. Guarded on settled>0 because
  # avg() over zero rows is NULL, and NULL poisons the whole concatenation.
  if [ "${pos_settled:-0}" -gt 0 ] 2>/dev/null; then
    book=$(q "SELECT 'pnl=' || to_char(COALESCE(sum(pnl),0), 'FM9990.00')
                   || '  win_rate=' || to_char(avg((pnl > 0)::int), 'FM0.00')
                   || '  staked=' || to_char(COALESCE(sum(stake),0), 'FM9990.00')
              FROM paper_positions WHERE status <> 'open'")
    echo "    paper book: ${book:-?}"
  fi
fi

# --- 7. Host resources -----------------------------------------------------
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
