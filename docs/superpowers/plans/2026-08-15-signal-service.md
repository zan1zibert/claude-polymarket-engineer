# Signal Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `signal` service — the singleton that compares our belief against the live Polymarket price, records a decision row when the edge clears the filters, and opens a €1 simulated position that settles against the market's outcome.

**Architecture:** Pure decision math in `lib/signals.py` (no I/O, mirroring `lib/scoring.py`). Two entry paths into one evaluation function: a Redis dirty-market set the worker `SADD`s to, and an hourly sweep that settles resolved positions and rescans conviction-band markets. Both read belief and article attribution from Postgres, so no state lives in Redis beyond a market id. Two new tables, `signals` and `paper_positions`, with a partial unique index enforcing one open position per market in the database rather than in application code.

**Tech Stack:** Python 3.12, psycopg 3 (`psycopg[binary]`), redis-py, httpx, prometheus_client, pytest. Postgres 16 + pgvector. All work happens in `part-3/`.

**Spec:** `docs/superpowers/specs/2026-08-15-signal-service-design.md` — read it before starting. The plan argues from that spec; where this plan gives a concrete signature the spec gives prose, the plan wins.

## Global Constraints

- **All paths are relative to `part-3/`.** Run `pytest` from `part-3/`; `pytest.ini` sets `pythonpath = .`.
- **Everything is paper trading.** No code in this plan places a real order or touches a wallet. `stake` is euros in a simulated ledger.
- **Migrations are forward-only.** Add `db/migrations/0005_signals_positions.sql`; never edit an applied migration. Keep statements `IF NOT EXISTS` where cheap.
- **`lib/signals.py` must stay pure** — no DB, no network, no `datetime.now()` inside it. `now` is always a parameter. This is what lets a future backtest reuse the identical math.
- **DB tests skip without `TEST_DATABASE_URL`.** A bare `pytest` must stay green. Use `pytestmark = pytest.mark.skipif(...)` exactly as `tests/test_scorer_db.py` does.
- **Test market ids are prefixed `utest_`** and each test module cleans up its own ids. Delete in FK order: `paper_positions` → `signals` → `belief_updates` → `markets`.
- **Timestamps are timezone-aware UTC.** Postgres columns are `TIMESTAMPTZ`; any `datetime` compared against one must carry `tzinfo`.
- **Config knobs live only in `lib/config.py`**, read from env, with the default stated in the table there. Naming follows the existing `sync_*` / `scorer_*` prefix convention → `signal_*`.
- **Metric names carry a service prefix, no service label** (`lib/metrics.py` header explains why): `signal_*`.
- **Default thresholds, copied verbatim from the spec:** `min_edge` 0.05, `min_conviction_high` 0.80, `max_conviction_low` 0.20, `max_horizon_days` 14, `min_cost_basis` 0.05, `max_cost_basis` 0.95, `stake` 1.0, `sweep_interval_seconds` 3600.
- **Redis key is `belief_dirty`** (a set), replacing the `belief_updates` list. The old key is left alone; nothing reads it.

### One refinement of the spec

The spec's gate table lists `position_open` alongside the edge gates. That was imprecise: the spec also says a `signals` row is written even when a position is already open. So `position_open` is **not** an `evaluate()` gate — `evaluate()` stays pure and knows nothing about positions. The service inserts the `signals` row whenever `evaluate()` fires, then attempts the position and treats "already open" as a normal, counted outcome. `evaluate()`'s gates are: `market_closed`, `no_belief`, `no_price`, `no_end_date`, `horizon`, `conviction`, `min_edge`, `cost_basis_band`.

---

### Task 1: Migration — `signals` and `paper_positions`

**Files:**
- Create: `db/migrations/0005_signals_positions.sql`
- Test: `tests/test_signals_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces: tables `signals` (columns per the SQL below, `id BIGSERIAL` primary key) and `paper_positions`; the partial unique index `paper_positions_one_open_idx` on `(market_id) WHERE status = 'open'`. Later tasks insert into both.

- [ ] **Step 1: Write the failing test**

Create `tests/test_signals_db.py`:

```python
"""Integration tests for the signal service's schema + DB access.

The partial unique index and the settlement UPDATE are real SQL, so they are
tested against a live Postgres rather than mocked. Point a test DB at it (same
convention as tests/test_scorer_db.py):

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_signals_db.py

Without TEST_DATABASE_URL (or if unreachable) the whole module is skipped, so a
bare `pytest` stays green. Each test uses its own market ids and cleans up.
"""
import os

import psycopg
import pytest

from db import migrate

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run signal DB tests"
)

_MARKET_IDS = ("utest_gA", "utest_gB", "utest_gC", "utest_gD")
_ZERO_VECTOR = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture(scope="module")
def _schema():
    try:
        migrate.run(TEST_DATABASE_URL)
    except Exception as exc:
        pytest.skip(f"TEST_DATABASE_URL not usable: {exc}")


def _conn():
    return psycopg.connect(TEST_DATABASE_URL, autocommit=True)


def _cleanup():
    ids = list(_MARKET_IDS)
    with _conn() as c, c.cursor() as cur:
        # FK order: positions -> signals -> belief_updates -> markets
        cur.execute("DELETE FROM paper_positions WHERE market_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM signals WHERE market_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM belief_updates WHERE market_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (ids,))


@pytest.fixture(autouse=True)
def _clean(_schema):
    _cleanup()
    yield
    _cleanup()


def _seed_market(mid, *, closed=False, current_score=0.85, end_date=None,
                 resolved_outcome=None):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO markets
                (id, question, current_score, seed_price, closed, resolved_outcome,
                 end_date, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (mid, f"q {mid}", current_score, 0.5, closed, resolved_outcome,
             end_date, _ZERO_VECTOR),
        )


def _seed_signal(mid, *, side="YES"):
    """Insert a minimal signals row and return its id."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signals
                (market_id, market_title, rule, source, article_url, belief,
                 yes_price, side, cost_basis, edge, win_prob, expected_roi,
                 kelly, sharpe, end_date, horizon_days)
            VALUES (%s, %s, 'conviction_edge', 'sweep', NULL, 0.85, 0.75, %s,
                    0.75, 0.10, 0.85, 0.1333, 0.4, 0.28, NULL, 7.0)
            RETURNING id
            """,
            (mid, f"q {mid}", side),
        )
        return cur.fetchone()[0]


def _seed_position(mid, signal_id, *, side="YES", entry_price=0.75, stake=1.0,
                   status="open"):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_positions
                (signal_id, market_id, side, entry_price, stake, shares, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (signal_id, mid, side, entry_price, stake, stake / entry_price, status),
        )


def test_second_open_position_on_same_market_is_rejected():
    mid = "utest_gA"
    _seed_market(mid)
    sid = _seed_signal(mid)
    _seed_position(mid, sid)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _seed_position(mid, sid)


def test_a_settled_position_frees_the_market_for_re_entry():
    """The index is partial on status='open', so history never blocks re-entry."""
    mid = "utest_gB"
    _seed_market(mid)
    sid = _seed_signal(mid)
    _seed_position(mid, sid, status="settled")
    _seed_position(mid, sid, status="open")  # must not raise

    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM paper_positions WHERE market_id = %s", (mid,))
        assert cur.fetchone()[0] == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_signals_db.py -v`

Expected: FAIL — `psycopg.errors.UndefinedTable: relation "paper_positions" does not exist`.

If you instead see the module SKIP, you have no test Postgres. Start one:
`docker compose up -d postgres` from `part-3/`, then re-run.

Also confirm the suite stays green without a DB: `pytest tests/test_signals_db.py -v` → 2 skipped.

- [ ] **Step 3: Write the migration**

Create `db/migrations/0005_signals_positions.sql`:

```sql
-- 0005 — signals + paper_positions (the trading loop).
--
-- The scorer closed the *forecasting* loop: it grades our belief against the
-- outcome. Nothing closed the *trading* loop, because nothing ever compared a
-- belief to the live price and committed to a position. These two tables are
-- that record.
--
--   signals         — one row per fired signal: the decision and every number it
--                     was made from. Append-only; an audit trail of intent.
--   paper_positions — the simulated fill and its lifecycle, settled against
--                     markets.resolved_outcome once the market resolves.
--
-- The split matters: a signal can fire on a market we already hold, in which case
-- the signals row is written and no position is opened. Keeping intent (signals)
-- separate from exposure (paper_positions) means that case is visible instead of
-- silently dropped.
--
-- Derived metrics (expected_roi, kelly, sharpe) are stored even though nothing
-- gates on them. They are pure functions of edge and cost_basis, so recomputing
-- them later would be possible — but storing them means a P&L query can bucket by
-- them directly, and the position-sizing algorithm that comes next can be fitted
-- against realised outcomes without a backfill.

CREATE TABLE IF NOT EXISTS signals (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id      TEXT NOT NULL REFERENCES markets(id),
    market_title   TEXT NOT NULL,
    rule           TEXT NOT NULL,              -- entry rule that fired ('conviction_edge')
    source         TEXT NOT NULL,              -- 'belief_update' | 'sweep'
                                               -- (`source`, not `trigger` — PG keyword)
    article_url    TEXT,                       -- newest belief_updates article;
                                               -- NULL if the worker never moved this belief
    belief         DOUBLE PRECISION NOT NULL,  -- markets.current_score at decision time
    yes_price      DOUBLE PRECISION NOT NULL,  -- live Gamma YES price at decision time
    side           TEXT NOT NULL,              -- 'YES' | 'NO' — the side we would buy
    cost_basis     DOUBLE PRECISION NOT NULL,  -- c: price of our side per share
    edge           DOUBLE PRECISION NOT NULL,  -- q - c, positive by construction
    win_prob       DOUBLE PRECISION NOT NULL,  -- q: our P(our side wins)
    expected_roi   DOUBLE PRECISION NOT NULL,  -- edge / c
    kelly          DOUBLE PRECISION NOT NULL,  -- edge / (1 - c)
    sharpe         DOUBLE PRECISION NOT NULL,  -- edge / sqrt(q(1-q))
    end_date       TIMESTAMPTZ,
    horizon_days   DOUBLE PRECISION
);

-- "every signal for this market, newest first" — the one hot read.
CREATE INDEX IF NOT EXISTS signals_market_idx ON signals (market_id, ts DESC);

CREATE TABLE IF NOT EXISTS paper_positions (
    id            BIGSERIAL PRIMARY KEY,
    signal_id     BIGINT NOT NULL REFERENCES signals(id),
    market_id     TEXT NOT NULL REFERENCES markets(id),
    opened_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    side          TEXT NOT NULL,               -- 'YES' | 'NO'
    entry_price   DOUBLE PRECISION NOT NULL,   -- cost basis per share at entry
    stake         DOUBLE PRECISION NOT NULL,   -- euros committed
    shares        DOUBLE PRECISION NOT NULL,   -- stake / entry_price
    status        TEXT NOT NULL DEFAULT 'open', -- 'open' | 'settled' | 'closed_early'
    closed_at     TIMESTAMPTZ,
    exit_price    DOUBLE PRECISION,            -- 1.0/0.0 on settlement; live price on early exit
    exit_reason   TEXT,                        -- 'resolved'; future: 'profit_target'
    pnl           DOUBLE PRECISION             -- euros: shares * exit_price - stake
);

-- One open position per market, enforced by the DATABASE rather than by
-- application code. A partial unique index makes double entry impossible even if
-- the notification path and the sweep path race, or the service restarts
-- mid-cycle. Being partial on status='open' means closed history never blocks a
-- legitimate re-entry after settlement.
CREATE UNIQUE INDEX IF NOT EXISTS paper_positions_one_open_idx
    ON paper_positions (market_id) WHERE status = 'open';

