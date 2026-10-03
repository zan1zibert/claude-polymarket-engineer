"""Jev-backed relevance verification — the worker's filter between pgvector
retrieval and Claude.

Cosine similarity retrieves candidates that are topically close, but it can't
tell two different events discussed with overlapping vocabulary apart (e.g.
two different "the Fed" stories). This module asks TypeSafe's Jev a narrower
question instead: does this article describe the same real-world event that
this market's question is about?

All of an article's candidates go in ONE request: the article is the shared
state, and each candidate market is its own Noul question (the duplicate-
matching pattern from the TypeSafe Noul docs). Questions are evaluated in
parallel, so the request count is one per article regardless of top_k.
"""
from typesafe_sdk import (
    Noul,
    NoulCriteria,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeClient,
    TypeSafeError,
)

from lib.metrics import JEV_HTTP_STATUS, JEV_REQUEST_DURATION, JEV_TOKENS

SAME_EVENT_QUESTION = (
    "Does `article` describe the same real-world event or development that "
    "`market`'s question asks about?"
)
SAME_EVENT_CRITERIA = NoulCriteria(
    true=(
        "The article reports on the specific event, decision, or development the "
        "market resolves on, so it could move a well-informed person's estimate of "
        "the market's probability."
    ),
    false=(
        "The article is about a different event, even if it shares people, "
        "organizations, topics, or keywords with the market."
    ),
)

_client: TypeSafeClient | None = None


def _get_client() -> TypeSafeClient:
    # Built lazily: the SDK validates TYPESAFE_API_KEY at construction, and
    # importing this module must not require a key (tests, other services).
    global _client
    if _client is None:
        _client = TypeSafeClient()
    return _client


def _question_id(index: int) -> str:
    return f"same_event_{index}"


def _questions(markets: list[dict]) -> dict[str, Noul]:
    return {
        _question_id(i): Noul(
            instructions={
                "market": {
                    "question": market["question"],
                    "description": market.get("description") or "",
                },
                "question": SAME_EVENT_QUESTION,
            },
            criteria=SAME_EVENT_CRITERIA,
        )
        for i, market in enumerate(markets)
    }


def check_relevance(article: dict, markets: list[dict], *, model: str) -> dict:
    """Ask Jev, in one request, whether `article` describes the same event as each market.

    `article` needs {title, summary}; each market needs {question, description}.
    Returns {"probabilities": [...], "model": <resolved model version>} with one
    P(same event) per market, in the order given, or {"error": str} if the
    request failed or an answer is missing.
    """
    state = {"article": {"title": article["title"], "summary": article.get("summary") or ""}}
    try:
        with JEV_REQUEST_DURATION.time():
            response = _get_client().system_one(
                state=state, questions=_questions(markets), model=model,
            )
    except TypeSafeAPIError as exc:
        JEV_HTTP_STATUS.labels(code=str(exc.status)).inc()
        return {"error": f"TypeSafe API error: {exc}"}
    except TypeSafeAPIConnectionError as exc:
        JEV_HTTP_STATUS.labels(code="connection").inc()
        return {"error": f"TypeSafe connection error: {exc}"}
    except TypeSafeError as exc:  # e.g. missing/invalid TYPESAFE_API_KEY at client creation
        JEV_HTTP_STATUS.labels(code="client").inc()
        return {"error": f"TypeSafe error: {exc}"}

    JEV_HTTP_STATUS.labels(code="200").inc()
    if response.usage.input_tokens is not None:
        JEV_TOKENS.labels(type="input").inc(response.usage.input_tokens)
    if response.usage.output_tokens is not None:
        JEV_TOKENS.labels(type="output").inc(response.usage.output_tokens)

    probabilities = []
    for i in range(len(markets)):
        answer = response.nouls.get(_question_id(i))
        if answer is None:
            return {"error": f"Missing answer for {_question_id(i)}"}
        probabilities.append(answer.noul)
    return {"probabilities": probabilities, "model": response.model}
