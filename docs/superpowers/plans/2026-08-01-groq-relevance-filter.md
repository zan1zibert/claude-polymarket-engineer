# Groq Relevance Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the worker's cosine-distance relevance gate with a Groq-backed event-identity check, so Claude only sees article/market pairs that actually describe the same real-world event.

**Architecture:** `db.top_k_markets` becomes retrieval-only (top-k nearest by embedding, no distance cutoff). A new `lib/groq_relevance.check_relevance` call — one per (article, candidate market) pair — decides relevant/not, using Groq's `llama-3.1-8b-instant`. Every verdict (accepted, rejected, or Groq failure) is written to a new `relevance_checks` Postgres table. Only candidates verdict `relevant=True` proceed to the existing `reevaluate()` Claude call.

**Tech Stack:** Python, psycopg + pgvector (Postgres), Groq Python SDK (`groq` package), pytest.

## Global Constraints

- A Groq API error, timeout, or unparseable response is treated as **not relevant** (fail closed) — logged to `relevance_checks` with `relevant = false` and a `reasoning` value prefixed `groq_error: `, and counted separately in `WORKER_GROQ_FAILURES` so outages are visible.
- `top_k` default moves from `5` to `10` (still overridable via `TOP_K`).
- `max_cosine_distance` / `MAX_COSINE_DISTANCE` is removed entirely — retrieval width is governed only by `top_k`.
- Relevance decisions are recorded in a Postgres table (`relevance_checks`), not a JSONL file — every `top_k_markets` candidate gets exactly one row, regardless of verdict.
- No change to `reevaluate()`'s behavior, prompt, or signature.

---

## Task 1: Retrieval-only `top_k_markets` + `relevance_checks` table + `log_relevance_check`

**Files:**
- Create: `part-3/db/migrations/0004_relevance_checks.sql`
- Modify: `part-3/lib/db.py:49-77` (`top_k_markets`), add new method after `apply_belief_update` (currently ends `part-3/lib/db.py:123`)
- Test: `part-3/tests/test_relevance_checks_db.py`

**Interfaces:**
- Produces: `Db.top_k_markets(self, embedding: list[float], k: int) -> list[Market]` (drops the `max_distance` parameter it has today).
- Produces: `Db.log_relevance_check(self, article_url: str, article_title: str, market_id: str, relevant: bool, reasoning: str, model: str) -> None`.
- Consumes: `lib.schemas.Market` (unchanged), the `markets` table (unchanged).

- [ ] **Step 1: Write the migration**

Create `part-3/db/migrations/0004_relevance_checks.sql`:

```sql
-- 0004 — relevance_checks (audit log for the Groq relevance filter).
--
-- The worker used to filter pgvector's top_k_markets candidates with a
-- cosine-distance threshold (max_cosine_distance). That threshold catches
-- topical/vocabulary proximity, not event identity, so genuinely unrelated
-- markets routinely passed the gate and reached Claude. This table replaces
-- the threshold with a per-candidate Groq relevance verdict, and records
-- EVERY verdict (accepted, rejected, or a Groq failure) so the filter's
-- precision can be reviewed from real data instead of guessed at.
--
-- market_id is a real FK: every row originates from a top_k_markets
-- candidate, so the referenced market always exists.

CREATE TABLE IF NOT EXISTS relevance_checks (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    article_url   TEXT NOT NULL,
    article_title TEXT NOT NULL,
    market_id     TEXT NOT NULL REFERENCES markets(id),
    relevant      BOOLEAN NOT NULL,
    reasoning     TEXT NOT NULL,
    model         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS relevance_checks_market_idx
    ON relevance_checks (market_id, ts DESC);
```

- [ ] **Step 2: Write the failing test**

Create `part-3/tests/test_relevance_checks_db.py`:

```python
"""Integration tests for the relevance-check DB layer: retrieval-only
top_k_markets (no distance filter) and log_relevance_check.

Point a test DB at these with:

    TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_relevance_checks_db.py

Without TEST_DATABASE_URL (or if the DB is unreachable) the whole module is
skipped, so a bare `pytest` stays green with no infrastructure.
"""
import os

import psycopg
import pytest

from db import migrate
from lib.db import Db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set TEST_DATABASE_URL to run relevance-check DB tests"
)

_MARKET_IDS = ("utest_rcA", "utest_rcB")


def _unit_vector(index: int, dim: int = 1024) -> str:
    """A pgvector literal with a 1 at `index` and 0 elsewhere (a well-defined,
    nonzero vector — needed because cosine distance from the zero vector is
    undefined)."""
    values = ["0"] * dim
    values[index] = "1"
    return "[" + ",".join(values) + "]"


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
            cur.execute("DELETE FROM relevance_checks WHERE market_id = ANY(%s)", (list(_MARKET_IDS),))
            cur.execute("DELETE FROM markets WHERE id = ANY(%s)", (list(_MARKET_IDS),))

    _cleanup()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        # A (index 0) sits at distance 0 from the query; B (index 1) is
        # orthogonal to it — cosine distance 1.0, well beyond the old 0.35
        # threshold. Both must still come back once filtering is Groq's job.
        cur.execute(
            "INSERT INTO markets (id, question, embedding) VALUES (%s, %s, %s)",
            (_MARKET_IDS[0], "question A", _unit_vector(0)),
        )
        cur.execute(
            "INSERT INTO markets (id, question, embedding) VALUES (%s, %s, %s)",
            (_MARKET_IDS[1], "question B", _unit_vector(1)),
        )
    yield Db(TEST_DATABASE_URL)
    _cleanup()


def test_top_k_markets_has_no_distance_filter(db):
    query_embedding = [1.0] + [0.0] * 1023  # matches market A exactly, orthogonal to B

    results = db.top_k_markets(query_embedding, k=2)

    assert {m.id for m in results} == set(_MARKET_IDS)


def test_top_k_markets_respects_k(db):
    query_embedding = [1.0] + [0.0] * 1023

    results = db.top_k_markets(query_embedding, k=1)

    assert len(results) == 1
    assert results[0].id == _MARKET_IDS[0]  # the exact match, nearest by distance


def test_log_relevance_check_inserts_a_row(db):
    db.log_relevance_check(
        article_url="https://example.com/a",
        article_title="Some Article",
        market_id=_MARKET_IDS[0],
        relevant=True,
        reasoning="same event",
        model="llama-3.1-8b-instant",
    )

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            "SELECT article_url, article_title, market_id, relevant, reasoning, model "
            "FROM relevance_checks WHERE market_id = %s",
            (_MARKET_IDS[0],),
        )
        row = cur.fetchone()

    assert row == (
        "https://example.com/a", "Some Article", _MARKET_IDS[0], True, "same event",
        "llama-3.1-8b-instant",
    )
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd part-3 && TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_relevance_checks_db.py -v`
Expected: FAIL — `relation "relevance_checks" does not exist` (migration not applied yet) or `TypeError: top_k_markets() missing 1 required positional argument: 'max_distance'` depending on which runs first. Either failure confirms the test is exercising code that doesn't exist yet.

- [ ] **Step 4: Apply the migration**

Run: `cd part-3 && python db/migrate.py` (with `DATABASE_URL` pointed at the same DB as `TEST_DATABASE_URL`, or `TEST_DATABASE_URL` itself — the fixture also calls `migrate.run` automatically, so this step is mainly to sanity-check the SQL applies cleanly by hand).

- [ ] **Step 5: Update `Db.top_k_markets`**

In `part-3/lib/db.py`, replace the method at lines 49-77:

```python
    def top_k_markets(self, embedding: list[float], k: int) -> list[Market]:
        """The k markets nearest the embedding, by cosine distance.

        Retrieval only — no relevance filtering here. `<=>` is pgvector's
        cosine distance (0 = identical, 2 = opposite); we order by it and take
        the k closest open markets. The caller (worker) is responsible for
        deciding which of these candidates are actually relevant (see
        lib/groq_relevance.check_relevance) — a distance cutoff can't tell two
        different events discussed with overlapping vocabulary apart.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, question, description, current_score
                FROM markets
                WHERE NOT closed
                ORDER BY embedding <=> %s
                LIMIT %s
                """,
                (Vector(embedding), k),
            )
            return [
                Market(id=r[0], question=r[1], description=r[2], current_score=r[3])
                for r in cur.fetchall()
            ]
```

- [ ] **Step 6: Add `Db.log_relevance_check`**

In `part-3/lib/db.py`, add this method immediately after `apply_belief_update` (after the closing of the method that currently ends at line 123, before the `# ----- syncer` comment):

