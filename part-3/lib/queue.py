"""Thin Redis-list queue wrappers.

Two hops in the pipeline, both Redis lists (the simplest durable queue we need):
  - NewsQueue   : feeder LPUSHes articles; the worker BRPOPs them.
  - BeliefQueue : the worker LPUSHes belief updates for the signal service.
"""
import redis

from lib.schemas import Article, BeliefUpdate


class NewsQueue:
    def __init__(self, redis_url: str, queue_key: str):
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._key = queue_key

    def push(self, article: Article) -> None:
        self._r.lpush(self._key, article.to_json())

    def pop(self, timeout: int = 5):
        """Blocking pop of the oldest article, or None if `timeout` seconds pass.

        BRPOP pairs with the feeder's LPUSH for FIFO order. The timeout lets the
        worker loop wake periodically to check for a shutdown signal.
        """
        item = self._r.brpop(self._key, timeout=timeout)
        if item is None:
            return None
        _key, raw = item
        return Article.from_json(raw)

    def depth(self) -> int:
        return self._r.llen(self._key)

    def ping(self) -> bool:
        return bool(self._r.ping())


class BeliefQueue:
    def __init__(self, redis_url: str, queue_key: str):
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._key = queue_key

    def push(self, update: BeliefUpdate) -> None:
        self._r.lpush(self._key, update.to_json())

    def depth(self) -> int:
        return self._r.llen(self._key)