-- The settlement work queue: open positions, joined to their market's outcome.
CREATE INDEX IF NOT EXISTS paper_positions_open_idx
    ON paper_positions (market_id) WHERE status = 'open';
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_signals_db.py -v`

Expected: 2 passed.

Then confirm the migration is idempotent (it will be re-run by the `migrate` service on every `up`):

Run: `TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm python -c "from db import migrate; import os; migrate.run(os.environ['TEST_DATABASE_URL'])"`

Expected: `[migrate] up to date (5 applied, nothing to do)`.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/0005_signals_positions.sql tests/test_signals_db.py
git commit -m "Add signals + paper_positions tables

One open position per market is enforced by a partial unique index rather
than application code, so the notification and sweep paths cannot race
into a double entry."
```

---

### Task 2: `lib/signals.py` — the pure decision function

**Files:**
- Create: `lib/signals.py`
- Test: `tests/test_signals.py`

**Interfaces:**
- Consumes: `lib.scoring.clamp01(p, eps)` (already exists) to keep the Sharpe denominator finite.
- Produces:
  - `Thresholds` frozen dataclass: `min_edge, min_conviction_high, max_conviction_low, max_horizon_days, min_cost_basis, max_cost_basis` (all `float`).
  - `Decision` frozen dataclass: `fired: bool`, `reason: Optional[str]`, `side: Optional[str]`, `cost_basis`, `win_prob`, `edge`, `expected_roi`, `kelly`, `sharpe`, `horizon_days` (all `Optional[float]`, default `None`).
  - `evaluate(*, belief, yes_price, end_date, now, closed, thresholds) -> Decision` — keyword-only.
  - `RULE_CONVICTION_EDGE = "conviction_edge"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_signals.py`:

```python
"""Unit tests for the pure signal decision function.

Pure math, no I/O, so these always run (no TEST_DATABASE_URL needed).

The three rows from the design doc's theory table are asserted literally. They
are the worked examples the whole filter design was argued from, so if someone
"simplifies" the arithmetic later this fails loudly rather than silently
changing which bets the system takes.
"""
from datetime import datetime, timedelta, timezone

import pytest

from lib import signals

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)

# Sentinel so a test can pass end_date=None explicitly and still get the default
# "5 days out" behaviour when it says nothing.
_MISSING = object()

THRESHOLDS = signals.Thresholds(
    min_edge=0.05,
    min_conviction_high=0.80,
    max_conviction_low=0.20,
    max_horizon_days=14,
    min_cost_basis=0.05,
    max_cost_basis=0.95,
)


def _evaluate(belief, yes_price, *, days_out=7.0, closed=False,
              thresholds=THRESHOLDS, end_date=_MISSING):
    end = NOW + timedelta(days=days_out) if end_date is _MISSING else end_date
    return signals.evaluate(
        belief=belief,
        yes_price=yes_price,
        end_date=end,
        now=NOW,
        closed=closed,
        thresholds=thresholds,
    )


# --- side selection -------------------------------------------------------

def test_belief_above_price_buys_yes():
    d = _evaluate(0.85, 0.75)
    assert d.fired
    assert d.side == "YES"
    assert d.cost_basis == pytest.approx(0.75)
    assert d.win_prob == pytest.approx(0.85)
    assert d.edge == pytest.approx(0.10)


def test_belief_below_price_buys_no():
    """High conviction does NOT mean we buy YES. Direction is sign(belief-price)."""
    d = _evaluate(0.80, 0.90)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.10)
    assert d.win_prob == pytest.approx(0.20)
    assert d.edge == pytest.approx(0.10)


def test_low_conviction_band_also_fires():
    d = _evaluate(0.15, 0.30)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.70)
    assert d.win_prob == pytest.approx(0.85)
    assert d.edge == pytest.approx(0.15)


# --- the theory table, asserted literally --------------------------------

def test_theory_row_cheap_longshot():
    """market 0.95, belief 0.85 -> NO at c=0.05: +200% ROI, 10.5% Kelly."""
    d = _evaluate(0.85, 0.95)
    assert d.fired
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.05)
    assert d.win_prob == pytest.approx(0.15)
    assert d.edge == pytest.approx(0.10)
    assert d.expected_roi == pytest.approx(2.00, abs=1e-4)
    assert d.kelly == pytest.approx(0.10526, abs=1e-4)
    assert d.sharpe == pytest.approx(0.28005, abs=1e-4)


def test_theory_row_middling():
    """market 0.85, belief 0.80 -> NO at c=0.15: +33% ROI, 5.9% Kelly."""
    d = _evaluate(0.80, 0.85)
    assert d.side == "NO"
    assert d.cost_basis == pytest.approx(0.15)
    assert d.expected_roi == pytest.approx(0.33333, abs=1e-4)
    assert d.kelly == pytest.approx(0.05882, abs=1e-4)


def test_theory_row_favourite():
    """market 0.75, belief 0.80 -> YES at c=0.75: +6.7% ROI, 20% Kelly."""
    d = _evaluate(0.80, 0.75)
    assert d.side == "YES"
    assert d.cost_basis == pytest.approx(0.75)
    assert d.expected_roi == pytest.approx(0.06667, abs=1e-4)
    assert d.kelly == pytest.approx(0.20, abs=1e-4)


def test_favourite_has_lower_sharpe_than_the_big_edge_longshot():
    """The design's claim that tail bets win on risk-adjusted terms."""
    longshot = _evaluate(0.85, 0.95)
    favourite = _evaluate(0.80, 0.75)
    assert longshot.sharpe > 2 * favourite.sharpe


# --- gates ---------------------------------------------------------------

def test_closed_market_is_rejected():
    d = _evaluate(0.85, 0.75, closed=True)
    assert not d.fired
    assert d.reason == "market_closed"


def test_missing_belief_is_rejected():
    d = _evaluate(None, 0.75)
    assert not d.fired
    assert d.reason == "no_belief"


def test_missing_price_is_rejected():
    d = _evaluate(0.85, None)
    assert not d.fired
    assert d.reason == "no_price"


def test_missing_end_date_is_rejected():
    d = _evaluate(0.85, 0.75, end_date=None)
    assert not d.fired
    assert d.reason == "no_end_date"


def test_beyond_horizon_is_rejected():
    d = _evaluate(0.85, 0.75, days_out=30.0)
    assert not d.fired
    assert d.reason == "horizon"


def test_already_past_end_date_is_rejected():
    d = _evaluate(0.85, 0.75, days_out=-1.0)
    assert not d.fired
    assert d.reason == "horizon"


def test_mid_conviction_is_rejected():
    """The case the future market_overconfidence rule will pick up."""
    d = _evaluate(0.55, 0.15)
    assert not d.fired
    assert d.reason == "conviction"


def test_edge_below_threshold_is_rejected():
    d = _evaluate(0.85, 0.82)
    assert not d.fired
    assert d.reason == "min_edge"


def test_zero_edge_is_rejected():
    d = _evaluate(0.85, 0.85)
    assert not d.fired
    assert d.reason == "min_edge"


def test_cost_basis_below_band_is_rejected():
    """market 0.97, belief 0.85 -> NO at c=0.03, under the 0.05 floor."""
    d = _evaluate(0.85, 0.97)
    assert not d.fired
    assert d.reason == "cost_basis_band"


def test_tiny_cost_basis_on_the_yes_side_is_rejected():
    """Mirror case: market 0.02, belief 0.15 -> YES at c=0.02, under the floor.

    Note the max_cost_basis ceiling is defensive rather than currently reachable:
    c > 0.95 forces edge = q - c < 0.05, so min_edge rejects first while
    min_edge >= 0.05. It exists so raising min_edge later can't silently admit
    near-certain bets.
    """
    d = _evaluate(0.15, 0.02)
    assert not d.fired
    assert d.reason == "cost_basis_band"


def test_raising_min_cost_basis_removes_longshots():
    """min_cost_basis is the risk dial: it filters out cheap-side bets."""
    strict = signals.Thresholds(
        min_edge=0.05, min_conviction_high=0.80, max_conviction_low=0.20,
        max_horizon_days=14, min_cost_basis=0.30, max_cost_basis=0.95,
    )
    assert _evaluate(0.85, 0.95).fired                       # c=0.05, default band
    assert not _evaluate(0.85, 0.95, thresholds=strict).fired
    assert _evaluate(0.85, 0.95, thresholds=strict).reason == "cost_basis_band"
    assert _evaluate(0.80, 0.75, thresholds=strict).fired    # c=0.75 survives


# --- boundaries ----------------------------------------------------------

def test_edge_exactly_at_threshold_fires():
    d = _evaluate(0.85, 0.80)
    assert d.fired
    assert d.edge == pytest.approx(0.05)


def test_belief_exactly_at_conviction_boundary_fires():
    assert _evaluate(0.80, 0.70).fired
    assert _evaluate(0.20, 0.30).fired


def test_belief_just_inside_the_bands_is_rejected():
    assert _evaluate(0.79, 0.60).reason == "conviction"
    assert _evaluate(0.21, 0.40).reason == "conviction"


def test_end_date_exactly_at_horizon_fires():
    assert _evaluate(0.85, 0.75, days_out=14.0).fired


def test_cost_basis_exactly_on_the_floor_fires():
    """Both sides: the floor is inclusive, so c == min_cost_basis is allowed."""
    assert _evaluate(0.85, 0.95).fired    # NO  at c = 0.05 exactly
    assert _evaluate(0.15, 0.05).fired    # YES at c = 0.05 exactly


# --- gate ordering -------------------------------------------------------

def test_first_failing_gate_wins():
    """Closed AND mid-conviction AND no edge -> reports market_closed."""
    d = _evaluate(0.55, 0.55, closed=True)
    assert d.reason == "market_closed"


def test_conviction_is_checked_before_edge():
    """Mid-conviction with a huge edge reports conviction, not min_edge."""
    d = _evaluate(0.55, 0.15)
    assert d.reason == "conviction"


# --- degenerate inputs ---------------------------------------------------

def test_certain_belief_gives_finite_sharpe():
    """belief=1.0 would divide by sqrt(0); clamped instead of raising."""
    d = _evaluate(1.0, 0.60)
    assert d.fired
    assert d.sharpe == pytest.approx(0.4 / 1e-9 ** 0.5, rel=0.01)
    assert d.sharpe == d.sharpe  # not NaN


def test_rejected_decisions_carry_no_metrics():
    d = _evaluate(0.85, 0.82)
    assert d.expected_roi is None
    assert d.kelly is None
    assert d.sharpe is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_signals.py -v`

Expected: collection error — `ModuleNotFoundError: No module named 'lib.signals'`.

- [ ] **Step 3: Write the implementation**

Create `lib/signals.py`:

