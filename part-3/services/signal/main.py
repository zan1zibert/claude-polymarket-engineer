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
