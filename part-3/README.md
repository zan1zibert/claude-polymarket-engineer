# Part 3 — News-driven prediction engine

A small, always-on service that reacts to breaking news, finds the Polymarket
markets it affects, asks Claude how the probability should move, and (later)
turns large disagreements with the market into trade signals.

## Architecture

```
            ┌────────┐   Redis    ┌────────┐   beliefs   ┌────────┐
  RSS  ───▶ │ feeder │ ── queue ─▶│ worker │ ── + log ──▶│ signal │──▶ signals
  feeds     └────────┘            └────────┘             └────────┘
           (singleton)        (scalable, ×N)            (singleton)
                                   │
                                   ▼
                          Postgres + pgvector
                       (markets + embeddings + beliefs)
```

- **feeder** — polls RSS, dedups, pushes fresh articles to Redis. *Dumb on purpose.*
- **worker** — embeds news, retrieves candidate markets (pgvector), asks Claude
  for a price-blind probability, updates beliefs, logs the transition. *Scale this one.*
- **signal** — reads belief updates, fetches live Polymarket price, applies edge/
  conviction filters, records intended trades. *Singleton — mutates positions.*

Monorepo, multiple images: one shared `base` stage (`lib/`) + a thin stage per
service, selected via `target:` in `docker-compose.yml`.

## What's built so far

Only the **feeder** + **Redis**. The rest are stubbed in the compose file.

## Run the feeder

```sh
cd part-3
docker compose up --build           # starts redis + feeder
docker compose logs -f feeder       # watch "pushed N new articles"
```

Inspect the queue:

```sh
docker compose exec redis redis-cli LLEN news_queue
docker compose exec redis redis-cli LRANGE news_queue 0 0   # peek newest article
```

Stop (and optionally wipe Redis data):

```sh
docker compose down        # keep data
docker compose down -v     # wipe redis volume
```

### Run without Docker (dev)

```sh
cd part-3
pip install -r requirements.txt
# needs a local redis on :6379
python -m services.feeder.main --once   # one poll cycle, then exit
python -m services.feeder.main          # poll forever
```

## Layout

```
lib/                shared, importable package
  config.py         env-driven settings
  feeds.py          the RSS feed registry (edit to add sources)
  schemas.py        Article — the queue payload contract
  queue.py          Redis list queue wrapper
  dedup.py          atomic Redis URL de-duplication
services/feeder/    the poller (this is the only live service for now)
Dockerfile          multi-stage: base + per-service targets
docker-compose.yml  local stack
```
