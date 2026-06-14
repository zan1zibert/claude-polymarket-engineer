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
    )
