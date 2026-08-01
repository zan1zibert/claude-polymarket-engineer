"""Groq-backed relevance verification — the worker's replacement for the
cosine-distance gate.

Cosine similarity retrieves candidates that are topically close, but it can't
tell two different events discussed with overlapping vocabulary apart (e.g.
two different "the Fed" stories). This module asks a fast, cheap Groq-hosted
model a narrower question instead: does this article actually describe the
same real-world event/development that this market's question is about?

Mirrors lib/claude.py's prompt-loading + defensive JSON-extraction approach.
"""
import json
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

_client = Groq()  # reads GROQ_API_KEY from env

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompts() -> tuple[str, str]:
    system = (PROMPTS_DIR / "relevance_system_prompt.txt").read_text()
    template = (PROMPTS_DIR / "relevance_prompt.txt").read_text()
    return system, template


def check_relevance(article: dict, market: dict, *, model: str) -> dict:
    """Ask Groq whether `article` and `market` describe the same real-world event.

    `article` needs {title, summary}; `market` needs {question, description}.
    Returns {"relevant": bool, "reasoning": str} on success, or
    {"error": ..., "raw": ...} if the response can't be parsed or is missing
    the "relevant" field.
    """
    system, template = _load_prompts()
    user_prompt = template.format(
        question=market["question"],
        description=market.get("description", "") or "(no description)",
        article_title=article["title"],
        article_summary=article.get("summary", "") or "(no summary)",
    )

    try:
        response = _client.chat.completions.create(
            model=model,
            max_tokens=512,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
        )
    except Exception as exc:
        return {"error": f"Groq API error: {exc}", "raw": ""}

    text = response.choices[0].message.content.strip()

    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        parsed = json.loads(text[start:end])
    except (json.JSONDecodeError, ValueError):
        return {"error": "Failed to parse JSON", "raw": text}

    if "relevant" not in parsed:
        return {"error": "Missing 'relevant' field", "raw": text}

    return {"relevant": bool(parsed["relevant"]), "reasoning": parsed.get("reasoning", "")}
