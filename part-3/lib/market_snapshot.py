"""Redis-backed snapshot of the open-market set: (id, question) pairs.

The syncer (the only writer of the market set) publishes the full open-market
list to one key each cycle; the feeder reads it to build per-market query feeds.
A full overwrite means closed markets simply drop out — there is no per-market
delete to get wrong, mirroring the DB's `WHERE NOT closed` semantics.
"""
import json
import logging

import redis

log = logging.getLogger("market_snapshot")


class MarketSnapshot:
    def __init__(self, redis_url: str, key: str):
        self._r = redis.from_url(redis_url, decode_responses=True)
        self._key = key

    @classmethod
    def from_client(cls, client, key: str) -> "MarketSnapshot":
        self = cls.__new__(cls)
        self._r = client
        self._key = key
        return self

    def publish(self, markets: list[tuple[str, str]]) -> None:
        """Overwrite the snapshot with the current open-market set."""
        self._r.set(self._key, json.dumps([list(m) for m in markets]))

    def read(self) -> list[tuple[str, str]]:
        """Current snapshot, or [] if missing/empty/unparseable."""
        raw = self._r.get(self._key)
        if not raw:
            return []
        try:
            return [(str(i), str(q)) for i, q in json.loads(raw)]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning("unparseable market snapshot: %s", exc)
            return []