```python
"""Pure signal-decision math — no I/O, no clock, no dependencies.

Given our belief and the live market price, decide whether to bet, which side,
and how good the bet is. Everything here is a pure function of its arguments —
`now` is a parameter, never `datetime.now()` — for the same reason lib/scoring.py
is: a future backtest must be able to replay the identical decisions offline, and
an online/offline disagreement in *this* file would invalidate every conclusion
drawn from the paper P&L.

## The math

Side selection happens first; everything else is expressed in terms of the side
we would buy:

    side = YES if belief > price else NO
    c    = price  if YES else 1 - price      # what one share costs
    q    = belief if YES else 1 - belief     # our P(this side wins)
    edge = q - c                             # positive by construction

Three quantities follow, and with a flat stake they are the whole basis of the
risk/reward tradeoff:

    expected_roi = edge / c            reward per euro staked
    kelly        = edge / (1 - c)      fraction of bankroll this bet deserves
    sharpe       = edge / sqrt(q(1-q)) reward per unit of variance

ROI and Kelly point in OPPOSITE directions in c, which is why `min_cost_basis` is
the system's risk dial rather than an arbitrary sanity bound: c < 0.5 means we are
buying the underdog, so raising the floor trades ROI for hit rate. Worked
examples, asserted in tests/test_signals.py:

    market 0.95, belief 0.85 -> NO  at c=0.05: ROI +200%, Kelly 10.5%, wins 15%
    market 0.85, belief 0.80 -> NO  at c=0.15: ROI  +33%, Kelly  5.9%, wins 20%
    market 0.75, belief 0.80 -> YES at c=0.75: ROI  +6.7%, Kelly 20.0%, wins 80%

## What the conviction band does NOT do

It does not pick a direction. It only decides whether our belief is confident
enough to act on at all; direction is sign(belief - price). So a high-conviction
0.80 belief against a market at 0.85 buys NO — betting against our own
directional view because the market is *more* extreme than we are. That is
correct by expected value and intended; `Decision.side` records it so a log
reader is never left guessing.
"""
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import Optional

from lib.scoring import clamp01

RULE_CONVICTION_EDGE = "conviction_edge"

# Keeps sqrt(q(1-q)) finite when a belief sits exactly at 0 or 1. Such a belief is
# already pathological; the point is to produce a very large Sharpe rather than
# raise ZeroDivisionError inside a service loop.
_SHARPE_EPS = 1e-9

SIDE_YES = "YES"
SIDE_NO = "NO"


@dataclass(frozen=True)
class Thresholds:
    """The filter configuration, straight from lib.config.Settings."""
    min_edge: float
    min_conviction_high: float
    max_conviction_low: float
    max_horizon_days: float
    min_cost_basis: float
    max_cost_basis: float


@dataclass(frozen=True)
class Decision:
    """Either a fired signal with every number it was made from, or a rejection.

    On a rejection only `reason` is set — the metric fields stay None, so a caller
    can never accidentally persist half-computed arithmetic.
    """
    fired: bool
    reason: Optional[str] = None
    side: Optional[str] = None
    cost_basis: Optional[float] = None
    win_prob: Optional[float] = None
    edge: Optional[float] = None
    expected_roi: Optional[float] = None
    kelly: Optional[float] = None
    sharpe: Optional[float] = None
    horizon_days: Optional[float] = None


def _reject(reason: str, *, horizon_days: Optional[float] = None) -> Decision:
    return Decision(fired=False, reason=reason, horizon_days=horizon_days)


def evaluate(
    *,
    belief: Optional[float],
    yes_price: Optional[float],
    end_date: Optional[datetime],
    now: datetime,
    closed: bool,
    thresholds: Thresholds,
) -> Decision:
    """Decide whether this market is worth a bet right now.

    `end_date` and `now` must both be timezone-aware (Postgres hands back
    TIMESTAMPTZ). Gates run in a fixed order and the FIRST failure is the reason,
    so a rejection reason is always the cheapest true explanation — that ordering
    is what makes the `signal_rejected_total{reason}` counter readable.

    Note what is absent: this function knows nothing about open positions. A
    signal can legitimately fire on a market we already hold (the signals row is
    still worth recording); deciding whether to take exposure is the service's
    job, not the math's.
    """
    if closed:
        return _reject("market_closed")
    if belief is None:
        return _reject("no_belief")
    if yes_price is None:
        return _reject("no_price")
    if end_date is None:
        return _reject("no_end_date")

    horizon_days = (end_date - now).total_seconds() / 86400.0
    if not (0.0 < horizon_days <= thresholds.max_horizon_days):
        return _reject("horizon", horizon_days=horizon_days)

    if not (belief >= thresholds.min_conviction_high
            or belief <= thresholds.max_conviction_low):
        return _reject("conviction", horizon_days=horizon_days)

    # Side, then everything in terms of the side we would buy.
    if belief > yes_price:
        side, cost_basis, win_prob = SIDE_YES, yes_price, belief
    else:
        side, cost_basis, win_prob = SIDE_NO, 1.0 - yes_price, 1.0 - belief
    edge = win_prob - cost_basis

    if edge < thresholds.min_edge:
        return _reject("min_edge", horizon_days=horizon_days)
    if not (thresholds.min_cost_basis <= cost_basis <= thresholds.max_cost_basis):
        return _reject("cost_basis_band", horizon_days=horizon_days)

    # Derived metrics are computed only after the band gate, which is what makes
    # both divisions safe: the gate guarantees 0 < min_cost_basis <= c <=
    # max_cost_basis < 1, so neither c nor (1 - c) can be zero.
    variance = win_prob * (1.0 - win_prob)
    return Decision(
        fired=True,
        side=side,
        cost_basis=cost_basis,
        win_prob=win_prob,
        edge=edge,
        expected_roi=edge / cost_basis,
        kelly=edge / (1.0 - cost_basis),
        sharpe=edge / sqrt(clamp01(variance, _SHARPE_EPS)),
        horizon_days=horizon_days,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_signals.py -v`

Expected: all pass (about 25 tests).

Then confirm nothing else broke: `pytest -q`

- [ ] **Step 5: Commit**

```bash
git add lib/signals.py tests/test_signals.py
git commit -m "Add pure signal decision math

Side comes from sign(belief - price), not from the conviction band; the
band only decides whether the belief is trustworthy. Derived metrics are
computed after the cost-basis gate, which is what makes both divisions
safe by construction."
```

---

### Task 3: The dirty-market set replaces the belief list

**Files:**
- Modify: `lib/queue.py` (replace the `BeliefQueue` class)
- Modify: `lib/config.py:31` (`belief_queue_key` → `belief_dirty_key`) and its `load_settings` entry
- Modify: `services/worker/main.py:135` (the push), `:72` (parameter type), `:159` (construction)
- Modify: `lib/metrics.py` (add the depth gauge)
- Test: `tests/test_dirty_markets.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `lib.queue.DirtyMarkets(redis_url: str, key: str)` with `from_client(cls, client, key)`, `add(market_id: str) -> None`, `pop() -> Optional[str]`, `depth() -> int`.
  - `Settings.belief_dirty_key: str` (env `BELIEF_DIRTY_KEY`, default `belief_dirty`).
  - `metrics.BELIEF_DIRTY_DEPTH` gauge.

This task must land as one commit: renaming the config field breaks the worker until its call site is updated too.

- [ ] **Step 1: Write the failing test**

Create `tests/test_dirty_markets.py`:

```python
"""Unit tests for DirtyMarkets against an in-memory fake redis.

The fake models only the set ops used (SADD/SPOP/SCARD), which is enough to
verify the property the whole design turns on: adding the same market twice
leaves ONE pending entry, so repeat notifications collapse for free. A Redis list
cannot do that without an O(n) scan-and-LREM, which is why this is a set.

Same fake-client injection pattern as tests/test_market_snapshot.py.
"""
from lib.queue import DirtyMarkets


class _FakeRedis:
    def __init__(self):
        self.sets = {}

    def sadd(self, key, *values):
        s = self.sets.setdefault(key, set())
        before = len(s)
        s.update(values)
        return len(s) - before

    def spop(self, key):
        s = self.sets.get(key)
        if not s:
            return None
        return s.pop()

    def scard(self, key):
        return len(self.sets.get(key, ()))


def _dirty():
    return DirtyMarkets.from_client(_FakeRedis(), "belief_dirty")


def test_pop_on_empty_returns_none():
    assert _dirty().pop() is None


def test_add_then_pop_roundtrips():
    d = _dirty()
    d.add("123")
    assert d.pop() == "123"
    assert d.pop() is None


def test_adding_the_same_market_twice_collapses():
    """The reason this is a set: repeat notifications dedupe by construction."""
    d = _dirty()
    d.add("123")
    d.add("123")
    d.add("123")
    assert d.depth() == 1
    assert d.pop() == "123"
    assert d.pop() is None


def test_distinct_markets_are_all_kept():
    d = _dirty()
    d.add("123")
    d.add("456")
    assert d.depth() == 2
    assert {d.pop(), d.pop()} == {"123", "456"}


def test_depth_reflects_pending_count():
    d = _dirty()
    assert d.depth() == 0
    d.add("123")
    assert d.depth() == 1
    d.pop()
    assert d.depth() == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_dirty_markets.py -v`

Expected: collection error — `ImportError: cannot import name 'DirtyMarkets' from 'lib.queue'`.

- [ ] **Step 3: Replace `BeliefQueue` with `DirtyMarkets`**

In `lib/queue.py`, update the module docstring's second bullet and swap the class. Replace:

```python
class BeliefQueue:
    def __init__(self, redis_url: str, queue_key: str):
        self._r = _client(redis_url)
        self._key = queue_key

    def push(self, update: BeliefUpdate) -> None:
        self._r.lpush(self._key, update.to_json())

    def depth(self) -> int:
        return self._r.llen(self._key)
```

with:

```python
class DirtyMarkets:
    """The worker -> signal notification channel: a Redis SET of market ids.

    Why a set and not a list. The signal service reads the belief itself from
    Postgres (markets.current_score) and the triggering article from the newest
    belief_updates row, so Redis has nothing to carry but "this market needs
    another look". Once the payload is just an id, SADD gives collapsing for
    free: a second notification for a market that is still pending is a no-op.
    A list cannot do that — LREM matches only on exact element value, so
    per-market dedup would mean LRANGE-ing the whole list, parsing every payload
    and LREM-ing the matches: O(n) per push, and racy without a Lua script.

    Collapsing is an efficiency property, not a correctness one — the partial
    unique index on paper_positions already prevents a double entry. What it
    saves is redundant Gamma calls and duplicate `signals` rows, because with the
    belief read from the DB, evaluating the same market three times in a row just
    reaches the same verdict three times.

    Losing FIFO and blocking BRPOP costs nothing here: the markets are
    independent of each other, and the consumer loop already wakes every few
    seconds to check whether a sweep is due.
    """

    def __init__(self, redis_url: str, key: str):
        self._r = _client(redis_url)
        self._key = key

    @classmethod
    def from_client(cls, client, key: str) -> "DirtyMarkets":
        """Inject a client directly — used by tests with a fake redis."""
        self = cls.__new__(cls)
        self._r = client
        self._key = key
        return self

    def add(self, market_id: str) -> None:
        """Mark a market as needing evaluation. Idempotent while it stays pending."""
        self._r.sadd(self._key, market_id)

    def pop(self) -> Optional[str]:
        """Claim one pending market id, or None if nothing is pending.

        SPOP removes before the caller evaluates, so a crash mid-evaluation drops
        that notification. That is acceptable rather than sloppy: the hourly sweep
        re-examines every conviction-band market anyway, so the cost is latency,
        and each signal/position write is a single transaction that cannot be left
        half-applied.
        """
        return self._r.spop(self._key)

    def depth(self) -> int:
        return self._r.scard(self._key)
```

At the top of `lib/queue.py`, the import of `BeliefUpdate` is now unused — change

```python
from lib.schemas import Article, BeliefUpdate
```

to

```python
from typing import Optional

from lib.schemas import Article
```

(`BeliefUpdate` itself stays in `lib/schemas.py` untouched — it is still the return type of `db.apply_belief_update`, the shape of a `belief_updates` row, and what the worker writes to the JSONL audit log.)

Update the module docstring's queue list:

```python
"""Thin Redis queue wrappers.

Two hops in the pipeline:
  - NewsQueue    : feeder LPUSHes articles; the worker BRPOPs them (a list, because
                   order and at-least-once delivery of distinct articles matter).
  - DirtyMarkets : the worker SADDs market ids for the signal service to SPOP (a
                   set, because repeat notifications for one market should
                   collapse — see the class docstring).
"""
```

- [ ] **Step 4: Rename the config key**

In `lib/config.py`, change the field declaration (currently `belief_queue_key: str  # second Redis list: worker -> signal`) to:

```python
    belief_dirty_key: str        # Redis SET of market ids: worker -> signal
```

and in `load_settings()` replace the `belief_queue_key=...` line with:

