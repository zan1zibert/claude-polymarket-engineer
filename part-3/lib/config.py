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
    belief_queue_key: str        # second Redis list: worker -> signal
    anthropic_model: str
    voyage_api_key: str
    voyage_model: str
    embedding_dim: int
    top_k: int                   # candidate markets retrieved per article
    max_cosine_distance: float   # relevance gate; matches beyond this are dropped
    audit_log_path: str          # append-only JSONL of every belief update
    worker_use_web_search: bool  # worker already has the article; off by default

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
        belief_queue_key=os.environ.get("BELIEF_QUEUE_KEY", "belief_updates"),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        voyage_api_key=os.environ.get("VOYAGE_API_KEY", ""),
        voyage_model=os.environ.get("VOYAGE_MODEL", "voyage-3.5"),
        embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1024")),
        top_k=int(os.environ.get("TOP_K", "5")),
        max_cosine_distance=float(os.environ.get("MAX_COSINE_DISTANCE", "0.35")),
        audit_log_path=os.environ.get("AUDIT_LOG_PATH", "outputs/belief_updates.jsonl"),
        worker_use_web_search=os.environ.get("WORKER_USE_WEB_SEARCH", "false").lower()
        in ("1", "true", "yes"),
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
    )
