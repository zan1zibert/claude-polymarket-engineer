# Signal service — turning beliefs into paper trades

**Date:** 2026-08-15
**Status:** approved, ready for implementation planning

## Problem

The pipeline is open-loop on the trading side. The worker maintains our belief
(`markets.current_score`) as news arrives, and the scorer grades that belief
against the outcome once a market resolves — so we can already answer "are we
better forecasters than the market?". We cannot answer "would acting on those
beliefs have made money?", because nothing ever compares a belief to the live
price and commits to a position.

The `signal` service has been named in the README architecture and stubbed in
`docker-compose.yml` since the start. This spec builds it.

## Scope

In scope: a singleton service that evaluates markets against the live Polymarket
price, records a decision row for every signal it fires, and opens a simulated
€1 position so paper P&L can be scored against resolution. Positions settle when
the market resolves.

Out of scope, deliberately:

- **Real order placement.** Paper only.
- **Position sizing.** Flat stake, one knob (`signal_stake`, default 1.0). The
  sizing algorithm comes later; this spec records the inputs it will need.
- **Early exit on a profit target.** The schema and the sweep loop are shaped to
  accept it without a migration (see *Future work*), but no rule writes it.
- **Grafana panels.** Metrics are exposed and scraped; dashboards are a
  follow-up, once real signal volume and P&L are observable.
- **The `market_overconfidence` entry rule.** See *Future work*.

## Theory: what the numbers mean

With a flat stake `S`, buying the side that costs `c` per share, where `q` is our
belief that side wins:

```
edge = q - c
```

Three quantities follow, and they are the whole basis of the filters:

| Quantity | Formula | Meaning |
|---|---|---|
| Expected ROI | `edge / c` | reward per € staked |
| Kelly fraction | `edge / (1 - c)` | fraction of bankroll this bet deserves |
| Per-bet Sharpe | `edge / sqrt(q(1-q))` | reward per unit of variance |

Downside is always exactly the stake — bounded, no ruin risk — so "risk" here is
hit rate and variance, not loss magnitude.

The key structural fact: **ROI is `edge/c` and Kelly is `edge/(1-c)`, so they
point in opposite directions in `c`.** That is the risk/reward tradeoff, and it
is controlled by a single quantity, the cost basis of the side bought:

| Market | Belief | Side | `c` | `q` | ROI | Kelly | Hit rate |
|---|---|---|---|---|---|---|---|
| 0.95 | 0.85 | NO | 0.05 | 0.15 | +200% | 10.5% | 15% |
| 0.85 | 0.80 | NO | 0.15 | 0.20 | +33% | 5.9% | 20% |
| 0.75 | 0.80 | YES | 0.75 | 0.80 | +6.7% | 20% | 80% |

Cheap underdog side (low `c`): enormous ROI, rare wins, high variance. Expensive
favourite side (high `c`): modest ROI, frequent wins, low variance.

`c < 0.5` is exactly the case where we are buying the underdog, which makes
`min_cost_basis` a longshot filter and the system's primary risk dial. Note it is
a blunt instrument: raising it removes both the "market is overconfident" trades
and some large-edge underdog trades. It is one number, and P&L bucketed by
`cost_basis` will show where to set it.

Both Kelly and Sharpe actually *favour* large-edge tail bets — the 0.95/0.85 row
has roughly twice the Sharpe of the 0.75/0.80 row, and in log-odds it is a ~4×
bigger disagreement. The only sound reason to avoid the tails is suspected
miscalibration of our own model there, which is an empirical question answerable
from `forecast_scores` and the reliability bins in `lib/scoring.py`. Hence a
tunable band rather than a hardcoded rule.

## The decision function

Pure logic in a new `lib/signals.py`, dependency-free in the same spirit as
`lib/scoring.py`, so the live service and any future backtest cannot drift apart.
One entry point, `evaluate(...) -> Decision`, returning either a pass carrying
all derived metrics or a rejection carrying a machine-readable reason.

Side selection happens first; every subsequent quantity is expressed in terms of
the side we would buy:

```
side = YES if belief > price else NO
c    = price   if YES else 1 - price      # cost per share
q    = belief  if YES else 1 - belief     # our P(this side wins)
edge = q - c                              # positive by construction
```

Gates, evaluated in order. The first failure is the recorded reason:

| Gate | Rule | Default |
|---|---|---|
| `market_closed` | market still open | — |
| `no_belief` | `markets.current_score` is not NULL | — |
| `no_price` | Gamma returned a YES price | — |
| `no_end_date` | market has an `end_date` | — |
| `horizon` | `0 < end_date - now <= max_horizon_days` | 14 days |
| `conviction` | `belief >= 0.80` or `belief <= 0.20` | 0.80 / 0.20 |
| `min_edge` | `edge >= min_edge` | 0.05 |
| `cost_basis_band` | `min_cost_basis <= c <= max_cost_basis` | 0.05 / 0.95 |

`evaluate()` knows nothing about open positions — those gates are all it has. A
signal firing on a market we already hold is not a rejection: the `signals` row is
still written, and only the position is skipped (see *Settlement* below). Keeping
position state out of the pure function is what lets a backtest replay decisions
without simulating a book.

`expected_roi`, `kelly` and `sharpe` are computed and stored on every fired
signal but gate nothing. They cost no external calls and are what the sizing
algorithm will consume.

### The conviction gate does not choose a direction

Worth stating explicitly because it surprises people reading the log: the
conviction band only decides whether our belief is confident enough to act on.
Direction comes from `sign(belief - price)`. So a high-conviction belief of 0.80
against a market at 0.85 makes us buy **NO** — betting against our own
directional view on relative-value grounds. This is correct by expected value and
is intended. The `side` column records it unambiguously.

A consequence: in the high conviction band, buying NO requires
`price > belief >= 0.80`, so `c < 0.20` always. Every "we are less extreme than
the market" trade is therefore a low-cost-basis trade, and raising
`min_cost_basis` above 0.20 removes the whole class.

### Horizon

`resolution_window_days` is overridden to ~2 months in production, so the
14-day horizon gate is load-bearing rather than a no-op. It also means a market
can become horizon-eligible purely through the passage of time, with no belief
update — which the sweep exists to catch.

## Data model

One migration, `db/migrations/0005_signals_positions.sql`.

### `signals` — one row per fired signal

```
id             BIGSERIAL PK
ts             TIMESTAMPTZ NOT NULL DEFAULT now()
market_id      TEXT NOT NULL REFERENCES markets(id)
market_title   TEXT NOT NULL
rule           TEXT NOT NULL     -- 'conviction_edge'
source         TEXT NOT NULL     -- 'belief_update' | 'sweep'
article_url    TEXT              -- newest belief_updates article; NULL if never moved
belief         DOUBLE PRECISION NOT NULL
yes_price      DOUBLE PRECISION NOT NULL   -- live Gamma price at decision time
side           TEXT NOT NULL     -- 'YES' | 'NO'
cost_basis     DOUBLE PRECISION NOT NULL
edge           DOUBLE PRECISION NOT NULL
win_prob       DOUBLE PRECISION NOT NULL
expected_roi   DOUBLE PRECISION NOT NULL
kelly          DOUBLE PRECISION NOT NULL
sharpe         DOUBLE PRECISION NOT NULL
end_date       TIMESTAMPTZ
horizon_days   DOUBLE PRECISION
```

Index on `(market_id, ts DESC)`, matching `belief_updates` and
`relevance_checks`.

`source` rather than `trigger`, which is a Postgres keyword.

`rule` exists from day one so a second entry rule can be added beside the first
rather than tangled into it.

### `paper_positions` — the simulated fill and its lifecycle

```
id            BIGSERIAL PK
signal_id     BIGINT NOT NULL REFERENCES signals(id)
market_id     TEXT NOT NULL REFERENCES markets(id)
opened_at     TIMESTAMPTZ NOT NULL DEFAULT now()
side          TEXT NOT NULL
entry_price   DOUBLE PRECISION NOT NULL   -- cost basis per share
stake         DOUBLE PRECISION NOT NULL   -- euros; 1.0 for now
shares        DOUBLE PRECISION NOT NULL   -- stake / entry_price
status        TEXT NOT NULL DEFAULT 'open'  -- 'open' | 'settled' | 'closed_early'
closed_at     TIMESTAMPTZ
exit_price    DOUBLE PRECISION
exit_reason   TEXT              -- 'resolved'; future: 'profit_target'
pnl           DOUBLE PRECISION  -- euros
```

