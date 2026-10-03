"""Unit tests for services/worker/main.process_article — the relevance-filter
integration. All dependencies (Db, embedder, dirty_markets, check_relevance,
reevaluate) are mocked; no network or DB access.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lib.schemas import Article, BeliefUpdate, Market
from services.worker.main import process_article


def _settings(tmp_path, top_k=10):
    return SimpleNamespace(
        top_k=top_k,
        anthropic_model="claude-sonnet-5",
        anthropic_max_tokens=8192,
        worker_use_web_search=False,
        belief_move_epsilon=0.02,
        audit_log_path=str(tmp_path / "belief_updates.jsonl"),
        jev_model="jev-latest",
        jev_relevance_threshold=0.7,
    )


def _article():
    return Article(
        url="https://example.com/a", title="Fed nominee withdraws", summary="...",
        source="test-feed", category="politics", published_at=None, fetched_at="2026-08-01T00:00:00Z",
    )


def _market(market_id, question="Will the Fed cut rates?"):
    return Market(id=market_id, question=question, description="...", current_score=0.4)


def test_process_article_only_reevaluates_relevant_candidates(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = [
        _market("m1"), _market("m2", question="Will Congress pass the budget bill?"),
    ]
    db.apply_belief_update.return_value = BeliefUpdate(
        timestamp="2026-08-01T00:00:00Z", market_id="m1", market_title="Will the Fed cut rates?",
        previous_score=0.4, new_score=0.5, article_url="https://example.com/a", reasoning="moved",
    )
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    dirty_markets = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance",
               return_value={"probabilities": [0.92, 0.1], "model": "jev-1.13.0"}) as mock_check, \
         patch("services.worker.main.reevaluate", return_value={"probability": 0.5, "reasoning": "moved"}) as mock_reeval:
        process_article(_article(), db, embedder, dirty_markets, settings)

    mock_check.assert_called_once()
    assert [m["question"] for m in mock_check.call_args.args[1]] == [
        "Will the Fed cut rates?", "Will Congress pass the budget bill?",
    ]
    assert mock_reeval.call_count == 1
    assert db.apply_belief_update.call_count == 1
    assert db.apply_belief_update.call_args.args[0] == "m1"
    dirty_markets.add.assert_called_once_with("m1")


def test_process_article_applies_the_threshold_inclusively(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = [_market("m1"), _market("m2"), _market("m3")]
    db.apply_belief_update.return_value = BeliefUpdate(
        timestamp="2026-08-01T00:00:00Z", market_id="m1", market_title="q",
        previous_score=0.4, new_score=0.5, article_url="https://example.com/a", reasoning="moved",
    )
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    dirty_markets = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance",
               return_value={"probabilities": [0.7, 0.69, 0.99], "model": "jev-1.13.0"}), \
         patch("services.worker.main.reevaluate", return_value={"probability": 0.5, "reasoning": "moved"}):
        process_article(_article(), db, embedder, dirty_markets, settings)

    assert [c.args[0] for c in db.apply_belief_update.call_args_list] == ["m1", "m3"]


def test_process_article_logs_a_relevance_check_per_candidate(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = [_market("m1"), _market("m2")]
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    dirty_markets = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance",
               return_value={"probabilities": [0.05, 0.3], "model": "jev-1.13.0"}), \
         patch("services.worker.main.reevaluate") as mock_reeval:
        process_article(_article(), db, embedder, dirty_markets, settings)

    assert mock_reeval.call_count == 0
    assert db.log_relevance_check.call_count == 2
    first, second = db.log_relevance_check.call_args_list
    assert first.args == ("https://example.com/a", "Fed nominee withdraws", "m1", False, "", "jev-1.13.0")
    assert first.kwargs == {"probability": 0.05}
    assert second.args[2] == "m2"
    assert second.kwargs == {"probability": 0.3}
    dirty_markets.add.assert_not_called()


def test_process_article_treats_jev_failure_as_not_relevant_for_every_candidate(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = [_market("m1"), _market("m2")]
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    dirty_markets = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance",
               return_value={"error": "TypeSafe API error: 429"}), \
         patch("services.worker.main.reevaluate") as mock_reeval:
        process_article(_article(), db, embedder, dirty_markets, settings)

    assert mock_reeval.call_count == 0
    assert db.log_relevance_check.call_count == 2
    for call in db.log_relevance_check.call_args_list:
        assert call.args[3] is False  # relevant
        assert call.args[4] == "jev_error: TypeSafe API error: 429"
        assert call.args[5] == "jev-latest"
        assert call.kwargs == {"probability": None}


def test_process_article_skips_when_no_candidates(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = []
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    dirty_markets = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance") as mock_check, \
         patch("services.worker.main.reevaluate") as mock_reeval:
        process_article(_article(), db, embedder, dirty_markets, settings)

    mock_check.assert_not_called()
    mock_reeval.assert_not_called()
    db.log_relevance_check.assert_not_called()