```python
        # Renamed from BELIEF_QUEUE_KEY / `belief_updates`: the key now holds a
        # SET, and a set cannot share a key with the old list (Redis raises
        # WRONGTYPE). Renaming makes the stale, never-consumed list inert instead
        # of fatal, so no manual flush is needed before first run.
        belief_dirty_key=os.environ.get("BELIEF_DIRTY_KEY", "belief_dirty"),
```

- [ ] **Step 5: Update the worker's call site**

In `services/worker/main.py`:

- Change the import `from lib.queue import BeliefQueue, NewsQueue` to `from lib.queue import DirtyMarkets, NewsQueue`. (Check the exact current import line and edit it in place.)
- In `process_article`'s signature (around line 72), rename the parameter `belief_queue: BeliefQueue` to `dirty_markets: DirtyMarkets`.
- Replace line 135:

```python
        belief_queue.push(update)
```

with:

```python
        # Notify the signal service. Only the id travels: it reads the belief from
        # markets.current_score and the article from belief_updates itself, so a
        # payload here could only go stale. The DB row and the JSONL audit log
        # below still carry the full update.
        dirty_markets.add(market.id)
```

- Around line 159, replace the construction:

```python
    belief_queue = BeliefQueue(settings.redis_url, settings.belief_queue_key)
```

with:

```python
    dirty_markets = DirtyMarkets(settings.redis_url, settings.belief_dirty_key)
```

- Update the `process_article(...)` call (around line 183) to pass `dirty_markets`.
- Update the module docstring line about pushing to the belief queue to say it marks the market dirty for the signal service.

- [ ] **Step 6: Add the depth gauge**

In `lib/metrics.py`, next to the existing `NEWS_QUEUE_DEPTH` gauge, add:

```python
BELIEF_DIRTY_DEPTH = Gauge(
    "belief_dirty_depth",
    "Markets pending evaluation by the signal service (SCARD of the dirty set)",
)
```

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_dirty_markets.py -v`
Expected: 5 passed.

Run: `pytest -q`
Expected: everything green. If `tests/test_worker.py` fails on a `belief_queue` keyword or a fake with a `push` method, update the fake there to expose `add(market_id)` and assert on the collected ids instead of pushed payloads.

Run: `grep -rn "BeliefQueue\|belief_queue_key\|BELIEF_QUEUE_KEY" lib/ services/ tests/ db/`
Expected: no output. (`.env.example` still mentions it; Task 6 fixes the docs.)

- [ ] **Step 8: Commit**

```bash
git add lib/queue.py lib/config.py lib/metrics.py services/worker/main.py tests/test_dirty_markets.py
git commit -m "Replace the belief list with a dirty-market set

