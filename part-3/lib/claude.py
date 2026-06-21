"""Claude re-evaluation — the worker's call to the model.

Price-blind by design: Claude is given the market, our current belief (the
prior), and the news — never the live Polymarket price. It returns an updated
probability. The downstream `signal` service is the only thing that compares
against price.

Mirrors the parsing approach of part-1/utils/claude.py (final text block + a
defensive JSON extraction) so behaviour is consistent across the repo.
"""
import json
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

_client = Anthropic()  # reads ANTHROPIC_API_KEY from env

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
WEB_SEARCH_MAX_USES = 1
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": WEB_SEARCH_MAX_USES,
}


def _load_prompts() -> tuple[str, str]:
    system = (PROMPTS_DIR / "worker_system_prompt.txt").read_text()
    template = (PROMPTS_DIR / "reeval_prompt.txt").read_text()
    return system, template


def _final_text(response) -> str:
    """Pull the model's last text block, ignoring tool-use and search-result blocks."""
    for block in reversed(response.content):
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    return ""


def _count_searches(response) -> int:
    return sum(
        1 for block in response.content
        if getattr(block, "type", None) == "server_tool_use"
        and getattr(block, "name", None) == "web_search"
    )


def reevaluate(
    market: dict,
    current_score,
    article: dict,
    *,
    model: str,
    use_web_search: bool = False,
) -> dict:
    """Ask Claude to update the prior for one market given one article.

    `market` needs {question, description}; `article` needs {title, summary, url}.
    `current_score` is our prior belief (0..1) or None on the first evaluation.
    Returns {probability, confidence, reasoning} or an {error, raw} dict on a
    parse failure.
    """
    system, template = _load_prompts()
    prior = "unknown (no prior estimate yet)" if current_score is None else f"{current_score:.2f}"

    user_prompt = template.format(
        question=market["question"],
        description=market.get("description", "") or "(no description)",
        current_score=prior,
        article_title=article["title"],
        article_summary=article.get("summary", "") or "(no summary)",
        article_url=article["url"],
    )

    kwargs = {
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if use_web_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]

    response = _client.messages.create(**kwargs)
    text = _final_text(response)
    searches = _count_searches(response)

    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        parsed = json.loads(text[start:end])
    except (json.JSONDecodeError, ValueError):
        return {"error": "Failed to parse JSON", "raw": text, "web_searches": searches}

    parsed["web_searches"] = searches
    return parsed