```python
    def log_relevance_check(
        self,
        article_url: str,
        article_title: str,
        market_id: str,
        relevant: bool,
        reasoning: str,
        model: str,
    ) -> None:
        """Persist one Groq relevance verdict for a (article, market) candidate.

        Called once per top_k_markets candidate, regardless of verdict —
        accepted, rejected, or a Groq failure (relevant=False with a
        "groq_error: ..." reasoning) — so the filter's precision can be
        reviewed from real data instead of guessed at.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO relevance_checks
                    (article_url, article_title, market_id, relevant, reasoning, model)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (article_url, article_title, market_id, relevant, reasoning, model),
            )
```

Also update the module docstring's operations list at the top of `part-3/lib/db.py` (lines 3-5) to add:

```python
  - log_relevance_check : record a Groq relevance verdict for one candidate
```

- [ ] **Step 7: Run test to verify it passes**

Run: `cd part-3 && TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest tests/test_relevance_checks_db.py -v`
Expected: PASS (3 tests)

- [ ] **Step 8: Run the full suite to check nothing else broke**

Run: `cd part-3 && pytest`
Expected: existing tests still PASS (DB-dependent ones skip without `TEST_DATABASE_URL`). `tests/test_db.py` is unaffected — it doesn't call `top_k_markets`.

- [ ] **Step 9: Commit**

```bash
cd part-3
git add db/migrations/0004_relevance_checks.sql lib/db.py tests/test_relevance_checks_db.py
git commit -m "feat(db): retrieval-only top_k_markets + relevance_checks table"
```

---

## Task 2: Groq relevance-check module (`lib/groq_relevance.py`)

**Files:**
- Create: `part-3/prompts/relevance_system_prompt.txt`
- Create: `part-3/prompts/relevance_prompt.txt`
- Create: `part-3/lib/groq_relevance.py`
- Modify: `part-3/requirements.txt` (add `groq`)
- Test: `part-3/tests/test_groq_relevance.py`

**Interfaces:**
- Produces: `check_relevance(article: dict, market: dict, *, model: str) -> dict`, returning `{"relevant": bool, "reasoning": str}` on success or `{"error": str, "raw": str}` on any parse/response failure. `article` needs `{title, summary}` (url/description are for context only, not required by this function). `market` needs `{question, description}`.
- Consumes: nothing from Task 1.

- [ ] **Step 1: Write the prompt files**

Create `part-3/prompts/relevance_system_prompt.txt`:

```
You are a strict relevance classifier for a prediction-market news pipeline.

You will be given a prediction market's question and description, and a news
article. Your only job is to decide whether the article describes the SAME
real-world event or development that the market's question is asking about —
not whether they share topics, keywords, or entities.

Guidelines:
- Two items can share vocabulary (people, organizations, general topics) while
  being about completely unrelated events. That does NOT count as relevant.
- The article must describe something that could plausibly move a well-informed
  person's probability estimate for the market's specific question.
- When in doubt, say NOT relevant. False positives waste an expensive downstream
  analysis; false negatives just mean the news is skipped.

Output valid JSON only, with exactly these fields:
  "relevant": true | false,
  "reasoning": "one sentence explaining the verdict"
```

Create `part-3/prompts/relevance_prompt.txt`:

```
Prediction market:

Question: {question}
Description: {description}

News article:

Title: {article_title}
Summary: {article_summary}

Does this article describe the same real-world event or development this
market's question is about? Output only valid JSON:

"relevant": true|false,
"reasoning": one sentence
```

- [ ] **Step 2: Write the failing test**

Create `part-3/tests/test_groq_relevance.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd part-3 && pytest tests/test_groq_relevance.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'lib.groq_relevance'`

- [ ] **Step 4: Add `groq` to requirements**

In `part-3/requirements.txt`, update the header comment and add the dependency:

```
# Shared dependencies across the part-3 services.
# feeder:  httpx, feedparser, redis
# worker:  + anthropic, voyageai, groq, psycopg, pgvector
httpx
feedparser
redis
python-dotenv
anthropic
voyageai
groq
psycopg[binary]
pgvector
prometheus_client
```

Run: `cd part-3 && pip install -r requirements.txt`

- [ ] **Step 5: Write `lib/groq_relevance.py`**

Create `part-3/lib/groq_relevance.py`:

```python
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

    response = _client.chat.completions.create(
        model=model,
        max_tokens=512,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
    )
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
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd part-3 && pytest tests/test_groq_relevance.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
cd part-3
git add prompts/relevance_system_prompt.txt prompts/relevance_prompt.txt lib/groq_relevance.py requirements.txt tests/test_groq_relevance.py
git commit -m "feat: add Groq relevance-check module"
```