Once the signal service reads belief from Postgres, Redis only needs to
carry which markets need another look — and SADD then collapses repeat
notifications for free, which a list cannot do without an O(n) scan.
BeliefUpdate is unchanged; it still backs the DB row and audit log."
```

---

### Task 4: `lib/db.py` — signal and position access

**Files:**
- Modify: `lib/db.py` (add six methods; extend the module docstring)
- Test: `tests/test_signals_db.py` (extend the file from Task 1)

**Interfaces:**
- Consumes: `signals` / `paper_positions` from Task 1.
- Produces, all on `Db`:
  - `market_for_signal(market_id: str) -> Optional[dict]` — keys: `market_id, question, current_score, end_date, closed, article_url`.
  - `signal_candidate_markets(*, min_conviction_high: float, max_conviction_low: float, max_horizon_days: float) -> list[dict]` — same keys.
  - `insert_signal(*, market_id, market_title, rule, source, article_url, belief, yes_price, side, cost_basis, edge, win_prob, expected_roi, kelly, sharpe, end_date, horizon_days) -> int` — returns the new `signals.id`.
  - `open_position(*, signal_id: int, market_id: str, side: str, entry_price: float, stake: float) -> bool` — `False` when one is already open.
  - `settle_positions() -> list[dict]` — keys: `market_id, side, exit_price, pnl`.
  - `position_aggregates() -> dict` — keys: `open, settled, pnl_total, wins, staked`.

- [ ] **Step 1: Widen the cleanup, then write the failing tests**

The aggregate tests read whole-table sums, so this module must own
`paper_positions` for the duration of its run. In `tests/test_signals_db.py`,
replace the two positions/signals lines inside `_cleanup()` with prefix deletes:

```python
def _cleanup():
    ids = list(_MARKET_IDS)
    with _conn() as c, c.cursor() as cur:
        # FK order: positions -> signals -> belief_updates -> markets.
        # Positions and signals are cleared by the utest_ prefix rather than by
        # this module's ids, because position_aggregates sums the whole table.
        cur.execute("DELETE FROM paper_positions WHERE market_id LIKE 'utest\\_%'")
        cur.execute("DELETE FROM signals WHERE market_id LIKE 'utest\\_%'")
        cur.execute("DELETE FROM belief_updates WHERE market_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (ids,))
```

Add `from datetime import datetime, timedelta, timezone` and `from lib.db import Db`
to the imports at the **top** of the file, then append the tests below.

```python
def _db():
    return Db(TEST_DATABASE_URL)


def _seed_belief_update(mid, article_url, *, ts):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO belief_updates
                (ts, market_id, market_title, previous_score, new_score,
                 article_url, reasoning)
            VALUES (%s, %s, %s, 0.5, 0.85, %s, 'because')
            """,
            (ts, mid, f"q {mid}", article_url),
        )


# --- market_for_signal ---------------------------------------------------

def test_market_for_signal_returns_none_for_unknown_market():
    assert _db().market_for_signal("utest_gZ_nope") is None


def test_market_for_signal_returns_the_newest_article():
    mid = "utest_gA"
    end = datetime.now(timezone.utc) + timedelta(days=5)
    _seed_market(mid, end_date=end)
    now = datetime.now(timezone.utc)
    _seed_belief_update(mid, "https://old.example", ts=now - timedelta(hours=2))
    _seed_belief_update(mid, "https://new.example", ts=now)

    row = _db().market_for_signal(mid)
    assert row["market_id"] == mid
    assert row["current_score"] == pytest.approx(0.85)
    assert row["closed"] is False
    assert row["article_url"] == "https://new.example"


def test_market_for_signal_article_is_none_when_never_evaluated():
    mid = "utest_gB"
    _seed_market(mid, end_date=datetime.now(timezone.utc) + timedelta(days=5))
    assert _db().market_for_signal(mid)["article_url"] is None


# --- signal_candidate_markets -------------------------------------------

_BANDS = dict(min_conviction_high=0.80, max_conviction_low=0.20, max_horizon_days=14)


def _candidate_ids():
    return {r["market_id"] for r in _db().signal_candidate_markets(**_BANDS)}


def test_candidates_include_both_conviction_bands():
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _seed_market("utest_gA", current_score=0.85, end_date=soon)
    _seed_market("utest_gB", current_score=0.15, end_date=soon)
    ids = _candidate_ids()
    assert {"utest_gA", "utest_gB"} <= ids


def test_candidates_exclude_mid_conviction():
    soon = datetime.now(timezone.utc) + timedelta(days=5)
    _seed_market("utest_gC", current_score=0.55, end_date=soon)
    assert "utest_gC" not in _candidate_ids()


def test_candidates_exclude_beyond_horizon_and_already_ended():
    _seed_market("utest_gA", current_score=0.85,
                 end_date=datetime.now(timezone.utc) + timedelta(days=40))
    _seed_market("utest_gB", current_score=0.85,
                 end_date=datetime.now(timezone.utc) - timedelta(days=1))
    ids = _candidate_ids()
    assert "utest_gA" not in ids
    assert "utest_gB" not in ids


def test_candidates_exclude_closed_markets():
    _seed_market("utest_gC", current_score=0.85, closed=True,
                 end_date=datetime.now(timezone.utc) + timedelta(days=5))
    assert "utest_gC" not in _candidate_ids()


def test_candidates_exclude_markets_with_an_open_position():
    mid = "utest_gD"
    _seed_market(mid, current_score=0.85,
                 end_date=datetime.now(timezone.utc) + timedelta(days=5))
    assert mid in _candidate_ids()
    _seed_position(mid, _seed_signal(mid))
    assert mid not in _candidate_ids()


# --- insert_signal + open_position --------------------------------------

def test_insert_signal_returns_an_id_and_open_position_uses_it():
    mid = "utest_gA"
    _seed_market(mid, end_date=datetime.now(timezone.utc) + timedelta(days=5))
    db = _db()
    sid = db.insert_signal(
        market_id=mid, market_title="q", rule="conviction_edge",
        source="belief_update", article_url="https://a.example", belief=0.85,
        yes_price=0.75, side="YES", cost_basis=0.75, edge=0.10, win_prob=0.85,
        expected_roi=0.1333, kelly=0.40, sharpe=0.28,
        end_date=datetime.now(timezone.utc) + timedelta(days=5), horizon_days=5.0,
    )
    assert isinstance(sid, int)
    assert db.open_position(
        signal_id=sid, market_id=mid, side="YES", entry_price=0.75, stake=1.0
    ) is True


def test_open_position_computes_shares_from_stake_and_price():
    mid = "utest_gB"
    _seed_market(mid)
    db = _db()
    sid = _seed_signal(mid)
    db.open_position(signal_id=sid, market_id=mid, side="NO", entry_price=0.25,
                     stake=1.0)
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT shares, status FROM paper_positions WHERE market_id = %s",
                    (mid,))
        shares, status = cur.fetchone()
    assert shares == pytest.approx(4.0)
    assert status == "open"


def test_open_position_returns_false_when_one_is_already_open():
    """The unique-index violation is a normal outcome, not an exception."""
    mid = "utest_gC"
    _seed_market(mid)
    db = _db()
    sid = _seed_signal(mid)
    assert db.open_position(signal_id=sid, market_id=mid, side="YES",
                            entry_price=0.75, stake=1.0) is True
    assert db.open_position(signal_id=sid, market_id=mid, side="YES",
                            entry_price=0.75, stake=1.0) is False


# --- settle_positions ---------------------------------------------------

def _settled_row(mid):
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT status, exit_price, exit_reason, pnl, closed_at "
            "FROM paper_positions WHERE market_id = %s",
            (mid,),
        )
        return cur.fetchone()


@pytest.mark.parametrize(
    "side,outcome,expected_exit,expected_pnl",
    [
        ("YES", 1.0, 1.0, pytest.approx(1.0 / 0.5 - 1.0)),   # YES won:  +1.0
        ("YES", 0.0, 0.0, pytest.approx(-1.0)),              # YES lost: -stake
        ("NO", 0.0, 1.0, pytest.approx(1.0 / 0.5 - 1.0)),    # NO won:   +1.0
        ("NO", 1.0, 0.0, pytest.approx(-1.0)),               # NO lost:  -stake
    ],
)
def test_settlement_pnl_for_every_outcome(side, outcome, expected_exit, expected_pnl):
    mid = "utest_gA"
    _seed_market(mid, closed=True, resolved_outcome=outcome)
    _seed_position(mid, _seed_signal(mid, side=side), side=side, entry_price=0.5,
                   stake=1.0)

    settled = _db().settle_positions()
    assert [s["market_id"] for s in settled] == [mid]
    assert settled[0]["exit_price"] == pytest.approx(expected_exit)
    assert settled[0]["pnl"] == expected_pnl

    status, exit_price, exit_reason, pnl, closed_at = _settled_row(mid)
    assert status == "settled"
    assert exit_price == pytest.approx(expected_exit)
    assert exit_reason == "resolved"
    assert pnl == expected_pnl
    assert closed_at is not None


def test_settlement_skips_markets_without_a_known_outcome():
    """Closed but resolved_outcome NULL -> stays open, mirroring forecast_scores."""
    mid = "utest_gB"
    _seed_market(mid, closed=True, resolved_outcome=None)
    _seed_position(mid, _seed_signal(mid))
    assert _db().settle_positions() == []
    assert _settled_row(mid)[0] == "open"


def test_settlement_skips_an_ambiguous_half_outcome():
    """resolved_outcome = 0.5 means undetermined; the scorer excludes it too."""
    mid = "utest_gC"
    _seed_market(mid, closed=True, resolved_outcome=0.5)
    _seed_position(mid, _seed_signal(mid))
    assert _db().settle_positions() == []
    assert _settled_row(mid)[0] == "open"


def test_settlement_is_idempotent():
    mid = "utest_gD"
    _seed_market(mid, closed=True, resolved_outcome=1.0)
    _seed_position(mid, _seed_signal(mid), side="YES", entry_price=0.5)
    db = _db()
    assert len(db.settle_positions()) == 1
    assert db.settle_positions() == []          # second sweep books nothing twice
    assert _settled_row(mid)[3] == pytest.approx(1.0)


# --- position_aggregates ------------------------------------------------

def test_position_aggregates_over_a_mixed_book():
    win, loss, still_open = "utest_gA", "utest_gB", "utest_gC"
    _seed_market(win, closed=True, resolved_outcome=1.0)
    _seed_market(loss, closed=True, resolved_outcome=0.0)
    _seed_market(still_open)
    _seed_position(win, _seed_signal(win, side="YES"), side="YES", entry_price=0.5)
    _seed_position(loss, _seed_signal(loss, side="YES"), side="YES", entry_price=0.5)
    _seed_position(still_open, _seed_signal(still_open))

    db = _db()
    db.settle_positions()
    agg = db.position_aggregates()
    assert agg["open"] == 1
    assert agg["settled"] == 2
    assert agg["wins"] == 1
    assert agg["staked"] == pytest.approx(2.0)
    assert agg["pnl_total"] == pytest.approx(0.0)   # +1.0 and -1.0


def test_position_aggregates_on_an_empty_book():
    agg = _db().position_aggregates()
    assert agg["settled"] == 0
    assert agg["pnl_total"] == pytest.approx(0.0)
```

The two `position_aggregates` tests depend on the widened `_cleanup()` from the
top of this step: they assert whole-table sums, which only hold because the
`autouse` `_clean` fixture has emptied every `utest_`-prefixed row first. If you
run this module concurrently with another that writes `paper_positions`, they will
flake — don't.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_signals_db.py -v`

Expected: FAIL — `AttributeError: 'Db' object has no attribute 'market_for_signal'`.

- [ ] **Step 3: Write the implementation**

In `lib/db.py`, extend the module docstring's operation list:

```python
Operations the signal service needs (services/signal/):
  - market_for_signal        : one market's belief/horizon + its newest article
  - signal_candidate_markets : the sweep's work list (conviction band, in horizon,
                               no open position)
  - insert_signal            : record a fired signal, returns its id
  - open_position            : open a €1 paper position (False if one is open)
  - settle_positions         : book P&L on positions whose market has resolved
  - position_aggregates      : book-wide totals for the Prometheus gauges
```

Add these methods to `Db`:

```python
    # A market's belief and horizon, plus the article that last moved the belief.
    # The LATERAL join is the "newest child row" idiom; belief_updates already has
    # a (market_id, ts DESC) index, so it is an index scan of one row per market.
    _MARKET_SELECT = """
        SELECT m.id, m.question, m.current_score, m.end_date, m.closed,
               b.article_url
        FROM markets m
        LEFT JOIN LATERAL (
            SELECT article_url
            FROM belief_updates
            WHERE market_id = m.id
            ORDER BY ts DESC
            LIMIT 1
        ) b ON TRUE
    """

    @staticmethod
    def _market_row(r) -> dict:
        return {
            "market_id": r[0],
            "question": r[1],
            "current_score": r[2],
            "end_date": r[3],
            "closed": r[4],
            "article_url": r[5],
        }

    def market_for_signal(self, market_id: str) -> Optional[dict]:
        """Everything the decision needs about one market, or None if unknown.

        The signal service reads the belief from here rather than from the Redis
        notification: with repeat notifications collapsing, a payload's score
        could be several updates stale by the time it is popped, while
        current_score is by definition the latest.
        """
        with self._conn.cursor() as cur:
            cur.execute(self._MARKET_SELECT + " WHERE m.id = %s", (market_id,))
            row = cur.fetchone()
            return None if row is None else self._market_row(row)

    def signal_candidate_markets(
        self,
        *,
        min_conviction_high: float,
        max_conviction_low: float,
        max_horizon_days: float,
    ) -> list[dict]:
        """The sweep's work list: markets worth re-examining right now.

        Two reasons this exists rather than relying on the notification path
        alone. A market crosses into the horizon window purely by the passage of
        time (the syncer ingests up to ~2 months out), and edge can appear from
        the price drifting while our belief sits still — neither produces a belief
        update, so neither would ever wake the consumer.

        The band and horizon filters are duplicated here and in lib.signals for
        different jobs: this one keeps the sweep from fetching prices for
        thousands of hopeless markets, and evaluate() is still the single
        authority on the verdict.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                self._MARKET_SELECT
                + """
                WHERE NOT m.closed
                  AND m.current_score IS NOT NULL
                  AND (m.current_score >= %s OR m.current_score <= %s)
                  AND m.end_date IS NOT NULL
                  AND m.end_date > now()
                  AND m.end_date <= now() + make_interval(days => %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM paper_positions p
                      WHERE p.market_id = m.id AND p.status = 'open'
                  )
                """,
                (min_conviction_high, max_conviction_low, int(max_horizon_days)),
            )
            return [self._market_row(r) for r in cur.fetchall()]

    def insert_signal(
        self,
        *,
        market_id: str,
        market_title: str,
        rule: str,
        source: str,
        article_url: Optional[str],
        belief: float,
        yes_price: float,
        side: str,
        cost_basis: float,
        edge: float,
        win_prob: float,
        expected_roi: float,
        kelly: float,
        sharpe: float,
        end_date: Optional[datetime],
        horizon_days: Optional[float],
    ) -> int:
        """Record one fired signal; returns its id for the position to reference.

        Written even when a position is already open on this market — the fact
        that the filters fired is worth keeping either way, and separating intent
        from exposure is why signals and paper_positions are two tables.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals
                    (market_id, market_title, rule, source, article_url, belief,
                     yes_price, side, cost_basis, edge, win_prob, expected_roi,
                     kelly, sharpe, end_date, horizon_days)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (market_id, market_title, rule, source, article_url, belief,
                 yes_price, side, cost_basis, edge, win_prob, expected_roi,
                 kelly, sharpe, end_date, horizon_days),
            )
            return cur.fetchone()[0]

    def open_position(
        self,
        *,
        signal_id: int,
        market_id: str,
        side: str,
        entry_price: float,
        stake: float,
    ) -> bool:
        """Open a paper position. Returns False if this market already has one.

        "Already open" is a normal outcome, not an error: the notification path
        and the sweep can legitimately reach the same market, and the partial
        unique index is what decides. Catching the violation here means the
        database stays the single arbiter — no read-then-write race to lose.
        """
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_positions
                        (signal_id, market_id, side, entry_price, stake, shares)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (signal_id, market_id, side, entry_price, stake,
                     stake / entry_price),
                )
            return True
        except psycopg.errors.UniqueViolation:
            return False

    def settle_positions(self) -> list[dict]:
        """Book P&L on every open position whose market now has an outcome.

        One statement, so a crash mid-sweep cannot half-settle the book. Our side
        pays 1.0 per share if it won and 0.0 if it lost, hence
        pnl = shares * exit_price - stake.

        The CASE is repeated because SQL cannot reference a column it is assigning
        in the same UPDATE. Markets with resolved_outcome NULL or 0.5 are skipped:
        0.5 means the outcome was not determinable, the same rows
        resolved_unscored_markets excludes, so the two ledgers agree on what
        counts as resolved.
        """
        won = """
            (p.side = 'YES' AND m.resolved_outcome = 1.0)
         OR (p.side = 'NO'  AND m.resolved_outcome = 0.0)
        """
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE paper_positions p
                SET status      = 'settled',
                    closed_at   = now(),
                    exit_reason = 'resolved',
                    exit_price  = CASE WHEN {won} THEN 1.0 ELSE 0.0 END,
                    pnl         = p.shares * (CASE WHEN {won} THEN 1.0 ELSE 0.0 END)
                                  - p.stake
                FROM markets m
                WHERE m.id = p.market_id
                  AND p.status = 'open'
                  AND m.resolved_outcome IS NOT NULL
                  AND m.resolved_outcome != 0.5
                RETURNING p.market_id, p.side, p.exit_price, p.pnl
                """
            )
            return [
                {"market_id": r[0], "side": r[1], "exit_price": r[2], "pnl": r[3]}
                for r in cur.fetchall()
            ]

    def position_aggregates(self) -> dict:
        """Book-wide totals for the gauges: counts, realised P&L, wins, stake.

        Recomputed each sweep rather than tracked incrementally, so a restart or a
        hand-edited row can never leave the gauges drifting from the table.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FILTER (WHERE status = 'open'),
                       count(*) FILTER (WHERE status <> 'open'),
                       coalesce(sum(pnl), 0.0),
                       count(*) FILTER (WHERE status <> 'open' AND pnl > 0),
                       coalesce(sum(stake) FILTER (WHERE status <> 'open'), 0.0)
                FROM paper_positions
                """
            )
            open_n, settled_n, pnl_total, wins, staked = cur.fetchone()
            return {
                "open": open_n,
                "settled": settled_n,
                "pnl_total": float(pnl_total),
                "wins": wins,
                "staked": float(staked),
            }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_signals_db.py -v`

Expected: all pass (~20 tests).

Run: `pytest -q` (no DB) — expected: green, that module skipped.

- [ ] **Step 5: Commit**

```bash
git add lib/db.py tests/test_signals_db.py
git commit -m "Add signal + paper-position DB access

Settlement is one UPDATE...FROM so a crash cannot half-settle the book,
and open_position treats the unique-index violation as a normal outcome,
keeping the database the single arbiter of one-position-per-market."
```

---

### Task 5: The signal service

**Files:**
- Create: `services/signal/__init__.py` (empty), `services/signal/main.py`
- Modify: `lib/config.py` (eight `signal_*` knobs), `lib/metrics.py` (the `SIGNAL_*` collectors)
- Test: `tests/test_signal.py`, `tests/test_config_signal.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4, plus `lib.polymarket.fetch_statuses(client, ids, url=..., closed=False) -> dict[str, dict]` where each value has keys `closed, end_date, resolved_outcome, yes_price`.
- Produces:
  - `Settings.signal_min_edge, signal_min_conviction_high, signal_max_conviction_low, signal_max_horizon_days, signal_min_cost_basis, signal_max_cost_basis, signal_stake, signal_sweep_interval_seconds`.
  - `services.signal.main.thresholds(settings) -> signals.Thresholds`
  - `services.signal.main.evaluate_market(db, market: dict, yes_price: Optional[float], settings, *, source: str) -> str` — returns a rejection reason, `"fired"`, or `"position_open"`. `market` is a `market_for_signal` / `signal_candidate_markets` dict, passed directly rather than looked up by id.
  - `services.signal.main.evaluate_notified(db, client, settings, market_id: str) -> str` — same return values plus `"unknown_market"`.
  - `services.signal.main.sweep_once(db, client, settings) -> dict` with keys `settled, evaluated, fired`.
  - `services.signal.main.run(once: bool = False) -> None`

**A naming hazard worth knowing:** `services/signal/main.py` does `import signal` for `SIGINT`/`SIGTERM`. That resolves to the stdlib module, not the package, because the package's importable name is `services.signal` — nothing puts `services/` itself on `sys.path`. Don't "fix" it.

