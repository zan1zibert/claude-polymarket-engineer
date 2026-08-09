"""publish_snapshot reads open-market questions and overwrites the Redis snapshot."""
from unittest.mock import MagicMock

from services.syncer.main import publish_snapshot


def test_publish_snapshot_publishes_open_market_questions():
    db = MagicMock()
    db.open_market_questions.return_value = ["Will X?", "Will Y?"]
    snapshot = MagicMock()

    count = publish_snapshot(db, snapshot)

    assert count == 2
    snapshot.publish.assert_called_once_with(["Will X?", "Will Y?"])


def test_publish_snapshot_handles_empty_open_set():
    db = MagicMock()
    db.open_market_questions.return_value = []
    snapshot = MagicMock()

    count = publish_snapshot(db, snapshot)

    assert count == 0
    snapshot.publish.assert_called_once_with([])
