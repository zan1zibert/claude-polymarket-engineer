"""Unit tests for lib/groq_relevance.check_relevance — JSON parsing only, the
Groq client itself is mocked out (no network calls in this suite).
"""
from unittest.mock import MagicMock, patch

from lib import groq_relevance
from lib.metrics import GROQ_TOKENS

_ARTICLE = {"title": "Fed nominee withdraws", "summary": "...", "url": "https://x"}
_MARKET = {"question": "Will the Fed cut rates in March?", "description": "..."}


def _mock_response(content: str):
    response = MagicMock()
    response.choices[0].message.content = content
    response.usage.prompt_tokens = 100
    response.usage.completion_tokens = 20
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


def test_check_relevance_records_groq_token_usage():
    before_input = GROQ_TOKENS.labels(type="input")._value.get()
    before_output = GROQ_TOKENS.labels(type="output")._value.get()

    content = '{"relevant": true, "reasoning": "same event"}'
    with patch.object(groq_relevance._client.chat.completions, "create",
                       return_value=_mock_response(content)):
        groq_relevance.check_relevance(_ARTICLE, _MARKET, model="llama-3.1-8b-instant")

    assert GROQ_TOKENS.labels(type="input")._value.get() == before_input + 100
    assert GROQ_TOKENS.labels(type="output")._value.get() == before_output + 20


def test_check_relevance_does_not_record_tokens_on_api_error():
    before_input = GROQ_TOKENS.labels(type="input")._value.get()
    before_output = GROQ_TOKENS.labels(type="output")._value.get()

    with patch.object(groq_relevance._client.chat.completions, "create",
                       side_effect=Exception("connection timeout")):
        groq_relevance.check_relevance(_ARTICLE, _MARKET, model="llama-3.1-8b-instant")

    assert GROQ_TOKENS.labels(type="input")._value.get() == before_input
    assert GROQ_TOKENS.labels(type="output")._value.get() == before_output