- [ ] **Step 1: Write the failing config test**

Create `tests/test_config_signal.py`:

```python
"""The signal service's knobs: defaults, and that each env var is actually read.

Defaults are load-bearing here — they are the filter, and the design doc argues
for these specific numbers — so they are asserted rather than assumed.
"""
import pytest

from lib.config import load_settings

_ENV = {
    "SIGNAL_MIN_EDGE": ("signal_min_edge", "0.09", 0.09, 0.05),
    "SIGNAL_MIN_CONVICTION_HIGH": ("signal_min_conviction_high", "0.9", 0.9, 0.80),
    "SIGNAL_MAX_CONVICTION_LOW": ("signal_max_conviction_low", "0.1", 0.1, 0.20),
    "SIGNAL_MAX_HORIZON_DAYS": ("signal_max_horizon_days", "30", 30, 14),
    "SIGNAL_MIN_COST_BASIS": ("signal_min_cost_basis", "0.3", 0.3, 0.05),
    "SIGNAL_MAX_COST_BASIS": ("signal_max_cost_basis", "0.9", 0.9, 0.95),
    "SIGNAL_STAKE": ("signal_stake", "5", 5.0, 1.0),
    "SIGNAL_SWEEP_INTERVAL_SECONDS": ("signal_sweep_interval_seconds", "60", 60, 3600),
}


@pytest.mark.parametrize("env_var", sorted(_ENV))
def test_default_when_unset(monkeypatch, env_var):
    field, _raw, _override, default = _ENV[env_var]
    monkeypatch.delenv(env_var, raising=False)
    assert getattr(load_settings(), field) == pytest.approx(default)


@pytest.mark.parametrize("env_var", sorted(_ENV))
def test_env_var_is_read(monkeypatch, env_var):
    field, raw, override, _default = _ENV[env_var]
    monkeypatch.setenv(env_var, raw)
    assert getattr(load_settings(), field) == pytest.approx(override)


def test_belief_dirty_key_default():
    assert load_settings().belief_dirty_key == "belief_dirty"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_config_signal.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'signal_min_edge'`.

- [ ] **Step 3: Add the config knobs**

In `lib/config.py`, add to the `Settings` dataclass after the scorer block:

```python
    # --- signal (belief vs live price -> paper positions) ---
    signal_min_edge: float                  # noise floor: is the disagreement real
    signal_min_conviction_high: float       # belief >= this counts as confident YES-ish
    signal_max_conviction_low: float        # belief <= this counts as confident NO-ish
    signal_max_horizon_days: float          # only bet markets resolving this soon
    signal_min_cost_basis: float            # THE risk dial: raise it to drop longshots
    signal_max_cost_basis: float            # defensive ceiling on the expensive side
    signal_stake: float                     # euros per paper position (flat, for now)
    signal_sweep_interval_seconds: int      # settle + rescan cadence
```

and to `load_settings()`:

```python
        signal_min_edge=float(os.environ.get("SIGNAL_MIN_EDGE", "0.05")),
        signal_min_conviction_high=float(
            os.environ.get("SIGNAL_MIN_CONVICTION_HIGH", "0.80")),
        signal_max_conviction_low=float(
            os.environ.get("SIGNAL_MAX_CONVICTION_LOW", "0.20")),
        signal_max_horizon_days=float(os.environ.get("SIGNAL_MAX_HORIZON_DAYS", "14")),
        # c < 0.5 means buying the underdog, so this floor is the risk/reward dial:
        # raising it trades expected ROI for hit rate. 0.05 mirrors the syncer's
        # ingestion price band — the price moves after ingest, so the gate is
        # re-applied at decision time.
        signal_min_cost_basis=float(os.environ.get("SIGNAL_MIN_COST_BASIS", "0.05")),
        signal_max_cost_basis=float(os.environ.get("SIGNAL_MAX_COST_BASIS", "0.95")),
        signal_stake=float(os.environ.get("SIGNAL_STAKE", "1.0")),
        signal_sweep_interval_seconds=int(
            os.environ.get("SIGNAL_SWEEP_INTERVAL_SECONDS", "3600")),
```

- [ ] **Step 4: Verify the config test passes**

Run: `pytest tests/test_config_signal.py -v`
Expected: 17 passed.

- [ ] **Step 5: Add the metrics**

In `lib/metrics.py`, after the scorer block:

```python
# --- signal (belief vs live price -> paper positions) ---
SIGNAL_EVALUATED = Counter(
    "signal_evaluated_total", "Markets run through the decision function",
    ["source"],
)
SIGNAL_REJECTED = Counter(
    "signal_rejected_total",
    "Candidates that failed a gate, by the first gate they failed",
    ["reason"],
)
SIGNAL_FIRED = Counter(
    "signal_fired_total", "Signals that passed every gate",
    ["side", "rule"],
)
SIGNAL_POSITIONS_OPENED = Counter(
    "signal_positions_opened_total", "Paper positions opened",
    ["side"],
)
SIGNAL_POSITIONS_SETTLED = Counter(
    "signal_positions_settled_total", "Paper positions settled against an outcome"
)
SIGNAL_POSITIONS_OPEN = Gauge(
    "signal_positions_open", "Paper positions currently open"
)
SIGNAL_PNL_TOTAL = Gauge(
    "signal_pnl_total", "Realised paper P&L in euros across all settled positions"
)
SIGNAL_WIN_RATE = Gauge(
    "signal_win_rate", "Share of settled paper positions that were profitable"
)
SIGNAL_ROI = Gauge(
    "signal_roi", "Realised paper P&L divided by total stake on settled positions"
)
SIGNAL_LAST_SWEEP_TIMESTAMP = Gauge(
    "signal_last_sweep_timestamp",
    "Unix timestamp of the last completed settle + rescan sweep",
)
```

- [ ] **Step 6: Write the failing service test**

Create `tests/test_signal.py`:

```python
"""Unit tests for the signal service's orchestration, with fakes for DB + Gamma.

The decision math is covered in tests/test_signals.py and the SQL in
tests/test_signals_db.py; what is left here is the wiring — that a fired signal
writes a row AND a position, that an already-held market writes the row but not a
position, that a rejection writes neither, and that the sweep settles before it
rescans.
"""
from datetime import datetime, timedelta, timezone

import pytest

from lib.config import load_settings
from services.signal import main as signal_service

NOW = datetime.now(timezone.utc)


def _settings(**overrides):
    import dataclasses
    return dataclasses.replace(load_settings(), **overrides)


class _FakeDb:
    def __init__(self, markets, *, open_markets=(), settle=()):
        self._markets = markets
        self._open = set(open_markets)
        self._settle = list(settle)
        self.signals = []
        self.positions = []
        self.settled_calls = 0

    def market_for_signal(self, market_id):
        return self._markets.get(market_id)

    def signal_candidate_markets(self, **_kwargs):
        return [m for mid, m in self._markets.items() if mid not in self._open]

    def insert_signal(self, **kwargs):
        self.signals.append(kwargs)
        return len(self.signals)

    def open_position(self, **kwargs):
        if kwargs["market_id"] in self._open:
            return False
        self._open.add(kwargs["market_id"])
        self.positions.append(kwargs)
        return True

    def settle_positions(self):
        self.settled_calls += 1
        out, self._settle = self._settle, []
        return out

    def position_aggregates(self):
        return {"open": len(self._open), "settled": 0, "pnl_total": 0.0,
                "wins": 0, "staked": 0.0}


def _market(mid, *, score=0.85, days_out=5.0, closed=False, article=None):
    return {
        "market_id": mid,
        "question": f"q {mid}",
        "current_score": score,
        "end_date": NOW + timedelta(days=days_out),
        "closed": closed,
        "article_url": article,
    }


def _prices(mapping):
    """Stand in for polymarket.fetch_statuses."""
    def _fetch(_client, ids, **_kwargs):
        return {i: {"yes_price": mapping.get(i), "closed": False,
                    "end_date": None, "resolved_outcome": None}
                for i in ids if i in mapping}
    return _fetch


# --- evaluate_market ----------------------------------------------------

def test_fired_signal_writes_a_row_and_a_position():
    db = _FakeDb({})   # evaluate_market takes the market directly, not by id
    outcome = signal_service.evaluate_market(
        db, _market("m1", article="https://a.example"), 0.75,
        _settings(), source="belief_update",
    )
    assert outcome == "fired"
    assert len(db.signals) == 1
    assert len(db.positions) == 1

    sig = db.signals[0]
    assert sig["side"] == "YES"
    assert sig["source"] == "belief_update"
    assert sig["rule"] == "conviction_edge"
    assert sig["article_url"] == "https://a.example"
    assert sig["cost_basis"] == pytest.approx(0.75)

    pos = db.positions[0]
    assert pos["signal_id"] == 1
    assert pos["entry_price"] == pytest.approx(0.75)
    assert pos["stake"] == pytest.approx(1.0)


def test_position_entry_price_is_the_cost_basis_not_the_yes_price():
    """A NO position is entered at 1 - yes_price, not at yes_price."""
    db = _FakeDb({})
    signal_service.evaluate_market(
        db, _market("m1", score=0.80), 0.90, _settings(), source="sweep"
    )
    assert db.signals[0]["side"] == "NO"
    assert db.positions[0]["side"] == "NO"
    assert db.positions[0]["entry_price"] == pytest.approx(0.10)


def test_already_held_market_records_the_signal_but_opens_nothing():
    db = _FakeDb({}, open_markets=["m1"])
    outcome = signal_service.evaluate_market(
        db, _market("m1"), 0.75, _settings(), source="sweep"
    )
    assert outcome == "position_open"
    assert len(db.signals) == 1     # intent is still recorded
    assert db.positions == []       # exposure is not doubled


def test_rejected_candidate_writes_nothing():
    db = _FakeDb({})
    outcome = signal_service.evaluate_market(
        db, _market("m1", score=0.55), 0.50, _settings(), source="sweep"
    )
    assert outcome == "conviction"
    assert db.signals == []
    assert db.positions == []


def test_missing_price_is_rejected_as_no_price():
    db = _FakeDb({})
    assert signal_service.evaluate_market(
        db, _market("m1"), None, _settings(), source="sweep"
    ) == "no_price"


def test_thresholds_are_taken_from_settings():
    db = _FakeDb({})
    strict = _settings(signal_min_cost_basis=0.30)
    assert signal_service.evaluate_market(
        db, _market("m1", score=0.85), 0.95, strict, source="sweep"
    ) == "cost_basis_band"
    assert signal_service.evaluate_market(
        db, _market("m1", score=0.85), 0.95, _settings(), source="sweep"
    ) == "fired"


# --- sweep_once ---------------------------------------------------------

def test_sweep_settles_then_rescans(monkeypatch):
    db = _FakeDb(
        {"m1": _market("m1"), "m2": _market("m2", score=0.55)},
        settle=[{"market_id": "m0", "side": "YES", "exit_price": 1.0, "pnl": 1.0}],
    )
    monkeypatch.setattr(signal_service.polymarket, "fetch_statuses",
                        _prices({"m1": 0.75, "m2": 0.50}))

    sweep = signal_service.sweep_once(db, object(), _settings())
    assert db.settled_calls == 1
    assert sweep["settled"] == 1
    assert sweep["evaluated"] == 2
    assert sweep["fired"] == 1
    assert [s["source"] for s in db.signals] == ["sweep"]


def test_sweep_with_no_candidates_makes_no_gamma_call(monkeypatch):
    db = _FakeDb({})
    called = []

    def _boom(*_a, **_k):
        called.append(1)
        return {}

    monkeypatch.setattr(signal_service.polymarket, "fetch_statuses", _boom)
    assert signal_service.sweep_once(db, object(), _settings())["evaluated"] == 0
    assert called == []


def test_settlement_completes_even_when_the_price_fetch_fails(monkeypatch):
    """Settlement runs first, so a Gamma outage cannot delay booking known P&L.

    sweep_once lets the error propagate; run()'s try/except logs it and the next
    cycle retries the rescan.
    """
    db = _FakeDb({"m1": _market("m1")},
                 settle=[{"market_id": "m0", "side": "YES", "exit_price": 1.0,
                          "pnl": 1.0}])

    def _raise(*_a, **_k):
        raise RuntimeError("gamma down")

    monkeypatch.setattr(signal_service.polymarket, "fetch_statuses", _raise)
    with pytest.raises(RuntimeError):
        signal_service.sweep_once(db, object(), _settings())
    assert db.settled_calls == 1
```

