"""Voyage embeddings — the one place that owns the model id and dimension.

The worker embeds an incoming article and asks pgvector for the nearest markets.
Voyage distinguishes the two sides of a retrieval pair via `input_type`:
  - markets are embedded as "document" at ingest time (the syncer),
  - the article is the "query" we search with (the worker).
Matching the types is what makes the cosine distances meaningful.
"""
import voyageai

from lib.metrics import VOYAGE_EMBEDDING_TOKENS


class Embedder:
    # Voyage caps input length; we stay well under it and trust the API to
    # truncate. Articles are already <= ~1KB of summary + a title.
    _MAX_CHARS = 8000

    def __init__(self, api_key: str, model: str, dim: int):
        self._client = voyageai.Client(api_key=api_key)
        self._model = model
        self._dim = dim

    def embed_query(self, text: str) -> list[float]:
        result = self._client.embed(
            [text[: self._MAX_CHARS]],
            model=self._model,
            input_type="query",
            truncation=True,
            output_dimension=self._dim,
        )
        VOYAGE_EMBEDDING_TOKENS.labels(operation="query").inc(result.total_tokens)
        return result.embeddings[0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed markets (the "document" side) in one batched call.

        Used by the syncer at ingest. Returns one vector per input, in order;
        an empty input list short-circuits so we never make an empty API call.
        """
        if not texts:
            return []
        result = self._client.embed(
            [t[: self._MAX_CHARS] for t in texts],
            model=self._model,
            input_type="document",
            truncation=True,
            output_dimension=self._dim,
        )
        VOYAGE_EMBEDDING_TOKENS.labels(operation="document").inc(result.total_tokens)
        return result.embeddings