---

## Task 3: Worker integration — config, metrics, and `process_article`

**Files:**
- Modify: `part-3/lib/config.py` (`Settings` dataclass + `load_settings`)
- Modify: `part-3/lib/metrics.py` (add 3 counters)
- Modify: `part-3/services/worker/main.py` (`process_article`, `run`)
- Modify: `part-3/.env.example`
- Modify: `part-3/docker-compose.yml`
- Test: `part-3/tests/test_worker.py`

**Interfaces:**
- Consumes: `Db.top_k_markets(embedding, k)` (Task 1), `Db.log_relevance_check(...)` (Task 1), `check_relevance(article, market, *, model)` (Task 2).
- Produces: nothing new consumed elsewhere — this is the top-level integration.

- [ ] **Step 1: Update `Settings`**

In `part-3/lib/config.py`, in the `Settings` dataclass (around line 28-29), replace:

```python
    top_k: int                   # candidate markets retrieved per article
    max_cosine_distance: float   # relevance gate; matches beyond this are dropped
```

with:

```python
    top_k: int                   # candidate markets retrieved per article
    groq_model: str               # model used for the per-candidate relevance check
```

In `load_settings()` (around lines 72-73), replace:

```python
        top_k=int(os.environ.get("TOP_K", "5")),
        max_cosine_distance=float(os.environ.get("MAX_COSINE_DISTANCE", "0.35")),
```

with:

```python
        top_k=int(os.environ.get("TOP_K", "10")),
        groq_model=os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"),
```

- [ ] **Step 2: Add the three Groq metrics**

In `part-3/lib/metrics.py`, immediately after `WORKER_BELIEF_MOVED` (currently ending at line 68) and before `CLAUDE_REEVAL_DURATION`, add:

```python
WORKER_GROQ_RELEVANT = Counter(
    "worker_groq_relevant_total",
    "Candidates the Groq relevance check accepted (proceeded to Claude)",
    ["source"],
)
WORKER_GROQ_REJECTED = Counter(
    "worker_groq_rejected_total",
    "Candidates the Groq relevance check rejected as not relevant",
    ["source"],
)
WORKER_GROQ_FAILURES = Counter(
    "worker_groq_failures_total",
    "Candidates dropped because the Groq relevance check errored or failed to parse "
    "(counted separately from worker_groq_rejected_total so outages are visible)",
    ["source"],
)
```

- [ ] **Step 3: Write the failing test**

Create `part-3/tests/test_worker.py`:

```python
"""Unit tests for services/worker/main.process_article — the relevance-filter
integration. All dependencies (Db, embedder, belief_queue, check_relevance,
reevaluate) are mocked; no network or DB access.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lib.schemas import Article, BeliefUpdate, Market
from services.worker.main import process_article


def _settings(tmp_path, top_k=10):
    return SimpleNamespace(
        top_k=top_k,
        anthropic_model="claude-sonnet-4-6",
        worker_use_web_search=False,
        belief_move_epsilon=0.02,
        audit_log_path=str(tmp_path / "belief_updates.jsonl"),
        groq_model="llama-3.1-8b-instant",
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
    db.top_k_markets.return_value = [_market("m1"), _market("m2")]
    db.apply_belief_update.return_value = BeliefUpdate(
        timestamp="2026-08-01T00:00:00Z", market_id="m1", market_title="Will the Fed cut rates?",
        previous_score=0.4, new_score=0.5, article_url="https://example.com/a", reasoning="moved",
    )
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    belief_queue = MagicMock()
    settings = _settings(tmp_path)

    def fake_check_relevance(article, market, *, model):
        return {"relevant": market["question"] == "Will the Fed cut rates?", "reasoning": "r"}

    with patch("services.worker.main.check_relevance", side_effect=fake_check_relevance), \
         patch("services.worker.main.reevaluate", return_value={"probability": 0.5, "reasoning": "moved"}) as mock_reeval:
        process_article(_article(), db, embedder, belief_queue, settings)

    assert mock_reeval.call_count == 1
    assert db.apply_belief_update.call_count == 1
    assert db.apply_belief_update.call_args.args[0] == "m1"


def test_process_article_logs_a_relevance_check_per_candidate(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = [_market("m1"), _market("m2")]
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    belief_queue = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance",
               return_value={"relevant": False, "reasoning": "different event"}), \
         patch("services.worker.main.reevaluate") as mock_reeval:
        process_article(_article(), db, embedder, belief_queue, settings)

    assert mock_reeval.call_count == 0
    assert db.log_relevance_check.call_count == 2
    _, kwargs = db.log_relevance_check.call_args
    call_args = db.log_relevance_check.call_args.args
    assert call_args[3] is False  # relevant
    assert call_args[4] == "different event"  # reasoning


def test_process_article_treats_groq_failure_as_not_relevant(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = [_market("m1")]
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    belief_queue = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance",
               return_value={"error": "Failed to parse JSON", "raw": "garbage"}), \
         patch("services.worker.main.reevaluate") as mock_reeval:
        process_article(_article(), db, embedder, belief_queue, settings)

    assert mock_reeval.call_count == 0
    db.log_relevance_check.assert_called_once()
    call_args = db.log_relevance_check.call_args.args
    assert call_args[3] is False  # relevant
    assert call_args[4].startswith("groq_error:")


def test_process_article_skips_when_no_candidates(tmp_path):
    db = MagicMock()
    db.top_k_markets.return_value = []
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0] * 1024
    belief_queue = MagicMock()
    settings = _settings(tmp_path)

    with patch("services.worker.main.check_relevance") as mock_check, \
         patch("services.worker.main.reevaluate") as mock_reeval:
        process_article(_article(), db, embedder, belief_queue, settings)

    mock_check.assert_not_called()
    mock_reeval.assert_not_called()
    db.log_relevance_check.assert_not_called()
```

