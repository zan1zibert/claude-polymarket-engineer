"""Prometheus metrics — one place that owns every counter/gauge/histogram.

Each service imports the metrics it touches and bumps them at the relevant point
in its loop; `start_metrics_server` exposes them over HTTP for Prometheus to
scrape. The objects are module-level singletons living in the default registry,
which is exactly right here: every service is a single process per container, so
no multiprocess mode is needed (a scaled worker is scraped per-replica and summed
in PromQL).

Prometheus attaches `job`/`instance` labels per scrape target, so metric names do
NOT carry a service label — `feeder_*`, `worker_*`, `syncer_*` prefixes keep them
readable, and the shared token counters (Voyage/Claude) are distinguished by the
scraping job automatically.
"""
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# --- feeder ---
FEEDER_RSS_FEEDS = Gauge(
    "feeder_rss_feeds", "Number of RSS feeds in the registry"
)
FEEDER_MARKET_QUERY_FEEDS = Gauge(
    "feeder_market_query_feeds",
    "Number of per-market Google News query feeds active after the last dynamic refresh",
)
FEEDER_POLL_CYCLES = Counter(
    "feeder_poll_cycles_total", "Completed feeder poll cycles"
)
FEEDER_ARTICLES_FETCHED = Counter(
    "feeder_articles_fetched_total",
    "Articles parsed off a feed this poll, before dedup/freshness gates",
    ["source"],
)
FEEDER_ARTICLES_PUSHED = Counter(
    "feeder_articles_pushed_total",
    "Articles pushed onto the news queue (survived dedup + freshness)",
    ["source"],
)
NEWS_QUEUE_DEPTH = Gauge(
    "news_queue_depth", "Current depth of the news queue (LLEN)"
)

# --- worker ---
WORKER_ARTICLES_PROCESSED = Counter(
    "worker_articles_processed_total", "Articles dequeued and processed by the worker",
    ["source"],
)
WORKER_ARTICLES_SKIPPED = Counter(
    "worker_articles_skipped_total",
    "Articles with no top_k_markets candidates, or none that passed the Groq relevance check",
    ["source"],
)
WORKER_MARKETS_MATCHED = Counter(
    "worker_markets_matched_total", "Markets that passed the Groq relevance check",
    ["source"],
)
WORKER_MARKETS_REEVALUATED = Counter(
    "worker_markets_reevaluated_total", "Markets successfully re-evaluated by Claude",
    ["source"],
)
WORKER_REEVAL_FAILURES = Counter(
    "worker_reeval_failures_total", "Claude re-evaluations that errored or failed to parse",
    ["source"],
)
WORKER_BELIEF_UPDATES = Counter(
    "worker_belief_updates_total", "Belief updates produced and pushed downstream",
    ["source"],
)
WORKER_BELIEF_MOVED = Counter(
    "worker_belief_moved_total",
    "Re-evaluations that moved the belief by at least belief_move_epsilon "
    "(the signal, as opposed to re-evals that left the score flat)",
    ["source"],
)
WORKER_GROQ_RELEVANT = Counter(
    "worker_groq_relevant_total",
    "Candidates the Groq relevance check accepted (proceeded to Claude)",
    ["source"],
)
WORKER_GROQ_REJECTED = Counter(
    "worker_groq_rejected_total",
    "Candidates the Groq relevance check rejected as not relevant",
    ["source"],
)
WORKER_GROQ_FAILURES = Counter(
    "worker_groq_failures_total",
    "Candidates dropped because the Groq relevance check errored or failed to parse "
    "(counted separately from worker_groq_rejected_total so outages are visible)",
    ["source"],
)
CLAUDE_REEVAL_DURATION = Histogram(
    "claude_reeval_duration_seconds", "Wall-clock duration of one Claude re-evaluation call"
)

# --- shared external-API token usage ---
# `type` = input|output for Claude and Groq; `operation` = query|document for Voyage.
CLAUDE_TOKENS = Counter(
    "claude_tokens_total", "Claude tokens consumed", ["type"]
)
GROQ_TOKENS = Counter(
    "groq_tokens_total", "Groq tokens consumed", ["type"]
)
VOYAGE_EMBEDDING_TOKENS = Counter(
    "voyage_embedding_tokens_total", "Voyage embedding tokens consumed", ["operation"]
)

