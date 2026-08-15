"""Shared configuration loaded from environment variables.

Every service imports its settings from here so there is one place that
documents the knobs. Values are read once at startup.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    redis_url: str
    queue_key: str
    poll_interval_seconds: int
    freshness_window_minutes: int
    dedup_ttl_seconds: int
    http_timeout_seconds: float
    user_agent: str
    metrics_port: int            # port each service exposes /metrics on (Prometheus scrape)

    # --- worker (market analyzer) ---
    database_url: str
    belief_dirty_key: str        # Redis SET of market ids: worker -> signal
    anthropic_model: str
    anthropic_max_tokens: int
    voyage_api_key: str
    voyage_model: str
    embedding_dim: int
    top_k: int                   # candidate markets retrieved per article
    groq_model: str               # model used for the per-candidate relevance check
    audit_log_path: str          # append-only JSONL of every belief update
    worker_use_web_search: bool  # worker already has the article; off by default
    belief_move_epsilon: float   # min |new-prev| score move to count a re-eval as "moving" the belief

    # --- market-syncer (ingestion) ---
    gamma_markets_url: str
    sync_interval_seconds: int       # how often to re-sync the market set
    resolution_window_days: int      # only ingest markets resolving within this
    sync_fetch_limit: int            # max markets pulled from Gamma per cycle
    sync_tag_filter: int             # filter markets by tag_id
    sync_min_volume_24h: float       # ingestion liquidity/volume gates
    sync_min_liquidity: float
    sync_price_band: tuple[float, float]  # drop near-resolved (0/1) markets
    price_change_epsilon: float      # min YES-price move to record a new price-series point

    # --- scorer (grades resolved markets) ---
    scorer_interval_seconds: int     # how often to grade newly-resolved markets

    # --- signal (belief vs live price -> paper positions) ---
    signal_min_edge: float                  # noise floor: is the disagreement real
    signal_min_conviction_high: float       # belief >= this counts as confident YES-ish
    signal_max_conviction_low: float        # belief <= this counts as confident NO-ish
    signal_max_horizon_days: float          # only bet markets resolving this soon
    signal_min_cost_basis: float            # THE risk dial: raise it to drop longshots
    signal_max_cost_basis: float            # defensive ceiling on the expensive side
    signal_stake: float                     # euros per paper position (flat, for now)
    signal_sweep_interval_seconds: int      # settle + rescan cadence

    # --- dynamic market feeds (feeder second loop) ---
    market_feed_poll_interval_seconds: int   # dynamic-loop interval
    market_feed_snapshot_key: str            # Redis key: syncer writes, feeder reads
    market_feed_max_concurrency: int         # cap on in-flight dynamic fetches
    market_feed_freshness_window_minutes: int  # freshness cutoff for dynamic articles


def load_settings() -> Settings:
    return Settings(
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        queue_key=os.environ.get("NEWS_QUEUE_KEY", "news_queue"),
        poll_interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        freshness_window_minutes=int(os.environ.get("FRESHNESS_WINDOW_MINUTES", "30")),
        # how long a URL stays "seen" before it could be re-enqueued (default 7 days)
        dedup_ttl_seconds=int(os.environ.get("DEDUP_TTL_SECONDS", str(7 * 24 * 3600))),
        http_timeout_seconds=float(os.environ.get("HTTP_TIMEOUT_SECONDS", "15")),
        user_agent=os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        ),
        metrics_port=int(os.environ.get("METRICS_PORT", "8000")),
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://pm:pm@localhost:5432/pm"
        ),
        # Renamed from BELIEF_QUEUE_KEY / `belief_updates`: the key now holds a
        # SET, and a set cannot share a key with the old list (Redis raises
        # WRONGTYPE). Renaming makes the stale, never-consumed list inert instead
        # of fatal, so no manual flush is needed before first run.
        belief_dirty_key=os.environ.get("BELIEF_DIRTY_KEY", "belief_dirty"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        anthropic_max_tokens=int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8192")),
        voyage_api_key=os.environ.get("VOYAGE_API_KEY", ""),
        voyage_model=os.environ.get("VOYAGE_MODEL", "voyage-3.5"),
        embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1024")),
        top_k=int(os.environ.get("TOP_K", "10")),
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"),
        audit_log_path=os.environ.get("AUDIT_LOG_PATH", "outputs/belief_updates.jsonl"),
        worker_use_web_search=os.environ.get("WORKER_USE_WEB_SEARCH", "false").lower()
        in ("1", "true", "yes"),
        belief_move_epsilon=float(os.environ.get("BELIEF_MOVE_EPSILON", "0.02")),
        gamma_markets_url=os.environ.get(
            "GAMMA_MARKETS_URL", "https://gamma-api.polymarket.com/markets"
        ),
        sync_interval_seconds=int(os.environ.get("SYNC_INTERVAL_SECONDS", "86400")),
        resolution_window_days=int(os.environ.get("RESOLUTION_WINDOW_DAYS", "7")),
        sync_fetch_limit=int(os.environ.get("SYNC_FETCH_LIMIT", "500")),
        sync_tag_filter=int(os.environ.get("SYNC_TAG_FILTER", "2")), # Politics
        sync_min_volume_24h=float(os.environ.get("SYNC_MIN_VOLUME_24H", "5000")),
        sync_min_liquidity=float(os.environ.get("SYNC_MIN_LIQUIDITY", "10000")),
        sync_price_band=(
            float(os.environ.get("SYNC_PRICE_BAND_LOW", "0.05")),
            float(os.environ.get("SYNC_PRICE_BAND_HIGH", "0.95")),
        ),
        price_change_epsilon=float(os.environ.get("PRICE_CHANGE_EPSILON", "0.005")),
        # Hourly by default: markets resolve on the order of days, and scoring is
        # cheap and idempotent, so a tight loop just re-checks an empty work queue.
        scorer_interval_seconds=int(os.environ.get("SCORER_INTERVAL_SECONDS", "3600")),
        market_feed_poll_interval_seconds=int(
            os.environ.get("MARKET_FEED_POLL_INTERVAL_SECONDS", "900")),
        market_feed_snapshot_key=os.environ.get(
            "MARKET_FEED_SNAPSHOT_KEY", "market_feed_snapshot"),
        market_feed_max_concurrency=int(
            os.environ.get("MARKET_FEED_MAX_CONCURRENCY", "8")),
        market_feed_freshness_window_minutes=int(
            os.environ.get("MARKET_FEED_FRESHNESS_WINDOW_MINUTES", "180")),
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
    )
