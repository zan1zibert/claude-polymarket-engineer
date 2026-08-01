"""Unit tests for lib/groq_relevance.check_relevance — JSON parsing only, the
Groq client itself is mocked out (no network calls in this suite).
"""
from unittest.mock import MagicMock, patch

from lib import groq_relevance

_ARTICLE = {"title": "Fed nominee withdraws", "summary": "...", "url": "https://x"}
_MARKET = {"question": "Will the Fed cut rates in March?", "description": "..."}


def _mock_response(content: str):
    response = MagicMock()
    response.choices[0].message.content = content
    return response


def test_check_relevance_parses_valid_json():
    content = '{"relevant": false, "reasoning": "different Fed story"}'
    with patch.object(groq_relevance._client.chat.completions, "create",
                       return_value=_mock_response(content)):
        result = groq_relevance.check_relevance(_ARTICLE, _MARKET, model="llama-3.1-8b-instant")

    assert result == {"relevant": False, "reasoning": "different Fed story"}


def test_check_relevance_handles_relevant_true():
    content = '{"relevant": true, "reasoning": "same rate decision"}'
    with patch.object(groq_relevance._client.chat.completions, "create",
                       return_value=_mock_response(content)):
        result = groq_relevance.check_relevance(_ARTICLE, _MARKET, model="llama-3.1-8b-instant")

    assert result == {"relevant": True, "reasoning": "same rate decision"}


def test_check_relevance_handles_malformed_json():
    content = "not json at all"
    with patch.object(groq_relevance._client.chat.completions, "create",
                       return_value=_mock_response(content)):
        result = groq_relevance.check_relevance(_ARTICLE, _MARKET, model="llama-3.1-8b-instant")

    assert result["error"] == "Failed to parse JSON"
    assert result["raw"] == content


def test_check_relevance_handles_missing_relevant_field():
    content = '{"reasoning": "forgot the verdict"}'
    with patch.object(groq_relevance._client.chat.completions, "create",
                       return_value=_mock_response(content)):
        result = groq_relevance.check_relevance(_ARTICLE, _MARKET, model="llama-3.1-8b-instant")

    assert result["error"] == "Missing 'relevant' field"


def test_check_relevance_handles_groq_api_error():
    with patch.object(groq_relevance._client.chat.completions, "create",
                       side_effect=Exception("connection timeout")):
        result = groq_relevance.check_relevance(_ARTICLE, _MARKET, model="llama-3.1-8b-instant")

    assert "error" in result
    assert "connection timeout" in result["error"]
    assert result["raw"] == ""
