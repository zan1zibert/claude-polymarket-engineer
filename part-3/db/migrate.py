"""Forward-only SQL migration runner (Postgres).

Why this exists: the schema used to be a single `init.sql` mounted into the
postgres container's /docker-entrypoint-initdb.d/. That file runs ONLY on the
first start of an empty data volume — so once the DB held data, editing the
schema did nothing, and the only way to apply a change was to wipe the volume
(and every row with it). This runner applies changes to the *existing* database
instead, so schema evolution never costs data again.

Model: every *.sql file under db/migrations/ is a forward-only migration,
applied in filename order exactly once. A `schema_migrations` ledger records
which have run. Each file is applied inside its own transaction, so a failing
migration rolls back cleanly and leaves the ledger untouched.

Run standalone (reads DATABASE_URL from the environment):

    python db/migrate.py

In the stack it runs as the one-shot `migrate` service, which the worker and
syncer wait on (depends_on: service_completed_successfully) before starting.

Adding a migration: drop a new file in db/migrations/ with the next number,
e.g. `0004_add_positions_table.sql`. Keep statements idempotent where cheap
(IF NOT EXISTS) so a re-run against a hand-patched DB is harmless; never edit an
already-applied file — add a new one.
"""
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _log(msg: str) -> None:
    print(f"[migrate] {msg}", flush=True)


def _ensure_ledger(conn: psycopg.Connection) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename    TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )


def _applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {r[0] for r in cur.fetchall()}


def run(database_url: str) -> int:
    """Apply every pending migration in order. Returns the count applied."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        _log(f"no migrations found in {MIGRATIONS_DIR}")
        return 0

    with psycopg.connect(database_url) as conn:
        _ensure_ledger(conn)
        done = _applied(conn)

        pending = [f for f in files if f.name not in done]
        if not pending:
            _log(f"up to date ({len(done)} applied, nothing to do)")
            return 0

        for f in pending:
            sql = f.read_text()
            _log(f"applying {f.name} ...")
            # One transaction per file: DDL rolls back atomically on failure,
            # and the ledger row is written in the same txn so a crash can't
            # record a migration that didn't fully apply.
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (f.name,),
                )
            _log(f"applied  {f.name}")

    _log(f"done ({len(pending)} applied)")
    return len(pending)


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        _log("error: DATABASE_URL is not set")
        return 1
    try:
        run(database_url)
    except Exception as exc:  # noqa: BLE001 — surface any failure as a nonzero exit
        _log(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
