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
   Polymarket ──▶ ┌────────┐   Postgres + pgvector
   (Gamma API)    │ syncer │──▶ (markets + embeddings + beliefs)
                  └────────┘
                 (singleton)

- **syncer** — fetches fresh Polymarket markets, embeds them, marks resolved
  ones closed. *Keeps the market set the worker searches against live. Singleton.*
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

The **feeder**, **Redis**, **Postgres + pgvector**, the **worker** (market
analyzer), and the **syncer** (market ingestion). The `signal` service is still
stubbed in the compose file.

`db/seed_markets.py` remains as a quick fixture loader for smoke-testing the
worker without running the syncer.

### Schema changes

The schema lives in `db/migrations/` as numbered, forward-only SQL files. The
one-shot `migrate` service applies any unapplied ones against the live database
on every `docker compose up` (worker and syncer wait for it to finish), so
schema changes never require wiping the `pg_data` volume. To change the schema,
add a new file with the next number — e.g. `db/migrations/0004_add_positions.sql`
— and bring the stack up; never edit a migration that has already been applied.
Run it by hand against a running DB with:

```sh
docker compose run --rm migrate
```

### Tests

```sh
pip install -r requirements.txt -r requirements-dev.txt
pytest                                                    # unit tests
TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest   # + DB integration tests
```

Pure-logic tests (e.g. the Gamma price parsers) always run. Tests that need
Postgres are skipped unless `TEST_DATABASE_URL` points at a reachable DB, so a
bare `pytest` stays green with no infrastructure.

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

## Run the syncer

```sh
cd part-3
docker compose up --build syncer    # starts postgres + syncer
docker compose logs -f syncer       # watch "synced: +N new, ~M refreshed, K resolved"
```

One cycle on the host (needs `DATABASE_URL` + `VOYAGE_API_KEY`):

```sh
pip install -r requirements.txt
python -m services.syncer.main --once   # fetch + embed + resolve once, then exit
python -m services.syncer.main          # sync forever (default once a day)
```

Inspect the ingested markets:

```sh
docker compose exec postgres psql -U pm -d pm -c \
  'SELECT id, slug, current_score, volume_24h, end_date, closed FROM markets ORDER BY updated_at DESC LIMIT 10;'
```

`current_score` is seeded with the Polymarket yes-price at ingest, then the
worker overwrites it as news arrives. Re-syncing an existing market refreshes its
volume/liquidity/end_date but never resets that belief.

Each cycle the syncer re-checks every open market against Gamma and, when one has
resolved (Gamma reports it closed or no longer returns it), sets `closed = TRUE`
(and `resolved_at`) rather than deleting it: the worker excludes closed markets
from retrieval, but the row and its `belief_updates` history are kept so a later
pass can score our predictions against the outcome. The check is cheap because it
queries only our own open markets (a few hundred), not Polymarket's full list.

## Layout

```
lib/                shared, importable package
  config.py         env-driven settings
  feeds.py          the RSS feed registry (edit to add sources)
  schemas.py        Article + BeliefUpdate — the queue payload contracts
  queue.py          Redis list queue wrappers (news + belief)
  dedup.py          atomic Redis URL de-duplication
  embeddings.py     Voyage embedder (query side + document side)
  polymarket.py     Gamma API client (fetch fresh markets + resolution status)
  db.py             Postgres + pgvector access (top-k, score swap, sync, resolve)
  claude.py         price-blind re-evaluation call
services/feeder/    the RSS poller (producer)
services/worker/    the market analyzer (consumer, scalable)
services/syncer/    the market ingestion service (singleton)
prompts/            worker system + re-eval prompt templates
db/                 migrations/ (versioned schema) + migrate.py (runner) + seed_markets.py (dev fixtures)
Dockerfile          multi-stage: base + per-service targets
docker-compose.yml  local stack
```