# --- syncer ---
SYNCER_MARKETS_FETCHED = Counter(
    "syncer_markets_fetched_total", "Markets fetched from Gamma per sync cycle"
)
SYNCER_MARKETS_INSERTED = Counter(
    "syncer_markets_inserted_total", "New markets inserted into the DB"
)
SYNCER_MARKETS_RESOLVED = Counter(
    "syncer_markets_resolved_total", "Markets marked resolved"
)
SYNCER_OUTCOMES_BACKFILLED = Counter(
    "syncer_outcomes_backfilled_total",
    "Awaiting-outcome markets whose outcome was backfilled once Gamma settled them",
)
SYNCER_PRICES_RECORDED = Counter(
    "syncer_prices_recorded_total", "Price-series observations written (changed prices only)"
)
# Corpus state — gauges the syncer OVERWRITES each cycle from a COUNT (a level,
# not a rate), mirroring the scorer's forecast_* gauges. These answer "how big is
# the live market set right now", which the syncer_*_total counters (flows) can't.
SYNCER_OPEN_MARKETS = Gauge(
    "syncer_open_markets", "Markets currently open (NOT closed) — the retrieval corpus"
)
SYNCER_CLOSED_MARKETS = Gauge(
    "syncer_closed_markets", "Markets marked resolved/closed (retained for scoring)"
)
SYNCER_AWAITING_OUTCOME = Gauge(
    "syncer_awaiting_outcome",
    "Closed markets with no resolved_outcome — resolved but not yet gradeable. "
    "The syncer re-checks these each cycle and backfills once Gamma settles them "
    "(see syncer_outcomes_backfilled_total), so sustained growth means Polymarket "
    "is slow to settle, not a leak",
)
# Unix timestamp of the last successful sync cycle. Panel it as `time() - metric`
# to get a staleness gauge — the one alarm the rate panels can't raise, since a
# dead syncer and a quiet one both read as zero throughput.
SYNCER_LAST_SYNC_TIMESTAMP = Gauge(
    "syncer_last_sync_timestamp_seconds", "Unix time of the last completed sync cycle"
)

# --- scorer ---
# One monotonic counter for the event (markets graded), and a set of gauges that
# the scorer OVERWRITES each cycle from aggregate SQL. Gauges (not counters)
# because these are a snapshot of the whole scored corpus — a mean, not a rate.
SCORER_MARKETS_SCORED = Counter(
    "scorer_markets_scored_total", "Resolved markets graded and written to forecast_scores"
)
FORECAST_BRIER_MEAN = Gauge(
    "forecast_brier_mean", "Mean Brier score of our belief over all scored markets"
)
FORECAST_LOGLOSS_MEAN = Gauge(
    "forecast_logloss_mean", "Mean log loss of our belief over all scored markets"
)
FORECAST_BRIER_BASELINE_MEAN = Gauge(
    "forecast_brier_baseline_mean", "Mean Brier score of the market-at-ingest baseline"
)
FORECAST_LOGLOSS_BASELINE_MEAN = Gauge(
    "forecast_logloss_baseline_mean", "Mean log loss of the market-at-ingest baseline"
)
FORECAST_BRIER_SKILL = Gauge(
    "forecast_brier_skill",
    "Brier skill vs the market baseline (1 - belief/baseline; >0 = beating the market)",
)
FORECAST_SCORED_MARKETS = Gauge(
    "forecast_scored_markets", "Total markets graded so far (rows in forecast_scores)"
)
# Unix timestamp of the last completed scorer cycle, whether or not it graded
# any markets — scorer_markets_scored_total's rate is near-zero most of the
# time by design (markets resolve every few days, not every cycle), so it
# can't tell a hung scorer from a quiet one. Panel it as `time() - metric` to
# get a staleness gauge, same pattern as SYNCER_LAST_SYNC_TIMESTAMP above.
SCORER_LAST_RUN_TIMESTAMP = Gauge(
    "scorer_last_run_timestamp_seconds", "Unix time of the last completed scorer cycle"
)


def start_metrics_server(port: int) -> None:
    """Expose /metrics on `port` in a background daemon thread.

    Safe to call once at service startup — works for the async feeder and the
    sync worker/syncer alike, since the server runs in its own thread.
    """
    start_http_server(port)
