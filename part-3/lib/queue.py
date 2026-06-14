"""Thin Redis-list queue wrapper.

The feeder pushes (LPUSH); the worker will pop (BRPOP) from the same key.
A Redis list is the simplest durable queue and is all we need locally.
"""
import redis

from lib.schemas import Article


class NewsQueue:
    def __init__(self, redis_url: str, queue_key: str):
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._key = queue_key

    def push(self, article: Article) -> None:
        self._r.lpush(self._key, article.to_json())

    def depth(self) -> int:
        return self._r.llen(self._key)

    def ping(self) -> bool:
        return bool(self._r.ping())
