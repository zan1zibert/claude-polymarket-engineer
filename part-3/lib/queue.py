"""Thin Redis-list queue wrappers.

Two hops in the pipeline, both Redis lists (the simplest durable queue we need):
  - NewsQueue   : feeder LPUSHes articles; the worker BRPOPs them.
  - BeliefQueue : the worker LPUSHes belief updates for the signal service.
"""
import redis

from lib.schemas import Article, BeliefUpdate


def _client(redis_url: str) -> "redis.Redis":
    """Shared client config. `socket_keepalive` + `health_check_interval` keep an
    idle connection alive (the worker's blocking pop can sit idle for minutes),
    and `retry_on_timeout` rides out a blip instead of surfacing it.
    """
    return redis.from_url(
        redis_url,
        decode_responses=True,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )


class NewsQueue:
    def __init__(self, redis_url: str, queue_key: str):
        self._r = _client(redis_url)
        self._key = queue_key

    def push(self, article: Article) -> None:
        self._r.lpush(self._key, article.to_json())

    def pop(self, timeout: int = 5):
        """Blocking pop of the oldest article, or None if `timeout` seconds pass.

        BRPOP pairs with the feeder's LPUSH for FIFO order. The timeout lets the
        worker loop wake periodically to check for a shutdown signal.

        A client-side socket read timeout on an empty queue is expected, not an
        error: BRPOP blocks server-side for `timeout`s, and if the client's socket
        timeout fires first (or races the nil reply) we treat it exactly like an
        empty queue — return None and let the caller loop. Only genuine outages
        raise ConnectionError, which still propagates.
        """
        try:
            item = self._r.brpop(self._key, timeout=timeout)
        except redis.exceptions.TimeoutError:
            return None
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
        self._r = _client(redis_url)
        self._key = queue_key

    def push(self, update: BeliefUpdate) -> None:
        self._r.lpush(self._key, update.to_json())

    def depth(self) -> int:
        return self._r.llen(self._key)