- [ ] **Step 4: Run test to verify it fails**

Run: `cd part-3 && pytest tests/test_worker.py -v`
Expected: FAIL — `TypeError: top_k_markets() missing 1 required positional argument` (mock call vs. old 3-arg call site) or `AttributeError`/`NameError` around `check_relevance` not being imported in `services.worker.main` yet.

- [ ] **Step 5: Update `process_article` and `run`**

In `part-3/services/worker/main.py`, add the import (alongside the existing `from lib.claude import reevaluate`):

```python
from lib.groq_relevance import check_relevance
```

Replace the body of `process_article` (currently lines 67-119) with:

```python
def process_article(
    article: Article,
    db: Db,
    embedder: Embedder,
    belief_queue: BeliefQueue,
    settings: Settings,
) -> None:
    src = article.source
    metrics.WORKER_ARTICLES_PROCESSED.labels(source=src).inc()
    embedding = embedder.embed_query(f"{article.title}\n{article.summary}")
    candidates = db.top_k_markets(embedding, settings.top_k)
    if not candidates:
        metrics.WORKER_ARTICLES_SKIPPED.labels(source=src).inc()
        log.info("no candidate markets for %r, skipping", article.title)
        return

    article_payload = {"title": article.title, "summary": article.summary, "url": article.url}

    markets = []
    for candidate in candidates:
        market_payload = {"question": candidate.question, "description": candidate.description}
        verdict = check_relevance(article_payload, market_payload, model=settings.groq_model)

        if "error" in verdict:
            metrics.WORKER_GROQ_FAILURES.labels(source=src).inc()
            db.log_relevance_check(
                article.url, article.title, candidate.id, False,
                f"groq_error: {verdict['error']}", settings.groq_model,
            )
            continue

        relevant = bool(verdict.get("relevant"))
        reasoning = verdict.get("reasoning", "")
        db.log_relevance_check(
            article.url, article.title, candidate.id, relevant, reasoning, settings.groq_model,
        )
        if relevant:
            metrics.WORKER_GROQ_RELEVANT.labels(source=src).inc()
            markets.append(candidate)
        else:
            metrics.WORKER_GROQ_REJECTED.labels(source=src).inc()

    if not markets:
        metrics.WORKER_ARTICLES_SKIPPED.labels(source=src).inc()
        log.info("no relevant markets for %r, skipping", article.title)
        return

    metrics.WORKER_MARKETS_MATCHED.labels(source=src).inc(len(markets))
    log.info("%r matched %d market(s)", article.title, len(markets))

    for market in markets:
        with metrics.CLAUDE_REEVAL_DURATION.time():
            result = reevaluate(
                {"question": market.question, "description": market.description},
                market.current_score,
                article_payload,
                model=settings.anthropic_model,
                use_web_search=settings.worker_use_web_search,
            )
        if "error" in result or "probability" not in result:
            metrics.WORKER_REEVAL_FAILURES.labels(source=src).inc()
            log.warning("eval failed for market %s: %s", market.id, result.get("error", result))
            continue

        metrics.WORKER_MARKETS_REEVALUATED.labels(source=src).inc()
        new_score = float(result["probability"])
        reasoning = result.get("reasoning", "")
        update = db.apply_belief_update(market.id, new_score, article.url, reasoning)

        belief_queue.push(update)
        metrics.WORKER_BELIEF_UPDATES.labels(source=src).inc()
        moved = (
            update.previous_score is None
            or abs(update.new_score - update.previous_score) >= settings.belief_move_epsilon
        )
        if moved:
            metrics.WORKER_BELIEF_MOVED.labels(source=src).inc()
        _audit(settings.audit_log_path, update)
        prev = "—" if update.previous_score is None else f"{update.previous_score:.2f}"
        log.info(
            "market %s: %s -> %.2f (%s)",
            market.id, prev, update.new_score, article.url,
        )
```

