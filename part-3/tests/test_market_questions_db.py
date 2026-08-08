"""Integration test for Db.open_market_questions.

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_market_questions_db.py

Skipped without TEST_DATABASE_URL so a bare pytest stays green.
"""
import os

import psycopg
import pytest

from db import migrate
from lib.db import Db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run open_market_questions DB test"
)

_IDS = ("utest_qOpen", "utest_qClosed")
_ZERO_VECTOR = "[" + ",".join(["0"] * 1024) + "]"


@pytest.fixture(scope="module")
def _schema():
    try:
        migrate.run(TEST_DATABASE_URL)
    except Exception as exc:
        pytest.skip(f"TEST_DATABASE_URL not usable: {exc}")


@pytest.fixture
def db(_schema):
    def _cleanup():
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
            cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (list(_IDS),))
    _cleanup()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO markets (id, question, description, embedding, closed) "
            "VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)",
            ("utest_qOpen", "Will the open market resolve yes?", "d", _ZERO_VECTOR, False,
             "utest_qClosed", "Will the closed market resolve yes?", "d", _ZERO_VECTOR, True),
        )
    d = Db(TEST_DATABASE_URL)
    yield d
    _cleanup()


def test_open_market_questions_returns_only_open_with_question(db):
    rows = dict(db.open_market_questions())
    assert rows.get("utest_qOpen") == "Will the open market resolve yes?"
    assert "utest_qClosed" not in rows