**One open position per market is enforced by the database, not by application
code:**

```sql
CREATE UNIQUE INDEX paper_positions_one_open_idx
    ON paper_positions (market_id) WHERE status = 'open';
```

A partial unique index makes double entry impossible even if the notification path and
the sweep path race, or the service restarts mid-cycle. Same instinct as
`forecast_scores` keying on `market_id` for idempotency.

When a signal fires on a market that already has an open position, the `signals`
row is still written (so the fact it fired is visible) and no position is opened.

### Settlement

For each open position whose market has a non-NULL `markets.resolved_outcome`:

```
won        = (side == 'YES' and outcome == 1.0) or (side == 'NO' and outcome == 0.0)
exit_price = 1.0 if won else 0.0
pnl        = shares * exit_price - stake
status     = 'settled', exit_reason = 'resolved', closed_at = now()
```

Positions on markets that are closed but whose `resolved_outcome` is still NULL
stay open, mirroring how `forecast_scores` declines to grade an unknown outcome.

## The service

`services/signal/main.py`, singleton, a near-clone of the scorer: periodic loop,
`--once` flag, metrics server, graceful shutdown on SIGINT/SIGTERM,
`_wait_for_db` retry at startup.

```
while not stop:
    if sweep is due:                 # signal_sweep_interval_seconds, default 3600
        settle_resolved_positions()
        rescan_candidates()
    market_id = dirty_markets.pop()  # SPOP, non-blocking
    if market_id:
        evaluate_one(market_id, source='belief_update')
    else:
        stop.wait(timeout=5)         # nothing pending; nap, but wake on shutdown
```

### The notification channel: a dirty-market set, not a list

The worker's Redis hop changes shape. Because the signal service reads the belief
from Postgres, the only thing Redis needs to carry is *which markets are dirty*:

```
worker:  SADD belief_dirty <market_id>
signal:  SPOP belief_dirty
```

`SADD` is O(1) and atomic, and a repeat push for a market that is already pending
is a no-op — so redundant notifications collapse by construction, with no scan
and no Lua script. (A Redis list cannot do this: `LREM` matches only on exact
element value, so collapsing would mean `LRANGE`-ing the whole list, parsing
every payload, and `LREM`-ing matches — O(n) per push and racy.)

What collapsing buys is efficiency, not correctness: the partial unique index
already prevents duplicate positions. Since belief comes from the DB, evaluating
the same market three times yields the same verdict three times, so two of the
three were wasted Gamma calls and duplicate `signals` rows.

Losing FIFO and blocking `BRPOP` costs nothing here — the markets are
independent, and the loop already wakes every few seconds for the sweep check,
which is irrelevant latency against price movements measured in hours.

**`BeliefUpdate` is unchanged.** It is one object serving three sinks —
`db.apply_belief_update` returns it, the worker writes it to the JSONL audit log,
and it mirrors the `belief_updates` row — so the audit trail keeps `reasoning`
and both scores. Only the Redis payload shrinks, to a bare market id.

**The Redis key is renamed** from `belief_updates` to `belief_dirty`
(`BELIEF_DIRTY_KEY`). A set and a list cannot share a key — Redis raises
`WRONGTYPE` — and the old list has accumulated unread entries since the worker
started. Renaming makes the leftover list inert rather than fatal, so no manual
flush is required before first run; it can be deleted whenever.

### Two entry paths, one evaluation

Both paths call the same `evaluate_one`, and **both read everything from
Postgres**: the belief from `markets.current_score`, and the triggering
`article_url` (plus `reasoning`) from the newest `belief_updates` row for that
market, which is already indexed on `(market_id, ts DESC)`. Reading the belief
from the DB rather than a payload matters because a payload's `new_score` is only
a snapshot of the moment it was pushed — with collapsing, several updates may
have landed since. The paths therefore differ only in `source`.