In `run()`, update the startup log line (currently referencing `settings.max_cosine_distance`):

```python
    log.info(
        "worker started: model=%s top_k=%d groq_model=%s",
        settings.anthropic_model, settings.top_k, settings.groq_model,
    )
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd part-3 && pytest tests/test_worker.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Update `.env.example`**

In `part-3/.env.example`, under `# --- worker (market analyzer) ---`, add the Groq key next to the other required keys:

```
ANTHROPIC_API_KEY=sk-ant-...
VOYAGE_API_KEY=pa-...
GROQ_API_KEY=gsk_...
DATABASE_URL=postgresql://pm:pm@localhost:5432/pm
```

And in the "Optional worker tuning" comment block, replace:

```
# TOP_K=5
# MAX_COSINE_DISTANCE=0.35
```

with:

```
# TOP_K=10
# GROQ_MODEL=llama-3.1-8b-instant
```

- [ ] **Step 8: Wire `GROQ_API_KEY` into docker-compose**

In `part-3/docker-compose.yml`, in the `worker` service's `environment` block, add `GROQ_API_KEY` alongside the existing keys:

```yaml
    environment:
      REDIS_URL: redis://redis:6379/0
      DATABASE_URL: postgresql://pm:pm@postgres:5432/pm
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      VOYAGE_API_KEY: ${VOYAGE_API_KEY}
      GROQ_API_KEY: ${GROQ_API_KEY}
      AUDIT_LOG_PATH: /data/belief_updates.jsonl
```

- [ ] **Step 9: Run the full suite**

Run: `cd part-3 && pytest`
Expected: all pure-logic and mocked tests PASS; DB-dependent tests skip without `TEST_DATABASE_URL`, PASS with it (`TEST_DATABASE_URL=postgresql://pm:pm@localhost:5432/pm pytest`).

- [ ] **Step 10: Manual smoke test (optional, needs real API keys)**

Run: `cd part-3 && docker compose up --build redis postgres migrate feeder worker`, watch `docker compose logs -f worker` for `"worker started: model=... top_k=10 groq_model=llama-3.1-8b-instant"`, then inspect `relevance_checks` once an article is processed:

```sh
docker compose exec postgres psql -U pm -d pm -c 'SELECT article_title, market_id, relevant, reasoning FROM relevance_checks ORDER BY ts DESC LIMIT 10;'
```

- [ ] **Step 11: Commit**

```bash
cd part-3
git add lib/config.py lib/metrics.py services/worker/main.py .env.example docker-compose.yml tests/test_worker.py
git commit -m "feat(worker): filter candidates with Groq relevance check instead of cosine threshold"
```

---

## Self-Review Notes (for the plan author, already applied above)

- **Spec coverage:** retrieval/filter split (Task 1), Groq module + prompts (Task 2), worker integration + config + metrics + fail-closed handling + env/compose wiring (Task 3) — all spec sections have a home. Audit table (not JSONL) is Task 1. Testing section requirements (unit tests for parsing, unit test for `process_article` filtering, DB integration test) are covered by the three test files.
- **Placeholder scan:** no TBD/TODO; every step has complete code.
- **Type consistency:** `check_relevance` returns `{"relevant": bool, "reasoning": str}` or `{"error", "raw"}` consistently across Task 2's implementation, Task 2's tests, and Task 3's `process_article` usage. `Db.log_relevance_check`'s positional argument order (`article_url, article_title, market_id, relevant, reasoning, model`) matches between Task 1's implementation and Task 3's call sites/tests.
