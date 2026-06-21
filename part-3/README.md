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

- **worker** is **price-blind**: it never sees the live Polymarket price. The
  "current score" it updates is *our own* prior belief, stored in Postgres. Only
  the (future) `signal` service compares against price.

## What's built so far

The **feeder**, **Redis**, **Postgres + pgvector**, and the **worker** (market
analyzer). The `signal` service is still stubbed in the compose file.

Market *ingestion* (loading markets + embeddings into Postgres) is a separate
component and not built yet — `db/seed_markets.py` inserts a couple of fixtures
so the worker is exercisable end-to-end in the meantime.

## Run the worker

```sh
cd part-3
cp .env.example .env                 # fill in ANTHROPIC_API_KEY + VOYAGE_API_KEY
docker compose up --build            # redis + postgres + feeder + worker
docker compose up --scale worker=3   # the worker is the scalable stage
```

Seed a few markets (needs the env keys; run on the host with deps installed):

```sh
pip install -r requirements.txt
python -m db.seed_markets
```

Inspect the outputs of a re-evaluation — all three should agree:

```sh
docker compose exec postgres psql -U pm -d pm -c 'SELECT market_id, previous_score, new_score FROM belief_updates ORDER BY ts DESC LIMIT 5;'
docker compose exec redis redis-cli LRANGE belief_updates 0 0   # newest event for signal
docker compose exec worker cat /data/belief_updates.jsonl       # append-only audit log
```

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
  schemas.py        Article + BeliefUpdate — the queue payload contracts
  queue.py          Redis list queue wrappers (news + belief)
  dedup.py          atomic Redis URL de-duplication
  embeddings.py     Voyage embedder
  db.py             Postgres + pgvector access (top-k + atomic score swap)
  claude.py         price-blind re-evaluation call
services/feeder/    the RSS poller (producer)
services/worker/    the market analyzer (consumer, scalable)
prompts/            worker system + re-eval prompt templates
db/                 init.sql (schema) + seed_markets.py (dev fixtures)
Dockerfile          multi-stage: base + per-service targets
docker-compose.yml  local stack
```