- [ ] **Step 7: Run it to verify it fails**

Run: `pytest tests/test_signal.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'services.signal'`.

- [ ] **Step 8: Write the service**

Create an empty `services/signal/__init__.py`.

Create `services/signal/main.py`:

```python
"""Signal — the service that turns beliefs into (paper) bets.

The scorer closed the forecasting loop: it grades our belief against the outcome.
This closes the trading loop. The worker is deliberately price-blind — the score
it maintains is our own prior, never anchored to Polymarket — so this is the only
service that ever sees a live price, and the only one that commits to a position.

  1. Take a market, read our belief from markets.current_score.
  2. Fetch the live YES price from Gamma.
  3. lib.signals.evaluate decides: which side, how big the edge, does it clear the
     gates (see lib/signals.py for the math and why min_cost_basis is the risk dial).
  4. If it fires, record a `signals` row, then open a flat-stake paper position —
     unless this market already has one open.

Two entry paths, one evaluation:

  - notification — the worker SADDs a market id to the dirty set when it moves a
    belief; we SPOP it and evaluate that one market. This is the fast path: react
    to news within seconds.
  - sweep — every signal_sweep_interval_seconds, settle any positions whose
    market resolved, then rescan every conviction-band market in the horizon. Two
    things make this necessary rather than redundant: a market crosses into the
    14-day horizon purely by time passing (the syncer ingests up to ~2 months
    out), and edge appears when the *price* drifts while our belief sits still.
    Neither produces a belief update, so neither would ever wake the fast path.

Both paths read the belief from Postgres, never from the Redis payload — with
repeat notifications collapsing into one set member, a payload's score could be
several updates stale by the time it is popped.

Singleton, and deliberately a near-clone of the scorer (periodic loop, --once
flag, metrics server, graceful shutdown). It mutates positions, so a second
instance would be racing over the same book; the partial unique index on
paper_positions would stop the worst of it, but there is nothing to gain.

Everything here is PAPER. No order is placed anywhere.

Run:
    python -m services.signal.main          # run forever
    python -m services.signal.main --once   # one settle + rescan sweep, then exit
                                            # (does not drain the dirty set)
"""
import logging
import signal as signal_module  # stdlib; aliased so `signals` stays unambiguous
import sys
import threading
import time
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

from lib import metrics, polymarket, signals
from lib.config import Settings, load_settings
from lib.db import Db
from lib.queue import DirtyMarkets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
log = logging.getLogger("signal")


def thresholds(settings: Settings) -> signals.Thresholds:
    """Lift the flat env-driven Settings into the decision function's config."""
    return signals.Thresholds(
        min_edge=settings.signal_min_edge,
        min_conviction_high=settings.signal_min_conviction_high,
        max_conviction_low=settings.signal_max_conviction_low,
        max_horizon_days=settings.signal_max_horizon_days,
        min_cost_basis=settings.signal_min_cost_basis,
        max_cost_basis=settings.signal_max_cost_basis,
    )


def evaluate_market(
    db: Db,
    market: dict,
    yes_price,
    settings: Settings,
    *,
    source: str,
) -> str:
    """Decide and act on one market. Returns the outcome for metrics/logging.

    One of: a rejection reason from lib.signals, "fired", or "position_open".

    Note the asymmetry between the two writes. A `signals` row is written
    whenever the gates pass, even on a market we already hold — the filters
    firing is worth recording either way. A position is opened only if the
    database's partial unique index allows it, which is what keeps intent
    (signals) and exposure (paper_positions) honestly separate.
    """
    metrics.SIGNAL_EVALUATED.labels(source=source).inc()
    decision = signals.evaluate(
        belief=market["current_score"],
        yes_price=yes_price,
        end_date=market["end_date"],
        now=datetime.now(timezone.utc),
        closed=market["closed"],
        thresholds=thresholds(settings),
    )

    if not decision.fired:
        metrics.SIGNAL_REJECTED.labels(reason=decision.reason).inc()
        return decision.reason

    signal_id = db.insert_signal(
        market_id=market["market_id"],
        market_title=market["question"],
        rule=signals.RULE_CONVICTION_EDGE,
        source=source,
        article_url=market["article_url"],
        belief=market["current_score"],
        yes_price=yes_price,
        side=decision.side,
        cost_basis=decision.cost_basis,
        edge=decision.edge,
        win_prob=decision.win_prob,
        expected_roi=decision.expected_roi,
        kelly=decision.kelly,
        sharpe=decision.sharpe,
        end_date=market["end_date"],
        horizon_days=decision.horizon_days,
    )
    metrics.SIGNAL_FIRED.labels(
        side=decision.side, rule=signals.RULE_CONVICTION_EDGE
    ).inc()

    # entry_price is the cost basis, NOT the YES price: a NO position is entered
    # at 1 - yes_price, and P&L is computed from what we actually paid.
    opened = db.open_position(
        signal_id=signal_id,
        market_id=market["market_id"],
        side=decision.side,
        entry_price=decision.cost_basis,
        stake=settings.signal_stake,
    )
    if not opened:
        metrics.SIGNAL_REJECTED.labels(reason="position_open").inc()
        log.info(
            "signal on %s (%s, edge %+.3f) — position already open, no entry",
            market["market_id"], decision.side, decision.edge,
        )
        return "position_open"

    metrics.SIGNAL_POSITIONS_OPENED.labels(side=decision.side).inc()
    log.info(
        "OPEN %s %s @ %.3f  belief %.2f vs price %.2f  edge %+.3f  "
        "roi %+.1f%%  kelly %.1f%%  (%s)",
        decision.side, market["market_id"], decision.cost_basis,
        market["current_score"], yes_price, decision.edge,
        100 * decision.expected_roi, 100 * decision.kelly, source,
    )
    return "fired"


def _refresh_gauges(db: Db) -> dict:
    """Recompute the book gauges from the table (never tracked incrementally)."""
    agg = db.position_aggregates()
    metrics.SIGNAL_POSITIONS_OPEN.set(agg["open"])
    metrics.SIGNAL_PNL_TOTAL.set(agg["pnl_total"])
    if agg["settled"]:
        metrics.SIGNAL_WIN_RATE.set(agg["wins"] / agg["settled"])
    if agg["staked"]:
        metrics.SIGNAL_ROI.set(agg["pnl_total"] / agg["staked"])
    return agg


def sweep_once(db: Db, client: httpx.Client, settings: Settings) -> dict:
    """Settle resolved positions, then rescan candidates. Returns counts.

    Settlement runs first and unconditionally: it needs no network, so a Gamma
    outage must not be able to delay booking P&L that is already determined.
    """
    settled = db.settle_positions()
    for s in settled:
        log.info(
            "SETTLE %s %s exit %.1f  pnl %+.2f",
            s["side"], s["market_id"], s["exit_price"], s["pnl"],
        )
    metrics.SIGNAL_POSITIONS_SETTLED.inc(len(settled))

    candidates = db.signal_candidate_markets(
        min_conviction_high=settings.signal_min_conviction_high,
        max_conviction_low=settings.signal_max_conviction_low,
        max_horizon_days=settings.signal_max_horizon_days,
    )
    fired = 0
    if candidates:
        # One chunked Gamma call for every candidate, not one per market.
        statuses = polymarket.fetch_statuses(
            client,
            [m["market_id"] for m in candidates],
            url=settings.gamma_markets_url,
        )
        for m in candidates:
            status = statuses.get(m["market_id"], {})
            outcome = evaluate_market(
                db, m, status.get("yes_price"), settings, source="sweep"
            )
            if outcome == "fired":
                fired += 1

    _refresh_gauges(db)
    metrics.SIGNAL_LAST_SWEEP_TIMESTAMP.set(time.time())
    return {"settled": len(settled), "evaluated": len(candidates), "fired": fired}


def evaluate_notified(
    db: Db, client: httpx.Client, settings: Settings, market_id: str
) -> str:
    """Evaluate one market popped off the dirty set."""
    market = db.market_for_signal(market_id)
    if market is None:
        metrics.SIGNAL_EVALUATED.labels(source="belief_update").inc()
        metrics.SIGNAL_REJECTED.labels(reason="unknown_market").inc()
        log.warning("notified about market %s, which is not in the DB", market_id)
        return "unknown_market"

    statuses = polymarket.fetch_statuses(
        client, [market_id], url=settings.gamma_markets_url
    )
    yes_price = statuses.get(market_id, {}).get("yes_price")
    return evaluate_market(db, market, yes_price, settings, source="belief_update")


def _wait_for_db(settings: Settings, attempts: int = 30) -> Db:
    for i in range(attempts):
        try:
            db = Db(settings.database_url)
            if db.ping():
                return db
        except Exception:
            pass
        log.info("waiting for postgres... (%d/%d)", i + 1, attempts)
        time.sleep(1)
    raise RuntimeError("postgres not reachable")


def run(once: bool = False) -> None:
    load_dotenv()
    settings = load_settings()

    db = _wait_for_db(settings)
    dirty = DirtyMarkets(settings.redis_url, settings.belief_dirty_key)
    metrics.start_metrics_server(settings.metrics_port)

    stop = threading.Event()
    for sig in (signal_module.SIGINT, signal_module.SIGTERM):
        signal_module.signal(sig, lambda *_: stop.set())

    log.info(
        "signal started: stake €%.2f, edge >= %.2f, conviction >= %.2f / <= %.2f, "
        "cost basis %.2f-%.2f, horizon <= %.0fd, sweep every %ds",
        settings.signal_stake, settings.signal_min_edge,
        settings.signal_min_conviction_high, settings.signal_max_conviction_low,
        settings.signal_min_cost_basis, settings.signal_max_cost_basis,
        settings.signal_max_horizon_days, settings.signal_sweep_interval_seconds,
    )

    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        last_sweep = 0.0
        while not stop.is_set():
            if time.time() - last_sweep >= settings.signal_sweep_interval_seconds:
                try:
                    c = sweep_once(db, client, settings)
                    log.info(
                        "sweep: %d settled, %d candidates evaluated, %d fired",
                        c["settled"], c["evaluated"], c["fired"],
                    )
                except Exception:
                    log.exception("sweep failed")
                last_sweep = time.time()

            if once:
                break

            try:
                market_id = dirty.pop()
                metrics.BELIEF_DIRTY_DEPTH.set(dirty.depth())
            except Exception:
                log.exception("dirty-set pop failed")
                stop.wait(timeout=5)
                continue

            if market_id is None:
                # Nothing pending. Nap, but wake immediately on shutdown — SPOP
                # does not block, so this is what keeps the loop from spinning.
                stop.wait(timeout=5)
                continue

            try:
                evaluate_notified(db, client, settings, market_id)
            except Exception:
                # The notification is already consumed and is not retried; the
                # sweep re-examines this market within the interval, so the cost
                # of dropping it is latency, not a lost signal.
                log.exception("evaluation of market %s failed", market_id)

    log.info("signal stopped")


if __name__ == "__main__":
    run(once="--once" in sys.argv)
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `pytest tests/test_signal.py -v`
Expected: all pass (~9 tests).

If `test_sweep_settles_then_rescans` fails because `_FakeDb.signal_candidate_markets` returns markets keyed differently than the fake price map, check that `_prices` is keyed on the same ids as `_market(...)`.

Run: `pytest -q`
Expected: green.

Run with a DB: `TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest -q`
Expected: green.

- [ ] **Step 10: Smoke-test one real cycle**

With the stack's Postgres up and migrations applied:

```bash
DATABASE_URL=postgresql://pm:pm@localhost:5432/pm \
REDIS_URL=redis://localhost:6379/0 \
SIGNAL_SWEEP_INTERVAL_SECONDS=1 \
python -m services.signal.main --once
```

Expected: it starts, logs the threshold line, logs `sweep: 0 settled, N candidates evaluated, M fired`, and exits 0. `N` may be 0 on an empty DB — that is a pass. It must not traceback.

- [ ] **Step 11: Commit**

```bash
git add services/signal lib/config.py lib/metrics.py tests/test_signal.py tests/test_config_signal.py
git commit -m "Add the signal service

