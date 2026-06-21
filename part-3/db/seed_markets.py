"""Dev-only fixture loader.

Real market ingestion (fetch from Gamma, embed, upsert) is a separate component
and out of scope. This script just inserts a couple of markets with real Voyage
embeddings so the worker can be exercised end-to-end locally.

Run (needs DATABASE_URL + VOYAGE_API_KEY):
    python -m db.seed_markets
"""
import voyageai
from dotenv import load_dotenv

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector

from lib.config import load_settings

# (id, question, description) — embedded on the "document" side to match the
# worker's "query"-side article embeddings.
MARKETS = [
    (
        "seed-fed-cut",
        "Will the Federal Reserve cut interest rates at its next meeting?",
        "Resolves YES if the FOMC lowers the target federal funds rate at its "
        "next scheduled meeting.",
    ),
    (
        "seed-btc-100k",
        "Will Bitcoin trade above $100,000 before the end of the year?",
        "Resolves YES if the BTC/USD spot price exceeds $100,000 at any point "
        "this calendar year.",
    ),
]


def main() -> None:
    load_dotenv()
    settings = load_settings()

    client = voyageai.Client(api_key=settings.voyage_api_key)
    texts = [f"{q}\n{d}" for _, q, d in MARKETS]
    embeddings = client.embed(
        texts,
        model=settings.voyage_model,
        input_type="document",
        output_dimension=settings.embedding_dim,
    ).embeddings

    conn = psycopg.connect(settings.database_url, autocommit=True)
    register_vector(conn)
    with conn.cursor() as cur:
        for (mid, question, description), emb in zip(MARKETS, embeddings):
            cur.execute(
                """
                INSERT INTO markets (id, question, description, embedding)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                    SET question = EXCLUDED.question,
                        description = EXCLUDED.description,
                        embedding = EXCLUDED.embedding
                """,
                (mid, question, description, Vector(emb)),
            )
    print(f"seeded {len(MARKETS)} markets")


if __name__ == "__main__":
    main()
