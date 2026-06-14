"""Shared data models that travel through the queue.

Keeping the queue payload schema in one place means the feeder (producer) and
the worker (consumer) can't drift apart.
"""
import json
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Article:
    url: str
    title: str
    summary: str
    source: str          # feed name, e.g. "BBC World"
    category: str        # e.g. "crypto"
    published_at: Optional[str]   # ISO-8601 UTC, may be None if the feed omits it
    fetched_at: str               # ISO-8601 UTC, when the feeder saw it

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str) -> "Article":
        return Article(**json.loads(raw))
