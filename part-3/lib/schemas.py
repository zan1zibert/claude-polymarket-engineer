"""Shared data models that travel through the queues / DB.

Keeping payload schemas in one place means producers and consumers can't drift
apart: the feeder and worker agree on `Article`; the worker and signal agree on
`BeliefUpdate`.
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


@dataclass
class Market:
    """A row from the `markets` table (without the embedding vector)."""
    id: str
    question: str
    description: str
    current_score: Optional[float]   # our prior belief, None until first eval


@dataclass
class BeliefUpdate:
    """One re-evaluation: the worker's output contract for the signal service.

    Mirrors a `belief_updates` row. `previous_score` is None on the first eval.
    """
    timestamp: str                   # ISO-8601 UTC
    market_id: str
    market_title: str
    previous_score: Optional[float]
    new_score: float
    article_url: str
    reasoning: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str) -> "BeliefUpdate":
        return BeliefUpdate(**json.loads(raw))
