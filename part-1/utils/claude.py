import json
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

client = Anthropic()

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
MODEL = "claude-sonnet-4-6"
WEB_SEARCH_MAX_USES = 1

WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": WEB_SEARCH_MAX_USES,
}


def _load_prompts():
    system = (PROMPTS_DIR / "system_prompt.txt").read_text()
    template = (PROMPTS_DIR / "analysis_prompt.txt").read_text()
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


def analyze_market(market: dict, use_web_search: bool = True) -> dict:
    """Send market to Claude and get structured analysis. Optionally lets Claude search the web."""
    system, template = _load_prompts()

    user_prompt = template.format(
        question=market["question"],
        description=market["description"],
        end_date=market.get("end_date", "Unknown"),
    )

    kwargs = {
        "model": MODEL,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if use_web_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]

    response = client.messages.create(**kwargs)
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
