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
            "claude-polymarket-engineer/0.1 (+https://github.com/zan1zibert/claude-polymarket-engineer)",
        ),
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
    )