- **Notification path** — `SPOP` a dirty market id, then one Gamma call for that
  market's live price. Belief updates are sporadic, so a per-event call is cheap
  and exact at decision time.
- **Sweep path** — candidates are open markets inside the horizon whose
  `current_score` is in a conviction band and which have no open position. Prices
  come from one chunked `polymarket.fetch_statuses` call for all of them. This
  path exists because markets become horizon-eligible through time alone, and
  because edge can appear from price drift with no news at all: our belief sits
  still while the price moves.

Sweep-triggered signals still carry an `article_url` when the market has a
`belief_updates` history; it is NULL only for markets whose belief has never been
moved by the worker (belief still equal to `seed_price`).

The sweep reads `closed` and `resolved_outcome` from Postgres, never from Gamma.
Deciding what "resolved" means is the syncer's job, and two services must not
disagree about it. The sweep calls Gamma only for candidate prices.

### Files

| File | Change |
|---|---|
| `lib/signals.py` | new — `evaluate()`, `Decision`, the side and metric math |
| `lib/db.py` | add `insert_signal`, `open_position_market_ids`, `settle_positions`, `signal_candidate_markets`, `market_for_signal` (belief + latest triggering article in one query), `position_aggregates` |
| `lib/queue.py` | replace `BeliefQueue` with `DirtyMarkets` — `add`/`pop`/`depth` over a Redis set (`SADD`/`SPOP`/`SCARD`) |
| `services/worker/main.py` | one line: push a market id to the set instead of a `BeliefUpdate` blob; DB row and JSONL audit unchanged |
| `lib/config.py` | the `signal_*` knobs |
| `lib/metrics.py` | `SIGNAL_*` collectors |
| `db/migrations/0005_signals_positions.sql` | new |
| `services/signal/` | new — `main.py`, `__init__.py` |
| `Dockerfile` | new `signal` target |
| `docker-compose.yml`, `docker-compose.prod.yml` | replace the stub with the real service, `replicas: 1` |
| `monitoring/prometheus/` | scrape target for the signal service |
| `README.md` | mark signal built, add a run section, update the layout tree, and replace the `LRANGE belief_updates 0 0` debug command with `SMEMBERS belief_dirty` |

### Config

All in `lib/config.py`, following the existing `sync_*` / `scorer_*` convention.

| Setting | Env var | Default |
|---|---|---|
| `signal_min_edge` | `SIGNAL_MIN_EDGE` | 0.05 |
| `signal_min_conviction_high` | `SIGNAL_MIN_CONVICTION_HIGH` | 0.80 |
| `signal_max_conviction_low` | `SIGNAL_MAX_CONVICTION_LOW` | 0.20 |
| `signal_max_horizon_days` | `SIGNAL_MAX_HORIZON_DAYS` | 14 |
| `signal_min_cost_basis` | `SIGNAL_MIN_COST_BASIS` | 0.05 |
| `signal_max_cost_basis` | `SIGNAL_MAX_COST_BASIS` | 0.95 |
| `signal_stake` | `SIGNAL_STAKE` | 1.0 |
| `signal_sweep_interval_seconds` | `SIGNAL_SWEEP_INTERVAL_SECONDS` | 3600 |

Plus one rename: `belief_queue_key` / `BELIEF_QUEUE_KEY` (default `belief_updates`)
becomes `belief_dirty_key` / `BELIEF_DIRTY_KEY` (default `belief_dirty`), since the
key now holds a set.

### Metrics

Rejections are counted, not stored as rows: the hourly sweep would otherwise
accumulate thousands of near-identical rejection rows a day, and the data needed
to tune `min_cost_basis` is P&L bucketed by `cost_basis`, which comes from fired
signals plus settled positions.

```
signal_evaluated_total{source}
signal_rejected_total{reason}
signal_fired_total{side,rule}
signal_positions_open
signal_positions_settled_total
signal_pnl_total
signal_win_rate
signal_roi
signal_last_sweep_timestamp
```

## Error handling

