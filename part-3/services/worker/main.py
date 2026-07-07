"""Market analyzer — the worker (consumer).

For each article on the news queue:
  1. Embed it (Voyage).
  2. Retrieve the top-k nearest markets from pgvector, dropping anything beyond
     the relevance gate (an off-topic article matches nothing and is skipped).
  3. For each matched market, ask Claude — price-blind — to update our prior in
     light of the news.
  4. Atomically swap the stored score and append a belief_updates row.
  5. Fan the transition out to the belief_updates queue (for the signal service)
     and to an append-only audit log.

SCALABLE: run several of these (docker compose up --scale worker=3). They share
the queue via BRPOP and the per-market row lock keeps concurrent score swaps honest.

Delivery is at-most-once: BRPOP removes the article immediately, so a crash
mid-article drops it. That mirrors the feeder's "dumb, cheap restart" philosophy;
a reliable variant (BRPOPLPUSH into a processing list) is a future improvement.

Run:
    python -m services.worker.main
"""
import json
import logging
import os
import signal
import time

from dotenv import load_dotenv

from lib.claude import reevaluate
from lib.config import Settings, load_settings
from lib.db import Db
from lib.embeddings import Embedder
from lib import metrics
from lib.queue import BeliefQueue, NewsQueue
from lib.schemas import Article, BeliefUpdate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
log = logging.getLogger("worker")

POP_TIMEOUT_SECONDS = 5  # how often the blocking pop wakes to check for shutdown


def _wait_for(name: str, ping, attempts: int = 30) -> None:
    for i in range(attempts):
        try:
            if ping():
                return
        except Exception:
            pass
        log.info("waiting for %s... (%d/%d)", name, i + 1, attempts)
        time.sleep(1)
    raise RuntimeError(f"{name} not reachable")


def _audit(path: str, update: BeliefUpdate) -> None:
    """Append the update as one JSON line to the audit log."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a") as f:
        f.write(update.to_json() + "\n")


def process_article(
    article: Article,
    db: Db,
    embedder: Embedder,
    belief_queue: BeliefQueue,
    settings: Settings,
) -> None:
    src = article.source
    metrics.WORKER_ARTICLES_PROCESSED.labels(source=src).inc()
    embedding = embedder.embed_query(f"{article.title}\n{article.summary}")
    markets = db.top_k_markets(embedding, settings.top_k, settings.max_cosine_distance)
    if not markets:
        metrics.WORKER_ARTICLES_SKIPPED.labels(source=src).inc()
        log.info("no relevant markets for %r, skipping", article.title)
        return

    metrics.WORKER_MARKETS_MATCHED.labels(source=src).inc(len(markets))
    log.info("%r matched %d market(s)", article.title, len(markets))
    article_payload = {"title": article.title, "summary": article.summary, "url": article.url}

    for market in markets:
        with metrics.CLAUDE_REEVAL_DURATION.time():
            result = reevaluate(
                {"question": market.question, "description": market.description},
                market.current_score,
                article_payload,
                model=settings.anthropic_model,
                use_web_search=settings.worker_use_web_search,
            )
        if "error" in result or "probability" not in result:
            metrics.WORKER_REEVAL_FAILURES.inc()
            log.warning("eval failed for market %s: %s", market.id, result.get("error", result))
            continue

        metrics.WORKER_MARKETS_REEVALUATED.inc()
        new_score = float(result["probability"])
        reasoning = result.get("reasoning", "")
        update = db.apply_belief_update(market.id, new_score, article.url, reasoning)

        belief_queue.push(update)
        metrics.WORKER_BELIEF_UPDATES.labels(source=src).inc()
        _audit(settings.audit_log_path, update)
        prev = "—" if update.previous_score is None else f"{update.previous_score:.2f}"
        log.info(
            "market %s: %s -> %.2f (%s)",
            market.id, prev, update.new_score, article.url,
        )


def run() -> None:
    load_dotenv()
    settings = load_settings()

    queue = NewsQueue(settings.redis_url, settings.queue_key)
    belief_queue = BeliefQueue(settings.redis_url, settings.belief_queue_key)
    _wait_for("redis", queue.ping)

    db = Db(settings.database_url)
    _wait_for("postgres", db.ping)

    embedder = Embedder(settings.voyage_api_key, settings.voyage_model, settings.embedding_dim)

    metrics.start_metrics_server(settings.metrics_port)

    stop = {"flag": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.__setitem__("flag", True))

    log.info(
        "worker started: model=%s top_k=%d max_distance=%.2f",
        settings.anthropic_model, settings.top_k, settings.max_cosine_distance,
    )

    while not stop["flag"]:
        try:
            article = queue.pop(timeout=POP_TIMEOUT_SECONDS)
            if article is None:
                continue  # timeout — loop back to check the shutdown flag
            process_article(article, db, embedder, belief_queue, settings)
        except Exception:
            log.exception("failed processing article")

    log.info("worker stopped")


if __name__ == "__main__":
    run()
