"""Thin Redis queue wrappers.

Two hops in the pipeline:
  - NewsQueue    : feeder LPUSHes articles; the worker BRPOPs them (a list, because
                   order and at-least-once delivery of distinct articles matter).
  - DirtyMarkets : the worker SADDs market ids for the signal service to SPOP (a
                   set, because repeat notifications for one market should
                   collapse — see the class docstring).
"""
from typing import Optional

import redis

from lib.schemas import Article


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