- **Gamma unreachable or returns no price for a market** — that candidate is
  rejected with reason `no_price` and counted. Not an error; the next sweep
  retries. A failure of the whole chunked fetch is logged and the sweep is
  abandoned for this cycle, matching how the scorer wraps a cycle in
  `try/except` and continues.
- **A market_id from the set that is not in `markets`** — rejected with reason
  `unknown_market`. Should not happen (the worker only updates markets that
  exist) but Redis outlives the DB.
- **Race on the partial unique index** — a duplicate insert is caught and treated
  as `position_open`, not as a crash.
- **Postgres unavailable at startup** — `_wait_for_db` retry loop, as the scorer
  and syncer already do.
- **The stale `belief_updates` list** — neutralised by the key rename rather than
  handled. The old list has never had a consumer; nothing reads `belief_dirty`
  until the worker starts writing it, so there is no backlog to drain and no
  `WRONGTYPE` collision. The old key can be deleted at leisure.
- **A dirty market id lost to a crash** — `SPOP` removes before evaluating, so a
  crash mid-evaluation drops that notification. Acceptable: the sweep re-examines
  every conviction-band market within the hour, so the only cost is latency, and
  no position or signal row can be half-written (each is one transaction).

## Testing

Following the existing split: pure tests always run; DB tests skip unless
`TEST_DATABASE_URL` points at a reachable Postgres.

`tests/test_signals.py` — pure:

- Side selection in both directions, including `belief < price` picking NO.
- Each gate rejecting with the correct reason, and gate *ordering* (a candidate
  failing two gates reports the first).
- Boundary values: edge exactly `min_edge`, `c` exactly on each band edge,
  belief exactly 0.80 and exactly 0.20, `end_date` exactly at the horizon.
- The three theory-table rows as literal assertions — `0.95/0.85 -> NO, c=0.05,
  roi=+2.00, kelly=0.105`, and the other two — so a later "simplification" of
  the math fails loudly.

`tests/test_signals_db.py` — needs Postgres:

- The partial unique index rejects a second open position on the same market.
- Settlement P&L across all four cases: YES won, YES lost, NO won, NO lost.
- Settlement skips positions whose `resolved_outcome` is NULL.
- Re-running the sweep is idempotent — no duplicate positions, no double P&L.

`tests/test_dirty_markets.py` — the notification channel against an in-memory
fake redis, mirroring `tests/test_market_snapshot.py`. The load-bearing one:
adding the same market id three times leaves one pending entry.

`tests/test_signal.py` — the service's wiring with fakes for the DB and Gamma,
mirroring `tests/test_worker.py`: a fired signal writes both a `signals` row and a
position; an already-held market writes the row but no position; a rejection
writes neither; a NO position is entered at `1 - yes_price`, not at `yes_price`;
the sweep settles before it rescans.

## Future work

Recorded here because the design accommodates it deliberately, not because it is
being built.

**Early exit on a profit target.** The sweep already runs periodically and
already fetches live prices for open markets, so the rule is a predicate inside
an existing loop. It writes the same columns settlement does, with a live exit
price and `exit_reason = 'profit_target'`. No migration needed.

**The `market_overconfidence` entry rule.** A second qualification path for the
case where the *market* is extreme and our belief is moderate, so the
disagreement is large even though our conviction is not:

- market 0.15, we 0.55 → buy YES
- market 0.85, we 0.45 → buy NO

Direction already falls out correctly from `sign(belief - price)` in both cases.
What blocks them today is only the conviction gate. The `rule` column exists so
this becomes a sibling entry rule rather than a modification of
`conviction_edge`. Note both examples land at `c = 0.15`, so `min_cost_basis`
will also gate them.

**Position sizing.** `expected_roi`, `kelly`, `win_prob` and `cost_basis` are
stored on every signal specifically so a sizing algorithm can be fitted against
realized P&L rather than guessed at. Fractional Kelly is the obvious starting
point, with the fraction as the risk-appetite parameter.

**Grafana paper-trading panels.** Realized P&L over time, open positions, win
rate, rejection-reason breakdown, and P&L bucketed by `cost_basis` — the view
that actually tunes `min_cost_basis`.
