"""Voyage embeddings — the one place that owns the model id and dimension.

The worker embeds an incoming article and asks pgvector for the nearest markets.
Voyage distinguishes the two sides of a retrieval pair via `input_type`:
  - markets are embedded as "document" at ingest time (out of scope here),
  - the article is the "query" we search with.
Matching the types is what makes the cosine distances meaningful.
"""
import voyageai


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
        return result.embeddings[0]
