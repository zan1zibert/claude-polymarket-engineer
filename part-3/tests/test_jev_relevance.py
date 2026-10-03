"""Unit tests for lib/jev_relevance.check_relevance — request shape and answer
mapping. The TypeSafe client is mocked out (no network calls in this suite).
"""
from unittest.mock import MagicMock, patch

import httpx2
from typesafe_sdk import SystemOneResponse, TypeSafeAPIConnectionError, TypeSafeRateLimitError

from lib import jev_relevance
from lib.metrics import JEV_HTTP_STATUS, JEV_TOKENS

_ARTICLE = {"title": "Fed nominee withdraws", "summary": "The nominee pulled out.", "url": "https://x/a"}
_MARKETS = [
    {"question": "Will the Fed nominee be confirmed?", "description": "Resolves YES if confirmed."},
    {"question": "Will the Fed cut rates in October?", "description": ""},
    {"question": "Will Congress pass the budget?", "description": None},
]


def _response(nouls: dict[str, float], usage=None, model="jev-1.13.0") -> SystemOneResponse:
    return SystemOneResponse.model_validate({
        "model": model,
        "answers": {qid: {"type": "noul", "noul": p} for qid, p in nouls.items()},
        "usage": usage if usage is not None else {"input_tokens": 500, "output_tokens": 30},
    })


def _client_returning(response) -> MagicMock:
    client = MagicMock()
    client.system_one.return_value = response
    return client


def test_check_relevance_sends_one_request_with_one_noul_per_market():
    client = _client_returning(_response({"same_event_0": 0.9, "same_event_1": 0.2, "same_event_2": 0.01}))

    with patch.object(jev_relevance, "_get_client", return_value=client):
        jev_relevance.check_relevance(_ARTICLE, _MARKETS, model="jev-latest")

    client.system_one.assert_called_once()
    kwargs = client.system_one.call_args.kwargs
    assert kwargs["model"] == "jev-latest"
    assert kwargs["state"] == {"article": {"title": _ARTICLE["title"], "summary": _ARTICLE["summary"]}}
    questions = kwargs["questions"]
    assert list(questions) == ["same_event_0", "same_event_1", "same_event_2"]
    assert questions["same_event_0"].instructions["market"] == {
        "question": "Will the Fed nominee be confirmed?",
        "description": "Resolves YES if confirmed.",
    }
    # Every Noul asks the same question; only the market differs.
    assert len({q.instructions["question"] for q in questions.values()}) == 1
    assert questions["same_event_2"].instructions["market"]["description"] == ""


def test_check_relevance_maps_probabilities_back_in_market_order():
    client = _client_returning(_response({"same_event_2": 0.01, "same_event_0": 0.9, "same_event_1": 0.2}))

    with patch.object(jev_relevance, "_get_client", return_value=client):
        result = jev_relevance.check_relevance(_ARTICLE, _MARKETS, model="jev-latest")

    assert result == {"probabilities": [0.9, 0.2, 0.01], "model": "jev-1.13.0"}


def test_check_relevance_reports_a_missing_answer_as_an_error():
    client = _client_returning(_response({"same_event_0": 0.9, "same_event_1": 0.2}))

    with patch.object(jev_relevance, "_get_client", return_value=client):
        result = jev_relevance.check_relevance(_ARTICLE, _MARKETS, model="jev-latest")

    assert "error" in result
    assert "same_event_2" in result["error"]


def test_check_relevance_handles_api_error():
    client = MagicMock()
    client.system_one.side_effect = TypeSafeRateLimitError(429, {"error": "slow down"}, httpx2.Headers())
    before = JEV_HTTP_STATUS.labels(code="429")._value.get()

    with patch.object(jev_relevance, "_get_client", return_value=client):
        result = jev_relevance.check_relevance(_ARTICLE, _MARKETS, model="jev-latest")

    assert result["error"].startswith("TypeSafe API error")
    assert JEV_HTTP_STATUS.labels(code="429")._value.get() == before + 1


def test_check_relevance_handles_connection_error():
    client = MagicMock()
    client.system_one.side_effect = TypeSafeAPIConnectionError("connection refused")
    before = JEV_HTTP_STATUS.labels(code="connection")._value.get()

    with patch.object(jev_relevance, "_get_client", return_value=client):
        result = jev_relevance.check_relevance(_ARTICLE, _MARKETS, model="jev-latest")

    assert "error" in result
    assert JEV_HTTP_STATUS.labels(code="connection")._value.get() == before + 1


def test_check_relevance_records_token_usage_and_success_status():
    client = _client_returning(_response(
        {"same_event_0": 0.9, "same_event_1": 0.2, "same_event_2": 0.01},
        usage={"input_tokens": 100, "output_tokens": 20},
    ))
    before_in = JEV_TOKENS.labels(type="input")._value.get()
    before_out = JEV_TOKENS.labels(type="output")._value.get()
    before_ok = JEV_HTTP_STATUS.labels(code="200")._value.get()

    with patch.object(jev_relevance, "_get_client", return_value=client):
        jev_relevance.check_relevance(_ARTICLE, _MARKETS, model="jev-latest")

    assert JEV_TOKENS.labels(type="input")._value.get() == before_in + 100
    assert JEV_TOKENS.labels(type="output")._value.get() == before_out + 20
    assert JEV_HTTP_STATUS.labels(code="200")._value.get() == before_ok + 1


def test_check_relevance_tolerates_unreported_usage():
    client = _client_returning(_response(
        {"same_event_0": 0.9, "same_event_1": 0.2, "same_event_2": 0.01}, usage={},
    ))
    before_in = JEV_TOKENS.labels(type="input")._value.get()

    with patch.object(jev_relevance, "_get_client", return_value=client):
        result = jev_relevance.check_relevance(_ARTICLE, _MARKETS, model="jev-latest")

    assert result["probabilities"] == [0.9, 0.2, 0.01]
    assert JEV_TOKENS.labels(type="input")._value.get() == before_in
