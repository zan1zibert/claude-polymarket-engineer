"""Scorer — the service that closes the loop.

The worker mutates our belief (markets.current_score) as news arrives but never
learns whether it was right. Once the syncer marks a market resolved and records
its outcome (markets.resolved_outcome), this service grades the market:

  1. Pull the resolved-but-unscored markets (db.resolved_unscored_markets).
  2. For each, compute Brier + log loss for our final belief AND for the market's
     price at ingest (seed_price, the baseline), against the 0/1 outcome.
  3. Write one forecast_scores row (db.insert_score — idempotent per market).
  4. Refresh the corpus gauges, headline being forecast_brier_skill:
     1 - mean_brier_belief / mean_brier_baseline. >0 means we're beating the
     market; that's the number the whole project is judged on.

The scoring math lives in lib/scoring.py (pure, dependency-free), so the same
functions run here and in the future offline backtest — the live scoreboard and a
replay can't drift apart.

Singleton, and deliberately a near-clone of the syncer (periodic loop, --once
flag, metrics server, graceful shutdown): scoring is cheap, idempotent, and
order-independent, so there is nothing to gain from a second instance.

Run:
    python -m services.scorer.main          # score forever
    python -m services.scorer.main --once    # one cycle then exit (handy for dev)
"""
import logging
import signal
import sys
import threading
import time

from dotenv import load_dotenv

from lib import metrics, scoring
from lib.config import Settings, load_settings
from lib.db import Db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
log = logging.getLogger("scorer")


def score_once(db: Db) -> dict:
    """Grade every resolved-but-unscored market. Returns counts + fresh gauges."""
    pending = db.resolved_unscored_markets()

    scored = 0
    for m in pending:
        outcome = m["outcome"]
        belief = m["final_belief"]
        seed = m["seed_price"]

        # Our belief is always gradeable (the query guarantees it's non-NULL).
        brier_belief = scoring.brier(belief, outcome)
        logloss_belief = scoring.log_loss(belief, outcome)

        # The baseline only exists if we captured the ingest price; keep the
        # market in the scoreboard either way, leaving the baseline columns NULL.
        if seed is None:
            brier_baseline = logloss_baseline = None
        else:
            brier_baseline = scoring.brier(seed, outcome)
            logloss_baseline = scoring.log_loss(seed, outcome)

        inserted = db.insert_score(
            m["market_id"],
            outcome=outcome,
            final_belief=belief,
            seed_price=seed,
            n_updates=m["n_updates"],
            brier_belief=brier_belief,
            logloss_belief=logloss_belief,
            brier_baseline=brier_baseline,
            logloss_baseline=logloss_baseline,
        )
        if inserted:
            scored += 1

    agg = _refresh_gauges(db)
    return {"pending": len(pending), "scored": scored, "skill": agg["skill"]}


def _refresh_gauges(db: Db) -> dict:
    """Recompute the corpus-wide gauges from aggregate SQL. Returns the skill score."""
    a = db.score_aggregates()
    skill = scoring.skill_score(a["brier_belief"], a["brier_baseline"])

    # Prometheus gauges can't hold NULL; leave a gauge untouched when its
    # aggregate is undefined (no rows yet, or no baseline) rather than forcing 0,
    # which would read as a real value on the dashboard.
    def _set(gauge, value):
        if value is not None:
            gauge.set(value)

    _set(metrics.FORECAST_BRIER_MEAN, a["brier_belief"])
    _set(metrics.FORECAST_LOGLOSS_MEAN, a["logloss_belief"])
    _set(metrics.FORECAST_BRIER_BASELINE_MEAN, a["brier_baseline"])
    _set(metrics.FORECAST_LOGLOSS_BASELINE_MEAN, a["logloss_baseline"])
    _set(metrics.FORECAST_BRIER_SKILL, skill)
    metrics.FORECAST_SCORED_MARKETS.set(a["count"] or 0)
    return {"skill": skill}


def _wait_for_db(settings: Settings, attempts: int = 30) -> Db:
    """Retry connecting to Postgres until it's accepting connections."""
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
    metrics.start_metrics_server(settings.metrics_port)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("scorer started: every %ds", settings.scorer_interval_seconds)

    while not stop.is_set():
        try:
            c = score_once(db)
            metrics.SCORER_MARKETS_SCORED.inc(c["scored"])
            metrics.SCORER_LAST_RUN_TIMESTAMP.set(time.time())
            skill = "n/a" if c["skill"] is None else f"{c['skill']:+.3f}"
            log.info(
                "scored: %d newly graded (%d pending), brier skill vs market %s",
                c["scored"], c["pending"], skill,
            )
        except Exception:
            log.exception("score cycle failed")

        if once:
            break

        # Sleep for the interval, but wake immediately on shutdown signal.
        stop.wait(timeout=settings.scorer_interval_seconds)

    log.info("scorer stopped")


if __name__ == "__main__":
    run(once="--once" in sys.argv)