Two entry paths into one evaluation: the worker's dirty-market set for
fast reaction, and an hourly sweep that settles resolved positions and
rescans candidates — needed because a market enters the horizon window by
time alone, and edge appears when the price drifts while belief sits still."
```

---

### Task 6: Package it — image, compose, scraping, docs

**Files:**
- Modify: `Dockerfile` (replace the placeholder comment with a `signal` stage)
- Modify: `docker-compose.yml` (replace the commented stub around line 172; add `signal` to `prometheus.depends_on`)
- Modify: `docker-compose.prod.yml` (logging + real DSN)
- Modify: `monitoring/prometheus.yml` (add `signal` to the DNS SD names)
- Modify: `.env.example` (signal block; fix the stale `BELIEF_QUEUE_KEY` line)
- Modify: `README.md` (architecture, what's built, a run section, layout, the Redis debug command)

**Interfaces:**
- Consumes: `services/signal/main.py` from Task 5.
- Produces: a runnable `signal` container in the stack. Nothing later depends on this task.

- [ ] **Step 1: Add the Dockerfile stage**

Replace the last line of `Dockerfile`:

```dockerfile
# ---- signal stage will be added here as we build it ----
```

with:

```dockerfile
# ---- signal (singleton: belief vs live price -> paper positions) ----
FROM base AS signal
COPY services/signal/ ./services/signal/
CMD ["python", "-m", "services.signal.main"]
```

- [ ] **Step 2: Replace the compose stub**

In `docker-compose.yml`, replace:

```yaml
  # signal:        # SINGLETON: mutates positions, do not scale
  #   build: { context: ., target: signal }
  #   deploy: { replicas: 1 }
```

with:

```yaml
  signal:        # SINGLETON: mutates positions, do not scale
    build:
      context: .
      target: signal
    environment:
      DATABASE_URL: postgresql://pm:pm@postgres:5432/pm
      REDIS_URL: redis://redis:6379/0
      SIGNAL_SWEEP_INTERVAL_SECONDS: ${SIGNAL_SWEEP_INTERVAL_SECONDS}
      SIGNAL_MIN_EDGE: ${SIGNAL_MIN_EDGE}
      SIGNAL_MIN_COST_BASIS: ${SIGNAL_MIN_COST_BASIS}
      SIGNAL_MAX_HORIZON_DAYS: ${SIGNAL_MAX_HORIZON_DAYS}
      SIGNAL_STAKE: ${SIGNAL_STAKE}
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
      migrate:
        condition: service_completed_successfully
    restart: unless-stopped
    # SINGLETON — it owns the position book. The partial unique index on
    # paper_positions would stop a second instance double-entering, but two
    # instances would still duplicate Gamma calls and signal rows for nothing.
    deploy:
      replicas: 1
```

Then add `- signal` to the `prometheus` service's `depends_on` list, and update that service's trailing comment to mention signal.

- [ ] **Step 3: Add the prod override**

In `docker-compose.prod.yml`, after the `scorer` block:

```yaml
  signal:
    logging: *default-logging
    environment:
      DATABASE_URL: postgresql://pm:${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}@postgres:5432/pm
```

- [ ] **Step 4: Add the scrape target**

In `monitoring/prometheus.yml`, change:

```yaml
        names: [feeder, worker, syncer, scorer]
```

to:

```yaml
        names: [feeder, worker, syncer, scorer, signal]
```

- [ ] **Step 5: Update `.env.example`**

Replace the stale line `# BELIEF_QUEUE_KEY=belief_updates` with `# BELIEF_DIRTY_KEY=belief_dirty`, and add a block before the `--- metrics ---` section:

```
# --- signal (belief vs live price -> paper positions) ---
# Needs DATABASE_URL + REDIS_URL (shared, above). Reads live prices from Gamma.
# Everything here is PAPER trading — no order is ever placed.
# Optional signal tuning (compose defaults shown; uncomment to override):
# SIGNAL_SWEEP_INTERVAL_SECONDS=3600  # settle resolved positions + rescan candidates
# SIGNAL_MIN_EDGE=0.05             # noise floor: minimum |belief - price| to act
# SIGNAL_MIN_CONVICTION_HIGH=0.80  # belief at/above this is confident enough to act
# SIGNAL_MAX_CONVICTION_LOW=0.20   # ... and at/below this
# SIGNAL_MAX_HORIZON_DAYS=14       # only bet markets resolving this soon
# SIGNAL_MIN_COST_BASIS=0.05       # THE risk dial: raise it to drop longshot bets
# SIGNAL_MAX_COST_BASIS=0.95       # defensive ceiling on the expensive side
# SIGNAL_STAKE=1.0                 # euros per paper position (flat for now)
```

Add matching entries to your local `.env` if you run the stack, since compose
references them without defaults (`${SIGNAL_MIN_EDGE}` etc. resolve to empty and
then the service falls back to its own default — acceptable, but noisy in
`docker compose config`).

- [ ] **Step 6: Update the README**

Four edits in `README.md`:

1. In the "What's built so far" section, move `signal` from "still stubbed in the compose file" to the built list.
2. In the architecture bullet list, the `signal` bullet currently says it "records intended trades". Replace with:

```markdown
- **signal** — reads the dirty-market set, fetches the live Polymarket price,
  applies the edge / conviction / cost-basis filters, records every fired signal
  and opens a flat-stake **paper** position that settles against the outcome.
  *Singleton — it owns the position book. No real order is ever placed.*
```

3. Replace the `belief_updates` Redis inspection command. The current block is:

```sh
docker compose exec redis redis-cli LRANGE belief_updates 0 0   # newest event for signal
```

Replace with:

```sh
docker compose exec redis redis-cli SMEMBERS belief_dirty       # markets awaiting the signal service
```

4. Add a run section after "Run the scorer":

```markdown
## Run the signal service

The signal service is the only one that sees a live Polymarket price. It reads
our belief from Postgres, compares it against Gamma, and opens **paper**
positions — nothing here places a real order.

```sh
cd part-3
docker compose up --build signal     # starts postgres + redis + migrate + signal
docker compose logs -f signal        # watch "OPEN NO 12345 @ 0.150 ... edge +0.100"
```

One cycle on the host (needs `DATABASE_URL` + `REDIS_URL`):

```sh
pip install -r requirements.txt
python -m services.signal.main --once   # one settle + rescan sweep, then exit
python -m services.signal.main          # run forever
```

Inspect the decisions and the book:

```sh
docker compose exec postgres psql -U pm -d pm -c \
  'SELECT market_id, side, round(belief::numeric,2) AS belief,
          round(yes_price::numeric,2) AS price, round(edge::numeric,3) AS edge,
          round(cost_basis::numeric,2) AS cost, round(expected_roi::numeric,2) AS roi,
          source
   FROM signals ORDER BY ts DESC LIMIT 10;'

docker compose exec postgres psql -U pm -d pm -c \
  'SELECT status, count(*), round(sum(pnl)::numeric, 2) AS pnl
   FROM paper_positions GROUP BY status;'
```

A signal fires only when all of these hold: our belief is at/above
`SIGNAL_MIN_CONVICTION_HIGH` or at/below `SIGNAL_MAX_CONVICTION_LOW`, the market
resolves within `SIGNAL_MAX_HORIZON_DAYS`, the edge on the side we would buy is
at least `SIGNAL_MIN_EDGE`, and that side's cost basis is inside
`[SIGNAL_MIN_COST_BASIS, SIGNAL_MAX_COST_BASIS]`.

`SIGNAL_MIN_COST_BASIS` is the risk dial. A cost basis below 0.5 means we are
buying the underdog: high expected return per euro, low hit rate, high variance.
Raising the floor trades return for hit rate. `signal_rejected_total{reason}` in
Prometheus shows which gate is doing the filtering.

Note that the conviction band does **not** pick a side — direction is
`sign(belief - price)`. A confident 0.80 belief against a market at 0.85 buys NO,
because the market is more extreme than we are. See
`docs/superpowers/specs/2026-08-15-signal-service-design.md` for the reasoning.
```

5. In the "Layout" tree, add after the `scoring.py` line:

```
  signals.py        pure edge / cost-basis / Kelly decision math
```

and after the scorer service line:

```
services/signal/    belief vs live price -> paper positions (singleton)
```

- [ ] **Step 7: Verify the stack builds and runs**

Run: `docker compose config --quiet`
Expected: no output (valid compose, all interpolations resolvable).

Run: `docker compose build signal`
Expected: builds clean.

Run: `docker compose up -d postgres redis migrate && docker compose run --rm signal python -m services.signal.main --once`
Expected: logs the threshold line and a `sweep:` line, exits 0.

Run: `pytest -q`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add Dockerfile docker-compose.yml docker-compose.prod.yml monitoring/prometheus.yml .env.example README.md
git commit -m "Wire the signal service into the stack

Image target, compose entry (singleton), Prometheus scrape target, env
documentation, and README run section."
```

---

## Verification

After Task 6, confirm the whole thing end to end:

- [ ] `pytest -q` — green with no DB.
- [ ] `TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest -q` — green with DB tests running (they should no longer skip).
- [ ] `grep -rn "BeliefQueue\|BELIEF_QUEUE_KEY\|belief_queue_key" . --include=*.py --include=*.yml --include=*.md` — no hits outside `docs/superpowers/` history.
- [ ] `docker compose up --build -d` then `docker compose logs signal` — starts, sweeps, no traceback.
- [ ] `curl -s localhost:9090/api/v1/targets | grep -c signal` — Prometheus discovered the target (or check the Targets page).
- [ ] Force one real signal: pick an open market inside the horizon, set its belief so an edge exists, and run a sweep.

```sh
docker compose exec postgres psql -U pm -d pm -c \
  "SELECT id, question, current_score, end_date FROM markets
   WHERE NOT closed AND end_date < now() + interval '14 days' LIMIT 5;"
# then, with a real id and a belief far from its price:
docker compose exec postgres psql -U pm -d pm -c \
  "UPDATE markets SET current_score = 0.95 WHERE id = '<id>';"
docker compose restart signal && docker compose logs -f signal
```

Expected: an `OPEN ...` line, one `signals` row, one `paper_positions` row.
Afterwards, reset that belief so you have not poisoned the forecast record:
`UPDATE markets SET current_score = <original> WHERE id = '<id>';` and delete the
test position and signal row.

## Not in this plan

Deliberately deferred, per the spec's *Future work*:

- Early exit on a profit target — the schema (`closed_early`, `exit_reason`) and
  the sweep loop already accommodate it; no migration will be needed.
- The `market_overconfidence` entry rule (market extreme, belief moderate) — the
  `rule` column exists so it becomes a sibling of `conviction_edge`.
- Position sizing — `expected_roi`, `kelly`, `win_prob` and `cost_basis` are
  stored on every signal so a sizing rule can be fitted against realised P&L.
- Grafana paper-trading panels.
