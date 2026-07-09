"""Market-syncer — the ingestion path.

Keeps the `markets` table in sync with Polymarket so the worker's pgvector
retrieval runs against a live market set. Each cycle:

  1. Fetch fresh binary markets resolving within RESOLUTION_WINDOW_DAYS.
  2. Embed the title + description of the *new* ones (Voyage, document side) and
     insert them — current_score seeded with the Polymarket yes-price, plus slug,
     volume, liquidity and end_date. Existing markets are left untouched, keeping
     their worker-evolved belief and embedding.
  3. Re-check every open market against Gamma and mark the resolved ones (closed,
     or no longer returned) as closed — rows are kept (excluded from retrieval)
     so a later scoring pass can grade our predictions against the outcome.

Singleton — like the feeder, two of these just duplicate work. Synchronous on
purpose: it makes a handful of sequential API + DB calls, not a fan-out.

Run:
    python -m services.syncer.main          # sync forever
    python -m services.syncer.main --once    # one cycle then exit (handy for dev)
"""
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv

from lib import metrics, polymarket
from lib.config import Settings, load_settings
from lib.db import Db
from lib.embeddings import Embedder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
)
log = logging.getLogger("syncer")


def _is_resolved(status: Optional[dict]) -> bool:
    """Has this stored market resolved (so we should mark it closed)?

    Resolved if Gamma no longer returns it (delisted/archived), reports it
    `closed`, or its resolution date has passed.
    """
    if status is None:
        return True
    if status.get("closed"):
        return True
    end = status.get("end_date")
    if end:
        try:
            return datetime.fromisoformat(end) < datetime.now(timezone.utc)
        except ValueError:
            pass
    return False


def sync_once(
    client: httpx.Client,
    db: Db, embedder: Embedder,
    settings: Settings
) -> dict:
    """Run one full sync. Returns counts: fetched / inserted / resolved."""
    fetched = polymarket.fetch_markets(
        client,
        window_days=settings.resolution_window_days,
        limit=settings.sync_fetch_limit,
        tag_id=settings.sync_tag_filter,
        url=settings.gamma_markets_url,
    )
    candidates = polymarket.filter_markets(
        fetched,
        min_volume_24h=settings.sync_min_volume_24h,
        min_liquidity=settings.sync_min_liquidity,
        price_band=settings.sync_price_band,
    )

    existing = db.existing_market_ids([m["id"] for m in candidates])
    new_rows = [m for m in candidates if m["id"] not in existing]

    # Embed only the new markets (title + description), document side.
    if new_rows:
        vectors = embedder.embed_documents(
            [f"{m['question']}\n{m['description']}" for m in new_rows]
        )
        for m, v in zip(new_rows, vectors):
            m["embedding"] = v

    inserted = db.insert_markets(new_rows)

    # Re-check every open market against Gamma: a market has resolved once Gamma
    # reports it closed or stops returning it as open (see _is_resolved). We check
    # all open markets, not just those past end_date, because markets can resolve
    # early — and it's cheap, since the query is over our holdings, not all of Gamma.
    open_ids = db.open_market_ids()
    statuses = polymarket.fetch_statuses(client, open_ids, url=settings.gamma_markets_url)
    outcomes = {
        i: (statuses.get(i) or {}).get("resolved_outcome")
        for i in open_ids if _is_resolved(statuses.get(i))
    }
    resolved = db.mark_resolved(outcomes)

    return {
        "fetched": len(candidates),
        "inserted": inserted,
        "resolved": resolved,
    }


def _wait_for_db(settings: Settings, attempts: int = 30) -> Db:
    """Retry connecting to Postgres until it's accepting connections."""
    for i in range(attempts):
        try:
            db = Db(settings.database_url)
            if db.ping():
                return db
        except Exception:
            pass
        log.info("waiting for postgres... (%d/%d)", i + 1, attempts)
        time.sleep(1)
    raise RuntimeError("postgres not reachable")


def run(once: bool = False) -> None:
    load_dotenv()
    settings = load_settings()

    db = _wait_for_db(settings)
    embedder = Embedder(
        settings.voyage_api_key, settings.voyage_model, settings.embedding_dim
    )

    metrics.start_metrics_server(settings.metrics_port)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info(
        "syncer started: window %dd, every %ds, fetch<=%d, tag_id=%d",
        settings.resolution_window_days,
        settings.sync_interval_seconds,
        settings.sync_fetch_limit,
        settings.sync_tag_filter
    )

    with httpx.Client(
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    ) as client:
        while not stop.is_set():
            try:
                c = sync_once(client, db, embedder, settings)
                metrics.SYNCER_MARKETS_FETCHED.inc(c["fetched"])
                metrics.SYNCER_MARKETS_INSERTED.inc(c["inserted"])
                metrics.SYNCER_MARKETS_RESOLVED.inc(c["resolved"])
                log.info(
                    "synced: %d candidates, +%d new, %d resolved",
                    c["fetched"], c["inserted"], c["resolved"],
                )
            except Exception:
                log.exception("sync cycle failed")

            if once:
                break

            # Sleep for the interval, but wake immediately on shutdown signal.
            stop.wait(timeout=settings.sync_interval_seconds)

    log.info("syncer stopped")


if __name__ == "__main__":
    run(once="--once" in sys.argv)
